"""The hub daemon (plan sections 4.1, 4.3, 12): the one long-lived process.

Every `jarvis` command is a fresh short-lived process, so nothing that must stay connected
for minutes (a 4 GB transfer, a stalled retry waiting for its turn) can live in a tool
call.  This process is where downloads actually happen:

  tick()      one pass, called about once a second:
                1. reap finished worker threads
                2. apply control requests (pause / cancel) written by tool calls
                3. start workers for waiting items, within the concurrency limits
                4. announce what just finished / failed (notify.py, through the existing
                   notifier) and do each download's "when it finishes" action (service.py)
                5. every ~5 s: heartbeat, apply spooled hub_client reports, flag silent
                   provider rows stale, advance the demo provider

  worker      one thread per running item: runs the engine, then turns its Outcome into
              a state.  THE retry policy lives in `_finish` and nowhere else.

On start every row a previous daemon left running goes back to `queued` and the engine
resumes it (HTTP `Range` / yt-dlp `.part`); a stop (SIGTERM / SIGINT / SIGBREAK) asks the
workers to hand their items back the same way.  Provider rows are not touched by any of
this: they are somebody else's transfer, so they simply go stale when nobody reports.

Run as `python <tool dir>/_hub_daemon.py` (what DAEMONS declares) or `python -m jarvis.hub.daemon`.
"""

import collections
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

from . import engines, notify, paths, service, settings as settings_mod, states
from .engines.base import EngineContext, Outcome
from .store import Store

log = logging.getLogger("jarvis.hub")

TICK_BUSY = 0.5
TICK_IDLE = 1.0
HOUSEKEEPING_EVERY = 5.0
DEMO_EVERY = 1.0
SETTINGS_TTL = 2.0
EXIT_ALREADY_RUNNING = 3


def pid_alive(pid):
    """Is that pid a running process?  psutil is a Jarvis dependency; os.kill(pid, 0) is
    only an existence probe on POSIX, so it is the fallback, not the first choice."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:  # noqa: BLE001 - an absurd pid must read as "not running"
            return False
    if sys.platform.startswith("win"):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Worker(threading.Thread):
    """One running item.  `stop_reason` is written by the supervisor, read by the engine."""

    def __init__(self, daemon, item):
        super().__init__(name="hub-%s" % item["id"], daemon=True)
        self.hub = daemon
        self.item_id = item["id"]
        self.engine_name = item["engine"]
        self.limit_kbps = settings_mod.item_limit_kbps(item)    # this download's own cap, KB/s
        self.stop_reason = None

    def reason(self):
        if self.hub.shutdown.is_set():
            return "shutdown"
        return self.stop_reason

    def run(self):
        try:
            self.hub.run_item(self)
        except Exception:  # noqa: BLE001 - a worker must never die silently
            log.exception("worker for %s crashed", self.item_id)


class HubDaemon:
    def __init__(self, store=None, clock=time.time, deliver=None):
        self.store = store or Store()
        self._clock = clock
        self.workers = {}
        self.shutdown = threading.Event()
        self.pid = os.getpid()
        self.started_at = clock()
        self._settings = None
        self._settings_at = 0.0
        self._last_housekeeping = 0.0
        self._last_demo = 0.0
        self._notifier = notify.Dispatcher(self.store, deliver=deliver, clock=clock)
        self._recent_opens = collections.deque()
        self._rates, self._rates_key, self._rates_at = {}, None, 0.0
        self._rates_lock = threading.Lock()

    # ------------------------------------------------------------------ settings

    def settings(self):
        now = time.monotonic()
        if self._settings is None or now - self._settings_at > SETTINGS_TTL:
            self._settings = self.store.get_settings()
            self._settings_at = now
        return self._settings

    def _caps(self):
        conc = dict(self.settings()["concurrency"])
        total = max(1, int(conc.pop("total", 4)))
        conc["default"] = 2
        return conc, total

    def _global_bps(self):
        """The speed limit in force right now (the owner's, or a schedule window's), bytes/s; 0 = none."""
        kbps, _source = settings_mod.effective_rate_limit_kbps(self.settings())
        return max(0, int(kbps)) * 1024

    def _rate_share(self, engine_name=None):
        """The global limit split evenly over every running transfer, bytes per second."""
        bps = self._global_bps()
        if bps <= 0:
            return 0
        running = sum(1 for w in list(self.workers.values()) if w.is_alive())
        return int(bps / max(1, running))

    def _rate_for(self, worker):
        """What one running download may use, bytes/s (0 = unlimited).

        The global limit is shared out fairly, but a download with a cap of its own never takes
        more than that, and what it leaves unused goes to the others (so one capped download
        doesn't waste the rest of the bandwidth the owner allowed).
        """
        with self._rates_lock:
            now = time.monotonic()
            live = sorted(w.item_id for w in list(self.workers.values()) if w.is_alive())
            key = (tuple(live), self._global_bps())
            if key != self._rates_key or now - self._rates_at >= 1.0:
                self._rates_key, self._rates_at = key, now
                self._rates = self._compute_rates(live, key[1])
            return self._rates.get(worker.item_id, 0)

    def _compute_rates(self, live, global_bps):
        caps = {}
        for item_id in live:
            worker = self.workers.get(item_id)
            if worker is None:
                continue
            try:                                  # an edit made while it runs takes effect within a second
                fresh = self.store.get_item(item_id)
                worker.limit_kbps = settings_mod.item_limit_kbps(fresh) if fresh else worker.limit_kbps
            except Exception:  # noqa: BLE001
                pass
            caps[item_id] = worker.limit_kbps * 1024
        if global_bps <= 0:
            return dict(caps)                     # no global limit: just each one's own cap (0 = none)
        rates, remaining = {}, float(global_bps)
        ordered = sorted(caps, key=lambda i: (caps[i] == 0, caps[i]))     # smallest caps first, uncapped last
        for position, item_id in enumerate(ordered):
            share = remaining / (len(ordered) - position)
            give = min(caps[item_id], share) if caps[item_id] else share
            rates[item_id] = int(give)
            remaining -= give
        return rates

    # ------------------------------------------------------------------ the loop

    def start_up(self):
        moved = self.store.recover_after_restart()
        if moved:
            log.info("recovered %d download(s) a previous run left active; they resume", moved)
        self.store.daemon_beat(self.pid, self.started_at)
        loaded = engines.availability()
        for name, info in loaded.items():
            log.info("engine %-8s %s", name, "ready" if info["available"] else "NOT ready: " + info["reason"])
        for name, why in engines.LOAD_ERRORS.items():
            log.error("engine module %s failed to load: %s", name, why)

    def tick(self):
        self._reap()
        self._apply_controls()
        if not self.shutdown.is_set():
            self._schedule()
        self._finish_pass()
        now = time.monotonic()
        if now - self._last_housekeeping >= HOUSEKEEPING_EVERY:
            self._last_housekeeping = now
            self._housekeeping()
        if now - self._last_demo >= DEMO_EVERY:
            self._last_demo = now
            self._demo()

    def run(self, max_ticks=None):
        self.start_up()
        ticks = 0
        while not self.shutdown.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything a tick throws
                log.exception("tick failed")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            self.shutdown.wait(TICK_BUSY if self.workers else TICK_IDLE)
        self.stop()

    def stop(self, timeout=15.0):
        """Ask every worker to hand its item back, wait for them, mark the heartbeat dead."""
        self.shutdown.set()
        deadline = time.monotonic() + timeout
        for worker in list(self.workers.values()):
            worker.join(max(0.1, deadline - time.monotonic()))
        self._reap()
        try:
            self._finish_pass()                              # whatever finished while stopping
            self._notifier.flush_now(self.settings())        # and anything still being held back
        except Exception:  # noqa: BLE001
            log.exception("final notification flush failed")
        try:
            self.store.set_setting_raw("daemon", {"pid": self.pid, "started_at": self.started_at, "beat_at": 0})
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ pieces of a tick

    def _reap(self):
        for item_id, worker in list(self.workers.items()):
            if not worker.is_alive():
                del self.workers[item_id]

    def _apply_controls(self):
        for request in self.store.pending_controls():
            worker = self.workers.get(request["id"])
            if worker is not None and worker.is_alive():
                worker.stop_reason = request["control"]
                continue
            item = self.store.get_item(request["id"])
            if item is not None:
                service.apply_control_directly(self.store, item)

    def _schedule(self):
        caps, total = self._caps()
        while len(self.workers) < total:
            item = self.store.claim_next(caps, total)
            if item is None:
                return
            worker = Worker(self, item)
            self.workers[item["id"]] = worker
            worker.start()

    def _finish_pass(self):
        """Announce what just finished, then run each finished download's open-when-done action."""
        try:
            cfg = self.settings()
            self._notifier.scan(cfg)
            for item_id, what in service.run_auto_open(self.store, cfg, self._notifier.since(),
                                                       self._recent_opens, now=self._clock()):
                log.info("after-download action on %s: %s", item_id, what)
        except Exception:  # noqa: BLE001 - never let a notification stop the downloads
            log.exception("the finish pass failed")

    def _housekeeping(self):
        self.store.daemon_beat(self.pid, self.started_at)
        try:
            from .. import hub_client
            applied = hub_client.drain_inbox(self.store)
            if applied:
                log.info("applied %d spooled report(s)", applied)
        except Exception:  # noqa: BLE001
            log.exception("draining the inbox failed")
        limits = self.settings()["provider_limits"]
        flagged = self.store.mark_stale(stale_after=float(limits["stale_after_s"]))
        if flagged["stale"] or flagged["unknown"]:
            log.info("provider rows: %d went stale, %d became unknown", flagged["stale"], flagged["unknown"])

    def _demo(self):
        if not self.settings().get("demo", {}).get("enabled"):
            return
        try:
            from . import demo
            demo.tick(self.store, self._clock())
        except Exception:  # noqa: BLE001
            log.exception("demo provider tick failed")

    # ------------------------------------------------------------------ one item

    def run_item(self, worker):
        item = self.store.get_item(worker.item_id)
        if item is None:
            return
        engine = engines.get(item["engine"])
        if engine is None:
            outcome = Outcome.failed("no engine called %r is installed" % item["engine"])
        else:
            ok, why = engine.available()
            if not ok:
                outcome = Outcome.failed("the %s engine isn't ready: %s" % (engine.name, why or "unavailable"))
            else:
                ctx = EngineContext(self.store, item, self.settings(), stop_reason=worker.reason,
                                    rate_limit_bps=lambda: self._rate_for(worker))
                try:
                    outcome = engine.run(item, ctx)
                except Exception as exc:  # noqa: BLE001
                    log.exception("engine %s crashed on %s", engine.name, item["id"])
                    outcome = Outcome.failed("the %s engine crashed: %s" % (engine.name, exc))
        self._finish(worker, outcome)

    def _max_retries(self, engine_name):
        block = self.settings().get(engine_name)
        if isinstance(block, dict) and "max_retries" in block:
            try:
                return max(0, int(block["max_retries"]))
            except (TypeError, ValueError):
                pass
        return 3

    def _finish(self, worker, outcome):
        """Turn an Outcome into a state.  The ONE place retry / stall / restart rules live."""
        store = self.store
        item = store.get_item(worker.item_id)
        if item is None:
            return
        active = states.ACTIVE_STATES
        now = self._clock()
        kind = outcome.kind

        if kind == Outcome.DONE:
            fields = {"percent": 100.0, "message": None, "error": None}
            if outcome.dest_file:
                fields["dest_file"] = outcome.dest_file
            moved = store.transition(item["id"], "done", expect=active, event="finished", **fields)
        elif kind == Outcome.PAUSED:
            moved = store.transition(item["id"], "paused", expect=active, message=None)
        elif kind == Outcome.CANCELLED:
            moved = store.transition(item["id"], "cancelled", expect=active, message=None)
            if moved:
                service._discard_part(store, item)
        elif kind == Outcome.REQUEUE:
            moved = store.transition(item["id"], "queued", expect=active, message="resuming after a restart")
        elif kind in (Outcome.FAILED, Outcome.STALLED) and outcome.retryable \
                and item["attempts"] <= self._max_retries(item["engine"]):
            delay = outcome.delay_s if outcome.delay_s else states.backoff_seconds(item["attempts"])
            retries = self._max_retries(item["engine"])
            note = "%s; retry %d of %d in %d s" % (outcome.error, item["attempts"], retries, int(delay))
            # A stall stays amber (stalled) while it waits; a dropped connection just waits queued.
            target = "stalled" if kind == Outcome.STALLED else "queued"
            moved = store.transition(item["id"], target, expect=active, message=note,
                                     next_attempt_at=now + delay, error=None)
        else:
            error = outcome.error or "failed"
            if kind == Outcome.STALLED:
                error = "stalled: %s" % error
            moved = store.transition(item["id"], "failed", expect=active, error=error, message=None)
            if moved:
                log.warning("%s failed: %s", item["id"], error)
        if moved is None:
            log.info("%s: the %s result was not applied (the row changed while it ran)", item["id"], kind)


# --------------------------------------------------------------------------- entry point

def _setup_logging():
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stderr)      # stdout stays clean: the Daemons console reads both
    stream.setFormatter(fmt)
    log.addHandler(stream)
    try:
        paths.hub_dir().mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            str(paths.log_path()), maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        rotating.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(rotating)
    except OSError:
        pass


def main(argv=None):
    _setup_logging()
    store = Store()
    info = store.daemon_info()
    if info["alive"] and info["pid"] not in (None, os.getpid()) and pid_alive(info["pid"]):
        log.error("another hub daemon (pid %s) is already running; exiting", info["pid"])
        return EXIT_ALREADY_RUNNING
    daemon = HubDaemon(store)

    def _stop(signum, frame):
        daemon.shutdown.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass
    log.info("download hub daemon starting (pid %d, library root %s)",
             daemon.pid, settings_mod.library_root(store.get_settings()))
    daemon.run()
    log.info("download hub daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
