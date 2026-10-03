"""Tool Manager (L.25) -- the per-tool safeguard switches and the catalogue the
manager reads.

Covers:
  1. tool_safety knows the third flag, approval_summary: defaults to what
     job_risk already flags, round-trips through set_flag, rejects unknown keys.
  2. The switch changes what an approval says AND what it covers: switched OFF
     for a flagged tool it leaves the summary and stops authorising the call;
     switched ON for an unflagged tool it joins the summary under "marked".
  3. AI overview and confirmation are independent switches (the executor asks
     when EITHER is on), so the manager can show both without implying one.
  4. tools_list_payload() carries approval_summary, group and file for every
     tool, and the CLI/server accept the new key.
  5. No model-facing tool can write tool_safety.json.
  6. The editor's validator (custom_tools_store.validate_source) rejects what
     the loader (tool_loader._validate) rejects, so "Valid" in the Tool Manager
     means the tool will actually load.

No network, no live model. Runs against a throwaway HOME.

Run: python3 tests/test_tool_manager_flags.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import job_risk, policy, tool_safety  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def reset():
    tool_safety.CONFIG_FILE.write_text(json.dumps({"tools": {}}) + "\n", encoding="utf-8")


# --- 1. the flag itself ------------------------------------------------------
reset()
check("approval_summary is a valid safety key", "approval_summary" in tool_safety.VALID_KEYS)
check("flagged tool defaults to ON", tool_safety.get_flags("click")["approval_summary"] is True)
check("shell tool defaults to ON", tool_safety.in_approval_summary("run_shell"))
check("unflagged tool defaults to OFF", tool_safety.get_flags("get_battery")["approval_summary"] is False)
flags = tool_safety.set_flag("get_battery", "approval_summary", True)
check("set_flag round-trips ON", flags["approval_summary"] is True and tool_safety.in_approval_summary("get_battery"))
check("setting one flag leaves the other two alone",
      flags["confirm_required"] is False and flags["ai_review"] is False, flags)
tool_safety.set_flag("click", "approval_summary", False)
check("set_flag round-trips OFF on a default-ON tool", tool_safety.get_flags("click")["approval_summary"] is False)
check("the other flags of that tool are untouched",
      tool_safety.get_flags("click")["confirm_required"] is True)
try:
    tool_safety.set_flag("click", "enabled", True)
    check("unknown key rejected", False, "no error")
except ValueError:
    check("unknown key rejected", True)
check("all_flags carries the third flag",
      set(tool_safety.all_flags(["click"])["click"]) == {"confirm_required", "ai_review", "approval_summary"})
(tool_safety.CONFIG_FILE).write_text("{ not json", encoding="utf-8")
check("a corrupt file falls back to built-in defaults, not an error",
      tool_safety.get_flags("click")["approval_summary"] is True)

# --- 2. what the switch does to approvals -----------------------------------
reset()
action = {"type": "tool", "tool": "click", "args": {}}
check("baseline: click is summarised as desktop control", job_risk.assess(action)["kinds"] == ["desktop"])
tool_safety.set_flag("click", "approval_summary", False)
check("OFF: the summary no longer mentions it", job_risk.assess(action)["kinds"] == [])
check("OFF: kind_of is None", job_risk.kind_of("click") is None)
check("OFF: it does not sneak back in through the desktop group's read cover",
      job_risk.covering_kind("click") is None)
check("OFF for one tool leaves its siblings flagged", job_risk.kind_of("type_text") == "desktop")
check("OFF: a read-only desktop probe is still covered by an approved desktop job",
      job_risk.covering_kind("read_screen") == "desktop")
d_off = policy.decide("click", {}, context=policy.CTX_SCHEDULED, approved_kinds=["desktop"])
check("OFF: an approved desktop job is no longer allowed to click", d_off["decision"] != policy.ALLOW, d_off)
tool_safety.set_flag("click", "approval_summary", True)
d_on = policy.decide("click", {}, context=policy.CTX_SCHEDULED, approved_kinds=["desktop"])
check("back ON: the same approval covers it again", d_on["decision"] == policy.ALLOW, d_on)

reset()
check("unflagged tool is not in the summary", job_risk.kind_of("get_battery") is None)
tool_safety.set_flag("get_battery", "approval_summary", True)
a = job_risk.assess({"type": "tool", "tool": "get_battery", "args": {}})
check("ON for an unflagged tool: it joins the summary as 'marked'", a["kinds"] == ["marked"], a)
check("the marked line names the tool", "get_battery" in a["lines"][0], a["lines"])
check("marked is a real approvable kind", "marked" in job_risk.KINDS and job_risk.phrase("marked"))
d = policy.decide("get_battery", {}, context=policy.CTX_SCHEDULED, approved_kinds=["marked"])
check("an approval of 'marked' covers a marked tool", d["decision"] == policy.ALLOW, d)
check("marked kinds survive authorized_kinds()",
      job_risk.authorized_kinds({"approved_at": "x", "approved_kinds": ["marked"]}) == ["marked"])
reset()

# --- 3. AI overview and confirmation are independent ------------------------
reset()
tool_safety.set_flag("get_battery", "ai_review", True)
f = tool_safety.get_flags("get_battery")
check("AI overview on, confirmation off is a representable state", f["ai_review"] and not f["confirm_required"], f)
src = (ROOT / "jarvis-cli" / "jarvis" / "ai_client.py").read_text(encoding="utf-8")
check("the executor prompts when EITHER is on (the manager's copy depends on this)",
      re.search(r"requires_confirmation\(name\)[\s\S]{0,200}requires_ai_review\(name\)", src) is not None)
reset()

# --- 4. the catalogue the manager reads -------------------------------------
payload = system_tools.tools_list_payload()
check("payload is non-empty", len(payload) > 50, len(payload))
keys_ok = all({"approval_summary", "group", "file", "source", "confirm_required", "ai_review"} <= set(i) for i in payload)
check("every item carries approval_summary, group, file, source and both older flags", keys_ok)
by = {i["name"]: i for i in payload}
check("power_action reports the file it came from", by["power_action"]["file"] == "power_tools.py", by["power_action"])
check("a hand-wired built-in has no file", by["write_file"]["file"] == "")
check("group is filled for a routed tool", by["click"]["group"] == "desktop", by["click"]["group"])
check("the payload's approval_summary matches tool_safety",
      by["click"]["approval_summary"] == tool_safety.get_flags("click")["approval_summary"])
check("every item carries the built-in defaults, unaffected by an override",
      all(set(i.get("defaults") or {}) == {"confirm_required", "ai_review", "approval_summary"} for i in payload))
tool_safety.set_flag("click", "approval_summary", False)
click_now = {i["name"]: i for i in system_tools.tools_list_payload()}["click"]
check("an override changes the live flag but not its default",
      click_now["approval_summary"] is False and click_now["defaults"]["approval_summary"] is True, click_now)
reset()
cli_src = (ROOT / "jarvis-cli" / "jarvis" / "cli.py").read_text(encoding="utf-8")
check("the CLI usage line names approval_summary", "confirm_required|ai_review|approval_summary" in cli_src)
srv = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
check("the server allows the approval_summary key", '"approval_summary"].includes(key)' in srv)

# --- 5. the model cannot write the switches ---------------------------------
model_facing = [n for n in system_tools.TOOLS]
writers = []
for name in model_facing:
    fn = system_tools.TOOLS[name]
    try:
        text = Path(sys.modules[fn.__module__].__file__).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        continue
    if re.search(r"tool_safety\.(set_flag|_save)\b", text):
        writers.append(f"{name} ({fn.__module__})")
check("no model-facing tool module calls tool_safety.set_flag/_save",
      not writers, sorted(set(writers)))

# --- 6. editor validator == loader validator ---------------------------------
from jarvis import custom_tools_store as cts  # noqa: E402
from jarvis import tool_loader  # noqa: E402

GOOD = cts.template_source("minimal")
check("the Minimal template validates", cts.validate_source(GOOD, "t_good")["ok"])


def parity(label, source, expect_text):
    """Both validators must reject `source`; the editor must say why."""
    mine = cts.validate_source(source, "t_parity")
    import types
    mod = types.ModuleType("t_parity")
    mod.__dict__["__file__"] = "t_parity.py"
    exec(compile(source, "t_parity", "exec"), mod.__dict__)
    theirs = tool_loader._validate(mod, "t_parity.py", lambda *_a, **_k: None)
    check(label + ": the loader rejects it", not theirs.valid, getattr(theirs, "error", ""))
    check(label + ": so does the editor", not mine["ok"], mine)
    check(label + ": with a useful reason", expect_text in (mine.get("error") or ""), mine.get("error"))


parity("keywords for a tool that isn't in the file",
       GOOD.replace('TOOL_KEYWORDS = {"hello"', 'TOOL_KEYWORDS = {"nope"'), "TOOL_KEYWORDS has entries")
parity("keywords that aren't a dict", GOOD.replace('TOOL_KEYWORDS = {"hello": {"say hello": 10, "greet": 8}}', 'TOOL_KEYWORDS = ["x"]'),
       "TOOL_KEYWORDS must be a dict")
parity("result specs that aren't a dict", GOOD + '\nTOOL_RESULT_SPECS = ["x"]\n', "TOOL_RESULT_SPECS must be a dict")
check("the keyword rejection carries a hint the editor can show",
      "hint" in cts.validate_source(GOOD.replace('TOOL_KEYWORDS = {"hello"', 'TOOL_KEYWORDS = {"nope"'), "t_h"))
check("a file with NO keywords still validates (the loader derives fallbacks)",
      cts.validate_source(GOOD.replace('TOOL_KEYWORDS = {"hello": {"say hello": 10, "greet": 8}}', ''), "t_nokw")["ok"])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
