"""Download notifications: WHEN to speak.  How a message is delivered is not decided here.

Every message goes out through the existing notifier (`jarvis/notifier.py`, `notify()`), so it
reaches the inbox, the in-app toast, the desktop notification, voice and the phone exactly the
way every other Jarvis notification does, chosen by the notifier's importance level and the
owner's notify_config.json.  This module never opens a channel of its own.

What it does own:
  * the event types (started, done, failed, stalled, group_done, queue_empty) and the owner's
    on/off + optional level for each (hub settings `notify.*`);
  * telling one occurrence from the next, so a daemon restart or a second scan can never
    announce the same finish twice (the `notices` table);
  * folding a burst of finishes into one message (`notify.coalesce_s`);
  * the wording, and the per-download override (`options.notify`: "on" / "off").

The daemon calls `Dispatcher.scan()` once a tick; everything else is plain functions so the
tests can drive it with a fake clock and a fake `deliver`.
"""

import os
import time

from . import states

EVENT_LABELS = {
    "started": "Download started",
    "done": "Download finished",
    "failed": "Download failed",
    "stalled": "Download stalled",
    "group_done": "Batch finished",
    "queue_empty": "All downloads finished",
}
# Events that are held for `coalesce_s` and merged; the other two go out at once (after flushing).
_COALESCED = ("started", "done", "failed", "stalled")
_SHOW_TITLES = 3


def default_deliver(title, message, *, level=None, failed=False):
    """Hand one message to the existing notifier.  Never raises; returns the notifier's record.

    JARVIS_HUB_NO_DELIVER=1 (set by the test-suite) turns this into a no-op, so a test that runs
    the daemon can't pop a real desktop notification on the developer's machine.
    """
    if os.environ.get("JARVIS_HUB_NO_DELIVER"):
        return {"delivered": [], "failed_channels": []}
    try:
        from .. import notifier
        kwargs = {"level": level} if level else {}
        return notifier.notify(title, message, kind="download", source="download_hub",
                               failed=bool(failed), **kwargs)
    except Exception as exc:                          # a notification must never break the daemon
        return {"delivered": [], "failed_channels": ["notifier"], "error": str(exc)[:200]}


def _human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def _name(item):
    return (item.get("title") or item.get("filename") or item.get("source") or "download")[:90]


def compose(event, entries, extra=None):
    """(title, message) for one event; `entries` is a list of small dicts about the rows."""
    extra = extra or {}
    n = len(entries)
    if event == "group_done":
        done, failed = extra.get("done", 0), extra.get("failed", 0)
        bits = ["%d finished" % done] + (["%d failed" % failed] if failed else [])
        return ("Batch finished", "%s: %s." % (extra.get("title") or "Batch", ", ".join(bits)))
    if event == "queue_empty":
        done, failed = extra.get("done", 0), extra.get("failed", 0)
        bits = ["%d finished" % done] + (["%d failed" % failed] if failed else [])
        return ("All downloads finished", ", ".join(bits).capitalize() + ".")
    label = EVENT_LABELS[event]
    if n == 1:
        e = entries[0]
        if event == "done":
            tail = " (%s)" % _human_bytes(e["size"]) if e.get("size") else ""
            where = (" Saved in %s." % e["folder"]) if e.get("folder") else ""
            return (label, "%s%s.%s" % (e["name"], tail, where))
        if event == "failed":
            return (label, "%s: %s" % (e["name"], e.get("error") or "it didn't finish"))
        if event == "stalled":
            return (label, "%s stopped receiving data; the hub will retry." % e["name"])
        return (label, e["name"])
    plural = {"started": "%d downloads started", "done": "%d downloads finished",
              "failed": "%d downloads failed", "stalled": "%d downloads stalled"}[event] % n
    names = [e["name"] for e in entries[:_SHOW_TITLES]]
    more = n - len(names)
    return (plural, ", ".join(names) + (" and %d more" % more if more > 0 else ""))


class Dispatcher:
    """One per daemon.  `deliver(title, message, level=, failed=)` defaults to the notifier."""

    def __init__(self, store, deliver=None, clock=None):
        self.store = store
        self.deliver = deliver or default_deliver
        self.clock = clock or time.time
        self._pending = {}                  # event -> {"first": ts, "entries": [...], "failed": bool}
        self._was_busy = False
        self._burst = {"done": 0, "failed": 0}   # finishes since the queue last went idle
        self._since = None

    # ------------------------------------------------------------ baseline

    def since(self):
        """When announcing began.  Rows that finished earlier are history and stay quiet."""
        if self._since is None:
            state = self.store.get_raw_setting("notify_state") or {}
            if not state.get("since"):
                state = dict(state, since=self.clock())
                self.store.set_setting_raw("notify_state", state)
            self._since = float(state["since"])
        return self._since

    # ------------------------------------------------------------ settings helpers

    @staticmethod
    def _event_cfg(cfg, event):
        block = (cfg.get("notify") or {}).get(event) or {}
        return bool(block.get("enabled")), block.get("level")

    # ------------------------------------------------------------ the pass

    def scan(self, settings):
        """Look for new finishes / failures / stalls / starts and announce what is due.
        Returns how many messages were handed to the notifier."""
        cfg = settings.get("notify") or {}
        if not cfg.get("enabled", True):
            self._pending.clear()
            self._was_busy = self.store.waiting_work() > 0
            return 0
        since = self.since()
        sent = 0
        queued_group_checks = []

        for state_name, event, column in (("done", "done", "finished_at"), ("failed", "failed", "finished_at")):
            for item in self.store.unclaimed(event, state_name, since=since, stamp_column=column):
                if not self.store.claim_notice(item["id"], event, item.get(column) or 0):
                    continue
                self._burst[event] += 1
                self._consider(event, item, cfg, settings)
                if item.get("group_id"):
                    queued_group_checks.append(item)
        for item in self.store.unclaimed("stalled", "stalled", since=since, stamp_column="attempts"):
            if self.store.claim_notice(item["id"], "stalled", item.get("attempts") or 0) and item["owner"] == "hub":
                self._consider("stalled", item, cfg, settings)
        for item in self.store.unclaimed("started", states.ACTIVE_STATES, since=since, stamp_column="started_at"):
            if self.store.claim_notice(item["id"], "started", item.get("started_at") or 0) and item["owner"] == "hub":
                self._consider("started", item, cfg, settings)

        sent += self._flush(cfg, force=False)

        for item in queued_group_checks:
            sent += self._group_done(item, cfg)
        sent += self._queue_empty(cfg)
        return sent

    # ------------------------------------------------------------ per-row decision

    def _consider(self, event, item, cfg, settings):
        mine = item["owner"] == "hub"
        if not mine and (event not in ("done", "failed") or not cfg.get("providers", True)):
            return
        override = (item.get("options") or {}).get("notify")
        if override == "off":
            return
        enabled, level = self._event_cfg(settings, event)
        forced = override == "on" and event in ("done", "failed")
        if not enabled and not forced:
            return
        if event == "done" and not forced:
            took = (item.get("finished_at") or 0) - (item.get("started_at") or item.get("created_at") or 0)
            if cfg.get("min_seconds", 0) and took < cfg["min_seconds"]:
                return
        entry = {"name": _name(item), "size": item.get("bytes_done") or item.get("bytes_total") or 0,
                 "folder": os.path.basename(item.get("dest_dir") or "") if item.get("dest_dir") else "",
                 "error": (item.get("error") or "")[:160]}
        if forced and not enabled:
            self._send(event, [entry], level or 2)        # asked for this one: say so now, plainly
            return
        bucket = self._pending.setdefault(event, {"first": self.clock(), "entries": [], "level": level})
        bucket["entries"].append(entry)
        if not cfg.get("coalesce_s", 0):
            self._flush(cfg, force=True)

    # ------------------------------------------------------------ sending

    def _send(self, event, entries, level, extra=None):
        title, message = compose(event, entries, extra)
        self.deliver(title, message, level=level, failed=(event == "failed"))
        return 1

    def _flush(self, cfg, force):
        wait = cfg.get("coalesce_s", 0)
        sent = 0
        for event in [e for e in _COALESCED if e in self._pending]:
            bucket = self._pending[event]
            if force or self.clock() - bucket["first"] >= wait:
                del self._pending[event]
                sent += self._send(event, bucket["entries"], bucket["level"])
        return sent

    def flush_now(self, settings):
        """Send whatever is being held (the daemon calls this on a clean shutdown)."""
        return self._flush(settings.get("notify") or {}, force=True)

    def _group_done(self, item, cfg):
        enabled, level = self._event_cfg({"notify": cfg}, "group_done")
        gid = item["group_id"]
        prog = self.store.group_progress(gid)
        if prog["total"] < 2 or prog["final"] < prog["total"] or prog["done"] + prog["failed"] == 0:
            return 0
        if not self.store.claim_notice("group:" + gid, "group_done", prog["last_finished"]):
            return 0
        if not enabled or item["owner"] != "hub":
            return 0
        sent = self._flush(cfg, force=True)             # the per-file lines go first
        return sent + self._send("group_done", [], level, prog)

    def _queue_empty(self, cfg):
        busy = self.store.waiting_work() > 0
        sent = 0
        if self._was_busy and not busy and (self._burst["done"] + self._burst["failed"]) > 0:
            enabled, level = self._event_cfg({"notify": cfg}, "queue_empty")
            if enabled:
                sent += self._flush(cfg, force=True)
                sent += self._send("queue_empty", [], level, dict(self._burst))
            self._burst = {"done": 0, "failed": 0}
        if not busy and not self._was_busy:
            self._burst = {"done": 0, "failed": 0}
        self._was_busy = busy
        return sent

    # ------------------------------------------------------------ "Send test"

    def send_test(self, event, settings):
        """Fire a sample of one event through the real notifier, ignoring its on/off switch, and
        return the notifier's record so the panel can say which channels actually delivered."""
        if event not in EVENT_LABELS:
            raise ValueError("unknown notification type: %s" % event)
        _enabled, level = self._event_cfg(settings, event)
        sample = {"name": "Example file.zip", "size": 734_003_200, "folder": "Files",
                  "error": "the server closed the connection"}
        title, message = compose(event, [sample], {"title": "Example batch", "done": 3, "failed": 0})
        record = self.deliver("Test: " + title, message + " (this is only a test)",
                              level=level, failed=(event == "failed"))
        return record or {}
