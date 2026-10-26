"""The Download Hub's reporting client (plan section 5.6).

A tool that brings its own downloader (a vendor CLI, a model fetcher, a scraper ...) tells
the hub how it is going and the row shows up in Menu > Downloads next to everything else:

    from jarvis import hub_client as hub          # inside the handler / worker, not at module top

    item = hub.begin("model_fetch", local_id="llama-8b-q4", title="llama-8b-q4.gguf",
                     kind="file", total_bytes=4_900_000_000)
    for done, speed in my_downloader(...):
        item.progress(bytes_done=done, speed_bps=speed)      # percent is derived from the bytes
    item.phase("verifying", index=2, count=2)
    item.done(path="D:/Models/llama-8b-q4.gguf")             # or item.fail("checksum mismatch")

Works from ANY process (a handler, a DAEMONS worker, a plain script): it writes the shared
sqlite store directly.  The promises that make that safe to call from a download loop:

* It NEVER raises and never prints to stdout.  A download must not fail because the panel,
  the store or the daemon is down: a report that can't be written is spooled to
  ~/.jarvis/hub/inbox/ (the daemon applies it later) or, if even that fails, dropped.
* `begin` is idempotent per (provider, local_id): calling it again after a crash re-attaches
  to the same row instead of creating a duplicate.
* Reports are coalesced to at most one write per item per second, and a provider is held to
  20 writes a second (plan 5.4 item 5, 5.9); changes of STATE and the final report are
  always written at once.
* A provider writes only rows with its own id; the store enforces it (hub/store.py).  There
  is no model-facing way to call this module: only code the owner installed does.

Update findings (N23): "a newer version of X exists" is not a download, so it is not a row.  Code that is
not a tool (a worker, a script) reports one with

    hub.offer_update("model_fetch", "llama-8b-q4", title="llama-8b-q4.gguf",
                     installed="v1.4", available="v1.5", size_bytes=4_900_000_000, source="my-registry")
    hub.clear_update("model_fetch", "llama-8b-q4")          # it was updated (or is no longer offered)

Both follow the same promises as every call here: they NEVER raise, and a finding the store can't take
right now is spooled to the inbox and applied by the daemon later.  The hub never compares the two version
texts; it shows them as given.  A tool that has an `update_tool` in its TOOL_DOWNLOADS entry answers with a
list instead and needs neither call.
"""

import json
import sys
import threading
import time
import uuid

from .hub import paths, states

MIN_INTERVAL = 1.0          # per item, seconds
MAX_REPORTS_PER_S = 20      # per provider, per process
MAX_OFFERS_PER_S = 50       # update findings: a whole list arrives at once, so the burst is a provider's maximum
MAX_OFFER_BURST = 500
MAX_INBOX_FILES = 500

_UNSET = object()
_lock = threading.Lock()
_buckets = {}
_offer_buckets = {}
_warned = set()
_store_factory = None       # tests swap this for one pointed at a temp database


def set_store_factory(factory):
    global _store_factory
    _store_factory = factory


def _store():
    if _store_factory is not None:
        return _store_factory()
    from .hub.store import Store
    return Store()


def _warn_once(key, message):
    if key in _warned:
        return
    _warned.add(key)
    try:
        print("[hub_client] %s" % message, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _valid_provider(provider):
    if not isinstance(provider, str) or not states.PROVIDER_ID_RE.match(provider):
        _warn_once(("bad", str(provider)), "provider id %r must be lower_snake (a-z, 0-9, _)" % (provider,))
        return False
    if provider in states.RESERVED_PROVIDERS:
        _warn_once(("reserved", provider), "provider id %r is reserved for the hub's own engines" % provider)
        return False
    return True


def _allow(provider, now):
    """A token bucket per provider: MAX_REPORTS_PER_S writes a second, burst of the same."""
    with _lock:
        tokens, last = _buckets.get(provider, (float(MAX_REPORTS_PER_S), now))
        tokens = min(float(MAX_REPORTS_PER_S), tokens + (now - last) * MAX_REPORTS_PER_S)
        if tokens < 1.0:
            _buckets[provider] = (tokens, now)
            return False
        _buckets[provider] = (tokens - 1.0, now)
        return True


def _allow_offer(provider, now):
    """A token bucket for update findings (separate from the row bucket): a list of up to MAX_OFFER_BURST
    findings may arrive at once, then MAX_OFFERS_PER_S a second."""
    with _lock:
        tokens, last = _offer_buckets.get(provider, (float(MAX_OFFER_BURST), now))
        tokens = min(float(MAX_OFFER_BURST), tokens + (now - last) * MAX_OFFERS_PER_S)
        if tokens < 1.0:
            _offer_buckets[provider] = (tokens, now)
            return False
        _offer_buckets[provider] = (tokens - 1.0, now)
        return True


def _spool(provider, local_id, fields, create, kind=None):
    """Keep a report the store couldn't take, for the daemon to apply.  `kind` is None for a row report and
    \"offer_update\" / \"clear_update\" for an update finding (N23)."""
    try:
        inbox = paths.inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        if len(list(inbox.glob("*.json"))) >= MAX_INBOX_FILES:
            return False
        name = "%016d-%s.json" % (int(time.time() * 1000), uuid.uuid4().hex[:8])
        tmp = inbox / (name + ".tmp")
        body = {"provider": provider, "local_id": local_id, "fields": fields,
                "create": bool(create), "ts": time.time()}
        if kind:
            body["kind"] = kind
        tmp.write_text(json.dumps(body, default=str), encoding="utf-8")
        tmp.replace(inbox / name)
        return True
    except Exception:  # noqa: BLE001
        return False


def _write(provider, local_id, fields, create=True, force=False):
    """One report.  True if it reached the store (or the spool)."""
    try:
        if not _valid_provider(provider) or not local_id:
            return False
        if not force and not _allow(provider, time.monotonic()):
            return False
        try:
            item, _problems = _store().upsert_provider_row(provider, str(local_id), fields, create=create)
            return item is not None
        except Exception:  # noqa: BLE001 - locked, missing, corrupt: spool it
            return _spool(provider, str(local_id), fields, create)
    except Exception:  # noqa: BLE001
        return False


def drain_inbox(store=None, limit=200):
    """Apply spooled reports (oldest first), delete what was applied.  Returns the count.
    Called by the hub daemon; safe to call anywhere."""
    applied = 0
    try:
        inbox = paths.inbox_dir()
        if not inbox.is_dir():
            return 0
        store = store or _store()
        for path in sorted(inbox.glob("*.json"))[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                provider, local = data["provider"], data["local_id"]
                fields = data.get("fields") or {}
                create = bool(data.get("create", True))
                kind = data.get("kind")
                ts = float(data.get("ts") or time.time())
            except (OSError, ValueError, KeyError, TypeError):
                _unlink(path)                       # unreadable: nothing to retry
                continue
            if not _valid_provider(provider):
                _unlink(path)
                continue
            try:
                if kind == "offer_update":
                    store.update_offer(provider, local, fields, now=ts)
                elif kind == "clear_update":
                    store.update_clear(provider, local)
                else:
                    store.upsert_provider_row(provider, local, fields, create=create, now=ts)
            except Exception:  # noqa: BLE001 - still busy: leave the rest for next time
                break
            _unlink(path)
            applied += 1
    except Exception:  # noqa: BLE001
        pass
    return applied


def _unlink(path):
    try:
        path.unlink()
    except OSError:
        pass


class Item:
    """A handle on one provider row.  Every method is safe to call and returns nothing
    you must check; `ok` is False when the provider id was refused."""

    def __init__(self, provider, local_id, ok=True):
        self.provider = provider
        self.local_id = str(local_id)
        self.ok = ok
        self.id = "%s:%s" % (provider, self.local_id)
        self._pending = {}
        self._last_write = 0.0
        self._state = None

    def _queue(self, fields):
        for key, value in fields.items():
            if value is not _UNSET:
                self._pending[key] = value

    def flush(self):
        if not self.ok or not self._pending:
            return False
        fields, self._pending = self._pending, {}
        self._last_write = time.monotonic()
        if "state" in fields:
            self._state = fields["state"]
        return _write(self.provider, self.local_id, fields, force=True)

    def progress(self, bytes_done=None, bytes_total=None, percent=_UNSET, speed_bps=None,
                 eta_s=None, state=None, message=None, **extra):
        """Report how far along it is.  Pass only what you know.  `percent=None` says
        "running, can't tell how far" (an animated bar); leave it out to have the hub work
        it out from the bytes.  Coalesced to one write a second unless `state` changes."""
        try:
            fields = {"bytes_done": bytes_done, "bytes_total": bytes_total, "percent": percent,
                      "speed_bps": speed_bps, "eta_s": eta_s, "state": state, "message": message}
            self._queue({k: v for k, v in fields.items() if v is not None or k == "percent"})
            self._queue(extra)
            changed = state is not None and state != self._state
            if changed or time.monotonic() - self._last_write >= MIN_INTERVAL:
                return self.flush()
        except Exception:  # noqa: BLE001
            pass
        return False

    def phase(self, label, index=None, count=None):
        """"verifying", 2 of 2: the row shows one bar plus the phase, so 100 % never looks frozen."""
        try:
            self._queue({"phase": label, "phase_index": index, "phase_count": count,
                         "state": "postprocessing"})
            return self.flush()
        except Exception:  # noqa: BLE001
            return False

    def done(self, path=None):
        try:
            fields = {"state": "done", "message": None, "error": None}
            if path:
                fields["dest"] = str(path)
            self._queue(fields)
            return self.flush()
        except Exception:  # noqa: BLE001
            return False

    def fail(self, error="failed"):
        try:
            self._queue({"state": "failed", "error": str(error)})
            return self.flush()
        except Exception:  # noqa: BLE001
            return False

    def cancel(self):
        try:
            self._queue({"state": "cancelled"})
            return self.flush()
        except Exception:  # noqa: BLE001
            return False


def begin(provider, local_id, title="", kind=None, total_bytes=None, dest=None, subtitle=None,
          group=None, group_title=None, state=None, **fields):
    """Start (or re-attach to) a download row.  Returns an Item; never raises."""
    try:
        ok = _valid_provider(provider) and bool(str(local_id or "").strip())
        item = Item(provider, local_id, ok=ok)
        if not ok:
            return item
        first = {"title": title or None, "kind": kind, "bytes_total": total_bytes, "dest": dest,
                 "subtitle": subtitle, "group": group, "group_title": group_title, "state": state}
        first.update(fields)
        first = {k: v for k, v in first.items() if v is not None}
        _write(provider, item.local_id, first, create=True, force=True)
        item._state = first.get("state")
        item._last_write = time.monotonic()
        return item
    except Exception:  # noqa: BLE001
        return Item(str(provider), str(local_id), ok=False)


def update(provider, local_id, **fields):
    """Change a row without holding its Item (for code that only has the ids)."""
    return _write(provider, local_id, fields, create=False, force=True)


def finish(provider, local_id, state="done", **fields):
    fields["state"] = state
    return _write(provider, local_id, fields, create=False, force=True)


def _offer(provider, local_id, fields, clear=False):
    """One update finding (or its removal).  True if it reached the store or the spool."""
    try:
        if not _valid_provider(provider) or not str(local_id or "").strip():
            return False
        if not _allow_offer(provider, time.monotonic()):
            return False
        local = str(local_id)
        try:
            store = _store()
            if clear:
                store.update_clear(provider, local)
                return True
            finding, _problems = store.update_offer(provider, local, fields)
            return finding is not None
        except Exception:  # noqa: BLE001 - locked, missing, corrupt: spool it
            return _spool(provider, local, fields, False, kind="clear_update" if clear else "offer_update")
    except Exception:  # noqa: BLE001
        return False


def offer_update(provider, local_id, title="", installed=None, available=None, size_bytes=None,
                 source=None, detail=None):
    """Report that an update is available for `<provider>:<local_id>` (N23).  The same id again replaces
    the finding.  `installed` and `available` are version texts exactly as the source states them: the hub
    never parses or compares them.  `detail` is an optional list of {label, value}.  Everything is capped
    and printable-only on the way in.  Never raises; returns True if it reached the store or the inbox."""
    try:
        fields = {"title": title, "installed": installed, "available": available,
                  "size_bytes": size_bytes, "source": source, "detail": detail}
        return _offer(provider, local_id, {k: v for k, v in fields.items() if v is not None})
    except Exception:  # noqa: BLE001
        return False


def clear_update(provider, local_id):
    """Report that `<provider>:<local_id>` has no update any more (it was updated, or removed).  Never raises."""
    try:
        return _offer(provider, local_id, {}, clear=True)
    except Exception:  # noqa: BLE001
        return False


def register_provider(provider_id, label=None, mode="push", capabilities=None, controls=None,
                      open_roots=None, kind="file", icon="", interval_s=3.0, poll_tool=None,
                      settings=None, source_file=""):
    """The runtime equivalent of `TOOL_DOWNLOADS` (plan decision 7).  Call it from code the
    owner installed, never from a model-facing tool.  What the tool supplies is refreshed
    on every call; what the owner may change (enabled, interval, settings) is only a
    starting value.  Returns True on success."""
    try:
        if not _valid_provider(provider_id):
            return False
        provider, err = _store().register_provider(
            provider_id, label=label, mode=mode, kind=kind, icon=icon, capabilities=capabilities,
            controls=controls, open_roots=open_roots, interval_s=interval_s, poll_tool=poll_tool,
            settings=settings, source_file=source_file)
        if err:
            _warn_once(("register", provider_id), "register_provider(%r): %s" % (provider_id, err))
        return provider is not None
    except Exception:  # noqa: BLE001
        return False
