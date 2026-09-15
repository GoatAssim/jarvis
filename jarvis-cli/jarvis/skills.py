"""Agent Skills for jarvis — lever 1 of
skills-and-token-optimization-research.md, implemented literally.

A skill is a folder under ~/.jarvis/skills/<slug>/ containing a SKILL.md
with YAML frontmatter, plus any number of reference documents and scripts:

    ~/.jarvis/skills/
      blender-render/
        SKILL.md            <- frontmatter + instructions
        RENDER-SETTINGS.md  <- reference, loaded only when asked for
        submit.py           <- script, RUN not read

===========================================================================
The three tiers, and what each one costs
===========================================================================

TIER 1 — DISCOVERY. Only `name` + `description` from the frontmatter, for
    every installed skill. Always present, appended to the system prompt by
    catalog_text(). ~15-25 tokens per skill.

    Critically, catalog_text() output goes into the STATIC half of the
    system prompt (see ai_client._system_prompt_parts), so it sits inside
    the cached prefix rather than in front of it. The catalog is exactly the
    kind of content prompt caching exists for: identical on every turn,
    growing slowly, and in front of everything that changes. Installing ten
    skills costs ~200 tokens once per cache write, not once per request.

TIER 2 — ACTIVATION. The full SKILL.md body, loaded only when the model
    decides a description matches the task and calls load_skill. Anthropic's
    guidance caps this at <5k tokens; BODY_SOFT_LIMIT below enforces the
    same ceiling with a warning rather than a hard failure, because a
    truncated skill that says so beats a rejected one.

TIER 3 — EXECUTION. Referenced files, loaded one at a time, only if the
    SKILL.md body points at them for this specific task. Effectively
    unlimited: cost is paid only for what's opened.

    The load-bearing distinction in tier 3, and the reason a skill can ship
    megabytes for ~0 ongoing token cost: SCRIPTS ARE RUN, NOT READ. A
    bundled .py/.ps1/.sh never enters the context window — the model calls
    it through the existing command tools and only stdout comes back.
    read_reference() enforces that by refusing to return the source of an
    executable file and pointing at how to run it instead. Without that
    refusal, "reference" and "script" collapse into the same thing and the
    tier-3 saving evaporates.

===========================================================================
Why a folder of markdown rather than more Python
===========================================================================

actions/*.py (see tool_loader.py) is the right shape for a new CAPABILITY —
something that needs to run code. Skills are the right shape for new
KNOWLEDGE — how this user wants a thing done, a house style, a checklist, a
build convention. Those need no code, should be editable without restarting
anything, and must not cost tokens when the current task is unrelated.

The format is Anthropic's Agent Skills standard (SKILL.md + frontmatter),
which OpenAI, Google, GitHub Copilot and Cursor all adopted, so a skill
written here is portable to those clients and vice versa.
"""

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

SKILLS_DIR = Path.home() / ".jarvis" / "skills"
ENCODING = "utf-8"
SKILL_FILE = "SKILL.md"

# Tier-1 budget. A description longer than this is clipped in the catalog
# (never in the file) — one skill cannot be allowed to crowd out the prompt
# for every other skill just by writing a paragraph where a line belongs.
DESC_MAX = 160

# Tier-2 ceiling, matching Anthropic's <5k-token guidance at ~4 chars/token.
# Enforced as a warning + clip rather than a rejection: a skill that loads
# with "[truncated]" on the end is still useful, while one that refuses to
# load is not.
BODY_SOFT_LIMIT = 20000

# Tier-3 per-file ceiling. Generous, because the whole point of tier 3 is
# that you only pay for the one file the task needs — but not unbounded,
# since a stray 50MB CSV in a skill folder should degrade gracefully rather
# than blow out the context window.
REFERENCE_MAX = 40000

# Extensions treated as executable. Reading these into context is the exact
# mistake tier 3 exists to prevent, so read_reference() refuses them by name.
SCRIPT_SUFFIXES = {".py", ".ps1", ".sh", ".bat", ".cmd", ".js", ".rb", ".pl", ".exe"}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def slugify(name):
    """'Blender Render Pipeline' -> 'blender-render-pipeline'."""
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return slug[:64]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def parse_frontmatter(text):
    """Split '---\\nkey: value\\n---\\nbody' into (dict, body).

    A deliberately small, dependency-free subset of YAML: `key: value` pairs
    and inline `[a, b, c]` lists, which is everything the Agent Skills
    frontmatter spec actually uses. Bringing in PyYAML for this would add a
    hard dependency to a program whose whole appeal is that it runs from a
    single bundled exe.

    Missing or malformed frontmatter returns ({}, original_text) rather than
    raising — a skill with a broken header should show up in the list with a
    warning, not vanish or take the scan down with it.
    """
    if not text:
        return {}, ""
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return {}, text
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key] = value
    return meta, parts[2].lstrip("\n")


def render_frontmatter(meta):
    lines = ["---"]
    for key in ("name", "description", "keywords", "version", "created"):
        if key not in meta:
            continue
        value = meta[key]
        if isinstance(value, (list, tuple)):
            value = "[" + ", ".join(str(v) for v in value) + "]"
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tier 1 — discovery
# ---------------------------------------------------------------------------

def _skill_dirs():
    try:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        return sorted((p for p in SKILLS_DIR.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        # A read-only or missing home directory must never take jarvis down;
        # it just means no skills this run. Same fault tolerance as
        # tool_loader.discover_actions().
        return []


def _read_skill_file(folder):
    path = folder / SKILL_FILE
    try:
        return path.read_text(encoding=ENCODING, errors="replace")
    except OSError:
        return None


def list_skills(include_body=False):
    """Every installed skill's tier-1 metadata, in name order.

    A folder without a SKILL.md, or with an unreadable one, is reported with
    `valid: False` and an `error` rather than skipped silently — a skill the
    user thinks they installed but that never loads should be visible in the
    manager, not invisible everywhere.
    """
    out = []
    for folder in _skill_dirs():
        raw = _read_skill_file(folder)
        if raw is None:
            out.append({
                "name": folder.name,
                "valid": False,
                "error": f"No readable {SKILL_FILE} in this folder.",
                "description": "",
            })
            continue
        meta, body = parse_frontmatter(raw)
        name = (meta.get("name") or folder.name).strip()
        desc = (meta.get("description") or "").strip()
        entry = {
            "name": name,
            "slug": folder.name,
            "description": desc,
            "valid": bool(desc),
            "keywords": meta.get("keywords") or [],
            "version": meta.get("version") or "",
            "references": sorted(
                p.name for p in folder.iterdir()
                if p.is_file() and p.name != SKILL_FILE
            ) if folder.is_dir() else [],
            "body_chars": len(body),
        }
        if not desc:
            entry["error"] = (
                "No `description:` in the frontmatter — without one the model "
                "has nothing to match a task against, so this skill will never "
                "be loaded."
            )
        if include_body:
            entry["body"] = body
        out.append(entry)
    return out


def catalog_text(max_skills=40):
    """The tier-1 block injected into the system prompt, or "".

    Deliberately terse: one line per skill, description clipped to DESC_MAX.
    This is the only skills content that is present unconditionally, so it is
    the only part whose cost scales with skills-that-exist rather than
    skills-that-are-used. Everything else is behind load_skill.

    Returns "" when nothing is installed, so a user with no skills pays
    exactly zero tokens for the feature.
    """
    skills = [s for s in list_skills() if s.get("valid")]
    if not skills:
        return ""
    lines = [
        "Skills available (instructions you can load on demand). Call "
        "load_skill with the name when a task matches one — the description "
        "is all you have until you do:"
    ]
    for skill in skills[:max_skills]:
        desc = skill["description"]
        if len(desc) > DESC_MAX:
            desc = desc[:DESC_MAX - 1].rstrip() + "\u2026"
        lines.append(f"- {skill['name']}: {desc}")
    if len(skills) > max_skills:
        lines.append(f"- (+{len(skills) - max_skills} more — call list_skills to see them)")
    return "\n".join(lines)


def find_skill(name):
    """Resolve a user/model-supplied name to a folder. Returns (path, error).

    Matches on slug first, then exact name, then case-insensitive name, then
    a unique prefix — because the model is working from a catalog line and
    will sometimes paraphrase. An ambiguous prefix is an error naming the
    candidates rather than an arbitrary pick.
    """
    wanted = (name or "").strip()
    if not wanted:
        return None, "Pass the name of a skill."
    slug = slugify(wanted)

    folders = _skill_dirs()
    by_slug = {p.name: p for p in folders}
    if slug in by_slug:
        return by_slug[slug], None

    named = []
    for folder in folders:
        raw = _read_skill_file(folder)
        if raw is None:
            continue
        meta, _ = parse_frontmatter(raw)
        real = (meta.get("name") or folder.name).strip()
        if real.lower() == wanted.lower():
            return folder, None
        named.append((real, folder))

    prefix = [f for real, f in named if real.lower().startswith(wanted.lower())]
    if len(prefix) == 1:
        return prefix[0], None
    if len(prefix) > 1:
        return None, f"'{wanted}' matches several skills: {sorted(p.name for p in prefix)}."
    available = sorted(by_slug)
    return None, (
        f"No skill named '{wanted}'."
        + (f" Installed: {available}." if available else " None are installed yet.")
    )


# ---------------------------------------------------------------------------
# Tier 2 — activation
# ---------------------------------------------------------------------------

def load_skill(name):
    """The full SKILL.md body for one skill. Tier 2."""
    folder, error = find_skill(name)
    if error:
        return {"error": error}
    raw = _read_skill_file(folder)
    if raw is None:
        return {"error": f"Couldn't read {SKILL_FILE} for '{name}'."}
    meta, body = parse_frontmatter(raw)

    truncated = False
    if len(body) > BODY_SOFT_LIMIT:
        body = body[:BODY_SOFT_LIMIT] + "\n\n[...truncated — this skill's body exceeds the size limit]"
        truncated = True

    references = sorted(p.name for p in folder.iterdir() if p.is_file() and p.name != SKILL_FILE)
    result = {
        "name": (meta.get("name") or folder.name).strip(),
        "loaded": True,
        "instructions": body,
    }
    if references:
        # Tier 3 is advertised but NOT loaded. Listing the filenames costs a
        # few tokens and is what lets the model open exactly the one it
        # needs; loading them here would collapse tier 3 back into tier 2 and
        # throw away the entire saving.
        result["references"] = references
        result["note"] = (
            "Reference files are NOT loaded. Call load_skill_reference for a "
            "document you actually need. Scripts are run through the command "
            "tools, never read."
        )
    if truncated:
        result["warning"] = f"Body clipped at {BODY_SOFT_LIMIT} chars."
    return result


# ---------------------------------------------------------------------------
# Tier 3 — execution
# ---------------------------------------------------------------------------

def read_reference(name, filename):
    """One reference document from a skill folder. Tier 3."""
    folder, error = find_skill(name)
    if error:
        return {"error": error}

    requested = (filename or "").strip()
    if not requested:
        return {"error": "Pass the reference filename to read."}

    # Path containment: a skill is user-authored content and a reference name
    # arrives from the model, so "../../.ssh/id_rsa" is a request that will
    # eventually be made. resolve() both sides and require containment.
    target = (folder / requested).resolve()
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        return {"error": "Reference path escapes the skill folder."}
    if not target.is_file():
        return {"error": f"'{requested}' isn't a file in skill '{name}'."}

    if target.suffix.lower() in SCRIPT_SUFFIXES:
        # The tier-3 rule from the research doc: scripts are RUN, not READ.
        # Returning the source would put the whole file in context, which is
        # precisely the cost this tier exists to avoid — and the model
        # doesn't need the source to call it.
        return {
            "error": f"'{requested}' is a script, not a reference document.",
            "hint": (
                "Run it with the command tools and use its output. Its source "
                "is deliberately not loaded into context."
            ),
            "path": str(target),
        }

    try:
        text = target.read_text(encoding=ENCODING, errors="replace")
    except OSError as e:
        return {"error": f"Couldn't read '{requested}': {e}"}

    truncated = len(text) > REFERENCE_MAX
    if truncated:
        text = text[:REFERENCE_MAX] + "\n\n[...truncated]"
    out = {"skill": folder.name, "file": requested, "content": text}
    if truncated:
        out["warning"] = f"Clipped at {REFERENCE_MAX} chars."
    return out


# ---------------------------------------------------------------------------
# Management
# ---------------------------------------------------------------------------

def _validate_new(name, description):
    slug = slugify(name)
    if not slug or not _NAME_RE.match(slug):
        return None, "Name must contain letters or numbers (e.g. 'Blender Render')."
    if not (description or "").strip():
        return None, (
            "A description is required — it's the only thing the model sees "
            "until the skill is loaded, so a skill without one can never be "
            "chosen."
        )
    if (SKILLS_DIR / slug).exists():
        return None, f"A skill '{slug}' already exists. Remove it first, or pick another name."
    return slug, None


def create_skill(name, description, instructions, keywords=None):
    """Write a brand-new skill folder from supplied content."""
    slug, error = _validate_new(name, description)
    if error:
        return {"error": error}
    body = (instructions or "").strip()
    if not body:
        return {"error": "Instructions are required — an empty skill does nothing."}

    meta = {
        "name": name.strip(),
        "description": " ".join((description or "").split()),
        "version": "1",
        "created": _now(),
    }
    if keywords:
        meta["keywords"] = keywords if isinstance(keywords, list) else [
            k.strip() for k in str(keywords).split(",") if k.strip()
        ]

    folder = SKILLS_DIR / slug
    try:
        folder.mkdir(parents=True, exist_ok=False)
        (folder / SKILL_FILE).write_text(
            f"{render_frontmatter(meta)}\n\n{body}\n", encoding=ENCODING
        )
    except OSError as e:
        return {"error": f"Couldn't create skill: {e}"}
    return {
        "created": True,
        "name": meta["name"],
        "slug": slug,
        "path": str(folder),
        "catalog_cost_note": (
            "Only the name and description are in the prompt from now on; the "
            "instructions load only when this skill is used."
        ),
    }


def add_skill(source, name=None):
    """Install a skill from existing content: a folder, a SKILL.md file, or
    raw markdown pasted in.

    The three shapes exist because they're the three ways a skill actually
    arrives — copied from another agent's skills directory, downloaded as a
    single file, or pasted from a chat window.
    """
    raw = (source or "").strip()
    if not raw:
        return {"error": "Pass a folder path, a SKILL.md path, or the skill's markdown."}

    path = Path(raw).expanduser()
    try:
        is_dir, is_file = path.is_dir(), path.is_file()
    except OSError:
        is_dir = is_file = False

    if is_dir:
        src_file = path / SKILL_FILE
        if not src_file.is_file():
            return {"error": f"'{path}' has no {SKILL_FILE}."}
        meta, _ = parse_frontmatter(src_file.read_text(encoding=ENCODING, errors="replace"))
        slug, error = _validate_new(name or meta.get("name") or path.name, meta.get("description"))
        if error:
            return {"error": error}
        try:
            shutil.copytree(path, SKILLS_DIR / slug)
        except OSError as e:
            return {"error": f"Couldn't copy skill folder: {e}"}
        return {"added": True, "name": meta.get("name") or slug, "slug": slug,
                "source": "folder", "path": str(SKILLS_DIR / slug)}

    if is_file:
        try:
            raw = path.read_text(encoding=ENCODING, errors="replace")
        except OSError as e:
            return {"error": f"Couldn't read '{path}': {e}"}

    meta, body = parse_frontmatter(raw)
    if not meta.get("description"):
        return {
            "error": (
                "This content has no frontmatter `description:`. Add one, or "
                "use create_skill with an explicit description."
            )
        }
    slug, error = _validate_new(name or meta.get("name") or "skill", meta.get("description"))
    if error:
        return {"error": error}
    folder = SKILLS_DIR / slug
    try:
        folder.mkdir(parents=True, exist_ok=False)
        (folder / SKILL_FILE).write_text(raw if raw.startswith("---") else
                                         f"{render_frontmatter(meta)}\n\n{body}\n",
                                         encoding=ENCODING)
    except OSError as e:
        return {"error": f"Couldn't write skill: {e}"}
    return {"added": True, "name": meta.get("name") or slug, "slug": slug,
            "source": "markdown", "path": str(folder)}


def remove_skill(name):
    """Delete a skill folder and everything in it. Irreversible."""
    folder, error = find_skill(name)
    if error:
        return {"error": error}
    try:
        shutil.rmtree(folder)
    except OSError as e:
        return {"error": f"Couldn't remove '{folder.name}': {e}"}
    return {
        "removed": True,
        "slug": folder.name,
        "note": "Its catalog line is gone from the prompt as of the next ask.",
    }


def export_skill(name):
    """The raw SKILL.md text, for the web manager's editor."""
    folder, error = find_skill(name)
    if error:
        return {"error": error}
    raw = _read_skill_file(folder)
    if raw is None:
        return {"error": f"Couldn't read {SKILL_FILE}."}
    return {"slug": folder.name, "content": raw}


def write_skill_file(name, content):
    """Overwrite a skill's SKILL.md — the web manager's save path.

    Validates the frontmatter BEFORE writing, so a bad edit is rejected while
    the user still has the text on screen rather than silently producing a
    skill that never loads again.
    """
    folder, error = find_skill(name)
    if error:
        return {"error": error}
    meta, _ = parse_frontmatter(content or "")
    if not meta.get("description"):
        return {"error": "Frontmatter must include a `description:` line."}
    try:
        (folder / SKILL_FILE).write_text(content, encoding=ENCODING)
    except OSError as e:
        return {"error": f"Couldn't save: {e}"}
    return {"saved": True, "slug": folder.name, "name": meta.get("name") or folder.name}


def stats():
    """Counts + the measured tier-1 prompt cost, for the web manager."""
    from . import token_usage

    skills = list_skills()
    catalog = catalog_text()
    return {
        "installed": len(skills),
        "valid": sum(1 for s in skills if s.get("valid")),
        "catalog_tokens": token_usage.estimate_tokens_for(catalog),
        "skills_dir": str(SKILLS_DIR),
    }
