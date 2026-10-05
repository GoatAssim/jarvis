"""Confines every dev_agent file write to one jailed project directory.

file_tools.py's own path resolution has no containment check at all —
fine for a single human-confirmed write, wrong for an autonomous loop
that writes N files across a whole planning/fix cycle without a
per-file confirmation. This module is what makes "one confirmation up
front, then the agent writes freely within its own sandbox" safe. See
§4.3 of the dev_agent implementation plan.
"""

import os
import shutil
from pathlib import Path

PROJECTS_ROOT = Path.home() / ".jarvis" / "dev_agent_projects"


def new_project_dir(job_id, project_name=None):
    """Create and return a fresh, unique directory under PROJECTS_ROOT for
    one dev_agent job. Prefers a human-readable slug derived from
    project_name; falls back to the job_id itself when no project_name was
    given, and to a job_id-suffixed variant on a name collision so this
    never raises just because two jobs picked the same slug."""
    slug = _slugify(project_name) if project_name else job_id
    d = PROJECTS_ROOT / slug
    if d.exists():
        d = PROJECTS_ROOT / f"{slug}_{job_id[-6:]}"  # collision fallback, still unique+readable
    d.mkdir(parents=True, exist_ok=False)
    return d


def resolve_within(project_dir, relative_path):
    """Resolve relative_path against project_dir and REJECT anything that
    escapes it (../, absolute paths, symlink tricks) — raises ValueError,
    never silently clamps, so a planner-AI-hallucinated '../../etc/passwd'
    fails loudly and becomes a 'write' fail event, not a silent no-op.

    Symlink tricks are caught for free: Path.resolve() follows symlinks,
    so a symlink planted inside project_dir that points outside it
    resolves to its real, outside target before the containment check
    below ever runs — there's no separate symlink-detection branch needed.
    """
    if not relative_path or not str(relative_path).strip():
        raise ValueError("relative_path is required")
    base = project_dir.resolve()
    candidate = (project_dir / relative_path).resolve()
    if base != candidate and base not in candidate.parents:
        raise ValueError(f"path escapes project sandbox: {relative_path!r}")
    return candidate


def _slugify(name):
    keep = [c.lower() if c.isalnum() else "-" for c in name.strip()]
    slug = "".join(keep).strip("-") or "project"
    # collapse repeated hyphens, cap length — same spirit as commands_config's
    # existing name validation elsewhere in the codebase
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:40]


# ---------------------------------------------------------------------------
# Delivery to a user-named folder (BUG-3)
#
# The build itself ALWAYS happens inside PROJECTS_ROOT (the jail above).
# `output_dir` only decides where a SUCCESSFUL build is copied afterwards, so
# the autonomous write/fix loop never gets write access outside the sandbox.
# The destination arrives as a model-filled tool argument, and the text that
# produced it can come from anyone Jarvis is reachable by, so it is validated
# before any AI call is spent: absolute only, never overwrite existing files,
# never inside (or above) Jarvis's own state directory, never a system folder.
# dev_agent also stays in TOOL_CONFIRM_REQUIRED, so the person confirms the
# call (destination included) before anything happens.
# ---------------------------------------------------------------------------

# Rebuilt by `npm install` / a fresh venv; a venv is not relocatable and
# node_modules can be huge. Symlinks are never copied either: the run phase
# executes generated code, which could plant one pointing at a private file.
_COPY_SKIP_DIRS = frozenset({".venv", "venv", "node_modules", "__pycache__", ".git"})

_POSIX_SYSTEM_DIRS = ("/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64",
                      "/var", "/sys", "/proc", "/dev", "/root")


def _system_dirs():
    dirs = [Path(d) for d in _POSIX_SYSTEM_DIRS]
    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        v = os.environ.get(var)
        if v:
            dirs.append(Path(v))
    return dirs


def _is_within(path, parent):
    try:
        parent = parent.resolve()
    except OSError:
        return False
    return path == parent or parent in path.parents


def validate_output_dir(raw):
    """Check a requested delivery folder. Returns (Path, None) or (None, reason).

    Never raises. The folder may not exist yet (it is created on delivery) but
    if it does exist it must be an empty directory: delivery never overwrites
    or merges into somebody's existing files."""
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return None, "output_dir must be a non-empty path"
    try:
        p = Path(raw.strip()).expanduser()
        if not p.is_absolute():
            return None, f"output_dir must be an absolute path (got {raw.strip()!r})"
        dest = p.resolve()
        home = Path.home().resolve()
        jarvis_home = (home / ".jarvis")

        if dest == Path(dest.anchor):
            return None, "output_dir can't be a drive or filesystem root"
        if dest == home or dest in home.parents:
            return None, "output_dir can't be your home folder or a parent of it; name a subfolder"
        if _is_within(dest, jarvis_home) or _is_within(PROJECTS_ROOT.resolve(), dest):
            return None, "output_dir can't be inside Jarvis's own ~/.jarvis state folder"
        for sysdir in _system_dirs():
            if _is_within(dest, sysdir):
                return None, f"output_dir can't be inside the system folder {sysdir}"

        if dest.exists():
            if not dest.is_dir():
                return None, f"{dest} exists and is a file, not a folder"
            if any(dest.iterdir()):
                return None, f"{dest} already exists and is not empty; pick a new or empty folder"
        return dest, None
    except (OSError, RuntimeError, ValueError) as e:
        return None, f"output_dir could not be used: {e}"


def deliver(project_dir, dest):
    """Copy a finished project from the sandbox into `dest` (already passed
    through validate_output_dir). Returns (ok, info). info on success:
    {"files_copied": int, "not_copied": [names]} (dependency folders that
    were left behind, so the caller can say to reinstall); on failure:
    {"error": str}. Re-checks emptiness at copy time (the folder can change
    between validation and delivery) and never raises."""
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if any(dest.iterdir()):
            return False, {"error": f"{dest} is not empty; nothing was copied"}
        base = Path(project_dir)
        copied, left_behind = 0, set()
        for root, dirs, files in os.walk(base, followlinks=False):
            root_p = Path(root)
            rel = root_p.relative_to(base)
            keep = []
            for d in dirs:
                if d in _COPY_SKIP_DIRS:
                    if not rel.parts:  # only report top-level skips
                        left_behind.add(d)
                elif not (root_p / d).is_symlink():
                    keep.append(d)
            dirs[:] = keep
            (dest / rel).mkdir(parents=True, exist_ok=True)
            for f in files:
                src = root_p / f
                if src.is_symlink():
                    continue
                shutil.copy2(src, dest / rel / f)
                copied += 1
        return True, {"files_copied": copied, "not_copied": sorted(left_behind)}
    except OSError as e:
        return False, {"error": f"could not copy into {dest}: {e}"}
