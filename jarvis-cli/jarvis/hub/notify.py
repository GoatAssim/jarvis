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
  * the wording, and the per-download overrides, which beat the global settings for that one row:
      options.notify           "on" / "off"
      options.notify_channels  where THIS download is announced (e.g. ["discord"]); the notifier
                               still keeps its inbox record, the other channels stay quiet
      options.notify_events    which of started / done / failed / stalled fire for it
    A download with its own choices is announced even when the global switch is off, and is never
    merged into another download's message (it can be going somewhere else).
  * the one provider rule: a STEAM UPDATE is announced only when it is big enough.  The owner's
    threshold is the Steam provider's own setting `notify_min_update_gb` (settings.py: default
    1 GB, 0 = every update, changed with `dl_provider_prefs`).  Installs of new games are not
    subject to it and follow the normal `notify.done` rules.  Steam failures are never held back.

The daemon calls `Dispatcher.scan()` once a tick; everything else is plain functions so the
tests can drive it with a fake clock and a fake `deliver`.
"""

import os
import time

from . import settings as settings_mod, states

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
# How each event is DRAWN on the open web page. The notifier's small toast (look "compact", the
# same one-liner as "Autostart on" in the Daemons panel) for the three quiet ones, in the tone
# the toast shows as its mark: `started` is information, the other two are good news.
# `done`, `failed` and `stalled` are not listed, so they stay full cards. A download that chose
# its own channels still gets exactly those (explicit channels beat a look), and the notifier
# draws a failure, anything persistent and anything needing acknowledgment as a card whatever
# is asked for here. The ids are notify_registry.LOOKS / TONES (the notifier ignores an
# unknown one).
EVENT_LOOKS = {
    "started": ("compact", "info"),
    "group_done": ("compact", "success"),
    "queue_empty": ("compact", "success"),
}


def _look_kwargs(event):
    look = EVENT_LOOKS.get(event)
    return {"look": look[0], "tone": look[1]} if look else {}
_SHOW_TITLES = 3
_GAME_ACTIONS = {"install": "installing", "update": "updating"}


def game_action(item):
    """'install' / 'update' for a game row a provider reports (Steam says which in `subtitle`),
    else ''.  Only provider-owned game rows: a hub download is never called an install."""
    if item.get("owner") == "hub" or item.get("kind") != "game":
        return ""
    word = str(item.get("subtitle") or "").strip().lower()
    return word if word in _GAME_ACTIONS else ""


def item_bytes(item):
    """How big the row is: the larger of what was fetched and what was announced to fetch."""
    return max(int(item.get("bytes_total") or 0), int(item.get("bytes_done") or 0))


def below_update_threshold(item, provider_settings):
    """True when this is a Steam UPDATE smaller than the owner's `notify_min_update_gb`.

    A size of 0 (Steam sometimes runs a queued background update with nothing to fetch) is below
    any threshold above 0, so it stays quiet unless the owner asked for every update.  Anything
    that is not a Steam update (an install, another provider, a hub download) is never below it.
    """
    if item.get("provider") != "steam" or game_action(item) != "update":
        return False
    return item_bytes(item) < settings_mod.steam_min_update_bytes(provider_settings)


def default_deliver(title, message, *, level=None, failed=False, channels=None, look=None, tone=None):
    """Hand one message to the existing notifier.  Never raises; returns the notifier's record.

    JARVIS_HUB_NO_DELIVER=1 (set by the test-suite) turns this into a no-op, so a test that runs
    the daemon can't pop a real desktop notification on the developer's machine.
    """
    if os.environ.get("JARVIS_HUB_NO_DELIVER"):
        return {"delivered": [], "failed_channels": []}
    try:
        from .. import notifier
        kwargs = {"level": level} if level else {}
        if channels:
            kwargs["channels"] = list(channels)
        if look:
            kwargs["look"] = look
        if tone:
            kwargs["tone"] = tone
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
            if e.get("action") in _GAME_ACTIONS:       # "Hades II finished installing."
                return ("Game %s" % ("installed" if e["action"] == "install" else "updated"),
                        "%s finished %s%s." % (e["name"], _GAME_ACTIONS[e["action"]], tail))
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


def _channels_of(item):
    """This row's own notification channels (a list), or None to follow the global setting."""
    value = (item.get("options") or {}).get("notify_channels")
    return list(value) if isinstance(value, (list, tuple)) and value else None


def _events_of(item):
    value = (item.get("options") or {}).get("notify_events")
    return list(value) if isinstance(value, (list, tuple)) and value else None


def _has_own_choice(item):
    """Did the owner pick notification settings for this one download?"""
    return (item.get("options") or {}).get("notify") == "on" or bool(_channels_of(item) or _events_of(item))


class Dispatcher:
    """One per daemon.  `deliver(title, message, level=, failed=)` defaults to the notifier."""

    def __init__(self, store, deliver=None, clock=None):
        self.store = store
        self.deliver = deliver or default_deliver
        self.clock = clock or time.time
        self._pending = {}                  # (event, channels) -> {"first": ts, "entries": [...], "level": n, "channels": [...]}
        self._was_busy = False
        self._burst = {"done": 0, "failed": 0}   # finishes since the queue last went idle
        self._since = None
        self._provider_cache = {}           # provider id -> its settings block, for one pass

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

    def _provider_settings(self, provider_id):
        """A provider's own settings block (the owner's choices), read once per pass."""
        if provider_id not in self._provider_cache:
            try:
                self._provider_cache[provider_id] = (self.store.get_provider(provider_id) or {}).get("settings") or {}
            except Exception:  # noqa: BLE001 - a notification must never break the daemon
                self._provider_cache[provider_id] = {}
        return self._provider_cache[provider_id]

    def _quiet_update(self, event, item):
        """Is this a finished Steam update that is too small to announce?"""
        return (event == "done" and item.get("owner") != "hub"
                and below_update_threshold(item, self._provider_settings(item.get("provider"))))

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
        master = bool(cfg.get("enabled", True))
        self._provider_cache = {}                         # the owner may have changed a threshold since the last pass
        since = self.since()
        sent = 0
        queued_group_checks = []

        for state_name, event, column in (("done", "done", "finished_at"), ("failed", "failed", "finished_at")):
            for item in self.store.unclaimed(event, state_name, since=since, stamp_column=column):
                if not self.store.claim_notice(item["id"], event, item.get(column) or 0):
                    continue
                if not _channels_of(item) and not self._quiet_update(event, item):
                    # a download announced elsewhere, or a small Steam update nobody is told about,
                    # isn't part of the queue's tally
                    self._burst[event] += 1
                self._consider(event, item, cfg, settings, master)
                if item.get("group_id"):
                    queued_group_checks.append(item)
        for item in self.store.unclaimed("stalled", "stalled", since=since, stamp_column="attempts"):
            if self.store.claim_notice(item["id"], "stalled", item.get("attempts") or 0) and item["owner"] == "hub":
                self._consider("stalled", item, cfg, settings, master)
        for item in self.store.unclaimed("started", states.ACTIVE_STATES, since=since, stamp_column="started_at"):
            if self.store.claim_notice(item["id"], "started", item.get("started_at") or 0) and item["owner"] == "hub":
                self._consider("started", item, cfg, settings, master)

        sent += self._flush(cfg, force=False)

        if not master:
            # The global switch is off: only downloads with their own choices spoke (above).  No batch
            # or "all done" messages, and nothing is held for later.
            self._was_busy = self.store.waiting_work() > 0
            self._burst = {"done": 0, "failed": 0}
            return sent
        for item in queued_group_checks:
            sent += self._group_done(item, cfg)
        sent += self._queue_empty(cfg)
        return sent

    # ------------------------------------------------------------ per-row decision

    def _consider(self, event, item, cfg, settings, master=True):
        mine = item["owner"] == "hub"
        if not mine and (event not in ("done", "failed") or not cfg.get("providers", True)):
            return
        if self._quiet_update(event, item):
            return                                       # a Steam update under the owner's size threshold
        options = item.get("options") or {}
        override = options.get("notify")
        if override == "off":
            return
        own_events, channels = _events_of(item), _channels_of(item)
        if not master and not (mine and _has_own_choice(item)):
            return                                       # global switch off, nothing asked for this one
        enabled, level = self._event_cfg(settings, event)
        if own_events is not None:
            # This download's own list of types replaces the global on/off for it.
            enabled = forced = event in own_events
        else:
            # While the global switch is off nothing can say which types this one wants, so a download
            # that picked channels (or "on") is announced when it finishes or fails.
            forced = (override == "on" or (bool(channels) and not master)) and event in ("done", "failed")
            if not master:
                enabled = False
        if not enabled and not forced:
            return
        if event == "done" and not forced:
            took = (item.get("finished_at") or 0) - (item.get("started_at") or item.get("created_at") or 0)
            if cfg.get("min_seconds", 0) and took < cfg["min_seconds"]:
                return
        entry = {"name": _name(item), "size": item_bytes(item),
                 "action": game_action(item),
                 "folder": os.path.basename(item.get("dest_dir") or "") if item.get("dest_dir") else "",
                 "error": (item.get("error") or "")[:160]}
        if forced and not self._event_cfg(settings, event)[0]:
            self._send(event, [entry], level or 2, channels=channels)    # asked for this one: say so now, plainly
            return
        key = (event, tuple(channels) if channels else None)
        bucket = self._pending.setdefault(key, {"first": self.clock(), "entries": [], "level": level,
                                                "channels": channels})
        bucket["entries"].append(entry)
        if not cfg.get("coalesce_s", 0):
            self._flush(cfg, force=True)

    # ------------------------------------------------------------ sending

    def _send(self, event, entries, level, extra=None, channels=None):
        title, message = compose(event, entries, extra)
        look = _look_kwargs(event)          # only the three small-toast events pass anything
        if channels:
            # Only passed when this download chose its own channels, so a plain deliver(title,
            # message, level=, failed=) keeps working everywhere else.
            self.deliver(title, message, level=level, failed=(event == "failed"), channels=list(channels), **look)
        else:
            self.deliver(title, message, level=level, failed=(event == "failed"), **look)
        return 1

    def _flush(self, cfg, force):
        wait = cfg.get("coalesce_s", 0)
        sent = 0
        for key in [k for k in list(self._pending) if k[0] in _COALESCED]:
            bucket = self._pending[key]
            if force or self.clock() - bucket["first"] >= wait:
                del self._pending[key]
                sent += self._send(key[0], bucket["entries"], bucket["level"], channels=bucket.get("channels"))
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
        return sent + self._send("group_done", [], level, prog, channels=_channels_of(item))

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
                              level=level, failed=(event == "failed"), **_look_kwargs(event))
        return record or {}
