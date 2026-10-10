"""L.43 follow-up -- "changed from default" and per-value reset for the Files tree.

Pinned here:
  1. default_doc: a private copy (changing it never changes the module's constant); known
     files resolve; a file with no recorded default gives None.
  2. defaults_report: nothing for a file with no defaults; "file not created" is not a
     list of differences; changed / missing / extra are told apart; a list is one value;
     secret values are shown as the mask.
  3. reset_value: puts ONE value back through the same gate as every write (backup, change
     log with paths only, stale refusal, unattended refusal, safety files need the confirm);
     repairs a value of the wrong type; refuses a path the default does not have and a file
     with no default; recreates a deleted key and a deleted section.
  4. the CLI verbs, the server routes and the front-end wiring exist, and no model-facing
     module can reach them.

Run: python3 tests/test_l43_tree.py   (no key, no network)
"""
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l43tree-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT"):
    os.environ.pop(_var, None)

_ROOT = Path(__file__).resolve().parent.parent
_JARVIS_CLI = _ROOT / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

from jarvis import settings_admin as sa  # noqa: E402

PASS, FAIL = [], []
JDIR = Path(_HOME) / ".jarvis"
SECRET = "sk-tree-SECRET-987654321"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


def _reset_home():
    import shutil
    shutil.rmtree(JDIR, ignore_errors=True)
    JDIR.mkdir(parents=True, exist_ok=True)


def _put(name, doc):
    path = JDIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _get(name):
    return json.loads((JDIR / name).read_text(encoding="utf-8"))


def _version(name):
    return sa.version_of((JDIR / name).read_text(encoding="utf-8"))


def _by_path(report):
    return {d["path_text"]: d for d in report["deviations"]}


# ---- 1. default_doc ---------------------------------------------------------------------
def test_default_doc():
    for name in sa._DEFAULT_SOURCES:
        doc = sa.default_doc(name)
        check(f"a built-in default is recorded for {name}", isinstance(doc, (dict, list)), type(doc).__name__)
    check("a file with no recorded default gives None", sa.default_doc("commands.json") is None and sa.default_doc("nope.json") is None)
    from jarvis import notifier
    d = sa.default_doc("notify_config.json")
    d["levels"]["notify"] = 99
    check("the document handed out is a private copy", notifier.DEFAULT_CONFIG["levels"]["notify"] != 99)
    check("only files with a default are in the table, and every one is a real settings file",
          all(n in sa._FILES for n in sa._DEFAULT_SOURCES))


# ---- 2. defaults_report -----------------------------------------------------------------
def test_defaults_report():
    _reset_home()
    r = sa.defaults_report("commands.json")
    check("a file with no defaults says so and lists nothing", r["ok"] and r["has_defaults"] is False and r["deviations"] == [], r)
    r = sa.defaults_report("notify_config.json")
    check("a file that does not exist is not a list of differences", r["ok"] and r["has_defaults"] and r["exists"] is False and r["deviations"] == [], r)
    check("a bad name is refused", sa.defaults_report("../x.json")["ok"] is False)

    base = sa.default_doc("notify_config.json")
    _put("notify_config.json", base)
    r = sa.defaults_report("notify_config.json")
    check("a file equal to its default has no differences", r["ok"] and r["exists"] and r["deviations"] == [], r)

    doc = copy.deepcopy(base)
    doc["levels"]["notify"] = 5
    del doc["levels"]["task"]
    doc["levels"]["mine"] = 3
    doc["enabled"] = not base["enabled"]
    _put("notify_config.json", doc)
    devs = _by_path(sa.defaults_report("notify_config.json"))
    check("a changed leaf is reported as changed, with a path list the UI can send back",
          devs["levels.notify"]["state"] == "changed" and devs["levels.notify"]["path"] == ["levels", "notify"]
          and devs["levels.notify"]["resettable"] is True, devs)
    check("a key the file lacks is reported as missing and can be reset", devs["levels.task"]["state"] == "missing" and devs["levels.task"]["resettable"])
    check("a key the default lacks is 'extra' and has nothing to reset to", devs["levels.mine"]["state"] == "extra" and devs["levels.mine"]["resettable"] is False)
    check("a top-level switch is found too", devs["enabled"]["state"] == "changed")
    check("previews carry both sides (current and default) where they exist",
          devs["levels.notify"]["current"] == "5" and devs["levels.notify"]["default"] == str(base["levels"]["notify"])
          and devs["levels.task"]["current"] is None and devs["levels.mine"]["default"] is None)

    doc = copy.deepcopy(base)
    doc["channels"]["reminder"] = ["inbox"]
    _put("notify_config.json", doc)
    devs = _by_path(sa.defaults_report("notify_config.json"))
    check("a list is compared and reported as one value", "channels.reminder" in devs and not any(k.startswith("channels.reminder[") for k in devs), list(devs))

    _put("notify_config.json", {"enabled": "yes"})
    check("a type difference is a difference", _by_path(sa.defaults_report("notify_config.json"))["enabled"]["state"] == "changed")

    (JDIR / "notify_config.json").write_text("{not json", encoding="utf-8")
    r = sa.defaults_report("notify_config.json")
    check("a file that does not parse is an error, not a crash", r["ok"] is False and "JSON" in r["error"], r)

    devs = sa._deviations({"api_key": "", "token": "x"}, {"api_key": SECRET, "token": SECRET + "2"}, "x.json")
    check("secret values are shown as the mask, never as the value",
          all(SECRET not in json.dumps(d) for d in devs) and {d["current"] for d in devs} == {sa.MASK}, devs)
    check("an unset secret is not masked (it still looks unset)", sa._deviations({"api_key": "x"}, {"api_key": ""}, "x.json")[0]["current"] == '""')
    many = sa._deviations({}, {str(i): i for i in range(sa.MAX_DIFF_ENTRIES + 50)}, "x.json")
    check("the list is capped", len(many) == sa.MAX_DIFF_ENTRIES)


# ---- 3. reset_value ---------------------------------------------------------------------
def test_reset_value():
    _reset_home()
    base = sa.default_doc("notify_config.json")
    doc = copy.deepcopy(base)
    doc["levels"]["notify"] = 5
    doc["levels"]["mine"] = 3
    _put("notify_config.json", doc)

    res = sa.reset_value("notify_config.json", ["levels", "notify"], _version("notify_config.json"))
    check("a reset succeeds and reports a change", res.get("ok") and res.get("changed"), res)
    after = _get("notify_config.json")
    check("the value is the default and nothing else moved", after["levels"]["notify"] == base["levels"]["notify"] and after["levels"]["mine"] == 3 and after["enabled"] == doc["enabled"], after)
    check("a backup of the file as it was is made", sa.list_backups("notify_config.json")["backups"], sa.list_backups("notify_config.json"))
    log = (JDIR / sa.BACKUP_DIR_NAME / sa.CHANGE_LOG_NAME).read_text(encoding="utf-8")
    last = json.loads(log.strip().splitlines()[-1])
    check("the change log says 'reset', names the path and holds no value", last["action"] == "reset" and "levels.notify" in last["paths"] and "5" not in json.dumps(last.get("paths")) .replace("levels.notify", ""), last)
    check("undo restores the previous value (the generic restore works on a reset)",
          sa.restore("notify_config.json", None, _version("notify_config.json")).get("ok") and _get("notify_config.json")["levels"]["notify"] == 5)

    res = sa.reset_value("notify_config.json", ["levels", "notify"], "0000000000000000")
    check("a stale reset is refused", res.get("ok") is False and res.get("code") == "stale", res)
    res = sa.reset_value("notify_config.json", ["levels", "mine"], _version("notify_config.json"))
    check("a path the default does not have is refused with the reason", res.get("ok") is False and res.get("code") == "no_default" and "nothing to reset" in res["error"], res)
    res = sa.reset_value("notify_config.json", [], _version("notify_config.json"))
    check("an empty path is refused", res.get("ok") is False and res.get("code") == "bad_path", res)
    res = sa.reset_value("notify_config.json", "levels", _version("notify_config.json"))
    check("a path that is not a list is refused", res.get("ok") is False and res.get("code") == "bad_path", res)
    _put("commands.json", {"commands": {}})
    res = sa.reset_value("commands.json", ["commands"], _version("commands.json"))
    check("a file with no default has nothing to reset to", res.get("ok") is False and res.get("code") == "no_defaults", res)

    # a value of the wrong type is exactly what a reset is for
    doc = copy.deepcopy(base)
    doc["levels"]["notify"] = "loud"
    _put("notify_config.json", doc)
    blocked = sa.set_value("notify_config.json", ["levels", "notify"], 2, _version("notify_config.json"))
    check("a plain set may not flip a text value to a number (unchanged rule)", blocked.get("ok") is False and "keep its type" in blocked.get("error", ""), blocked)
    res = sa.reset_value("notify_config.json", ["levels", "notify"], _version("notify_config.json"))
    check("a reset DOES repair a wrong-typed value", res.get("ok") and _get("notify_config.json")["levels"]["notify"] == base["levels"]["notify"], res)

    # a deleted key and a deleted section come back
    doc = copy.deepcopy(base)
    del doc["levels"]["task"]
    _put("notify_config.json", doc)
    res = sa.reset_value("notify_config.json", ["levels", "task"], _version("notify_config.json"))
    check("a missing key is put back", res.get("ok") and _get("notify_config.json")["levels"]["task"] == base["levels"]["task"], res)
    _put("notify_config.json", {"enabled": True})
    res = sa.reset_value("notify_config.json", ["levels", "notify"], _version("notify_config.json"))
    check("a missing section is created for it", res.get("ok") and _get("notify_config.json")["levels"] == {"notify": base["levels"]["notify"]}, res)

    # a list resets as a whole
    doc = copy.deepcopy(base)
    doc["channels"]["reminder"] = ["inbox"]
    _put("notify_config.json", doc)
    res = sa.reset_value("notify_config.json", ["channels", "reminder"], _version("notify_config.json"))
    check("a list is reset as a whole", res.get("ok") and _get("notify_config.json")["channels"]["reminder"] == base["channels"]["reminder"], res)

    # nothing to do
    res = sa.reset_value("notify_config.json", ["levels", "task"], _version("notify_config.json"))
    check("resetting a value that already is the default changes nothing", res.get("ok") and res.get("changed") is False, res)

    # safety files need the confirm; the default is a private copy
    pol = copy.deepcopy(sa.default_doc("policy.json"))
    pol["enabled"] = False
    _put("policy.json", pol)
    res = sa.reset_value("policy.json", ["enabled"], _version("policy.json"))
    check("a safety file's reset needs the confirmation", res.get("ok") is False and res.get("code") == "needs_confirm", res)
    res = sa.reset_value("policy.json", ["enabled"], _version("policy.json"), confirm_guarded=True)
    check("...and works with it", res.get("ok") and _get("policy.json")["enabled"] is True, res)

    # unattended
    os.environ["JARVIS_SCHEDULED"] = "1"
    try:
        _put("notify_config.json", {"enabled": False})
        res = sa.reset_value("notify_config.json", ["enabled"], _version("notify_config.json"))
        check("a scheduled process may not reset", res.get("ok") is False and res.get("code") == "unattended", res)
        check("...and read-only reports still work there", sa.defaults_report("notify_config.json")["ok"] is True)
    finally:
        os.environ.pop("JARVIS_SCHEDULED", None)

    # read-only groups
    _put("daemons.json", {"daemons": {}})
    res = sa.reset_value("daemons.json", ["daemons"], _version("daemons.json"))
    check("a file another panel owns is not reset from here", res.get("ok") is False, res)


# ---- 4. CLI, routes, wiring, reach -------------------------------------------------------
def _cli(sub, payload=None, **env):
    e = dict(os.environ)
    e.update({"HOME": _HOME, "USERPROFILE": _HOME})
    e.update(env)
    proc = subprocess.run([sys.executable, "-m", "jarvis", "settings-admin", sub], input=None if payload is None else json.dumps(payload),
                          capture_output=True, text=True, env=e, timeout=120, cwd=str(_JARVIS_CLI))
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"_raw": proc.stdout[-300:], "_err": proc.stderr[-300:]}


def test_cli_and_wiring():
    _reset_home()
    base = sa.default_doc("notify_config.json")
    doc = copy.deepcopy(base)
    doc["levels"]["notify"] = 5
    _put("notify_config.json", doc)
    code, out = _cli("defaults", {"name": "notify_config.json"})
    check("settings-admin defaults prints the report", code == 0 and out.get("ok") and out["deviations"] and out["deviations"][0]["path"] == ["levels", "notify"], out)
    code, out = _cli("reset", {"name": "notify_config.json", "path": ["levels", "notify"], "base_version": _version("notify_config.json")})
    check("settings-admin reset resets", code == 0 and out.get("ok") and _get("notify_config.json")["levels"]["notify"] == base["levels"]["notify"], out)
    _put("notify_config.json", doc)
    code, out = _cli("reset", {"name": "notify_config.json", "path": ["levels", "notify"], "base_version": _version("notify_config.json")}, JARVIS_CHANNEL_SENDER="intruder")
    check("a chat-triggered process cannot reset", out.get("ok") is False and out.get("code") == "unattended", out)

    server = (_ROOT / "web" / "server.js").read_text(encoding="utf-8")
    check("server.js has both routes behind the same-origin guard",
          re.search(r'app\.post\("/api/settings/file/defaults", requireSettingsOrigin', server) is not None
          and re.search(r'app\.post\("/api/settings/file/reset", requireSettingsOrigin', server) is not None)
    check("the reset route forwards the confirmation only when it is exactly true", "confirm_guarded: req.body.confirm_guarded === true" in server)
    js = (_ROOT / "web" / "public" / "settings.js").read_text(encoding="utf-8")
    check("the tree reads defaults and resets through those routes", "/api/settings/file/defaults" in js and "/api/settings/file/reset" in js)
    check("the tree edits a single value through the existing set route", "/api/settings/file/set" in js)
    check("the tree never builds markup from a value", ".innerHTML" not in js)
    css = (_ROOT / "web" / "public" / "settings.css").read_text(encoding="utf-8")
    check("the tree has styles", ".adv-tree__row" in css and ".adv-treewrap" in css)

    offenders = []
    for path in sorted((_JARVIS_CLI / "jarvis").rglob("*.py")):
        if path.name in ("settings_admin.py", "tunables.py", "cli.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "settings_admin" in text or "reset_value" in text and "settings" in text.lower() and "default_doc" in text:
            offenders.append(path.name)
    check("nothing else reaches the settings writers (the reset included)", offenders == [], offenders)
    from jarvis import tools
    check("no model tool is named reset-something for settings", not [n for n in tools.TOOLS if "setting" in n.lower() or "reset_value" in n.lower()])


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
