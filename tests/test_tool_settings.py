"""TOOL_SETTINGS: settings a tool file supplies for Settings > Tool settings.

Covers:
  1. tool_settings.validate_settings: good entries of every type, every way an entry can be
     wrong (key shape, duplicate, missing label, bad type, bad choices, bad range, a default
     outside its own range), the entry cap, a non-list.
  2. tool_settings.coerce: types, ranges, bool/NaN refusal, text length, choice membership.
  3. Storage: set / get / reset round trip, only the owner's changed values are stored, a hand
     edited value that no longer fits reads as the default, a corrupt file reads as empty, the
     file never contains anything but {stem: {key: value}}.
  4. tool_loader: a module's TOOL_SETTINGS lands on its record; a bad entry never rejects the file.
  5. CLI (throwaway HOME, a real tool file in ~/.jarvis/tools): `tool-settings list|set|reset`,
     a wrapped value, a refused undeclared key, a refused out-of-range value, and that the tool
     itself reads the value through tool_settings.get.
  6. web/server.js: the three /api/settings/tools routes exist behind the Settings origin guard
     and the server never validates a value itself.

No network, no live model.

Run: python3 tests/test_tool_settings.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)
ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI_DIR))

from jarvis import tool_loader, tool_settings as ts  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def entries(raw, filename="x.py"):
    return ts.validate_settings(raw, filename)


# ---------------------------------------------------------------- 1. validation
good, errs = entries([
    {"key": "flag", "label": "A switch", "type": "boolean", "default": True},
    {"key": "count", "label": "How many", "type": "integer", "min": 1, "max": 10, "default": 3, "unit": "items"},
    {"key": "ratio", "label": "Ratio", "type": "number", "default": 0.5},
    {"key": "folder", "label": "Folder", "type": "string", "default": "notes", "section": "Where"},
    {"key": "mode", "label": "Mode", "type": "choice", "choices": ["a", ["b", "Bee"]], "default": "b"},
    {"key": "bare", "label": "No type given"},
])
check("every type validates", len(good) == 6 and not errs, errs)
by = {e["key"]: e for e in good}
check("choices are normalised to [value, label]", by["mode"]["choices"] == [["a", "a"], ["b", "Bee"]], by["mode"])
check("a missing type is a string with an empty default", by["bare"]["type"] == "string" and by["bare"]["default"] == "")
check("a missing default is the type's own", by["flag"]["default"] is True and entries([{"key": "q", "label": "Q", "type": "boolean"}])[0][0]["default"] is False)
check("section and unit are kept", by["folder"]["section"] == "Where" and by["count"]["unit"] == "items")

bad_cases = {
    "uppercase key": {"key": "Bad", "label": "x"},
    "key with dash": {"key": "a-b", "label": "x"},
    "key too long": {"key": "a" * 41, "label": "x"},
    "no label": {"key": "nolabel"},
    "unknown type": {"key": "t", "label": "x", "type": "color"},
    "choice without choices": {"key": "c", "label": "x", "type": "choice"},
    "choice with duplicates": {"key": "c", "label": "x", "type": "choice", "choices": ["a", "a"]},
    "min above max": {"key": "r", "label": "x", "type": "integer", "min": 5, "max": 1},
    "default outside range": {"key": "r", "label": "x", "type": "integer", "min": 1, "max": 5, "default": 9},
    "default not a choice": {"key": "c", "label": "x", "type": "choice", "choices": ["a"], "default": "z"},
    "integer default that is a float": {"key": "r", "label": "x", "type": "integer", "default": 1.5},
}
for label, raw in bad_cases.items():
    got, why = entries([raw])
    check(f"refused: {label}", got == [] and len(why) == 1, (got, why))
got, why = entries([{"key": "dup", "label": "a"}, {"key": "dup", "label": "b"}])
check("a duplicate key is dropped, the first stays", len(got) == 1 and got[0]["label"] == "a" and len(why) == 1)
check("a non-list is refused", entries({"key": "x"}) == ([], ["x.py: TOOL_SETTINGS must be a list of dicts"]))
got, why = entries([{"key": f"k{i}", "label": "l"} for i in range(ts.MAX_ENTRIES_PER_FILE + 5)])
check("the entry cap holds", len(got) == ts.MAX_ENTRIES_PER_FILE and len(why) == 1)
got, _ = entries([{"key": "n", "label": "N", "type": "integer", "min": 5}])
check("a default that falls below min sits on min", got and got[0]["default"] == 5, got)

# ---------------------------------------------------------------- 2. coerce
ent = by
check("coerce: boolean text", ts.coerce(ent["flag"], "off") == (True, False, "") and ts.coerce(ent["flag"], "yes")[1] is True)
check("coerce: boolean refuses a number", ts.coerce(ent["flag"], 3)[0] is False)
check("coerce: integer from text", ts.coerce(ent["count"], "7") == (True, 7, ""))
check("coerce: integer refuses a fraction, bool, range", not ts.coerce(ent["count"], 2.5)[0] and not ts.coerce(ent["count"], True)[0]
      and not ts.coerce(ent["count"], 0)[0] and not ts.coerce(ent["count"], 11)[0])
check("coerce: number refuses NaN and infinity", not ts.coerce(ent["ratio"], float("nan"))[0] and not ts.coerce(ent["ratio"], float("inf"))[0])
check("coerce: choice must be a member", ts.coerce(ent["mode"], "a")[0] and not ts.coerce(ent["mode"], "z")[0] and not ts.coerce(ent["mode"], True)[0])
check("coerce: string must be text, bounded, no NUL", ts.coerce(ent["folder"], "ok")[0] and not ts.coerce(ent["folder"], 5)[0]
      and not ts.coerce(ent["folder"], "x" * (ts.MAX_STRING + 1))[0] and not ts.coerce(ent["folder"], "a\x00b")[0])

# ---------------------------------------------------------------- 3. storage
check("nothing stored yet", ts.stored("demo.py") == {} and not ts.settings_path().exists())
ok, val, why = ts.set_value(ent["count"], "demo.py", 4)
check("set_value stores a coerced value", ok and val == 4 and ts.stored("demo") == {"count": 4}, (ok, val, why))
ok, _v, why = ts.set_value(ent["count"], "demo.py", 99)
check("set_value refuses an out-of-range value and writes nothing", not ok and ts.stored("demo") == {"count": 4}, why)
file_data = json.loads(ts.settings_path().read_text(encoding="utf-8"))
check("the file holds only {stem: {key: value}}", file_data == {"demo": {"count": 4}}, file_data)
value, is_default = ts.value_of(ent["count"], "demo.py")
check("value_of reports a changed value as not default", value == 4 and is_default is False)
value, is_default = ts.value_of(ent["flag"], "demo.py")
check("value_of reports an unset value as default", value is True and is_default is True)
# a hand edit that no longer fits reads as the default
ts.settings_path().write_text(json.dumps({"demo": {"count": "lots", "flag": "maybe"}}), encoding="utf-8")
check("a hand-edited bad value reads as the default", ts.value_of(ent["count"], "demo.py") == (3, True) and ts.value_of(ent["flag"], "demo.py")[0] is True)
ts.settings_path().write_text("{not json", encoding="utf-8")
check("a corrupt file reads as empty and never raises", ts.stored("demo") == {} and ts.get("demo.py", "count", 9) == 9)
ts.set_value(ent["count"], "demo.py", 5)
ok, _why = ts.reset_value(ent["count"], "demo.py")
check("reset removes the key and an emptied file entry", ok and ts.stored("demo") == {} and json.loads(ts.settings_path().read_text(encoding="utf-8")) == {})
check("get() with no declaration falls back to the caller's default", ts.get("nobody.py", "anything", 12) == 12 and ts.get("nobody.py", "anything") is None)
check("stem_of accepts a path, a file name and a stem", ts.stem_of("/a/b/notes.py") == "notes" == ts.stem_of("notes.py") == ts.stem_of("notes"))

# ---------------------------------------------------------------- 4. loader
TOOL = '''
import json
TOOL_GROUP = "demo"
TOOL_SCHEMAS = [{"name": "demo_read", "description": "Read a demo setting.", "parameters": {"type": "object", "properties": {}}}]

def tool_demo_read(args):
    from jarvis import tool_settings
    return {"ok": True, "count": tool_settings.get(__file__, "count"), "mode": tool_settings.get(__file__, "mode")}

TOOLS = {"demo_read": tool_demo_read}
TOOL_KEYWORDS = {"demo_read": {"demo read": 10}}
TOOL_SETTINGS = [
    {"key": "count", "label": "How many", "type": "integer", "min": 1, "max": 10, "default": 3},
    {"key": "mode", "label": "Mode", "type": "choice", "choices": ["a", "b"], "default": "a"},
    {"key": "BAD KEY", "label": "dropped"},
]
'''
tools_dir = Path(_HOME) / ".jarvis" / "tools"
tools_dir.mkdir(parents=True, exist_ok=True)
(tools_dir / "demo_settings_tool.py").write_text(TOOL, encoding="utf-8")
logged = []
records = tool_loader.discover_actions(tools_dir, set(), logger=logged.append)
rec = next((r for r in records if r.file == "demo_settings_tool.py"), None)
check("the file loads", rec is not None and rec.valid, rec and rec.error)
check("its good settings are on the record, the bad one is not", rec and [s["key"] for s in rec.settings] == ["count", "mode"], rec and rec.settings)
check("the bad entry is logged, with its tag", any("[tool-settings]" in line and "BAD KEY" in line for line in logged), logged)

# ---------------------------------------------------------------- 5. CLI
CLI_ENV = dict(os.environ)


def cli(*args):
    p = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(CLI_DIR), env=CLI_ENV,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


def as_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return {}


rc, out, err = cli("tool-settings", "list")
listing = as_json(out)
mine = next((f for f in listing.get("files", []) if f.get("file") == "demo_settings_tool.py"), None)
check("tool-settings list shows the file and both settings", rc == 0 and mine and [s["key"] for s in mine["settings"]] == ["count", "mode"], (rc, out[:300], err[:300]))
check("every setting starts at its default", mine and all(s["is_default"] for s in mine["settings"]) and mine["settings"][0]["value"] == 3)
check("a choice carries its choices, a number its range", mine and mine["settings"][1]["choices"] == [["a", "a"], ["b", "b"]]
      and mine["settings"][0]["min"] == 1 and mine["settings"][0]["max"] == 10)

rc, out, err = cli("tool-settings", "set", "demo_settings_tool.py", "count", json.dumps({"value": 7}))
check("set with the web server's wrapped value", rc == 0 and as_json(out) == {"ok": True, "value": 7}, (rc, out, err[:200]))
rc, out, err = cli("tool-settings", "set", "demo_settings_tool", "mode", "b")
check("set by file stem, with a bare word taken as text", rc == 0 and as_json(out).get("value") == "b", (rc, out, err[:200]))
rc, out, err = cli("tool-settings", "set", "demo_settings_tool.py", "count", json.dumps({"value": 99}))
check("an out-of-range value is refused", rc != 0 and as_json(out).get("ok") is False and "above" in as_json(out).get("error", ""), (rc, out))
rc, out, err = cli("tool-settings", "set", "demo_settings_tool.py", "nope", json.dumps({"value": 1}))
check("an undeclared key is refused", rc != 0 and as_json(out).get("ok") is False, (rc, out))
rc, out, err = cli("tool-settings", "set", "some_other_file.py", "count", json.dumps({"value": 1}))
check("an undeclared file is refused", rc != 0 and as_json(out).get("ok") is False, (rc, out))
rc, out, err = cli("tool-settings", "set", "demo_settings_tool.py", "count")
check("a missing value prints usage", rc != 0 and "usage" in as_json(out).get("error", ""), (rc, out))

listing = as_json(cli("tool-settings", "list")[1])
mine = next((f for f in listing.get("files", []) if f.get("file") == "demo_settings_tool.py"), None)
check("list now shows the saved values as changed", mine and mine["settings"][0]["value"] == 7 and not mine["settings"][0]["is_default"]
      and mine["settings"][1]["value"] == "b", mine)

rc, out, err = cli("tool-run", "demo_read", "{}")
result = as_json(out)
check("the tool reads the saved values through tool_settings.get", rc == 0 and result.get("count") == 7 and result.get("mode") == "b", (rc, out[:300], err[:300]))

rc, out, err = cli("tool-settings", "reset", "demo_settings_tool.py", "count")
check("reset succeeds", rc == 0 and as_json(out).get("ok") is True, (rc, out))
result = as_json(cli("tool-run", "demo_read", "{}")[1])
check("after a reset the tool sees the declared default again", result.get("count") == 3 and result.get("mode") == "b", result)

# ---------------------------------------------------------------- 6. the web server's side
server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
for route in ('app.get("/api/settings/tools"', 'app.post("/api/settings/tools/set"', 'app.post("/api/settings/tools/reset"'):
    line = next((l for l in server.splitlines() if l.startswith(route)), "")
    check(f"{route.split('(')[0]} {route.split('(')[1]} sits behind the Settings origin guard",
          "requireSettingsOrigin" in line and "requireJarvis" in line, line)
block = server[server.index("const TOOL_SETTING_FILE_RE"):server.index('app.post("/api/settings/file/defaults"')]
check("server.js holds no range or type rules of its own (the CLI decides)", not re.search(r"\b(min|max|isInteger|isFinite|parseFloat|parseInt)\b", block), re.findall(r"\b(min|max|isInteger|isFinite|parseFloat|parseInt)\b", block))
check("server.js passes only a file name and key it pattern-checked",
      "TOOL_SETTING_FILE_RE.test(file)" in block and "TOOL_SETTING_KEY_RE.test(key)" in block)
check("the file-name pattern cannot start with a dash (flag injection)", re.search(r"TOOL_SETTING_FILE_RE = /\^\[A-Za-z0-9\]", block) is not None)
check("the value is wrapped so a negative number is not read as a flag", 'JSON.stringify({ value })' in block)

reserved = (CLI_DIR / "jarvis" / "reserved_names.py").read_text(encoding="utf-8")
check("tool-settings is a reserved CLI name", '"tool-settings"' in reserved)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
