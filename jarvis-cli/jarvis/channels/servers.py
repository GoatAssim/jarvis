"""Discord servers (guilds) and their channels -- what Jarvis can see, and the
per-server / per-channel switches the owner can flip.

WHY THIS EXISTS
---------------
The gate already understood servers: `permissions.decide()` filters on
`allowed_guilds` / `allowed_channels` and `resolve_scope()` layers a
`"guild:<id>"` / `"channel:<id>"` override on top of the platform config. What
was missing was everything around it: nothing recorded which servers the bot is
in or what they are called (a gateway gets the names for free, an ID-only
config does not), and the only way to change a server's rules was to hand-edit
`channels.json`. This module is that missing half.

TWO SEPARATE JOBS
-----------------
1. A registry (`note_guild` / `note_channel`) the gateway keeps up to date:
   `~/.jarvis/channels/servers.json`, `{platform: {guild_id: {name, channels}}}`.
   Names only -- never a message, never a member list. It is a cache: losing it
   costs the panel its labels and changes no behaviour.
2. Switches (`set_switch`) that write ONLY into `scopes` of the platform block
   in `channels.json`, i.e. the override mechanism that already existed.

SWITCHES CAN ONLY TAKE ACCESS AWAY (like channels/user_perms.py)
----------------------------------------------------------------
    enabled   off  -> the scope carries `"enabled": false`
              on   -> the key is removed (inherit). It is never written as true:
                      a scope `"enabled": true` would override a platform that
                      is switched off, i.e. GRANT access.
    tools     off  -> `"allow_tools": false`.  on -> inherit (never true).
    mention   on   -> `"require_mention": true`.  off is refused: answering
                      without a mention widens who can reach the bot and, with
                      the message-content intent off, would receive empty text
                      anyway. Change that for the whole platform by hand.

`allowed_guilds` / `allowed_channels` are NOT written here. They are
"empty = unrestricted", so removing the last entry would silently open every
server -- the exact inversion this module exists to avoid. They are read and
reported (a server outside them is shown as not answering) and stay hand-edited.

Pure of any Discord SDK: the gateway hands in plain ids and strings.
"""

import json
import os
import re
import threading
from datetime import datetime

from . import DISCORD, PLATFORMS
from . import config as channel_config
from . import transcript

ENCODING = "utf-8"
SUPPORTED = (DISCORD,)           # Instagram groups have thread ids, no servers
KINDS = ("guild", "channel")
SWITCHES = ("enabled", "tools", "mention")
VALUES = ("on", "off", "inherit")
MAX_GUILDS = 500                 # a registry bound, not a policy
MAX_CHANNELS_PER_GUILD = 1000
_ID = re.compile(r"^[0-9]{1,25}$")
_LOCK = threading.Lock()
_cache = {}                      # (guild_id, channel_id) -> (guild_name, channel_name)

# switch -> the scope field it writes, and the ONE value it may set
_FIELD = {"enabled": ("enabled", False),
          "tools": ("allow_tools", False),
          "mention": ("require_mention", True)}


def registry_file():
    """Resolved on each call: tests redirect HOME before import, but a long
    gateway should also follow transcript's directory, never a stale copy."""
    return transcript.CHANNELS_DIR / "servers.json"


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


def _clean_name(value, limit=100):
    text = " ".join(str(value or "").split())
    return text[:limit]


# ---------------------------------------------------------------------------
# registry (written by the gateway)
# ---------------------------------------------------------------------------

def _load():
    try:
        data = json.loads(registry_file().read_text(encoding=ENCODING))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


def _save(data):
    path = registry_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       encoding=ENCODING)
        os.replace(str(tmp), str(path))
        return True
    except OSError:
        return False


def read(platform=DISCORD):
    """{guild_id: {name, left, seen, channels: {id: {name, kind}}}}."""
    block = _load().get(platform)
    return block if isinstance(block, dict) else {}


def note_guild(platform, guild_id, name, channels=None, left=None):
    """Record a server (and optionally its full channel list). Never raises.

    `channels` is an iterable of (id, name, kind); when given it REPLACES the
    stored list, because the gateway knows the whole current set on connect and
    a channel deleted while the bot was offline should disappear. `left=True`
    marks a server the bot is no longer in (kept, so its switches stay visible).
    Returns True when something changed on disk."""
    try:
        gid = str(guild_id or "").strip()
        if platform not in SUPPORTED or not _ID.match(gid):
            return False
        with _LOCK:
            data = _load()
            block = data.setdefault(platform, {})
            existed = gid in block
            if not existed and len(block) >= MAX_GUILDS:
                return False
            rec = block.setdefault(gid, {"name": "", "left": False,
                                         "seen": _now(), "channels": {}})
            before = _fingerprint(rec)
            if name:
                rec["name"] = _clean_name(name)
            if left is not None:
                rec["left"] = bool(left)
            if channels is not None:
                fresh = {}
                for cid, cname, kind in list(channels)[:MAX_CHANNELS_PER_GUILD]:
                    cid = str(cid or "").strip()
                    if _ID.match(cid):
                        fresh[cid] = {"name": _clean_name(cname),
                                      "kind": str(kind or "text")[:16]}
                rec["channels"] = fresh
            for key in [k for k in _cache if k[0] == gid]:
                del _cache[key]
            if existed and _fingerprint(rec) == before:
                return False
            rec["seen"] = _now()
            return _save(data)
    except Exception:  # noqa: BLE001 -- a label cache must never hurt a reply
        return False


def _fingerprint(rec):
    """The record without its `seen` stamp, so a re-announce of the same facts
    is not a write."""
    return json.dumps({k: v for k, v in rec.items() if k != "seen"},
                      sort_keys=True, default=str)


def note_channel(platform, guild_id, channel_id, channel_name, guild_name="",
                 kind="text"):
    """Record one channel seen on a message or a create/rename event. Cheap on
    repeat: an in-process cache answers "already known" with no disk read.
    Never raises."""
    try:
        gid, cid = str(guild_id or "").strip(), str(channel_id or "").strip()
        if platform not in SUPPORTED or not _ID.match(gid) or not _ID.match(cid):
            return False
        names = (_clean_name(guild_name), _clean_name(channel_name))
        if _cache.get((gid, cid)) == names:
            return False
        with _LOCK:
            data = _load()
            block = data.setdefault(platform, {})
            existed = gid in block
            if not existed and len(block) >= MAX_GUILDS:
                return False
            rec = block.setdefault(gid, {"name": "", "left": False,
                                         "seen": _now(), "channels": {}})
            changed = not existed
            if names[0] and rec.get("name") != names[0]:
                rec["name"], changed = names[0], True
            chans = rec.setdefault("channels", {})
            if cid not in chans and len(chans) >= MAX_CHANNELS_PER_GUILD:
                _cache[(gid, cid)] = names
                return False
            want = {"name": names[1] or (chans.get(cid) or {}).get("name", ""),
                    "kind": str(kind or "text")[:16]}
            if chans.get(cid) != want:
                chans[cid], changed = want, True
            if rec.get("left"):
                rec["left"], changed = False, True
            _cache[(gid, cid)] = names
            if not changed:
                return False
            rec["seen"] = _now()
            return _save(data)
    except Exception:  # noqa: BLE001
        return False


def forget_channel(platform, guild_id, channel_id):
    """A channel was deleted. Never raises."""
    try:
        gid, cid = str(guild_id or ""), str(channel_id or "")
        with _LOCK:
            data = _load()
            chans = ((data.get(platform) or {}).get(gid) or {}).get("channels")
            if isinstance(chans, dict) and cid in chans:
                del chans[cid]
                _cache.pop((gid, cid), None)
                return _save(data)
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------------------
# switches (written by the CLI / panel)
# ---------------------------------------------------------------------------

def _scopes(cfg):
    scopes = (cfg or {}).get("scopes")
    return scopes if isinstance(scopes, dict) else {}


def set_switch(platform, kind, ident, switch, value):
    """Flip one switch on one server or channel. Returns (ok, error, detail).

    See the module docstring for which directions are allowed. Everything is
    validated before the config is read, so a bad request cannot half-write."""
    if platform not in PLATFORMS:
        return False, f"unknown platform '{platform}'", {}
    if platform not in SUPPORTED:
        return False, f"{platform} has no servers", {}
    if kind not in KINDS:
        return False, "kind must be guild or channel", {}
    ident = str(ident or "").strip()
    if not _ID.match(ident):
        return False, "that isn't a Discord id (digits only)", {}
    if switch not in SWITCHES:
        return False, "switch must be one of: " + ", ".join(SWITCHES), {}
    value = str(value or "").strip().lower()
    if value not in VALUES:
        return False, "value must be on, off or inherit", {}
    field, only = _FIELD[switch]
    if value == "off" and switch == "mention":
        return False, ("this can only be switched on here. Answering without "
                       "an @mention widens who can reach the bot; do that for "
                       "the whole platform by hand (require_mention)."), {}
    # What to write: the single restrictive value, or "remove the key".
    if switch == "mention":
        write = True if value == "on" else None
    else:
        write = False if value == "off" else None       # on == inherit
    key = f"{kind}:{ident}"
    cfg = channel_config.load_config()
    block = cfg[platform]
    scopes = dict(_scopes(block))
    entry = dict(scopes.get(key) or {}) if isinstance(scopes.get(key), dict) else {}
    if write is None:
        entry.pop(field, None)
    else:
        if write is not only:         # the one value this switch may ever write
            return False, "internal: refusing to widen access", {}
        entry[field] = write
    if entry:
        scopes[key] = entry
    else:
        scopes.pop(key, None)
    block["scopes"] = scopes
    if not channel_config.save_config(cfg):
        return False, "could not write config", {}
    return True, "", {"scope": key, "switch": switch, "value": value,
                      "override": entry.get(field)}


# ---------------------------------------------------------------------------
# the read model the panel and `jarvis channels-servers` show
# ---------------------------------------------------------------------------

def _override(scopes, kind, ident, field):
    entry = scopes.get(f"{kind}:{ident}")
    if isinstance(entry, dict) and field in entry:
        return entry[field]
    return None


def _ids(value):
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def view(platform=DISCORD):
    """Every server Jarvis knows about with its switches and what actually
    applies. Pure read; the effective flags use the same rules decide() does
    (channel override beats server override beats platform)."""
    if platform not in SUPPORTED:
        return {"ok": True, "platform": platform, "supported": False,
                "servers": [], "orphans": []}
    cfg = channel_config.platform_config(platform)
    scopes = _scopes(cfg)
    reg = read(platform)
    allowed_g = _ids(cfg.get("allowed_guilds"))
    allowed_c = _ids(cfg.get("allowed_channels"))
    plat_on = bool(cfg.get("enabled"))
    plat_tools = bool(cfg.get("allow_tools"))
    plat_mention = bool(cfg.get("require_mention", True))

    guild_ids = list(reg)
    for key in scopes:
        if key.startswith("guild:") and key[6:] not in guild_ids:
            guild_ids.append(key[6:])
    for gid in allowed_g:
        if gid not in guild_ids:
            guild_ids.append(gid)

    def state_of(gid, cid=None):
        g_enabled = _override(scopes, "guild", gid, "enabled")
        g_tools = _override(scopes, "guild", gid, "allow_tools")
        g_mention = _override(scopes, "guild", gid, "require_mention")
        c_enabled = _override(scopes, "channel", cid, "enabled") if cid else None
        c_tools = _override(scopes, "channel", cid, "allow_tools") if cid else None
        c_mention = _override(scopes, "channel", cid, "require_mention") if cid else None
        why = []
        answering = plat_on
        if not plat_on:
            why.append("Discord is switched off")
        if g_enabled is False or c_enabled is False:
            answering = False
            why.append("switched off here")
        if allowed_g and gid not in allowed_g:
            answering = False
            why.append("not in allowed_guilds")
        if cid and allowed_c and cid not in allowed_c:
            answering = False
            why.append("not in allowed_channels")
        tools = plat_tools and g_tools is not False and c_tools is not False
        mention = plat_mention or g_mention is True or c_mention is True
        return {"answering": answering, "why_not": why, "tools": tools,
                "mention": mention,
                "override": {"enabled": "off" if (c_enabled if cid else g_enabled) is False else "inherit",
                             "tools": "off" if (c_tools if cid else g_tools) is False else "inherit",
                             "mention": "on" if (c_mention if cid else g_mention) is True else "inherit"}}

    servers, seen_channels = [], set()
    for gid in guild_ids:
        rec = reg.get(gid) if isinstance(reg.get(gid), dict) else {}
        chans = rec.get("channels") if isinstance(rec.get("channels"), dict) else {}
        channel_ids = list(chans)
        rows = []
        for cid in channel_ids:
            meta = chans[cid] if isinstance(chans[cid], dict) else {}
            seen_channels.add(cid)
            rows.append({"id": cid, "name": meta.get("name") or "",
                         "kind": meta.get("kind") or "text",
                         **state_of(gid, cid)})
        rows.sort(key=lambda r: (r["name"] or "~").lower())
        servers.append({"id": gid, "name": rec.get("name") or "",
                        "known": bool(rec), "left": bool(rec.get("left")),
                        "seen": rec.get("seen") or "",
                        "channels": rows, **state_of(gid)})
    servers.sort(key=lambda s: (s["left"], (s["name"] or "~").lower()))

    orphans = []
    for key in scopes:
        if key.startswith("channel:") and key[8:] not in seen_channels:
            orphans.append({"id": key[8:], "scope": scopes[key]})
    for cid in allowed_c:
        if cid not in seen_channels and not any(o["id"] == cid for o in orphans):
            orphans.append({"id": cid, "scope": {}})
    return {"ok": True, "platform": platform, "supported": True,
            "platform_enabled": plat_on, "platform_tools": plat_tools,
            "platform_mention": plat_mention,
            "allowed_guilds": allowed_g, "allowed_channels": allowed_c,
            "servers": servers, "orphans": orphans}
