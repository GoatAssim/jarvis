"""Edit `allowed_guilds` from the panel / `jarvis channels-guilds` (L.36-P17).

WHY THIS EXISTS
---------------
`allowed_guilds` is the "only these servers" filter. It was hand-edit only: the
Servers section of the panel said "allowed_guilds and allowed_channels are
edited by hand", and `channels-allow` / `channels-deny` only know the four
who-lists (dm / reply / tool / image). So the owner could see "allowed_guilds is
set" in the panel and had no way to add a server to it without opening
channels.json.

`servers.py` still never writes it (see that module's docstring and AGENTS.md):
this is the separate, narrower door, and every rule below exists because the
list means "empty = unrestricted".

THE RULES
---------
    ADD     narrows access. Adding to an EMPTY list is the one step that changes
            the meaning of the whole setting (every server not named goes quiet),
            and a mistyped id does the same while naming nothing real. So both
            are PREVIEW-FIRST: without `confirm` nothing is written and the
            result lists the servers that would stop answering. Adding to a list
            that already has entries, for a server Jarvis has seen, applies at
            once (it only adds one more server to an existing restriction).
    REMOVE  narrows access too, EXCEPT removing the last entry: an empty list
            is unrestricted, so that would open every server Jarvis is in. It is
            refused here, always, confirmed or not. Clearing the filter is a
            deliberate hand edit (`jarvis channels-set discord allowed_guilds []`).

It writes only `allowed_guilds`, only ever as a clean list of digit ids, through
`config.save_config`; never `allowed_channels`, never `scopes`, never a token.
Each real change is handed to the permission change log (`changelog.py`).
No model tool reaches this module.
"""

from . import DISCORD
from . import config as channel_config
from . import servers

KEY = "allowed_guilds"
MAX_LISTED = 25      # how many "would stop answering" names a preview carries


def _guild_rows(platform, skip=""):
    """Servers Jarvis has seen and is still in, as [{id, name}] -- minus `skip`."""
    rows = []
    for gid, rec in servers.read(platform).items():
        if gid == skip or not isinstance(rec, dict) or rec.get("left"):
            continue
        rows.append({"id": gid, "name": rec.get("name") or ""})
    rows.sort(key=lambda r: (r["name"] or "~").lower())
    return rows


def view(platform=DISCORD):
    """The current filter with a name for each entry. Writes nothing."""
    if platform not in servers.SUPPORTED:
        return {"ok": False, "error": f"{platform} has no servers"}
    cfg = channel_config.platform_config(platform)
    current = channel_config.normalize_entries(cfg.get(KEY))
    reg = servers.read(platform)
    entries = []
    for gid in current:
        rec = reg.get(gid) if isinstance(reg.get(gid), dict) else {}
        entries.append({"id": gid, "name": rec.get("name") or "",
                        "known": bool(rec), "left": bool(rec.get("left"))})
    return {"ok": True, "platform": platform, "restricted": bool(current),
            "allowed_guilds": current, "entries": entries}


def _result(platform, ident, action, **extra):
    out = {"ok": True, "error": "", "applied": False, "dry_run": False,
           "needs_confirm": False, "platform": platform, "id": ident,
           "action": action, "note": "", "would_stop": [], "unknown": False}
    out.update(extra)
    return out


def set_allowed(platform, ident, remove=False, confirm=False):
    """Add (or remove) one server on `allowed_guilds`. Returns a dict:

        ok             the call did what was asked, or only previewed
        applied        the list was written (False for a preview / no-op / refusal)
        dry_run        nothing was written because `confirm` is needed
        needs_confirm  the same, as a flag for the caller
        error          why it was refused
        would_stop     servers that stop answering if this is applied (a preview)
        unknown        the id is not a server Jarvis has seen
        allowed_guilds the list as it is now (after the write, when applied)
    """
    action = "remove" if remove else "add"
    if platform not in servers.SUPPORTED:
        return _result(platform, ident, action, ok=False,
                       error=f"{platform} has no servers")
    ident = str(ident or "").strip()
    if not servers._ID.match(ident):
        return _result(platform, ident, action, ok=False,
                       error="that isn't a Discord id (digits only)")
    ident = channel_config.normalize_entry(ident)

    cfg = channel_config.load_config()
    current = channel_config.normalize_entries(cfg[platform].get(KEY))
    out = _result(platform, ident, action, allowed_guilds=list(current))

    if remove:
        if ident not in current:
            out["note"] = "that server wasn't on the list \u2014 nothing changed"
            return out
        if len(current) == 1:
            out.update(ok=False, error=(
                "that is the last server on allowed_guilds, and an empty list "
                "means EVERY server Jarvis is in can reach it. The panel won't "
                "open that up from a click: add the server you want first, or "
                "clear the filter on purpose from a terminal with "
                "`jarvis channels-set discord allowed_guilds []`"))
            return out
        after = [g for g in current if g != ident]
        cfg[platform][KEY] = after
        if not channel_config.save_config(cfg):
            out.update(ok=False, error="could not write config")
            return out
        channel_config._log_change(platform, "list", ident, list=KEY, on=False)
        out.update(applied=True, allowed_guilds=after,
                   note="that server no longer answers (it is not on the list)")
        return out

    # ---- add
    if ident in current:
        out["note"] = "already on the list \u2014 nothing changed"
        return out
    known = ident in servers.read(platform)
    others = _guild_rows(platform, skip=ident)
    first_entry = not current
    # The two cases that need a deliberate second step.
    if first_entry:
        out["would_stop"] = others[:MAX_LISTED]
        out["would_stop_total"] = len(others)
    out["unknown"] = not known
    if (first_entry and others) or not known:
        if not confirm:
            reasons = []
            if first_entry:
                reasons.append("this turns the filter ON: every server not on the "
                               "list stops answering")
            if not known:
                reasons.append("Jarvis hasn't seen this server, so check the id")
            out.update(dry_run=True, needs_confirm=True,
                       note="preview only \u2014 nothing was changed (" +
                            "; ".join(reasons) + ")")
            return out
    after = current + [ident]
    cfg[platform][KEY] = after
    if not channel_config.save_config(cfg):
        out.update(ok=False, error="could not write config")
        return out
    channel_config._log_change(platform, "list", ident, list=KEY, on=True)
    out.update(applied=True, allowed_guilds=after,
               note="added \u2014 servers not on the list are ignored")
    return out
