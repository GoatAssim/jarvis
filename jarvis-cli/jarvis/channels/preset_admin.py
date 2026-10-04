"""Quick setups and bulk edits for the Channels panel (L.36-P4 and P5).

WHAT THIS ADDS -- AND WHAT IT DOES NOT
--------------------------------------
Nothing here decides access and nothing here writes a store directly. Every
change is made by calling user_admin.set_flag() and user_admin.set_tools(),
the same calls the individual switches make, so:

  * a preset or a bulk edit can only produce states the switches can produce;
  * every refusal those calls already make (blocked, "*" covering someone,
    unreadable limits, one owner per platform) still happens and is REPORTED
    per person and per switch, never swallowed;
  * the panel, `jarvis channels-preset` and `jarvis channels-bulk` run this
    one implementation (server.js shells out; it re-implements nothing).

ORDER OF WRITES (fails closed)
------------------------------
When a setup turns tools on, the tool LIST is written first and the `tool`
switch second, the order the panel's own "Only tools I choose" button uses.
If the list cannot be written the switch is not turned on -- the person ends
up with the access they had, never with every tool because half a setup ran.

PREVIEW
-------
plan_preset() computes what a setup would do from the stored state, through
user_admin.list_flag_refusal() (the same rule set_flag() applies), and writes
nothing. apply_preset() runs the same plan and then makes the calls; whatever
the real call says is what is reported, so a preview can be wrong only by a
change that happened in between.

WHAT A SETUP NEVER TOUCHES
--------------------------
Ownership, `send_dm`, blocking, the platform's master `allow_tools` switch
(it is only mentioned when it would make a granted tool list pointless), a
stored tool list when the setup leaves tools off, and anyone's messages.

THE OWNER IS LEFT ALONE
-----------------------
A setup is refused for the platform's owner: a custom tool list limits the
owner inside chats too, and handing that to a one-click button is how the
owner gets locked out of their own bot. The owner's switches are still there
on their own page. In a bulk edit the owner is reported as refused like
anyone else who cannot be changed.
"""

from . import PERM_DM, PERM_REPLY, PERM_TOOLS
from . import config as channel_config
from . import people, permissions, presets, user_admin, user_perms

MAX_BULK = 100                       # people per request
EXEC_ORDER = ("tools", "tool", "reply", "dm")   # tool list before the switch
DISPLAY_ORDER = ("dm", "reply", "tool", "tools")
LABELS = {"dm": "Direct messages", "reply": "Replies", "tool": "Tool use",
          "tools": "Which tools"}
# What a bulk edit may flip. `owner` is refused per person (one owner per
# platform); `tool` can be switched OFF in bulk but not ON, because turning it
# on without saying WHICH tools is the broadest grant there is -- a quick
# setup does both together.
BULK_FLAGS = ("dm", "reply", "tool", "send_dm", "blocked", "owner")

ST_APPLIED, ST_UNCHANGED, ST_PARTIAL = "applied", "unchanged", "partial"
ST_REFUSED, ST_FAILED, ST_PREVIEW = "refused", "failed", "preview"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _name(rec):
    return (people.effective_name(rec) or
            ("@" + rec["handle"] if rec.get("handle") else "") or
            str(rec.get("user_id") or ""))


def _onoff(value):
    return "on" if value else "off"


def _scope_text(mode, allow):
    if mode == user_perms.TOOLS_CUSTOM:
        names = [n for n in (allow or [])]
        return "only: " + ", ".join(names) if names else "only: nothing"
    return "every tool"


def _same_scope(mode_a, allow_a, mode_b, allow_b):
    if mode_a != mode_b:
        return False
    if mode_a == user_perms.TOOLS_INHERIT:
        return True
    return set(allow_a or []) == set(allow_b or [])


def _counts(results):
    out = {"people": len(results), ST_APPLIED: 0, ST_UNCHANGED: 0,
           ST_PARTIAL: 0, ST_REFUSED: 0, ST_FAILED: 0}
    for r in results:
        if r.get("status") in out:
            out[r["status"]] += 1
    return out


# --------------------------------------------------------------------------
# one person, one quick setup
# --------------------------------------------------------------------------

def _plan(platform, rec, preset, cfg):
    """(steps, whole_person_refusal). `steps` follow DISPLAY_ORDER; each is
    {switch, label, from, to, state, reason}, state in change | same |
    refused. A non-empty refusal means nothing is attempted at all."""
    idents = user_admin._idents(rec)
    if permissions.is_owner(cfg, idents):
        return [], ("that's the owner \u2014 a quick setup would limit your own "
                    "account inside chats. Use their switches instead")
    wants_on = bool(preset["dm"] or preset["reply"] or preset["tool"])
    if wants_on and (rec.get("follow") or "") == people.FOLLOW_BLOCKED:
        return [], "they're blocked \u2014 unblock them first"

    steps = []
    for flag, which in (("dm", PERM_DM), ("reply", PERM_REPLY),
                        ("tool", PERM_TOOLS)):
        now = user_admin.membership(cfg, which, rec)["on"]
        want = bool(preset[flag])
        step = {"switch": flag, "label": LABELS[flag], "from": _onoff(now),
                "to": _onoff(want), "state": "same", "reason": ""}
        if now != want:
            reason = user_admin.list_flag_refusal(flag, want, rec, cfg)
            step["state"] = "refused" if reason else "change"
            step["reason"] = reason
        steps.append(step)

    spec = preset["tools"]
    if preset["tool"] and spec:
        step = {"switch": "tools", "label": LABELS["tools"],
                "to": _scope_text(spec["mode"], spec["allow"]),
                "state": "same", "reason": "", "from": ""}
        try:
            perms = user_perms.get(platform, rec["user_id"])
        except user_perms.PermsUnreadable as exc:
            step["from"] = "unreadable"
            step["state"] = "refused"
            step["reason"] = (f"their tool limits can't be read ({exc}) "
                              f"\u2014 fix that first")
        else:
            cur = perms["tools"]
            step["from"] = _scope_text(cur["mode"], cur["allow"])
            if not _same_scope(cur["mode"], cur["allow"],
                               spec["mode"], spec["allow"]):
                step["state"] = "change"
        steps.append(step)
    return steps, ""


def _notes(platform, cfg, preset, steps):
    """Heads-ups that are true after the setup, not errors."""
    notes = []
    label = platform.capitalize()
    tools_target = bool(preset["tool"])
    if tools_target and not cfg.get("allow_tools"):
        notes.append(f"The {label} master tools switch is off, so tools have "
                     f"no effect yet. Run: jarvis channels-set {platform} "
                     f"allow_tools true")
    if not preset["reply"] and preset["dm"]:
        notes.append("They can open a DM but would get no answer.")
    return notes


def _resolve(platform, user_id, preset_id):
    """(rec, preset, error) -- the three things every entry point needs."""
    preset = presets.get(preset_id)
    if preset is None:
        return None, None, (f"unknown quick setup '{preset_id}' \u2014 use one "
                            f"of: {', '.join(presets.ids())}")
    rec, err = user_admin._registered(platform, user_id)
    if rec is None:
        return None, preset, err
    return rec, preset, ""


def plan_preset(platform, user_id, preset_id):
    """What this setup WOULD do for one person. Writes nothing."""
    rec, preset, err = _resolve(platform, user_id, preset_id)
    if rec is None:
        return {"ok": False, "error": err}
    cfg = channel_config.platform_config(platform)
    steps, refusal = _plan(platform, rec, preset, cfg)
    base = {"platform": platform, "user_id": str(rec["user_id"]),
            "name": _name(rec), "preset": preset["id"],
            "label": preset["label"], "risk": preset["risk"], "dry_run": True}
    if refusal:
        return dict(base, ok=False, error=refusal, status=ST_REFUSED,
                    steps=[], notes=[], will_change=0)
    ordered = sorted(steps, key=lambda s: DISPLAY_ORDER.index(s["switch"]))
    return dict(base, ok=True, error="", status=ST_PREVIEW, steps=ordered,
                notes=_notes(platform, cfg, preset, ordered),
                will_change=sum(1 for s in ordered if s["state"] == "change"))


def apply_preset(platform, user_id, preset_id):
    """Apply one quick setup to one person. Returns the plan's shape with each
    step's real outcome: state done | failed | skipped | refused | same.

    ok is False only when nothing could be attempted at all (unknown setup,
    not registered, the owner, blocked). A partly-done setup is ok=True with
    status "partial" and the steps saying which part was not done and why."""
    rec, preset, err = _resolve(platform, user_id, preset_id)
    if rec is None:
        return {"ok": False, "error": err, "status": ST_REFUSED}
    cfg = channel_config.platform_config(platform)
    steps, refusal = _plan(platform, rec, preset, cfg)
    base = {"platform": platform, "user_id": str(rec["user_id"]),
            "name": _name(rec), "preset": preset["id"],
            "label": preset["label"], "risk": preset["risk"], "dry_run": False}
    if refusal:
        return dict(base, ok=False, error=refusal, status=ST_REFUSED,
                    steps=[], notes=[])
    by = {s["switch"]: s for s in steps}

    for switch in EXEC_ORDER:
        step = by.get(switch)
        if step is None or step["state"] != "change":
            continue
        if switch == "tool" and preset["tool"]:
            lst = by.get("tools")
            if lst is not None and lst["state"] in ("refused", "failed"):
                # Never switch tools on when the list that limits them is not
                # in place: that would be "every tool" by accident.
                step["state"] = "skipped"
                step["reason"] = ("not turned on, because their tool list "
                                  "could not be set first")
                continue
        try:
            if switch == "tools":
                spec = preset["tools"]
                ok, e = user_admin.set_tools(platform, rec["user_id"],
                                             spec["mode"], spec["allow"])
            else:
                ok, e, _note = user_admin.set_flag(
                    platform, rec["user_id"], switch, bool(preset[switch]))
        except Exception as exc:  # noqa: BLE001 -- report, keep going
            ok, e = False, str(exc)
        step["state"] = "done" if ok else "failed"
        step["reason"] = "" if ok else (e or "that didn't save")

    ordered = sorted(steps, key=lambda s: DISPLAY_ORDER.index(s["switch"]))
    done = sum(1 for s in ordered if s["state"] == "done")
    bad = sum(1 for s in ordered
              if s["state"] in ("refused", "failed", "skipped"))
    if bad and done:
        status = ST_PARTIAL
    elif bad:
        status = ST_REFUSED
    elif done:
        status = ST_APPLIED
    else:
        status = ST_UNCHANGED
    return dict(base, ok=True, error="", status=status, steps=ordered,
                changed=done, notes=_notes(platform, cfg, preset, ordered))


# --------------------------------------------------------------------------
# several people (P5)
# --------------------------------------------------------------------------

def _clean_refs(refs):
    """De-duplicated [(platform, user_id)], or (None, error)."""
    out, seen = [], set()
    for ref in refs or []:
        try:
            platform, uid = ref
        except (TypeError, ValueError):
            return None, "each person must be a platform and an id"
        key = (str(platform), str(uid).strip())
        if not key[1]:
            return None, "each person must be a platform and an id"
        if key not in seen:
            seen.add(key)
            out.append(key)
    if not out:
        return None, "pick at least one person"
    if len(out) > MAX_BULK:
        return None, f"too many people at once (max {MAX_BULK})"
    return out, ""


def _flag_now(platform, rec, cfg, flag):
    if flag in user_admin._SET_FOR:
        return user_admin.membership(
            cfg, user_admin._SET_FOR[flag], rec)["on"]
    if flag == "send_dm":
        return bool(user_perms.dm_allowed(platform, rec["user_id"]))
    return (rec.get("follow") or "") == people.FOLLOW_BLOCKED  # blocked


def _bulk_flag_one(platform, user_id, flag, value):
    base = {"platform": platform, "user_id": str(user_id), "name": "",
            "flag": flag, "value": value, "reason": "", "note": ""}
    rec, err = user_admin._registered(platform, user_id)
    if rec is None:
        return dict(base, status=ST_REFUSED, reason=err)
    base["name"] = _name(rec)
    base["user_id"] = str(rec["user_id"])
    cfg = channel_config.platform_config(platform)
    if flag == "owner":
        return dict(base, status=ST_REFUSED, reason=(
            "one owner per platform \u2014 set it on that person's own page"))
    if flag == "tool" and value:
        return dict(base, status=ST_REFUSED, reason=(
            "turning tools on needs to say which tools \u2014 use a quick "
            "setup (it sets both together)"))
    if flag == "send_dm" and permissions.is_owner(cfg, user_admin._idents(rec)):
        return dict(base, status=ST_REFUSED, reason=(
            "that's the owner's own account \u2014 Jarvis reaches you with "
            "notifications, not DMs"))
    try:
        if _flag_now(platform, rec, cfg, flag) == value:
            return dict(base, status=ST_UNCHANGED)
        ok, e, note = user_admin.set_flag(platform, rec["user_id"], flag, value)
    except Exception as exc:  # noqa: BLE001
        return dict(base, status=ST_FAILED, reason=str(exc))
    if not ok:
        return dict(base, status=ST_REFUSED, reason=e or "that didn't save")
    return dict(base, status=ST_APPLIED, note=note or "")


def bulk_flag(refs, flag, value):
    """Set one switch for several people. One entry per person comes back,
    including the ones that were refused and why."""
    if flag not in BULK_FLAGS:
        return {"ok": False, "error": (f"unknown switch '{flag}' \u2014 use one "
                                       f"of: {', '.join(BULK_FLAGS)}")}
    if not isinstance(value, bool):
        return {"ok": False, "error": "value must be on or off"}
    refs, err = _clean_refs(refs)
    if refs is None:
        return {"ok": False, "error": err}
    results = [_bulk_flag_one(p, u, flag, value) for p, u in refs]
    return {"ok": True, "action": "flag", "flag": flag, "value": value,
            "results": results, "summary": _counts(results)}


def bulk_preset(refs, preset_id, preview=False):
    """Apply (or, with preview=True, only plan) one quick setup for several
    people. The setup is validated once up front, so a typo is one error and
    not N identical ones."""
    if presets.get(preset_id) is None:
        return {"ok": False, "error": (f"unknown quick setup '{preset_id}' "
                                       f"\u2014 use one of: "
                                       f"{', '.join(presets.ids())}")}
    refs, err = _clean_refs(refs)
    if refs is None:
        return {"ok": False, "error": err}
    run = plan_preset if preview else apply_preset
    results = []
    for platform, uid in refs:
        try:
            r = run(platform, uid, preset_id)
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "error": str(exc), "status": ST_FAILED}
        r.setdefault("platform", platform)
        r.setdefault("user_id", uid)
        r.setdefault("name", "")
        if not r.get("ok"):
            # Not attempted (owner, blocked, not registered): a per-person
            # reason, the same field a refused switch uses.
            r["reason"] = r.get("error", "")
            r.setdefault("steps", [])
            if r.get("status") not in (ST_FAILED,):
                r["status"] = ST_REFUSED
        results.append(r)
    summary = _counts(results)
    if preview:
        # Nothing was written, so "applied / partial" make no sense here:
        # say how many people WOULD change, how many are already there, and
        # how many cannot be changed at all.
        previewed = [r for r in results if r.get("ok")]
        summary = {
            "people": len(results),
            "would_change": sum(1 for r in previewed if r.get("will_change")),
            "unchanged": sum(1 for r in previewed if not r.get("will_change")),
            "refused": summary[ST_REFUSED], "failed": summary[ST_FAILED],
        }
    return {"ok": True, "action": "preview" if preview else "preset",
            "preset": preset_id, "results": results, "summary": summary}
