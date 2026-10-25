"""The one-call play flow: `play_episode` resolves, searches, ranks, tries and says plainly what happened
(Download Hub round 8, brief 8A; items N25, N26 and N27).

    run(args, ctx, route=..., lookup=..., server_line=...)

`play_episode` calls it when it has a title or an IMDb id and no stream of its own.  The steps:

    1. the video id (`tt2861424:1:3`; a film is its IMDb id).  The title was resolved, and a series with no
       season or episode was asked about, BEFORE this point, in actions/play_media.py (those answers are old and pinned).
    2. the stream search for that one episode (`discovery.streams_for`, with the play intent when the
       callee has it), ranked (`ranking.rank`, the play intent when it has it) against the owner's `want`:
       quality, audio, language, max_gb, min_seeders.
    3. `ranking.choose(.., policy="ask")`: an EXACT release goes ahead without asking; with none, the answer is a
       question with one option per tier (never a quiet swap of a dub for a sub).
    4. the chosen release and up to two more exact ones are tried, in order, each with its own short wait
       (BUDGET_WITH_INDEX_S / BUDGET_NO_INDEX_S).  The stream's file index goes all the way to the player.
    5. the answer says which release played, why it was chosen, and what was tried.  When nothing started it says
       `ok: false`, one line per release with the real reason, and the server's state.  It opens NO page by itself.

`pick=N` plays the N-th row of the last `stream_search` for the episode instead of searching (contract C1,
hub/stremio/pick_store.py, written by another agent: a missing module gives one sentence).

Callers hand in what they need (`route`, `lookup`, `server_line`), so this file imports nothing from actions/.
Helpers that call a function another agent is adding (`discovery.streams_for(.., intent=)`,
`ranking.rank(.., intent=)`, `ranking.normalize_want(.., language=)`) look at its signature first and fall back to
today's call (contracts C3 and C5).  The merge brief removes the checks.
"""

import importlib
import inspect

MAX_TRIES = 3                  # N25c: the chosen release and two more exact ones
BUDGET_WITH_INDEX_S = 8.0      # N27b (a proposal until the owner's experiment): the most one release may take
BUDGET_NO_INDEX_S = 15.0       # ... when the stream did not name a file and the file list must be waited for
SETUP_FAILURES = frozenset({"no_player", "server_down", "launch_failed", "blocked"})   # not about the release: stop

NO_SEARCH_SENTENCE = "no search is remembered for that episode: call stream_search first"
PAGE_SENTENCE = ("Nothing was opened. If the owner wants the episode's stream list in stremio-web, "
                 "call play_episode again with via=\"stremio\".")


def budget_for(parsed):
    """How long one release may take before the next one is tried."""
    return BUDGET_WITH_INDEX_S if (parsed or {}).get("file_idx") is not None else BUDGET_NO_INDEX_S


def whole(value):
    """A whole number from 1 (an int, or a numeric string), else None."""
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None


# --------------------------------------------------------------------------- calls into the other agents' code

def _accepts(fn, name):
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _streams_for(net, found, type_, video_id):
    """discovery.streams_for with the play intent (C5) when it has one, else the call as it is today."""
    from . import discovery
    if _accepts(discovery.streams_for, "intent"):
        return discovery.streams_for(net, found, type_, video_id, intent="play")
    return discovery.streams_for(net, found, type_, video_id)


def _rank(releases, want):
    """ranking.rank with the play intent (C3) when it has one, else today's order."""
    from . import ranking
    if _accepts(ranking.rank, "intent"):
        return ranking.rank(releases, want, intent="play")
    return ranking.rank(releases, want)


def _want(args):
    """-> (want, problem, notes).  `language` is passed on when normalize_want takes it (C3); when it does not,
    the answer says the language was not used, so nobody reads 'english' into a result that never checked it."""
    from . import ranking
    language = " ".join(str(args.get("language") or "").split())[:30] or None
    base = (args.get("quality"), args.get("audio"), args.get("max_gb"), args.get("min_seeders"))
    notes = []
    if language and _accepts(ranking.normalize_want, "language"):
        want, problem = ranking.normalize_want(*base, language=language)
    else:
        want, problem = ranking.normalize_want(*base)
        if language and not problem:
            notes.append("language %r was not used: the ranking in this build does not look at language, so only quality "
                         "and dub/sub counted. Read the release name before telling the owner it is %s." % (language, language))
    return want, problem, notes


def _want_text(want):
    bits = ["%dp" % want["resolution"] if want.get("resolution") else "any quality"]
    if want.get("audio") and want["audio"] != "any":
        bits.append(want["audio"])
    if want.get("language"):
        bits.append(str(want["language"]))
    return " ".join(bits)


# --------------------------------------------------------------------------- the pick store (contract C1)

def _pick_store():
    """The module another agent writes, or None while it does not exist (this build alone)."""
    try:
        return importlib.import_module((__package__ or "jarvis.hub.stremio") + ".pick_store")
    except ImportError:
        return None


def _remember(video_id, kept):
    """Best effort: keep the ranked list, so an option named in a question can be played with `pick`."""
    store = _pick_store()
    if store is None or not hasattr(store, "remember"):
        return False
    try:
        store.remember(video_id, kept)
        return True
    except Exception:  # noqa: BLE001  (a memory that fails must not stop a play)
        return False


def _recall(video_id, pick):
    """-> (candidate, problem)."""
    store = _pick_store()
    if store is None or not hasattr(store, "recall"):
        return None, NO_SEARCH_SENTENCE
    candidate = store.recall(video_id, pick)
    if not isinstance(candidate, dict):
        return None, NO_SEARCH_SENTENCE
    return candidate, None


# --------------------------------------------------------------------------- words about a release

def _facts(c):
    """'1080p dub, 1.4 GB, Fake Catalog listed 120 seeders': a release in one short phrase.  Seeders are the
    addon's number, said as that (N27): it is never a reason a release failed."""
    from . import ranking
    bits = [str(c.get("label") or "release")]
    gb = ranking.fmt_gb(c.get("size"))
    if gb:
        bits.append("%s GB" % gb)
    source = str(c.get("addon") or "an addon")
    if c.get("seeders") is not None:
        bits.append("%s listed %s seeders" % (source, c["seeders"]))
    elif c.get("addon"):
        bits.append("from %s" % source)
    if c.get("pack"):
        bits.append("a pack")
    return ", ".join(bits)


def _parsed_of(candidate):
    """The torrent of a ranked candidate -> {infohash, file_idx, trackers, name}, file index and trackers kept."""
    from . import links
    parsed = links.parse_stream(candidate.get("stream")) if isinstance(candidate.get("stream"), dict) else None
    return parsed or links.parse_stream({"infohash": candidate.get("infohash"), "file_idx": candidate.get("file_idx"),
                                         "name": candidate.get("name")})


def _release_row(c):
    from . import ranking
    row = {"label": c.get("label"), "tier": c.get("tier"), "size_gb": ranking.fmt_gb(c.get("size")),
           "seeders_listed": c.get("seeders"), "addon": c.get("addon"), "name": c.get("name")}
    if c.get("file_idx") is not None:
        row["file_idx"] = c["file_idx"]
    for key in ("pack", "file_known"):
        if c.get(key):
            row[key] = True
    if c.get("language_match") is not None:
        row["language_match"] = c["language_match"]
    return {k: v for k, v in row.items() if v not in (None, "")}


def _ask_with(c):
    """The quality and audio that would make this release an exact match, for the model to call again with."""
    out = {}
    if c.get("resolution"):
        out["quality"] = "%dp" % c["resolution"]
    out["audio"] = c["audio"] if c.get("audio") in ("dub", "sub") else "any"
    return out


# --------------------------------------------------------------------------- the flow

def _fail(ctx, vid, error, **more):
    out = {"ok": False, "played": False, "error": error, "video_id": vid}
    out.update({k: v for k, v in more.items() if v not in (None, [], {}, "")})
    if ctx.get("matched"):
        out["matched"] = ctx["matched"]
    return out


def run(args, ctx, *, route, lookup, server_line):
    """The whole flow for one episode or film.  `args` carries the resolved `imdb_id`; `ctx` the title lookup's
    `matched` text and `kind`.  `route(parsed, season, episode, args, budget_s)` starts ONE release and answers
    {ok, how, player} or {ok: False, error, reason, server}; `lookup()` -> (net, found, notes);
    `server_line()` -> "up v4.22.3" or "down".  Returns the tool's answer."""
    from . import addons as A
    from . import discovery, links, player, ranking
    kind = str(ctx.get("kind") or args.get("type") or "series").strip().lower()
    imdb = str(args.get("imdb_id") or "").strip().lower()
    season, episode = args.get("season"), args.get("episode")
    if kind == "series" and (season is None or episode is None):
        return {"needs_clarification": True, "message": "Which season and episode?"}
    try:
        vid = links.video_id(imdb, season, episode) if kind == "series" else imdb
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    pick = args.get("pick")
    if pick is not None:
        pick = whole(pick)
        if pick is None:
            return {"ok": False, "error": "pick must be a whole number from 1 (the rank of a stream_search row)"}
    want, notes, searched = {}, [], None
    if pick is None:
        want, problem, notes = _want(args)
        if problem:
            return {"ok": False, "error": problem}
    # a player is needed whatever the search finds: say so before any addon is asked
    kind_p, _exe, why = player.find(args.get("player"))
    if not kind_p:
        return _fail(ctx, vid, "%s %s" % (why.rstrip(". "), PAGE_SENTENCE), reason="no_player", server=server_line())

    # ---- the candidates
    if pick is not None:
        candidate, problem = _recall(vid, pick)
        if candidate is None:
            return {"ok": False, "error": problem}
        tries, chosen_by = [candidate], "the release you picked (number %d of the last search)" % pick
    else:
        net, found, lookup_notes = lookup()
        if not found:
            return _fail(ctx, vid, ("no addon is installed that can give streams. " + " ".join(str(n) for n in lookup_notes)).strip())
        if not discovery.offers_streams(found, kind, imdb):
            return _fail(ctx, vid, discovery.no_streams_sentence(kind, imdb))
        try:
            answer = _streams_for(net, found, kind, vid)
        except A.AddonError as exc:
            return _fail(ctx, vid, str(exc))
        releases = []
        for stream, addon_name in answer["streams"]:
            release = ranking.parse_release(stream)
            if release is not None:
                release["addon"] = addon_name
                releases.append(release)
        kept, rejected = _rank(releases, want)
        remembered = _remember(vid, kept) if kept else False
        exact = sum(1 for c in kept if c["tier"] == "exact")
        searched = {"asked": answer.get("asked"), "answered": len(answer.get("answered") or []), "found": len(kept), "exact": exact}
        problems = (answer.get("failed") or [])[:3]
        if not kept:
            if not answer.get("answered") and problems:
                why_none = "no addon answered: %s" % problems[0]
            else:
                why_none = ("no usable torrent release for that episode from the addons that answered "
                            "(if the season or episode may not exist, media_episodes lists them)")
            return _fail(ctx, vid, why_none + ". " + PAGE_SENTENCE, searched=searched, rejected=dict(rejected) or None,
                         problems=problems, notes=notes)
        decision = ranking.choose(kept, "ask")
        if decision["status"] != "exact":
            options = []
            for option in decision["options"]:
                first = next((i for i, c in enumerate(kept, 1) if c["tier"] == option["tier"]), None)
                row = {k: option.get(k) for k in ("label", "tier", "size_gb", "seeders", "name") if option.get(k) is not None}
                if first is not None:
                    row["ask_with"] = _ask_with(kept[first - 1])
                    if remembered:
                        row["pick"] = first
                options.append(row)
            text = _want_text(want)
            return {"needs_clarification": True, "video_id": vid, "searched": searched, "options": options, "notes": notes or None,
                    "matched": ctx.get("matched"),
                    "message": ("No %s release for that episode, so nothing was started. What exists is in options (one per kind). "
                                "Ask the owner which one to play, then call play_episode again with the same title, season and "
                                "episode and either its pick number or its ask_with quality and audio." % text)}
        tries = [decision["pick"]] + list(decision["fallbacks"])
        tries = tries[:MAX_TRIES]
        chosen_by = "the best of %d exact release%s (%d usable in all)" % (exact, "" if exact == 1 else "s", len(kept))

    # ---- try them, in order
    tried, last_server, setup_reason = [], None, None
    route_season = season if kind == "series" else None
    route_episode = episode if kind == "series" else None
    for number, candidate in enumerate(tries, 1):
        parsed = _parsed_of(candidate)
        if parsed is None:
            tried.append("#%d %s: no infohash in this release" % (number, _facts(candidate)))
            continue
        res = route(parsed, route_season, route_episode, args, budget_for(parsed))
        if res.get("ok"):
            tried.append("#%d %s: %s" % (number, _facts(candidate), res.get("how") or "started"))
            out = {"ok": True, "played": True, "tier": res.get("tier", 3), "how": res.get("how"), "player": res.get("player"),
                   "release": _release_row(candidate), "tried": tried, "video_id": vid,
                   "why": "%s: %s" % (chosen_by, _facts(candidate))}
            if searched:
                out["searched"] = searched
            if notes:
                out["notes"] = notes
            if ctx.get("matched"):
                out["matched"] = ctx["matched"]
            return out
        tried.append("#%d %s: %s" % (number, _facts(candidate), res.get("error") or "did not start"))
        last_server = res.get("server") or last_server
        if res.get("reason") in SETUP_FAILURES:
            setup_reason = res.get("error")
            break
    if setup_reason:
        error = "Could not start playback: %s." % setup_reason.rstrip(". ")
    else:
        error = "None of the %d release%s I tried could be started (see tried)." % (len(tried), "" if len(tried) == 1 else "s")
    return _fail(ctx, vid, error + " " + PAGE_SENTENCE, tried=tried, server=last_server or server_line(), searched=searched, notes=notes)
