"""Auto-generated build metadata — DO NOT EDIT BY HAND.

Regenerated on every `script.bat` build run by
build_tools/bump_build_version.py, which increments BUILD_NUMBER by 1 and
restamps BUILT_AT, unconditionally, every single build. See that file's
docstring for why this exists (short version: pyproject.toml's version
doesn't change per-build, so this is the only reliable signal that a
running jarvis.exe is actually today's build and not a stale one).

Safe to commit. Safe to delete -- bump_build_version.py recreates it from
scratch (starting at build 1) if it's missing.
"""

BUILD_NUMBER = 4
BUILT_AT = "2026-09-14 23:45:11"


def version_string():
    """'jarvis-cli <pkg-version> build <N> (<built-at>)' -- <pkg-version>
    comes from the installed package's own metadata (pyproject.toml's
    [project].version), not duplicated here, so it can never drift out of
    sync with the real thing. Falls back gracefully if the package isn't
    installed the normal way (e.g. running straight from source)."""
    try:
        from importlib.metadata import version as _pkg_version
        pkg_version = _pkg_version("jarvis-cli")
    except Exception:
        pkg_version = "unknown"

    built = f" ({BUILT_AT})" if BUILT_AT else ""
    return f"jarvis-cli {pkg_version} build {BUILD_NUMBER}{built}"
