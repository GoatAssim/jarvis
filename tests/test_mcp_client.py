"""Tests for jarvis/mcp_client.py — the MCP (Model Context Protocol) client.

Run: python3 ../tests/test_mcp_client.py   (from jarvis-cli/)

Talks to a REAL stdio MCP server over a real pipe — the fake server is
written to a temp file and spawned as a subprocess, exactly as a genuine
`npx @modelcontextprotocol/server-...` would be. Mocking the transport
would defeat the purpose: every bug this client is likely to have
(handshake ordering, banner noise, a stderr pipe filling up, notification
frames interleaved with responses) lives in the transport.

No network is needed — the fake server is plain stdlib Python.
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import mcp_client  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


# A deliberately badly-behaved-but-legal server: it prints a non-JSON banner
# on stdout, chatters on stderr, and emits an unsolicited notification
# between the handshake and the first response. All three are things real
# servers do, and all three broke naive clients.
FAKE_SERVER = r'''
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()

sys.stdout.write("starting up, not JSON\n"); sys.stdout.flush()
for i in range(50):
    print("chatty stderr line %d" % i, file=sys.stderr, flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "notifications/initialized":
        send({"jsonrpc": "2.0", "method": "notifications/message",
              "params": {"level": "info", "data": "noise"}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": "echo", "description": "Echo text back.",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}},
                             "required": ["text"]}},
            {"name": "add", "description": "Add two numbers.",
             "inputSchema": {"type": "object",
                             "properties": {"a": {"type": "number"},
                                            "b": {"type": "number"}}}},
        ]}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "echo: " + str(args.get("text"))}]}})
        elif name == "add":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": str((args.get("a") or 0) + (args.get("b") or 0))}]}})
        elif name == "picture":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "image", "data": "xxx"}]}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "no such tool"}], "isError": True}})
'''


def fresh(server_spec=None):
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_mcp_test_"))
    script = tmp / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")

    mcp_client.JARVIS_DIR = tmp
    mcp_client.CONFIG_FILE = tmp / "mcp_config.json"
    mcp_client.CACHE_FILE = tmp / "mcp_cache.json"
    mcp_client._POOL.clear()

    spec = server_spec or {
        "enabled": True, "transport": "stdio",
        "command": sys.executable, "args": [str(script)], "trusted": False,
    }
    mcp_client.CONFIG_FILE.write_text(
        json.dumps({"servers": {"Fake Server": spec}}), encoding="utf-8")
    return tmp


def cleanup(tmp):
    mcp_client.close_all()
    shutil.rmtree(tmp, ignore_errors=True)


def test_name_sanitizing():
    check("a spaced name becomes a slug", mcp_client.sanitize_name("Fake Server") == "fake_server")
    check("punctuation is collapsed", mcp_client.sanitize_name("my-server!!") == "my_server")
    check("a leading digit is made legal", mcp_client.sanitize_name("3d").startswith("s_"))
    name = mcp_client.tool_name_for("fake_server", "echo")
    check("tool names are prefixed", name == "mcp_fake_server_echo", name)
    # tool_loader rejects the whole file if any name breaks this pattern.
    long_name = mcp_client.tool_name_for("s" * 40, "t" * 40)
    check("an over-long name is clamped to 64 chars", len(long_name) <= 64, str(len(long_name)))


def test_handshake_and_listing():
    tmp = fresh()
    try:
        check("only enabled servers are returned", list(mcp_client.enabled_servers()) == ["fake_server"])
        tools = mcp_client.list_tools("fake_server")
        names = sorted(t["name"] for t in tools)
        # Getting here at all proves the banner line, the 50 stderr lines and
        # the stray notification frame were all survived.
        check("tools/list works through banner + stderr + notification noise",
              names == ["add", "echo"], str(names))
        check("descriptions come through", tools[0]["description"] == "Echo text back.")
        check("the JSON Schema comes through",
              tools[0]["input_schema"]["properties"]["text"]["type"] == "string")
    finally:
        cleanup(tmp)


def test_refresh_populates_the_cache():
    tmp = fresh()
    try:
        summary = mcp_client.refresh()
        check("refresh reports what it found",
              summary == [{"server": "fake_server", "tools": 2, "error": None}], str(summary))
        cached = mcp_client.cached_tools()
        check("the cache holds the tools", len(cached["fake_server"]["tools"]) == 2)
        check("and is not stale immediately", cached["fake_server"]["stale"] is False)
        check("cache_is_stale agrees", mcp_client.cache_is_stale() is False)
    finally:
        cleanup(tmp)


def test_cache_read_never_spawns():
    tmp = fresh()
    try:
        mcp_client.refresh()
        mcp_client.close_all()
        # The catalog reads this on EVERY jarvis invocation, so it has to be
        # a file read and nothing else.
        started = time.time()
        cached = mcp_client.cached_tools()
        elapsed = time.time() - started
        check("reading the cache is effectively free", elapsed < 0.2, f"{elapsed:.3f}s")
        check("and still returns the tools", len(cached["fake_server"]["tools"]) == 2)
        check("without leaving a session open", not mcp_client._POOL)
    finally:
        cleanup(tmp)


def test_calling_tools():
    tmp = fresh()
    try:
        echoed = mcp_client.call_tool("fake_server", "echo", {"text": "hello"})
        check("a text result is flattened to .text", echoed.get("text") == "echo: hello", str(echoed))
        check("and marked ok", echoed["ok"] is True)
        added = mcp_client.call_tool("fake_server", "add", {"a": 2, "b": 40})
        check("arguments are passed through correctly", added.get("text") == "42", str(added))
    finally:
        cleanup(tmp)


def test_tool_errors_are_results_not_exceptions():
    tmp = fresh()
    try:
        result = mcp_client.call_tool("fake_server", "nope", {})
        check("isError becomes ok=False", result["ok"] is False)
        check("with the server's message attached", "no such tool" in (result.get("error") or ""))
    finally:
        cleanup(tmp)


def test_non_text_content_is_described_not_dropped():
    tmp = fresh()
    try:
        result = mcp_client.call_tool("fake_server", "picture", {})
        # Returning a bare empty string for an image would read to the model
        # as "the tool did nothing".
        check("non-text blocks are reported", result.get("non_text_content") == ["image"], str(result))
    finally:
        cleanup(tmp)


def test_session_is_pooled_within_a_process():
    tmp = fresh()
    try:
        mcp_client.call_tool("fake_server", "echo", {"text": "one"})
        first = mcp_client._POOL["fake_server"].proc.pid
        mcp_client.call_tool("fake_server", "echo", {"text": "two"})
        second = mcp_client._POOL["fake_server"].proc.pid
        # Three tool calls in one ask should pay spawn+handshake once.
        check("a second call reuses the same server process", first == second, f"{first} != {second}")
        mcp_client.close_all()
        check("close_all really closes it", not mcp_client._POOL)
    finally:
        cleanup(tmp)


def test_broken_server_is_recorded_not_fatal():
    tmp = fresh({"enabled": True, "transport": "stdio",
                 "command": "definitely-not-a-real-binary-xyz", "args": []})
    try:
        summary = mcp_client.refresh()
        check("a failing server doesn't raise out of refresh", len(summary) == 1)
        check("its error is recorded for diagnosis", bool(summary[0]["error"]), str(summary[0]))
        status = mcp_client.status()
        # The difference between "github: command not found" and a silent
        # empty list is the difference between fixable and mysterious.
        check("status surfaces the error", bool(status["servers"][0]["error"]))
        check("and reports zero tools", status["servers"][0]["tool_count"] == 0)
    finally:
        cleanup(tmp)


def test_disabled_servers_are_ignored():
    tmp = fresh({"enabled": False, "transport": "stdio", "command": sys.executable, "args": []})
    try:
        check("a disabled server is not enabled", mcp_client.enabled_servers() == {})
        status = mcp_client.status()
        check("but it still shows in status, so it's discoverable",
              len(status["servers"]) == 1 and status["servers"][0]["enabled"] is False)
        check("with nothing enabled overall", status["enabled_count"] == 0)
    finally:
        cleanup(tmp)


def test_stale_cache_entries_are_pruned():
    tmp = fresh()
    try:
        mcp_client.refresh()
        # A server the user later removed from the config must not keep
        # contributing tools to the catalog from the cache.
        cache = mcp_client.load_cache()
        cache["ghost_server"] = {"fetched_at": time.time(), "tools": [{"name": "x"}], "error": None}
        mcp_client.save_cache(cache)
        mcp_client.refresh()
        check("a server no longer in the config is dropped from the cache",
              "ghost_server" not in mcp_client.load_cache())
    finally:
        cleanup(tmp)


def test_unknown_transport_is_rejected():
    tmp = fresh({"enabled": True, "transport": "carrier-pigeon", "command": "x"})
    try:
        try:
            mcp_client.list_tools("fake_server")
            check("an unknown transport is rejected", False, "accepted")
        except mcp_client.MCPError as e:
            check("an unknown transport is rejected clearly", "transport" in str(e), str(e))
    finally:
        cleanup(tmp)


for fn in [
    test_name_sanitizing, test_handshake_and_listing, test_refresh_populates_the_cache,
    test_cache_read_never_spawns, test_calling_tools,
    test_tool_errors_are_results_not_exceptions,
    test_non_text_content_is_described_not_dropped,
    test_session_is_pooled_within_a_process, test_broken_server_is_recorded_not_fatal,
    test_disabled_servers_are_ignored, test_stale_cache_entries_are_pruned,
    test_unknown_transport_is_rejected,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
