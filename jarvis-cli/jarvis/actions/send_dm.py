"""Auto-discovered tool: L.20 — send_dm, Jarvis DMs specific known people.

"DM Maryem that I'll be late", from chat or the UI. It is the building block
the plan's L.23 (important-reply notifications) sits on.

WHY THIS IS NOT notify_owner WITH A TARGET ARGUMENT
---------------------------------------------------
actions/notify_owner.py takes no recipient on purpose: a message tool that
takes a target lets any prompt injection reachable from a chat channel turn
Jarvis into a spam relay against arbitrary users. This tool takes a target,
so it is the dangerous sibling, and every control below exists because of
that:

  1. OWNER-ONLY. Same `JARVIS_CHANNEL_SENDER` / `is_owner` guard recent_dms
     uses (D-I10: "the send-DM tool itself ... should stay owner-only
     regardless"). At the PC (no chat context) it is allowed -- the PC is the
     owner's own trusted context; inside a chat it is allowed only when
     channels/permissions.is_owner() said so for THIS sender.
  2. KNOWN CONTACTS ONLY. The recipient is resolved against
     channels/people.py (name / handle / id) and channels/directory.py
     (@handle). Ambiguous or unknown is an error that lists candidates --
     never a guess. The one exception is a numeric id the owner types
     themselves, and it needs an explicit platform.
  3. CONFIRMED. TOOL_CONFIRM_REQUIRED below puts it behind the ordinary
     confirm gate, and policy.py scores it (`send_dm`: 50) and describes the
     exact recipient and text in its dry-run. `dry_run=true` resolves the
     person and returns who and what WITHOUT sending.
  4. RATE LIMITED per recipient and overall, persisted on disk (every
     `jarvis` call is a fresh process, so an in-memory counter would reset
     on every call and limit nothing).
  5. NEVER UNATTENDED -- with exactly one exception. Refuses under
     JARVIS_SCHEDULED / an unattended context outright, EXCEPT for a scheduled
     `tool` job (scheduler._do_tool sets JARVIS_JOB_ACTION=tool) whose approval
     included job_risk.KIND_MESSAGE -- the approval line names the recipient and
     the exact text, and the run may only go to a KNOWN contact. An `ask` job,
     a saved command, a task step and a shell the model started can never
     qualify (they set or clear JARVIS_JOB_ACTION, and an ask job's predicted
     tool list is never given the kind). The plan's L.20 item 8.
  6. LOGGED. Every attempt, delivered or not, is appended to the person's
     channels/transcript.py thread as an outbound `owner_dm` record, so
     `recent_dms` and the audit log show what Jarvis said on the owner's
     behalf.

SEVERAL RECIPIENTS
------------------
`person` is a string for one recipient or a list of strings for several
("person": ["maryem", "discord:123"]). Nothing above is loosened for a list:
every name is resolved on its own with the same never-guess rules, and every
recipient goes through the same blocked / DMs-off checks, the same per-person
and overall on-disk rate limit, the same transcript audit line and the same
awaiting-reply marker, exactly as if the call had been made once per person.
A list is only a convenience (one confirm, one scheduled job to approve).
  * Sending now is best effort per person: a name that is unknown, ambiguous,
    blocked or rate limited fails ALONE, the rest are still sent, and the result
    says who got it and who did not. Nobody is ever sent to twice (two spellings
    of one person count once).
  * Scheduling (`when`) is all or nothing: the owner approves ONE job that
    names the recipients, so silently dropping one of them would approve
    something other than what was asked.
  * At most MAX_RECIPIENTS per call; the overall hourly cap still applies.
A single string, or a one-item list, returns the same shape it always did.

Delivery goes through channels/outbound.dm_person(), beside dm_owner, so
there is one send path. Failure is an expected steady state, not an
exception, and the result names the cause: a Discord bot can only DM someone
who shares a server with it and allows server-member DMs, and Instagram only
inside the 24-hour window the other person opens by messaging the bot first.
"""

import json
import os
import time

# Must match channels.base.SENDER_ENV. Not imported -- same reasoning as
# actions/recent_dms.py and actions/channel_people.py: this file is imported
# during tools.py's own initialization, and reaching back into a package that
# imports ai_client risks the circular import actions/dev_agent.py documents.
SENDER_ENV = "JARVIS_CHANNEL_SENDER"

# A DM is a short note. Discord chunks at ~1900 so a longer text would be
# split into several pings; refuse instead of silently spamming.
MAX_MESSAGE_CHARS = 1500

# Rate limit window and ceilings. Per recipient is the one that matters (a
# loop messaging one person); the overall cap stops a loop that walks the
# whole contact list.
RATE_WINDOW_SECONDS = 3600
MAX_PER_RECIPIENT = 5
MAX_OVERALL = 20

# Most people one call may address. Well under MAX_OVERALL on purpose: a list is
# for "tell the Among Us group", not for walking the whole contact list.
MAX_RECIPIENTS = 100

# Candidates / known contacts shown in an error, so a long people.json never
# floods the result.
MAX_LISTED = 8


def _current_sender():
    """Mirrors actions/recent_dms.py's helper of the same name."""
    raw = os.environ.get(SENDER_ENV) or ""
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("platform") or not data.get("user_id"):
        return None
    return data


def _job_authorizes_message():
    """True only for a scheduled `tool` job whose approval included the
    "message" kind (job_risk.KIND_MESSAGE). scheduler._do_tool is the only
    caller that sets JARVIS_JOB_ACTION=tool; every other launcher sets or clears
    it, so an ask job -- or a shell the model started -- can never reach this."""
    if os.environ.get("JARVIS_JOB_ACTION") != "tool":
        return False
    try:
        from .. import policy
        return "message" in policy.approved_kinds_from_env()
    except Exception:  # noqa: BLE001 -- fail closed
        return False


def _is_unattended():
    """True for a scheduled job or any run with nobody watching. Keys off the
    same explicit signals policy.unattended_context_from_env() does (not
    imported: policy pulls in job_risk/tool_registry, and this runs during
    discovery)."""
    if os.environ.get("JARVIS_SCHEDULED"):
        return True
    return os.environ.get("JARVIS_CONTEXT") in ("scheduled", "unattended")


# ---------------------------------------------------------------------------
# Resolving "who"
# ---------------------------------------------------------------------------


def _label(rec):
    return rec.get("name") or (f"@{rec['handle']}" if rec.get("handle") else "") \
        or str(rec.get("user_id") or "?")


def _describe(rec):
    return {
        "platform": rec.get("platform"),
        "name": rec.get("name") or "",
        "handle": rec.get("handle") or "",
        "user_id": str(rec.get("user_id") or ""),
    }


def _closeness(needle, rec):
    """0..1: how close `needle` is to any word of this contact's name or
    handle. Only ever used to ORDER and LABEL suggestions -- never to pick."""
    import difflib
    import re
    words = []
    for field in (rec.get("name"), rec.get("handle")):
        field = (field or "").lower()
        if field:
            words.append(field)
            words.extend(w for w in re.split(r"[^a-z0-9]+", field) if w)
    return max((difflib.SequenceMatcher(None, needle, w).ratio() for w in words),
               default=0.0)


NEAR_MISS_RATIO = 0.75      # "mariem" vs "maryem" = 0.83; "mar" vs "maryem" = 0.67
MAX_NEAR_MISSES = 3


def _known_hint(people_mod, platforms, needle=""):
    out = []
    for platform in platforms:
        for rec in people_mod.all_people(platform):
            if rec.get("is_owner"):
                continue
            out.append(rec)
    if needle:
        out.sort(key=lambda r: -_closeness(needle, r))   # stable: ties keep order
    return [_describe(r) for r in out[:MAX_LISTED]]


def _near_misses(people_mod, platforms, needle):
    """Known contacts whose name/handle is a close SPELLING of what the owner
    said ("mariem" for "maryem"). Suggestions to confirm, never a match:
    _resolve() still refuses to guess a recipient."""
    if len(needle) < 3:
        return []
    scored = []
    for platform in platforms:
        for rec in people_mod.all_people(platform):
            if rec.get("is_owner") or not rec.get("user_id"):
                continue
            ratio = _closeness(needle, rec)
            if ratio >= NEAR_MISS_RATIO:
                scored.append((-ratio, _describe(rec)))
    scored.sort(key=lambda x: x[0])
    return [d for _, d in scored[:MAX_NEAR_MISSES]]


def _resolve(person, platform_arg, for_dm=True):
    """(record, None) or (None, error_result). Never guesses.

    Matching, in order, stopping at the first tier that finds anyone:
      1. the whole text equals a saved name, handle or user id
         (case-insensitive, leading @ ignored)
      2. the text equals one whole WORD of a saved name ("maryem" for
         "Maryem Ben Ali") -- so the common phrasing works, but "mar" does
         not match "Maryem"
      3. directory.py's @handle table (people who have messaged the bot but
         have no people.json record)
      4. a bare numeric id typed by the owner, with an explicit platform
    More than one hit in a tier is an error listing them, not a pick.

    `for_dm=False` is for the per-person memory tools (L.21/L.22): the same
    matching, but a blocked person or one with DMs switched off may still be
    remembered about -- those two refusals are about MESSAGING them.
    """
    from ..channels import PLATFORMS, directory, people

    raw = (person or "").strip()
    platforms = [platform_arg] if platform_arg else list(PLATFORMS)

    # "discord:123456" / "instagram:abc" -- the platform:id form.
    head, sep, tail = raw.partition(":")
    if sep and head.strip().lower() in PLATFORMS and tail.strip():
        forced = head.strip().lower()
        if platform_arg and platform_arg != forced:
            return None, {"ok": False,
                          "error": (f"'{raw}' names {forced} but platform is "
                                    f"'{platform_arg}'")}
        platforms, raw = [forced], tail.strip()

    needle = raw.lstrip("@").strip().lower()
    if not needle:
        return None, {"ok": False, "error": "person is required"}

    records = []
    for platform in platforms:
        for rec in people.all_people(platform):
            # A hand-added person known only by handle (placeholder) has no
            # platform id yet, so there is nothing a DM could be addressed
            # to. They become sendable the moment they write once.
            if (isinstance(rec, dict) and rec.get("user_id")
                    and not rec.get("placeholder")):
                records.append(rec)

    def _fields(rec):
        return (str(rec.get("user_id") or "").lower(),
                (rec.get("handle") or "").lower(),
                (rec.get("name") or "").lower())

    hits = [r for r in records if needle in _fields(r)]
    if not hits and len(needle) >= 2:
        hits = [r for r in records
                if needle in (r.get("name") or "").lower().split()]

    if not hits:
        # Tier 3: someone who has messaged the bot but has no people record.
        for platform in platforms:
            found, _err = directory.resolve(platform, "@" + needle)
            if found:
                hits.append({"platform": platform, "user_id": str(found),
                             "handle": needle, "name": ""})

    if not hits and needle.isdigit():
        # Tier 4: the owner pasted a raw id. It needs a platform, because the
        # same digits mean different people (or nobody) on different apps.
        if len(platforms) != 1:
            return None, {
                "ok": False,
                "error": f"'{raw}' is a bare id, so say which platform it is on",
                "hint": "Pass platform='discord' or platform='instagram'.",
            }
        hits.append({"platform": platforms[0], "user_id": needle,
                     "handle": "", "name": "", "_unknown": True})

    # Same person found by two tiers / two tables is still one person.
    seen, unique = set(), []
    for rec in hits:
        k = (rec.get("platform"), str(rec.get("user_id")))
        if k not in seen:
            seen.add(k)
            unique.append(rec)

    if not unique:
        err = {
            "ok": False,
            "error": f"I don't know anyone called '{raw}'",
            "hint": ("Jarvis only knows people who have messaged the bot. "
                     "Ask them to DM it first, or give a numeric id with a "
                     "platform."),
            "known_contacts": _known_hint(people, platforms, needle),
        }
        close = _near_misses(people, platforms, needle)
        if close:
            err["did_you_mean"] = close
            err["hint"] = ("Nobody matches that exactly, but did_you_mean lists "
                           "close spellings of known contacts. They are "
                           "suggestions, not matches: confirm with the owner "
                           "which one they mean before sending, never pick.")
        return None, err
    if len(unique) > 1:
        return None, {
            "ok": False,
            "error": f"'{raw}' matches more than one person",
            "hint": ("Ask the owner which one -- repeat the call with a "
                     "handle, a platform, or platform:id."),
            "candidates": [_describe(r) for r in unique[:MAX_LISTED]],
        }

    rec = unique[0]
    if rec.get("is_owner"):
        return None, {"ok": False,
                      "error": f"{_label(rec)} is the owner's own account",
                      "hint": "Use notify_owner to message the owner."}
    if for_dm and (rec.get("follow") or "") == "blocked":
        return None, {"ok": False,
                      "error": f"{_label(rec)} is blocked, so I won't message them",
                      "hint": ("Unblock them first (`jarvis channels-follow`) "
                               "if the owner really wants this.")}
    # The owner can also switch DMs off for ONE person without blocking them
    # (Menu > Channels > Permissions). This only ever adds a refusal: it is
    # one more reason to say no, so none of the limits above are loosened.
    # An unreadable permissions file answers "no" (user_perms.dm_allowed).
    from ..channels import user_perms
    if for_dm and not user_perms.dm_allowed(rec.get("platform") or "", rec.get("user_id") or ""):
        return None, {"ok": False,
                      "error": f"DMs to {_label(rec)} are switched off, so I won't message them",
                      "hint": ("The owner can switch this on in Menu > Channels > "
                               "Permissions, or with `jarvis channels-user`.")}
    return rec, None


# ---------------------------------------------------------------------------
# Rate limit (on disk -- every jarvis call is a fresh process)
# ---------------------------------------------------------------------------


def _rate_file():
    from ..channels import directory
    return directory.CHANNELS_DIR / "dm_sends.json"


def _load_sends(now):
    from .. import atomic_io
    data = atomic_io.read_json(_rate_file(), default={}, expect=dict)
    cutoff = now - RATE_WINDOW_SECONDS
    clean = {}
    for k, stamps in data.items():
        if isinstance(stamps, list):
            kept = [t for t in stamps if isinstance(t, (int, float)) and t > cutoff]
            if kept:
                clean[k] = kept
    return clean


def _rate_check(platform, user_id, now):
    """None when allowed, else an error string saying when to retry."""
    sends = _load_sends(now)
    mine = sends.get(f"{platform}:{user_id}", [])
    everyone = sends.get("_all", [])
    if len(mine) >= MAX_PER_RECIPIENT:
        wait = int(min(mine) + RATE_WINDOW_SECONDS - now) // 60 + 1
        return (f"rate limit: already sent {len(mine)} messages to this person "
                f"in the last hour (max {MAX_PER_RECIPIENT}); try again in "
                f"~{wait} min")
    if len(everyone) >= MAX_OVERALL:
        wait = int(min(everyone) + RATE_WINDOW_SECONDS - now) // 60 + 1
        return (f"rate limit: {len(everyone)} DMs sent in the last hour "
                f"(max {MAX_OVERALL}); try again in ~{wait} min")
    return None


def _rate_record(platform, user_id, now):
    from .. import atomic_io
    sends = _load_sends(now)
    for k in (f"{platform}:{user_id}", "_all"):
        sends.setdefault(k, []).append(now)
    atomic_io.write_json(_rate_file(), sends)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _thread_for(platform, user_id):
    """Which transcript thread this person's DMs live in, so the owner's DM
    shows up next to the conversation it belongs to. Instagram threads are
    keyed by the sender id; Discord's by the DM *channel* id, which only the
    inbound log knows -- so look for this user's thread, bounded, and fall
    back to a stable per-user name."""
    if platform != "discord":
        return str(user_id)
    try:
        from ..channels import permissions, transcript
        for cand in transcript.list_threads(platform=platform)[:60]:
            recs = transcript.read_thread(platform, cand["thread_id"], limit=20)
            # DM threads only: this person's message in a server channel or a
            # group is also an inbound record from them, and the owner's DM
            # audit line must never land in a room other people can read.
            if any(r.get("dir") == "in" and str(r.get("user_id")) == str(user_id)
                   and r.get("context") == permissions.CTX_DM
                   for r in recs):
                return cand["thread_id"]
    except Exception:  # noqa: BLE001 -- a log lookup must never block a send
        pass
    return f"dm-{user_id}"


def _log(platform, user_id, text, ok, error):
    try:
        from ..channels import transcript
        transcript.log_outbound(platform, _thread_for(platform, user_id), text,
                                kind="owner_dm", ok=ok, error=error)
    except Exception:  # noqa: BLE001 -- the audit line is best-effort
        pass


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def _people_arg(raw):
    """Normalise the `person` argument -> (list of non-empty strings, error).

    A string is one recipient; a list is several. A JSON-encoded list in a
    string (some models stringify arrays) is unpacked too. Repeats of the same
    text are dropped here; two different spellings of ONE person are caught
    after resolution, in _send_many.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                decoded = json.loads(text)
            except (ValueError, TypeError):
                decoded = None
            if isinstance(decoded, list):
                raw = decoded
    if raw is None:
        return [], None
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out, seen = [], set()
    for item in items:
        if item is None or isinstance(item, bool) or not isinstance(item, (str, int)):
            if item is None:
                continue
            return [], {"ok": False,
                        "error": "each entry in person must be a name, an @handle or platform:id text"}
        text = str(item).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out, None


def _prepare(person, platform_arg, unattended):
    """Resolve one recipient -> (rec, who, error_result)."""
    rec, err = _resolve(person, platform_arg)
    if err:
        return None, None, err
    if unattended and rec.get("_unknown"):
        # An approved job names someone Jarvis knows. A bare numeric id is the
        # "owner typed it just now" path; nobody is typing at 3am.
        return None, None, {
            "ok": False, "error": "a scheduled message can only go to a known contact",
            "hint": "Have them DM the bot once, then schedule it again."}
    return rec, _describe(rec), None


def _deliver(rec, message):
    """Rate check, send, audit line, awaiting marker for ONE resolved person.
    The whole of what used to be the tail of tool_send_dm, so one person and
    many people share exactly the same limits and bookkeeping."""
    from ..channels import outbound

    platform, user_id = rec["platform"], str(rec["user_id"])
    who = _describe(rec)
    now = time.time()
    limited = _rate_check(platform, user_id, now)
    if limited:
        return {"ok": False, "error": limited, "to": who}

    ok, detail = outbound.dm_person(platform, user_id, message)
    _log(platform, user_id, message, ok, "" if ok else str(detail))
    if not ok:
        return {"ok": False, "error": f"could not message {_label(rec)}: {detail}",
                "to": who,
                "hint": "Check `jarvis channels-status`; nothing was delivered."}

    _rate_record(platform, user_id, now)
    # L.23: from here on a reply from them may be worth telling the owner
    # about. Best effort -- a failure here must not turn a delivered message
    # into an error.
    try:
        from ..channels import awaiting
        awaiting.mark(platform, user_id, ask=message, source="send_dm")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "sent_to": who, "detail": detail,
            "known_contact": not rec.get("_unknown", False),
            "chars": len(message)}


def _send_many(people_list, message, platform_arg, unattended, dry_run, when):
    """The list form of `person`. See "SEVERAL RECIPIENTS" in the module
    docstring for the rules; each person still goes through _prepare/_deliver."""
    resolved, problems, seen = [], [], {}
    for text in people_list:
        rec, who, err = _prepare(text, platform_arg, unattended)
        if err:
            problems.append({"person": text, **{k: v for k, v in err.items() if k != "ok"}})
            continue
        key = (rec["platform"], str(rec["user_id"]))
        if key in seen:
            # "maryem" and "discord:111" are one person: they get it once.
            problems.append({"person": text, "skipped": True,
                             "note": f"same person as '{seen[key]}', so not sent twice"})
            continue
        seen[key] = text
        resolved.append((text, rec, who))
    # A skipped duplicate is not a failure; everything else in `problems` is.
    failures = [p for p in problems if not p.get("skipped")]

    if dry_run:
        out = {"ok": not failures, "dry_run": True,
               "would_send_to": [w for _, _, w in resolved],
               "recipients": len(resolved), "message": message,
               "note": "Nothing was sent."}
        if when:
            out["can_schedule"] = (not failures and
                                   not any(r.get("_unknown") for _, r, _ in resolved))
            out["note"] = "Nothing was sent or scheduled."
        if failures or len(problems) != len(failures):
            out["problems"] = problems
        if failures:
            out["error"] = f"{len(failures)} recipient(s) can't be messaged"
        return out

    if when:
        # Attended path only (the unattended refusal already ran). ALL or
        # nothing: the owner approves one job that names these people.
        if failures:
            return {"ok": False, "scheduled": False,
                    "error": ("nothing was scheduled: " + str(len(failures)) +
                              " of the recipients can't be messaged"),
                    "problems": problems,
                    "hint": "Fix or drop those people and schedule it again."}
        return _schedule_send([r for _, r, _ in resolved], message, when)

    results = []
    for text, rec, who in resolved:
        res = _deliver(rec, message)
        res["person"] = text
        results.append(res)
    sent = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")] + failures
    out = {"ok": not failed, "recipients": len(people_list),
           "sent_count": len(sent), "failed_count": len(failed),
           "sent_to": [r["sent_to"] for r in sent],
           "chars": len(message)}
    if failed:
        out["failed"] = failed
        out["error"] = (f"{len(sent)} of {len(people_list)} sent; "
                        f"{len(failed)} could not be messaged")
        out["hint"] = ("Do NOT resend to anyone in sent_to -- they already got "
                       "it. Tell the owner who it failed for and why.")
    skipped = [p for p in problems if p.get("skipped")]
    if skipped:
        out["skipped"] = skipped
    return out


def tool_send_dm(args):
    from ..channels import PLATFORMS

    sender = _current_sender()
    if sender is not None and not sender.get("is_owner"):
        return {
            "ok": False,
            "error": "send_dm is owner-only",
            "hint": ("Only the owner can ask Jarvis to message other people "
                     "-- this isn't available to whoever is messaging you "
                     "over chat right now."),
        }
    unattended = _is_unattended()
    if unattended and not _job_authorizes_message():
        return {
            "ok": False,
            "error": "send_dm does not run unattended",
            "hint": ("Jarvis won't message another person from a scheduled "
                     "or unattended run. Ask for it in a chat, or at the PC."),
        }

    args = args or {}
    people_list, perr = _people_arg(args.get("person"))
    if perr:
        return perr
    message = str(args.get("message") or "").strip()
    if not people_list:
        return {"ok": False, "error": "person is required"}
    if len(people_list) > MAX_RECIPIENTS:
        return {"ok": False,
                "error": (f"{len(people_list)} recipients; the limit is "
                          f"{MAX_RECIPIENTS} per call"),
                "hint": "Split it into smaller groups, or ask the owner who matters most."}
    if not message:
        return {"ok": False, "error": "message is required"}
    if len(message) > MAX_MESSAGE_CHARS:
        return {"ok": False,
                "error": (f"message is {len(message)} characters; the limit "
                          f"is {MAX_MESSAGE_CHARS}"),
                "hint": "Shorten it -- a DM is a short note."}

    platform_arg = str(args.get("platform") or "").strip().lower()
    if platform_arg in ("", "any", "auto"):
        platform_arg = ""
    elif platform_arg not in PLATFORMS:
        return {"ok": False,
                "error": (f"unknown platform '{platform_arg}' -- use discord, "
                          "instagram, or leave blank")}

    when = str(args.get("when") or "").strip()
    dry_run = bool(args.get("dry_run"))

    if len(people_list) > 1:
        return _send_many(people_list, message, platform_arg, unattended,
                          dry_run, when)

    # ONE recipient (a string, or a one-item list): the original shape.
    rec, who, err = _prepare(people_list[0], platform_arg, unattended)
    if err:
        return err

    if dry_run:
        out = {"ok": True, "dry_run": True, "would_send_to": who,
               "known_contact": not rec.get("_unknown", False),
               "message": message, "note": "Nothing was sent."}
        if when:
            out["can_schedule"] = not rec.get("_unknown", False)
            out["note"] = "Nothing was sent or scheduled."
            if not out["can_schedule"]:
                out["hint"] = _UNKNOWN_SCHEDULE_HINT
        return out

    if when:
        # Attended path only: _is_unattended() already refused above, so a
        # scheduled run can never schedule another send.
        return _schedule_send([rec], message, when)

    return _deliver(rec, message)


_UNKNOWN_SCHEDULE_HINT = (
    "A scheduled DM can only go to a known contact (someone who has messaged "
    "the bot, or an @handle Jarvis has seen), because nobody is there to "
    "confirm an unverified id when it fires. Have them DM the bot once, then "
    "schedule it again. Sending it right now to a typed id still works.")


def _schedule_send(recs, message, when):
    """Create the one-off scheduled job that sends `message` later, to one
    person or to a list of them (ONE job, so the owner approves one line).

    This is the friendly front door to schedule_task(do='tool', tool='send_dm'):
    the model makes ONE send_dm call with `when` instead of nesting a tool call
    inside schedule_task. It loosens nothing. Each recipient is resolved and
    checked NOW (known contact, not blocked, DMs on), the job is created
    untrusted so it parks at needs_approval until the owner approves it, and
    at fire time send_dm still runs its own unattended checks, the on-disk rate
    limit and the known-contact rule (see _job_authorizes_message) for every
    recipient.
    """
    from .. import scheduler
    # The approval gate asks tool_safety whether send_dm needs confirming, and
    # that registry is filled when jarvis.tools has loaded. It always has in a
    # real run; importing it here keeps this path from depending on that.
    from .. import tools as _tools  # noqa: F401

    many = len(recs) > 1
    whos = [_describe(r) for r in recs]
    who = whos if many else whos[0]
    if any(r.get("_unknown") for r in recs):
        return {"ok": False, "scheduled": False,
                "error": "a scheduled message can only go to a known contact",
                "to": who, "hint": _UNKNOWN_SCHEDULE_HINT}
    try:
        trig = scheduler.normalize_trigger(when=when)
    except scheduler.SchedulerError as e:
        return {"ok": False, "scheduled": False, "error": str(e),
                "hint": "Give a clear time, e.g. 'in 6 hours 30 minutes' or "
                        "'tomorrow at 9am'."}
    if trig.get("type") != "at":
        return {"ok": False, "scheduled": False,
                "error": "a scheduled DM is a one-off, so `when` must be a single point in time",
                "hint": "Use something like 'in 2 hours' or 'tomorrow at 9am', not a repeat or an event."}
    ids = [f"{r['platform']}:{r['user_id']}" for r in recs]
    if many:
        # Each id carries its own platform prefix, so no top-level `platform`.
        args = {"person": ids, "message": message}
        names = ", ".join(_label(r) for r in recs[:3])
        if len(recs) > 3:
            names += f" +{len(recs) - 3}"
    else:
        args = {"person": ids[0], "platform": recs[0]["platform"],
                "message": message}
        names = _label(recs[0])
    action = {"type": "tool", "tool": "send_dm", "report": True, "args": args}
    try:
        job = scheduler.create(
            kind="task", title=f"DM {names}: {message[:60]}",
            when=when, action=action, max_runs=1)
    except scheduler.SchedulerError as e:
        return {"ok": False, "scheduled": False, "error": str(e)}
    summary = scheduler.summarize(job)
    out = {"ok": True, "scheduled": True, "id": job["id"], "to": who,
           "message": message, "when": summary.get("when"),
           "next_run": summary.get("next_run"), "in": summary.get("in"),
           "status": job["status"], "nothing_sent_yet": True}
    if job["status"] != scheduler.STATUS_NEEDS_APPROVAL:
        # Gate did not engage, so the job has no approval and send_dm would
        # refuse it at fire time. Don't leave a dead job behind or claim success.
        try:
            scheduler.delete_job(job["id"])
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "scheduled": False,
                "error": "could not park the scheduled DM for approval, so nothing was scheduled"}
    if job["status"] == scheduler.STATUS_NEEDS_APPROVAL:
        out["needs_approval"] = True
        out["note"] = (
            "Created but NOT active yet: it is parked until the owner approves "
            "it in the web console's Scheduled panel or with `jarvis "
            "sched-approve %s`. You cannot approve it yourself. Tell them "
            "that, and that it only fires after approval." % job["id"])
    return out


TOOL_SCHEMAS = [
    {
        "name": "send_dm",
        "description": (
            "Send a direct message to one specific person -- or to several "
            "people at once, by passing `person` as a list -- on Discord or "
            "Instagram, as the Jarvis bot, on the owner's request (e.g. \"DM "
            "Maryem that I'll be late\"). Only people Jarvis already knows "
            "(they have messaged the bot) can be reached; an unknown or "
            "ambiguous name returns an error with candidates -- ask the "
            "owner, never guess. With a list, everyone who can be messaged "
            "gets it and the result says who did not; don't resend to the "
            "ones it already reached. Owner-only, always asks for "
            "confirmation, rate limited. To send LATER ('dm X in 6 hours') pass `when` -- "
            "do not say you lack a scheduler. To message the "
            "owner themselves use notify_owner instead. Pass dry_run=true to "
            "see who a name resolves to without sending."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "person": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": (
                        "Who to message: a saved name, an @handle, or "
                        "platform:id (e.g. discord:123456789). To message "
                        f"several people pass a list (max {MAX_RECIPIENTS}), "
                        "e.g. [\"maryem\", \"discord:123456789\"]."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": (
                        f"The text to send, max {MAX_MESSAGE_CHARS} "
                        "characters. Send what the owner asked you to pass "
                        "on -- don't add promises or claim to be the owner."
                    ),
                },
                "platform": {
                    "type": "string",
                    "enum": ["discord", "instagram", "any"],
                    "description": (
                        "Which app. Omit when the name is unique across "
                        "apps; required for a bare numeric id."
                    ),
                },
                "when": {
                    "type": "string",
                    "description": (
                        "Send LATER instead of now, in the owner's own words "
                        "('in 6 hours 30 minutes', 'tomorrow at 9am'). One "
                        "time only, known contacts only. Creates a scheduled "
                        "job the owner must approve before it fires. Omit to "
                        "send immediately."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "Resolve the person and return who/what would be "
                        "sent, without sending. Default false."
                    ),
                },
            },
            "required": ["person", "message"],
        },
    },
]

TOOLS = {"send_dm": tool_send_dm}

# Joins the existing "channels" group (notify_owner, recent_dms,
# channel_people): all are "Jarvis and a person over a chat app", and a
# separate group would compete with them for the router's two-group budget on
# the same trigger phrases.
TOOL_GROUP = "channels"

TOOL_KEYWORDS = {
    "send_dm": {
        "send a dm": 10,
        "send dm": 10,
        "send a message to": 10,
        "send message to": 10,
        "send her a message": 10,
        "send him a message": 10,
        "send them a message": 9,
        "dm her": 10,
        "dm him": 10,
        "dm them": 9,
        "message her": 9,
        "message him": 9,
        "text her": 9,
        "text him": 9,
        "let her know": 8,
        "let him know": 8,
        "tell her": 7,
        "tell him": 7,
        # Bare "dm" for "dm maryem that ...". Not when it is the owner being
        # messaged (notify_owner's "dm me") or the inbox being read
        # (recent_dms' phrases) -- the router counts both of those already.
        "dm": {
            "weight": 7,
            "not_with": ["me", "anyone", "anybody", "who", "recent", "new",
                         "unread", "lately", "history"],
        },
    },
}

# No TOOL_PACK_INSTRUCTION here on purpose. "channels" is shared by four files and
# tools.AUTO_TOOL_PACK_INSTRUCTIONS keeps only the LAST discovered file's text, so a
# second copy silently replaced the real one (actions/channel_people.py). The one
# instruction for the group, send_dm included, lives there; tests/
# test_channels_pack_instruction.py fails if another file in the group adds its own.

# Sends a message to a third party: always behind the ordinary confirm gate.
# No second-provider review -- the confirm already shows the exact recipient
# and text, which is all a reviewer would see (and AI review per DM is the
# confirm churn F.12 complains about).
TOOL_CONFIRM_REQUIRED = {"send_dm"}
TOOL_AI_REVIEW = set()

# No inline TEST_CHECKLIST: this ships with jarvis, so its entry lives in
# web/public/test-checklist-data.js (the "channels" group), as recent_dms's
# does.
