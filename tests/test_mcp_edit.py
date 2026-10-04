"""Tests for L.31 — editing MCP servers (mcp_client.save_server / set_server_flag /
remove_server / validate_server_spec), the extra per-server fields in status(),
and the `jarvis mcp-edit` CLI verb the web panel calls.

Run: python3 tests/test_mcp_edit.py   (from the repo root)

Real files, real subprocesses: the config lives in a throwaway directory, the
CLI cases run `python -m jarvis mcp-edit ...` against a throwaway HOME, and the
end-to-end case adds a server through save_server() and then refreshes it
against a real stdio server. The one pinned invariant that matters most is the
last test: nothing a model can call may reach the edit functions.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import mcp_client, tool_disable  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def raises(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except mcp_client.ConfigEditError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return "WRONG EXCEPTION %s: %s" % (type(e).__name__, e)
    return None


def fresh(servers=None):
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_mcp_edit_"))
    mcp_client.JARVIS_DIR = tmp
    mcp_client.CONFIG_FILE = tmp / "mcp_config.json"
    mcp_client.CACHE_FILE = tmp / "mcp_cache.json"
    mcp_client._POOL.clear()
    tool_disable.JARVIS_DIR = tmp
    tool_disable.DISABLED_FILE = tmp / "disabled.json"
    if servers is not None:
        mcp_client.CONFIG_FILE.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return tmp


def cfg():
    return json.loads(mcp_client.CONFIG_FILE.read_text(encoding="utf-8"))


def put_cache(slug, tools, error=None, age=0):
    cache = mcp_client.load_cache()
    cache[slug] = {"fetched_at": time.time() - age, "tools": tools, "error": error, "transport": "stdio"}
    mcp_client.save_cache(cache)


GOOD = {"transport": "stdio", "command": "npx", "args": ["-y", "pkg"]}


def test_validation():
    v = mcp_client.validate_server_spec
    check("a plain stdio spec is valid", v(GOOD) == [], v(GOOD))
    check("transport defaults to stdio", v({"command": "x"}) == [])
    check("a stdio spec with no command is refused", any("command" in p for p in v({"transport": "stdio"})))
    check("a blank command is refused", v({"command": "   "}) != [])
    check("a multi-line command is refused", v({"command": "a\nb"}) != [])
    check("http needs an http(s) url", v({"transport": "http", "url": "ftp://x"}) != [])
    check("http with a good url is valid", v({"transport": "http", "url": "https://x.example/mcp"}) == [])
    check("a url with a space is refused", v({"transport": "http", "url": "https://x y"}) != [])
    check("an unknown transport is refused", v({"transport": "carrier-pigeon", "command": "x"}) != [])
    check("sse / https are still accepted (the runtime accepts them)",
          v({"transport": "sse", "url": "http://x"}) == [] and v({"transport": "https", "url": "https://x"}) == [])
    check("args must be a list", v({"command": "x", "args": "-y pkg"}) != [])
    check("an argument may be a number", v({"command": "x", "args": [1, "a"]}) == [])
    check("an argument may not be an object", v({"command": "x", "args": [{}]}) != [])
    check("a bool argument is refused", v({"command": "x", "args": [True]}) != [])
    check("env must be an object", v({"command": "x", "env": ["A=1"]}) != [])
    check("an env name with a dash is refused", v({"command": "x", "env": {"A-B": "1"}}) != [])
    check("a bool env value is refused", v({"command": "x", "env": {"A": True}}) != [])
    check("a description over 300 chars is refused", v({"command": "x", "description": "d" * 301}) != [])
    check("a non-object spec is refused", v([]) != [] and v("x") != [])


def test_add_defaults_and_order():
    fresh({"first": dict(GOOD, enabled=True)})
    out = mcp_client.save_server("Second One", dict(GOOD, env={"TOKEN": "s3cret"}, description="d"))
    check("add reports created", out["created"] is True and out["slug"] == "second_one", out)
    check("a new server starts switched off and confirm-gated",
          out["enabled"] is False and out["trusted"] is False and out["needs_refresh"] is False, out)
    data = cfg()
    check("it is appended after the existing server", list(data["servers"]) == ["first", "Second One"])
    spec = data["servers"]["Second One"]
    check("the saved entry has the documented keys",
          spec["command"] == "npx" and spec["args"] == ["-y", "pkg"] and spec["env"] == {"TOKEN": "s3cret"}
          and spec["description"] == "d" and spec["enabled"] is False and spec["trusted"] is False, spec)
    check("the other server is untouched", data["servers"]["first"] == dict(GOOD, enabled=True))
    out2 = mcp_client.save_server("on-add", dict(GOOD, enabled=True))
    check("saving it switched on says a refresh is needed", out2["needs_refresh"] is True, out2)


def test_add_from_nothing():
    tmp = fresh()
    check("no config file exists yet", not mcp_client.CONFIG_FILE.exists())
    mcp_client.save_server("files", GOOD)
    check("adding to a missing config creates it with just that server", list(cfg()["servers"]) == ["files"])
    check("the example server is NOT silently added", "filesystem" not in cfg()["servers"])
    mcp_client.CONFIG_FILE.write_text("", encoding="utf-8")
    mcp_client.save_server("again", GOOD)
    check("an empty config file counts as no servers", list(cfg()["servers"]) == ["again"])
    del tmp


def test_refusals_write_nothing():
    fresh({"My Files": dict(GOOD)})
    before = mcp_client.CONFIG_FILE.read_text(encoding="utf-8")
    cases = {
        "duplicate": ("My Files", GOOD),
        "same tool prefix": ("my-files", GOOD),
        "empty name": ("   ", GOOD),
        "name with no letters or digits": ("!!!", GOOD),
        "control character in name": ("a\x07b", GOOD),
        "name too long": ("n" * 65, GOOD),
        "no command": ("x", {"transport": "stdio"}),
        "bad url": ("x", {"transport": "http", "url": "nope"}),
        "spec not an object": ("x", ["a"]),
    }
    for label, (name, spec) in cases.items():
        err = raises(mcp_client.save_server, name, spec)
        check("refused: %s" % label, bool(err) and not err.startswith("WRONG"), err)
    check("none of the refusals touched the file", mcp_client.CONFIG_FILE.read_text(encoding="utf-8") == before)
    err = raises(mcp_client.save_server, "x", GOOD, replace="ghost")
    check("editing a server that doesn't exist is refused", bool(err) and "ghost" in err, err)
    mcp_client.save_server("other", GOOD)
    err = raises(mcp_client.save_server, "other", GOOD, replace="My Files")
    check("renaming onto an existing name is refused", bool(err) and "already" in err, err)


def test_malformed_config_is_never_overwritten():
    fresh()
    broken = '{"servers": {"a": {"command": "x",}}'   # trailing comma, unclosed
    mcp_client.CONFIG_FILE.write_text(broken, encoding="utf-8")
    for label, fn in [("save", lambda: mcp_client.save_server("n", GOOD)),
                      ("flag", lambda: mcp_client.set_server_flag("a", "enabled", True)),
                      ("remove", lambda: mcp_client.remove_server("a"))]:
        err = raises(fn)
        check("%s refuses on invalid JSON" % label, bool(err) and "valid JSON" in err, err)
    check("the broken file is byte-for-byte untouched", mcp_client.CONFIG_FILE.read_text(encoding="utf-8") == broken)
    mcp_client.CONFIG_FILE.write_text('["not", "an", "object"]', encoding="utf-8")
    check("a JSON array at the top is refused too", raises(mcp_client.save_server, "n", GOOD) is not None)
    mcp_client.CONFIG_FILE.write_text('{"servers": "oops"}', encoding="utf-8")
    check("\"servers\" that isn't an object is refused", raises(mcp_client.save_server, "n", GOOD) is not None)


def test_edit_preserves_what_it_does_not_own():
    fresh({"a": {"enabled": True, "trusted": True, "transport": "stdio", "command": "old", "args": ["1"],
                 "env": {"KEEP": "k1", "DROP": "d1"}, "cwd": "~/w", "description": "was", "headers": {"X": "y"}},
           "b": dict(GOOD)})
    out = mcp_client.save_server("a", {"command": "new", "env": {"KEEP": None, "ADDED": "v"}}, replace="a")
    spec = cfg()["servers"]["a"]
    check("a field the caller sent is replaced", spec["command"] == "new")
    check("keys the caller left out are kept (args, cwd, description)",
          spec["args"] == ["1"] and spec["cwd"] == "~/w" and spec["description"] == "was", spec)
    check("an env value sent as null keeps the stored secret", spec["env"]["KEEP"] == "k1", spec["env"])
    check("an env name left out is dropped, a new one is added", "DROP" not in spec["env"] and spec["env"]["ADDED"] == "v")
    check("switches are kept when not mentioned", spec["enabled"] is True and spec["trusted"] is True)
    check("a hand-added key the editor doesn't know survives", spec["headers"] == {"X": "y"}, spec)
    check("changing the command says a refresh is needed", out["needs_refresh"] is True, out)
    err = raises(mcp_client.save_server, "a", {"env": {"NEVER_STORED": None}}, replace="a")
    check("\"keep\" for a variable that has no stored value is refused", bool(err) and "NEVER_STORED" in err, err)
    check("the other server is untouched", cfg()["servers"]["b"] == GOOD)


def test_http_url_null_keeps_the_stored_url():
    fresh({"w": {"enabled": True, "transport": "http", "url": "https://u:p@h.example/mcp?token=abc"}})
    mcp_client.save_server("w", {"transport": "http", "url": None, "description": "d"}, replace="w")
    check("url null keeps the secret url", cfg()["servers"]["w"]["url"] == "https://u:p@h.example/mcp?token=abc")
    mcp_client.save_server("w", {"transport": "http", "url": "https://new.example/x"}, replace="w")
    check("a typed url replaces it", cfg()["servers"]["w"]["url"] == "https://new.example/x")
    mcp_client.save_server("w", {"transport": "stdio", "command": "npx"}, replace="w")
    spec = cfg()["servers"]["w"]
    check("switching to stdio drops the url and writes the stdio keys",
          "url" not in spec and spec["command"] == "npx" and spec["args"] == [] and spec["env"] == {}, spec)


def test_rename_keeps_position_and_resets_state():
    fresh({"a": dict(GOOD, enabled=True), "b": dict(GOOD, enabled=True), "c": dict(GOOD, enabled=True)})
    put_cache("a", [{"name": "t"}])
    put_cache("b", [{"name": "echo"}])
    tool_disable.set_tool_disabled("mcp_b_echo", True)
    out = mcp_client.save_server("beta", {}, replace="b")
    check("a rename keeps the server where it was", list(cfg()["servers"]) == ["a", "beta", "c"], list(cfg()["servers"]))
    check("a rename reports the new slug and needs a refresh", out["slug"] == "beta" and out["needs_refresh"] is True, out)
    check("the old cache entry is gone", "b" not in mcp_client.load_cache())
    check("an unrelated server's cache survives", "a" in mcp_client.load_cache())
    check("the Tool Manager's switch for the old tool name is forgotten",
          "mcp_b_echo" not in tool_disable.disabled_tools())


def test_description_edit_keeps_the_cache():
    fresh({"a": dict(GOOD, enabled=True)})
    put_cache("a", [{"name": "t"}])
    out = mcp_client.save_server("a", {"description": "now with words"}, replace="a")
    check("a description-only edit doesn't invalidate the tool list",
          out["needs_refresh"] is False and "a" in mcp_client.load_cache(), out)
    out = mcp_client.save_server("a", {"args": ["-y", "other"]}, replace="a")
    check("an argument change does", out["needs_refresh"] is True and "a" not in mcp_client.load_cache(), out)


def test_flags():
    fresh({"a": dict(GOOD, enabled=True, trusted=False), "b": dict(GOOD)})
    put_cache("a", [{"name": "t"}])
    out = mcp_client.set_server_flag("a", "trusted", True)
    check("trust is recorded", cfg()["servers"]["a"]["trusted"] is True and out["action"] == "trust", out)
    check("trusting doesn't touch the cache", "a" in mcp_client.load_cache())
    check("untrust", mcp_client.set_server_flag("a", "trusted", False)["action"] == "untrust"
          and cfg()["servers"]["a"]["trusted"] is False)
    out = mcp_client.set_server_flag("a", "enabled", False)
    check("disable switches it off and drops its cached tools at once",
          cfg()["servers"]["a"]["enabled"] is False and "a" not in mcp_client.load_cache() and out["action"] == "disable", out)
    out = mcp_client.set_server_flag("a", "enabled", True)
    check("enable asks for a refresh (nothing cached)", out["needs_refresh"] is True and out["action"] == "enable", out)
    put_cache("a", [{"name": "t"}])
    out = mcp_client.set_server_flag("a", "enabled", True)
    check("enabling an already-on, already-cached server asks for nothing", out["needs_refresh"] is False, out)
    check("an unknown flag is refused", raises(mcp_client.set_server_flag, "a", "command", "rm") is not None)
    check("a missing server is refused", raises(mcp_client.set_server_flag, "zzz", "enabled", True) is not None)
    fresh({"junk": "not an object"})
    check("a hand-broken entry is refused, not overwritten", raises(mcp_client.set_server_flag, "junk", "enabled", True) is not None)


def test_remove():
    fresh({"a": dict(GOOD, enabled=True), "b": dict(GOOD)})
    put_cache("a", [{"name": "echo"}])
    tool_disable.set_tool_disabled("mcp_a_echo", True)
    out = mcp_client.remove_server("a")
    check("remove deletes only that entry", list(cfg()["servers"]) == ["b"] and out["action"] == "remove", out)
    check("its cache entry is gone", "a" not in mcp_client.load_cache())
    check("its Tool Manager switch is forgotten", "mcp_a_echo" not in tool_disable.disabled_tools())
    check("a .bak of the previous config is kept", mcp_client.CONFIG_FILE.with_suffix(".json.bak").exists())
    check("removing it again is refused", raises(mcp_client.remove_server, "a") is not None)


def test_status_fields_and_states():
    fresh({
        "ok one": dict(GOOD, enabled=True, trusted=True, description="Does things", env={"API_KEY": "topsecret"}, cwd="~/w"),
        "old": dict(GOOD, enabled=True),
        "broken": dict(GOOD, enabled=True),
        "fresh": dict(GOOD, enabled=True),
        "off": dict(GOOD),
        "nocmd": {"enabled": True, "transport": "stdio"},
        "remote": {"enabled": True, "transport": "http", "url": "https://u:pw@h.example:8443/mcp?token=XYZ#frag"},
    })
    put_cache("ok_one", [{"name": "echo", "description": "Echo text back " + "x" * 400}, {"name": "add", "description": "Add"}])
    put_cache("old", [{"name": "t"}], age=mcp_client.CACHE_TTL_SECONDS + 60)
    put_cache("broken", [], error="couldn't start 'npx': not found")
    put_cache("remote", [{"name": "ping"}])
    tool_disable.set_tool_disabled("mcp_ok_one_add", True)
    st = mcp_client.status()
    rows = {r["name"]: r for r in st["servers"]}
    check("the original top-level fields are all still there",
          all(k in st for k in ("servers", "enabled_count", "total_tools", "needs_refresh", "config_file")))
    check("the original per-server fields are all still there",
          all(k in rows["ok one"] for k in ("name", "slug", "enabled", "transport", "trusted", "tool_count",
                                            "last_refreshed", "stale", "error")))
    states = {n: r["state"] for n, r in rows.items()}
    check("state: ok", states["ok one"] == "ok", states)
    check("state: stale", states["old"] == "stale", states)
    check("state: error", states["broken"] == "error", states)
    check("state: not refreshed yet", states["fresh"] == "not_refreshed", states)
    check("state: disabled", states["off"] == "disabled", states)
    check("state: misconfigured, with the reason", states["nocmd"] == "misconfigured"
          and any("command" in p for p in rows["nocmd"]["problems"]), rows["nocmd"])
    ok = rows["ok one"]
    check("description, command, args and cwd are exposed",
          ok["description"] == "Does things" and ok["command"] == "npx" and ok["args"] == ["-y", "pkg"] and ok["cwd"] == "~/w")
    check("environment NAMES are exposed", ok["env_keys"] == ["API_KEY"], ok["env_keys"])
    check("the tool list carries the full tool name", [t["tool_name"] for t in ok["tools"]] == ["mcp_ok_one_echo", "mcp_ok_one_add"])
    check("a tool the Tool Manager switched off is marked", [t["disabled"] for t in ok["tools"]] == [False, True]
          and ok["disabled_tool_count"] == 1, ok["tools"])
    check("a tool description is clipped to 300 chars", len(ok["tools"][0]["description"]) <= 300)
    blob = json.dumps(st)
    check("an environment VALUE never appears anywhere in the payload", "topsecret" not in blob)
    check("a URL's password, token and fragment never appear", all(x not in blob for x in ("pw@", "XYZ", "frag", "u:pw")), blob)
    r = rows["remote"]
    check("the url is shown without its credentials, and flagged", r["url"] == "https://h.example:8443/mcp?\u2026"
          and r["url_redacted"] is True, r["url"])


def test_cache_entry_of_a_disabled_server_does_not_count():
    fresh({"a": dict(GOOD)})
    put_cache("a", [{"name": "t"}])
    st = mcp_client.status()
    check("a disabled server never reads as ok", st["servers"][0]["state"] == "disabled" and st["total_tools"] == 0, st)


def test_end_to_end_with_a_real_server():
    tmp = fresh()
    script = tmp / "srv.py"
    script.write_text(r'''
import json, sys
def send(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    m = json.loads(line); mid = m.get("id"); method = m.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "t", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "ping", "description": "Ping", "inputSchema": {"type": "object", "properties": {}}}]}})
''', encoding="utf-8")
    out = mcp_client.save_server("Live One", {"transport": "stdio", "command": sys.executable,
                                              "args": [str(script)], "enabled": True})
    check("saved enabled, refresh requested", out["needs_refresh"] is True and out["slug"] == "live_one", out)
    check("before the refresh it reads as not connected yet", mcp_client.status()["servers"][0]["state"] == "not_refreshed")
    summary = mcp_client.refresh(out["slug"])
    mcp_client.close_all()
    check("refresh through the saved spec finds the tool", summary == [{"server": "live_one", "tools": 1, "error": None}], summary)
    row = mcp_client.status()["servers"][0]
    check("now it reads as ready with its tool", row["state"] == "ok" and row["tools"][0]["tool_name"] == "mcp_live_one_ping", row)
    mcp_client.set_server_flag("Live One", "enabled", False)
    check("switching it off removes its tools from the cache immediately",
          mcp_client.cached_tools() == {} and mcp_client.status()["servers"][0]["state"] == "disabled")


def run_cli(home, *args):
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(ROOT / "jarvis-cli"),
                          env=env, capture_output=True, text=True, timeout=60)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"_raw": proc.stdout, "_err": proc.stderr[-300:]}
    return proc.returncode, payload


def test_cli_verb():
    home = Path(tempfile.mkdtemp(prefix="jarvis_mcp_cli_"))
    spec = json.dumps({"transport": "stdio", "command": "npx", "args": ["-y", "p"], "env": {"K": "v"}})
    code, out = run_cli(home, "mcp-edit", "save", "cli one", spec)
    check("mcp-edit save exits 0 and reports created", code == 0 and out.get("created") is True, (code, out))
    path = home / ".jarvis" / "mcp_config.json"
    check("the real config file was written under HOME", path.exists() and "cli one" in json.loads(path.read_text())["servers"])
    code, out = run_cli(home, "mcp-edit", "save", "cli one", spec)
    check("a duplicate exits 1 with an error", code == 1 and "already exists" in out.get("error", ""), (code, out))
    code, out = run_cli(home, "mcp-edit", "save", "renamed", json.dumps({"description": "d"}), "--replace", "cli one")
    check("--replace edits and renames", code == 0 and out.get("name") == "renamed" and out.get("created") is False, (code, out))
    code, out = run_cli(home, "mcp-edit", "save", "x", json.dumps({}), "--replace")
    check("--replace with no value is an error", code == 1 and "--replace" in out.get("error", ""), (code, out))
    code, out = run_cli(home, "mcp-edit", "save", "x", "{not json")
    check("a definition that isn't JSON is an error", code == 1 and "valid JSON" in out.get("error", ""), (code, out))
    for action, key in (("enable", "enabled"), ("trust", "trusted")):
        code, out = run_cli(home, "mcp-edit", action, "renamed")
        check("mcp-edit %s works" % action, code == 0 and out.get(key) is True, (code, out))
    code, out = run_cli(home, "mcp-status")
    row = out["servers"][0]
    check("mcp-status shows the new fields and the edit", row["name"] == "renamed" and row["trusted"] is True
          and row["enabled"] is True and row["env_keys"] == ["K"] and "v" != row.get("env"), row)
    code, out = run_cli(home, "mcp-edit", "bogus", "renamed")
    check("an unknown action prints usage and exits 1", code == 1 and "usage" in out.get("error", ""), (code, out))
    code, out = run_cli(home, "mcp-edit")
    check("no arguments prints usage and exits 1", code == 1 and "usage" in out.get("error", ""), (code, out))
    code, out = run_cli(home, "mcp-edit", "remove", "renamed")
    check("mcp-edit remove works", code == 0 and out.get("action") == "remove", (code, out))
    code, out = run_cli(home, "mcp-edit", "remove", "renamed")
    check("removing twice is an error", code == 1, (code, out))


def test_registered_everywhere_it_must_be():
    from jarvis import reserved_names
    names = getattr(reserved_names, "RESERVED_NAMES", None) or getattr(reserved_names, "RESERVED", None)
    src = (ROOT / "jarvis-cli" / "jarvis" / "reserved_names.py").read_text(encoding="utf-8")
    check("mcp-edit is a reserved CLI name", '"mcp-edit"' in src or (names and "mcp-edit" in names))
    slash = (ROOT / "web" / "public" / "slash-commands-data.js").read_text(encoding="utf-8")
    check("the /mcp palette verb covers mcp-edit", re.search(r'"covers":\s*\[[^\]]*"mcp-edit"', slash) is not None)


def test_no_model_tool_can_reach_the_edit_functions():
    """The security rule at the top of mcp_client.py: nothing the model can say
    may introduce a new executable. The edit functions may be referenced by
    mcp_client.py itself and the CLI verb (a human types or clicks that) and
    by tests - and by nothing else under jarvis-cli/jarvis."""
    pattern = re.compile(r"\b(save_server|set_server_flag|remove_server|_write_config)\b")
    offenders = []
    base = ROOT / "jarvis-cli" / "jarvis"
    for path in base.rglob("*.py"):
        rel = path.relative_to(base).as_posix()
        if rel in ("mcp_client.py", "cli.py"):
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(rel)
    check("only mcp_client.py and the mcp-edit CLI verb mention the edit functions", offenders == [], offenders)
    from jarvis.actions import mcp_tools
    names = list(mcp_tools.TOOLS) + [s.get("name", "") for s in mcp_tools.TOOL_SCHEMAS]
    check("the mcp tool group exposes no add/edit/remove-server tool",
          not any(re.search(r"(save|add|edit|remove|set|write).*(server|config)", n or "") for n in names), names)
    cli_src = (base / "cli.py").read_text(encoding="utf-8")
    check("the mcp-edit verb is not offered to the model as a callable tool",
          "mcp-edit" not in (base / "tool_registry.py").read_text(encoding="utf-8")
          and "mcp-edit" in cli_src)


for fn in [
    test_validation, test_add_defaults_and_order, test_add_from_nothing, test_refusals_write_nothing,
    test_malformed_config_is_never_overwritten, test_edit_preserves_what_it_does_not_own,
    test_http_url_null_keeps_the_stored_url, test_rename_keeps_position_and_resets_state,
    test_description_edit_keeps_the_cache, test_flags, test_remove, test_status_fields_and_states,
    test_cache_entry_of_a_disabled_server_does_not_count, test_end_to_end_with_a_real_server,
    test_cli_verb, test_registered_everywhere_it_must_be, test_no_model_tool_can_reach_the_edit_functions,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
