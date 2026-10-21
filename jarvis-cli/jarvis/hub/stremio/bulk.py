"""\"Download the whole season of <show>\": plan it, then queue it as ONE group (plan 9.3, brief 07).

    make_plan(...)   asks the addons, ranks, and returns the dry-run table.  Queues NOTHING.
                     The plan is saved under ~/.jarvis/hub/bulk_plans/ and named by a plan id.
    run_plan(...)    queues a saved plan: one group, one hub row per episode, each row carrying its
                     own fallback releases.  Safe to run twice (nothing is duplicated).

Why `run_plan` writes rows with the store instead of calling `dl_add` (decision for the owner, see
the report): a row needs `show` / `season` / `episode` (the file name and folder) and `fallbacks`
(the next releases, so a release nobody seeds is replaced instead of failing the episode), and by
the N4 rule `dl_add` takes no engine-specific argument.  limitation H23 names exactly this: write
them yourself.  Everything else `add_items` does is done the same way here: the router checks the
source (`service.preview`), the dedupe key is `router.dedupe_key`, the row is created by
`Store.add_item`.  The engine reads `show` / `season` / `episode` from `options` and the fallback
list from `source_ref["fallbacks"]` (hub/engines/stremio.py), and nothing else here touches it.

No restart duplicates anything, in three ways:
  * the group id is a hash of (title id, season, audio, resolution), so a second run joins the same group;
  * a live row with the same infohash + file index is the same download (`router.dedupe_key`);
  * an episode this group already finished is skipped, not downloaded again.
"""

import hashlib
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .. import naming, paths, router, service, states
from .. import settings as settings_mod
from . import addons as A
from . import discovery, links, ranking
from ...atomic_io import read_json, write_json

PLAN_TTL_S = 24 * 3600
MAX_PLANS = 20
MAX_RUN_ITEMS = 60                     # plan 9.3: hard cap per run
MAX_RUN_GB = 150.0
FREE_MARGIN = 1.10
EPISODE_WORKERS = 4
PLAN_BUDGET_S = 75.0                   # asking every addon about every episode never takes longer
PLAN_ID_RE = re.compile(r"^bp[0-9a-f]{12}$")
_GB = 1024 ** 3


class BulkError(Exception):
    """A planning / running problem with a sentence the owner can act on."""


# --------------------------------------------------------------------------- plan files

def plans_dir():
    return paths.hub_dir() / "bulk_plans"


def _plan_path(plan_id):
    if not PLAN_ID_RE.match(str(plan_id or "")):
        raise BulkError("that isn't a plan id (they look like bp0123456789ab; bulk_plan makes one)")
    return plans_dir() / ("%s.json" % plan_id)


def save_plan(plan):
    folder = plans_dir()
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / ("%s.json" % plan["id"]), plan, indent=1)
    old = sorted(folder.glob("bp*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[MAX_PLANS:]
    for path in old:
        try:
            path.unlink()
        except OSError:
            pass


def load_plan(plan_id):
    path = _plan_path(plan_id)
    plan = read_json(path, default=None, expect=dict)
    if not plan:
        raise BulkError("no saved plan with that id (plans are kept for a day; run bulk_plan again)")
    if time.time() - float(plan.get("created") or 0) > PLAN_TTL_S:
        raise BulkError("that plan is more than a day old, and addons' answers change: run bulk_plan again")
    return plan


# --------------------------------------------------------------------------- names

def group_id_for(media_id, season, want):
    key = "%s|%s|%s|%s" % (media_id, season, want.get("audio"), want.get("resolution"))
    return "gb_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _se(season, episode):
    return "S%02dE%02d" % (season, episode)


def _want_text(want):
    bits = []
    if want.get("audio") in ("dub", "sub"):
        bits.append("dubbed" if want["audio"] == "dub" else "subbed")
    if want.get("resolution"):
        bits.append("%dp" % want["resolution"])
    return " ".join(bits) or "any quality"


def destination(library_root, show, season):
    show_clean = naming.sanitize_filename(show, default="Show", max_len=60)
    return "%s/Stremio/%s/Season %02d" % (str(library_root).replace("\\", "/").rstrip("/"), show_clean, season)


def _magnet_of(release):
    parsed = links.parse_stream(release["stream"])
    if not parsed:
        return None
    return links.build_magnet(parsed["infohash"], parsed["file_idx"], parsed["name"], parsed["trackers"])


def _entry(candidate):
    """What the plan remembers about a release (enough to queue it, and to explain it)."""
    return {"label": candidate["label"], "tier": candidate["tier"], "size": candidate["size"],
            "size_gb": ranking.fmt_gb(candidate["size"]), "seeders": candidate["seeders"],
            "name": candidate["name"], "addon": candidate.get("addon"), "magnet": _magnet_of(candidate)}


# --------------------------------------------------------------------------- planning

def _episode_candidates(net, found, type_, video, want):
    answer = discovery.streams_for(net, found, type_, video["id"])
    releases = []
    for stream, addon_name in answer["streams"]:
        release = ranking.parse_release(stream)
        if release is not None:
            release["addon"] = addon_name
            releases.append(release)
    return releases, answer


def make_plan(net, found, *, media_id, type_, season=None, episodes=None, want, policy="ask",
              library_root, budget_s=PLAN_BUDGET_S):
    """-> the plan dict (also saved).  Queues nothing."""
    if policy not in ranking.POLICIES:
        raise BulkError("on_missing must be one of: %s" % ", ".join(ranking.POLICIES))
    ep = discovery.episodes(net, found, type_, media_id)
    videos = ep["videos"]
    if not videos:
        raise BulkError("the addons list no episodes for %s; it may be a film (use dl_add or play_episode)" % (ep["title"] or media_id))
    seasons = discovery.seasons_of(videos)
    assumed = False
    if season in (None, "", "latest"):
        season = discovery.latest_season(videos)
        assumed = len([s for s in seasons if s["season"] > 0]) > 1
    try:
        season = int(season)
    except (TypeError, ValueError):
        raise BulkError("season must be a number (or 'latest')")
    in_season = [v for v in videos if v["season"] == season]
    if not in_season:
        raise BulkError("%s has no season %d; it has %s" % (ep["title"] or media_id, season,
                                                          ", ".join("S%d (%d eps)" % (s["season"], s["episodes"]) for s in seasons[:12])))
    wanted_numbers = None
    if episodes:
        wanted_numbers = set()
        for n in episodes[:200]:
            try:
                wanted_numbers.add(int(n))
            except (TypeError, ValueError):
                raise BulkError("episodes must be episode numbers")
        missing = sorted(wanted_numbers - {v["episode"] for v in in_season})
        if missing:
            raise BulkError("season %d has no episode %s" % (season, ", ".join(str(m) for m in missing[:8])))
        in_season = [v for v in in_season if v["episode"] in wanted_numbers]

    rows = {}
    notes = []
    todo = []
    for video in in_season:
        if video["out"]:
            todo.append(video)
        else:
            rows[video["episode"]] = {"episode": video["episode"], "season": season, "video_id": video["id"],
                                      "title": video["title"], "status": "unreleased",
                                      "note": "not released yet"}
    gathered = {}
    pool = ThreadPoolExecutor(max_workers=min(EPISODE_WORKERS, max(1, len(todo))))
    try:
        futures = {pool.submit(_episode_candidates, net, found, ep["type"], v, want): v for v in todo}
        try:
            for future in as_completed(futures, timeout=budget_s):
                video = futures[future]
                try:
                    gathered[video["episode"]] = future.result()
                except Exception as exc:  # noqa: BLE001 - one episode failing must not lose the rest
                    gathered[video["episode"]] = ([], {"answered": [], "failed": [str(exc)[:120]], "asked": 0})
        except Exception:  # noqa: BLE001 - timeout: the rest are reported as unanswered
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # first pass: each episode's best release, to learn the season's usual encode (bingeGroup)
    firsts = []
    for video in todo:
        releases, _ = gathered.get(video["episode"], ([], None))
        kept, _rej = ranking.rank(releases, want)
        firsts.append(kept[0] if kept else None)
    binge = ranking.usual_binge(firsts)

    addon_failures = set()
    for video in todo:
        if video["episode"] not in gathered:
            rows[video["episode"]] = {"episode": video["episode"], "season": season, "video_id": video["id"],
                                      "title": video["title"], "status": "unanswered",
                                      "note": "the addons didn't answer in time; plan this episode again"}
            continue
        releases, answer = gathered[video["episode"]]
        for sentence in answer.get("failed") or []:
            addon_failures.add(sentence)
        kept, rejected = ranking.rank(releases, want, batch_binge=binge)
        decision = ranking.choose(kept, policy)
        row = {"episode": video["episode"], "season": season, "video_id": video["id"], "title": video["title"],
               "status": decision["status"], "candidates": len(kept), "rejected": dict(rejected)}
        if decision["pick"]:
            row["pick"] = _entry(decision["pick"])
            row["fallbacks"] = [_entry(c) for c in decision["fallbacks"]]
            if decision["status"] == "fallback":
                row["note"] = "no %s release; using %s" % (_want_text(want), decision["pick"]["label"])
        elif decision["status"] == "pending":
            row["options"] = decision["options"]
            row["note"] = "no %s release" % _want_text(want)
        else:
            row["note"] = ("no release at all from the addons" if not kept
                           else "no %s release, and the plan skips what's missing" % _want_text(want))
            if answer.get("answered") == [] and answer.get("failed"):
                row["note"] = "no addon answered: %s" % answer["failed"][0]
        rows[video["episode"]] = row
    if addon_failures:
        notes.append("addon problems: " + "; ".join(sorted(addon_failures)[:4]))

    ordered = [rows[k] for k in sorted(rows)]
    queued = [r for r in ordered if r["status"] in ("exact", "fallback")]
    sizes = [(r["pick"].get("size") or 0) for r in queued]
    totals = {"episodes": len(ordered), "exact": sum(1 for r in ordered if r["status"] == "exact"),
              "fallback": sum(1 for r in ordered if r["status"] == "fallback"),
              "pending": sum(1 for r in ordered if r["status"] == "pending"),
              "none": sum(1 for r in ordered if r["status"] == "none"),
              "unreleased": sum(1 for r in ordered if r["status"] == "unreleased"),
              "unanswered": sum(1 for r in ordered if r["status"] == "unanswered"),
              "to_queue": len(queued), "size_gb": round(sum(sizes) / _GB, 1),
              "unknown_size": sum(1 for s in sizes if not s)}
    plan_id = "bp" + hashlib.sha1(("%s|%s|%s" % (media_id, season, time.time())).encode("utf-8")).hexdigest()[:12]
    show = ep["title"] or media_id
    plan = {"id": plan_id, "created": time.time(), "media": {"id": ep["id"], "type": ep["type"], "title": show,
                                                             "year": ep.get("year"), "images": ep.get("images") or {}},
            "season": season, "seasons_available": seasons, "season_assumed": assumed,
            "want": want, "policy": policy, "group_id": group_id_for(ep["id"], season, want),
            "group_title": "%s S%02d (%s)" % (show, season, _want_text(want)),
            "destination": destination(library_root, show, season), "episodes": ordered, "totals": totals,
            "notes": notes}
    save_plan(plan)
    return plan


def plan_message(plan):
    """The plan in words, for the model to relay and the owner to confirm."""
    t, want = plan["totals"], plan["want"]
    show = plan["media"]["title"]
    out = ["%s season %d: %d episode%s, %s." % (show, plan["season"], t["episodes"], "" if t["episodes"] == 1 else "s", _want_text(want))]
    if plan.get("season_assumed"):
        out.append("You didn't name a season, so this is the latest one (%d of %d); say so if you meant another."
                   % (plan["season"], len([s for s in plan["seasons_available"] if s["season"] > 0])))
    got = t["exact"] + t["fallback"]
    size = ("about %.1f GB" % t["size_gb"]) if t["size_gb"] else "size not reported"
    if t["unknown_size"]:
        size += " (%d without a size)" % t["unknown_size"]
    out.append("%d of them have a release to queue (%s), saved to %s, via Stremio." % (got, size, plan["destination"]))
    if t["fallback"]:
        out.append("%d use a lesser release than asked for (see each row's note)." % t["fallback"])
    pending = [r for r in plan["episodes"] if r["status"] == "pending"]
    if pending:
        eps = ", ".join(str(r["episode"]) for r in pending[:12])
        kinds = sorted({o["label"] for r in pending for o in r.get("options", [])})
        out.append("Episode%s %s ha%s no %s release. Ask what to do: %s, or skip %s. "
                   "(Plan again with on_missing set to the choice; only the missing episodes need planning.)"
                   % ("s" if len(pending) > 1 else "", eps, "ve" if len(pending) > 1 else "s", _want_text(want),
                      " / ".join(kinds) or "nothing else exists", "them" if len(pending) > 1 else "it"))
    for key, text in (("none", "have no release the plan allows"), ("unreleased", "aren't out yet"),
                      ("unanswered", "didn't get an answer from the addons in time")):
        rows = [r["episode"] for r in plan["episodes"] if r["status"] == key]
        if rows:
            out.append("Episode%s %s %s." % ("s" if len(rows) > 1 else "", ", ".join(str(n) for n in rows[:12]), text))
    out.append("Nothing is queued yet. If they say yes: bulk_run with this plan_id.")
    return " ".join(out)


def public_plan(plan, detail=True):
    """What the tool returns: the table without the magnets (long, and not for the model)."""
    rows = []
    for r in plan["episodes"]:
        row = {"episode": r["episode"], "title": r.get("title") or "", "status": r["status"]}
        if r.get("pick"):
            row["pick"] = {k: r["pick"].get(k) for k in ("label", "size_gb", "seeders", "addon")}
            row["fallbacks"] = len(r.get("fallbacks") or [])
        if r.get("options"):
            row["options"] = [{k: o.get(k) for k in ("policy", "label", "size_gb")} for o in r["options"]]
        if r.get("note"):
            row["note"] = r["note"]
        rows.append(row)
    out = {"plan_id": plan["id"], "title": plan["media"]["title"], "season": plan["season"],
           "season_assumed": plan["season_assumed"], "seasons": plan["seasons_available"][:12],
           "wants": _want_text(plan["want"]), "on_missing": plan["policy"], "group": plan["group_title"],
           "destination": plan["destination"], "totals": plan["totals"], "notes": plan.get("notes") or [],
           "message": plan_message(plan)}
    if detail:
        out["episodes"] = rows[:120]
    return out


# --------------------------------------------------------------------------- running

def _free_bytes(path):
    probe = Path(path)
    for _ in range(12):
        if probe.exists():
            try:
                return shutil.disk_usage(str(probe)).free
            except OSError:
                return None
        if probe.parent == probe:
            return None
        probe = probe.parent
    return None


def _clean_entry(entry):
    """Re-check a saved magnet before it is queued (a plan file is data, not trust)."""
    parsed = links.parse_magnet((entry or {}).get("magnet") or "")
    if not parsed:
        return None
    return links.build_magnet(parsed["infohash"], parsed["file_idx"], parsed["name"], parsed["trackers"])


def run_plan(plan_id, *, store, override_caps=False, start_paused=False):
    """Queue a saved plan as one group.  -> a result dict (`ok`, counts, `group`, notes)."""
    plan = load_plan(plan_id)
    show = states.clean_text(plan["media"]["title"], 80) or "Show"
    season = int(plan["season"])
    wanted = [r for r in plan["episodes"] if r.get("status") in ("exact", "fallback") and r.get("pick")]
    held = [r for r in plan["episodes"] if r.get("status") == "pending"]
    if not wanted:
        return {"ok": False, "error": "nothing in that plan can be queued yet"
                + ((": episodes %s need a decision (plan again with on_missing)" % ", ".join(str(r["episode"]) for r in held[:12])) if held else "")}
    total = sum((r["pick"].get("size") or 0) for r in wanted)
    if not override_caps and (len(wanted) > MAX_RUN_ITEMS or total > MAX_RUN_GB * _GB):
        return {"ok": False, "error": "that is %d episodes / %.0f GB; one run is capped at %d episodes / %d GB. "
                                      "Run it with override_caps=true if you mean it (it asks first)."
                                      % (len(wanted), total / _GB, MAX_RUN_ITEMS, int(MAX_RUN_GB))}
    settings = store.get_settings()
    root = settings_mod.library_root(settings)
    notes = []
    unknown = sum(1 for r in wanted if not r["pick"].get("size"))
    free = _free_bytes(root)
    if total and free is not None and unknown <= len(wanted) // 2 and free < int(total * FREE_MARGIN):
        return {"ok": False, "error": "not enough free space at %s: the plan needs about %.1f GB (+10%%), %.1f GB free. "
                                      "Free some space or plan fewer episodes." % (str(root)[:80], total / _GB, free / _GB)}
    if unknown:
        notes.append("%d episode%s had no size, so the disk check counts only the others." % (unknown, "" if unknown == 1 else "s"))

    gid = store.ensure_group(plan.get("group_title") or "%s S%02d" % (show, season), kind="batch",
                             meta={"show": show, "season": season, "plan": plan["id"], "media_id": plan["media"]["id"]},
                             group_id=plan.get("group_id") or group_id_for(plan["media"]["id"], season, plan["want"]))
    existing = {}
    for row in store.list_items(group_id=gid, limit=500):
        opts = row.get("options") or {}
        if opts.get("episode") is not None and opts.get("season") is not None:
            existing.setdefault((int(opts["season"]), int(opts["episode"])), []).append(row)

    queued, duplicates, already, skipped = [], [], [], []
    for r in wanted:
        number = int(r["episode"])
        rows_here = existing.get((season, number), [])
        if any(x["state"] == "done" for x in rows_here):
            already.append(number)
            continue
        source = _clean_entry(r["pick"])
        if not source:
            skipped.append({"episode": number, "problem": "its saved release is unreadable; plan again"})
            continue
        fallbacks = [m for m in (_clean_entry(f) for f in (r.get("fallbacks") or [])[:ranking.MAX_FALLBACKS]) if m]
        decision = service.preview(source, engine="stremio", store=store)
        if not decision["ok"]:
            problem = decision.get("problem") or "the Stremio engine can't take it"
            if not queued and not duplicates and not already and not skipped:
                return {"ok": False, "error": problem}        # the same reason would stop every row: say it once
            skipped.append({"episode": number, "problem": states.clean_text(problem, 160)})
            continue
        options = {"show": show, "season": season, "episode": number, "meta_id": plan["media"]["id"],
                   "meta_type": plan["media"]["type"]}
        item, created = store.add_item(
            engine="stremio", source_url=decision["source"], title="%s %s" % (show, _se(season, number)),
            kind="video", options=options, source_ref={"fallbacks": fallbacks} if fallbacks else None,
            group_id=gid, state="paused" if start_paused else "queued",
            dedupe_key=router.dedupe_key("stremio", router.canonical_url(decision["source"]), options))
        (queued if created else duplicates).append(number)
    out = {"ok": bool(queued or duplicates or already), "group": gid, "group_title": plan.get("group_title"),
           "queued": queued, "already_queued": duplicates, "already_downloaded": already, "skipped": skipped,
           "held_for_a_decision": [r["episode"] for r in held], "destination": plan["destination"], "library_root": str(root)}
    if not out["ok"]:
        out["error"] = skipped[0]["problem"] if skipped else "nothing was queued"
    if queued and not service.daemon_view(store)["alive"]:
        notes.append("The download hub daemon isn't running, so nothing will start until it is (Menu > Daemons > Download hub).")
    if queued:
        notes.append("They download as one group (Menu > Downloads; dl_list group=%s). A release nobody seeds is replaced by the "
                     "next one automatically, and the usual 'batch finished' notice comes when the whole group is done." % gid)
    out["message"] = _run_message(out, show, season)
    if notes:
        out["note"] = " ".join(notes)
    return out


def _run_message(out, show, season):
    bits = []
    if out["queued"]:
        bits.append("Queued %d episode%s of %s season %d as one group" % (len(out["queued"]), "" if len(out["queued"]) == 1 else "s", show, season))
    if out["already_queued"]:
        bits.append("%d were already queued (nothing doubled)" % len(out["already_queued"]))
    if out["already_downloaded"]:
        bits.append("%d were already downloaded" % len(out["already_downloaded"]))
    if out["skipped"]:
        bits.append("%d could not be queued" % len(out["skipped"]))
    if out["held_for_a_decision"]:
        bits.append("episodes %s still wait for a decision" % ", ".join(str(n) for n in out["held_for_a_decision"][:12]))
    return "; ".join(bits) + "." if bits else "Nothing was queued."


def plan_json_safe(plan):
    """For tests: the plan survives a JSON round trip."""
    return json.loads(json.dumps(plan))
