"""Guards against build_info.py drifting out of sync with the source tree
it claims to describe.

This is the "CI/build check that fails when build_info.py is stale" P0
item: build_tools/bump_build_version.py is the tool that regenerates
build_info.py, but nothing previously stopped someone from committing a
source change without re-running it. This test recomputes the hash the
same way bump_build_version.py does (build_tools.hash_source.hash_source_tree)
and fails loudly if it disagrees with what's checked in, with a message
that says exactly how to fix it.

Run with `python3 tests/test_build_info.py`. No API key or network needed.
"""

import sys
from pathlib import Path

JARVIS_CLI = Path(__file__).resolve().parent.parent / "jarvis-cli"
sys.path.insert(0, str(JARVIS_CLI))

from build_tools.hash_source import hash_source_tree  # noqa: E402
from jarvis import build_info  # noqa: E402


def test_source_hash_matches_tree():
    computed = hash_source_tree()
    assert build_info.SOURCE_HASH == computed, (
        "jarvis/build_info.py is stale: its SOURCE_HASH "
        f"({build_info.SOURCE_HASH[:12]}...) does not match the source "
        f"tree on disk right now ({computed[:12]}...). Someone edited "
        "jarvis-cli/jarvis/ without regenerating build metadata. Fix: "
        "run `python build_tools/bump_build_version.py` from jarvis-cli/ "
        "and commit the resulting jarvis/build_info.py."
    )
    print("ok  build_info.SOURCE_HASH matches the source tree")


def test_source_hash_is_well_formed():
    # Catches the file being hand-edited into something that isn't even a
    # real sha256 hex digest -- a stale-but-plausible-looking hash would
    # otherwise only surface via the (much less obvious) equality failure
    # above.
    assert isinstance(build_info.SOURCE_HASH, str) and len(build_info.SOURCE_HASH) == 64
    int(build_info.SOURCE_HASH, 16)  # raises ValueError if not hex
    print("ok  build_info.SOURCE_HASH is a well-formed sha256 hex digest")


if __name__ == "__main__":
    test_source_hash_matches_tree()
    test_source_hash_is_well_formed()
    print("\nall build_info tests passed")
