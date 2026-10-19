"""The contract every hub engine follows (plan section 7).

An engine does ONE thing: given an item, move its bytes, calling `ctx.update(...)` with
progress as it goes, and finally return an `Outcome`.  It never writes a state of its own
choosing after the run starts (apart from the active ones: probing -> connecting ->
downloading -> postprocessing) -- the daemon turns the Outcome into done / paused /
cancelled / failed / a retry.  That is deliberate: retry counts, backoff, stall handling
and "what happens when the daemon restarts" live in ONE place (daemon.py), so a new
engine (yt-dlp, Stremio, qBittorrent) can't implement them slightly differently.

An engine runs in a worker thread and must:
  * poll `ctx.stop_requested()` often (every chunk) and return the matching Outcome;
  * never raise past `run` (the daemon catches it and fails the item, but a clean Outcome
    carries a clearer message);
  * never write outside `ctx.library_root()`;
  * treat everything from the network as untrusted (names, headers, URLs).
"""

import time
from collections import namedtuple


class Outcome:
    """How a run ended.  Build one with the class methods."""

    DONE = "done"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"
    STALLED = "stalled"
    REQUEUE = "requeue"          # the daemon is shutting down: put it back, resume later

    __slots__ = ("kind", "error", "retryable", "dest_file", "delay_s")

    def __init__(self, kind, error=None, retryable=False, dest_file=None, delay_s=None):
        self.kind = kind
        self.error = error
        self.retryable = retryable
        self.dest_file = dest_file
        self.delay_s = delay_s

    @classmethod
    def done(cls, dest_file=None):
        return cls(cls.DONE, dest_file=dest_file)

    @classmethod
    def paused(cls):
        return cls(cls.PAUSED)

    @classmethod
    def cancelled(cls):
        return cls(cls.CANCELLED)

    @classmethod
    def requeue(cls):
        return cls(cls.REQUEUE)

    @classmethod
    def failed(cls, error, retryable=False, delay_s=None):
        """`retryable`: a connection problem or a 5xx -- the daemon retries with backoff up
        to max_retries.  Not retryable: a 404, a refused URL, a full disk."""
        return cls(cls.FAILED, error=str(error), retryable=bool(retryable), delay_s=delay_s)

    @classmethod
    def stalled(cls, reason):
        """No data for too long.  Shown amber while it waits for its retry."""
        return cls(cls.STALLED, error=str(reason), retryable=True)

    def __repr__(self):
        return "Outcome(%s%s)" % (self.kind, ", %r" % self.error if self.error else "")


# match(text, settings) -> None or {"reason": str, "kind": "video"|..., "source": canonical text}
RouteRule = namedtuple("RouteRule", "engine priority match")


class Engine:
    """Subclass, set `name`, implement `run`, export an instance as `ENGINE`."""

    name = ""
    label = ""
    # What the panel may offer for a row this engine owns.  The daemon only honours
    # pause / cancel for engines that say so.
    supports_pause = True

    def available(self):
        """(ok, reason).  `reason` says what is missing (ffmpeg, the Stremio server ...)."""
        return True, ""

    def run(self, item, ctx):
        raise NotImplementedError


class EngineContext:
    """What a running engine can see and do.  The daemon builds one per attempt."""

    def __init__(self, store, item, settings, stop_reason=None, rate_limit_bps=None):
        self.store = store
        self.item = item
        self.item_id = item["id"]
        self.settings = settings
        self._stop_reason = stop_reason or (lambda: None)
        self._rate_limit = rate_limit_bps or (lambda: 0)

    # -- writing ---------------------------------------------------------------
    def update(self, **fields):
        """Progress and details (bytes_done, percent, speed_bps, title, dest_file ...).
        Returns False if the row has gone away (deleted while running)."""
        return self.store.update_item(self.item_id, fields) is not None

    def progress(self, bytes_done=None, bytes_total=None, speed_bps=None, eta_s=None,
                 estimated=False, **more):
        """Report transfer progress by the plan-5.4 rules in one call (additive helper; the
        plain `update` keeps working exactly as before).

        What an argument left as None means: "I don't know it, let the hub work it out".  The
        hub then derives it from the bytes and marks it "≈" in the panel, so a number the engine
        measured and a number the hub guessed never look the same.

          * `bytes_total` known and exact: the percent is reported as exact (no "≈").
          * `estimated=True`: the total is itself a guess (a stream whose size is only an
            estimate, a plan of several streams).  Percent and ETA are then left for the hub to
            derive from the bytes, so they show "≈".
          * no `bytes_total`: an indeterminate bar (`percent` = None) with the bytes so far.

        Anything else (`phase`, `phase_index`, `message`, `title` ...) goes in `more`.
        Returns False if the row has gone away, like `update`.
        """
        fields = dict(more)
        done = None
        if bytes_done is not None:
            done = max(0, int(bytes_done))
            fields["bytes_done"] = done
        if bytes_total:
            total = max(int(bytes_total), done or 0)
            fields["bytes_total"] = total
            if not estimated and done is not None:
                fields["percent"] = min(100.0, done * 100.0 / total) if total else None
        else:
            fields["percent"] = None
        if speed_bps is not None:
            fields["speed_bps"] = speed_bps
        if eta_s is not None and not estimated:
            fields["eta_s"] = eta_s
        return self.update(**fields)

    def set_state(self, state, **fields):
        """Move between the active states (probing / connecting / downloading /
        postprocessing).  Returns False if that move isn't allowed right now."""
        from .. import states
        if state not in states.ACTIVE_STATES:
            return False
        return self.store.transition(self.item_id, state, expect=states.ACTIVE_STATES, **fields) is not None

    def event(self, level, message):
        self.store.add_event(self.item_id, level, message)

    # -- reading ---------------------------------------------------------------
    def stop_requested(self):
        """None, or "pause" / "cancel" / "shutdown".  Cheap: poll it every chunk."""
        return self._stop_reason()

    def rate_limit_bps(self):
        """This item's share of the global rate limit in bytes/s (0 = unlimited)."""
        try:
            return int(self._rate_limit() or 0)
        except (TypeError, ValueError):
            return 0

    def setting(self, *path, default=None):
        node = self.settings
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def library_root(self):
        from .. import settings as settings_mod
        return settings_mod.library_root(self.settings)

    def monotonic(self):
        return time.monotonic()
