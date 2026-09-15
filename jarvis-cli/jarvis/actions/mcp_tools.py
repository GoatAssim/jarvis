"""mcp — external MCP server tools, registered into jarvis's own catalog.

Every other actions/*.py file has a TOOL_SCHEMAS list written by hand. This
one BUILDS its list at import time from ~/.jarvis/mcp_cache.json, so the
tools a configured MCP server offers become ordinary jarvis tools: routed by
tool_router, gated by tool_safety, shaped by tool_result_shaping, callable
from the debug dashboard, indistinguishable at the call site from a built-in.

That's the whole point of the generic tool system — tool_loader doesn't care
where a schema came from, only that the module exposes the contract — so
plugging a foreign protocol in needs no changes to tools.py, tool_loader.py,
or anything else.

WHY IT READS A CACHE AND NEVER CONNECTS HERE
--------------------------------------------
This module's top level runs during tools.py's import, i.e. on EVERY jarvis
invocation, before the model is called. Connecting to servers here would put
a subprocess spawn and a protocol handshake per server on the startup path
of every command — several seconds, on questions that may have nothing to do
with MCP.

So import time reads a JSON file and nothing else. The cache is populated by
`jarvis mcp-refresh` (or automatically the first time an MCP tool is
actually called with a stale cache — see _ensure_fresh). A stale entry is
still served: an out-of-date tool list that mostly works beats an empty
catalog, and a genuinely removed tool fails with a clear message when
called.

TWO KINDS OF TOOL LIVE HERE
---------------------------
  mcp_<server>_<tool>  — one per discovered tool, the real work.
  mcp_list_servers     — always present, so the model can explain what's
                         connected (and say something useful when nothing
                         is). Also the one thing worth having when the
                         cache is empty, which is exactly when a user is
                         most likely to be asking why.
"""

from .. import mcp_client

# Populated by _build() below. Module-level names are the tool_loader
# contract, so they have to exist even when no server is configured — a
# file that conditionally omits TOOL_SCHEMAS would be silently treated as a
# non-action helper module rather than reported as a problem.
TOOL_SCHEMAS = []
TOOLS = {}
TOOL_KEYWORDS = {}
TOOL_CONFIRM_REQUIRED = set()
TOOL_AI_REVIEW = set()
TOOL_RESULT_SPECS = {}

TOOL_GROUP = "mcp"

TOOL_PACK_INSTRUCTION = (
    "mcp_* tools come from external MCP servers the user has connected, not from "
    "Jarvis itself. Call them exactly like any other tool. If one fails with "
    "'server no longer offers this tool' or 'isn't enabled', the cached tool list "
    "is out of date — tell the user to run `jarvis mcp-refresh`, don't retry in a "
    "loop. Use mcp_list_servers when asked what's connected or why an MCP tool is "
    "missing."
)


def _ensure_fresh():
    """Refresh the cache if it's expired, at CALL time rather than import
    time.

    This is the one place a connection is allowed to happen implicitly, and
    only because the user has already committed to an MCP call by this
    point — the latency is attributable to something they asked for, rather
    than tacked onto an unrelated `jarvis what's the weather`.
    """
    if not mcp_client.cache_is_stale():
        return
    try:
        mcp_client.refresh()
    except Exception:  # noqa: BLE001 — a refresh failure must not block the
        # call itself; the stale entry may well still be correct, and if it
        # isn't, the call below returns a far more specific error than this
        # refresh could.
        pass


def _make_handler(server, remote_tool):
    """One closure per discovered tool.

    Bound as default arguments rather than closed-over loop variables —
    Python's late binding would otherwise give every handler the last
    iteration's server/tool, a bug that only shows up once a second server
    is configured.
    """
    def handler(args, _server=server, _tool=remote_tool):
        _ensure_fresh()
        try:
            return mcp_client.call_tool(_server, _tool, args or {})
        except mcp_client.MCPError as e:
            return {"error": str(e), "server": _server, "tool": _tool}
        except Exception as e:  # noqa: BLE001 — a handler must never raise
            return {"error": "%s: %s" % (type(e).__name__, e),
                    "server": _server, "tool": _tool}
    return handler


def tool_mcp_list_servers(args):
    args = args or {}
    try:
        info = mcp_client.status()
    except Exception as e:  # noqa: BLE001
        return {"error": "couldn't read MCP config: %s" % e}
    if args.get("include_tools"):
        cached = mcp_client.cached_tools()
        for server in info["servers"]:
            entry = cached.get(server["slug"]) or {}
            server["tools"] = [t.get("name") for t in entry.get("tools") or []]
    if not info["servers"]:
        info["note"] = (
            "No MCP servers are configured. The user adds them by editing "
            "%s — you can't add one yourself." % info["config_file"]
        )
    elif not info["enabled_count"]:
        info["note"] = "MCP servers are configured but all of them are disabled."
    elif info.get("name_collisions"):
        renamed = ", ".join("%s -> mcp_%s_*" % (c["name"], c["slug"]) for c in info["name_collisions"])
        info["note"] = (
            "Two or more configured servers sanitize to the same tool-name "
            "prefix; the later ones were automatically renamed so none of "
            "them lost their tools: %s. Tell the user if they'd rather "
            "rename a server in the config for a cleaner prefix." % renamed
        )
    return info


def _schema_for(server, tool, trusted):
    """Translate one MCP tool descriptor into a jarvis schema.

    MCP's `inputSchema` is already JSON Schema, which is the same shape
    every provider adapter expects in `parameters`, so this is mostly a
    rename — the interesting part is the description, which is prefixed
    with the server name so the model can tell a foreign tool from a
    built-in when choosing between them.
    """
    name = mcp_client.tool_name_for(server, tool["name"])
    described = tool.get("description") or ("%s tool from the %s MCP server"
                                            % (tool["name"], server))
    schema = tool.get("input_schema") or {"type": "object", "properties": {}}
    if not isinstance(schema, dict) or schema.get("type") != "object":
        # A few servers declare a non-object top level, which no provider
        # will accept as a function signature. Wrapping is wrong (it would
        # change the argument shape the server expects), so this degrades to
        # an open object and lets the server validate.
        schema = {"type": "object", "properties": {}}
    return {
        "name": name,
        "description": "[MCP: %s] %s" % (server, described.strip()),
        "parameters": schema,
    }, name


def _build():
    """Assemble the module-level contract from the cache. Never raises — a
    corrupt cache file must not take the whole tool catalog down with it."""
    schemas, tools, keywords, confirm = [], {}, {}, set()

    try:
        servers = mcp_client.enabled_servers()
        cached = mcp_client.cached_tools()
    except Exception:  # noqa: BLE001
        servers, cached = {}, {}

    for server, spec in servers.items():
        entry = cached.get(server) or {}
        trusted = bool(spec.get("trusted"))
        for tool in entry.get("tools") or []:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            try:
                schema, name = _schema_for(server, tool, trusted)
            except Exception:  # noqa: BLE001
                continue
            if name in tools:
                # Cross-server slug collisions can't reach here any more —
                # mcp_client._assign_slugs() disambiguates them (mcp-status
                # surfaces it as name_collisions/renamed_due_to_collision
                # instead of it happening silently). This guard is now only
                # for the same server listing the same tool name twice,
                # which is a malformed server response, not a collision
                # between servers — first occurrence wins, harmlessly.
                continue
            schemas.append(schema)
            tools[name] = _make_handler(server, tool["name"])
            # Route on the server name and on the remote tool's own words.
            # Weight 6 clears tool_router.MIN_SCORE (5) without outbidding a
            # built-in's deliberate 9/10 — an MCP tool should be reachable,
            # not preferred over a first-party tool that does the same job.
            keywords[name] = {server: 6, str(tool["name"]).replace("_", " "): 6}
            if not trusted:
                # An MCP server is someone else's arbitrary program. Until a
                # human marks it trusted in mcp_config.json, its tools get
                # the same out-of-band confirmation gate write_file and
                # run_command get.
                confirm.add(name)

    schemas.append({
        "name": "mcp_list_servers",
        "description": (
            "List the external MCP servers connected to Jarvis, how many tools "
            "each provides, and whether their cached tool list is stale. Use when "
            "the user asks what's connected, what a server can do, or why an "
            "mcp_* tool isn't available."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "include_tools": {
                    "type": "boolean",
                    "description": "Also list each server's tool names. Default false.",
                },
            },
        },
    })
    tools["mcp_list_servers"] = tool_mcp_list_servers
    keywords["mcp_list_servers"] = {
        "mcp": 9, "mcp server": 10, "mcp servers": 10,
        "connected servers": 8, "model context protocol": 10,
    }

    # Every MCP result is text of unknown length from a foreign program, so
    # they all get the same trim rather than a per-tool spec that can't be
    # written in advance for tools this file has never seen.
    specs = {
        name: {"truncate_fields": {"text": {"medium": 4000, "low": 1200}}}
        for name in tools if name != "mcp_list_servers"
    }
    specs["mcp_list_servers"] = {
        "list_item_drop": {
            "servers": {
                "medium": ["last_refreshed", "transport"],
                "low": ["last_refreshed", "transport", "trusted", "stale"],
            },
        },
    }
    return schemas, tools, keywords, confirm, specs


TOOL_SCHEMAS, TOOLS, TOOL_KEYWORDS, TOOL_CONFIRM_REQUIRED, TOOL_RESULT_SPECS = _build()
