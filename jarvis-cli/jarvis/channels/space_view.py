"""The read side of the room registry: which rooms Jarvis has saved, and what was said in one.

    space_view.list_spaces(platform=None, kind=None)        the rooms, newest activity first
    space_view.view(platform, thread_id, limit=100)         one room's conversation

(channel spaces S10a). Used by `jarvis channels-spaces` / `channels-space` and by the two
read-only web routes next to the servers routes in web/server.js (S10b).

READ-ONLY, ALWAYS
-----------------
Nothing here writes, rotates, registers or creates a file, calls a model, or sends a
message: it wraps `transcript.list_threads`, `transcript.read_space` and `spaces.read` /
`spaces.get`, which are themselves read-only. `tests/test_space_view.py` snapshots the whole
state directory before and after to pin that.

WHAT A "ROOM" IS HERE
---------------------
A server channel, a server thread or a group chat (`space.KINDS` minus a DM). A listing
holds rooms only unless a kind is asked for BY NAME, and `kind="dm"` is the one way to list
DM threads (the owner's own CLI, the same data `channels-log` prints). `view(...,
rooms_only=True)` refuses a DM and a thread of unknown kind: that is the mode a model tool
must use (channel spaces S10c: the owner-only `recent_spaces` tool reads rooms, never DMs).

WHAT A LISTING ROW SAYS
-----------------------
`thread_id` is the id the room is registered under when it is registered (the one
`channels-space-set` and the scopes use), else the transcript file's name, which is the
sanitised id and works with `view()` too. `saved` is False for a room that is registered but
has no transcript (saving was off for it, or it was never written). `imported` marks a room
written from a data export (instagram_import.py).
"""

from . import PLATFORMS, space, spaces, transcript

ROOM_KINDS = (space.KIND_CHANNEL, space.KIND_THREAD, space.KIND_GROUP)
KIND_FILTERS = {
    "server": (space.KIND_CHANNEL, space.KIND_THREAD),
    "group": (space.KIND_GROUP,),
    "dm": (space.KIND_DM,),
    space.KIND_CHANNEL: (space.KIND_CHANNEL,),
    space.KIND_THREAD: (space.KIND_THREAD,),
}
MAX_ROWS = 1000


def _fail(message, **extra):
    return {"ok": False, "error": message, **extra}


def _room_switches(scopes, row_kind, thread_id):
    """The room's own Saving / Reading overrides, `inherit` or `off` (a room can only be
    switched OFF; `on` removes the override), read from the same `scopes` block the gate
    reads. A Discord thread is keyed `thread:<id>`, every other room `channel:<id>`; both
    are checked so a switch written either way shows. Read-only, never raises (channel
    spaces S11: the panel needs the state to draw the switch truthfully)."""
    out = {"log": "inherit", "ambient": "inherit"}
    try:
        from . import servers
        for scope_kind in ("channel", "thread"):
            if servers._override(scopes, scope_kind, thread_id, "log_conversations") is False:
                out["log"] = "off"
            if servers._override(scopes, scope_kind, thread_id, "ambient") is False:
                out["ambient"] = "off"
    except Exception:  # noqa: BLE001 - a broken config must not break the list
        pass
    return out


def list_spaces(platform=None, kind=None):
    """`{ok, platform, kind, count, truncated, spaces: [row]}`, newest activity first.

    Row: platform, thread_id, kind, title, imported, speakers (how many the registry has
    seen, None when unregistered), bytes (the whole log), modified, saved, switches
    (`{log, ambient}`: `off` when that room's own Saving / Reading switch is off, else
    `inherit`; the panel draws its switches from this).
    `kind` narrows the list: `server` (channels and threads), `group`, `dm`,
    `server_channel` or `server_thread`; with none, every room and no DM."""
    if platform and platform not in PLATFORMS:
        return _fail(f"unknown platform '{platform}'", spaces=[], count=0)
    wanted = ROOM_KINDS
    if kind:
        wanted = KIND_FILTERS.get(str(kind).strip().lower())
        if wanted is None:
            return _fail("kind must be server, group or dm (or server_channel, server_thread)",
                         spaces=[], count=0)
    rows = []
    for name in ([platform] if platform else list(PLATFORMS)):
        try:
            from . import config as channel_config
            scopes = channel_config.platform_config(name).get("scopes")
            scopes = scopes if isinstance(scopes, dict) else {}
        except Exception:  # noqa: BLE001
            scopes = {}
        registry = {transcript._safe_name(key): (key, rec)
                    for key, rec in spaces.read(name).items() if isinstance(rec, dict)}
        listed = set()
        for item in transcript.list_threads(platform=name, details=True):
            base = item["thread_id"]
            listed.add(base)
            raw, rec = registry.get(base, (base, None))
            if (item.get("space") or "") not in wanted:
                continue
            rows.append({
                "platform": name, "thread_id": raw, "kind": item.get("space") or "",
                "title": item.get("title") or "",
                "imported": bool(rec and rec.get("imported")),
                "speakers": len(rec.get("speakers") or []) if rec else None,
                "bytes": item.get("bytes") or 0, "modified": item.get("modified") or "",
                "saved": True,
                "switches": _room_switches(scopes, item.get("space") or "", raw)})
        for base, (raw, rec) in registry.items():
            if base in listed or rec.get("kind") not in wanted:
                continue
            rows.append({
                "platform": name, "thread_id": raw, "kind": rec.get("kind") or "",
                "title": str(rec.get("title") or ""), "imported": bool(rec.get("imported")),
                "speakers": len(rec.get("speakers") or []), "bytes": 0,
                "modified": str(rec.get("last_seen") or ""), "saved": False,
                "switches": _room_switches(scopes, rec.get("kind") or "", raw)})
    rows.sort(key=lambda r: r["modified"], reverse=True)
    return {"ok": True, "platform": platform or "", "kind": kind or "",
            "count": min(len(rows), MAX_ROWS), "truncated": len(rows) > MAX_ROWS,
            "spaces": rows[:MAX_ROWS]}


def view(platform, thread_id, limit=100, rooms_only=False):
    """One room's conversation: everything `transcript.read_space` returns (entries, total,
    shown, kind, title, participants, denied, unattributed_replies) plus `ok`, `known`
    (the room is in the registry) and `imported`. `limit` is the newest N lines (1..500).

    `ok` is False for an unknown platform, an id nothing was saved or registered for, and
    (with `rooms_only`) anything that is not a room."""
    if platform not in PLATFORMS:
        return _fail(f"unknown platform '{platform}'")
    thread = str(thread_id or "").strip()
    if not thread or len(thread) > 200:
        return _fail("a room id is required")
    data = transcript.read_space(platform, thread, limit)
    rec = spaces.get(platform, thread)
    if data["total"] == 0 and rec is None:
        return _fail("nothing is saved for that room", platform=platform, thread_id=thread)
    if not data["kind"] and rec:
        data["kind"], data["title"] = rec.get("kind") or "", str(rec.get("title") or "")
    if rooms_only and data["kind"] not in ROOM_KINDS:
        return _fail("that is not a room", platform=platform, thread_id=thread)
    imported = bool(rec and rec.get("imported")) or any(
        e.get("imported") for e in data["entries"])
    return {"ok": True, **data, "known": rec is not None, "imported": imported}
