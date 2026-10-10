"""Master plan L.43 -- the Settings modal's Advanced back end (tunables.py, settings_admin.py).

The promise: the owner can change any setting from the web UI, and doing so is never
less safe than editing the file by hand with a backup. Pinned here:

  1. tunables: every registered default is valid and equals the literal the module
     always had; resolution is env > saved override > default; a bad stored value is
     ignored; an unregistered name can never be set; ranges are enforced on write.
  2. wiring: a saved override changes the constant a module imports (real process), and
     with no override nothing changed; JARVIS_RAW_ARCHIVE honours an override and the
     environment still wins.
  3. settings_admin reads: secrets are masked unless revealed; a file that does not
     parse is returned as text so it can be repaired; names outside ~/.jarvis and
     ~/.jarvis/channels are refused.
  4. settings_admin writes: a masked secret survives a round trip; a stale write, bad
     JSON, a type flip and an invalid ai_config are all refused with the reason; a guarded
     file needs the confirmation; read-only groups refuse; a chat / scheduled / task
     process refuses; every accepted write leaves a backup and can be restored; the
     change log never holds a value.
  5. the CLI verb prints one JSON object and refuses a write from an unattended process.
  6. no model-facing module can reach the writers.

Run: python3 tests/test_l43_settings.py   (no key, no network)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l43-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_RAW_ARCHIVE", "JARVIS_WEB_STREAM", "JARVIS_TICK_MS", "JARVIS_CHANNEL",
             "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT"):
    os.environ.pop(_var, None)

_JARVIS_CLI = Path(__file__).resolve().parent.parent / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

from jarvis import settings_admin as sa  # noqa: E402
from jarvis import tunables  # noqa: E402

PASS, FAIL = [], []
JDIR = Path(_HOME) / ".jarvis"
SECRET = "sk-test-SECRET-1234567890"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


def _reset_home():
    import shutil
    shutil.rmtree(JDIR, ignore_errors=True)
    JDIR.mkdir(parents=True, exist_ok=True)
    tunables._cache.update(path=None, mtime=None, values={})


def _put(name, doc):
    path = JDIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _ai_doc():
    return {
        "providers": [{"name": "groq", "type": "openai_compatible", "enabled": True, "model": "m1",
                       "base_url": "https://example.test/v1", "api_keys": [SECRET, ""]}],
        "defaults": {"max_tokens": 700, "timeout": 30, "tools_enabled": True, "provider_priority": ["groq"]},
        "persona": {"name": "Jarvis"},
    }


def _env(**extra):
    env = dict(os.environ)
    env.update({"HOME": _HOME, "USERPROFILE": _HOME})
    for k in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT"):
        env.pop(k, None)
    env.update(extra)
    return env


# ---- 1. tunables ----------------------------------------------------------------------
def test_registry_is_sane():
    names = [t["name"] for t in tunables.registry()]
    check("registry names are unique", len(names) == len(set(names)))
    bad = []
    for t in tunables.registry():
        try:
            tunables.coerce(t, t["default"])
        except ValueError as exc:
            bad.append((t["name"], str(exc)))
        if t["type"] in ("int", "float") and not (t["min"] <= t["default"] <= t["max"]):
            bad.append((t["name"], "default outside its own range"))
    check("every default is valid and inside its range", not bad, bad)
    check("MAX_TOOL_ROUNDS is not a tunable (AGENTS.md invariant)",
          tunables.spec("MAX_TOOL_ROUNDS") is None)
    check("nothing safety-related is registered",
          not [n for n in names if any(w in n for w in ("SAFETY", "CONFIRM", "POLICY", "PERMISSION", "ROUNDS"))])


def test_defaults_equal_the_module_literals():
    _reset_home()
    from jarvis import (custom_tools_agent, key_health, memory, notifier, web_tools)
    pairs = [("DEFAULT_MIN_ROUND_INTERVAL", key_health.DEFAULT_MIN_ROUND_INTERVAL),
             ("MAX_FACTS", memory.MAX_FACTS), ("MAX_PROMPT_FACTS", memory.MAX_PROMPT_FACTS),
             ("MAX_INBOX", notifier.MAX_INBOX), ("TOAST_TIMEOUT", notifier.TOAST_TIMEOUT),
             ("AGENT_TIMEOUT", custom_tools_agent.AGENT_TIMEOUT),
             ("MAX_CONTINUATIONS", custom_tools_agent.MAX_CONTINUATIONS),
             ("FETCH_TIMEOUT", web_tools.FETCH_TIMEOUT), ("FETCH_MAX_CHARS", web_tools.FETCH_MAX_CHARS)]
    for name, live in pairs:
        check(f"{name}: with no override the module value is the registry default",
              tunables.spec(name)["default"] == live, (tunables.spec(name)["default"], live))


def test_resolution_order():
    _reset_home()
    check("default with nothing set", tunables.get("MAX_FACTS") == 80)
    ok, val = tunables.set_override("MAX_FACTS", 120)
    check("a valid override is accepted", ok and val == 120)
    check("the override is what get() returns", tunables.get("MAX_FACTS") == 120)
    check("const() keeps the module literal's type", tunables.const("MAX_FACTS", 80) == 120
          and isinstance(tunables.const("MAX_FACTS", 80), int))
    check("a float constant stays a float even when stored as a whole number",
          isinstance(tunables.const("DEFAULT_MIN_ROUND_INTERVAL", 3.0), float))

    ok, why = tunables.set_override("MAX_FACTS", 5)
    check("below the minimum is refused with the range in the message", (not ok) and "minimum" in why, why)
    ok, why = tunables.set_override("MAX_FACTS", 10 ** 9)
    check("above the maximum is refused", not ok and "maximum" in why, why)
    ok, why = tunables.set_override("MAX_FACTS", "lots")
    check("a non-number is refused", not ok, why)
    ok, why = tunables.set_override("MAX_FACTS", True)
    check("on/off is not a number", not ok, why)
    ok, why = tunables.set_override("MAX_FACTS", 80.5)
    check("a fraction is not a whole number", not ok, why)
    ok, why = tunables.set_override("MAX_TOOL_ROUNDS", 99)
    check("an unregistered name cannot be set", not ok and "not a registered" in why, why)
    check("a refused write left the stored value alone", tunables.get("MAX_FACTS") == 120)

    store = JDIR / tunables.STORE_NAME
    store.write_text(json.dumps({"version": 1, "values": {"MAX_FACTS": 999999, "NOT_REAL": 1}}), encoding="utf-8")
    tunables._cache.update(path=None, mtime=None, values={})
    check("a hand-edited out-of-range value is ignored, not trusted", tunables.get("MAX_FACTS") == 80)
    store.write_text("{not json", encoding="utf-8")
    tunables._cache.update(path=None, mtime=None, values={})
    check("a corrupt store never raises and falls back to defaults", tunables.get("MAX_FACTS") == 80)
    check("an unregistered name returns the caller's default", tunables.get("NOPE", 7) == 7)

    ok, _ = tunables.set_override("JARVIS_RAW_ARCHIVE", False)
    check("a bool override is accepted", ok and tunables.get("JARVIS_RAW_ARCHIVE") is False)
    os.environ["JARVIS_RAW_ARCHIVE"] = "1"
    try:
        check("the environment beats a saved override for an env tunable", tunables.get("JARVIS_RAW_ARCHIVE") is True)
        snap = {t["name"]: t for t in tunables.snapshot()}["JARVIS_RAW_ARCHIVE"]
        check("snapshot says the environment wins", snap["env_wins"] and snap["source"] == "env", snap)
    finally:
        os.environ.pop("JARVIS_RAW_ARCHIVE", None)
    ok, _ = tunables.reset_override("JARVIS_RAW_ARCHIVE")
    check("reset removes the override", ok and tunables.get("JARVIS_RAW_ARCHIVE") is True)


def test_override_reaches_a_real_process():
    _reset_home()
    tunables.set_override("MAX_FACTS", 123)
    code = "import sys; sys.path.insert(0, %r)\nfrom jarvis import memory, raw_archive\nprint(memory.MAX_FACTS, raw_archive.enabled())" % str(_JARVIS_CLI)
    out = subprocess.run([sys.executable, "-c", code], env=_env(), capture_output=True, text=True, timeout=120)
    check("a fresh process imports the overridden constant", out.stdout.split()[:1] == ["123"], out.stderr[-300:] + out.stdout)
    tunables.set_override("JARVIS_RAW_ARCHIVE", False)
    out = subprocess.run([sys.executable, "-c", code], env=_env(), capture_output=True, text=True, timeout=120)
    check("raw_archive.enabled() follows a saved off switch", out.stdout.split()[-1:] == ["False"], out.stdout)
    out = subprocess.run([sys.executable, "-c", code], env=_env(JARVIS_RAW_ARCHIVE="1"), capture_output=True, text=True, timeout=120)
    check("and the environment still turns it back on", out.stdout.split()[-1:] == ["True"], out.stdout)
    out = subprocess.run([sys.executable, "-c", code], env=_env(JARVIS_RAW_ARCHIVE="0"), capture_output=True, text=True, timeout=120)
    check("and JARVIS_RAW_ARCHIVE=0 still turns it off (unchanged)", out.stdout.split()[-1:] == ["False"], out.stdout)


# ---- 3. reads -------------------------------------------------------------------------
def test_reads_mask_and_reveal():
    _reset_home()
    _put("ai_config.json", _ai_doc())
    masked = sa.read("ai_config.json")
    check("read ok and versioned", masked["ok"] and len(masked["version"]) == 16)
    check("the secret is not in the masked text", SECRET not in masked["text"])
    check("one hidden value is reported (the empty key stays empty)", masked["masked"] == 1, masked["masked"])
    check("an empty key is shown as empty, not masked", '""' in masked["text"])
    revealed = sa.read("ai_config.json", reveal=True)
    check("reveal returns the real value", SECRET in revealed["text"] and revealed["masked"] == 0)
    check("masked and revealed reads share one version", masked["version"] == revealed["version"])

    _put("mcp_config.json", {"servers": {"x": {"command": "run", "env": {"PLAIN": "visible?"}, "headers": {"X": "h"}}}})
    text = sa.read("mcp_config.json")["text"]
    check("mcp env and headers values are hidden whatever they look like", "visible?" not in text and '"h"' not in text)

    (JDIR / "broken.json").write_text("{oops", encoding="utf-8")
    broken = sa.read("broken.json")
    check("a file that does not parse comes back as text with the reason",
          broken["ok"] and broken["parse_ok"] is False and broken["text"] == "{oops" and "line" in broken["parse_error"])
    check("a missing file reads as an empty document", sa.read("onboarding.json")["exists"] is False)

    for bad in ("../secret.json", "..\\x.json", "a/b.json", "channels/../x.json", "x.txt", "", None, "channels/", ".json"):
        check(f"name {bad!r} is refused", sa.read(bad)["ok"] is False)


def test_catalog_groups():
    _reset_home()
    _put("tool_safety.json", {})
    _put("key_health.json", {})
    _put("scheduled.json", {})
    _put("channels/people.json", {})
    cat = sa.catalog()
    by = {f["name"]: f for f in cat["files"]}
    check("catalog ok with tunables and plumbing", cat["ok"] and cat["tunables"] and cat["plumbing"])
    check("safety files are guarded and editable", by["tool_safety.json"]["guarded"] and by["tool_safety.json"]["editable"])
    check("channels.json is a safety file", by["channels.json"]["group"] == sa.GROUP_SAFETY)
    check("a state file is read-only", by["key_health.json"]["editable"] is False)
    check("a panel-owned file is read-only", by["scheduled.json"]["editable"] is False)
    check("channels/ files are listed and read-only", by["channels/people.json"]["editable"] is False)
    check("a known settings file is listed before it exists", "ai_config.json" in by and by["ai_config.json"]["exists"] is False)
    check("no secret-looking value is in the catalog", SECRET not in json.dumps(cat))


# ---- 4. writes ------------------------------------------------------------------------
def test_secret_round_trip_and_edit():
    _reset_home()
    _put("ai_config.json", _ai_doc())
    r = sa.read("ai_config.json")
    doc = json.loads(r["text"])
    doc["defaults"]["max_tokens"] = 900
    res = sa.write("ai_config.json", json.dumps(doc), r["version"])
    check("saving the masked document succeeds", res["ok"] and res["changed"], res)
    on_disk = json.loads((JDIR / "ai_config.json").read_text(encoding="utf-8"))
    check("the real secret survived the round trip", on_disk["providers"][0]["api_keys"][0] == SECRET)
    check("the edit landed", on_disk["defaults"]["max_tokens"] == 900)
    check("a backup of the previous file exists", len(sa.list_backups("ai_config.json")["backups"]) == 1)

    again = sa.write("ai_config.json", json.dumps(doc), res["version"])
    check("saving an identical document is a no-op with no new backup",
          again["ok"] and again["changed"] is False and len(sa.list_backups("ai_config.json")["backups"]) == 1)

    r2 = sa.read("ai_config.json")
    doc2 = json.loads(r2["text"])
    doc2["providers"][0]["api_keys"] = [sa.MASK, "", sa.MASK]
    res = sa.write("ai_config.json", json.dumps(doc2), r2["version"])
    check("a hidden value that has nothing to map back to is refused",
          res["ok"] is False and "hidden value" in " ".join(res.get("problems", [])), res)

    r3 = sa.read("ai_config.json", reveal=True)
    doc3 = json.loads(r3["text"])
    doc3["providers"][0]["api_keys"][0] = "sk-new"
    res = sa.write("ai_config.json", json.dumps(doc3), r3["version"])
    check("a revealed secret can be changed", res["ok"] and
          json.loads((JDIR / "ai_config.json").read_text(encoding="utf-8"))["providers"][0]["api_keys"][0] == "sk-new")

    log = (JDIR / sa.BACKUP_DIR_NAME / sa.CHANGE_LOG_NAME).read_text(encoding="utf-8")
    check("the change log holds paths, never values", "sk-new" not in log and SECRET not in log and "max_tokens" in log, log[:300])


def test_refusals():
    _reset_home()
    _put("ai_config.json", _ai_doc())
    r = sa.read("ai_config.json")
    good = json.loads(r["text"])

    res = sa.write("ai_config.json", json.dumps(good), "0" * 16)
    check("a stale version is refused with code stale", res["ok"] is False and res["code"] == "stale", res)

    res = sa.write("ai_config.json", "{nope", r["version"])
    check("invalid JSON is refused with a line and column", res["ok"] is False and "line" in res["error"], res)

    flipped = json.loads(r["text"]); flipped["defaults"]["max_tokens"] = "lots"
    res = sa.write("ai_config.json", json.dumps(flipped), r["version"])
    check("a number turned into text is refused naming the path",
          res["ok"] is False and any("defaults.max_tokens" in p for p in res["problems"]), res)

    bad = json.loads(r["text"]); bad["providers"][0]["type"] = "carrier-pigeon"
    res = sa.write("ai_config.json", json.dumps(bad), r["version"])
    check("an unknown provider type is refused", res["ok"] is False and any("type" in p for p in res["problems"]), res)

    two = json.loads(r["text"]); two["providers"].append(dict(two["providers"][0]))
    res = sa.write("ai_config.json", json.dumps(two), r["version"])
    check("a duplicate provider name is refused", res["ok"] is False and any("twice" in p for p in res["problems"]), res)

    many = json.loads(r["text"]); many["defaults"]["max_tokens"] = -3; many["defaults"]["timeout"] = 0
    res = sa.write("ai_config.json", json.dumps(many), r["version"])
    check("all problems are reported together", res["ok"] is False and len(res["problems"]) >= 2, res)

    res = sa.write("ai_config.json", "[]", r["version"])
    check("the top level cannot change from object to list", res["ok"] is False, res)
    check("nothing was written by any refusal",
          json.loads((JDIR / "ai_config.json").read_text(encoding="utf-8")) == _ai_doc()
          and sa.list_backups("ai_config.json")["backups"] == [])

    _put("scheduled.json", {"jobs": []})
    ro = sa.write("scheduled.json", "{}", sa.read("scheduled.json")["version"])
    check("a panel-owned file is read-only", ro["ok"] is False and ro["code"] == "read_only", ro)
    check("a path outside the settings folder cannot be written", sa.write("../x.json", "{}", "absent")["ok"] is False)

    for var, val in (("JARVIS_CHANNEL_SENDER", "someone"), ("JARVIS_SCHEDULED", "1"), ("JARVIS_TASK_ID", "t1"), ("JARVIS_CONTEXT", "unattended")):
        os.environ[var] = val
        try:
            res = sa.write("ai_config.json", json.dumps(good), r["version"])
            tun = sa.tunable_set("MAX_FACTS", 100)
            check(f"{var} set: file writes and tunable writes are refused",
                  res["ok"] is False and res["code"] == "unattended" and tun["ok"] is False and tun["code"] == "unattended")
            check(f"{var} set: reads still work", sa.read("ai_config.json")["ok"])
        finally:
            os.environ.pop(var, None)


def test_guarded_files():
    _reset_home()
    _put("tool_safety.json", {"tools": {"run_shell": {"confirm": True}}})
    r = sa.read("tool_safety.json")
    doc = json.loads(r["text"]); doc["tools"]["run_shell"]["confirm"] = False
    res = sa.write("tool_safety.json", json.dumps(doc), r["version"])
    check("a guarded file is refused without confirmation", res["ok"] is False and res["code"] == "needs_confirm", res)
    check("and the file is untouched", json.loads((JDIR / "tool_safety.json").read_text(encoding="utf-8"))["tools"]["run_shell"]["confirm"] is True)
    res = sa.write("tool_safety.json", json.dumps(doc), r["version"], confirm_guarded=True)
    check("with the confirmation it is written", res["ok"] and res["changed"], res)
    res = sa.restore("tool_safety.json", None, None)
    check("undo of a guarded file also needs the confirmation", res["ok"] is False and res["code"] == "needs_confirm", res)
    res = sa.restore("tool_safety.json", None, None, confirm_guarded=True)
    check("and works with it", res["ok"] and json.loads((JDIR / "tool_safety.json").read_text(encoding="utf-8"))["tools"]["run_shell"]["confirm"] is True, res)


def test_set_value_backup_restore_and_prune():
    _reset_home()
    res = sa.set_value("notify_config.json", ["levels", "ambient"], 3, "absent")
    check("a never-written section is created by set_value", res["ok"] and res["changed"], res)
    check("the value is on disk", json.loads((JDIR / "notify_config.json").read_text(encoding="utf-8"))["levels"]["ambient"] == 3)
    check("a brand-new file made no backup (nothing to back up)", sa.list_backups("notify_config.json")["backups"] == [])

    v = sa.read("notify_config.json")["version"]
    res = sa.set_value("notify_config.json", ["levels", "ambient"], 4, "0" * 16)
    check("set_value refuses a stale version", res["ok"] is False and res["code"] == "stale", res)
    res = sa.set_value("notify_config.json", ["levels", "ambient"], 4, v)
    check("set_value with the current version works and backs up", res["ok"] and len(sa.list_backups("notify_config.json")["backups"]) == 1, res)
    res = sa.set_value("ai_config.json", ["providers", 0, "enabled"], False, "absent")
    check("a path through a list that does not exist is refused", res["ok"] is False, res)
    res = sa.set_value("ai_config.json", [], 1, "absent")
    check("an empty path is refused", res["ok"] is False and res["code"] == "bad_path", res)

    res = sa.restore("notify_config.json", None, None)
    check("undo puts the previous value back", res["ok"] and json.loads((JDIR / "notify_config.json").read_text(encoding="utf-8"))["levels"]["ambient"] == 3, res)
    check("undo backed up the state it replaced (so undo can be undone)", len(sa.list_backups("notify_config.json")["backups"]) == 2)
    bad = sa.restore("notify_config.json", "../../etc", None)
    check("a malformed backup id is refused", bad["ok"] is False and bad["code"] == "bad_backup", bad)
    check("undo with no backups says so", sa.restore("onboarding.json", None, None)["code"] == "no_backup")

    for i in range(sa.KEEP_BACKUPS + 6):
        cur = sa.read("notify_config.json")["version"]
        sa.set_value("notify_config.json", ["levels", "ambient"], 5 if i % 2 == 0 else 1, cur)
    check("backups are pruned to KEEP_BACKUPS", len(sa.list_backups("notify_config.json")["backups"]) <= sa.KEEP_BACKUPS,
          len(sa.list_backups("notify_config.json")["backups"]))


def test_diff_hides_secrets():
    _reset_home()
    _put("ai_config.json", _ai_doc())
    r = sa.read("ai_config.json", reveal=True)
    doc = json.loads(r["text"]); doc["providers"][0]["api_keys"][0] = "sk-other"; doc["defaults"]["timeout"] = 45
    p = sa.preview("ai_config.json", json.dumps(doc), r["version"])
    paths = {c["path"]: c for c in p["changes"]}
    check("preview lists both changes", "defaults.timeout" in paths and "providers[0].api_keys[0]" in paths, paths)
    check("a changed secret shows as changed but its value is hidden",
          paths["providers[0].api_keys[0]"]["after"] == sa.MASK and SECRET not in json.dumps(p) and "sk-other" not in json.dumps(p))
    check("preview wrote nothing", sa.list_backups("ai_config.json")["backups"] == [])
    check("preview says a guarded/stale state honestly", p["valid"] is True and p["stale"] is False)
    stale = sa.preview("ai_config.json", json.dumps(doc), "0" * 16)
    check("preview flags a stale version", stale["stale"] is True and stale["valid"] is False)


def test_commands_validation_uses_the_builder_rules():
    _reset_home()
    _put("commands.json", {"commands": {}})
    r = sa.read("commands.json")
    doc = {"commands": {"settings-admin": {"type": "shell", "command": "echo hi"}}}
    res = sa.write("commands.json", json.dumps(doc), r["version"])
    check("a command named after a built-in is refused with the builder's reason",
          res["ok"] is False and any("commands.settings-admin" in p for p in res["problems"]), res)


# ---- 5. CLI ---------------------------------------------------------------------------
def _cli(sub, payload=None, **env):
    proc = subprocess.run([sys.executable, "-m", "jarvis", "settings-admin", sub], input=None if payload is None else json.dumps(payload),
                          capture_output=True, text=True, env=_env(**env), timeout=120, cwd=str(_JARVIS_CLI))
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"_raw": proc.stdout[-300:], "_err": proc.stderr[-300:]}


def test_cli_verb():
    _reset_home()
    _put("ai_config.json", _ai_doc())
    code, out = _cli("catalog")
    check("catalog prints one JSON object", code == 0 and out.get("ok") is True and out["files"], out)
    code, out = _cli("read", {"name": "ai_config.json"})
    check("read through the CLI masks the secret", out.get("ok") and SECRET not in out["text"], out)
    version = out["version"]
    doc = json.loads(out["text"]); doc["defaults"]["max_tokens"] = 1000
    code, out = _cli("write", {"name": "ai_config.json", "text": json.dumps(doc), "base_version": version})
    check("write through the CLI works", out.get("ok") and out.get("changed"), out)
    code, out = _cli("write", {"name": "ai_config.json", "text": json.dumps(doc), "base_version": version})
    check("replaying the same write is stale", out.get("ok") is False and out.get("code") == "stale", out)
    code, out = _cli("write", {"name": "ai_config.json", "text": "{}", "base_version": "x"}, JARVIS_CHANNEL_SENDER="intruder")
    check("a write from a chat-triggered process is refused", out.get("ok") is False and out.get("code") == "unattended", out)
    code, out = _cli("tunable-set", {"name": "MAX_FACTS", "value": 150})
    check("tunable-set works", out.get("ok") and out.get("value") == 150, out)
    code, out = _cli("tunable-set", {"name": "MAX_TOOL_ROUNDS", "value": 50})
    check("an unregistered tunable is refused", out.get("ok") is False, out)
    code, out = _cli("tunable-reset", {"name": "MAX_FACTS"})
    check("tunable-reset works", out.get("ok"), out)
    code, out = _cli("changes")
    check("changes lists what was done", out.get("ok") and len(out["changes"]) >= 3, out)
    _put("tool_safety.json", {"tools": {}})
    code, out = _cli("write", {"name": "tool_safety.json", "text": "{}", "base_version": sa.read("tool_safety.json")["version"],
                               "confirm_guarded": "yes"})
    check("a truthy string is not a confirmation (must be exactly true)", out.get("ok") is False and out.get("code") == "needs_confirm", out)
    code, out = _cli("frobnicate", {})
    check("an unknown action prints usage as JSON, not a traceback", out.get("ok") is False and "usage" in out.get("error", ""), out)
    proc = subprocess.run([sys.executable, "-m", "jarvis", "settings-admin", "read"], input="not json", capture_output=True, text=True,
                          env=_env(), timeout=60, cwd=str(_JARVIS_CLI))
    check("malformed stdin is a JSON error, not a crash", json.loads(proc.stdout).get("ok") is False)


# ---- 6. the model cannot reach it -----------------------------------------------------
def test_model_cannot_reach_the_writers():
    pkg = _JARVIS_CLI / "jarvis"
    offenders = []
    for path in sorted(pkg.rglob("*.py")):
        if path.name in ("settings_admin.py", "tunables.py", "cli.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "settings_admin" in text or "set_override" in text or "reset_override" in text:
            offenders.append(path.name)
    check("only cli.py imports settings_admin; nothing else calls the tunable writers", offenders == [], offenders)
    from jarvis import tools
    names = [n.lower() for n in tools.TOOLS]
    check("no model tool is named for settings or tunables", not [n for n in names if "setting" in n or "tunable" in n], names)
    from jarvis import reserved_names
    check("settings-admin is a reserved command name", "settings-admin" in reserved_names.RESERVED_NAMES)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n== {name}")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(f"{name} ran without raising", False, e)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
