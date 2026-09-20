"""Keeps a conversation's most recently *confidently* routed tool group(s)
offered for a few follow-up turns, instead of tool_router.route() re-scoring
cold on every single message.

Why this exists: tool_router.route() only looks at the current message's
text. A message like "go on discord and say hello" confidently matches the
`desktop` group, but the natural follow-up — "no just say hello", "it's
open now", "yes do it" — usually carries none of the original keywords, so
route() comes back non-confident and ai_client.ask() falls back to offering
just the tiny search_tools/search_commands discovery pair. The model then
has no matching tool to actually call and (worse) sometimes claims it did
the thing anyway. This module patches that gap: once a group is matched
with real confidence, "stick" it to the conversation for STICKY_TURNS more
asks so a same-task follow-up still has the tools it needs on hand.

Deliberately small and best-effort, same spirit as discovery_cache.py: a
missing/corrupt file degrades to "no stickiness", never an error. One JSON
file, one entry per conversation_id, holding the sticky group list and a
turns-remaining counter.

Call shape (see ai_client.ask() for the actual wiring):

    sticky_before = get_sticky(conv_id)                # before routing
    route = tool_router.route(user_text)
    if route.confident:
        set_sticky(conv_id, route.groups)               # new match wins outright
    elif sticky_before:
        groups_to_offer = sticky_before                 # inherit, and...
        touch_sticky(conv_id)                            # ...tick the counter down

"A different toolset is called, drop the old one": handled by set_sticky()
simply overwriting whatever was stored for that conversation_id — there is
only ever one sticky entry per conversation, so a fresh confident match
always replaces (never merges with) the previous one, AS FAR AS THIS MODULE
IS CONCERNED. ai_client.ask() (master plan F.10 item 2) is one caller that
deliberately computes a merged list itself before calling set_sticky() —
when a confident match is also a short confirmation ("yes plz run this")
with a live sticky entry, that's continuing the same task, not switching to
a new one. set_sticky() doesn't know or care either way; it just stores
whatever list its caller hands it.
"""

import json
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
STICKY_FILE = JARVIS_DIR / "route_stickiness.json"
ENCODING = "utf-8"

# How many *non-confident* follow-up asks in the same conversation still get
# the last confidently-matched group(s) offered, before falling back to the
# normal discovery-only behavior. Each sticky turn re-offers the *full*
# group schema (same size as a confident match) instead of the tiny
# discovery pair — cheap for one immediate follow-up ("it's ready" ->
# "go ahead"), but 3 turns of that on every tool use was measurably
# doubling session token cost on conversations with a lot of ambiguous
# chit-chat between tool calls. 1 still covers the single-follow-up case
# this was built for, without taxing every message for two turns after.
STICKY_TURNS = 1


def _load():
    try:
        data = json.loads(STICKY_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data):
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        STICKY_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding=ENCODING)
    except OSError:
        # Best-effort only, same as discovery_cache.py — a failed write just
        # means the next ask() doesn't get sticky groups, same as a miss.
        pass


def get_sticky(conv_id):
    """Groups still "live" for this conversation, or [] if there are none
    (no prior confident match, entry expired, or file missing/corrupt)."""
    if not conv_id:
        return []
    entry = _load().get(str(conv_id))
    if not isinstance(entry, dict):
        return []
    groups = entry.get("groups")
    turns_left = entry.get("turns_left", 0)
    if not isinstance(groups, list) or not groups or turns_left <= 0:
        return []
    return [g for g in groups if isinstance(g, str)]


def set_sticky(conv_id, groups):
    """Overwrite whatever was previously sticky for this conversation with
    `groups`, and reset the counter to STICKY_TURNS. Simple last-write-wins
    storage — it's the caller's job to decide what `groups` should be; see
    the module docstring for ai_client.ask()'s merge-on-confirmation case."""
    if not conv_id or not groups:
        return
    data = _load()
    data[str(conv_id)] = {"groups": list(groups), "turns_left": STICKY_TURNS}
    _save(data)


def touch_sticky(conv_id):
    """Call once per non-confident ask() that actually consumed the sticky
    groups (i.e. right after get_sticky() returned something non-empty) to
    tick the counter down by one. Deletes the entry once it hits zero so
    get_sticky() naturally reverts to the normal discovery-only fallback."""
    if not conv_id:
        return
    data = _load()
    entry = data.get(str(conv_id))
    if not isinstance(entry, dict):
        return
    entry["turns_left"] = entry.get("turns_left", 0) - 1
    if entry["turns_left"] <= 0:
        data.pop(str(conv_id), None)
    else:
        data[str(conv_id)] = entry
    _save(data)


def clear_sticky(conv_id):
    """Drop any sticky entry for this conversation outright \u2014 e.g. when the
    user explicitly clears/starts a conversation (conversations.py's
    clear/new-conversation path), so a brand-new chat never inherits an old
    one's tool group."""
    if not conv_id:
        return
    data = _load()
    if data.pop(str(conv_id), None) is not None:
        _save(data)
