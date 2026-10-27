"""P3: the CurseForge key (modhosts/config.py) and everything around it: the Settings back end
(settings_admin entries for curseforge.json and modhosts/cache.json), the policy entry, the doctor
check and the Settings page entry (web/public/settings.js, checked as text and with `node --check`).

No network, no real key, `HOME` redirected before any jarvis import. The key used throughout is an
obviously fake one, and the promise tested is: it never appears in a settings read, a result, a diff,
the change log, a doctor report, `public_view()`, an error text or a message, and the Settings page
never asks for it back.

Run: python3 tests/test_modhosts_config.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-p3-cfg-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("CURSEFORGE_API_KEY", "JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT"):
    os.environ.pop(_var, None)

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "jarvis-cli"))

from jarvis import doctor, policy, settings_admin as sa  # noqa: E402
from jarvis.modhosts import config  # noqa: E402

PASS, FAIL = [], []
JDIR = Path(_HOME) / ".jarvis"
KEY_FILE = JDIR / "curseforge.json"
FAKE_KEY = "cf-test-FAKE-KEY-not-a-real-key-0123456789"
OTHER_KEY = "cf-test-OTHER-FAKE-key-9876543210"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


def _reset():
    shutil.rmtree(JDIR, ignore_errors=True)
    JDIR.mkdir(parents=True, exist_ok=True)
    os.environ.pop("CURSEFORGE_API_KEY", None)
    for var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT"):
        os.environ.pop(var, None)


def _put(doc):
    JDIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")


def _dump(*things):
    return json.dumps(things, default=str)


# ---- 1. config ------------------------------------------------------------------------------
def test_get_key():
    _reset()
    check("no file, no variable: no key", config.get_key() is None)
    check("the file lives in ~/.jarvis/curseforge.json", config.config_path() == KEY_FILE)
    os.environ["CURSEFORGE_API_KEY"] = OTHER_KEY
    check("the environment variable is the fallback", config.get_key() == OTHER_KEY)
    _put({"api_key": FAKE_KEY})
    check("the file wins over the variable", config.get_key() == FAKE_KEY)
    _put({"api_key": ""})
    check("an empty key in the file falls back to the variable", config.get_key() == OTHER_KEY)
    os.environ.pop("CURSEFORGE_API_KEY")
    _put({"api_key": "  " + FAKE_KEY + "\n"})
    check("surrounding whitespace is removed", config.get_key() == FAKE_KEY)
    _put({"api_key": '"' + FAKE_KEY + '"'})
    check("one pair of surrounding quotes is removed (a paste accident)", config.get_key() == FAKE_KEY)
    _put({"api_key": "$2a$10$AbCdEf.GhIjKl/MnOpQrStUvWxYz0123456789abcdefghijklmn"})
    check("a real-looking key ($2a$10$..., dots and slashes) is valid", config.get_key() is not None)
    for label, text in (("not JSON", "{oops"), ("a list", "[1]"), ("a number", "7"), ("an empty file", "")):
        _put(text)
        check(f"a key file that is {label} gives no key and does not raise", config.get_key() is None)
        os.environ["CURSEFORGE_API_KEY"] = OTHER_KEY
        check(f"... and the variable still works ({label})", config.get_key() == OTHER_KEY)
        os.environ.pop("CURSEFORGE_API_KEY")
    _put({"api_key": 12345})
    check("a key that is not text is no key", config.get_key() is None)


def test_damaged_key():
    _reset()
    _put({"api_key": "has spaces inside"})
    check("a key with spaces inside is damaged: no key", config.get_key() is None)
    p = config.key_problem()
    check("the problem is one sentence that does not hold the key", p and "has spaces inside" not in p and "curseforge.json" in p, p)
    os.environ["CURSEFORGE_API_KEY"] = OTHER_KEY
    check("a damaged file key does not block a good variable", config.get_key() == OTHER_KEY and config.key_problem() == "")
    os.environ["CURSEFORGE_API_KEY"] = "bad variable value"
    _put({})
    check("a damaged variable is damaged too", config.get_key() is None and "CURSEFORGE_API_KEY" in config.key_problem()
          and "bad variable value" not in config.key_problem(), config.key_problem())
    os.environ.pop("CURSEFORGE_API_KEY")
    _put({"api_key": "x" * 201})
    check("a key longer than 200 characters is damaged", config.get_key() is None)
    _put({"api_key": "tab\tinside"})
    check("a control character is damaged", config.get_key() is None)
    check("looks_valid: printable, no space, 1 to 200", config.looks_valid("abc") and not config.looks_valid("") and not config.looks_valid("a b") and not config.looks_valid("é") and not config.looks_valid(None))
    check("clean: text only", config.clean(None) == "" and config.clean(" 'a b' ") == "a b" and config.clean("'") == "'")


def test_public_view():
    _reset()
    v = config.public_view()
    check("nothing set: not set, no source, file missing", v["api_key_set"] is False and v["source"] is None and v["file_state"] == "missing" and v["api_key"] == "", v)
    _put({"api_key": FAKE_KEY, "note": "x"})
    v = config.public_view()
    check("a key in the file: set, from the file, and blank in the view", v["api_key_set"] is True and v["source"] == "file" and v["api_key"] == "" and v["file_state"] == "ok", v)
    check("the view never contains the key", FAKE_KEY not in _dump(v) and FAKE_KEY not in repr(v), v)
    _put({})
    os.environ["CURSEFORGE_API_KEY"] = OTHER_KEY
    v = config.public_view()
    check("from the variable: set, source env, key absent", v["api_key_set"] is True and v["source"] == "env" and OTHER_KEY not in _dump(v), v)
    os.environ.pop("CURSEFORGE_API_KEY")
    _put("{broken " + FAKE_KEY)
    v = config.public_view()
    check("an unreadable file is reported as unreadable, and its text is not in the view", v["file_state"] == "unreadable" and FAKE_KEY not in _dump(v), v)
    msg = config.no_key_message()
    check("the no-key sentence says how to get a personal key",
          all(w in msg for w in ("console.curseforge.com", "personal", "curseforge.json", "CURSEFORGE_API_KEY", "Modrinth", "OptiFine")), msg)
    check("config reads nothing but the key file and the variable (no network module)",
          not re.search(r"^\s*(import|from)\s+(urllib|requests|http|socket)", (Path(config.__file__)).read_text(encoding="utf-8"), re.M))


# ---- 2. settings_admin ----------------------------------------------------------------------
def test_settings_read_masks():
    _reset()
    _put({"api_key": FAKE_KEY})
    r = sa.read("curseforge.json")
    check("a settings read of the key file succeeds", r["ok"] and r["exists"] and r["parse_ok"], r)
    check("the key is not in the read (text, meta, path): only the mask", FAKE_KEY not in _dump(r) and sa.MASK in r["text"] and r["masked"] == 1, r["text"])
    check("api_key is hidden by the automatic rule, no special case", sa.mask({"api_key": FAKE_KEY}, "curseforge.json") == {"api_key": sa.MASK})
    check("an empty key stays empty (so 'not set' still looks not set)", sa.mask({"api_key": ""}, "curseforge.json") == {"api_key": ""})
    _reset()
    r = sa.read("curseforge.json")
    check("a missing file reads as not existing, an empty object", r["ok"] and r["exists"] is False and json.loads(r["text"]) == {}, r)
    m = sa.meta_for("curseforge.json")
    check("curseforge.json is an editable settings file with a plain title and note",
          m["editable"] and m["group"] == sa.GROUP_SETTINGS and not m["guarded"] and "CurseForge" in m["title"] and "console.curseforge.com" in m["note"], m)
    names = [f["name"] for f in sa.catalog()["files"]]
    check("it is listed in the catalog even before it exists", "curseforge.json" in names, names)
    d = sa.default_doc("curseforge.json")
    check("a built-in default is recorded: an empty key", d == {"api_key": ""}, d)
    _put({"api_key": FAKE_KEY})
    rep = sa.defaults_report("curseforge.json")
    check("the defaults report shows the key as the mask", rep["ok"] and rep["has_defaults"] and FAKE_KEY not in _dump(rep) and any(x["current"] == sa.MASK for x in rep["deviations"]), rep)


def test_settings_set_key():
    _reset()
    v = sa.version_of(None)
    r = sa.set_value("curseforge.json", ["api_key"], FAKE_KEY, v)
    check("setting the key creates the file", r["ok"] and r["changed"] and json.loads(KEY_FILE.read_text(encoding="utf-8")) == {"api_key": FAKE_KEY}, r)
    check("the answer does not hold the key", FAKE_KEY not in _dump(r), r)
    check("config now finds it", config.get_key() == FAKE_KEY)
    r2 = sa.set_value("curseforge.json", ["api_key"], OTHER_KEY, r["version"])
    check("replacing it works and leaves a backup", r2["ok"] and r2["backup"], r2)
    check("the answer does not hold either key", FAKE_KEY not in _dump(r2) and OTHER_KEY not in _dump(r2))
    log = (JDIR / sa.BACKUP_DIR_NAME / sa.CHANGE_LOG_NAME).read_text(encoding="utf-8")
    check("the change log holds the path but never a value", "api_key" in log and FAKE_KEY not in log and OTHER_KEY not in log, log)
    ch = sa.recent_changes()
    check("recent changes (what Settings > About shows) never hold a key", FAKE_KEY not in _dump(ch) and OTHER_KEY not in _dump(ch))
    stale = sa.set_value("curseforge.json", ["api_key"], FAKE_KEY, "0123456789abcdef")
    check("a stale write is refused, without the key in the message", stale["ok"] is False and stale["code"] == "stale" and FAKE_KEY not in _dump(stale), stale)
    ver = sa.version_of(KEY_FILE.read_text(encoding="utf-8"))
    cleared = sa.set_value("curseforge.json", ["api_key"], "", ver)
    check("clearing the key writes an empty one", cleared["ok"] and json.loads(KEY_FILE.read_text(encoding="utf-8")) == {"api_key": ""} and config.get_key() is None, cleared)
    for label, bad in (("spaces", "two words"), ("a line break", "abc\ndef"), ("a very long key", "k" * 300), ("non-ASCII", "clé-é")):
        ver = sa.version_of(KEY_FILE.read_text(encoding="utf-8"))
        r = sa.set_value("curseforge.json", ["api_key"], bad, ver)
        check(f"a key with {label} is refused, and the text is not echoed", r["ok"] is False and r["code"] == "invalid" and "Paste just the key" in r["error"] and bad not in _dump(r), r)
    check("the refused values were not written", json.loads(KEY_FILE.read_text(encoding="utf-8")) == {"api_key": ""})
    ver = sa.version_of(KEY_FILE.read_text(encoding="utf-8"))
    r = sa.set_value("curseforge.json", ["api_key"], 123, ver)
    check("a number is not a key", r["ok"] is False and r["code"] == "invalid", r)
    r = sa.set_value("curseforge.json", ["api_key"], "  " + FAKE_KEY + " ", ver)
    check("a pasted key with spaces round it is accepted (cleaned when read)", r["ok"] and config.get_key() == FAKE_KEY, r)


def test_settings_write_roundtrip():
    _reset()
    _put({"api_key": FAKE_KEY})
    r = sa.read("curseforge.json")
    w = sa.write("curseforge.json", r["text"], r["version"])
    check("saving the masked text back keeps the real key (unmask)", w["ok"] and w.get("changed") is False and config.get_key() == FAKE_KEY, w)
    new = json.loads(r["text"])
    new["api_key"] = OTHER_KEY
    w = sa.write("curseforge.json", json.dumps(new), r["version"])
    check("typing a new key into the editor replaces it", w["ok"] and config.get_key() == OTHER_KEY and OTHER_KEY not in _dump(w), w)
    r = sa.read("curseforge.json", reveal=True)
    check("only an explicit reveal shows it (Advanced, deliberate)", OTHER_KEY in r["text"])
    r = sa.read("curseforge.json")
    w = sa.write("curseforge.json", "[1]", r["version"])
    check("the file must stay an object", w["ok"] is False and w["code"] == "invalid", w)


def test_settings_unattended():
    _reset()
    for var, val in (("JARVIS_CHANNEL", "discord"), ("JARVIS_SCHEDULED", "1"), ("JARVIS_TASK_ID", "t1"), ("JARVIS_CONTEXT", "unattended")):
        os.environ[var] = val
        r = sa.set_value("curseforge.json", ["api_key"], FAKE_KEY, sa.version_of(None))
        check(f"{var}: a chat, scheduled or background process cannot set the key", r["ok"] is False and r["code"] == "unattended" and not KEY_FILE.exists(), r)
        os.environ.pop(var)


def test_cache_entry():
    _reset()
    check("modhosts/cache.json is a known read-only state file",
          sa.meta_for("modhosts/cache.json")["group"] == sa.GROUP_STATE and sa.meta_for("modhosts/cache.json")["editable"] is False
          and "never holds your key" in sa.meta_for("modhosts/cache.json")["note"], sa.meta_for("modhosts/cache.json"))
    names = [f["name"] for f in sa.catalog()["files"]]
    check("it is not listed until the cache exists (like the other state files)", "modhosts/cache.json" not in names, names)
    (JDIR / "modhosts").mkdir(parents=True, exist_ok=True)
    (JDIR / "modhosts" / "cache.json").write_text(json.dumps({"entries": {"k": {"at": 1, "value": {"x": 1}}}}), encoding="utf-8")
    cat = sa.catalog()
    row = [f for f in cat["files"] if f["name"] == "modhosts/cache.json"]
    check("once it exists it is listed under State, not editable", row and row[0]["group"] == sa.GROUP_STATE and row[0]["editable"] is False and row[0]["exists"], row)
    r = sa.read("modhosts/cache.json")
    check("it can be read", r["ok"] and r["exists"] and json.loads(r["text"])["entries"]["k"]["at"] == 1, r)
    path = sa._path_for("modhosts/cache.json")
    check("it resolves to ~/.jarvis/modhosts/cache.json", path == JDIR / "modhosts" / "cache.json", path)
    w = sa.write("modhosts/cache.json", "{}", r["version"])
    s = sa.set_value("modhosts/cache.json", ["entries"], {}, r["version"])
    check("writes are refused as read-only", w["ok"] is False and w["code"] == "read_only" and s["ok"] is False and s["code"] == "read_only", (w, s))
    for bad in ("modhosts/other.json", "modhosts/../ai_config.json", "modhosts/a/b.json", "modhosts/", "Modhosts/cache.json", "channels/modhosts/cache.json", "web-data/modhosts/cache.json", "modhosts\\cache.json"):
        check(f"name {bad!r} is refused", sa._path_for(bad) is None and sa.read(bad)["ok"] is False)
    check("a top-level name still resolves where it did", sa._path_for("ai_config.json") == JDIR / "ai_config.json")


# ---- 3. policy ------------------------------------------------------------------------------
def test_policy():
    entry = [e for e in policy._SENSITIVE_PATHS if e[0] == "~/.jarvis/curseforge.json"]
    check("curseforge.json is on the sensitive list, labelled and weighted like the other key files",
          len(entry) == 1 and entry[0][2] == "your CurseForge API key" and entry[0][1] == [e for e in policy._SENSITIVE_PATHS if e[0] == "~/.jarvis/ai_config.json"][0][1], entry)
    r = policy.score_call("write_file", {"path": "~/.jarvis/curseforge.json"}, policy=policy.DEFAULT_POLICY)
    reasons = r[1] if isinstance(r, tuple) else r.get("reasons", [])
    check("writing it is scored as touching your CurseForge API key", any("your CurseForge API key" in str(x) for x in reasons), reasons)
    r = policy.score_call("write_file", {"path": "~/Documents/notes.txt"}, policy=policy.DEFAULT_POLICY)
    reasons = r[1] if isinstance(r, tuple) else r.get("reasons", [])
    check("an ordinary path is not", not any("CurseForge" in str(x) for x in reasons), reasons)


# ---- 4. doctor ------------------------------------------------------------------------------
def _by_id(checks):
    return {c.id: c for c in checks}


def test_doctor():
    _reset()
    got = _by_id(doctor.check_minecraft())
    check("no key: skip, and it says how to get one", got["minecraft.key"].status == doctor.SKIP and "console.curseforge.com" in got["minecraft.key"].fix, got["minecraft.key"].to_dict())
    _put({"api_key": FAKE_KEY})
    got = _by_id(doctor.check_minecraft())
    c = got["minecraft.key"]
    check("a key from the file: ok, offline, names the file, not the key", c.status == doctor.OK and "~/.jarvis/curseforge.json" in c.detail and FAKE_KEY not in _dump(c.to_dict()) and "--deep" in c.detail, c.to_dict())
    os.environ["CURSEFORGE_API_KEY"] = OTHER_KEY
    _put({})
    got = _by_id(doctor.check_minecraft())
    check("a key from the variable says so", got["minecraft.key"].status == doctor.OK and "CURSEFORGE_API_KEY" in got["minecraft.key"].detail and OTHER_KEY not in _dump([c.to_dict() for c in got.values()]))
    os.environ.pop("CURSEFORGE_API_KEY")
    _put({"api_key": "has a space"})
    got = _by_id(doctor.check_minecraft())
    check("a damaged key is a warning with a fix, without the key", got["minecraft.key"].status == doctor.WARN and got["minecraft.key"].fix and "has a space" not in _dump([c.to_dict() for c in got.values()]), got["minecraft.key"].to_dict())
    _put("{broken")
    got = _by_id(doctor.check_minecraft())
    check("a key file that cannot be read is a warning", got["minecraft.file"].status == doctor.WARN and got["minecraft.key"].status == doctor.SKIP, {k: v.to_dict() for k, v in got.items()})

    # --deep: one request, answered by a stand-in for the adapter's ping
    from jarvis.modhosts import curseforge
    real = curseforge.ping
    try:
        _put({"api_key": FAKE_KEY})
        for result, status in (({"ok": True}, doctor.OK),
                               ({"ok": False, "reason": "bad_key", "error": "refused " + "(no key text)"}, doctor.FAIL),
                               ({"ok": False, "reason": "offline", "error": "CurseForge did not answer"}, doctor.WARN),
                               ({"ok": False, "reason": "unsupported", "error": "no net"}, doctor.WARN)):
            curseforge.ping = lambda result=result: result
            got = _by_id(doctor.check_minecraft(deep=True))
            check(f"--deep with ping {result.get('reason', 'ok')}: {status}", got["minecraft.reach"].status == status and FAKE_KEY not in _dump(got["minecraft.reach"].to_dict()), got["minecraft.reach"].to_dict())

        def boom():
            raise RuntimeError("x")
        curseforge.ping = boom
        got = _by_id(doctor.check_minecraft(deep=True))
        check("--deep survives a ping that raises", got["minecraft.reach"].status == doctor.WARN)
        calls = []
        curseforge.ping = lambda: calls.append(1) or {"ok": True}
        doctor.check_minecraft()
        check("without --deep there is no request", calls == [])
    finally:
        curseforge.ping = real
    report = doctor.run(only=["minecraft"])
    check("`jarvis doctor minecraft` runs just this group and holds no key", [c["group"] for c in report["checks"]] == ["minecraft"] and FAKE_KEY not in _dump(report), report["checks"])
    check("the group is registered", "minecraft" in [g for g, _t, _f in doctor._CHECKS])


# ---- 5. the Settings page -------------------------------------------------------------------
def test_settings_page():
    js_path = _REPO / "web" / "public" / "settings.js"
    js = js_path.read_text(encoding="utf-8")
    start, end = js.index("// ---- Minecraft mods (Settings > Integrations)"), js.index("function linkRow(spec)")
    block = js[start:end]
    check("the Integrations section has a 'Minecraft mods' group", 'group("Minecraft mods"' in block and "await minecraftGroup()" in js)
    check("the key box is a password field that never autofills", 'type: "password"' in block and 'autocomplete: "off"' in block)
    check("the key is read WITHOUT reveal (the page only learns set / not set)", "readDoc(CF_FILE)" in block and "reveal: true" not in block and "reveal:true" not in block)
    check("it saves through the existing settings write path, one value", '"/api/settings/file/set"' in block and 'path: ["api_key"]' in block and "base_version" in block)
    check("after a save the page keeps only a marker, never the typed key", 'setAt(st.doc, ["api_key"], value === "" ? "" : "set")' in block and "input.value = \"\"" in block)
    check("the typed key is never written to the DOM, storage or the console",
          not re.search(r"(textContent|innerHTML|localStorage|sessionStorage|console\.\w+)[^;\n]*(typed|value\b)", block.replace("input.value = \"\"", "")), "")
    check("Contact and Default folder reuse the Tool settings rows (one storage)", "toolSettingRow(file, s)" in block and 'MC_TOOL_KEYS = ["contact", "default_dest"]' in block and 'MC_TOOL_STEM = "minecraft_lookup"' in block)
    check("with no Minecraft tool loaded there is a link to Tool settings instead", 'select("toolsettings")' in block)
    check("a clear button asks first", "confirmDialog(" in block and "Clear the CurseForge key?" in block)
    check("the help line says personal, non-transferable, console.curseforge.com, and that Modrinth/OptiFine still work",
          "console.curseforge.com" in block and "cannot be shared" in block and "Modrinth and OptiFine still work" in block)
    check("no markup assignment in the new block (text only)", "innerHTML" not in block and "outerHTML" not in block and "insertAdjacentHTML" not in block)
    check("the Advanced file picker's Reveal button also appears for the key file", re.search(r'hasSecretsLikely[\s\S]{0,200}"curseforge\.json"', js) is not None)
    m = re.search(r"const FILE_NAME_RE = (/.+/);", js)
    check("the deep-link pattern lets modhosts/ files through", m is not None and "modhosts" in m.group(1) and "web-data" in m.group(1), m and m.group(1))
    check("the section's search terms find it", "minecraft" in js[js.index('id: "integrations"'):js.index('id: "memory"')] and "curseforge" in js[js.index('id: "integrations"'):js.index('id: "memory"')])
    node = shutil.which("node")
    if node:
        p = subprocess.run([node, "--check", str(js_path)], capture_output=True, text=True, timeout=60)
        check("node --check web/public/settings.js", p.returncode == 0, p.stderr[-300:])
    else:
        print("skip    node is not installed: node --check not run")


# ---- 7. the docstring names the real get_key() callers (M3) ---------------------------------
def test_docstring_names_the_real_callers():
    doc = config.__doc__ or ""
    named = ("modhosts/net.py", "modhosts/cache.py", "modhosts/sources.py", "modhosts/curseforge.py")
    check("the docstring names net.py, cache.py, sources.py and curseforge.py", all(n in doc for n in named), doc[:400])
    check("it no longer says only net.py and curseforge.py call get_key()", "its only callers are" not in doc)
    pkg = Path(config.__file__).resolve().parent
    callers = sorted(f.name for f in pkg.glob("*.py")
                     if f.name != "config.py" and re.search(r"\bconfig\.get_key\(", f.read_text(encoding="utf-8", errors="replace")))
    check("the files that really call config.get_key() are exactly those four", callers == ["cache.py", "curseforge.py", "net.py", "sources.py"], callers)


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
