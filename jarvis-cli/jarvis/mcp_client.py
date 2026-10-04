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


def _redact_url(url):
    """(display_url, was_redacted) for a server URL.

    A remote MCP URL very often carries a credential — userinfo, or a
    `?token=` / `?key=` query — and /api/mcp is read by the browser, logged
    by the web server's debugging, and pasted into bug reports. So the panel
    only ever gets scheme://host[:port]/path, with "?…" standing in for a
    query. The full URL stays in mcp_config.json; an edit that leaves the
    URL box untouched sends `null`, which save_server() reads as "keep it".
    """
    from urllib.parse import urlsplit
    text = str(url or "").strip()
    if not text:
        return "", False
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        if ":" in host:
            host = "[%s]" % host
        if parts.port:
            host = "%s:%d" % (host, parts.port)
    except ValueError:
        return "(unreadable URL)", True
    if not parts.scheme or not host:
        return "(unreadable URL)", True
    shown = "%s://%s%s" % (parts.scheme, host, parts.path or "")
    redacted = bool(parts.username or parts.password or parts.query or parts.fragment)
    if parts.query:
        shown += "?\u2026"
    return shown, redacted


def _server_state(enabled, problems, entry, stale):
    """One word for how a server is doing, decided in ONE place so the panel,
    `jarvis mcp-status` and anything later agree.

      disabled       switched off in the config
      misconfigured  switched on but the definition can't start (no command…)
      error          the last refresh failed (the error text is on the row)
      not_refreshed  switched on, never listed — a refresh is needed
      stale          the cached tool list is older than CACHE_TTL_SECONDS
      ok             switched on and the last refresh succeeded
    """
    if not enabled:
        return "disabled"
    if problems:
        return "misconfigured"
    if entry.get("error"):
        return "error"
    if not entry.get("fetched_at"):
        return "not_refreshed"
    if stale:
        return "stale"
    return "ok"


def _disabled_tool_names():
    """Tools switched off in the Tool Manager (L.25), so the MCP panel can say
    'plugged in but the model can't use 3 of its 9 tools'. Best effort: a
    missing or unreadable switch file just means nothing is marked."""
    try:
        from . import tool_disable
        return set(tool_disable.disabled_tools())
    except Exception:  # noqa: BLE001
        return set()


def status():
    """Everything `jarvis mcp-status` and the web panel need in one read —
    no subprocess, so it's safe to call from a UI poll.

    The original fields are unchanged; L.31 only ADDS per-server fields
    (`state`, `problems`, `description`, `command`, `args`, `cwd`, `url`,
    `url_redacted`, `env_keys`, `tools`, `disabled_tool_count`) so an older
    reader keeps working. Secrets stay out: environment VALUES are never
    returned (only the variable names) and a URL's userinfo/query is masked.
    """
    configured = load_config().get("servers") or {}
    slugs = _assign_slugs(configured)
    servers = enabled_servers()
    cache = load_cache()
    off = _disabled_tool_names()
    now = time.time()
    out = []
    collisions = []
    for raw_name, spec in configured.items():
        name, renamed = slugs.get(raw_name, ("", False))
        entry = cache.get(name) or {}
        if not isinstance(entry, dict):
            entry = {}
        good = isinstance(spec, dict)
        fetched = entry.get("fetched_at")
        stale = bool(fetched) and (now - float(fetched)) > CACHE_TTL_SECONDS
        enabled = bool(spec.get("enabled")) if good else False
        problems = validate_server_spec(spec) if good else ["the definition isn't a JSON object"]
        if not name:
            problems = ["the name can't be turned into a tool prefix (use letters or digits)"] + problems
        shown_url, url_redacted = _redact_url(spec.get("url")) if good else ("", False)
        tools = []
        for tool in (entry.get("tools") or []):
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            full = tool_name_for(name, tool["name"])
            tools.append({
                "name": str(tool["name"]),
                "tool_name": full,
                "description": str(tool.get("description") or "").strip()[:300],
                "disabled": full in off,
            })
        row = {
            "name": raw_name,
            "slug": name,
            "enabled": enabled,
            "transport": (spec.get("transport") if good else None) or "stdio",
            "trusted": bool(spec.get("trusted")) if good else False,
            "tool_count": len(entry.get("tools") or []),
            "last_refreshed": fetched,
            "stale": stale,
            "error": entry.get("error"),
            # --- L.31 additions -------------------------------------------
            "state": _server_state(enabled, problems, entry, stale),
            "problems": problems,
            "description": str(spec.get("description") or "") if good else "",
            "command": str(spec.get("command") or "") if good else "",
            "args": [str(a) for a in (spec.get("args") or [])] if good and isinstance(spec.get("args"), list) else [],
            "cwd": str(spec.get("cwd") or "") if good else "",
            "url": shown_url,
            "url_redacted": url_redacted,
            "env_keys": sorted(str(k) for k in (spec.get("env") or {})) if good and isinstance(spec.get("env"), dict) else [],
            "tools": tools,
            "disabled_tool_count": sum(1 for t in tools if t["disabled"]),
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


# ---------------------------------------------------------------------------
# Editing the config (L.31) — a HUMAN action, never a model tool
# ---------------------------------------------------------------------------
#
# The security rule at the top of this file is unchanged: nothing the model
# can say may introduce a new executable. Everything below is reachable only
# from the `jarvis mcp-edit` CLI verb (typed by the owner, or sent by the web
# panel's own buttons) — there is deliberately no entry in any TOOL_SCHEMAS
# and `actions/mcp_tools.py` imports none of it. tests/test_mcp_edit.py pins
# that.

MAX_NAME_CHARS = 64
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUNTIME_TRANSPORTS = ("stdio", "http", "https", "sse")
# The keys the editor owns. Anything else in a server's entry (a hand-added
# "headers", a note) is carried through an edit untouched.
_EDIT_KEYS = ("enabled", "transport", "command", "args", "env", "cwd", "url",
              "trusted", "description")
_FLAG_KEYS = ("enabled", "trusted")


class ConfigEditError(MCPError):
    """A config edit that was refused. Nothing was written."""


def _connection_signature(spec):
    """What makes a server THIS server, normalised so that a missing key and
    its empty default compare equal (an old hand-written entry with no "env"
    must not look 'changed' just because the editor writes "env": {})."""
    spec = spec if isinstance(spec, dict) else {}
    transport = spec.get("transport")
    args = spec.get("args")
    env = spec.get("env")
    return (
        "stdio" if transport in (None, "") else str(transport).strip().lower(),
        str(spec.get("command") or "").strip(),
        [] if args is None else [str(a) for a in args] if isinstance(args, list) else args,
        {} if env is None else {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else env,
        str(spec.get("cwd") or ""),
        str(spec.get("url") or "").strip(),
    )


def validate_server_spec(spec):
    """List of human-readable problems with one server definition (empty when
    it can start). Shared by the editor (strict, refuses to save) and
    status() (shows the problem on the card), so the two never disagree."""
    if not isinstance(spec, dict):
        return ["the server definition must be a JSON object"]
    problems = []
    raw_transport = spec.get("transport")
    transport = "stdio" if raw_transport in (None, "") else str(raw_transport).strip().lower()
    if transport not in _RUNTIME_TRANSPORTS:
        problems.append("transport must be \"stdio\" or \"http\" (got %r)" % transport)
    elif transport == "stdio":
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            problems.append("a stdio server needs a command (for example npx or uvx)")
        elif len(command) > 500 or "\n" in command or "\x00" in command:
            problems.append("the command must be one line of at most 500 characters")
    else:
        url = spec.get("url")
        if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
            problems.append("an http server needs a url starting with http:// or https://")
        elif len(url.strip()) > 2000 or re.search(r"\s", url.strip()):
            problems.append("the url must be a single token of at most 2000 characters, with no spaces")

    args = spec.get("args")
    if args is not None:
        if not isinstance(args, list):
            problems.append("args must be a list")
        elif len(args) > 100:
            problems.append("at most 100 arguments")
        else:
            for item in args:
                if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                    problems.append("every argument must be text or a number")
                    break
                if len(str(item)) > 2000 or "\x00" in str(item):
                    problems.append("an argument is over 2000 characters or contains a NUL")
                    break

    env = spec.get("env")
    if env is not None:
        if not isinstance(env, dict):
            problems.append("env must be an object of NAME: value pairs")
        elif len(env) > 100:
            problems.append("at most 100 environment variables")
        else:
            for key, value in env.items():
                if not _ENV_KEY_RE.match(str(key)):
                    problems.append("%r isn't a valid environment variable name" % key)
                    break
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    problems.append("the value of %s must be text" % key)
                    break
                if len(str(value)) > 4000 or "\x00" in str(value):
                    problems.append("the value of %s is over 4000 characters or contains a NUL" % key)
                    break

    cwd = spec.get("cwd")
    if cwd not in (None, "") and (not isinstance(cwd, str) or len(cwd) > 1000 or "\x00" in cwd):
        problems.append("cwd must be a path of at most 1000 characters")
    description = spec.get("description")
    if description not in (None, "") and (not isinstance(description, str) or len(description) > 300):
        problems.append("the description must be text of at most 300 characters")
    return problems


def _clean_name(raw):
    name = str(raw if raw is not None else "").strip()
    if not name:
        raise ConfigEditError("give the server a name")
    if len(name) > MAX_NAME_CHARS:
        raise ConfigEditError("the name is longer than %d characters" % MAX_NAME_CHARS)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ConfigEditError("the name can't contain control characters")
    if not sanitize_name(name):
        # The name becomes part of every tool name (mcp_<name>_<tool>), so it
        # has to contain at least one letter or digit.
        raise ConfigEditError("the name needs at least one letter or digit — it becomes "
                              "part of every tool name this server adds")
    return name


def _read_config_for_edit():
    """The whole parsed config, for a read-modify-write.

    Unlike load_config() this refuses to carry on when the file exists but
    isn't valid JSON: load_config() quietly returns "no servers", and writing
    on top of that would wipe a hand-edited file the owner merely made a typo
    in. A missing file is fine — it starts empty."""
    try:
        text = CONFIG_FILE.read_text(encoding=ENCODING)
    except FileNotFoundError:
        return {"servers": {}}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigEditError("couldn't read %s: %s" % (CONFIG_FILE, e))
    if not text.strip():
        return {"servers": {}}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigEditError(
            "%s isn't valid JSON (line %d, column %d) — fix it by hand first; "
            "nothing was changed" % (CONFIG_FILE.name, e.lineno, e.colno))
    if not isinstance(data, dict):
        raise ConfigEditError("%s must contain a JSON object — nothing was changed" % CONFIG_FILE.name)
    if not isinstance(data.get("servers"), dict):
        if "servers" in data and data["servers"] not in (None, {}):
            raise ConfigEditError("\"servers\" in %s must be an object — nothing was changed" % CONFIG_FILE.name)
        data["servers"] = {}
    return data


def _write_config(data):
    from . import atomic_io
    if not atomic_io.write_json(CONFIG_FILE, data):
        raise ConfigEditError("couldn't write %s (disk full or read-only?) — nothing was changed" % CONFIG_FILE)


def _drop_cache(*slugs):
    """Forget cached tool lists. A server that was switched off, edited or
    removed must not keep offering the tools it listed last time."""
    cache = load_cache()
    changed = False
    for slug in slugs:
        if slug and slug in cache:
            cache.pop(slug)
            changed = True
    if changed:
        save_cache(cache)
    return changed


def _forget_tool_switches(slug):
    """On removal, drop the Tool Manager's on/off switches for the server's
    tools, so adding a server with the same name later starts clean (the same
    courtesy deleting a saved command gets)."""
    try:
        from . import tool_disable
        for tool in ((load_cache().get(slug) or {}).get("tools") or []):
            if isinstance(tool, dict) and tool.get("name"):
                tool_disable.forget_tool(tool_name_for(slug, tool["name"]))
    except Exception:  # noqa: BLE001 — cosmetic; never fail a removal over it
        pass


def save_server(name, spec, replace=None):
    """Add a server (replace=None) or edit/rename one (replace=<its current
    name>). Returns a small result dict; raises ConfigEditError, writing
    nothing, on any problem.

    `None` for a URL or an environment value means "keep what's there" — the
    panel never receives those values (see _redact_url), so an edit form
    can't send them back, and a blank box must not wipe a secret.

    A new server starts switched off and confirm-gated unless the caller says
    otherwise; an edit keeps the existing switches unless told.
    """
    name = _clean_name(name)
    if not isinstance(spec, dict):
        raise ConfigEditError("the server definition must be a JSON object")
    data = _read_config_for_edit()
    servers = data["servers"]
    if replace is not None:
        replace = str(replace)
        if replace not in servers:
            raise ConfigEditError("there is no server named %r to edit" % replace)
    elif name in servers:
        raise ConfigEditError("a server named %r already exists — edit it instead" % name)
    if replace is not None and name != replace and name in servers:
        raise ConfigEditError("another server is already named %r" % name)
    for other in servers:
        if other != replace and other != name and sanitize_name(other) == sanitize_name(name):
            raise ConfigEditError(
                "%r would share the tool prefix mcp_%s_ with the existing server %r — "
                "pick a name that differs in letters or digits" % (name, sanitize_name(name), other))

    previous = servers.get(replace) if replace is not None else None
    previous = previous if isinstance(previous, dict) else {}

    transport = spec.get("transport") if spec.get("transport") not in (None, "") else previous.get("transport")
    transport = "stdio" if transport in (None, "") else str(transport).strip().lower()
    new = {k: v for k, v in previous.items() if k not in _EDIT_KEYS}  # carried-through extras
    new["enabled"] = bool(spec["enabled"]) if "enabled" in spec else bool(previous.get("enabled", False))
    new["transport"] = transport
    if transport == "stdio":
        # A key the caller left OUT is kept as it was; a key it sent is the
        # new value. (So a one-field edit over the CLI can't wipe the rest.)
        new["command"] = spec["command"] if "command" in spec else previous.get("command")
        new["args"] = spec["args"] if "args" in spec and spec["args"] is not None else (
            [] if "args" in spec else previous.get("args", []))
        old_env = previous.get("env") if isinstance(previous.get("env"), dict) else {}
        env_in = spec["env"] if "env" in spec else dict(old_env)
        if env_in is None:
            env_in = {}
        if isinstance(env_in, dict):
            env_out = {}
            for key, value in env_in.items():
                if value is None:
                    if key not in old_env:
                        raise ConfigEditError("%s has no stored value to keep — type one" % key)
                    env_out[key] = old_env[key]
                else:
                    env_out[key] = value
            new["env"] = env_out
        else:
            new["env"] = env_in  # not a dict: validate_server_spec reports it below
        cwd = spec["cwd"] if "cwd" in spec else previous.get("cwd")
        if cwd:
            new["cwd"] = cwd
    else:
        url = spec.get("url")
        if url is None:  # left out, or null = "keep the stored one" (see _redact_url)
            url = previous.get("url")
        new["url"] = url
    new["trusted"] = bool(spec["trusted"]) if "trusted" in spec else bool(previous.get("trusted", False))
    description = spec["description"] if "description" in spec else previous.get("description")
    if description:
        new["description"] = description

    problems = validate_server_spec(new)
    if problems:
        raise ConfigEditError("; ".join(problems))
    if isinstance(new.get("command"), str):
        new["command"] = new["command"].strip()
    if isinstance(new.get("url"), str):
        new["url"] = new["url"].strip()
    if isinstance(new.get("args"), list):
        new["args"] = [a if isinstance(a, str) else str(a) for a in new["args"]]

    old_slug = _assign_slugs(servers).get(replace, ("", False))[0] if replace is not None else ""
    # Rebuild the dict so a rename keeps the server where it was in the file.
    rebuilt = {}
    if replace is None:
        rebuilt.update(servers)
        rebuilt[name] = new
    else:
        for key, value in servers.items():
            if key == replace:
                rebuilt[name] = new
            else:
                rebuilt[key] = value
    data["servers"] = rebuilt
    new_slug = _assign_slugs(rebuilt).get(name, ("", False))[0]

    connection_changed = (replace is None or name != replace
                          or _connection_signature(previous) != _connection_signature(new))
    _write_config(data)
    if replace is not None and name != replace:
        # A rename changes every tool name, so the Tool Manager's switches
        # for the old names would point at tools that no longer exist.
        _forget_tool_switches(old_slug)
    if replace is not None and (connection_changed or not new["enabled"]):
        _drop_cache(old_slug)
    if connection_changed or not new["enabled"]:
        _drop_cache(new_slug)
    return {
        "ok": True, "action": "save", "name": name, "slug": new_slug,
        "created": replace is None, "enabled": new["enabled"], "trusted": new["trusted"],
        "needs_refresh": bool(new["enabled"] and connection_changed),
    }


def set_server_flag(name, key, value):
    """Switch one server on/off (`enabled`) or trusted/confirm-gated
    (`trusted`). Switching off drops its cached tools at once, so they leave
    the catalog on the very next jarvis process rather than after a refresh."""
    if key not in _FLAG_KEYS:
        raise ConfigEditError("only %s can be switched here" % " and ".join(_FLAG_KEYS))
    data = _read_config_for_edit()
    servers = data["servers"]
    name = str(name)
    if name not in servers:
        raise ConfigEditError("there is no server named %r" % name)
    spec = servers[name]
    if not isinstance(spec, dict):
        raise ConfigEditError("the entry for %r isn't a JSON object — fix it by hand" % name)
    slug = _assign_slugs(servers).get(name, ("", False))[0]
    was = bool(spec.get(key))
    spec[key] = bool(value)
    _write_config(data)
    needs_refresh = False
    if key == "enabled":
        if not value:
            _drop_cache(slug)
        else:
            needs_refresh = not was and slug not in load_cache()
    return {"ok": True, "action": "enable" if key == "enabled" and value else
            "disable" if key == "enabled" else "trust" if value else "untrust",
            "name": name, "slug": slug, key: bool(value), "needs_refresh": needs_refresh}


def remove_server(name):
    """Delete a server's entry (and its cached tools and tool switches)."""
    data = _read_config_for_edit()
    servers = data["servers"]
    name = str(name)
    if name not in servers:
        raise ConfigEditError("there is no server named %r" % name)
    slug = _assign_slugs(servers).get(name, ("", False))[0]
    _forget_tool_switches(slug)
    del servers[name]
    _write_config(data)
    _drop_cache(slug)
    return {"ok": True, "action": "remove", "name": name, "slug": slug}
