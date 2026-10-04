"""Who Jarvis is actually talking to on a chat platform.

WHY THIS ISN'T directory.py
---------------------------
`directory.py` is a lookup table: handle -> id, so an allowlist can say
`@someone` instead of a raw snowflake. It answers "which account is this
name?".

This file answers a completely different question: "who is this person,
and what is my relationship with them?". Name they asked to be called,
when they first showed up, how many times they've written, whether the
owner has approved them. Those are facts about a *person*, keyed by the
one thing that never changes (the platform id), whereas directory.py is
keyed by the thing that does change (the handle) precisely because its job
is to resolve renames.

Keeping them apart means a handle rename updates directory.py and leaves
this file's history intact, which is the correct behaviour for both.

THE PROBLEM THIS EXISTS TO FIX
------------------------------
Until now `base._ask_jarvis()` handed the model nothing but the message
text. Over Discord or Instagram that is actively misleading: the system
prompt says "you are a private AI assistant running locally for one user
on their own computer" and addresses whoever is typing as the owner. So a
stranger DMing the bot got answered as though they *were* the owner —
same form of address, same assumed history, same long-term memory about
someone else's life.

So every accepted message now carries a short identity block naming who
sent it and whether they're the owner. It's a handful of tokens and it is
the difference between "your usual, sir" and "I don't think we've met".

OWNER NOTIFICATION
------------------
First contact from a non-owner also pings the owner once — never on every
message, which is what `notified_at` is for. The owner decides whether to
"follow" that person (`follow`), and that decision is recorded here so the
question is asked exactly once per person.

`follow` is deliberately NOT a permission. Approving someone here writes
them into the real `reply_allowlist` via channels/config.py (see
`cli`-level channels-follow); this field only records that the question
was answered, so permissions.py stays the single source of truth for who
may be answered. Two systems that both decide access is how you end up
with a bot that answers someone every allowlist says it shouldn't.

PEOPLE THE OWNER ADDS BY HAND, AND LINKED ACCOUNTS
-------------------------------------------------
A record normally appears when someone messages the bot. Two other things
create or enrich one, both driven from the Channels panel / `jarvis
channels-add-person`:

  * `manual` records — the owner typed an id or @handle in before that
    person ever wrote. They sit at messages=0 until the first real message,
    which simply continues the same record. If only a HANDLE was known (the
    usual case on Instagram, which never reveals an id until the person
    writes), the record is a `placeholder` keyed by the handle; touch()
    adopts it the first time a real id shows up with that handle and
    migrates the per-person tool limits across (user_perms.py is keyed by
    id, and limits that stayed behind would silently widen access).
  * `linked` — "this Discord account and this Instagram account are the same
    human". One partner per record, stored on BOTH sides. A link is
    IDENTITY ONLY: it shares a name and notes with the model and shows both
    accounts together in the panel. It never shares a permission, never
    makes one account the owner because the other is, and permissions.py
    does not read it — two systems deciding access is the failure the
    follow-state note above already warns about.

  * `name_locked` — the owner typed this person's name by hand. A guest's
    own remember_sender call can no longer overwrite it.

STORAGE
-------
    ~/.jarvis/channels/people.json

Keyed "<platform>:<user_id>". Written through atomic_io like every other
store here — this accumulates slowly over months and is not regenerable
from anything.
"""

import re
import time

from . import PLATFORMS
from .. import atomic_io
from .directory import CHANNELS_DIR

PEOPLE_FILE = CHANNELS_DIR / "people.json"

# Follow states. "unknown" is the starting point for anyone who hasn't
# been asked about yet; "pending" means the owner has been told and hasn't
# answered. Both are non-permissions — see the module docstring.
FOLLOW_UNKNOWN = "unknown"
FOLLOW_PENDING = "pending"
FOLLOW_APPROVED = "approved"
FOLLOW_BLOCKED = "blocked"
FOLLOW_STATES = (FOLLOW_UNKNOWN, FOLLOW_PENDING, FOLLOW_APPROVED, FOLLOW_BLOCKED)

# A name is shown to the model and typed by a stranger, so it is capped and
# stripped of newlines — a "name" containing its own instructions on its
# own line is the obvious injection to try against a block that sits in the
# system prompt.
MAX_NAME_LEN = 48
# What an id or @handle may look like when the owner types one in. Matches
# web/server.js's CHANNEL_USER_ID, which is also what keeps a value from being
# read as a CLI flag.
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_NOTE_LEN = 160
MAX_NOTES = 6


def _load():
    return atomic_io.read_json(PEOPLE_FILE, default={}, expect=dict)


def _save(data):
    return atomic_io.write_json(PEOPLE_FILE, data)


def key(platform, user_id):
    return f"{platform}:{str(user_id or '').strip()}"


def _clean_text(value, limit):
    """One line, length-capped. Control characters and newlines are dropped
    rather than escaped: this text is interpolated into the system prompt,
    and a value that can introduce a line break can introduce a line that
    reads like an instruction."""
    text = " ".join(str(value or "").split())
    return text[:limit].strip()


# A profile picture is a URL the PLATFORM gave us, rendered by the owner's
# browser in the Channels panel — so it is held to an allow-list of image
# CDNs and to https. Nothing a chat guest TYPES ever reaches this field (the
# gateways pass the platform's own avatar URL), but the panel would load
# whatever is stored, and a stored URL is the one place an unexpected host
# could make the owner's browser call out.
AVATAR_HOSTS = ("discordapp.com", "discordapp.net", "discord.com",
                "cdninstagram.com", "fbcdn.net")
MAX_AVATAR_LEN = 300


def _clean_avatar(value):
    """The URL if it is a short https URL on a known image CDN, else ''."""
    text = str(value or "").strip()
    if not text or len(text) > MAX_AVATAR_LEN or not text.startswith("https://"):
        return ""
    host = text[len("https://"):].split("/", 1)[0].split("?", 1)[0].lower()
    host = host.split("@")[-1].split(":")[0]
    if not any(host == h or host.endswith("." + h) for h in AVATAR_HOSTS):
        return ""
    if any(ch in text for ch in " \t\r\n\"'<>"):
        return ""
    return text


def _blank(platform, user_id, handle=""):
    now = time.time()
    return {
        "platform": platform,
        "user_id": str(user_id or ""),
        "handle": _clean_text(handle, MAX_NAME_LEN).lstrip("@").lower(),
        "name": "",
        "avatar": "",
        "notes": [],
        "first_seen": now,
        "last_seen": now,
        "messages": 0,
        "follow": FOLLOW_UNKNOWN,
        "is_owner": False,
        "notified_at": None,
        "manual": False,        # added by the owner, not by a message
        "placeholder": False,   # manual and only a handle is known (user_id == handle)
        "name_locked": False,   # owner typed the name; a guest cannot overwrite it
        "linked": "",           # "<other platform>:<user_id>" of the same person
    }


def get(platform, user_id):
    """The stored record, or None. Never creates."""
    if not str(user_id or "").strip():
        return None
    entry = _load().get(key(platform, user_id))
    return entry if isinstance(entry, dict) else None


def touch(platform, user_id, handle="", is_owner=False, avatar=""):
    """Record that this person just sent an accepted message.

    Creates the record on first contact, bumps last_seen/messages after
    that, and refreshes the handle (people rename themselves). Returns the
    record, which is what the caller renders into the prompt block — so
    the common path is one read, one write, no extra lookup.

    `avatar` is the platform's profile-picture URL when the gateway has one;
    an empty value never erases a picture we already have (Instagram's
    webhook does not carry one, so a later message must not wipe a stored
    picture).

    `is_owner` is stored rather than recomputed at read time because the
    config's `owner` field can be edited later and it's genuinely useful
    to see that someone *was* being treated as the owner during a past
    exchange.
    """
    if platform not in PLATFORMS or not str(user_id or "").strip():
        return _blank(platform, user_id, handle)
    data = _load()
    k = key(platform, user_id)
    entry = data.get(k)
    if not isinstance(entry, dict):
        entry = _adopt_placeholder(data, platform, user_id, handle)
    if not isinstance(entry, dict):
        entry = _blank(platform, user_id, handle)
    if handle:
        entry["handle"] = _clean_text(handle, MAX_NAME_LEN).lstrip("@").lower()
    picture = _clean_avatar(avatar)
    if picture:
        entry["avatar"] = picture
    entry["is_owner"] = bool(is_owner)
    entry["last_seen"] = time.time()
    entry["messages"] = int(entry.get("messages") or 0) + 1
    # The owner is never a follow question — they already own the thing.
    if is_owner:
        entry["follow"] = FOLLOW_APPROVED
    data[k] = entry
    _save(data)
    return entry


def _update(platform, user_id, **fields):
    data = _load()
    k = key(platform, user_id)
    entry = data.get(k)
    if not isinstance(entry, dict):
        entry = _blank(platform, user_id)
    entry.update(fields)
    data[k] = entry
    _save(data)
    return entry


def set_name(platform, user_id, name, manual=False):
    """What this person asked to be called. Cleared by an empty name.

    `manual=True` is the owner typing it in the Channels panel (or `jarvis
    channels-rename`). That locks the name: a later remember_sender call —
    the person telling Jarvis their own name in chat — leaves it alone,
    because the owner's word about who someone is outranks what a stranger
    typed. Clearing the name (empty) unlocks it again."""
    cleaned = _clean_text(name, MAX_NAME_LEN)
    existing = get(platform, user_id)
    if not manual and existing and existing.get("name_locked"):
        return existing
    fields = {"name": cleaned}
    if manual:
        fields["name_locked"] = bool(cleaned)
    return _update(platform, user_id, **fields)


def add_note(platform, user_id, note):
    """Append one short fact about this person.

    Capped at MAX_NOTES, oldest dropped first. This is deliberately a small
    ring buffer and not long-term memory: memory.py is the notebook for
    facts about the *owner's* world, and letting an arbitrary stranger on
    the internet write unbounded entries into the same store that rides
    along in every prompt is not a thing to build on purpose.
    """
    note = _clean_text(note, MAX_NOTE_LEN)
    if not note:
        return get(platform, user_id) or _blank(platform, user_id)
    entry = get(platform, user_id) or _blank(platform, user_id)
    notes = [n for n in (entry.get("notes") or []) if isinstance(n, str)]
    if note not in notes:
        notes.append(note)
    return _update(platform, user_id, notes=notes[-MAX_NOTES:])


def set_follow(platform, user_id, status):
    if status not in FOLLOW_STATES:
        raise ValueError(f"unknown follow state '{status}' — expected one of: "
                         + ", ".join(FOLLOW_STATES))
    return _update(platform, user_id, follow=status)


def set_owner_flag(platform, user_id, flag):
    """Record whether this person is the configured owner RIGHT NOW.

    touch() already refreshes this on every message, but the owner can be
    changed from the Channels panel between two messages, and the panel reads
    this field. The config's `owner` stays the source of truth for who IS the
    owner (permissions.is_owner reads it); this only keeps the record from
    contradicting it until the next message."""
    return _update(platform, user_id, is_owner=bool(flag))


def mark_notified(platform, user_id):
    """Remember that the owner has already been told about this person, so
    they're told once and not on every message."""
    return _update(platform, user_id, notified_at=time.time(),
                   follow=FOLLOW_PENDING)


def needs_owner_notice(entry):
    """Should the owner be pinged about this sender right now?

    Once per person, and never for the owner themselves or for someone
    already decided about. A record that somehow lost its notified_at but
    has an answered follow state is treated as already handled — the point
    is to ask once, and re-asking because of a bookkeeping gap is worse
    than not asking.
    """
    if not isinstance(entry, dict) or entry.get("is_owner"):
        return False
    if entry.get("follow") in (FOLLOW_APPROVED, FOLLOW_BLOCKED, FOLLOW_PENDING):
        return False
    return not entry.get("notified_at")


def all_people(platform=None, follow=None):
    """Every known person, most recently seen first. For `channels-people`."""
    items = []
    for k, v in _load().items():
        if not isinstance(v, dict):
            continue
        if platform is not None and not k.startswith(f"{platform}:"):
            continue
        if follow is not None and (v.get("follow") or FOLLOW_UNKNOWN) != follow:
            continue
        items.append(v)
    # A person added by hand has never been seen; fall back to when they were
    # added so they sort to the top of the list rather than the bottom.
    items.sort(key=lambda it: it.get("last_seen") or it.get("first_seen") or 0,
               reverse=True)
    return items


def resolve_user_id(platform, entry_text):
    """Accept an id, a @handle, or a bare handle and return a stored id.

    Checks this file's own records before falling back to directory.py, so
    `jarvis channels-follow discord @someguy` works off the person you were
    just told about rather than needing the snowflake pasted in.
    """
    raw = str(entry_text or "").strip().lstrip("@").lower()
    if not raw:
        return None
    if raw.isdigit() and get(platform, raw):
        return raw
    for entry in all_people(platform):
        if (entry.get("handle") or "").lower() == raw:
            return entry.get("user_id")
        if (entry.get("name") or "").lower() == raw:
            return entry.get("user_id")
    if raw.isdigit():
        return raw
    from . import directory
    resolved, _err = directory.resolve(platform, entry_text)
    return resolved


# --------------------------------------------------------------------------
# People the owner adds by hand
# --------------------------------------------------------------------------

def _ident_of(text):
    """The id-or-handle the owner typed, or '' when it isn't one. No spaces,
    no leading '-' (it would read as a CLI flag), a bounded length."""
    raw = str(text or "").strip().lstrip("@")
    return raw if IDENT_RE.match(raw) else ""


def _find(data, platform, ident):
    """The record on `platform` whose id or handle is `ident`, or None."""
    low = ident.lower()
    for k, v in data.items():
        if not isinstance(v, dict) or not k.startswith(f"{platform}:"):
            continue
        if str(v.get("user_id") or "").lower() == low:
            return v
        if (v.get("handle") or "").lower() == low:
            return v
    return None


def _new_manual(platform, ident):
    """A blank record for someone who has not written yet.

    A numeric ident is a real platform id. A handle is looked up in
    directory.py (the notebook of handle -> id pairs seen on real messages);
    when that knows it the record is a normal one, otherwise it is a
    placeholder keyed by the handle until the person's first message shows
    their id (see _adopt_placeholder)."""
    if ident.isdigit():
        rec = _blank(platform, ident)
    else:
        from . import directory
        resolved, _err = directory.resolve(platform, "@" + ident)
        if resolved and str(resolved).strip():
            rec = _blank(platform, str(resolved).strip(), ident)
        else:
            rec = _blank(platform, ident.lower(), ident)
            rec["placeholder"] = True
    rec["manual"] = True
    rec["messages"] = 0
    rec["last_seen"] = None
    return rec


def add_person(platform, ident, name=""):
    """Create a record by hand. Returns (record, error).

    Grants NOTHING: no list entry, no tool, no owner status. It only gives
    the person a row in the Channels panel, so every switch there has
    something to act on. An id or @handle that is already known returns that
    record and an error naming it, rather than creating a twin."""
    if platform not in PLATFORMS:
        return None, f"unknown platform '{platform}'"
    typed = str(ident or "").strip()
    ident = _ident_of(typed)
    if not ident:
        return None, ("an id or @handle has no spaces and starts with a "
                      "letter or digit — put their name in the Name box")
    data = _load()
    hit = _find(data, platform, ident)
    if hit:
        return hit, f"{platform} already has {hit.get('name') or hit.get('handle') or hit.get('user_id')}"
    rec = _new_manual(platform, ident)
    # A resolved handle may now collide with an existing record under the id.
    clash = data.get(key(platform, rec["user_id"]))
    if isinstance(clash, dict):
        return clash, f"{platform} already has {clash.get('name') or clash.get('handle') or clash.get('user_id')}"
    cleaned = _clean_text(name, MAX_NAME_LEN)
    if cleaned:
        rec["name"] = cleaned
        rec["name_locked"] = True
    data[key(platform, rec["user_id"])] = rec
    if not _save(data):
        return None, "could not write people.json"
    return rec, ""


def adopt_listed(platform, entries):
    """Give everyone NAMED in an allow-list a record. Returns how many were
    created.

    The Channels panel lists records, and a record is made by a message — so
    someone added to dm/reply/tool_allowlist (Global lists, `channels-allow`,
    a hand-edit) who has not written yet had no row, and every switch on the
    panel needs a row. They get one here. Idempotent, creates nothing for
    "*" or for entries that are not an id/handle, and never touches an
    existing record."""
    if platform not in PLATFORMS:
        return 0
    data = _load()
    made = 0
    for raw in entries or []:
        ident = _ident_of(raw)
        if not ident or _find(data, platform, ident):
            continue
        rec = _new_manual(platform, ident)
        k = key(platform, rec["user_id"])
        if isinstance(data.get(k), dict):
            continue
        data[k] = rec
        made += 1
    if made and not _save(data):
        return 0
    return made


def remove_person(platform, user_id):
    """Delete a hand-added record that never sent a message. Returns
    (ok, error). Anyone who has actually written is blocked, not deleted —
    their history is not regenerable (see the module docstring)."""
    data = _load()
    k = key(platform, user_id)
    rec = data.get(k)
    if not isinstance(rec, dict):
        return False, f"{user_id} isn't in the list"
    if not rec.get("manual") or int(rec.get("messages") or 0) > 0:
        return False, ("only a person you added by hand who has never "
                       "messaged can be removed — block anyone else")
    partner = rec.get("linked")
    if partner and isinstance(data.get(partner), dict):
        data[partner]["linked"] = ""
    del data[k]
    return (True, "") if _save(data) else (False, "could not write people.json")


def _adopt_placeholder(data, platform, user_id, handle):
    """First real message from someone the owner added by handle only.

    Returns the placeholder re-keyed under the real id (and removes the old
    key from `data`), or None when there is nothing to adopt. Per-person tool
    limits and the DM switch live in user_perms.json, keyed by id, so they are
    copied to the real id — a limit left under the handle would stop applying
    the moment the person's real id appeared, which widens access."""
    ident = _ident_of(handle)
    if not ident:
        return None
    old_key = None
    for k, v in data.items():
        if (isinstance(v, dict) and k.startswith(f"{platform}:")
                and v.get("placeholder")
                and (v.get("handle") or "").lower() == ident.lower()):
            old_key = k
            break
    if old_key is None:
        return None
    rec = data.pop(old_key)
    old_id = str(rec.get("user_id") or "")
    rec["user_id"] = str(user_id)
    rec["placeholder"] = False
    new_key = key(platform, user_id)
    try:
        from . import user_perms
        store = user_perms._load()
        old_perm = store.get(user_perms.key(platform, old_id))
        if old_perm is not None:
            store[user_perms.key(platform, user_id)] = old_perm
            store.pop(user_perms.key(platform, old_id), None)
            user_perms._save(store)
    except Exception:  # noqa: BLE001 — unreadable limits already fail closed
        pass
    partner = rec.get("linked")
    if partner and isinstance(data.get(partner), dict):
        data[partner]["linked"] = new_key
    return rec


# --------------------------------------------------------------------------
# Linked accounts (same person, other platform)
# --------------------------------------------------------------------------

def partner(entry):
    """The linked record on the other platform, or None. A dangling link (the
    other record is gone) reads as no link."""
    if not isinstance(entry, dict) or not entry.get("linked"):
        return None
    other = _load().get(entry["linked"])
    return other if isinstance(other, dict) else None


def effective_name(entry):
    """Their own name, else the linked account's, else ''."""
    if not isinstance(entry, dict):
        return ""
    if entry.get("name"):
        return entry["name"]
    other = partner(entry)
    return (other or {}).get("name") or ""


def _candidates(data, platform, text):
    """Records on `platform` an id, @handle or name could mean."""
    low = " ".join(str(text or "").split()).lstrip("@").lower()
    found = []
    for k, v in data.items():
        if not isinstance(v, dict) or not k.startswith(f"{platform}:"):
            continue
        if low in (str(v.get("user_id") or "").lower(),
                   (v.get("handle") or "").lower(),
                   (v.get("name") or "").lower()):
            found.append(v)
    return found


def _break(data, k):
    rec = data.get(k)
    if not isinstance(rec, dict):
        return
    other = rec.get("linked")
    rec["linked"] = ""
    if other and isinstance(data.get(other), dict) and data[other].get("linked") == k:
        data[other]["linked"] = ""


def link(platform, user_id, other_platform, ident):
    """Say two accounts are one person. Returns (ok, error, other_record).

    `ident` is the other account's id, @handle or NAME. A name only matches
    someone already on file (and must be unambiguous); an id or handle nobody
    has used yet creates a hand-added record for it, which is what linking
    an Instagram account that has never written needs. Each record has one
    partner — linking again moves it. Permissions are not shared."""
    if platform not in PLATFORMS or other_platform not in PLATFORMS:
        return False, "unknown platform", None
    if platform == other_platform:
        return False, ("link a Discord account to an Instagram one (or the "
                       "other way round) — not two on the same platform"), None
    data = _load()
    me_key = key(platform, user_id)
    me = data.get(me_key)
    if not isinstance(me, dict):
        return False, f"{user_id} isn't registered on {platform}", None
    text = " ".join(str(ident or "").split())
    if not text:
        return False, "type their id, @handle or name", None
    found = _candidates(data, other_platform, text)
    if len(found) > 1:
        return False, (f"more than one person on {other_platform} matches "
                       f"'{text}' — use their id or @handle"), None
    if found:
        other = found[0]
    else:
        typed = _ident_of(text)
        if not typed:
            return False, (f"nobody on {other_platform} is called '{text}'. "
                           f"Use their id or @handle to link someone who "
                           f"hasn't messaged yet"), None
        other = _new_manual(other_platform, typed)
        data[key(other_platform, other["user_id"])] = other
    other_key = key(other_platform, other["user_id"])
    if me.get("linked") == other_key and other.get("linked") == me_key:
        return True, "", other
    _break(data, me_key)
    _break(data, other_key)
    data[me_key]["linked"] = other_key
    data[other_key]["linked"] = me_key
    if not _save(data):
        return False, "could not write people.json", None
    return True, "", data[other_key]


def unlink(platform, user_id):
    """Forget the link, on both sides. Idempotent."""
    data = _load()
    k = key(platform, user_id)
    if not isinstance(data.get(k), dict):
        return False, f"{user_id} isn't registered on {platform}"
    if not data[k].get("linked"):
        return True, ""
    _break(data, k)
    return (True, "") if _save(data) else (False, "could not write people.json")


def describe(entry):
    """One human-readable line for `jarvis channels-people`."""
    if not isinstance(entry, dict):
        return ""
    who = entry.get("name") or entry.get("handle") or entry.get("user_id") or "?"
    bits = [f"{who}"]
    if entry.get("handle") and entry.get("name"):
        bits.append(f"(@{entry['handle']})")
    bits.append(f"id={entry.get('user_id')}")
    bits.append(f"follow={entry.get('follow') or FOLLOW_UNKNOWN}")
    bits.append(f"msgs={entry.get('messages') or 0}")
    if entry.get("is_owner"):
        bits.append("OWNER")
    return "  ".join(bits)


def prompt_block(entry, platform):
    """The identity block injected into the system prompt for one message.

    Kept to a few lines on purpose — it rides along on every single turn
    of a chat conversation, so it is exactly the kind of text that has to
    justify its tokens. What earns its place:

      * whether this is the owner (changes the entire register of the
        reply, and whether the owner's own long-term memory is theirs to
        talk about)
      * the person's name, if known, so it isn't asked for twice
      * an explicit instruction to ASK for the name when it isn't known,
        because otherwise the model cheerfully carries on forever calling
        a stranger "sir"

    Returns "" for the owner on the CLI/web path (no entry at all), which
    keeps every non-chat ask byte-identical to what it was before.
    """
    if not isinstance(entry, dict):
        return ""
    if entry.get("is_owner"):
        return (
            f"This message arrived over {platform}, from your owner's own "
            f"account. Treat it exactly like a message at the PC."
        )

    other = partner(entry)
    who = entry.get("name") or (other or {}).get("name") or ""
    handle = entry.get("handle") or ""
    label = who or (f"@{handle}" if handle else "someone you don't know")
    lines = [
        f"IDENTITY: this message came over {platform} from {label} — "
        f"NOT your owner. Do not address them as your owner, do not use "
        f"your owner's name for them, and do not repeat anything from "
        f"long-term memory about your owner's life, files, schedule or "
        f"machine. Answer them as a polite stranger would be answered."
    ]
    if not who:
        lines.append(
            "You don't know this person's name yet. Ask what to call them "
            "early in the conversation, and once they tell you, call "
            "remember_sender to save it."
        )
    else:
        lines.append(f"They asked to be called {who}.")
    notes = [n for n in (entry.get("notes") or []) if isinstance(n, str)]
    # A linked account is the same person, so what was learned about them on
    # the other platform applies here. Deliberately NOT done when the other
    # account is the owner's own: this block says "not your owner", and a
    # line saying they are also the owner's account would contradict it.
    # (Linking never grants ownership — see the module docstring.)
    if other and not other.get("is_owner"):
        other_label = (other.get("name") or
                       (f"@{other['handle']}" if other.get("handle") else "")
                       or "an account")
        lines.append(f"They are the same person as {other_label} on "
                     f"{other.get('platform') or 'the other platform'}.")
        for n in other.get("notes") or []:
            if isinstance(n, str) and n not in notes:
                notes.append(n)
    notes = notes[:MAX_NOTES]
    if notes:
        lines.append("What you already know about them: " + "; ".join(notes) + ".")
    return " ".join(lines)
