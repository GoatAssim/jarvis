"""Tests for jarvis/dev_agent_sandbox.py — path containment/jailing
(§4.3 of the §3.6 plan, §10's sandbox test spec):

- resolve_within accepts a plain nested relative path.
- resolve_within rejects a ../escape.txt traversal.
- resolve_within rejects an absolute path outside the project dir.
- resolve_within rejects a symlink planted inside the project dir that
  points outside it.

No test framework dependency — plain asserts, runnable directly:

    python3 tests/test_dev_agent_sandbox.py
"""

import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import dev_agent_sandbox  # noqa: E402

_tmp_roots = []  # cleaned up at the end of the run


def _tmp_project_dir():
    d = Path(tempfile.mkdtemp(prefix="dev_agent_sandbox_test_"))
    _tmp_roots.append(d)
    project_dir = d / "project"
    project_dir.mkdir()
    return project_dir


def test_accepts_plain_nested_relative_path():
    project_dir = _tmp_project_dir()
    resolved = dev_agent_sandbox.resolve_within(project_dir, "templates/index.html")
    assert resolved == (project_dir / "templates" / "index.html").resolve()
    assert project_dir.resolve() in resolved.parents


def test_accepts_a_top_level_file():
    project_dir = _tmp_project_dir()
    resolved = dev_agent_sandbox.resolve_within(project_dir, "app.py")
    assert resolved == (project_dir / "app.py").resolve()


def test_rejects_empty_or_blank_relative_path():
    project_dir = _tmp_project_dir()
    for bad in ("", "   ", None):
        try:
            dev_agent_sandbox.resolve_within(project_dir, bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_rejects_parent_traversal():
    project_dir = _tmp_project_dir()
    try:
        dev_agent_sandbox.resolve_within(project_dir, "../escape.txt")
        assert False, "expected ValueError for a ../ traversal"
    except ValueError:
        pass


def test_rejects_deeper_parent_traversal_that_still_lands_outside():
    project_dir = _tmp_project_dir()
    try:
        dev_agent_sandbox.resolve_within(project_dir, "nested/../../escape.txt")
        assert False, "expected ValueError for a nested ../.. traversal"
    except ValueError:
        pass


def test_accepts_a_traversal_that_still_lands_back_inside():
    # "a/../b" resolves to just "b", still inside the project dir — the
    # containment check is about the final resolved location, not about
    # banning the ".." token outright.
    project_dir = _tmp_project_dir()
    resolved = dev_agent_sandbox.resolve_within(project_dir, "a/../b.py")
    assert resolved == (project_dir / "b.py").resolve()


def test_rejects_absolute_path_outside_project_dir():
    project_dir = _tmp_project_dir()
    outside_dir = Path(tempfile.mkdtemp(prefix="dev_agent_sandbox_outside_"))
    _tmp_roots.append(outside_dir)
    outside_path = str(outside_dir / "somewhere_else.txt")
    try:
        dev_agent_sandbox.resolve_within(project_dir, outside_path)
        assert False, "expected ValueError for an absolute path outside project_dir"
    except ValueError:
        pass


def test_accepts_an_absolute_path_that_happens_to_be_inside_project_dir():
    project_dir = _tmp_project_dir()
    inside_absolute = str(project_dir / "sub" / "file.py")
    resolved = dev_agent_sandbox.resolve_within(project_dir, inside_absolute)
    assert resolved == (project_dir / "sub" / "file.py").resolve()


def test_rejects_symlink_pointing_outside_project_dir():
    project_dir = _tmp_project_dir()
    outside_dir = Path(tempfile.mkdtemp(prefix="dev_agent_sandbox_outside_"))
    _tmp_roots.append(outside_dir)
    target = outside_dir / "secret.txt"
    target.write_text("shh")
    link = project_dir / "link_to_outside"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        # Some environments (notably Windows without dev-mode/elevated
        # privileges) can't create symlinks at all — skip rather than
        # fail the suite over an environment limitation, same spirit as
        # dev_agent_events.py's own "never take the suite down" philosophy.
        print("skip  test_rejects_symlink_pointing_outside_project_dir (symlinks unsupported here)")
        return
    try:
        dev_agent_sandbox.resolve_within(project_dir, "link_to_outside")
        assert False, "expected ValueError for a symlink resolving outside project_dir"
    except ValueError:
        pass


def test_rejects_symlinked_directory_escape():
    # Same idea one level deeper: a symlinked directory inside the
    # project dir, with the escaping file referenced through it.
    project_dir = _tmp_project_dir()
    outside_dir = Path(tempfile.mkdtemp(prefix="dev_agent_sandbox_outside_"))
    _tmp_roots.append(outside_dir)
    (outside_dir / "evil.py").write_text("import os; os.system('rm -rf /')")
    link_dir = project_dir / "linked_dir"
    try:
        link_dir.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        print("skip  test_rejects_symlinked_directory_escape (symlinks unsupported here)")
        return
    try:
        dev_agent_sandbox.resolve_within(project_dir, "linked_dir/evil.py")
        assert False, "expected ValueError for a path reached through a symlinked directory escape"
    except ValueError:
        pass


def test_new_project_dir_creates_a_real_unique_directory():
    root = Path(tempfile.mkdtemp(prefix="dev_agent_sandbox_root_"))
    _tmp_roots.append(root)
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", root):
        d1 = dev_agent_sandbox.new_project_dir("da_abc123def456", "My Cool App!")
        assert d1.exists() and d1.is_dir()
        assert d1.parent == root
        assert d1.name == "my-cool-app"

        # A second job with the same project_name must not collide with
        # the first — the fallback slug must still be unique and created.
        d2 = dev_agent_sandbox.new_project_dir("da_ghijk789lmno", "My Cool App!")
        assert d2.exists() and d2.is_dir()
        assert d2 != d1


def test_new_project_dir_falls_back_to_job_id_with_no_project_name():
    root = Path(tempfile.mkdtemp(prefix="dev_agent_sandbox_root_"))
    _tmp_roots.append(root)
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", root):
        d = dev_agent_sandbox.new_project_dir("da_abc123def456", None)
        assert d.exists()
        assert d.name == "da_abc123def456"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
            print(f"ok  {t.__name__}")
        print(f"\n{len(tests)} passed")
    finally:
        for d in _tmp_roots:
            shutil.rmtree(d, ignore_errors=True)
