"""Tests for doctor.check_saved_commands() (D-I9, master plan Part I).

Before I-B2, a saved command could be given a name cli.py's own dispatcher
would later shadow with a built-in — and, separately from that bug, there
was no way to ask "does my current commands.json actually have this
problem right now" other than reading the warning cli.py already prints on
every CLI invocation (see cli.py's own collision check, just above where
RESERVED_NAMES is imported) — a warning that's invisible to anyone working
only through the web UI, and which itself relied on a materially incomplete
reserved-name list before I-B2 fixed reserved_names.py. `jarvis doctor`
exists specifically to be the place all of this is checked in one
dedicated, JSON-able, non-destructive pass — this is that pass's own test.

Run with `python3 tests/test_doctor_saved_commands.py`. No API key or
network needed.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["USERPROFILE"] = os.environ["HOME"]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import commands_config, doctor  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _write_commands(commands):
    commands_config.ensure_config()
    commands_config.save_commands_dict(commands)


def test_no_saved_commands_is_ok():
    _write_commands({})
    results = doctor.check_saved_commands()
    check("exactly one result", len(results) == 1, results)
    check("status is ok", results[0].status == doctor.OK, results[0].to_dict())
    check("mentions 0 saved", "0 saved" in results[0].detail, results[0].detail)


def test_ordinary_names_are_ok():
    _write_commands({
        "my-standup-notes": {"script": "echo hi"},
        "deploy-staging": {"script": "echo hi"},
    })
    results = doctor.check_saved_commands()
    check("status is ok when nothing is shadowed", results[0].status == doctor.OK, results[0].to_dict())
    check("mentions the count", "2 saved" in results[0].detail, results[0].detail)


def test_shadowed_names_are_warned_with_a_nondestructive_fix():
    # think, backlog, doctor: all real examples from the I-B2 audit of
    # names commands_config used to accept before reserved_names.py existed.
    _write_commands({
        "think": {"script": "echo hi"},
        "backlog": {"script": "echo hi"},
        "doctor": {"script": "echo hi"},
        "a-fine-name": {"script": "echo hi"},
    })
    results = doctor.check_saved_commands()
    check("exactly one result", len(results) == 1, results)
    r = results[0]
    check("status is warn, not fail (never destructive, never fatal)", r.status == doctor.WARN, r.to_dict())
    check("names all three shadowed commands", all(n in r.detail for n in ("think", "backlog", "doctor")), r.detail)
    check("does not name the ordinary command as shadowed", "a-fine-name" not in r.detail, r.detail)
    check("fix is a suggestion to rename, not an action doctor takes itself",
          "Rename" in r.fix and "commands-check-name" in r.fix, r.fix)
    check("check id is stable for scripting/JSON consumers", r.id == "commands.shadowed", r.id)


def test_this_check_is_read_only_never_touches_the_file():
    _write_commands({"think": {"script": "echo hi"}})
    before = commands_config.load_commands_dict()
    doctor.check_saved_commands()
    after = commands_config.load_commands_dict()
    check("commands.json is byte-identical after running the check", before == after, (before, after))


def test_included_in_a_full_run_and_json_serializable():
    _write_commands({"think": {"script": "echo hi"}})
    report = doctor.run(only=["commands"])
    check("report's overall status reflects the warning", report["overall"] == doctor.WARN, report["overall"])
    ids = [c["id"] for c in report["checks"]]
    check("commands.shadowed is in the report", "commands.shadowed" in ids, ids)
    # Round-trips through JSON cleanly — this is what --json and any future
    # web-surfaced health check would actually consume.
    json.dumps(report)
    check("report is JSON-serializable", True)


def test_a_corrupt_commands_file_warns_instead_of_crashing():
    commands_config.ensure_config()
    commands_config.CONFIG_FILE.write_text("{not json", encoding="utf-8")
    results = doctor.check_saved_commands()
    check("exactly one result even for a corrupt file", len(results) == 1, results)
    # load_commands_dict() already treats unparseable JSON as {} (empty),
    # not an error — so this should read as "0 saved", same as
    # test_no_saved_commands_is_ok, not a separate failure path.
    check("corrupt file reads as zero saved commands, not a crash",
          results[0].status == doctor.OK and "0 saved" in results[0].detail, results[0].to_dict())


for fn in [
    test_no_saved_commands_is_ok,
    test_ordinary_names_are_ok,
    test_shadowed_names_are_warned_with_a_nondestructive_fix,
    test_this_check_is_read_only_never_touches_the_file,
    test_included_in_a_full_run_and_json_serializable,
    test_a_corrupt_commands_file_warns_instead_of_crashing,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
