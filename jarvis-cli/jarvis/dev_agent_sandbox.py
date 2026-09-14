"""Confines every dev_agent file write to one jailed project directory.

file_tools.py's own path resolution has no containment check at all —
fine for a single human-confirmed write, wrong for an autonomous loop
that writes N files across a whole planning/fix cycle without a
per-file confirmation. This module is what makes "one confirmation up
front, then the agent writes freely within its own sandbox" safe. See
§4.3 of the dev_agent implementation plan.
"""

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
