"""Content hash of jarvis-cli/jarvis/ (the actual source the exe is built
from). Answers exactly one question: has anything the built exe depends
on changed since it was built? Shared by two callers that need the exact
same answer to that question:

  - bump_build_version.py: decides whether BUILD_NUMBER should increment
    at all (source changed) or stay put (rebuild-with-no-source-change,
    e.g. re-running script.bat twice in a row).
  - verify_exe.py: compares this hash, recomputed from source ON DISK
    RIGHT NOW, against the hash embedded in whatever exe `jarvis` on PATH
    actually resolves to.

Deliberately NOT part of the installed `jarvis` package (it lives in
build_tools/, which pyproject.toml's packages=["jarvis","jarvis.voice"]
does not ship) -- this hash is a build-time/dev-time question about the
source tree, not something the running exe needs to compute about
itself. The exe only ever reports the hash it was built with (baked into
jarvis/build_info.py at build time), never recomputes one live.
"""

import hashlib
from pathlib import Path

JARVIS_PKG_DIR = Path(__file__).resolve().parent.parent / "jarvis"

# build_info.py stores the hash itself -- hashing it would make the hash
# depend on its own previous value (it changes every bump, which would
# make the hash never stabilize even when real source is untouched).
# __pycache__ holds compiled artifacts, never source.
_EXCLUDE_NAMES = {"build_info.py"}
_EXCLUDE_DIRS = {"__pycache__"}


def hash_source_tree(root=JARVIS_PKG_DIR):
    """SHA-256 over every .py file under root (recursive), sorted by
    relative path for determinism. Each file is mixed in as
    "<relpath>\\n<content bytes>" so a rename/move and a pure content
    edit are both detected, not just "did total byte content change".
    Returns the hex digest string. Raises if root doesn't exist --
    callers decide how to handle that; this never silently returns a
    placeholder hash for a tree it couldn't actually read."""
    root = Path(root)
    files = sorted(
        p for p in root.rglob("*.py")
        if p.name not in _EXCLUDE_NAMES
        and not any(part in _EXCLUDE_DIRS for part in p.relative_to(root).parts)
    )

    digest = hashlib.sha256()
    for path in files:
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\n")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def short_hash(full_hash):
    """First 12 hex chars -- enough to tell apart at a glance in a
    terminal without truncating so hard two different builds could
    plausibly collide by eye."""
    return full_hash[:12]
