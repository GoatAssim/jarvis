"""Providers at run time (hub plan section 5): registering them, polling them, controlling them.

A provider is something that downloads on its own and only needs to be SHOWN (and, if it says
so, controlled).  Three pieces live here:

  sync_declared()      the TOOL_DOWNLOADS entries tool files declared -> rows of the
                       `providers` table (what the tool supplies is refreshed every time;
                       what the owner may change is written once, see Store.register_provider);
  PollRunner           the hub daemon's poll mode: calls each poll provider's source every
                       `interval_s` and applies the answer to its rows (`apply_poll`);
  run_control()        a provider row's pause / resume / cancel / retry / ... by running the
                       provider's own control tool.

Sources of a poll: a TOOL (`poll_tool`, a read-only tool of the declaring file, run through the
same `tools.execute_tool` every caller uses) or a BUILT-IN function registered in `BUILTIN`
(Steam's manifest watcher, the Playnite install watcher).  Both answer the same shape:

    {"items": [ {"id": ..., "title": ..., "state": ..., "percent": ..., ...} ], "complete": bool}

`complete: true` means "this is every download I know about": a row the hub holds that is
missing from the list is finished (done if it had reached 100 %, else unknown) instead of
being left spinning (plan 5.7).  Without it only what arrives is updated.

Everything a provider says goes through Store.upsert_provider_row, so it is sanitised the
same as a pushed report: only its own `<provider>:<id>` rows, paths checked against its
open_roots, links against the allowed schemes, size and row caps.  Nothing here ever raises into
the daemon; a failing poll is shown in the provider's health line and retried with backoff.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import states

log = logging.getLogger("jarvis.hub.providers")

DEFAULT_POLL_TIMEOUT_S = 5.0
IDLE_MIN_S = 30.0
IDLE_MAX_S = 60.0
BACKOFF_MAX_S = 300.0
MAX_ITEMS_PER_POLL = 500

# What a polled row may carry (the same fields a pushed report may).
_ROW_KEYS = frozenset((
    "title", "subtitle", "phase", "message", "error", "state", "kind", "percent", "bytes_done",
    "bytes_total", "peers", "seeds", "phase_index", "phase_count", "speed_bps", "eta_s",
    "dest", "thumb", "group", "group_title", "children", "detail", "links", "capabilities"))

# id -> BuiltinProvider; steam.py / playnite_provider.py add themselves in builtin_providers().
BUILTIN = {}


class BuiltinProvider:
    """A provider whose source is code in the hub, not a tool.

    `registration(store)` -> kwargs for Store.register_provider, or None when this machine has
    nothing for it (no Steam installed): then it is not registered and not polled.
    `poll(store, now)` -> {"items": [...], "complete": bool} or {"error": "..."}.
    `idle(store)` -> True when there is nothing to watch (the runner then polls rarely).
    """

    id = ""

    def registration(self, store):
        return None

    def poll(self, store, now):
        return {"items": [], "complete": False}


def builtin_providers():
    if not BUILTIN:
        from . import playnite_provider, steam
        for mod in (steam, playnite_provider):
            provider = mod.PROVIDER
            BUILTIN[provider.id] = provider
    return dict(BUILTIN)


# --------------------------------------------------------------------------- registration

def sync_declared(store, declared=None):
    """Register every TOOL_DOWNLOADS entry (and the built-in providers this machine has).
    Returns the list of provider ids registered.  Never raises."""
    done = []
    try:
        if declared is None:
            from .. import tools
            declared = getattr(tools, "AUTO_DOWNLOADS", [])
    except Exception:  # noqa: BLE001
        log.exception("couldn't read the declared providers")
        declared = []
    for entry in declared:
        try:
            provider, err = store.register_provider(
                entry["id"], label=entry["label"], mode=entry["mode"], kind=entry.get("kind", "file"),
                icon=entry.get("icon", ""), capabilities=entry.get("capabilities"), controls=entry.get("controls"),
                open_roots=entry.get("open_roots"), interval_s=entry.get("interval_s"),
                poll_tool=entry.get("poll_tool"), settings=entry.get("settings"),
                source_file=entry.get("source_file", ""))
            if provider is None:
                log.warning("provider %s not registered: %s", entry.get("id"), err)
            else:
                done.append(entry["id"])
        except Exception:  # noqa: BLE001
            log.exception("registering provider %s failed", entry.get("id"))
    try:
        for pid, builtin in builtin_providers().items():
            kwargs = builtin.registration(store)
            if kwargs is None:
                continue
            provider, err = store.register_provider(pid, source_file="hub.%s" % pid, **kwargs)
            if provider is not None:
                done.append(pid)
            else:
                log.warning("built-in provider %s not registered: %s", pid, err)
    except Exception:  # noqa: BLE001
        log.exception("registering the built-in providers failed")
    return done


# --------------------------------------------------------------------------- applying a poll

def _clean_row(raw):
    """One polled item -> (local id, fields) or (None, problem)."""
    if not isinstance(raw, dict):
        return None, "not an object"
    local = states.clean_text(raw.get("id"), 120)
    if not local:
        return None, "no id"
    if raw.get("state") not in states.STATES:
        return None, "unknown state %r" % (states.clean_text(raw.get("state"), 30),)
    if not states.clean_text(raw.get("title"), 20):
        return None, "no title"
    return local, {k: raw[k] for k in raw if k in _ROW_KEYS}


def apply_poll(store, provider_id, result, now=None):
    """Apply one poll answer.  Returns {"applied", "dropped", "finished", "error"}."""
    now = store.now() if now is None else now
    out = {"applied": 0, "dropped": 0, "finished": 0, "error": None}
    if not isinstance(result, dict):
        out["error"] = "the poll answered something other than an object"
        return out
    if result.get("error"):
        out["error"] = states.clean_text(result["error"], 200)
        return out
    items = result.get("items")
    if not isinstance(items, list):
        out["error"] = "the poll answer has no 'items' list"
        return out
    seen = set()
    for raw in items[:MAX_ITEMS_PER_POLL]:
        local, fields = _clean_row(raw)
        if local is None:
            out["dropped"] += 1
            continue
        row, _problems = store.upsert_provider_row(provider_id, local, fields, create=True, now=now)
        if row is None:
            out["dropped"] += 1
            continue
        seen.add(row["id"])
        out["applied"] += 1
    out["dropped"] += max(0, len(items) - MAX_ITEMS_PER_POLL)
    if result.get("complete") is True:
        for row in store.list_items(provider=provider_id, limit=2000, include_disabled=True):
            if row["state"] in states.FINAL_STATES or row["state"] == "unknown" or row["id"] in seen:
                continue
            if (row.get("percent") or 0) >= 100:
                new, note = "done", "no longer listed by the provider; it had reached 100 %"
            else:
                new, note = "unknown", "no longer listed by the provider"
            store.update_item(row["id"], {"state": new}, event=note)
            out["finished"] += 1
    return out


# --------------------------------------------------------------------------- the poll runner

class PollRunner:
    """Called by the hub daemon about once a second; polls providers that are due."""

    def __init__(self, store, clock=time.time, executor=None, timeout_s=DEFAULT_POLL_TIMEOUT_S):
        self.store = store
        self._clock = clock
        self._timeout = timeout_s
        self._pool = None
        self._pool_lock = threading.Lock()
        self._execute = executor            # (tool name, args) -> result; None = tools.execute_tool
        self._state = {}                    # id -> {"next", "fails", "future", "started"}

    def _tool_executor(self):
        if self._execute is not None:
            return self._execute
        from .. import tools
        return lambda name, args: tools.execute_tool(name, args)

    def _submit(self, fn):
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hub-poll")
            return self._pool.submit(fn)

    def close(self):
        with self._pool_lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False)
                self._pool = None

    def _live_rows(self, pid):
        return any(r["state"] not in states.FINAL_STATES and r["state"] != "unknown"
                   for r in self.store.list_items(provider=pid, limit=50, include_disabled=True))

    def _interval(self, provider, idle):
        base = float(provider.get("interval_s") or 3.0)
        if not idle:
            return base
        return min(IDLE_MAX_S, max(IDLE_MIN_S, base * 5))

    def tick(self, now=None):
        """One pass.  Returns the ids polled or applied this pass (for tests and the log)."""
        now = self._clock() if now is None else now
        touched = []
        builtins = builtin_providers()
        for provider in self.store.list_providers():
            pid = provider["id"]
            if not provider["enabled"] or provider["mode"] not in ("poll", "both"):
                continue
            st = self._state.setdefault(pid, {"next": 0.0, "fails": 0, "future": None, "started": 0.0})
            if st["future"] is not None:
                if st["future"].done():
                    self._collect(provider, st, now)
                    touched.append(pid)
                elif now - st["started"] > self._timeout:
                    self._fail(provider, st, now, "the poll took longer than %d s" % int(self._timeout))
                    st["future"].cancel()
                    st["future"] = None
                continue
            if now < st["next"]:
                continue
            source = self._source(provider, builtins)
            if source is None:
                continue
            st["started"] = now
            st["future"] = self._submit(source)
            touched.append(pid)
        return touched

    def _source(self, provider, builtins):
        pid = provider["id"]
        if provider.get("poll_tool"):
            tool, run = provider["poll_tool"], self._tool_executor()
            return lambda: run(tool, {})
        builtin = builtins.get(pid)
        if builtin is not None:
            return lambda: builtin.poll(self.store, self._clock())
        return None

    def _collect(self, provider, st, now):
        pid = provider["id"]
        future, st["future"] = st["future"], None
        try:
            result = future.result(timeout=0)
        except Exception as exc:  # noqa: BLE001
            self._fail(provider, st, now, "the poll failed: %s" % states.clean_text(exc, 150))
            return
        summary = apply_poll(self.store, pid, result, now=now)
        if summary["error"]:
            self._fail(provider, st, now, summary["error"])
            return
        st["fails"] = 0
        # How soon to look again depends on what this answer left behind: live rows = the fast
        # cadence, nothing live = the slow one (a new download is noticed within 30-60 s).
        builtin = builtin_providers().get(pid) if not provider.get("poll_tool") else None
        idle = bool(builtin.idle(self.store)) if builtin is not None else not self._live_rows(pid)
        st["next"] = now + self._interval(provider, idle)
        self.store.note_provider_health(pid, ok=True)
        if summary["dropped"]:
            self.store.note_provider_health(pid, ok=False, error="%d polled row(s) dropped as invalid" % summary["dropped"])

    def _fail(self, provider, st, now, message):
        st["fails"] += 1
        base = float(provider.get("interval_s") or 3.0)
        st["next"] = now + min(BACKOFF_MAX_S, base * (2 ** min(st["fails"], 8)))
        try:
            self.store.note_provider_health(provider["id"], ok=False, error=message)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- controls

def _local_id(item):
    return str(item["id"]).split(":", 1)[1] if ":" in str(item["id"]) else str(item["id"])


def _confirm_control(tool, verb, provider):
    """A control tool the owner marked confirm-required asks first (default no): the hub never
    turns a button into a way around a confirmation (plan 5.5)."""
    try:
        from .. import tool_safety
        needs = tool_safety.requires_confirmation(tool)
    except Exception:  # noqa: BLE001 - if we can't tell, ask
        needs = True
    if not needs:
        return True
    from .. import ui_bridge as ui
    return ui.confirm("%s: %s?" % ((provider or {}).get("label") or "Download", verb),
                      body="Runs the tool %s." % tool, level="warn",
                      confirm_label="Do it", cancel_label="Don't", default=False)


def run_control(item, verb, provider, executor=None, confirm=None):
    """Run the provider's own control tool for `verb`.  Returns (ok, message)."""
    tool = ((provider or {}).get("controls") or {}).get(verb)
    if not tool:
        return False, "%s offers %s but has no tool that performs it" % ((provider or {}).get("label") or "the provider", verb)
    ask = confirm or _confirm_control
    if not ask(tool, verb, provider):
        return False, "not confirmed"
    try:
        if executor is None:
            from .. import tools
            executor = lambda name, args: tools.execute_tool(name, args)       # noqa: E731
        result = executor(tool, {"id": _local_id(item)})
    except Exception as exc:  # noqa: BLE001
        return False, "%s failed: %s" % (tool, states.clean_text(exc, 150))
    if not isinstance(result, dict):
        return False, "%s answered something unexpected" % tool
    if result.get("error"):
        return False, states.clean_text(result["error"], 200)
    if result.get("ok") is False:
        return False, states.clean_text(result.get("message") or "the tool said no", 200)
    return True, states.clean_text(result.get("message") or "done", 120)
