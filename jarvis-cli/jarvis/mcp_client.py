"""MCP (Model Context Protocol) client — call tools that live in *other
people's* servers, not just jarvis's own actions/*.py.

An MCP server is a separate program exposing a set of tools over JSON-RPC:
a filesystem server, a GitHub server, a Postgres server, whatever someone
has written. This module speaks that protocol so those tools land in
jarvis's catalog next to the built-in ones, callable by the model in exactly
the same way.

THE FRESH-PROCESS PROBLEM, AGAIN
--------------------------------
Every other MCP client is a long-running host: it starts its servers once,
holds the connections open, and lists tools from memory. Jarvis is a brand
new OS process on every invocation, so doing that here would mean spawning
every configured server, handshaking with each one, and listing its tools —
on every single `jarvis ask`, before the model is even called. With three
servers that's several seconds of latency on a question that might not
involve MCP at all.

So discovery and execution are split:

  * The TOOL LIST is cached to disk (~/.jarvis/mcp_cache.json) and read at
    catalog-build time — no subprocess, no handshake, microseconds. It's
    refreshed explicitly (`jarvis mcp-refresh`) or when it ages past
    CACHE_TTL_SECONDS.
  * A TOOL CALL connects for real. Within one jarvis process the connection
    is pooled (see _POOL), so an ask that calls three tools on the same
    server pays the startup cost once, not three times.

This is the same trade the hybrid catalog tier makes for built-in tools: pay
a little staleness to avoid paying startup on every turn. The failure mode
is honest — a tool removed from a server since the last refresh returns a
clear "server no longer offers this tool" instead of hanging.

TRANSPORTS
----------
  stdio — the common case. The server is a local command (npx, uvx, a
          binary); we own its lifetime and talk newline-delimited JSON-RPC
          over its stdin/stdout. Its stderr is drained separately, because
          a server that logs chattily to stderr will deadlock on a full
          pipe buffer if nobody reads it — a genuinely nasty hang to debug.
  http  — a remote server speaking JSON-RPC over HTTP POST. Uses `requests`,
          already a base dependency.

SECURITY
--------
An MCP server is an arbitrary program, and its tools do arbitrary things.
Two deliberate consequences:

  1. Servers can only be added by editing ~/.jarvis/mcp_config.json (or the
     web console's config editor) — never by a model tool. Nothing the model
     can say should be able to introduce a new executable into the loop.
  2. MCP tools are confirm-gated by default. A server explicitly marked
     "trusted": true in the config opts out, per server, as a human choice.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "mcp_config.json"
CACHE_FILE = JARVIS_DIR / "mcp_cache.json"
ENCODING = "utf-8"

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "jarvis"

CACHE_TTL_SECONDS = 24 * 3600
CONNECT_TIMEOUT = 30       # handshake + tools/list
CALL_TIMEOUT = 120         # one tools/call
READ_POLL = 0.05
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_TOOLS_PER_SERVER = 120
MAX_RESULT_CHARS = 20000

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

DEFAULT_CONFIG = {
    "servers": {
        # Example only — disabled, so a fresh install starts with nothing
        # running. Copy the shape, set "enabled": true.
        "filesystem": {
            "enabled": False,
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "~/Documents"],
            "env": {},
            "trusted": False,
            "description": "Example stdio MCP server. Set enabled to true to use it.",
        },
    },
}


class MCPError(Exception):
    """Any failure talking to a server. Callers turn this into a tool result
    with an `error` key — never a raised exception out of a tool handler."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def ensure_config():
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n",
                                   encoding=ENCODING)
    except OSError:
        pass
    return CONFIG_FILE


def load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {"servers": {}}
    servers = data.get("servers") if isinstance(data, dict) else None
    return {"servers": servers if isinstance(servers, dict) else {}}


def _assign_slugs(configured):
    """Map every configured server's raw name to a unique sanitized slug.

    Two raw names can sanitize to the same slug ("My Server" and
    "my-server!" both become "my_server"). Previously `enabled_servers()`
    built its dict straight off `sanitize_name()`, so a collision meant
    the second entry silently clobbered the first at `out[name] = ...` —
    one whole server's tools vanished with no error, no log line, nothing
    in `jarvis mcp-status`. Whichever raw name happened to be iterated
    last "won", which also contradicted the comment in
    `actions/mcp_tools.py._build` claiming "first one registered wins".

    Fixed by disambiguating instead of colliding: servers are walked in
    config-file order (dict order, stable since Python 3.7 and since
    JSON objects preserve key order), the first raw name to produce a
    given slug keeps it, and every subsequent collision gets `_2`, `_3`,
    ... appended (re-clamped to the 32-char slug limit) until it's
    unique. Both servers stay usable — nothing is dropped, silently or
    otherwise — and this is the single place that decides slugs, so
    `enabled_servers()` and `status()` can't disagree about what a given
    server's tools are actually prefixed with.

    Returns {raw_name: (slug, renamed_due_to_collision)}.
    """
    seen = set()
    out = {}
    for raw_name in configured:
        base = sanitize_name(raw_name)
        if not base:
            out[raw_name] = ("", False)
            continue
        slug = base
        renamed = False
        suffix = 2
        while slug in seen:
            renamed = True
            candidate = "%s_%d" % (base[: 32 - len(str(suffix)) - 1], suffix)
            slug = candidate if _NAME_RE.match(candidate) else ""
            suffix += 1
            if not slug or suffix > 999:
                # Pathological: can't build a unique legal slug at all.
                # Treat like sanitize_name() failing outright — skipped
                # rather than silently colliding.
                slug = ""
                break
        if slug:
            seen.add(slug)
        out[raw_name] = (slug, renamed)
    return out


def enabled_servers():
    """Only servers explicitly switched on, keyed by a sanitized name.

    The name becomes part of every tool name this server contributes, so it
    has to survive _NAME_RE. A server whose name can't be sanitized is
    skipped rather than silently renamed to something the user never wrote.

    Two servers whose names sanitize to the same slug both stay enabled —
    see `_assign_slugs()` — rather than one silently disappearing.
    """
    configured = load_config().get("servers") or {}
    slugs = _assign_slugs(configured)
    out = {}
    for raw_name, spec in configured.items():
        if not isinstance(spec, dict) or not spec.get("enabled"):
            continue
        name, _renamed = slugs.get(raw_name, ("", False))
        if not name:
            continue
        out[name] = dict(spec, _raw_name=raw_name)
    return out


def sanitize_name(raw):
    slug = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = "s_" + slug if slug else ""
    return slug[:32] if _NAME_RE.match(slug[:32] or "") else ""


def tool_name_for(server, tool):
    """`mcp_<server>_<tool>`, clamped to the 64-char limit tool_loader
    enforces on every tool name. Prefixed so an MCP tool can never collide
    with a built-in — and so it's obvious in a trace where a call went."""
    base = "mcp_%s_%s" % (server, re.sub(r"[^a-z0-9]+", "_", str(tool).lower()).strip("_"))
    return base[:64].rstrip("_")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def load_cache():
    try:
        data = json.loads(CACHE_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(cache):
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, indent=2, default=str) + "\n", encoding=ENCODING)
        os.replace(str(tmp), str(CACHE_FILE))
        return True
    except OSError:
        return False


def cached_tools(server=None, include_stale=True):
    """Tool descriptors from the cache — the read the catalog does on every
    ask, so it must never touch a subprocess or the network."""
    cache = load_cache()
    out = {}
    now = time.time()
    for name, entry in cache.items():
        if server and name != server:
            continue
        if not isinstance(entry, dict):
            continue
        age = now - float(entry.get("fetched_at") or 0)
        if not include_stale and age > CACHE_TTL_SECONDS:
            continue
        out[name] = {
            "tools": entry.get("tools") or [],
            "fetched_at": entry.get("fetched_at"),
            "stale": age > CACHE_TTL_SECONDS,
            "error": entry.get("error"),
        }
    return out


def cache_is_stale():
    """True if any enabled server has no cache entry or an expired one —
    what `jarvis mcp-status` reports and what tells a user to refresh."""
    cache = load_cache()
    now = time.time()
    for name in enabled_servers():
        entry = cache.get(name)
        if not isinstance(entry, dict):
            return True
        if now - float(entry.get("fetched_at") or 0) > CACHE_TTL_SECONDS:
            return True
    return False


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class _StdioSession:
    """One live JSON-RPC conversation with a locally-spawned server."""

    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
        self.proc = None
        self._next_id = 0
        self._stderr_tail = []
        self._stderr_thread = None

    def start(self):
        command = (self.spec.get("command") or "").strip()
        if not command:
            raise MCPError("server %r has no 'command'" % self.name)
        args = [str(a) for a in (self.spec.get("args") or [])]
        # ~ in a path argument is extremely common in MCP config examples
        # (the official filesystem server's README uses it) and would
        # otherwise be passed through literally, since there's no shell here.
        args = [os.path.expanduser(a) if a.startswith("~") else a for a in args]

        env = dict(os.environ)
        for key, value in (self.spec.get("env") or {}).items():
            env[str(key)] = str(value)

        cwd = self.spec.get("cwd")
        cwd = os.path.expanduser(str(cwd)) if cwd else None

        try:
            self.proc = subprocess.Popen(
                [command] + args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, cwd=cwd, text=True, encoding=ENCODING, errors="replace",
                bufsize=1, creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, ValueError) as e:
            raise MCPError("couldn't start %r (%s): %s" % (self.name, command, e))

        # Drain stderr on a daemon thread. Without this, a server that logs
        # freely to stderr fills the pipe buffer and blocks forever on its
        # next write — which looks exactly like a hung handshake and is
        # miserable to diagnose. The tail is kept for error messages.
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        return self

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr_tail.append(line.rstrip())
                del self._stderr_tail[:-20]
        except (OSError, ValueError):
            pass

    def stderr_tail(self):
        return "\n".join(self._stderr_tail[-8:])

    def _send(self, payload):
        if not self.proc or self.proc.poll() is not None:
            raise MCPError("server %r is not running (%s)" % (self.name, self.stderr_tail()))
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise MCPError("couldn't write to %r: %s" % (self.name, e))

    def _read_response(self, want_id, timeout):
        """Read until the response with `want_id` arrives.

        Notifications and unrelated responses are skipped rather than
        treated as errors: a server is free to emit logging notifications
        mid-call, and rejecting those would break perfectly valid servers.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise MCPError("server %r exited (%s)" % (self.name, self.stderr_tail()))
            line = self.proc.stdout.readline()
            if not line:
                time.sleep(READ_POLL)
                continue
            if len(line) > MAX_LINE_BYTES:
                raise MCPError("server %r sent an oversized message" % self.name)
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Some servers print a banner before speaking protocol.
                # Skipping non-JSON is more useful than failing outright.
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") != want_id:
                continue
            if "error" in message and message["error"]:
                err = message["error"]
                raise MCPError("%s: %s" % (
                    self.name, err.get("message") if isinstance(err, dict) else err))
            return message.get("result") or {}
        raise MCPError("server %r timed out after %ss" % (self.name, timeout))

    def request(self, method, params=None, timeout=CONNECT_TIMEOUT):
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params or {}})
        return self._read_response(request_id, timeout)

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def handshake(self):
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": CLIENT_NAME, "version": _client_version()},
        })
        # Per spec the client must confirm before issuing further requests;
        # several servers legitimately refuse tools/list until they see it.
        self.notify("notifications/initialized")
        return result

    def close(self):
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None


class _HttpSession:
    """Remote server speaking JSON-RPC over HTTP POST. Stateless per
    request, so there's nothing to keep alive and close() is a no-op."""

    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
        self.url = (spec.get("url") or "").strip()
        self._next_id = 0

    def start(self):
        if not self.url.startswith(("http://", "https://")):
            raise MCPError("server %r needs a http(s) 'url'" % self.name)
        return self

    def request(self, method, params=None, timeout=CONNECT_TIMEOUT):
        try:
            import requests
        except ImportError:
            raise MCPError("http MCP servers need the 'requests' package")
        self._next_id += 1
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update({str(k): str(v) for k, v in (self.spec.get("headers") or {}).items()})
        try:
            response = requests.post(
                self.url, headers=headers, timeout=timeout,
                json={"jsonrpc": "2.0", "id": self._next_id, "method": method,
                      "params": params or {}},
            )
        except Exception as e:  # noqa: BLE001 — requests raises many types
            raise MCPError("%s: %s" % (self.name, e))
        if response.status_code >= 400:
            raise MCPError("%s: HTTP %s" % (self.name, response.status_code))
        try:
            message = response.json()
        except ValueError:
            raise MCPError("%s: response wasn't JSON" % self.name)
        if isinstance(message, dict) and message.get("error"):
            err = message["error"]
            raise MCPError("%s: %s" % (
                self.name, err.get("message") if isinstance(err, dict) else err))
        return (message or {}).get("result") or {}

    def notify(self, method, params=None):
        try:
            self.request(method, params, timeout=5)
        except MCPError:
            pass  # notifications are fire-and-forget by definition

    def handshake(self):
        return self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": CLIENT_NAME, "version": _client_version()},
        })

    def stderr_tail(self):
        return ""

    def close(self):
        return None


def _client_version():
    try:
        from . import build_info
        return str(build_info.version_string())[:32]
    except Exception:  # noqa: BLE001
        return "0"


# ---------------------------------------------------------------------------
# Connection pool — one live session per server, per jarvis process
# ---------------------------------------------------------------------------

_POOL = {}


def _session_for(name, spec):
    """Reuse a session within this process. An ask that calls three tools on
    the same server pays the spawn+handshake once; a process that calls none
    never spawns anything."""
    session = _POOL.get(name)
    if session is not None:
        proc = getattr(session, "proc", None)
        if proc is None or proc.poll() is None:
            return session
        _POOL.pop(name, None)  # died since last use — fall through and respawn

    transport = (spec.get("transport") or "stdio").strip().lower()
    if transport == "stdio":
        session = _StdioSession(name, spec)
    elif transport in ("http", "https", "sse"):
        session = _HttpSession(name, spec)
    else:
        raise MCPError("server %r has unknown transport %r" % (name, transport))
    session.start()
    session.handshake()
    _POOL[name] = session
    return session


def close_all():
    """Shut every pooled session down. Called from the CLI on the way out —
    an orphaned stdio child would otherwise outlive the jarvis process that
    spawned it and sit there holding a pipe open."""
    for name in list(_POOL):
        try:
            _POOL.pop(name).close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def list_tools(name, spec=None):
    """Live tools/list against one server. Slow (spawn + handshake) — this is
    the refresh path, not the read path."""
    spec = spec or enabled_servers().get(name)
    if not spec:
        raise MCPError("no enabled MCP server named %r" % name)
    session = _session_for(name, spec)
    result = session.request("tools/list", {}, timeout=CONNECT_TIMEOUT)
    tools = result.get("tools")
    if not isinstance(tools, list):
        return []
    out = []
    for tool in tools[:MAX_TOOLS_PER_SERVER]:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        out.append({
            "name": str(tool["name"]),
            "description": str(tool.get("description") or "").strip(),
            "input_schema": tool.get("inputSchema") or tool.get("input_schema") or
                            {"type": "object", "properties": {}},
        })
    return out


def refresh(server=None):
    """Re-list tools for every enabled server (or one) and rewrite the cache.

    A server that fails is recorded with its error rather than dropped, so
    `mcp-status` can show "github: command not found" instead of silently
    showing nothing — the difference between a diagnosable problem and a
    mystery.
    """
    servers = enabled_servers()
    if server:
        servers = {k: v for k, v in servers.items() if k == server}
        if not servers:
            raise MCPError("no enabled MCP server named %r" % server)

    cache = load_cache()
    summary = []
    for name, spec in servers.items():
        entry = {"fetched_at": time.time(), "tools": [], "error": None,
                 "transport": spec.get("transport") or "stdio"}
        try:
            entry["tools"] = list_tools(name, spec)
        except MCPError as e:
            entry["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            entry["error"] = "%s: %s" % (type(e).__name__, e)
        cache[name] = entry
        summary.append({"server": name, "tools": len(entry["tools"]),
                        "error": entry["error"]})

    # Drop cache entries for servers that are gone or disabled, so a stale
    # tool from a server you turned off months ago can't still show up in
    # the catalog.
    for stale_name in [n for n in cache if n not in enabled_servers()]:
        cache.pop(stale_name, None)

    save_cache(cache)
    close_all()
    return summary


def call_tool(server, tool, arguments=None, timeout=CALL_TIMEOUT):
    """Invoke one tool on one server and flatten its result.

    MCP returns a list of typed content blocks; the model wants text. Text
    blocks are joined; anything else is described rather than dropped, so a
    tool that returns an image doesn't come back as a confusing empty
    string.
    """
    spec = enabled_servers().get(server)
    if not spec:
        raise MCPError("MCP server %r isn't enabled" % server)
    session = _session_for(server, spec)
    result = session.request("tools/call",
                             {"name": tool, "arguments": arguments or {}},
                             timeout=timeout)

    blocks = result.get("content")
    if not isinstance(blocks, list):
        return {"ok": True, "result": result}

    texts, others = [], []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        else:
            others.append(block.get("type") or "unknown")

    payload = {"ok": not result.get("isError")}
    text = "\n".join(t for t in texts if t).strip()
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\u2026(truncated)"
    if text:
        payload["text"] = text
    if others:
        payload["non_text_content"] = others
    if result.get("isError"):
        payload["error"] = text or "the tool reported an error"
    if not text and not others:
        payload["note"] = "the tool returned no content"
    return payload


def status():
    """Everything `jarvis mcp-status` and the web panel need in one read —
    no subprocess, so it's safe to call from a UI poll."""
    configured = load_config().get("servers") or {}
    slugs = _assign_slugs(configured)
    servers = enabled_servers()
    cache = load_cache()
    out = []
    collisions = []
    for raw_name, spec in configured.items():
        name, renamed = slugs.get(raw_name, ("", False))
        entry = cache.get(name) or {}
        fetched = entry.get("fetched_at")
        row = {
            "name": raw_name,
            "slug": name,
            "enabled": bool(spec.get("enabled")) if isinstance(spec, dict) else False,
            "transport": (spec.get("transport") if isinstance(spec, dict) else None) or "stdio",
            "trusted": bool(spec.get("trusted")) if isinstance(spec, dict) else False,
            "tool_count": len(entry.get("tools") or []),
            "last_refreshed": fetched,
            "stale": bool(fetched) and (time.time() - float(fetched)) > CACHE_TTL_SECONDS,
            "error": entry.get("error"),
        }
        if renamed:
            # Made visible on purpose — this used to be the exact case that
            # silently dropped a whole server's tools (see _assign_slugs).
            row["renamed_due_to_collision"] = True
            collisions.append({"name": raw_name, "slug": name})
        out.append(row)
    result = {
        "servers": out,
        "enabled_count": len(servers),
        "total_tools": sum(len((cache.get(n) or {}).get("tools") or []) for n in servers),
        "needs_refresh": cache_is_stale(),
        "config_file": str(CONFIG_FILE),
    }
    if collisions:
        result["name_collisions"] = collisions
    return result
