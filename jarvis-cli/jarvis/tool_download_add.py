"""The add tool a download provider can ask the loader to generate (Download Hub, N4; plan 5.10).

A tool file that supplies its own downloads (TOOL_DOWNLOADS) may give an entry an `add` block:

    TOOL_DOWNLOADS = [{
        "id": "model_fetch", "label": "Model downloads", ...,
        "add": {
            "tool": "model_fetch_start",                    # one of THIS file's own tools
            "description": "Fetch a model from the hub.",   # one line, <= 200 chars
            "params": {"repo": {"type": "string", "description": "owner/name"}},
            "required": ["repo"],
            "shared": ["dest"],                             # optional: the start tool takes these too
        },
    }]

and the loader makes `dl_add_model_fetch` from it.  Nobody writes that tool by hand, and nobody
adds a provider-specific argument to `dl_add`.

The generated tool:
  - is made fresh at every discovery, so it exists exactly as long as the file does (a file that is
    removed, disabled or fails to load has no `dl_add_<id>`);
  - checks its arguments against `params` (unknown names, missing required ones, wrong types and
    values outside an enum are refused with a plain message) and then runs `add.tool` through
    tools.execute_tool, so the tool-off switch and the per-person allow-list still apply to it;
  - is confirm-gated when `add.tool` is (it is listed in TOOL_CONFIRM_REQUIRED by its own name,
    because confirmation is out-of-band by name), and AI-reviewed when `add.tool` is;
  - is the owner's: a chat guest is refused in the handler (5.10 rule 7), whatever a per-person
    allow-list says;
  - has `dry_run` (added by the generator): check the arguments and say what would run, run nothing;
  - gets a Test Checklist entry of its own;
  - never replaces a tool that already has the name: the clash is logged and the generated tool is
    skipped.

A bad `add` block is logged and dropped; the file's tools and the provider load regardless.
"""
import copy
import json
import os
import re

MAX_ARGS = 12                 # params + shared + dry_run
MAX_PARAMS_BYTES = 2048       # the JSON of the generated tool's `parameters`
MAX_DESCRIPTION = 200
NAME_PREFIX = "dl_add_"

_ADD_KEYS = frozenset(("tool", "description", "params", "required", "shared"))
_ARG_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TYPES = ("string", "integer", "number", "boolean", "array", "object")
_PROP_KEYS = frozenset(("type", "description", "enum", "items", "minimum", "maximum"))
_ITEM_TYPES = ("string", "integer", "number", "boolean")

# The shared arguments a provider may switch on, with the same wording the built-in add tools use.
SHARED_PROPS = {
    "dest": {"type": "string", "description": "Where THIS download is saved (the provider's tool decides what it accepts)."},
    "group": {"type": "string", "description": "A name for this batch."},
    "start_paused": {"type": "boolean", "description": "Queue it paused instead of starting it."},
    "start_at": {"type": "string", "description": "Hold it until a time, in the person's words: 'tomorrow at 3am', 'in 2 hours'."},
}
DRY_RUN_PROP = {"type": "boolean", "description": "Only check the arguments and say what would run; start nothing."}


def split_add(raw):
    """(raw TOOL_DOWNLOADS without the `add` keys, {provider id: add block}).  The loader hands the
    first to tool_downloads.validate_downloads (which knows nothing of `add`).  Anything that is not
    a list comes back unchanged so the validator reports it as it always did."""
    if not isinstance(raw, (list, tuple)):
        return raw, {}
    clean, adds = [], {}
    for entry in raw:
        if isinstance(entry, dict) and "add" in entry:
            entry = dict(entry)
            block = entry.pop("add")
            pid = entry.get("id")
            if isinstance(pid, str):
                adds[pid] = block
        clean.append(entry)
    return clean, adds


def _clean_prop(name, prop, problems):
    if not isinstance(prop, dict):
        problems.append("param %r must be a dict" % name)
        return None
    extra = set(prop) - _PROP_KEYS
    if extra:
        problems.append("param %r has unknown keys %s" % (name, sorted(extra)))
        return None
    typ = prop.get("type")
    if typ not in _TYPES:
        problems.append("param %r needs a 'type' of %s" % (name, "/".join(_TYPES)))
        return None
    out = {"type": typ}
    desc = prop.get("description")
    if desc is not None:
        if not isinstance(desc, str) or len(desc) > 300:
            problems.append("param %r description must be a string of at most 300 characters" % name)
            return None
        out["description"] = desc
    if "enum" in prop:
        enum = prop["enum"]
        if (not isinstance(enum, list) or not enum or len(enum) > 20
                or not all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in enum)):
            problems.append("param %r enum must be a list of 1-20 strings / numbers" % name)
            return None
        out["enum"] = list(enum)
    if "items" in prop:
        items = prop["items"]
        if typ != "array" or not isinstance(items, dict) or items.get("type") not in _ITEM_TYPES:
            problems.append("param %r items must be {'type': one of %s} and only on an array" % (name, "/".join(_ITEM_TYPES)))
            return None
        out["items"] = {"type": items["type"]}
    for key in ("minimum", "maximum"):
        if key in prop:
            val = prop[key]
            if typ not in ("integer", "number") or isinstance(val, bool) or not isinstance(val, (int, float)):
                problems.append("param %r %s must be a number and only on an integer / number" % (name, key))
                return None
            out[key] = val
    return out


def validate_add(block, provider, tool_names):
    """-> (clean add block | None, [problems]).  `provider` is the validated TOOL_DOWNLOADS entry,
    `tool_names` the names in the file's TOOLS."""
    where = "TOOL_DOWNLOADS[%s].add" % provider.get("id")
    problems = []
    if not isinstance(block, dict):
        return None, ["%s must be a dict" % where]
    extra = set(block) - _ADD_KEYS
    if extra:
        problems.append("unknown keys %s" % sorted(extra))
    tool = block.get("tool")
    if not isinstance(tool, str) or tool not in tool_names:
        problems.append("'tool' must be the name of one of this file's own tools")
    elif tool.startswith(NAME_PREFIX):
        problems.append("'tool' can't be a dl_add_* tool")
    desc = block.get("description")
    if not isinstance(desc, str) or not desc.strip() or len(desc.strip()) > MAX_DESCRIPTION:
        problems.append("'description' must be one line of 1-%d characters" % MAX_DESCRIPTION)
    raw_params = block.get("params", {})
    params = {}
    if not isinstance(raw_params, dict):
        problems.append("'params' must be a dict")
        raw_params = {}
    for name, prop in raw_params.items():
        if not isinstance(name, str) or not _ARG_RE.match(name):
            problems.append("param name %r must match ^[a-z][a-z0-9_]{0,31}$" % (name,))
            continue
        if name == "dry_run":
            problems.append("'dry_run' is added by the loader; remove it from params")
            continue
        cleaned = _clean_prop(name, prop, problems)
        if cleaned is not None:
            params[name] = cleaned
    shared = block.get("shared", [])
    if not isinstance(shared, list) or any(s not in SHARED_PROPS for s in shared):
        problems.append("'shared' must be a list drawn from %s" % sorted(SHARED_PROPS))
        shared = []
    shared = [s for i, s in enumerate(shared) if s not in shared[:i]]
    clash = [s for s in shared if s in params]
    if clash:
        problems.append("%s is in both 'params' and 'shared'" % clash)
    required = block.get("required", [])
    if (not isinstance(required, list) or any(not isinstance(r, str) for r in required)
            or any(r not in params and r not in shared for r in required)):
        problems.append("'required' must list names from 'params' / 'shared'")
        required = []
    if len(params) + len(shared) + 1 > MAX_ARGS:
        problems.append("at most %d arguments including dry_run" % MAX_ARGS)
    if problems:
        return None, ["%s: %s" % (where, p) for p in problems]
    clean = {"tool": tool, "description": desc.strip(), "params": params,
             "required": list(required), "shared": shared}
    if len(json.dumps(_properties(clean), ensure_ascii=False)) > MAX_PARAMS_BYTES:
        return None, ["%s: the arguments are over %d bytes of JSON; trim the descriptions" % (where, MAX_PARAMS_BYTES)]
    return clean, []


def _properties(add):
    props = copy.deepcopy(add["params"])
    for name in add["shared"]:
        props[name] = dict(SHARED_PROPS[name])
    props["dry_run"] = dict(DRY_RUN_PROP)
    return props


def _is_chat_guest():
    """Same rule as actions/download_hub.py: a chat message from someone who is not the owner.
    An unreadable value counts as a guest (a permission check fails closed)."""
    raw = os.environ.get("JARVIS_CHANNEL_SENDER") or ""
    if not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return True
    if not isinstance(data, dict):
        return True
    if not data.get("platform") or not data.get("user_id"):
        return False
    return not data.get("is_owner")


def check_args(add, args):
    """-> (clean arguments for add.tool, [problems]).  dry_run is not part of the result."""
    props = _properties(add)
    problems, clean = [], {}
    args = dict(args or {})
    args.pop("dry_run", None)
    for name in args:
        if name not in props:
            problems.append("unknown argument %r (known: %s)" % (name, ", ".join(sorted(n for n in props if n != "dry_run")) or "none"))
    for name in add["required"]:
        if args.get(name) in (None, ""):
            problems.append("%r is required" % name)
    for name, value in args.items():
        if name not in props or value is None:
            continue
        prop = props[name]
        typ = prop["type"]
        ok, value = _coerce(typ, value)
        if not ok:
            problems.append("%r must be %s" % (name, "an " + typ if typ in ("integer", "array", "object") else "a " + typ))
            continue
        if "enum" in prop and value not in prop["enum"]:
            problems.append("%r must be one of %s" % (name, ", ".join(str(v) for v in prop["enum"])))
            continue
        if typ == "array" and "items" in prop:
            item_t = prop["items"]["type"]
            coerced = [_coerce(item_t, v) for v in value]
            if not all(ok_ for ok_, _ in coerced):
                problems.append("every item of %r must be a %s" % (name, item_t))
                continue
            value = [v for _, v in coerced]
        if typ in ("integer", "number"):
            if "minimum" in prop and value < prop["minimum"]:
                problems.append("%r must be at least %s" % (name, prop["minimum"]))
                continue
            if "maximum" in prop and value > prop["maximum"]:
                problems.append("%r must be at most %s" % (name, prop["maximum"]))
                continue
        clean[name] = value
    return clean, problems


def _coerce(typ, value):
    if typ == "string":
        return (True, value) if isinstance(value, str) else (False, value)
    if typ == "boolean":
        if isinstance(value, bool):
            return True, value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return True, value.strip().lower() == "true"
        return False, value
    if typ in ("integer", "number"):
        if isinstance(value, bool):
            return False, value
        if isinstance(value, (int, float)):
            if typ == "integer" and isinstance(value, float):
                return (True, int(value)) if value == int(value) else (False, value)
            return True, value
        if isinstance(value, str):
            try:
                num = float(value.strip())
            except ValueError:
                return False, value
            if typ == "integer":
                return (True, int(num)) if num == int(num) else (False, value)
            return True, num
        return False, value
    if typ == "array":
        return (True, list(value)) if isinstance(value, (list, tuple)) else (False, value)
    if typ == "object":
        return (True, value) if isinstance(value, dict) else (False, value)
    return False, value


def _make_handler(provider, add):
    tool = add["tool"]
    name = NAME_PREFIX + provider["id"]

    def handler(args=None, context=None):
        if _is_chat_guest():
            return {"ok": False, "error": "adding downloads is the owner's: someone in a chat can look at the "
                                          "queue (dl_list, dl_status) but not add to it"}
        args = args if isinstance(args, dict) else {}
        dry = bool(args.get("dry_run"))
        clean, problems = check_args(add, args)
        if problems:
            return {"ok": False, "error": "; ".join(problems)}
        if dry:
            return {"ok": True, "dry_run": True, "provider": provider["id"], "would_run": tool,
                    "arguments": clean}
        # Lazy: tools.py imports the loader, so the loader can't import tools at import time.
        from . import tools as tools_mod
        result = tools_mod.execute_tool(tool, clean, context=context)
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("add_tool", name)
            result.setdefault("provider", provider["id"])
        return result

    handler.__name__ = "tool_" + name
    return handler


def _checklist_entry(provider, add, name, group):
    required = {r: "<%s>" % r for r in add["required"]}
    label = provider.get("label") or provider["id"]
    return {
        "does": "Queues a download from '%s' (%s): checks the arguments, then runs %s; generated from the "
                "provider's `add` block, never written by hand." % (label, add["description"].rstrip("."), add["tool"]),
        "steps": [
            {"run": dict(required, dry_run=True),
             "expect": "ok: true, dry_run: true, would_run: %s with the arguments echoed; nothing started." % add["tool"]},
            {"run": {"bogus_argument": 1},
             "expect": "ok: false: names the unknown argument and the known ones; nothing started."},
            {"run": dict(required),
             "expect": "Asks to confirm when %s does; then it runs %s and the download shows in Menu > Downloads "
                       "under the '%s' provider." % (add["tool"], add["tool"], label)},
        ],
        "watch": ["From a remote chat guest the tool is refused: adding downloads is the owner's.",
                  "dl_providers lists this provider with add_tool: %s." % name],
        "group": group,
    }


def _normalised_checklist(provider, add, name, group, logger, filename):
    """The entry as checklist_schema keeps it (a plain-JSON copy with `group` filled in), so the
    generated tool is listed exactly like a written one."""
    entry = _checklist_entry(provider, add, name, group)
    try:
        from . import checklist_schema
        entries, _meta, problems = checklist_schema.extract_supplied({name: entry}, None, {name}, group)
        for problem in problems:
            logger("[tool-downloads] %s: %s" % (filename, problem))
        return entries.get(name, entry)
    except Exception:  # noqa: BLE001
        return entry


def prepare(raw_adds, provider_entries, tool_names, confirm_required, ai_review, group, filename, logger):
    """The generated tools for one tool file, as a list of dicts the loader keeps on the record until
    every file is known (see attach).  Never raises; a bad block is logged and skipped."""
    out = []
    for provider in provider_entries or []:
        block = (raw_adds or {}).get(provider["id"])
        if block is None:
            continue
        try:
            add, problems = validate_add(block, provider, tool_names)
            for p in problems:
                logger("[tool-downloads] %s: %s - dropped" % (filename, p))
            if add is None:
                continue
            name = NAME_PREFIX + provider["id"]
            if name in tool_names:
                logger("[tool-downloads] %s: %s is already one of this file's tools - not generated" % (filename, name))
                continue
            label = provider.get("label") or provider["id"]
            schema = {
                "name": name,
                "description": "%s Queues it in the Download Hub as '%s' (runs %s); dry_run only checks the "
                               "arguments." % (add["description"] if add["description"].endswith((".", "!", "?"))
                                               else add["description"] + ".", label, add["tool"]),
                "parameters": {"type": "object", "properties": _properties(add)},
            }
            if add["required"]:
                schema["parameters"]["required"] = list(add["required"])
            out.append({
                "name": name,
                "schema": schema,
                "handler": _make_handler(provider, add),
                "keywords": {"download from %s" % label.lower(): 9, "add a %s download" % label.lower(): 9},
                "checklist": _normalised_checklist(provider, add, name, group, logger, filename),
                "confirm": add["tool"] in confirm_required,
                "ai_review": add["tool"] in ai_review,
            })
        except Exception as exc:  # noqa: BLE001 - a bad block never takes the file down
            logger("[tool-downloads] %s: add block of %r failed (%s) - dropped" % (filename, provider.get("id"), exc))
    return out


def attach(records, reserved, logger):
    """Second pass of discover_actions: fold each valid record's generated tools into it, after the
    whole scan, so a hand-written tool of the same name (in any file) always wins.  The record's own
    containers are copied first: the module's TOOLS / TOOL_SCHEMAS are never touched, so a reload
    starts clean."""
    taken = set(reserved or ())
    for rec in records:
        if getattr(rec, "valid", False):
            taken |= set(rec.tools)
    for rec in records:
        gen = getattr(rec, "download_adds", None)
        if not getattr(rec, "valid", False) or not gen:
            continue
        schemas, tools, keywords = list(rec.schemas), dict(rec.tools), dict(rec.keywords)
        confirm, review, checklist = set(rec.confirm_required), set(rec.ai_review), dict(rec.checklist)
        for g in gen:
            if g["name"] in taken:
                logger("[tool-downloads] %s: a tool called %s already exists - the generated one is not added"
                       % (rec.file, g["name"]))
                continue
            taken.add(g["name"])
            schemas.append(g["schema"])
            tools[g["name"]] = g["handler"]
            keywords[g["name"]] = g["keywords"]
            checklist[g["name"]] = g["checklist"]
            if g["confirm"]:
                confirm.add(g["name"])
            if g["ai_review"]:
                review.add(g["name"])
        rec.schemas, rec.tools, rec.keywords = schemas, tools, keywords
        rec.confirm_required, rec.ai_review, rec.checklist = confirm, review, checklist
