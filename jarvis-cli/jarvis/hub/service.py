"""What the tools, the panel and a future CLI call (dicts in, dicts out, never raises).

Tool handlers in actions/download_hub.py are thin wrappers over this module, so the rules
live once: what may be queued, what a pause means for each state, which actions a row
offers, and what is never opened or deleted.

A tool call is a short-lived process, so nothing here runs a transfer.  It enqueues rows,
records control requests, and reads state; the daemon does the work (daemon.py).

Control requests (pause / cancel):
  * the row is not held by a worker (queued, paused, stalled, failed ...), or the daemon is
    not running -> the change is applied directly;
  * a worker holds it and the daemon is alive -> `items.control` is set and the daemon's
    next tick (<= ~1 s) stops the worker and performs the change.
"""

import datetime
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import engines, naming, netguard, notify, router, settings as settings_mod, states
from .store import Store

SOURCE_RE_PREFIXES = ("http://", "https://")


def _store(store):
    return store or Store()


def _err(message, **extra):
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


# --------------------------------------------------------------------------- daemon / disk

def daemon_view(store):
    info = store.daemon_info()
    return {"alive": bool(info["alive"]), "pid": info["pid"],
            "age_s": None if info["age_s"] is None else round(info["age_s"], 1)}


def _free_bytes(path):
    """Free space where `path` will live (the nearest folder that exists)."""
    probe = Path(path)
    for _ in range(12):
        if probe.exists():
            try:
                return shutil.disk_usage(str(probe)).free
            except OSError:
                return None
        if probe.parent == probe:
            break
        probe = probe.parent
    return None


# --------------------------------------------------------------------------- adding

def _initial_title(source):
    name = naming.filename_from_url(source)
    if name:
        return naming.sanitize_filename(name, default="download")
    try:
        from urllib.parse import urlsplit
        return urlsplit(source).hostname or "download"
    except ValueError:
        return "download"


def preview(text, engine=None, store=None):
    """Router decision for one input without queueing anything (the panel's add box)."""
    store = _store(store)
    settings = store.get_settings()
    decision = router.route(text, engine=engine, settings=settings)
    if decision["ok"] and decision["engine"] == "http":
        try:
            netguard.check_url(decision["source"], settings["allow_private_hosts"])
        except netguard.GuardError as exc:
            decision = dict(decision, ok=False, available=False, problem=str(exc))
    return decision


ITEM_OPEN_VALUES = ("default",) + settings_mod.OPEN_MODES
ITEM_NOTIFY_VALUES = ("default", "on", "off")
ITEM_NOTIFY_EVENTS = ("started", "done", "failed", "stalled")      # the per-row events (not batch / queue)


def _channel_names():
    """The notifier's channel names.  Imported on demand: the notifier pulls in a lot."""
    from .. import notifier
    return notifier


def normalize_channels(value):
    """A list or comma-separated string of notifier channels -> ([canonical names] or None, error).

    None means 'follow the owner's notification settings' ("default", "", an empty list)."""
    if value is None:
        return None, None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "default", "auto", "none"):
            return None, None
        value = [p for p in text.replace(";", ",").replace(" ", ",").split(",") if p]
    if not isinstance(value, (list, tuple)):
        return None, "notify_channels must be a list like [\"discord\"]"
    if not value:
        return None, None
    notifier = _channel_names()
    out, bad = [], []
    for raw in value:
        # D2: every action in the notification catalog is allowed for a download, a behaviour
        # (`persistent`, `confirm`) as well as a place (`desktop`, `small_toast`, ...).
        chans, behaviours = notifier.canonical_actions([raw])
        names = chans + behaviours
        if not names:
            bad.append(str(raw)[:30])
        for name in names:
            if name not in out:
                out.append(name)
    if bad:
        from .. import notify_registry
        known = [a["id"] for a in notify_registry.actions()]
        return None, "unknown notification type %s; the types are: %s" % (
            ", ".join(repr(b) for b in bad), ", ".join(known))
    return out, None


def normalize_events(value):
    """A list or comma-separated string of started/done/failed/stalled -> ([names] or None, error)."""
    if value is None:
        return None, None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "default", "auto"):
            return None, None
        value = [p for p in text.replace(";", ",").replace(" ", ",").split(",") if p]
    if not isinstance(value, (list, tuple)):
        return None, "notify_events must be a list like [\"done\", \"failed\"]"
    if not value:
        return None, None
    out, bad = [], []
    for raw in value:
        name = str(raw).strip().lower()
        if name not in ITEM_NOTIFY_EVENTS:
            bad.append(str(raw)[:30])
        elif name not in out:
            out.append(name)
    if bad:
        return None, "unknown notification type %s; choose from: %s" % (
            ", ".join(repr(b) for b in bad), ", ".join(ITEM_NOTIFY_EVENTS))
    return out, None


# --------------------------------------------------------------------------- timed starts
#
# A download can be told to start later, the way a daemon can be given a `next_start`
# (daemons.schedule): the same plain-language grammar (jarvis/timespec.py), the same one-shot
# meaning.  It is stored as a PAUSED hub row carrying `options.start_at` (the owner's wording
# resolved to an ISO time) and `next_attempt_at` (the same moment as epoch seconds); the daemon
# moves it to `queued` when the time comes, or on its next start if it was down at the time.
# Resuming it by hand starts it at once and drops the schedule.

MAX_SCHEDULE_DAYS = 366
START_NOW_WORDS = ("now", "right now", "immediately", "asap", "start now")
CLEAR_SCHEDULE_WORDS = ("", "clear", "none", "off", "remove", "unschedule", "cancel")


def parse_start(text, now=None):
    """What the owner typed ("tomorrow 3am", "in 2 hours", "2026-11-02 01:30") -> ({iso, epoch}, error).

    Always a time in the future, within about a year; a recurring phrase ("every night") is
    refused because a download starts once."""
    from .. import timespec
    raw = str(text or "").strip()
    now_dt = now or datetime.datetime.now()
    if raw.lower().startswith(("every ", "each ", "daily", "hourly", "weekly", "monthly", "nightly")):
        return None, "a download starts once; give a single time like 'tomorrow at 3am' or 'in 2 hours'"
    try:
        when = timespec.parse_when(raw, now=now_dt)
    except timespec.TimeSpecError as exc:
        return None, "couldn't understand %r as a start time: %s" % (raw[:60], exc)
    if when <= now_dt:
        return None, "%s has already passed; give a time in the future" % timespec.to_iso(when)
    if when > now_dt + datetime.timedelta(days=MAX_SCHEDULE_DAYS):
        return None, "a start time more than a year away isn't allowed"
    return {"iso": timespec.to_iso(when), "epoch": when.timestamp()}, None


def _options_without_schedule(item):
    options = dict(item.get("options") or {})
    options.pop("start_at", None)
    return options


def scheduled_at(item):
    """Epoch seconds a download is waiting for, or None (it isn't a scheduled one)."""
    if item.get("owner") != "hub" or item.get("state") != "paused":
        return None
    if not (item.get("options") or {}).get("start_at") or not item.get("next_attempt_at"):
        return None
    return float(item["next_attempt_at"])


def can_schedule(item):
    return item.get("owner") == "hub" and item.get("state") in ("queued", "paused", "stalled")


def normalize_item_options(limit_kbps=None, open_when_done=None, notify_me=None,
                           notify_channels=None, notify_events=None):
    """The per-download extras -> ({options}, error).  A value of None leaves that option unset;
    "default" (or 0 for the speed) clears it so the global setting applies again.

    `notify_channels` / `notify_events` are the per-download notification choices: WHERE this one
    is announced (any of the notifier's channels, e.g. just discord) and WHICH events fire for it
    (started / done / failed / stalled).  Left unset, the owner's global notification settings apply."""
    out = {}
    if limit_kbps not in (None, ""):
        try:
            kbps = int(float(limit_kbps))
        except (TypeError, ValueError):
            return {}, "speed limit must be a number of KB/s (0 for no limit of its own)"
        if kbps < 0 or kbps > 10_000_000:
            return {}, "speed limit must be between 0 and 10,000,000 KB/s"
        out["limit_kbps"] = kbps or None
    if open_when_done not in (None, ""):
        text = str(open_when_done).strip().lower()
        text = "off" if text in ("none", "nothing", "no") else text
        if text not in ITEM_OPEN_VALUES:
            return {}, "open_when_done must be one of: %s" % ", ".join(ITEM_OPEN_VALUES)
        out["open"] = None if text == "default" else text
    if notify_me not in (None, ""):
        text = str(notify_me).strip().lower()
        text = {"true": "on", "yes": "on", "always": "on", "false": "off", "no": "off", "never": "off"}.get(text, text)
        if text not in ITEM_NOTIFY_VALUES:
            return {}, "notify must be one of: %s" % ", ".join(ITEM_NOTIFY_VALUES)
        out["notify"] = None if text == "default" else text
    if notify_channels is not None:
        channels, problem = normalize_channels(notify_channels)
        if problem:
            return {}, problem
        out["notify_channels"] = channels
    if notify_events is not None:
        events, problem = normalize_events(notify_events)
        if problem:
            return {}, problem
        out["notify_events"] = events
    return out, None


# --------------------------------------------------------------------------- engine item options
#
# An engine can read choices of its own from its row's `options` (the HTTP engine reads `segments`).
# `item_options` on add_items is the ONE way an add tool stores such a choice (N4, brief 31).  It is
# deliberately narrow: an allow-list, one entry per option, naming the engines that honour it and
# the checker that decides what a value may be.  A key not listed is an error, never stored.
#
# Not here on purpose: `headers` (an Authorization value would sit in the database in clear text; the
# owner said no, brief 7C: only a `referer` is allowed, below) and the Stremio engine's `show` /
# `season` / `episode` / `fallbacks` (H23, which the bulk tools write themselves).  Each is an owner
# decision (brief 31, "Open points").  `filename` and `referer` came in with brief 7C (owner: yes).

def _check_segments(value):
    """Parallel parts for ONE download: a whole number from 1 (off for this one) to the engine's maximum."""
    try:
        from .engines import http as http_engine
        top = int(http_engine.SEGMENT_MAX)
    except Exception:  # noqa: BLE001 - the validator must keep working without the engine module
        top = 16
    if isinstance(value, float) and value != int(value):
        return None, "segments must be a whole number"       # 2.5 is not quietly cut to 2
    return settings_mod._int_in(value, 1, top, "segments")


FILENAME_MAX_INPUT = 200      # the longest `filename` a caller may send at all
REFERER_MAX = 500


def _check_filename(value):
    """The name ONE download is saved as (brief 7C).  A name the hub would have to change is REFUSED in
    words, never quietly fixed: the engine's own `naming.sanitize_filename` would turn a path into its last
    part, `a:b` into `a_b` and cut a long name, and the owner would get a file they didn't ask for.
    The final name still goes through `naming.unique_path` and the engine's name reservation."""
    import re
    import unicodedata
    if not isinstance(value, str):
        return None, "filename must be text, like report.pdf"
    text = unicodedata.normalize("NFC", value)          # the same letters in another encoding: not a change
    if not text.strip():
        return None, "filename is empty"
    if len(text) > FILENAME_MAX_INPUT:
        return None, "filename can be at most %d characters" % FILENAME_MAX_INPUT
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None, "filename can't hold a NUL or another control character"
    if "/" in text or "\\" in text:
        return None, "filename is a file name, not a path: it can't hold / or \\ (use dest for a folder)"
    if ".." in text:
        return None, "filename can't hold '..'"
    if re.match(r"^[A-Za-z]:", text):
        return None, "filename can't start with a drive letter like C:"
    if text != text.strip(" ."):
        return None, "filename can't start or end with a space or a dot"
    if text.split(".")[0].lower() in naming._WIN_RESERVED:
        return None, "filename %r is a reserved Windows device name; pick another" % text[:40]
    if len(text) > naming.MAX_NAME:
        return None, ("a saved file name is at most %d characters here (this one has %d); shorten it"
                      % (naming.MAX_NAME, len(text)))
    if naming.sanitize_filename(text) != text:
        return None, "filename holds characters a file name can't carry (one of < > : \" | ? *); take them out"
    return text, None


def _check_referer(value):
    """The Referer sent on the FIRST request (brief 7C): an http(s) link up to REFERER_MAX characters.
    No user name or password in it, and no spaces or control characters (a header must stay one line)."""
    if not isinstance(value, str):
        return None, "referer must be an http or https link"
    text = value.strip()
    if not text:
        return None, "referer is empty"
    if len(text) > REFERER_MAX:
        return None, "referer can be at most %d characters" % REFERER_MAX
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in text):
        return None, "referer can't hold spaces or control characters"
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None, "referer isn't a usable link"
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None, "referer must be an http or https link like https://example.com/page"
    if parts.username is not None or parts.password is not None:
        return None, "referer can't hold a user name or password (it is stored in clear text)"
    return text, None


VERIFY_ALGOS = {"sha1": 40, "sha256": 64, "sha512": 128}     # algorithm -> hex characters of its digest
VERIFY_SIZE_MAX = 1 << 50


def _check_verify(value):
    """The checksum ONE download must match (P1): {algo: sha1 | sha256 | sha512, value: <hex>, size: <bytes, optional>}.

    The shape, the algorithm, the length and the characters of the hash are all checked here; what is stored is
    the clean form (algorithm lower case, hash lower-case hex, size a whole number) and nothing else.  The HTTP
    engine hashes the finished `.part` against it and fails the row, deleting that one file, on a mismatch."""
    import re
    if not isinstance(value, dict):
        return None, "verify must be an object like {\"algo\": \"sha256\", \"value\": \"<hex>\", \"size\": 12345}"
    extra = sorted(str(k)[:20] for k in value if k not in ("algo", "value", "size"))
    if extra:
        return None, "verify can only hold algo, value and size (not %s)" % ", ".join(extra[:3])
    algo = value.get("algo")
    if not isinstance(algo, str):
        return None, "verify.algo must be text: sha1, sha256 or sha512"
    key = algo.strip().lower().replace("-", "")
    if key not in VERIFY_ALGOS:
        return None, "verify.algo %r isn't supported; use sha1, sha256 or sha512" % algo[:20]
    digest = value.get("value")
    if not isinstance(digest, str):
        return None, "verify.value must be the hash as hex text"
    digest = digest.strip().lower()
    if len(digest) != VERIFY_ALGOS[key]:
        return None, "a %s hash is %d hex characters; this one has %d" % (key, VERIFY_ALGOS[key], len(digest))
    if not re.fullmatch(r"[0-9a-f]+", digest):
        return None, "verify.value can only hold the characters 0-9 and a-f"
    out = {"algo": key, "value": digest}
    size = value.get("size")
    if size is not None:
        if isinstance(size, float) and size != int(size):
            return None, "verify.size must be a whole number of bytes"       # 2.5 is not quietly cut to 2
        num, problem = settings_mod._int_in(size, 0, VERIFY_SIZE_MAX, "verify.size")
        if problem:
            return None, problem
        out["size"] = num
    return out, None


ITEM_OPTION_CHECKS = {
    # option -> (the engines that honour it, checker(value) -> (clean, error))
    "segments": (("http",), _check_segments),
    "filename": (("http",), _check_filename),
    "referer": (("http",), _check_referer),
    "verify": (("http",), _check_verify),
}


def normalize_engine_item_options(raw):
    """The `item_options` an add tool sends -> ({option: clean value}, error).

    None / {} is fine (nothing).  Anything but an object, an option this hub doesn't know and a value
    its checker refuses are errors, all in plain words.  A value of None leaves that option unset."""
    if raw in (None, {}, ""):
        return {}, None
    if not isinstance(raw, dict):
        return {}, "item_options must be an object like {\"segments\": 4}"
    out = {}
    for key, value in raw.items():
        name = str(key)
        if name not in ITEM_OPTION_CHECKS:
            return {}, "unknown item option %r; the options a download can carry here: %s" % (
                name[:40], ", ".join(sorted(ITEM_OPTION_CHECKS)))
        if value is None:
            continue
        clean, problem = ITEM_OPTION_CHECKS[name][1](value)
        if problem:
            return {}, problem
        out[name] = clean
    return out, None


def split_dest(dest, root, allow_absolute=False):
    """A folder the owner typed for one download -> ({"subdir": ...} or {"dest_abs": ...}, error).

    A sub-folder of the library root always works.  A full path (outside the library) is only taken
    when `allow_absolute` is true -- the caller sets that once the owner has said yes."""
    if dest in (None, ""):
        return {}, None
    if settings_mod.is_absolute_spec(dest):
        if not allow_absolute:
            return {}, "needs_absolute"
        full = settings_mod.abs_folder(dest)
        if full is None:
            return {}, "that isn't a usable full path (no drive roots, no '..')"
        return {"dest_abs": full}, None
    if naming.safe_subdir(root, dest) is None:
        return {}, ("dest must be a sub-folder of the library root (%s) or a full path, not '..' "
                    "or something too deeply nested" % root)
    return {"subdir": str(dest).strip()}, None


def add_items(inputs, *, engine=None, dest=None, group=None, start_paused=False,
              speed_limit_kbps=None, open_when_done=None, notify_me=None,
              notify_channels=None, notify_events=None, allow_absolute_dest=False, start_at=None,
              engine_options=None, item_options=None, store=None):
    """Queue one or more downloads.  Returns {ok, added, skipped, group, note}.

    `start_at` ("tomorrow 3am", "in 2 hours") holds them until then (see "timed starts" above);
    it wins over `start_paused`.

    `engine_options`: yt-dlp's own choices (mode, quality, container, audio_format ...).  They
    are checked once, here, and only stored on rows the yt-dlp engine takes.

    `item_options`: other per-download choices an engine reads from its row (today `segments`, the
    HTTP engine's parallel parts).  Checked once, here (`normalize_engine_item_options`), stored only on
    rows an engine that honours them takes; the other rows ignore them and the answer's `note` says so.
    A link that is already queued (`duplicate`) keeps the options it has."""
    try:
        store = _store(store)
        if isinstance(inputs, str):
            inputs = [inputs]
        wanted = []
        for raw in inputs or []:
            text = str(raw).strip()
            if text and text not in wanted:
                wanted.append(text)
        if not wanted:
            return _err("give me a link to download")
        settings = store.get_settings()
        cap = int(settings["limits"]["max_items_per_call"])
        if len(wanted) > cap:
            return _err("that is %d links; the limit is %d per call" % (len(wanted), cap))

        root = settings_mod.library_root(settings)
        where, problem = split_dest(dest, root, allow_absolute=allow_absolute_dest)
        if problem == "needs_absolute":
            return _err("a full path writes outside the library root (%s), so it has to be approved first; "
                        "until then dest must be a sub-folder of the library" % root)
        if problem:
            return _err(problem)

        extras, problem = normalize_item_options(speed_limit_kbps, open_when_done, notify_me,
                                                 notify_channels, notify_events)
        if problem:
            return _err(problem)
        extras = {k: v for k, v in extras.items() if v is not None}
        timed = None
        if str(start_at or "").strip() and str(start_at).strip().lower() not in START_NOW_WORDS:
            timed, problem = parse_start(start_at)
            if problem:
                return _err(problem)

        video_opts, problem = {}, None
        if engine_options:
            from .engines import ytdlp as ytdlp_engine
            video_opts, problem = ytdlp_engine.normalize_options(engine_options)
            if problem:
                return _err(problem)
        item_opts, problem = normalize_engine_item_options(item_options)
        if problem:
            return _err(problem)
        plan, skipped = [], []
        for text in wanted:
            decision = preview(text, engine=engine, store=store)
            if decision["ok"]:
                plan.append((text, decision))
            else:
                skipped.append({"input": states.clean_text(text, 120), "problem": decision["problem"]})
        added = []
        group_id = None
        if plan and (group or len(plan) > 1):
            group_id = store.ensure_group(group or "Batch of %d" % len(plan), kind="batch")
        ignored = False
        ignored_opts, kept_by_duplicate = set(), set()
        for text, decision in plan:
            options = dict(where)
            options.update(extras)            # extras never reach the dedupe key (options_hash)
            if timed:
                options["start_at"] = timed["iso"]
            if decision["engine"] == "ytdlp":
                options.update(video_opts)
            elif video_opts:
                ignored = True
            for opt_name, opt_value in item_opts.items():
                if decision["engine"] in ITEM_OPTION_CHECKS[opt_name][0]:
                    options[opt_name] = opt_value
                else:
                    ignored_opts.add(opt_name)
            if decision["engine"] == "ytdlp":
                try:
                    host = urlsplit(decision["source"]).hostname or "the web"
                except ValueError:
                    host = "the web"
                title = "Video from %s" % host
            elif decision["source"].lower().startswith(SOURCE_RE_PREFIXES):
                title = _initial_title(decision["source"])
            else:
                title = states.clean_text(text, 80)
            kind = decision["kind"]
            if decision["engine"] == "ytdlp" and options.get("mode") == "audio":
                kind = "audio"
            item, created = store.add_item(
                engine=decision["engine"], source_url=decision["source"], title=title,
                kind=naming.kind_for(title) if kind == "file" else kind,
                options=options, group_id=group_id, state="paused" if (start_paused or timed) else "queued",
                dedupe_key=router.dedupe_key(decision["engine"], router.canonical_url(decision["source"]), options))
            if not created:
                kept_by_duplicate.update(k for k in item_opts if k in options)
            if timed and created:
                store.update_item(item["id"], {"next_attempt_at": timed["epoch"]}, touch=False,
                                  event="scheduled to start at %s" % timed["iso"])
                item = store.get_item(item["id"]) or item
            added.append({"id": item["id"], "title": item["title"], "engine": item["engine"],
                          "reason": decision["reason"], "state": item["state"], "duplicate": not created,
                          "start_at": timed["iso"] if (timed and created) else None})
        out = {"ok": bool(added), "added": added, "skipped": skipped, "group": group_id,
               "count": len(added), "library_root": str(root)}
        if not added:
            out["error"] = skipped[0]["problem"] if skipped else "nothing was queued"
        notes = []
        if added and not daemon_view(store)["alive"]:
            notes.append("The download hub daemon isn't running, so nothing will start until it is "
                         "(Menu > Daemons > Download hub).")
        if ignored:
            notes.append("mode / quality / container / audio_format only apply to video pages (the yt-dlp "
                         "engine); they were ignored for the other links.")
        for opt_name in sorted(ignored_opts):
            notes.append("%s only applies to links the %s engine takes; it was ignored for the other links." % (
                opt_name, "/".join(ITEM_OPTION_CHECKS[opt_name][0])))
        for opt_name in sorted(kept_by_duplicate):
            notes.append("%s was not changed on a link that was already queued; it keeps its own options." % opt_name)
        if notes:
            out["note"] = " ".join(notes)
        return out
    except Exception as exc:  # noqa: BLE001 - tools never raise
        return _err("couldn't queue that: %s" % exc)


# --------------------------------------------------------------------------- reading

def _provider_map(store):
    return {p["id"]: p for p in store.list_providers()}


def effective_capabilities(item, provider=None, library_root=None):
    """What the panel may offer for a row (a button exists only if its capability does)."""
    state = item["state"]
    if item["owner"] == "hub":
        engine = engines.get(item["engine"])
        can_pause = engine is None or getattr(engine, "supports_pause", True)
        caps = []
        if state in states.ACTIVE_STATES or state == "queued":
            caps = (["pause"] if can_pause else []) + ["cancel"]
        elif state == "paused":
            caps = ["resume", "cancel"]
        elif state == "stalled":
            caps = ["retry"] + (["pause"] if can_pause else []) + ["cancel"]
        elif state in ("failed", "cancelled"):
            caps = ["retry"]
        elif state == "done" and item.get("dest_file"):
            caps = ["open", "reveal"]
        return caps
    declared = set((provider or {}).get("capabilities") or [])
    if item.get("capabilities") is not None:
        declared &= set(item["capabilities"])          # a row may narrow what its provider declared
    # A verb is only offered where it makes sense for the row's state, whatever the provider declared.
    sensible = set()
    if state in states.ACTIVE_STATES or state in ("queued", "stalled"):
        sensible |= {"pause"}
    if state == "paused":
        sensible |= {"resume"}
    if state not in states.FINAL_STATES:
        sensible |= {"cancel", "prioritize"}
    if state in ("failed", "cancelled", "stalled"):
        sensible |= {"retry"}
    if state in states.FINAL_STATES:
        sensible |= {"remove"}
    roots = (provider or {}).get("open_roots") or []
    dest = item.get("dest_file")
    if dest and roots and any(naming.under(r, dest) for r in roots):
        sensible |= {"open", "reveal", "play"}          # only for a file inside the provider's roots
    declared &= sensible
    return [c for c in states.CAPABILITIES if c in declared]


def present_item(item, providers, now):
    provider = providers.get(item["provider"]) if item["owner"] == "provider" else None
    last = item.get("last_report_at")
    return {
        "id": item["id"], "title": item["title"], "subtitle": item["subtitle"], "kind": item["kind"],
        "engine": item["engine"], "provider": item["provider"],
        "provider_label": item.get("provider_label") or (item["engine"] if item["owner"] == "hub" else item["provider"]),
        "provider_icon": item.get("provider_icon") or "", "owner": item["owner"],
        "state": item["state"], "phase": item["phase"], "phase_index": item["phase_index"],
        "phase_count": item["phase_count"], "percent": item["percent"], "bytes_done": item["bytes_done"],
        "bytes_total": item["bytes_total"], "speed_bps": item["speed_bps"], "eta_s": item["eta_s"],
        "derived": item["derived"], "peers": item["peers"], "seeds": item["seeds"],
        "group_id": item["group_id"], "group_title": item.get("group_title"),
        "dest": item.get("dest_file") or item.get("dest_dir"),
        "source": netguard.mask_url(item["source_url"]) if item.get("source_url") else "",
        "message": item["message"], "error": item["error"], "stale": item["stale"],
        "age_s": None if not last else max(0, int(now - last)),
        "attempts": item["attempts"], "created_at": item["created_at"], "finished_at": item["finished_at"],
        "retry_in_s": (max(0, int(item["next_attempt_at"] - now))
                       if item.get("next_attempt_at") and item["state"] != "paused" else None),
        "scheduled_at": scheduled_at(item), "can_schedule": can_schedule(item),
        "capabilities": effective_capabilities(item, provider),
        "read_only": bool(item["owner"] == "provider" and not (provider or {}).get("capabilities")),
        "links": item.get("links") or [], "pending": item.get("control") or "",
        # this download's own choices (hub rows only): speed cap KB/s, when-done action, notify
        "prefs": ({k: (item.get("options") or {}).get(k) for k in ("limit_kbps", "open", "notify", "notify_channels",
                                                             "notify_events", "subdir", "dest_abs", "segments")
                   if (item.get("options") or {}).get(k) is not None} if item["owner"] == "hub" else {}),
    }


_STATE_GROUPS = {"active": states.ACTIVE_STATES, "final": states.FINAL_STATES,
                 "waiting": ("queued", "stalled", "paused")}


def list_items(*, state=None, engine=None, provider=None, kind=None, group=None, search=None,
               limit=50, store=None, with_summary=False):
    try:
        store = _store(store)
        wanted = None
        if state:
            parts = [p.strip() for p in str(state).split(",") if p.strip()]
            wanted = []
            for p in parts:
                if p in _STATE_GROUPS:
                    wanted += list(_STATE_GROUPS[p])
                elif p in states.STATES:
                    wanted.append(p)
                else:
                    return _err("unknown state %r (use one of: %s, active, final, waiting)"
                                % (p[:20], ", ".join(states.STATES)))
        now = store.now()
        rows = store.list_items(states_in=wanted, engine=engine, provider=provider, kind=kind,
                                group_id=group, search=search, limit=limit)
        providers = _provider_map(store)
        items = [present_item(r, providers, now) for r in rows]
        out = {"ok": True, "items": items, "count": len(items)}
        if with_summary:
            out["summary"] = summary(store)
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the downloads: %s" % exc)


def summary(store, full=False):
    settings = store.get_settings()
    root = settings_mod.library_root(settings)
    kbps, source = settings_mod.effective_rate_limit_kbps(settings)
    out = {"counts": store.counts(), "daemon": daemon_view(store), "library_root": str(root),
           "free_bytes": _free_bytes(root),
           # the speed limit in force right now (the owner's, or a schedule window's); the panel
           # shows it without needing the whole settings object on every poll
           "speed_limit": {"kbps": kbps, "source": source},
           "after_download": settings["after_download"]["open"],
           "notifications_on": bool(settings["notify"]["enabled"]),
           # the global "downloads at once" ceiling and how much of it is in use (hub downloads only)
           "concurrency": {"total": int(settings["concurrency"]["total"]), "running": store.running_hub()},
           "notify_options": notify_options()}
    sched = store.list_scheduled()
    out["scheduled"] = {"count": len(sched), "next_at": sched[0][1] if sched else None}
    if full:
        out["engines"] = engines.availability()
        out["engine_load_errors"] = dict(engines.LOAD_ERRORS)
        out["settings"] = settings_view(settings)
    return out


def item_detail(item_id, store=None):
    """One download in full: the list row plus its detail pairs, sub-files and recent events
    (the panel's detail pane, and `dl_status(id=...)` for the model)."""
    try:
        store = _store(store)
        item = store.get_item(str(item_id or ""))
        if not item:
            return _err("no such download")
        row = present_item(item, _provider_map(store), store.now())
        row["detail"] = item.get("detail") or []
        row["children"] = item.get("children") or []
        row["events"] = store.list_events(item["id"], limit=30)
        row["control_pending"] = item.get("control") or ""
        return {"ok": True, "item": row}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read that download: %s" % exc)


def status(store=None, item_id=None):
    if item_id:
        return item_detail(item_id, store)
    try:
        store = _store(store)
        out = summary(store, full=True)
        out["ok"] = True
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the hub status: %s" % exc)


# --------------------------------------------------------------------------- providers

def list_providers(store=None):
    """Every provider with its health and row counts (`dl_providers`; the Providers tab later)."""
    try:
        store = _store(store)
        now = store.now()
        out = []
        for p in store.list_providers():
            rows = store.list_items(provider=p["id"], limit=2000, include_disabled=True)
            live = [r for r in rows if r["state"] not in states.FINAL_STATES and r["state"] != "unknown"]
            last = p.get("last_report_at")
            out.append({
                "id": p["id"], "label": p["label"], "mode": p["mode"], "kind": p["kind"], "icon": p["icon"],
                "enabled": p["enabled"], "interval_s": p["interval_s"], "capabilities": p["capabilities"],
                "controls": sorted(p["controls"]), "poll_tool": p.get("poll_tool"), "source": p.get("source_file") or "",
                "settings": settings_mod.owner_provider_settings(p["id"], p.get("settings")),
                "last_report_age_s": None if not last else max(0, int(now - last)),
                "last_ok_age_s": None if not p.get("last_ok_at") else max(0, int(now - p["last_ok_at"])),
                "last_error": p.get("last_error"), "rows": len(rows), "live": len(live),
                "stale": sum(1 for r in live if r["stale"]),
                "read_only": not p["capabilities"],
            })
        return {"ok": True, "providers": out, "count": len(out)}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the providers: %s" % exc)


def update_provider(provider_id, *, enabled=None, interval_s=None, settings=None, store=None):
    """The owner's choices for one provider: switch it on / off, how often it is polled, and its
    settings (e.g. {\"root\": \"D:/Steam\"} for Steam).  Switching off hides its rows and stops its
    polling; nothing the provider downloaded is touched."""
    try:
        store = _store(store)
        pid = str(provider_id or "").strip()
        provider = store.get_provider(pid)
        if provider is None:
            return _err("no provider called %r (see dl_providers)" % pid[:30])
        if enabled is None and interval_s is None and settings is None:
            return _err("say what to change: enabled, interval_s or settings")
        interval = None
        if interval_s is not None:
            interval = states.clean_number(interval_s, 0.5, 3600)
            if interval is None:
                return _err("interval_s must be a number of seconds from 0.5 to 3600")
        if settings is not None:
            if not isinstance(settings, dict) or any(not isinstance(k, str) for k in settings):
                return _err("settings must be an object like {\"root\": \"D:/Steam\"}")
            import json
            if len(json.dumps(settings, default=str)) > 2048:
                return _err("those settings are too large")
            settings, problems = settings_mod.validate_provider_settings(pid, settings)
            if problems:
                return _err("; ".join(problems))
        if enabled is not None:
            store.set_provider_enabled(pid, bool(enabled))
        if interval is not None or settings is not None:
            store.update_provider(pid, interval_s=interval, settings=settings)
        return {"ok": True, "provider": pid, "providers": list_providers(store)["providers"]}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't change that provider: %s" % exc)


# --------------------------------------------------------------------------- settings

def library_info(settings):
    """The download folder as the panel shows it: path, whether it exists / is writable, free space."""
    root = settings_mod.library_root(settings)
    exists = root.is_dir()
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return {"path": str(root), "exists": exists, "writable": os.access(str(probe), os.W_OK),
            "free_bytes": _free_bytes(probe), "folders": {k: naming.kind_folder(k, settings.get("folders"))
                                                          for k in settings_mod.FOLDER_KINDS}}


def notify_options():
    """The notification catalog (every notification type, level, kind, look and tone, and a
    `version` that changes when the table does), read live from jarvis/notifier.py, plus the
    download events the hub itself can announce.  The panel and the tools offer exactly this, so
    a level, type or position added to the notification module shows up here with no change to
    the hub, and the panel redraws its pickers when `version` changes.

    If the notifier cannot be read this returns EMPTY lists: no copy of the levels or channels
    is kept here, so the panel disables its pickers instead of offering stale ones."""
    try:
        from .. import notifier
        out = notifier.describe_options()
    except Exception:  # noqa: BLE001 - never break the panel over a notifier problem
        out = {"levels": [], "channels": [], "actions": [], "positions": [], "min_level": None,
               "max_level": None, "default_level": None, "default_position": None, "version": ""}
    # The panel polls this with every summary; it reads the levels, the types and `version`, not
    # the preset / kind / look tables, so those stay out of the payload.
    for heavy in ("presets", "kinds", "looks", "tones", "default_look", "default_tone"):
        out.pop(heavy, None)
    out["events"] = list(settings_mod.EVENTS)
    out["item_events"] = list(ITEM_NOTIFY_EVENTS)
    return out


def settings_view(settings):
    kbps, source = settings_mod.effective_rate_limit_kbps(settings)
    return {"library_root": str(settings_mod.library_root(settings)),
            "concurrency": settings["concurrency"], "rate_limit_kbps": settings["rate_limit_kbps"],
            "allow_private_hosts": settings["allow_private_hosts"], "http": settings["http"],
            "limits": settings["limits"],
            "schedule": settings.get("schedule") or [],
            "rate_limit_now": {"kbps": kbps, "source": source},
            "folders": settings["folders"], "ext_folders": settings.get("ext_folders") or {},
            "after_download": settings["after_download"],
            # only the known behaviour keys: the qBittorrent connection and its password are not settings
            "qbittorrent": settings_mod.qbittorrent_view(settings),
            "notify": settings["notify"], "notify_options": notify_options(),
            "library": library_info(settings)}


def get_settings(store=None):
    try:
        store = _store(store)
        return {"ok": True, "settings": settings_view(store.get_settings())}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the settings: %s" % exc)


def update_settings(updates, store=None):
    try:
        store = _store(store)
        clean, errors = settings_mod.validate_update(updates)
        if errors:
            return _err("; ".join(errors), errors=errors)
        changed = store.set_settings(clean)
        return {"ok": True, "changed": changed, "settings": settings_view(store.get_settings())}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't change the settings: %s" % exc)


def update_prefs(updates, *, id=None, allow_absolute_dest=False, store=None):
    """The everyday preferences, without the "are you sure" that `dl_settings` carries.

    No `id`: global preferences (speed limit, schedule, folders, extension folders, notifications,
    open-when-done, connections).  The two keys that decide where files are written and which
    private hosts the hub may fetch from are refused here; they need `dl_settings`.
    With `id`: that one download's own speed limit / open-when-done / notify choice, where its
    notifications go (`notify_channels`) and which ones fire (`notify_events`), and the folder it
    saves to (`folder`, only until it has started writing).  `allow_absolute_dest` is set by the
    caller once the owner approved a full path.
    """
    try:
        store = _store(store)
        if id:
            item = store.get_item(str(id))
            if not item:
                return _err("no such download")
            if item["owner"] != "hub":
                return _err("that row belongs to %s; its own tool decides how it runs" % (item["provider"] or "another tool"))
            if item["state"] in states.FINAL_STATES:
                return _err("that download has already finished")
            mapping = {"limit_kbps": "limit_kbps", "speed_limit_kbps": "limit_kbps",
                       "open": "open", "open_when_done": "open", "notify": "notify",
                       "notify_channels": "notify_channels", "channels": "notify_channels",
                       "notify_events": "notify_events", "events": "notify_events",
                       "folder": "folder", "dest": "folder"}
            unknown = [k for k in (updates or {}) if k not in mapping]
            if unknown or not updates:
                return _err("for one download you can set limit_kbps, open, notify, notify_channels, "
                            "notify_events and folder")
            raw = {mapping[k]: v for k, v in updates.items()}
            extras, problem = normalize_item_options(raw.get("limit_kbps"), raw.get("open"), raw.get("notify"),
                                                     raw.get("notify_channels"), raw.get("notify_events"))
            if problem:
                return _err(problem)
            options = dict(item.get("options") or {})
            clear_keys, where = set(), {}
            if "folder" in raw:
                if item.get("dest_file") or item.get("dest_dir"):
                    return _err("that download has already started writing its file, so its folder can't "
                                "change any more (cancel it and add it again to put it somewhere else)")
                where, problem = split_dest(raw["folder"], settings_mod.library_root(store.get_settings()),
                                            allow_absolute=allow_absolute_dest)
                if problem == "needs_absolute":
                    return _err("a full path writes outside the library root, so it has to be approved first; "
                                "until then the folder must be a sub-folder of the library")
                if problem:
                    return _err(problem)
                clear_keys |= {"subdir", "dest_abs"}      # a new choice (or "back to default") replaces the old
                for key, value in where.items():
                    options[key] = value
            for key in clear_keys:
                if key not in where:
                    options.pop(key, None)
            for key, value in extras.items():
                if value is None:
                    options.pop(key, None)
                else:
                    options[key] = value
            fresh = store.update_item(item["id"], {"options": options}, touch=False)
            return {"ok": True, "id": item["id"], "options": (fresh or item).get("options") or options}
        clean, errors = settings_mod.validate_update(updates, allowed=settings_mod.PREFS_WRITABLE)
        if errors:
            return _err("; ".join(errors), errors=errors)
        changed = store.set_settings(clean)
        return {"ok": True, "changed": changed, "settings": settings_view(store.get_settings())}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't change the preferences: %s" % exc)


def send_test_notification(event="done", *, store=None, deliver=None):
    """A sample of one notification type through the real notifier (the panel's "Send test")."""
    try:
        store = _store(store)
        if event not in notify.EVENT_LABELS:
            return _err("event must be one of: %s" % ", ".join(notify.EVENT_LABELS))
        record = notify.Dispatcher(store, deliver=deliver).send_test(event, store.get_settings())
        delivered = list(record.get("delivered") or [])
        return {"ok": True, "event": event, "delivered": delivered,
                "failed_channels": list(record.get("failed_channels") or []),
                "note": ("Sent through the notifier: %s." % (", ".join(delivered) or "inbox only"))}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't send the test: %s" % exc)


# --------------------------------------------------------------------------- controls

def select(store, *, id=None, group=None, engine=None, provider=None, all=False):
    """The rows a control call applies to; at least one selector is required."""
    if id:
        item = store.get_item(str(id))
        return [item] if item else []
    if not (group or engine or provider or all):
        return None
    return store.list_items(group_id=group, engine=engine, provider=provider, limit=2000, include_disabled=True)


def _discard_part(store, item):
    """Delete the leftovers of a cancelled hub download: the `.part` file (HTTP) or the private
    work folder (yt-dlp) -- only those, only inside the library root.  A finished file is never
    touched."""
    ref = item.get("source_ref") or {}
    roots = settings_mod.write_roots(store.get_settings(), item.get("options"))
    work = ref.get("work_dir")
    if work:
        try:
            from .engines import ytdlp as ytdlp_engine
            ytdlp_engine.remove_work_dir(work, roots)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass
    part = ref.get("part")
    if not part:
        return
    if naming.under_any(roots, part) and str(part).endswith(".part"):
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass


def control_provider_row(item, verb, provider, executor=None, confirm=None):
    """A provider row's pause / resume / cancel / retry: run the provider's own control tool for
    that verb.  Returns the same ("pending" | "skipped", detail) pair `_apply` uses.

    No capability, no action, and nothing runs.  The refusal says why: "read-only" only for a
    provider that declares no control verb at all (Steam, Playnite); a provider that has verbs but
    not this one for the row's state (Pause on a paused row) says "can't <verb> a download that is
    <state>".  The answer is "pending" (not "changed") because the row only changes when the
    provider next reports; the hub never edits a provider's row itself."""
    from . import providers as providers_mod
    caps = effective_capabilities(item, provider)
    label = (provider or {}).get("label") or item["provider"]
    if verb not in caps:
        declared = set((provider or {}).get("capabilities") or [])
        if not declared & set(CONTROL_VERBS):
            return "skipped", "read-only: %s doesn't offer %s" % (label, verb)
        return "skipped", "can't %s a download that is %s" % (verb, item["state"])
    ok, detail = providers_mod.run_control(item, verb, provider, executor=executor, confirm=confirm)
    return ("pending", detail) if ok else ("skipped", detail)


# What `_apply` can do to a row.  A provider that declares none of them is "read-only" (it may still
# declare `reveal`, as Steam does).
CONTROL_VERBS = ("pause", "resume", "cancel", "retry")


def _apply(store, verb, item, daemon_alive, providers, bulk=False):
    """One item, one verb.  Returns ("changed" | "pending" | "skipped", detail)."""
    state = item["state"]
    if item["owner"] != "hub":
        return control_provider_row(item, verb, providers.get(item["provider"]))
    engine = engines.get(item["engine"])
    held = state in states.ACTIVE_STATES and daemon_alive

    if verb == "pause":
        if engine is not None and not getattr(engine, "supports_pause", True):
            return "skipped", "the %s engine can't pause" % item["engine"]
        if state in states.FINAL_STATES:
            return "skipped", "already %s" % state
        if state == "paused":
            return "skipped", "already paused"
        if held:
            store.set_control(item["id"], "pause")
            return "pending", "pausing"
        ok = store.transition(item["id"], "paused", expect=state, message="paused")
        return ("changed", "paused") if ok else ("skipped", "its state just changed")

    if verb == "resume":
        if state != "paused":
            return "skipped", "not paused"
        was_timed = scheduled_at(item) is not None
        if was_timed and bulk:
            # "Resume all" must not undo the owner's start times; resume that one by itself to start it now.
            return "skipped", "waiting for its start time (resume it on its own to start it now)"
        ok = store.transition(item["id"], "queued", expect="paused", next_attempt_at=None, message=None,
                              options=_options_without_schedule(item),
                              event="started now, ahead of its start time" if was_timed else None)
        return ("changed", "queued") if ok else ("skipped", "its state just changed")

    if verb == "cancel":
        if state in states.FINAL_STATES:
            return "skipped", "already %s" % state
        if held:
            store.set_control(item["id"], "cancel")
            return "pending", "cancelling"
        ok = store.transition(item["id"], "cancelled", expect=state, message="cancelled")
        if ok:
            _discard_part(store, item)
            return "changed", "cancelled"
        return "skipped", "its state just changed"

    if verb == "retry":
        if state not in ("failed", "cancelled", "stalled"):
            return "skipped", "nothing to retry (state: %s)" % state
        ok = store.transition(item["id"], "queued", expect=state, error=None, message=None,
                              attempts=0, next_attempt_at=None, finished_at=None,
                              options=_options_without_schedule(item))
        return ("changed", "queued again") if ok else ("skipped", "its state just changed")
    return "skipped", "unknown action"


def control(verb, *, id=None, group=None, engine=None, provider=None, all=False, store=None):
    """pause / resume / cancel / retry on one item, a group, an engine, a provider or all."""
    try:
        store = _store(store)
        items = select(store, id=id, group=group, engine=engine, provider=provider, all=all)
        if items is None:
            return _err("say which download: an id, a group, an engine, a provider, or all=true")
        if not items:
            return _err("no such download")
        alive = daemon_view(store)["alive"]
        providers = _provider_map(store)
        out = {"ok": True, "changed": [], "pending": [], "skipped": []}
        for item in items:
            result, detail = _apply(store, verb, item, alive, providers, bulk=not id)
            if result == "changed":
                out["changed"].append(item["id"])
            elif result == "pending":
                out["pending"].append(item["id"])
            else:
                out["skipped"].append({"id": item["id"], "reason": detail})
        if id and not (out["changed"] or out["pending"]):
            detail = {k: v for k, v in out.items() if k != "ok"}     # `ok` must stay False here
            return _err(out["skipped"][0]["reason"] if out["skipped"] else "nothing changed", **detail)
        # A "skipped" row is the normal answer for a bulk call (everything already paused);
        # only a single named download reports it as an error.
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("%s failed: %s" % (verb, exc))


def _schedule_one(store, item, timed):
    """Give one download a start time (`timed` = parse_start's result) or, with None, take its
    schedule away.  Returns ("changed" | "skipped", detail)."""
    if item["owner"] != "hub":
        return "skipped", "that row belongs to %s; only its own tool can start it later" % (item["provider"] or "another tool")
    state = item["state"]
    if state in states.ACTIVE_STATES:
        return "skipped", "it is already downloading; pause it first, then give it a start time"
    if state in states.FINAL_STATES:
        return "skipped", "already %s" % state
    if timed is None:
        if scheduled_at(item) is None:
            return "skipped", "it has no start time"
        ok = store.update_item(item["id"], {"next_attempt_at": None, "options": _options_without_schedule(item)},
                               expect="paused", touch=False, event="start time cleared; still paused")
        return ("changed", "start time cleared (it stays paused until you resume it)") if ok else ("skipped", "its state just changed")
    options = _options_without_schedule(item)
    options["start_at"] = timed["iso"]
    event = "scheduled to start at %s" % timed["iso"]
    if state == "paused":
        ok = store.update_item(item["id"], {"next_attempt_at": timed["epoch"], "options": options},
                               expect="paused", touch=False, event=event)
    else:
        ok = store.transition(item["id"], "paused", expect=state, next_attempt_at=timed["epoch"],
                              options=options, message=None, event=event)
    return ("changed", "starts at %s" % timed["iso"]) if ok else ("skipped", "its state just changed")


def dismiss(*, id=None, finished=False, store=None):
    """Hide rows from the lists (`dl_dismiss`).  Nothing is deleted and no file is touched.

    `id`: one row.  `finished`: every row that can be dismissed (a provider row that is unknown or
    finished, a hub download that was cancelled).  A hub download is hidden once it failed or was
    cancelled; a running one is cancelled with dl_cancel and a finished one is removed from History.  A dismissed row stays hidden while
    its provider keeps reporting it finished or unknown, and comes back when the provider reports it
    live again."""
    try:
        store = _store(store)
        row_id = str(id or "").strip()
        if row_id and finished:
            return _err("give an id or finished: true, not both")
        if not row_id and not finished:
            return _err("say which row: an id, or finished: true to clear everything that can be dismissed")
        note = "hidden from the lists; no file was touched"
        if finished:
            rows = store.list_items(owner="provider", states_in=states.DISMISSABLE_STATES, limit=2000)
            rows += store.list_items(owner="hub", states_in=("cancelled",), limit=2000)     # ones the owner abandoned
            done = store.dismiss_items([r["id"] for r in rows])
            return {"ok": True, "dismissed": done, "count": len(done),
                    "note": note if done else "nothing to clear"}
        item = store.get_item(row_id)
        if not item:
            return _err("no such download")
        if item["owner"] == "hub" and not states.is_dismissable("hub", item["state"]):
            return _err("a hub download is hidden once it has failed or been cancelled: cancel it with dl_cancel first "
                        "(a finished one is removed from History in the Downloads panel)")
        if item["state"] not in states.DISMISSABLE_STATES:
            why = "it is still running" if item["state"] in states.ACTIVE_STATES else "it hasn't finished"
            return _err("%s (%s); only a row that is unknown or finished can be dismissed" % (why, item["state"]))
        if item.get("dismissed_at") is not None:
            return {"ok": True, "dismissed": [], "count": 0, "note": "it was already hidden"}
        done = store.dismiss_items([item["id"]])
        if not done:
            return _err("its state just changed; look at it again")
        return {"ok": True, "dismissed": done, "count": len(done), "note": note}
    except Exception as exc:  # noqa: BLE001
        return _err("dismiss failed: %s" % exc)


def schedule_items(when, *, id=None, group=None, engine=None, provider=None, all=False, store=None):
    """Start downloads later (or take a start time away).

    `when` is the owner's own wording ("tomorrow 3am", "in 2 hours", "2026-11-02 01:30").  "now"
    starts them at once; an empty value or "clear" removes the start time (they stay paused).
    Works on one download, a group, an engine or all of them, like pause and resume."""
    try:
        store = _store(store)
        items = select(store, id=id, group=group, engine=engine, provider=provider, all=all)
        if items is None:
            return _err("say which download: an id, a group, an engine, or all=true")
        if not items:
            return _err("no such download")
        text = str(when or "").strip()
        if text.lower() in START_NOW_WORDS:
            return control("resume", id=id, group=group, engine=engine, provider=provider, all=all, store=store)
        timed = None
        if text.lower() not in CLEAR_SCHEDULE_WORDS:
            timed, problem = parse_start(text)
            if problem:
                return _err(problem)
        out = {"changed": [], "skipped": [], "start_at": timed["iso"] if timed else None}
        for item in items:
            result, detail = _schedule_one(store, item, timed)
            if result == "changed":
                out["changed"].append(item["id"])
            else:
                out["skipped"].append({"id": item["id"], "reason": detail})
        if not out["changed"]:
            return _err(out["skipped"][0]["reason"] if out["skipped"] else "nothing changed", **out)
        out["ok"] = True
        if timed and not daemon_view(store)["alive"]:
            out["note"] = ("The download hub daemon isn't running, so nothing starts at that time until it is "
                           "(Menu > Daemons > Download hub); anything overdue starts as soon as it comes up.")
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("scheduling failed: %s" % exc)


def wake_scheduled(store, now=None):
    """The daemon, once a tick: move every download whose start time has come to `queued`.
    Returns the ids started.  A row the owner resumed or cancelled meanwhile is left alone."""
    started = []
    for item_id in store.due_scheduled(now):
        item = store.get_item(item_id)
        if item is None or scheduled_at(item) is None:
            continue
        if store.transition(item_id, "queued", expect="paused", next_attempt_at=None, message=None,
                            options=_options_without_schedule(item), event="start time reached"):
            started.append(item_id)
    return started


def apply_control_directly(store, item):
    """The daemon, for a control request nobody holds (the worker ended meanwhile)."""
    verb = item.get("control")
    state = item["state"]
    if verb == "pause" and state not in states.FINAL_STATES:
        store.transition(item["id"], "paused", expect=state, message="paused")
    elif verb == "cancel" and state not in states.FINAL_STATES:
        if store.transition(item["id"], "cancelled", expect=state, message="cancelled"):
            _discard_part(store, item)
    store.set_control(item["id"], "")


# --------------------------------------------------------------------------- opening files

def _default_launch(path, action):
    """Open a file with its default program, or show it in the file manager.  Never raises.

    Returns True when the launch started; False when it could not start; or a short text when it
    started but did something LESS than asked (Windows: the file could not be selected, so its
    folder was opened).  `open_item` passes that text on to the caller.  Explorer cannot say
    whether it found the item, so True means "started", not "the right window is showing".
    """
    try:
        path = str(path)
        if sys.platform.startswith("win"):
            if action == "reveal":
                from . import reveal       # the pure command builder (hub/reveal.py)
                try:
                    subprocess.Popen(reveal.explorer_command(path))    # one string: the path is quoted inside the switch
                    return True
                except Exception:  # noqa: BLE001 - fall back, and say so
                    os.startfile(reveal.windows_folder(path))  # noqa: S606
                    return "couldn't select the file; its folder was opened instead"
            os.startfile(path)  # noqa: S606 - the path was checked against the library root
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if action == "reveal" else ["open", path])
        else:
            target = os.path.dirname(path) if action == "reveal" else path
            subprocess.Popen(["xdg-open", target])
        return True
    except Exception:  # noqa: BLE001
        return False


LAUNCH = _default_launch       # swapped by tests; (path, action) -> True | False | a note (see above)


def open_item(item_id, action="open", store=None):
    """Open a finished file or reveal it in the file manager.

    The path is read from the database, never from the caller, and must sit inside the
    library root or a folder the owner chose (hub rows) or one of the provider's `open_roots`.
    A row's `dest` may be a folder (Steam's is, a multi-file torrent's can be): `reveal` then
    opens that folder itself and `folder` does not take its parent (see hub/reveal.py).
    """
    try:
        from . import reveal               # the pure "what to open" rules (hub/reveal.py)
        store = _store(store)
        if action not in ("open", "reveal", "folder"):
            return _err("action must be open, reveal or folder")
        item = store.get_item(str(item_id or ""))
        if not item:
            return _err("no such download")
        path = item.get("dest_file")
        if not path:
            return _err("that download has no file yet")
        if item["owner"] == "hub":
            roots = settings_mod.write_roots(store.get_settings(), item.get("options"))
        else:
            roots = [Path(r) for r in ((store.get_provider(item["provider"]) or {}).get("open_roots") or [])]
        if not roots or not any(naming.under(r, path) for r in roots):
            return _err("that file isn't inside a folder the hub may open")
        exists = os.path.exists(path)
        # The row's marker says what dest was when it was reported; the disk says what it is now.
        marker = item.get("dest_is_dir")            # 1 folder, 0 file, None not known
        is_dir = os.path.isdir(path) if exists else bool(marker)
        if not exists:
            return _err(("the file or folder is no longer there (or isn't there yet): %s" if marker is None
                         else "the folder isn't there yet, or is no longer there: %s" if marker
                         else "the file is no longer there: %s") % path)
        target, launch_action = reveal.plan_open(path, action, is_dir)
        result = LAUNCH(target, launch_action)
        if not result:
            return _err("couldn't open it on this system")
        out = {"ok": True, "action": action, "path": str(target)}
        if isinstance(result, str):        # the launcher did less than asked, and says what
            out["note"] = result
            if launch_action == "reveal":
                out["selected"] = False
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't open that: %s" % exc)


def open_library(store=None):
    """Open the download folder itself.  The path is the library root from the settings."""
    try:
        store = _store(store)
        root = settings_mod.library_root(store.get_settings())
        root.mkdir(parents=True, exist_ok=True)
        if not LAUNCH(root, "open"):
            return _err("couldn't open the folder on this system")
        return {"ok": True, "action": "folder", "path": str(root)}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't open the download folder: %s" % exc)


# Never opened by "open when done": opening these RUNS them (or runs a script inside them).
# They are shown in their folder instead.  A manual Open click is the owner's own decision and
# is not restricted here.
RISKY_EXTENSIONS = frozenset((
    ".exe", ".msi", ".msp", ".bat", ".cmd", ".com", ".scr", ".pif", ".cpl", ".msc", ".dll", ".sys",
    ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".jar", ".lnk", ".url",
    ".reg", ".inf", ".scf", ".gadget", ".application", ".appx", ".msix", ".appxbundle",
    ".sh", ".command", ".app", ".apk", ".appimage", ".py", ".pyw", ".pl", ".rb",
    ".docm", ".xlsm", ".pptm", ".xlam", ".dotm", ".html", ".htm", ".xhtml", ".mht", ".svg",
))
AUTO_OPEN_MAX = 3            # at most this many automatic opens ...
AUTO_OPEN_WINDOW_S = 60      # ... per this many seconds, so a 30-file batch can't open 30 windows


def is_risky_to_open(path):
    return os.path.splitext(str(path))[1].lower() in RISKY_EXTENSIONS


def run_auto_open(store, settings, since, recent, now=None):
    """Do the "when it finishes" action for downloads that just finished.  Once per finish.

    `recent`: a deque of the times of recent automatic opens (the daemon keeps one).  Hub rows
    only; the file must be inside the library root (open_item enforces that again).  Returns the
    list of (item id, what happened) for the tests and the daemon's log.
    """
    now = time.time() if now is None else now
    out = []
    default_mode = (settings.get("after_download") or {}).get("open", "off")
    for item in store.unclaimed("open", "done", since=since, stamp_column="finished_at"):
        if item["owner"] != "hub":
            continue
        if not store.claim_notice(item["id"], "open", item.get("finished_at") or 0):
            continue
        mode = (item.get("options") or {}).get("open")
        mode = mode if mode in settings_mod.OPEN_MODES else default_mode
        if mode == "off" or not item.get("dest_file"):
            continue
        while recent and now - recent[0] > AUTO_OPEN_WINDOW_S:
            recent.popleft()
        if len(recent) >= AUTO_OPEN_MAX:
            store.add_event(item["id"], "info", "not opened automatically: too many downloads finished at once")
            out.append((item["id"], "skipped"))
            continue
        note = {"open": "opened automatically", "reveal": "shown in its folder automatically",
                "folder": "its folder was opened automatically"}[mode]
        if mode == "open" and is_risky_to_open(item["dest_file"]):
            mode, note = "reveal", "shown in its folder instead of opened: programs and scripts are never opened automatically"
        result = open_item(item["id"], mode, store=store)
        recent.append(now)
        if result.get("ok"):
            # open_item answers ok with a `note` when it did less than asked (its folder was opened
            # because the file could not be selected): log that, not "shown in its folder".
            if result.get("note"):
                note = "%s (%s)" % (states.clean_text(result["note"], 200), mode)
            store.add_event(item["id"], "info", note)
            out.append((item["id"], mode))
        else:
            store.add_event(item["id"], "warn", "couldn't open it automatically: %s" % result.get("error"))
            out.append((item["id"], "failed"))
    return out
