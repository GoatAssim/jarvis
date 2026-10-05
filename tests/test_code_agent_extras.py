"""code_agent runs are saved as replay extras (L.47b).

Run: python3 tests/test_code_agent_extras.py

Pins: the extra exists and has dev_agent's shape; `root` becomes `projectDir`;
file CONTENT (write_file content, edit_file old_str/new_str) never reaches the
saved copy; the live-only `listing` is not saved; an early failure that has no
`steps` saves nothing; dev_agent's own extra is unchanged.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))
from jarvis import thread_extras  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


SECRET = "API_KEY=sk-live-DO-NOT-SAVE"
steps = [
    {"job_id": "c1", "seq": 0, "phase": "plan", "status": "start", "task": "fix it", "root": "/r"},
    {"job_id": "c1", "seq": 1, "phase": "step", "status": "start", "tool": "list_dir", "arguments": {"path": "."}},
    {"job_id": "c1", "seq": 2, "phase": "step", "status": "ok", "tool": "list_dir", "outcome": "3 entries", "listing": ["a.py"]},
    {"job_id": "c1", "seq": 3, "phase": "step", "status": "start", "tool": "write_file",
     "arguments": {"path": "new.py", "content": SECRET * 50}},
    {"job_id": "c1", "seq": 4, "phase": "step", "status": "start", "tool": "edit_file",
     "arguments": {"path": "a.py", "old_str": SECRET, "new_str": SECRET}},
    {"job_id": "c1", "seq": 5, "phase": "step", "status": "start", "tool": "run_shell",
     "arguments": {"command": "x" * 1000}},
    {"job_id": "c1", "seq": 6, "phase": "done", "status": "ok", "tool_calls": 4},
]
runs = [{"name": "code_agent", "arguments": {"task": "fix it"},
         "result": {"ok": True, "job_id": "c1", "root": "/r", "steps": steps, "summary": "done"}}]
extras = thread_extras.extras_from_runs(runs)
dev = [x for x in extras if x["type"] == "devAgent"]
check("one devAgent extra for a code_agent run", len(dev) == 1, extras)
d = dev[0]["data"] if dev else {}
check("jobId / ok carried", d.get("jobId") == "c1" and d.get("ok") is True, d)
check("root is saved as projectDir", d.get("projectDir") == "/r", d)
saved = d.get("steps", [])
check("every step is kept", len(saved) == len(steps), len(saved))
flat = repr(saved)
check("file content never saved", "DO-NOT-SAVE" not in flat)
check("path survives so the panel can name the file", saved[3]["arguments"] == {"path": "new.py"}, saved[3])
check("long command is clipped", len(saved[5]["arguments"]["command"]) == 300)
check("live-only listing is not saved", all("listing" not in e for e in saved))
check("the original step dicts are not mutated", "content" in steps[3]["arguments"] and "listing" in steps[2])

check("no steps (early failure) saves nothing",
      thread_extras.extras_from_runs([{"name": "code_agent", "arguments": {}, "result": {"error": "task is required"}}]) == [])

dev_runs = [{"name": "dev_agent", "arguments": {}, "result": {
    "ok": True, "job_id": "d1", "project_dir": "/p", "steps": [{"phase": "plan", "status": "start", "preview": "kept"}]}}]
dx = thread_extras.extras_from_runs(dev_runs)
check("dev_agent extra is unchanged (project_dir, verbatim steps)",
      dx and dx[0]["data"]["projectDir"] == "/p" and dx[0]["data"]["steps"][0].get("preview") == "kept", dx)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
