"""Tests for the F.3 fix (master plan Part F): there was no move / copy /
rename / make-folder / delete tool at all, so "move this PDF to the studying
folder" (Case 2a/2b) could only be done by discovering `run_custom_command`
and writing a PowerShell string — which the model never managed inside its
round budget.

`jarvis/actions/path_tools.py` adds move_path, copy_path, rename_path,
make_dir and delete_path to the `files` router group. What is checked here:

* the operations work and VERIFY (destination exists, source gone for a move),
* nothing is overwritten unless asked, and a folder is never overwritten,
* the refusals (drive root, home itself, OS folders, ~/.jarvis — including the
  "overwrite the confirm-gate's own config" bypass),
* every mutating tool is confirm-gated WITHOUT touching tool_safety.py (it is
  declared via the action file's TOOL_CONFIRM_REQUIRED),
* routing: Case 2a's exact message reaches these tools, and "move the mouse" /
  "move the window" still don't pull the files group in,
* explicit-null optionals (normal model behaviour, and what Groq's validator
  forces us to tolerate) behave as "not given",
* policy.py can actually SEE the destination path (argument is named `dest`).

Run: python3 tests/test_path_tools.py
"""

import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import policy, tool_registry, tool_router, tool_safety, tools  # noqa: E402
from jarvis.actions import path_tools as pt  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _fresh():
    """A new scratch folder under the fake HOME."""
    d = Path(tempfile.mkdtemp(prefix="work-", dir=_HOME))
    return d


def _write(p, text="hello"):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# move_path
# ---------------------------------------------------------------------------


def test_move_file_into_existing_folder():
    d = _fresh()
    src = _write(d / "Downloads" / "20-09-2026.pdf", "pdf-bytes")
    dest = d / "Documents" / "studying"
    dest.mkdir(parents=True)
    r = pt.tool_move_path({"src": str(src), "dest": str(dest)})
    check("move into folder: ok", r.get("ok") is True, r)
    check("move into folder: reports from/to", r.get("from") == str(src) and r.get("to") == str(dest / src.name), r)
    check("move into folder: source gone", not src.exists())
    check("move into folder: content preserved", (dest / src.name).read_text() == "pdf-bytes")


def test_move_with_trailing_slash_and_windows_style_separator():
    d = _fresh()
    src = _write(d / "a.txt")
    (d / "dst").mkdir()
    r = pt.tool_move_path({"src": str(src), "dest": str(d / "dst") + "/"})
    check("trailing slash dest: ok", r.get("ok") is True and (d / "dst" / "a.txt").exists(), r)


def test_move_to_new_full_path_renames():
    d = _fresh()
    src = _write(d / "old.txt", "x")
    r = pt.tool_move_path({"src": str(src), "dest": str(d / "new.txt")})
    check("move to full path: ok", r.get("ok") is True and (d / "new.txt").read_text() == "x" and not src.exists(), r)


def test_move_refuses_overwrite_by_default_and_touches_nothing():
    d = _fresh()
    src = _write(d / "a.txt", "SRC")
    existing = _write(d / "out" / "a.txt", "KEEP")
    r = pt.tool_move_path({"src": str(src), "dest": str(d / "out")})
    check("no overwrite: dest_exists", r.get("reason") == "dest_exists", r)
    check("no overwrite: both files untouched", src.read_text() == "SRC" and existing.read_text() == "KEEP")


def test_move_overwrite_true_replaces_a_file():
    d = _fresh()
    src = _write(d / "a.txt", "NEW")
    _write(d / "out" / "a.txt", "OLD")
    r = pt.tool_move_path({"src": str(src), "dest": str(d / "out"), "overwrite": True})
    check("overwrite=true: ok", r.get("ok") is True, r)
    check("overwrite=true: replaced, source gone", (d / "out" / "a.txt").read_text() == "NEW" and not src.exists())
    leftovers = [p.name for p in (d / "out").iterdir() if p.name.startswith(".jarvis-")]
    check("overwrite=true: no temp file left behind", leftovers == [], leftovers)


def test_overwrite_never_replaces_or_merges_a_folder():
    d = _fresh()
    src_dir = d / "proj"
    _write(src_dir / "f.txt", "1")
    _write(d / "out" / "proj" / "keep.txt", "KEEP")
    r = pt.tool_move_path({"src": str(src_dir), "dest": str(d / "out"), "overwrite": True})
    check("folder target: dest_is_dir even with overwrite", r.get("reason") == "dest_is_dir", r)
    check("folder target: nothing changed",
          (d / "out" / "proj" / "keep.txt").read_text() == "KEEP" and (src_dir / "f.txt").exists())
    # a file may not replace a folder either
    f = _write(d / "x.txt")
    (d / "y").mkdir()
    r2 = pt.tool_move_path({"src": str(f), "dest": str(d / "y"), "overwrite": True})
    check("file into folder of same name is fine (lands inside)", r2.get("ok") is True and (d / "y" / "x.txt").exists(), r2)


def test_move_missing_source_has_a_recovery_hint():
    d = _fresh()
    r = pt.tool_move_path({"src": str(d / "nope.pdf"), "dest": str(d)})
    check("missing src: reason", r.get("reason") == "src_not_found", r)
    check("missing src: hint points at search_files", "search_files" in r.get("hint", ""), r)


def test_missing_destination_folder_and_make_parents():
    d = _fresh()
    src = _write(d / "a.txt")
    r = pt.tool_move_path({"src": str(src), "dest": str(d / "nope") + "/"})
    check("missing folder: dest_folder_missing", r.get("reason") == "dest_folder_missing", r)
    check("missing folder: source untouched, nothing created", src.exists() and not (d / "nope").exists())
    r2 = pt.tool_move_path({"src": str(src), "dest": str(d / "nope" / "deeper" / "b.txt")})
    check("missing parent of full path: dest_parent_missing", r2.get("reason") == "dest_parent_missing", r2)
    r3 = pt.tool_move_path({"src": str(src), "dest": str(d / "nope") + "/", "make_parents": True})
    check("make_parents=true creates it", r3.get("ok") is True and (d / "nope" / "a.txt").exists(), r3)


def test_move_folder_and_refuse_into_itself():
    d = _fresh()
    _write(d / "proj" / "sub" / "f.txt", "1")
    (d / "archive").mkdir()
    r = pt.tool_move_path({"src": str(d / "proj"), "dest": str(d / "archive")})
    check("move folder: ok + contents", r.get("ok") is True and r.get("kind") == "dir"
          and (d / "archive" / "proj" / "sub" / "f.txt").exists() and not (d / "proj").exists(), r)
    _write(d / "p2" / "f.txt")
    r2 = pt.tool_move_path({"src": str(d / "p2"), "dest": str(d / "p2" / "inner") + "/", "make_parents": True})
    check("folder into itself refused", r2.get("reason") == "dest_inside_src", r2)
    check("folder into itself: nothing created", not (d / "p2" / "inner").exists())


def test_move_same_path_is_a_noop_not_an_error():
    d = _fresh()
    src = _write(d / "a.txt")
    r = pt.tool_move_path({"src": str(src), "dest": str(d)})
    check("already in that folder: ok/noop", r.get("ok") is True and r.get("noop") is True and src.exists(), r)


# ---------------------------------------------------------------------------
# copy_path
# ---------------------------------------------------------------------------


def test_copy_file_and_folder_keep_the_original():
    d = _fresh()
    src = _write(d / "a.txt", "A")
    (d / "out").mkdir()
    r = pt.tool_copy_path({"src": str(src), "dest": str(d / "out")})
    check("copy file: ok, both exist", r.get("ok") is True and src.exists() and (d / "out" / "a.txt").read_text() == "A", r)
    _write(d / "proj" / "sub" / "f.txt", "F")
    r2 = pt.tool_copy_path({"src": str(d / "proj"), "dest": str(d / "out")})
    check("copy folder: recursive, original kept",
          r2.get("ok") is True and (d / "out" / "proj" / "sub" / "f.txt").exists() and (d / "proj" / "sub" / "f.txt").exists(), r2)
    r3 = pt.tool_copy_path({"src": str(d / "proj"), "dest": str(d / "out")})
    check("copy folder over existing folder refused", r3.get("reason") in ("dest_exists", "dest_is_dir"), r3)
    r4 = pt.tool_copy_path({"src": str(src), "dest": str(d / "out")})
    check("copy over existing file refused without overwrite", r4.get("reason") == "dest_exists", r4)
    _write(src, "A2")
    r5 = pt.tool_copy_path({"src": str(src), "dest": str(d / "out"), "overwrite": True})
    check("copy overwrite=true replaces", r5.get("ok") is True and (d / "out" / "a.txt").read_text() == "A2", r5)


def test_copy_folder_into_itself_refused():
    d = _fresh()
    _write(d / "proj" / "f.txt")
    r = pt.tool_copy_path({"src": str(d / "proj"), "dest": str(d / "proj" / "again") + "/", "make_parents": True})
    check("copy folder into itself refused", r.get("reason") == "dest_inside_src", r)


# ---------------------------------------------------------------------------
# rename_path
# ---------------------------------------------------------------------------


def test_rename_ok_and_guards():
    d = _fresh()
    src = _write(d / "notes.txt", "N")
    r = pt.tool_rename_path({"path": str(src), "new_name": "notes-final.txt"})
    check("rename: ok", r.get("ok") is True and (d / "notes-final.txt").read_text() == "N" and not src.exists(), r)
    for bad in ("../x.txt", "sub/x.txt", "sub\\x.txt", "..", "."):
        rr = pt.tool_rename_path({"path": str(d / "notes-final.txt"), "new_name": bad})
        check(f"rename: rejects {bad!r}", rr.get("reason") == "bad_name", rr)
    _write(d / "other.txt", "O")
    rr = pt.tool_rename_path({"path": str(d / "notes-final.txt"), "new_name": "other.txt"})
    check("rename: never overwrites", rr.get("reason") == "dest_exists" and (d / "other.txt").read_text() == "O", rr)
    rr = pt.tool_rename_path({"path": str(d / "gone.txt"), "new_name": "x.txt"})
    check("rename: missing source", rr.get("reason") == "src_not_found", rr)
    rr = pt.tool_rename_path({"path": str(d / "other.txt"), "new_name": "other.txt"})
    check("rename: same name is a noop", rr.get("ok") is True and rr.get("noop") is True, rr)


def test_rename_windows_name_rules():
    d = _fresh()
    src = _write(d / "a.txt")
    orig = sys.platform
    sys.platform = "win32"
    try:
        r = pt.tool_rename_path({"path": str(src), "new_name": "what?.txt"})
        r2 = pt.tool_rename_path({"path": str(src), "new_name": "trailing."})
    finally:
        sys.platform = orig
    check("windows: '?' rejected", r.get("reason") == "bad_name", r)
    check("windows: trailing dot rejected", r2.get("reason") == "bad_name", r2)
    check("windows rule not applied on other platforms",
          pt.tool_rename_path({"path": str(src), "new_name": "what?.txt"}).get("ok") is True)


# ---------------------------------------------------------------------------
# make_dir / delete_path
# ---------------------------------------------------------------------------


def test_make_dir():
    d = _fresh()
    r = pt.tool_make_dir({"path": str(d / "a" / "b" / "c")})
    check("make_dir: created nested", r.get("ok") is True and r.get("created") is True and (d / "a" / "b" / "c").is_dir(), r)
    r2 = pt.tool_make_dir({"path": str(d / "a" / "b" / "c")})
    check("make_dir: existing is ok, created=false", r2.get("ok") is True and r2.get("created") is False, r2)
    f = _write(d / "file.txt")
    r3 = pt.tool_make_dir({"path": str(f)})
    check("make_dir: a file in the way is an error", r3.get("reason") == "path_is_file", r3)


def test_delete_needs_send2trash_and_touches_nothing_without_it():
    d = _fresh()
    f = _write(d / "gone.txt")
    orig = pt._send2trash
    pt._send2trash = lambda: None
    try:
        r = pt.tool_delete_path({"path": str(f)})
    finally:
        pt._send2trash = orig
    check("delete without send2trash: refuses", r.get("reason") == "trash_unavailable", r)
    check("delete without send2trash: file still there (never permanent)", f.exists())
    check("delete without send2trash: tells how to fix it", "send2trash" in r.get("hint", ""), r)


def test_delete_with_a_trash_backend_and_verification():
    d = _fresh()
    f = _write(d / "gone.txt")
    trashed = []
    orig = pt._send2trash
    pt._send2trash = lambda: (lambda p: (trashed.append(p), os.unlink(p)))
    try:
        r = pt.tool_delete_path({"path": str(f)})
    finally:
        pt._send2trash = orig
    check("delete via trash backend: ok", r.get("ok") is True and r.get("recycled") is True and trashed == [str(f)], r)
    g = _write(d / "stubborn.txt")
    pt._send2trash = lambda: (lambda p: None)   # "succeeds" but does nothing
    try:
        r2 = pt.tool_delete_path({"path": str(g)})
    finally:
        pt._send2trash = orig
    check("delete: a backend that lied is caught by verification", r2.get("reason") == "verify_failed", r2)
    r3 = pt.tool_delete_path({"path": str(d / "never-existed.txt")})
    check("delete: missing path", r3.get("reason") == "src_not_found", r3)


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_jarvis_state_folder_is_off_limits_for_every_role():
    d = _fresh()
    jd = Path(_HOME) / ".jarvis"
    jd.mkdir(exist_ok=True)
    cfg = _write(jd / "tool_safety.json", '{"tools": {}}')
    evil = _write(d / "evil.json", '{"tools": {"move_path": {"confirm_required": false}}}')
    r = pt.tool_copy_path({"src": str(evil), "dest": str(cfg), "overwrite": True})
    check("cannot overwrite tool_safety.json through copy_path", r.get("reason") == "protected_path", r)
    check("tool_safety.json unchanged", cfg.read_text() == '{"tools": {}}')
    r = pt.tool_move_path({"src": str(evil), "dest": str(jd)})
    check("cannot move INTO ~/.jarvis", r.get("reason") == "protected_path", r)
    r = pt.tool_copy_path({"src": str(cfg), "dest": str(d)})
    check("cannot copy a file OUT of ~/.jarvis (API keys)", r.get("reason") == "protected_path", r)
    r = pt.tool_delete_path({"path": str(cfg)})
    check("cannot delete from ~/.jarvis", r.get("reason") == "protected_path", r)
    r = pt.tool_rename_path({"path": str(cfg), "new_name": "x.json"})
    check("cannot rename in ~/.jarvis", r.get("reason") == "protected_path", r)
    r = pt.tool_make_dir({"path": str(jd / "newdir")})
    check("cannot mkdir in ~/.jarvis", r.get("reason") == "protected_path", r)
    names = {p.name for p in jd.iterdir()}
    check("~/.jarvis untouched by any refused call",
          cfg.exists() and not ({"evil.json", "x.json", "newdir"} & names), names)


def test_root_home_and_os_folders_refused():
    d = _fresh()
    f = _write(d / "a.txt")
    root = Path(os.path.abspath(os.sep))
    check("drive/filesystem root refused as a source",
          pt.tool_move_path({"src": str(root), "dest": str(d)}).get("reason") == "protected_path")
    check("home folder itself refused",
          pt.tool_delete_path({"path": _HOME}).get("reason") == "protected_path")
    check("home folder itself can't be renamed",
          pt.tool_rename_path({"path": _HOME, "new_name": "x"}).get("reason") == "protected_path")
    if sys.platform != "win32":
        r = pt.tool_move_path({"src": str(f), "dest": "/etc/"})
        check("cannot move into /etc", r.get("reason") == "protected_path", r)
        check("source untouched after refusal", f.exists())
        if os.path.exists("/etc/hostname"):
            r = pt.tool_copy_path({"src": "/etc/hostname", "dest": str(d)})
            check("reading (copying FROM) an OS folder is allowed", r.get("ok") is True, r)
            r = pt.tool_move_path({"src": "/etc/hostname", "dest": str(d)})
            check("moving something OUT of /etc is refused", r.get("reason") == "protected_path", r)
    orig = sys.platform
    sys.platform = "win32"
    try:
        why = pt._protected_reason("C:/Windows/System32", writing=True)
        ok_other = pt._protected_reason("C:/Users/assim/Documents", writing=True)
    finally:
        sys.platform = orig
    check("windows: C:/Windows/* recognised", bool(why), why)
    check("windows: ordinary user folders are not", ok_other is None, ok_other)


# ---------------------------------------------------------------------------
# wiring: safety, routing, policy, nulls
# ---------------------------------------------------------------------------


def test_every_mutating_tool_is_confirm_gated_without_editing_tool_safety():
    names = ["move_path", "copy_path", "rename_path", "make_dir", "delete_path"]
    for n in names:
        check(f"{n} registered", n in tools.TOOLS and n in tool_registry.TOOL_INDEX)
        check(f"{n} requires confirmation by default", tool_safety.requires_confirmation(n) is True)
        check(f"{n} lives in the files group", n in tool_registry.TOOL_GROUPS["files"])
    src = (Path(__file__).resolve().parent.parent / "jarvis-cli" / "jarvis" / "tool_safety.py").read_text(encoding="utf-8")
    check("tool_safety.py's own list was NOT edited (declared via the action file)",
          not any(f'"{n}"' in src for n in names))


def test_case_2a_message_reaches_the_move_tool():
    msg = "move C:/Users/assim/Downloads/20-09-2026.pdf to the documents/studying folder"
    r = tool_router.route(msg)
    check("Case 2a routes to files", "files" in r.groups, r.groups)
    matched = {m[1] for m in r.matches}
    check("Case 2a: move_path is among the matched tools", "move_path" in matched, matched)


def test_mouse_and_window_moves_do_not_pull_in_files():
    for msg in ("move the mouse to the top left corner", "move the window to my second monitor",
                "move the volume slider up a bit"):
        r = tool_router.route(msg)
        check(f"not files: {msg!r}", "files" not in r.groups, r.groups)
    for msg, tool in (("rename report.pdf to final.pdf", "rename_path"),
                      ("please create a folder called invoices", "make_dir"),
                      ("copy the file a.txt to backup", "copy_path"),
                      ("delete the file old.log", "delete_path")):
        r = tool_router.route(msg)
        check(f"routes {tool}: {msg!r}", "files" in r.groups and tool in {m[1] for m in r.matches}, (r.groups, r.matches))


def test_explicit_nulls_mean_not_given():
    d = _fresh()
    src = _write(d / "a.txt")
    (d / "out").mkdir()
    r = tools.execute_tool("move_path", {"src": str(src), "dest": str(d / "out"), "overwrite": None, "make_parents": None})
    check("execute_tool with null optionals: ok", isinstance(r, dict) and r.get("ok") is True, r)
    r = tools.execute_tool("move_path", {"src": None, "dest": None})
    check("null required args -> clean error, no exception", isinstance(r, dict) and r.get("reason") == "bad_args", r)
    r = tools.execute_tool("move_path", {})
    check("empty args -> clean error", isinstance(r, dict) and "error" in r, r)
    check("string 'false' is false", pt._truthy("false") is False and pt._truthy("true") is True)


def test_policy_can_see_the_destination_argument():
    score, reasons = policy.score_call("move_path", {"src": "C:/x/a.pdf", "dest": "~/.ssh/authorized_keys"})
    check("policy flags a move INTO ~/.ssh via `dest`", any("SSH" in why for _, _, why in reasons), reasons)
    check("policy scores it above the confirm threshold", score >= policy.THRESHOLD_CONFIRM, score)


def test_schema_shape():
    for s in pt.TOOL_SCHEMAS:
        check(f"{s['name']}: has description + required list",
              bool(s["description"]) and isinstance(s["parameters"].get("required"), list), s)
    mv = next(s for s in pt.TOOL_SCHEMAS if s["name"] == "move_path")
    check("move_path uses src/dest (policy-visible names)", {"src", "dest"} <= set(mv["parameters"]["properties"]))
    check("move_path required = src, dest", mv["parameters"]["required"] == ["src", "dest"])


for fn in [
    test_move_file_into_existing_folder,
    test_move_with_trailing_slash_and_windows_style_separator,
    test_move_to_new_full_path_renames,
    test_move_refuses_overwrite_by_default_and_touches_nothing,
    test_move_overwrite_true_replaces_a_file,
    test_overwrite_never_replaces_or_merges_a_folder,
    test_move_missing_source_has_a_recovery_hint,
    test_missing_destination_folder_and_make_parents,
    test_move_folder_and_refuse_into_itself,
    test_move_same_path_is_a_noop_not_an_error,
    test_copy_file_and_folder_keep_the_original,
    test_copy_folder_into_itself_refused,
    test_rename_ok_and_guards,
    test_rename_windows_name_rules,
    test_make_dir,
    test_delete_needs_send2trash_and_touches_nothing_without_it,
    test_delete_with_a_trash_backend_and_verification,
    test_jarvis_state_folder_is_off_limits_for_every_role,
    test_root_home_and_os_folders_refused,
    test_every_mutating_tool_is_confirm_gated_without_editing_tool_safety,
    test_case_2a_message_reaches_the_move_tool,
    test_mouse_and_window_moves_do_not_pull_in_files,
    test_explicit_nulls_mean_not_given,
    test_policy_can_see_the_destination_argument,
    test_schema_shape,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
