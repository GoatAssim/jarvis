"""Edit `allowed_channels` from the panel / `jarvis channels-channels` (L.36-P18).

WHY THIS EXISTS
---------------
`allowed_channels` is the "only these channels" filter. `allowed_guilds` got an
editor in L.36-P17, but this list was still hand-edit only: the Servers section
of the panel said "allowed_channels is edited by hand", and `channels-allow` /
`channels-deny` only know the four who-lists (dm / reply / tool / image). So the
owner could pick a server from a dropdown but had to open channels.json to say
"and only in #jarvis".

`servers.py` still never writes it (see that module's docstring and AGENTS.md).
This is the same separate, narrower door `allowed_guilds.py` is, and every rule
below exists for the same reason: the list means "empty = unrestricted".

THE RULES
---------
    ADD     narrows access. Adding to an EMPTY list is the one step that changes
            the meaning of the whole setting (every channel not named goes quiet,
            in every server), and a mistyped id does the same while naming
            nothing real. So both are PREVIEW-FIRST: without `confirm` nothing is
            written and the result lists the channels that would stop answering.
            Adding to a list that already has entries, for a channel Jarvis has
            seen, applies at once (it only adds one more channel to an existing
            restriction).
    REMOVE  narrows access too, EXCEPT removing the last entry: an empty list is
            unrestricted, so that would open every channel Jarvis can see. It is
            refused here, always, confirmed or not. Clearing the filter is a
            deliberate hand edit (`jarvis channels-set discord allowed_channels []`).

Two things worth knowing, both surfaced in the result rather than refused:
    * A message in a THREAD counts as the channel the thread hangs off (see
      discord_gateway._where), so the id to list is the parent channel's. A
      thread's own id never matches; it is reported as `unknown`.
    * The channel filter sits BESIDE `allowed_guilds`, not instead of it: a
      channel whose server is not on a non-empty `allowed_guilds` stays silent
      even when it is listed here. The result says so (`guild_blocked`); it does
      not change `allowed_guilds` (that is allowed_guilds.py's door, not this one).
    * Direct messages never go through either filter.

It writes only `allowed_channels`, only ever as a clean list of digit ids, through
`config.save_config`; never `allowed_guilds`, never `scopes`, never a token. Each
real change is handed to the permission change log (`changelog.py`). No model
tool reaches this module.
"""

from . import DISCORD
from . import config as channel_config
from . import servers

KEY = "allowed_channels"
MAX_LISTED = 25      # how many "would stop answering" names a preview carries


def _find(platform, cid):
    """(guild_id, guild_name, channel_name, guild_left) for a channel Jarvis has
    seen, else None."""
    for gid, rec in servers.read(platform).items():
        if not isinstance(rec, dict):
            continue
        chans = rec.get("channels")
        if isinstance(chans, dict) and cid in chans:
            meta = chans[cid] if isinstance(chans[cid], dict) else {}
            return (gid, rec.get("name") or "", meta.get("name") or "",
                    bool(rec.get("left")))
    return None


def _channel_rows(platform, skip=""):
    """Text channels Jarvis has seen in servers it is still in, as
    [{id, name, guild_id, guild_name}] -- minus `skip`."""
    rows = []
    for gid, rec in servers.read(platform).items():
        if not isinstance(rec, dict) or rec.get("left"):
            continue
        chans = rec.get("channels")
        if not isinstance(chans, dict):
            continue
        for cid, meta in chans.items():
            if cid == skip:
                continue
            meta = meta if isinstance(meta, dict) else {}
            rows.append({"id": cid, "name": meta.get("name") or "",
                         "guild_id": gid, "guild_name": rec.get("name") or ""})
    rows.sort(key=lambda r: ((r["guild_name"] or "~").lower(),
                             (r["name"] or "~").lower()))
    return rows


def view(platform=DISCORD):
    """The current filter with a name for each entry. Writes nothing."""
    if platform not in servers.SUPPORTED:
        return {"ok": False, "error": f"{platform} has no channels"}
    cfg = channel_config.platform_config(platform)
    current = channel_config.normalize_entries(cfg.get(KEY))
    entries = []
    for cid in current:
        found = _find(platform, cid)
        entries.append({
            "id": cid, "name": found[2] if found else "",
            "guild_id": found[0] if found else "",
            "guild_name": found[1] if found else "",
            "known": bool(found), "left": bool(found and found[3])})
    return {"ok": True, "platform": platform, "restricted": bool(current),
            "allowed_channels": current, "entries": entries}


def _result(platform, ident, action, **extra):
    out = {"ok": True, "error": "", "applied": False, "dry_run": False,
           "needs_confirm": False, "platform": platform, "id": ident,
           "action": action, "note": "", "would_stop": [], "unknown": False,
           "guild_blocked": False}
    out.update(extra)
    return out


def set_allowed(platform, ident, remove=False, confirm=False):
    """Add (or remove) one channel on `allowed_channels`. Returns a dict:

        ok             the call did what was asked, or only previewed
        applied        the list was written (False for a preview / no-op / refusal)
        dry_run        nothing was written because `confirm` is needed
        needs_confirm  the same, as a flag for the caller
        error          why it was refused
        would_stop     channels that stop answering if this is applied (a preview)
        unknown        the id is not a channel Jarvis has seen
        guild_blocked  the channel's server is not on a non-empty allowed_guilds,
                       so it stays silent even once listed
        allowed_channels  the list as it is now (after the write, when applied)
    """
    action = "remove" if remove else "add"
    if platform not in servers.SUPPORTED:
        return _result(platform, ident, action, ok=False,
                       error=f"{platform} has no channels")
    ident = str(ident or "").strip()
    if not servers._ID.match(ident):
        return _result(platform, ident, action, ok=False,
                       error="that isn't a Discord id (digits only)")
    ident = channel_config.normalize_entry(ident)

    cfg = channel_config.load_config()
    current = channel_config.normalize_entries(cfg[platform].get(KEY))
    out = _result(platform, ident, action, allowed_channels=list(current))

    if remove:
        if ident not in current:
            out["note"] = "that channel wasn't on the list \u2014 nothing changed"
            return out
        if len(current) == 1:
            out.update(ok=False, error=(
                "that is the last channel on allowed_channels, and an empty list "
                "means EVERY channel Jarvis can see can reach it. The panel won't "
                "open that up from a click: add the channel you want first, or "
                "clear the filter on purpose from a terminal with "
                "`jarvis channels-set discord allowed_channels []`"))
            return out
        after = [c for c in current if c != ident]
        cfg[platform][KEY] = after
        if not channel_config.save_config(cfg):
            out.update(ok=False, error="could not write config")
            return out
        channel_config._log_change(platform, "list", ident, list=KEY, on=False)
        out.update(applied=True, allowed_channels=after,
                   note="that channel no longer answers (it is not on the list)")
        return out

    # ---- add
    if ident in current:
        out["note"] = "already on the list \u2014 nothing changed"
        return out
    found = _find(platform, ident)
    known = bool(found)
    others = _channel_rows(platform, skip=ident)
    first_entry = not current
    # Where the channel lives, so the panel can name it in the preview.
    if found:
        out["guild_id"], out["guild_name"], out["channel_name"] = found[0], found[1], found[2]
    guilds = channel_config.normalize_entries(cfg[platform].get("allowed_guilds"))
    out["guild_blocked"] = bool(found and guilds and found[0] not in guilds)
    # The two cases that need a deliberate second step.
    if first_entry:
        out["would_stop"] = others[:MAX_LISTED]
        out["would_stop_total"] = len(others)
    out["unknown"] = not known
    if (first_entry and others) or not known:
        if not confirm:
            reasons = []
            if first_entry:
                reasons.append("this turns the filter ON: every channel not on the "
                               "list stops answering, in every server")
            if not known:
                reasons.append("Jarvis hasn't seen this channel, so check the id "
                               "(for a thread, list the channel it hangs off)")
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
    note = "added \u2014 channels not on the list are ignored"
    if out["guild_blocked"]:
        note += (", but its server isn't on allowed_guilds, so it stays silent "
                 "until that server is allowed too")
    out.update(applied=True, allowed_channels=after, note=note)
    return out
