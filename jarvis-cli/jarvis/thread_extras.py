"""The saved "extras" of an exchange -- screenshot cards, download cards, a
presented file, an organize-json result, a dev_agent trace, a resolved
confirmation, a declined action -- derived from the turn's tool runs.

WHY THIS IS ITS OWN MODULE (master plan L.38)
---------------------------------------------
These used to be computed inside ai_client at the moment a turn finished and
saved once. The raw event log (raw_archive.py) now keeps every tool run at full
size, so the same derivation can be applied to the log later: fix a card, add a
card kind or lift a cap here and history picks it up, with no storage change.
ai_client's ``_extras_from_runs`` / ``_declined_action_extra`` are thin wrappers
over the functions below, so write time and rebuild time run the same code.

Pure: no I/O, no config, no imports from the rest of the package.
"""

import json
import time

# Extras that are a function of the turn's tool runs and nothing else. Anything
# not listed (thinking, interimText, trace, console, consoleRef, offered
# action, ...) comes from somewhere other than the runs, so a rebuild keeps
# the saved copy of those and only re-derives these.
TOOL_DERIVED_TYPES = frozenset({
    "confirm", "screenshot", "organizeJson", "download", "presentFile",
    "devAgent", "declinedAction",
})


# code_agent saves its steps too (L.47b), but NOT verbatim. Its "start" events
# carry the model's whole tool arguments -- the full text of every write_file /
# edit_file call -- and a saved conversation must not become a second copy of
# the user's source. Only the small, display-relevant arguments survive: enough
# for the stepper card and the Focus Agent panel to name the file / command /
# pattern, nothing that is file content. Every other field is kept as it came.
_CODE_ARG_KEEP = ("path", "command", "pattern", "glob", "start_line", "end_line", "is_regex")
_CODE_ARG_CLIP = 300
_CODE_STEPS_CAP = 400   # a code_agent run is bounded by its rounds; this is a backstop
_CODE_TEXT_CLIP = 400   # outcome / error strings


def _slim_code_agent_steps(steps):
    out = []
    for e in steps[:_CODE_STEPS_CAP]:
        if not isinstance(e, dict):
            continue
        e = dict(e)
        args = e.get("arguments")
        if isinstance(args, dict):
            kept = {}
            for k in _CODE_ARG_KEEP:
                v = args.get(k)
                if isinstance(v, str):
                    kept[k] = v[:_CODE_ARG_CLIP]
                elif isinstance(v, (int, float, bool)):
                    kept[k] = v
            e["arguments"] = kept
        for k in ("outcome", "error"):
            if isinstance(e.get(k), str) and len(e[k]) > _CODE_TEXT_CLIP:
                e[k] = e[k][:_CODE_TEXT_CLIP]
        e.pop("listing", None)   # live-stream only (see code_agent's executor)
        out.append(e)
    return out


def extras_from_runs(runs):
    """Turns this turn's tool_executor.runs (see _make_tool_executor) into
    the same lightweight 'screenshot / download / organizeJson / confirm'
    shape the web UI already builds client-side for a live ask (see
    web/public/app.js's pushThreadExtra) so conversations.append_exchange
    can save them alongside the exchange. That's what lets the browser
    replay a screenshot, download card, organize-json result, or a
    resolved confirmation after a genuine page reload/reconnect — not just
    for as long as that browser tab's in-memory state happens to survive.
    """
    extras = []
    for run in (runs or []):
        name = run.get("name")
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        confirm = run.get("confirm")
        if isinstance(confirm, dict):
            extras.append({
                "type": "confirm",
                "data": {
                    "tool": name,
                    "arguments": run.get("arguments") or {},
                    "risk_note": confirm.get("risk_note"),
                    "resolved": bool(confirm.get("approved")),
                },
            })
        if name == "take_screenshot" and result.get("ok") and result.get("file"):
            extras.append({"type": "screenshot", "data": {"filename": result["file"]}})
        elif name == "organize_json" and result.get("ok") and result.get("path"):
            extras.append({"type": "organizeJson", "data": {"targetPath": result["path"], "payload": None}})
        elif name == "ytdl_download" and result.get("ok") and result.get("job_id"):
            # Mirror ytdl_tools.py's own MAX_MEDIA_EMITTED cap — only the
            # first few files ever got an inline player card live (see its
            # _emit_media loop), so a saved/replayed turn shouldn't show
            # more cards than the user actually saw at the time.
            for f in (result.get("files") or [])[:5]:
                if isinstance(f, dict) and f.get("file"):
                    extras.append({
                        "type": "download",
                        "data": {
                            "jobId": result["job_id"],
                            "filename": f["file"],
                            "title": f.get("title") or f["file"],
                        },
                    })
        elif name == "present_file" and result.get("ok"):
            # The gap that made present_file cards disappear on reload: every
            # other media tool had an entry here, this one never did, so its
            # card lived only in the live JARVIS_MEDIA stream. The field
            # names match app.js's showAskPresentFile(info) argument exactly
            # so the replay path can hand this straight to the same renderer
            # the live path uses, rather than a second near-copy of it.
            extras.append({
                "type": "presentFile",
                "data": {
                    "jobId": result.get("job_id"),
                    "filename": result.get("download_filename"),
                    "name": result.get("name"),
                    "type": result.get("type") or "file",
                    "sizeBytes": result.get("size_bytes"),
                    "path": result.get("path"),
                },
            })
        elif name == "dev_agent" and isinstance(result.get("steps"), list):
            # §3.6 plan §6. `result` here is run["result"] — the FULL,
            # pre-shaping copy (see the ordering fix in _executor above) —
            # so `steps` still has every preview/stdout_tail/stderr_tail
            # field a live view showed, not whatever verbosity trimmed for
            # the model. These are the exact same event dicts the live
            # stream already displayed (see dev_agent_events.emit's
            # docstring: dev_agent.py's own `steps` list is built from
            # emit()'s return value, never reconstructed separately), so a
            # reload replays provably the same trace, not an approximation
            # of it.
            extras.append({
                "type": "devAgent",
                "data": {
                    "jobId": result.get("job_id"),
                    "ok": result.get("ok"),
                    "projectDir": result.get("project_dir"),
                    "steps": result["steps"],
                },
            })
        elif name == "code_agent" and isinstance(result.get("steps"), list):
            # Same card type as dev_agent: renderThreadExtra's "devAgent" case
            # and the live stream already draw code_agent's events through
            # the same stepper, and the Focus Agent panel's restore() reads
            # this shape. `root` is code_agent's name for dev_agent's
            # `project_dir`. The steps are slimmed -- see _slim_code_agent_steps.
            extras.append({
                "type": "devAgent",
                "data": {
                    "jobId": result.get("job_id"),
                    "ok": result.get("ok"),
                    "projectDir": result.get("root"),
                    "steps": _slim_code_agent_steps(result["steps"]),
                },
            })
    return extras


def declined_action_extra(turn_runs, ts=None):
    """A `declinedAction` extra for THIS turn if its LAST tool call was
    declined by the user -- None otherwise. Only the last call counts: a
    decline three calls back in a turn that went on to do other things
    isn't "the thing that just got declined" by the time the turn ends.

    `ts` defaults to now (write time); a rebuild passes the time the declined
    call actually ran, so the freshness check on a repeat still means what it
    meant."""
    if not turn_runs:
        return None
    last = turn_runs[-1]
    result = last.get("result")
    name, args = last.get("name"), last.get("arguments")
    if (not isinstance(result, dict) or not result.get("cancelled")
            or not isinstance(name, str) or not isinstance(args, dict)):
        return None
    try:
        json.dumps(args)
    except (TypeError, ValueError):
        return None
    return {"type": "declinedAction", "data": {
        "name": name, "arguments": args, "ts": time.time() if ts is None else ts,
    }}


def rebuild_turn_extras(runs, stored_extras=None, ts=None):
    """The extras list for one exchange, re-derived from its raw tool runs.

    Tool-derived extras (TOOL_DERIVED_TYPES) are rebuilt from `runs`; every
    other saved extra is kept as it was saved, in its saved order, after them.
    That is the order the write path produces (run extras, the declined
    action, then thinking / interim text / trace / console). With no runs
    there is nothing to re-derive from, so the saved extras are returned
    unchanged -- a log that begins mid-turn never costs a card.
    """
    stored = [e for e in (stored_extras or []) if isinstance(e, dict)]
    if not runs:
        return stored
    derived = extras_from_runs(runs)
    declined = declined_action_extra(runs, ts=ts)
    if declined:
        derived.append(declined)
    kept = [e for e in stored if e.get("type") not in TOOL_DERIVED_TYPES]
    return derived + kept
