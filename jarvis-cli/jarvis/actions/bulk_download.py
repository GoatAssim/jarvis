"""Auto-discovered tool file: "download me the whole season of <show> using Stremio, dub, 1080p"
(Download Hub plan, phase 6 / 8; brief 07, N13).

  bulk_plan   resolves the show (asks when ambiguous), picks the season (the latest when none is
              named, and says so), finds the episodes, asks every installed addon for each
              episode's streams, ranks them (quality, then dub / sub, read from the stream's text)
              and returns the PLAN: which release each episode gets, the size, the episodes with no
              match.  It queues NOTHING.
  bulk_run    queues a saved plan as ONE group.  Confirm-gated out of band, like dl_cancel.

The model's part is the conversation: show the plan, ask when an episode has no match, get a yes,
call bulk_run.  The rules (what counts as a dub, what a missing episode may fall back to, the caps,
the disk check, "never queue twice") are in hub/stremio/ranking.py and bulk.py, not in a prompt.

No argument was added to `dl_add` (the N4 rule): a season row needs show / season / episode and
its fallback releases, which only bulk_run's own rows carry.
"""


import json
import os


def _is_chat_guest():
    """True when this turn answers a chat message from someone who is NOT the owner.  No chat context
    (the PC, a scheduled job) is the owner's own.  Same rule as download_hub._is_chat_guest: an
    unreadable value counts as a guest, because a permission check fails closed."""
    raw = os.environ.get("JARVIS_CHANNEL_SENDER") or ""
    if not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001  (bad JSON, or nested too deep to read: either way, not the owner)
        return True
    if not isinstance(data, dict):
        return True
    if not data.get("platform") or not data.get("user_id"):
        return False
    return not data.get("is_owner")


def _guest_refusal(tool):
    return {"ok": False, "error": "%s is the owner's: someone in a chat can't use it" % tool}


def _fail(label, exc):
    return {"ok": False, "error": "%s failed: %s" % (label, exc)}


def tool_bulk_plan(args):
    if _is_chat_guest():
        return _guest_refusal("bulk_plan")
    try:
        args = args or {}
        from ..hub import settings as settings_mod
        from ..hub.store import Store
        from ..hub.stremio import addons as A, bulk, discovery, ranking
        from . import stremio_discovery as disc

        title, media_id = str(args.get("title") or "").strip(), A.valid_id(args.get("id"))
        if not title and not media_id:
            return {"needs_clarification": True, "message": "Which show? Give its name (or an id from media_find)."}
        want, problem = ranking.normalize_want(args.get("quality"), args.get("audio"), args.get("max_gb_per_episode"),
                                               args.get("min_seeders"))
        if problem:
            return {"ok": False, "error": problem}
        policy = str(args.get("on_missing") or "ask").strip().lower()
        if policy not in ranking.POLICIES:
            return {"ok": False, "error": "on_missing must be one of: %s" % ", ".join(ranking.POLICIES)}

        store = Store()
        net = A.Net(allow=store.get_settings().get("allow_private_hosts") or [])
        found, notes = A.installed(net)
        if not found:
            return {"ok": False, "error": "no Stremio addon is known. " + " ".join(notes)}
        if not discovery.offers_streams(found):
            return {"ok": False, "error": discovery.no_streams_sentence(), "note": " ".join(notes) or None}

        type_ = A.valid_type(args.get("type")) if args.get("type") else None
        if media_id:
            kind_hint = type_ or "series"
        else:
            kind, payload = disc.resolve_title(net, found, title, type_)
            if kind == "none":
                return {"ok": False, "error": "no title found for %r; if that is an abbreviation, try the full title" % title[:60]}
            if kind == "ask":
                return {"ok": False, "needs_clarification": True,
                        "message": "Several titles match %r; which one?" % title[:60],
                        "matches": [disc._public_result(r) for r in payload[:8]]}
            media_id, kind_hint = payload["id"], payload["type"]

        episodes = args.get("episodes")
        if episodes is not None and not isinstance(episodes, list):
            return {"ok": False, "error": "episodes must be a list of episode numbers"}
        try:
            plan = bulk.make_plan(net, found, media_id=media_id, type_=kind_hint, season=args.get("season"),
                                  episodes=episodes, want=want, policy=policy,
                                  library_root=settings_mod.library_root(store.get_settings()))
        except (bulk.BulkError, A.AddonError) as exc:
            return {"ok": False, "error": str(exc)}
        out = bulk.public_plan(plan)
        out["ok"] = True
        if plan["notes"] or notes:
            out["note"] = " ".join(list(plan["notes"]) + list(notes))
        out["next"] = ("Nothing is queued. Read the message to the owner and ask if they want it. "
                       "Then bulk_run with plan_id=%s." % plan["id"])
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail("bulk_plan", exc)


def tool_bulk_run(args):
    if _is_chat_guest():
        return _guest_refusal("bulk_run")
    try:
        args = args or {}
        from ..hub.store import Store
        from ..hub.stremio import bulk
        if not args.get("plan_id"):
            return {"needs_clarification": True, "message": "Which plan? bulk_plan gives a plan_id."}
        try:
            return bulk.run_plan(str(args["plan_id"]).strip(), store=Store(), override_caps=args.get("override_caps") is True,
                                 start_paused=args.get("start_paused") is True)
        except bulk.BulkError as exc:
            return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _fail("bulk_run", exc)


TOOL_SCHEMAS = [
    {"name": "bulk_plan",
     "description": ("Plan a whole season (or chosen episodes) of a show through the owner's Stremio addons: finds the show, picks the "
                     "season, asks every addon for each episode's streams and picks the best release per episode by quality and by "
                     "audio (dub / sub). Returns a plan table with sizes and the episodes with no match. QUEUES NOTHING: show it to "
                     "the owner, ask about any episode with no match, then bulk_run. If the show name is ambiguous it asks which one."),
     "parameters": {"type": "object", "properties": {
         "title": {"type": "string", "description": "The show as the owner said it (or use id)."},
         "id": {"type": "string", "description": "An id from media_find (skips the title lookup)."},
         "type": {"type": "string", "description": "From media_find; omit to let the lookup decide."},
         "season": {"type": "integer", "description": "Omit for the latest released season (the plan says so)."},
         "episodes": {"type": "array", "items": {"type": "integer"}, "description": "Only these episodes (default the whole season)."},
         "quality": {"type": "string", "description": "2160, 1080, 720 or 480 (4k, 1080p ...). Omit for any."},
         "audio": {"type": "string", "enum": ["dub", "sub", "any"], "description": "dub = an English (dubbed) audio track, read from the release's text. Default any."},
         "max_gb_per_episode": {"type": "number"}, "min_seeders": {"type": "integer"},
         "on_missing": {"type": "string", "enum": ["ask", "skip", "other_quality", "other_audio", "best"],
                        "description": ("What an episode with no exact match gets. ask (default): it is left out and listed with what exists, so "
                                        "the owner can choose. skip: left out for good. other_quality: the right audio at the closest other "
                                        "quality. other_audio: the right quality with the other audio. best: whatever is best.")}}}},
    {"name": "bulk_run",
     "description": ("Queue a plan from bulk_plan as ONE collapsible group of downloads (the owner is asked to confirm). Safe to run "
                     "twice: nothing is queued twice and finished episodes are skipped. Episodes still waiting for a decision are not queued."),
     "parameters": {"type": "object", "properties": {
         "plan_id": {"type": "string", "description": "From bulk_plan."},
         "override_caps": {"type": "boolean", "description": "Allow more than 60 episodes / 150 GB in one run."},
         "start_paused": {"type": "boolean", "description": "Queue them paused."}},
         "required": ["plan_id"]}},
]

TOOLS = {"bulk_plan": tool_bulk_plan, "bulk_run": tool_bulk_run}
TOOL_GROUP = "stremio"

TOOL_KEYWORDS = {
    "bulk_plan": {"whole season": 10, "full season": 10, "entire season": 10, "the season of": 9, "all episodes": 10,
                  "every episode": 9, "season pack": 8, "queue a season": 10, "download the season": 10, "download season": 9,
                  "using stremio": 7, "dub 1080": 8, "dubbed 1080": 8},
    "bulk_run": {"queue the season": 10, "start the season": 9, "run the plan": 10, "yes queue it": 8},
}

# The group's one instruction text lives in stremio_stack.py (one declarer per group).

# One group of up to 60 downloads: confirmed out of band, never by an argument the model could fill.
TOOL_CONFIRM_REQUIRED = {"bulk_run"}
TOOL_AI_REVIEW = set()

TOOL_RESULT_SPECS = {
    "bulk_plan": {
        "drop_fields": {"medium": ["seasons"], "low": ["seasons", "notes"]},
        "list_item_drop": {"episodes": {"medium": ["title"], "low": ["title", "options"]}},
    },
}

TEST_CHECKLIST = {
    "bulk_plan": {
        "does": "Plans a whole season through your Stremio addons (show, season, best release per episode, size, gaps). Queues nothing.",
        "steps": [
            {"ask": "download me the whole season of <a show> using stremio dub 1080p (just a test)",
             "expect": "Resolves the show (asks which one if ambiguous), says which season, shows a plan with a size, and lists any episode with no dubbed 1080p release with what exists instead. Nothing is queued."},
            {"run": {"title": "<a show>", "season": 1, "quality": "1080", "audio": "dub"},
             "expect": "ok: true, a plan_id, per-episode status (exact / pending / none / unreleased) and a message in words."},
            {"run": {"title": "<a show>", "season": 1, "quality": "1080", "audio": "dub", "on_missing": "other_quality"},
             "expect": "episodes without a dubbed 1080p get the closest other quality with dub, marked fallback with a note."},
            {"run": {"title": "<a show>", "season": 99}, "expect": "ok: false, says the show has no season 99 and lists the seasons it has."},
            {"run": {},
             "expect": "No error, a question: needs_clarification 'Which show? Give its name (or an id from media_find).'; nothing is planned."},
            {"run": {"title": "<a show>", "season": 1},
             "expect": "With the network cut and no addon reachable: ok: false, a sentence starting 'no Stremio addon is known' and giving the reason Cinemeta was refused; nothing is planned or queued."},
        ],
        "needs": ["Stremio addons that offer streams (account key or addons.urls in ~/.jarvis/stremio.json)"],
        "care": "Asks every addon about every episode (a season of 12 with 5 addons is 60 requests, run 8 at a time).",
        "watch": ["⚠️ Whether the plan's dub / 1080p reading matches your addons' real wording is unchecked: compare a plan row with the addon's own stream text (tests/addon_checks.py).",
                  "A release that doesn't state its language is never picked as a dub.",
                  "From a remote chat guest (Discord / Instagram) it is refused before anything runs: 'bulk_plan is the owner's: someone in a chat can't use it'; a guest is not even offered the tool.",
                  "The season-99 step (and the plan steps) were not run against real addons when this entry was checked; only the empty-call and offline answers were."],
    },
    "bulk_run": {
        "does": "Queues a bulk_plan as one group of downloads in the hub.",
        "steps": [
            {"run": {"plan_id": "<plan_id from bulk_plan>"},
             "expect": "asks first; then one group of downloads appears in Menu > Downloads, one row per planned episode, saved under Stremio/<Show>/Season NN."},
            {"run": {"plan_id": "<the same plan_id>"}, "expect": "asks first; then ok: true with every episode 'already queued' and nothing new in the list."},
            {"run": {"plan_id": "bp000000000000"}, "expect": "ok: false, 'no saved plan with that id (plans are kept for a day; run bulk_plan again)'; nothing is queued."},
            {"run": {},
             "expect": "No error, a question: needs_clarification 'Which plan? bulk_plan gives a plan_id.'; nothing is queued."},
        ],
        "needs": ["the Stremio server (stack_start) and the download hub daemon"],
        "care": "Starts up to 60 downloads. Disk space is checked first; a release nobody seeds is replaced by the next one on its own.",
        "watch": ["Restart Jarvis (or the hub daemon) mid-season and run bulk_run again: no duplicate rows.",
                  "⚠️ never tried against a real Stremio server or real addons.",
                  "From a remote chat guest it is refused before anything runs: 'bulk_run is the owner's: someone in a chat can't use it'; a guest is not even offered the tool."],
    },
}
