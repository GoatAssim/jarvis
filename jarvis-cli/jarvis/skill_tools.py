"""The AI-callable surface of the skills system (see skills.py).

Six tools, mapping onto the three tiers:

    list_skills            tier 1 — what exists (also the web manager's read)
    load_skill             tier 2 — pull one skill's instructions into context
    load_skill_reference   tier 3 — open one document from a loaded skill
    create_skill           write a new skill from scratch
    add_skill              install a skill that already exists somewhere
    remove_skill           delete one

Wired into tools.py the same way every other *_tools.py module is (schemas
list + handler dict + a tool_registry group), rather than as an actions/*.py
drop-in, because these are core plumbing: auto-discovery is for capabilities
a user adds, and the skills system has to be present for a user to add
anything at all.

--- On why load_skill exists at all ---

An obvious objection: why not just inject every skill's full text and skip
the tool? Because that is the eager-loading antipattern the whole research
doc is about. Ten skills at 2k tokens each is 20k tokens on every ask,
including "what time is it". The catalog line is ~20 tokens, and the model
pays the 2k only on the ask that actually needs it. load_skill is what turns
a fixed cost into a conditional one.

--- On create_skill being an AI tool and not just a UI form ---

The highest-value moment for writing a skill is right after the user has
finished explaining how they want something done, in a conversation where
all that context is already loaded. Making them stop and open a form loses
it. The model can offer to write the skill there and then — which is also
why create_skill's description tells it to confirm the description line with
the user first: that line is the only thing future asks will see, so getting
it wrong silently breaks discovery forever.
"""

from . import skills

TOOL_GROUP = "skills"


def tool_list_skills(args):
    """Tier 1, on demand. The catalog is already in the system prompt, so
    this mainly serves the web manager and the "what can you do" question —
    it returns the extra bookkeeping (reference files, validity, sizes) that
    the prompt catalog deliberately omits to stay cheap."""
    detail = bool((args or {}).get("detail"))
    installed = skills.list_skills()
    out = {
        "count": len(installed),
        "skills": [
            {
                "name": s["name"],
                "description": s.get("description", ""),
                **({"slug": s.get("slug", ""),
                    "references": s.get("references", []),
                    "valid": s.get("valid", False),
                    **({"error": s["error"]} if s.get("error") else {})}
                   if detail else {}),
            }
            for s in installed
        ],
    }
    if not installed:
        out["note"] = (
            "No skills installed. create_skill writes one from scratch; "
            "add_skill installs an existing SKILL.md."
        )
    return out


def tool_load_skill(args):
    return skills.load_skill((args or {}).get("name"))


def tool_load_skill_reference(args):
    a = args or {}
    return skills.read_reference(a.get("name"), a.get("file"))


def tool_create_skill(args):
    a = args or {}
    return skills.create_skill(
        a.get("name"), a.get("description"), a.get("instructions"), a.get("keywords"),
        a.get("references"), a.get("scripts"),
    )


def tool_add_skill(args):
    a = args or {}
    return skills.add_skill(a.get("source"), a.get("name"))


def tool_remove_skill(args):
    return skills.remove_skill((args or {}).get("name"))


TOOL_SCHEMAS = [
    {
        "name": "list_skills",
        "short_description": "List installed skills.",
        "description": (
            "List every installed skill with its description. The short catalog is "
            "already in your system prompt; use this when you need the full detail "
            "(reference files, validity) or the user asks what skills exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "boolean",
                    "description": "Include slugs, reference filenames and validity errors.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "load_skill",
        "short_description": "Load one skill's full instructions.",
        "description": (
            "Load a skill's full instructions into context. Call this as soon as a task "
            "matches a skill's description in your system prompt — you only have the "
            "one-line description until you do, so don't try to guess the contents. "
            "Reference files are listed but not loaded; use load_skill_reference for those."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name as shown in the catalog."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "load_skill_reference",
        "short_description": "Open one reference doc from a skill.",
        "description": (
            "Read ONE reference document belonging to a skill you've already loaded. "
            "Only open a file the skill's instructions actually point you to for this "
            "task. Scripts bundled with a skill are run through the command tools, "
            "never read with this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name."},
                "file": {"type": "string", "description": "Reference filename, e.g. 'REFERENCE.md'."},
            },
            "required": ["name", "file"],
        },
    },
    {
        "name": "create_skill",
        "short_description": "Write a new skill from scratch.",
        "description": (
            "Create a new skill from scratch: reusable instructions for how the user "
            "wants a recurring task done. Good after the user has explained a process "
            "you'd otherwise have to be told again next time. The description is the "
            "ONLY thing visible on future asks, so make it say when to use the skill, "
            "and confirm it with the user before writing. If the skill is complicated "
            "(several distinct procedures, or any real amount of detail), don't put it "
            "all in `instructions` — split it: keep `instructions` a short overview that "
            "names each module and when to open it, put the step-by-step detail for each "
            "one in `references` (one file per topic), and put any code in `scripts`. "
            "That keeps the skill's own prompt cost low and lets a future ask load only "
            "the one module it actually needs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short human name, e.g. 'Weekly Report'."},
                "description": {
                    "type": "string",
                    "description": "One line: what it does and WHEN to use it. Always in the prompt.",
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "The skill body in markdown. For a simple skill, the whole thing. "
                        "For a complex one, an overview plus pointers to the reference files "
                        "below — not the full step-by-step detail."
                    ),
                },
                "references": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Optional: {filename: markdown content}, one file per distinct "
                        "procedure or topic for a complex skill. Plain filenames only, no "
                        "paths — e.g. 'SETUP.md'. Loaded one at a time via "
                        "load_skill_reference, only when a task needs that specific one."
                    ),
                },
                "scripts": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Optional: {filename: source code} for scripts this skill runs. "
                        "Plain filenames with a real extension — e.g. 'submit.py'. Run "
                        "through the command tools, never read back into context."
                    ),
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional words that should bring this skill to mind.",
                },
            },
            "required": ["name", "description", "instructions"],
        },
    },
    {
        "name": "add_skill",
        "short_description": "Install an existing skill.",
        "description": (
            "Install a skill that already exists: a folder path, a path to a SKILL.md "
            "file, a path to a .zip (a skill folder with scripts and reference docs, "
            "zipped), or the skill's markdown pasted in directly. The content must "
            "have frontmatter with a description — use create_skill instead if it "
            "doesn't."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Folder path, SKILL.md path, .zip path, or the raw markdown.",
                },
                "name": {"type": "string", "description": "Optional override for the skill's name."},
            },
            "required": ["source"],
        },
    },
    {
        "name": "remove_skill",
        "short_description": "Delete a skill permanently.",
        "description": (
            "Remove a skill and everything in its folder. Permanent — confirm with the "
            "user first. Its catalog line disappears from the prompt on the next ask."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name or slug to remove."},
            },
            "required": ["name"],
        },
    },
]

TOOLS = {
    "list_skills": tool_list_skills,
    "load_skill": tool_load_skill,
    "load_skill_reference": tool_load_skill_reference,
    "create_skill": tool_create_skill,
    "add_skill": tool_add_skill,
    "remove_skill": tool_remove_skill,
}

# remove_skill deletes a folder tree with no undo, so it goes through the
# same confirm gate as every other destructive tool rather than relying on
# the model's own description-level caution.
TOOL_CONFIRM_REQUIRED = {"remove_skill"}

TOOL_KEYWORDS = {
    "list_skills": {"skills": 9, "skill": 7, "what skills": 12},
    "load_skill": {"load skill": 12, "use skill": 10, "skill": 5},
    "create_skill": {"create skill": 12, "new skill": 12, "make a skill": 12, "remember how": 8},
    "add_skill": {"add skill": 12, "install skill": 12, "import skill": 10},
    "remove_skill": {"remove skill": 12, "delete skill": 12, "uninstall skill": 10, "unload skill": 10},
}

TOOL_PACK_INSTRUCTION = (
    "Skills are loaded on demand: you have each one's name and description "
    "only. Call load_skill before acting on a task a skill covers."
)
