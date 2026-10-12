"""Master plan L.43 follow-up -- Settings > Advanced > Files covers web/data/*.json.

The web console keeps four small files in web/data (gitignored): favorites.json (starred
commands), favorite-daemons.json (starred daemons), ask-console-filter.json (which lines the
Ask trace hides) and reboot.json (a restart marker). They are addressed in Advanced as
web-data/<name>.json. Pinned here:

  1. names and paths: web-data/<leaf>.json resolves under the web data folder and nowhere else
     (traversal, nesting, a wrong extension and a look-alike prefix are refused); the folder
     comes from WEB_DATA_DIR, then an ABSOLUTE JARVIS_WEB_DATA, then web/data beside the
     checkout; when none exists the files are not offered and the refusal says why.
  2. the catalog: the three preference files are listed (before they exist) and editable;
     reboot.json and any file not on the allow-list are listed read-only; the folder is shown.
  3. writes go through the same gate as every other file: backup first, stale refusal, change
     log with paths only, unattended refusal, no write to a read-only file; a missing file (and
     its folder) can be created; a wrong-shaped file can be repaired.
  4. the web server's own rules are repeated, not loosened: starred commands and daemons are
     lists of text with no duplicates; a daemon id matches the server's DAEMON_ID and there
     are at most 500; the console filter has only hiddenKinds, and only real kinds.
  5. the copies of those rules in Python equal the ones in web/server.js (parsed from the
     source), the routes and the page accept the web-data/ prefix, and the web server tells
     the CLI where its folder is.
  6. end to end through `jarvis settings-admin`: JARVIS_WEB_DATA is honoured and a relative
     one is not.

Run: python3 tests/test_l43_web_data.py   (no key, no network)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l43wd-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID",
             "JARVIS_CONTEXT", "JARVIS_WEB_DATA"):
    os.environ.pop(_var, None)

REPO = Path(__file__).resolve().parent.parent
_JARVIS_CLI = REPO / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

from jarvis import settings_admin as sa  # noqa: E402
from jarvis import tunables  # noqa: E402

PASS, FAIL = [], []
JDIR = Path(_HOME) / ".jarvis"
WEB = Path(tempfile.mkdtemp(prefix="jarvis-l43wd-web-")) / "data"
REAL_FILE = sa.__file__


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


def _fresh():
    """Empty ~/.jarvis and an empty web data folder (the folder itself does not exist yet)."""
    shutil.rmtree(JDIR, ignore_errors=True)
    JDIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(WEB, ignore_errors=True)
    sa.WEB_DATA_DIR = WEB
    sa.__file__ = REAL_FILE


def _put_web(leaf, doc):
    WEB.mkdir(parents=True, exist_ok=True)
    text = doc if isinstance(doc, str) else json.dumps(doc, indent=2)
    (WEB / leaf).write_text(text, encoding="utf-8")


def _write(name, doc, version=None):
    cur = sa.read(name)
    return sa.write(name, json.dumps(doc), cur["version"] if version is None else version)


def _backups(name):
    return sa.list_backups(name)["backups"]


# ---- 1. names and paths ----------------------------------------------------------------
def test_names_and_paths():
    _fresh()
    p = sa._path_for("web-data/favorites.json")
    check("web-data/<leaf>.json resolves under the web data folder", p == WEB / "favorites.json", p)
    check("a top-level name still resolves under ~/.jarvis", sa._path_for("ai_config.json") == JDIR / "ai_config.json")
    check("channels/ still resolves under ~/.jarvis/channels", sa._path_for("channels/people.json") == JDIR / "channels" / "people.json")
    for bad in ("web-data/../x.json", "web-data/a/b.json", "web-data//x.json", "web-data/x.txt", "web-data/",
                "web-data/.json", "webdata/x.json", "web/data/x.json", "Web-Data/x.json", "web-data\\x.json",
                "web-data/..\\x.json", "channels/web-data/x.json"):
        check(f"name {bad!r} is refused", sa._path_for(bad) is None and sa.read(bad)["ok"] is False)
    # A refused name never touches the disk outside the folder.
    check("reading a traversal name creates nothing", not (WEB.parent / "x.json").exists())

    sa.WEB_DATA_DIR = None
    other = Path(tempfile.mkdtemp(prefix="jarvis-l43wd-env-")) / "data"
    os.environ["JARVIS_WEB_DATA"] = str(other)
    check("JARVIS_WEB_DATA (absolute) is used when WEB_DATA_DIR is unset", sa._web_data_dir() == other, sa._web_data_dir())
    check("WEB_DATA_DIR wins over the environment", (setattr(sa, "WEB_DATA_DIR", WEB) or sa._web_data_dir()) == WEB)
    sa.WEB_DATA_DIR = None
    os.environ["JARVIS_WEB_DATA"] = "data"
    resolved = sa._web_data_dir()
    check("a relative JARVIS_WEB_DATA is ignored, not resolved against the working directory",
          resolved != Path("data") and (resolved is None or resolved.is_absolute()), resolved)
    os.environ.pop("JARVIS_WEB_DATA", None)
    check("run from a checkout the folder is web/data beside jarvis-cli",
          sa._web_data_dir() == REPO / "web" / "data", sa._web_data_dir())

    # An install with no web server beside it, and no variable: the folder is unknown.
    lonely = Path(tempfile.mkdtemp(prefix="jarvis-l43wd-lonely-"))
    (lonely / "jarvis-cli" / "jarvis").mkdir(parents=True)
    sa.__file__ = str(lonely / "jarvis-cli" / "jarvis" / "settings_admin.py")
    check("with no variable and no web/server.js the folder is unknown (None)", sa._web_data_dir() is None)
    check("so a web-data name has no path", sa._path_for("web-data/favorites.json") is None)
    res = sa.read("web-data/favorites.json")
    check("and the refusal says how to fix it", res["ok"] is False and "JARVIS_WEB_DATA" in res["error"], res)
    res = sa.write("web-data/favorites.json", "[]", "absent")
    check("a write says the same, with a bad_name code", res["ok"] is False and res["code"] == "bad_name" and "JARVIS_WEB_DATA" in res["error"], res)
    cat = sa.catalog()
    check("the catalog offers no web-data files and no folder",
          cat["web_data_dir"] is None and not [f for f in cat["files"] if f["name"].startswith("web-data/")], cat["web_data_dir"])
    check("an ordinary name is still refused the ordinary way", sa.read("x.txt")["error"] == "That is not a file this screen can open.")
    sa.__file__ = REAL_FILE


# ---- 2. the catalog --------------------------------------------------------------------
def test_catalog():
    _fresh()
    cat = sa.catalog()
    by = {f["name"]: f for f in cat["files"]}
    check("the catalog says where the web data folder is", cat["web_data_dir"] == str(WEB), cat["web_data_dir"])
    for leaf in ("favorites.json", "favorite-daemons.json", "ask-console-filter.json"):
        row = by.get("web-data/" + leaf)
        check(f"{leaf} is listed before it exists, and is editable",
              row is not None and row["exists"] is False and row["editable"] is True and row["guarded"] is False, row)
    check("reboot.json is not offered until it exists (nothing to edit)", "web-data/reboot.json" not in by)

    _put_web("reboot.json", {"mode": "normal", "timestamp": "Sun 08/30/2026 14:30:55.21"})
    _put_web("mystery.json", {"anything": 1})
    _put_web("notes.txt", "not json at all")
    _put_web("favorites.json.bak", "[]")
    by = {f["name"]: f for f in sa.catalog()["files"]}
    check("reboot.json is listed read-only as state", by["web-data/reboot.json"]["editable"] is False
          and by["web-data/reboot.json"]["group"] == sa.GROUP_STATE and by["web-data/reboot.json"]["exists"] is True)
    check("a file that is not on the allow-list is listed read-only",
          by["web-data/mystery.json"]["editable"] is False and "not one of" in by["web-data/mystery.json"]["note"].lower())
    check("files that are not .json (text, .bak) are not listed",
          not [n for n in by if n.endswith(".txt") or n.endswith(".bak")], sorted(by))
    check("a web-data file is never mistaken for a ~/.jarvis file of the same name",
          "favorites.json" not in by and "reboot.json" not in by)
    check("every web-data file has a title or a note", all((by[n]["title"] or by[n]["note"]) for n in by if n.startswith("web-data/")))
    check("web-data files are guarded by nothing (no secrets, no permissions)", not any(by[n]["guarded"] for n in by if n.startswith("web-data/")))
    meta = sa.read("web-data/favorites.json")["meta"]
    check("applies says reload the page", "reload" in meta["applies"].lower(), meta["applies"])


# ---- 3. writes -------------------------------------------------------------------------
def test_write_round_trip_and_gate():
    _fresh()
    check("the folder does not exist yet", not WEB.exists())
    r = sa.read("web-data/favorites.json")
    check("a file that does not exist reads as an empty document", r["ok"] and r["exists"] is False and r["version"] == "absent", r)
    res = sa.write("web-data/favorites.json", json.dumps(["spotify", "tts_bot"]), r["version"])
    check("a missing file (and its folder) can be created from here", res["ok"] and res["changed"] and (WEB / "favorites.json").is_file(), res)
    check("what the web server reads back is the list", json.loads((WEB / "favorites.json").read_text(encoding="utf-8")) == ["spotify", "tts_bot"])
    check("creating a file makes no backup (there was nothing before)", _backups("web-data/favorites.json") == [])

    r2 = sa.read("web-data/favorites.json")
    check("the read shows the list as text", json.loads(r2["text"]) == ["spotify", "tts_bot"] and r2["masked"] == 0)
    res = sa.write("web-data/favorites.json", json.dumps(["spotify", "tts_bot", "restart_jarvis_server"]), r2["version"])
    check("an edit saves", res["ok"] and res["changed"], res)
    bs = _backups("web-data/favorites.json")
    check("a backup of the previous contents was made first", len(bs) == 1, bs)
    backup_file = JDIR / sa.BACKUP_DIR_NAME / "web-data__favorites" / (bs[0]["id"] + ".json")
    check("the backup lives under ~/.jarvis/settings_backups, not in web/data",
          backup_file.is_file() and json.loads(backup_file.read_text(encoding="utf-8")) == ["spotify", "tts_bot"]
          and not [p for p in WEB.glob("*") if "backup" in p.name.lower()], backup_file)

    stale = sa.write("web-data/favorites.json", json.dumps(["x"]), r2["version"])
    check("a save made against an old version is refused as stale", stale["ok"] is False and stale["code"] == "stale", stale)
    check("and changed nothing", json.loads((WEB / "favorites.json").read_text(encoding="utf-8")) == ["spotify", "tts_bot", "restart_jarvis_server"])

    cur = sa.read("web-data/favorites.json")
    again = sa.write("web-data/favorites.json", cur["text"], cur["version"])
    check("saving the same list is a no-op with no new backup", again["ok"] and again["changed"] is False and len(_backups("web-data/favorites.json")) == 1)

    restored = sa.restore("web-data/favorites.json", None, sa.read("web-data/favorites.json")["version"])
    check("Undo puts the previous list back", restored["ok"] and json.loads((WEB / "favorites.json").read_text(encoding="utf-8")) == ["spotify", "tts_bot"], restored)

    log = (JDIR / sa.BACKUP_DIR_NAME / sa.CHANGE_LOG_NAME).read_text(encoding="utf-8")
    check("the change log names the file and holds paths, never the starred names",
          "web-data/favorites.json" in log and "restart_jarvis_server" not in log and "spotify" not in log, log[:300])

    one = sa.set_value("web-data/favorites.json", [0], "spotify_web", sa.read("web-data/favorites.json")["version"])
    check("the Tree view's one-value edit works on a list at the top", one["ok"] and one["changed"] and
          json.loads((WEB / "favorites.json").read_text(encoding="utf-8")) == ["spotify_web", "tts_bot"], one)
    pre = sa.preview("web-data/favorites.json", json.dumps(["a", "b"]), sa.read("web-data/favorites.json")["version"])
    check("preview lists what would change", pre["ok"] and pre["valid"] and len(pre["changes"]) == 2, pre)


def test_gate_refusals():
    _fresh()
    _put_web("favorites.json", ["a"])
    _put_web("reboot.json", {"mode": "normal"})
    _put_web("mystery.json", {"x": 1})
    for leaf, doc in (("reboot.json", {"mode": "x"}), ("mystery.json", {"x": 2})):
        name = "web-data/" + leaf
        res = sa.write(name, json.dumps(doc), sa.read(name)["version"])
        check(f"{leaf} cannot be written from here", res["ok"] is False and res["code"] == "read_only", res)
        res = sa.set_value(name, ["x"], 3, None)
        check(f"{leaf} cannot be changed value by value either", res["ok"] is False and res["code"] == "read_only", res)
        res = sa.restore(name, None, None)
        check(f"{leaf} cannot be restored over", res["ok"] is False and res["code"] == "read_only", res)
    check("read-only files were left alone", json.loads((WEB / "reboot.json").read_text(encoding="utf-8")) == {"mode": "normal"})

    for var, value in (("JARVIS_CHANNEL_SENDER", "intruder"), ("JARVIS_CHANNEL", "discord"), ("JARVIS_SCHEDULED", "1"), ("JARVIS_TASK_ID", "t1")):
        os.environ[var] = value
        res = sa.write("web-data/favorites.json", json.dumps(["z"]), sa.read("web-data/favorites.json")["version"])
        os.environ.pop(var)
        check(f"a write is refused while {var} is set", res["ok"] is False and res["code"] == "unattended", res)
    check("and nothing changed", json.loads((WEB / "favorites.json").read_text(encoding="utf-8")) == ["a"])

    res = sa.write("web-data/favorites.json", "[oops", sa.read("web-data/favorites.json")["version"])
    check("text that is not JSON is refused with the line and column", res["ok"] is False and "line" in res["error"], res)


def test_repairing_a_wrong_shaped_file():
    _fresh()
    _put_web("favorites.json", {"oops": True})
    r = sa.read("web-data/favorites.json")
    check("a wrong-shaped file is shown as it is", r["ok"] and json.loads(r["text"]) == {"oops": True})
    res = sa.write("web-data/favorites.json", json.dumps({"still": "wrong"}), r["version"])
    check("another object is refused: the top level must be a list", res["ok"] is False and "must be a list" in " ".join(res["problems"]), res)
    res = sa.write("web-data/favorites.json", json.dumps(["fixed"]), r["version"])
    check("a list repairs it", res["ok"] and json.loads((WEB / "favorites.json").read_text(encoding="utf-8")) == ["fixed"], res)
    _fresh()
    res = sa.write("web-data/ask-console-filter.json", json.dumps(["wrong"]), "absent")
    check("a new console filter cannot be created as a list", res["ok"] is False and "must be an object" in " ".join(res["problems"]), res)
    res = sa.write("web-data/favorites.json", json.dumps("text"), "absent")
    check("a bare string is refused", res["ok"] is False)


# ---- 4. the server's rules, repeated ---------------------------------------------------
def test_starred_commands_rules():
    _fresh()
    v = "absent"
    cases = [
        ("not a list of text (a number in it)", ["a", 3]),
        ("an empty name", ["a", ""]),
        ("a blank name", ["a", "   "]),
        ("null in it", [None]),
        ("a nested list", [["a"]]),
        ("a duplicate", ["a", "b", "a"]),
    ]
    for label, doc in cases:
        res = sa.write("web-data/favorites.json", json.dumps(doc), v)
        check(f"starred commands: {label} is refused", res["ok"] is False and res["code"] == "invalid" and res["problems"], res)
    check("nothing was written by the refused attempts", not (WEB / "favorites.json").exists())
    res = sa.write("web-data/favorites.json", json.dumps(["a", "b", "my cmd", "naïve"]), v)
    check("an ordinary list is accepted (a name is not otherwise judged: the command may be gone)", res["ok"], res)
    res = sa.write("web-data/favorites.json", json.dumps(["a", 5, "a"]), sa.read("web-data/favorites.json")["version"])
    check("every problem is reported at once", res["ok"] is False and len(res["problems"]) >= 2, res)
    res = sa.write("web-data/favorites.json", "[]", sa.read("web-data/favorites.json")["version"])
    check("an empty list is fine (nothing starred)", res["ok"] and json.loads((WEB / "favorites.json").read_text(encoding="utf-8")) == [])


def test_starred_daemons_rules():
    _fresh()
    v = "absent"
    for label, doc in (
        ("an upper-case id", ["Spotify"]),
        ("a space", ["my daemon"]),
        ("a leading dash", ["-x"]),
        ("a leading underscore", ["_x"]),
        ("41 characters", ["a" * 41]),
        ("a dot", ["a.b"]),
        ("a slash", ["a/b"]),
        ("a number", [7]),
        ("a duplicate", ["insta", "insta"]),
    ):
        res = sa.write("web-data/favorite-daemons.json", json.dumps(doc), v)
        check(f"starred daemons: {label} is refused", res["ok"] is False and res["code"] == "invalid", res)
    ids = ["insta", "discord", "sched-daemon", "tts_bot", "a", "0x", "a" * 40]
    res = sa.write("web-data/favorite-daemons.json", json.dumps(ids), v)
    check("valid ids (lower case, digits, - and _, up to 40) are accepted", res["ok"], res)
    big = ["d%d" % i for i in range(sa.MAX_FAVORITE_DAEMONS)]
    res = sa.write("web-data/favorite-daemons.json", json.dumps(big), sa.read("web-data/favorite-daemons.json")["version"])
    check("exactly 500 is accepted", res["ok"], res)
    big.append("one-too-many")
    res = sa.write("web-data/favorite-daemons.json", json.dumps(big), sa.read("web-data/favorite-daemons.json")["version"])
    check("501 is refused with the limit named", res["ok"] is False and "500" in " ".join(res["problems"]), res)


def test_console_filter_rules():
    _fresh()
    v = "absent"
    for label, doc in (
        ("an unknown kind", {"hiddenKinds": ["tool-call", "bogus"]}),
        ("a kind in the wrong case", {"hiddenKinds": ["Tokens"]}),
        ("a duplicate kind", {"hiddenKinds": ["tokens", "tokens"]}),
        ("hiddenKinds that is not a list", {"hiddenKinds": "tokens"}),
        ("hiddenKinds that is null", {"hiddenKinds": None}),
        ("a misspelt key (it would silently do nothing)", {"hiddenKind": ["tokens"]}),
        ("an extra key", {"hiddenKinds": [], "theme": "dark"}),
        ("a non-text kind", {"hiddenKinds": [1]}),
    ):
        res = sa.write("web-data/ask-console-filter.json", json.dumps(doc), v)
        check(f"console filter: {label} is refused", res["ok"] is False and res["code"] == "invalid", res)
    for kind in sa.ASK_CONSOLE_KINDS:
        res = sa.write("web-data/ask-console-filter.json", json.dumps({"hiddenKinds": [kind]}), sa.read("web-data/ask-console-filter.json")["version"])
        check(f"console filter: {kind!r} is accepted", res["ok"], res)
    res = sa.write("web-data/ask-console-filter.json", json.dumps({"hiddenKinds": list(sa.ASK_CONSOLE_KINDS)}), sa.read("web-data/ask-console-filter.json")["version"])
    check("every kind hidden at once is accepted", res["ok"], res)
    res = sa.write("web-data/ask-console-filter.json", "{}", sa.read("web-data/ask-console-filter.json")["version"])
    check("an empty object is accepted (nothing hidden)", res["ok"], res)
    cur = sa.read("web-data/ask-console-filter.json")
    res = sa.set_value("web-data/ask-console-filter.json", ["hiddenKinds"], ["status"], cur["version"])
    check("the Tree view can set the list", res["ok"] and json.loads((WEB / "ask-console-filter.json").read_text(encoding="utf-8")) == {"hiddenKinds": ["status"]}, res)
    cur = sa.read("web-data/ask-console-filter.json")
    res = sa.set_value("web-data/ask-console-filter.json", ["hiddenKinds", 0], "nonsense", cur["version"])
    check("and the validator still runs on a one-value edit", res["ok"] is False and res["code"] == "invalid", res)
    res = sa.reset_value("web-data/ask-console-filter.json", ["hiddenKinds"], cur["version"])
    check("there is no built-in default to reset to, and it says so", res["ok"] is False and res["code"] == "no_defaults", res)
    rep = sa.defaults_report("web-data/ask-console-filter.json")
    check("the defaults report says none are recorded", rep["ok"] and rep["has_defaults"] is False, rep)


def test_existing_files_are_unchanged():
    """The new validators and the new top-level rule must not touch the old files."""
    _fresh()
    JDIR.mkdir(parents=True, exist_ok=True)
    (JDIR / "notify_config.json").write_text(json.dumps({"levels": {"notify": 2}}), encoding="utf-8")
    r = sa.read("notify_config.json")
    res = sa.write("notify_config.json", json.dumps(["now", "a", "list"]), r["version"])
    check("a ~/.jarvis file still must keep its top-level type", res["ok"] is False and "must stay an object" in " ".join(res["problems"]), res)
    res = sa.write("notify_config.json", json.dumps({"levels": {"notify": 3}}), r["version"])
    check("and a normal edit still saves", res["ok"] and res["changed"], res)
    res = sa.write("notify_config.json", json.dumps({"levels": {"notify": "high"}}), sa.read("notify_config.json")["version"])
    check("a type flip is still refused", res["ok"] is False and "keep its type" in " ".join(res["problems"]), res)
    check("channels/ files are still read-only", sa.meta_for("channels/people.json")["editable"] is False)
    check("a web-data file is not in the ~/.jarvis list of known files", "web-data/favorites.json" not in sa._FILES)


# ---- 5. the copies agree with the web server -------------------------------------------
def test_rules_match_the_web_server():
    server = (REPO / "web" / "server.js").read_text(encoding="utf-8")
    m = re.search(r"const DAEMON_ID = /(.+?)/;", server)
    check("web/server.js has the DAEMON_ID rule", m is not None)
    check("the daemon-id rule here is the server's, character for character", m is not None and sa.DAEMON_ID_RE.pattern == m.group(1), (m and m.group(1), sa.DAEMON_ID_RE.pattern))
    m = re.search(r"const MAX_FAVORITE_DAEMONS = (\d+);", server)
    check("the starred-daemons limit is the server's", m is not None and int(m.group(1)) == sa.MAX_FAVORITE_DAEMONS, m and m.group(1))
    m = re.search(r"const ASK_CONSOLE_KINDS = \[(.*?)\];", server, re.S)
    kinds = tuple(re.findall(r'"([^"]+)"', m.group(1))) if m else ()
    check("the Ask console kinds are the server's, in the same order", kinds == sa.ASK_CONSOLE_KINDS, (kinds, sa.ASK_CONSOLE_KINDS))
    for leaf, const in (("favorites.json", "FAVORITES_PATH"), ("favorite-daemons.json", "FAVORITE_DAEMONS_PATH"),
                        ("ask-console-filter.json", "ASK_CONSOLE_FILTER_PATH")):
        check(f"the server's {const} is web/data/{leaf}", re.search(const + r' = path\.join\(__dirname, "data", "' + re.escape(leaf) + r'"\)', server) is not None)
    # Every file the web server writes under web/data must be on the allow-list, or be a deliberate read-only one.
    written = set(re.findall(r'path\.join\(__dirname, "data", "([^"]+\.json)"\)', server))
    check("every web/data file the server names is known to Settings", written <= set(sa._WEB_DATA_FILES), sorted(written - set(sa._WEB_DATA_FILES)))
    editable = {n for n, v in sa._WEB_DATA_FILES.items() if v[0] in sa.EDITABLE_GROUPS}
    check("exactly the three preference files are editable", editable == {"favorites.json", "favorite-daemons.json", "ask-console-filter.json"}, editable)
    check("every editable one has a root type and a validator",
          all(("web-data/" + n) in sa._ROOT_TYPE and (("web-data/" + n) in sa._VALIDATORS or ("web-data/" + n) in sa._LIST_VALIDATORS) for n in editable))

    app = (REPO / "web" / "public" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"ASK_CONSOLE_CATEGORIES\s*=\s*\[(.*?)\];", app, re.S)
    page_kinds = tuple(re.findall(r'id:\s*"([^"]+)"', m.group(1))) if m else ()
    check("the page's console categories are the same kinds, in the same order",
          page_kinds == sa.ASK_CONSOLE_KINDS, (page_kinds, sa.ASK_CONSOLE_KINDS))


def test_wiring_accepts_the_prefix():
    server = (REPO / "web" / "server.js").read_text(encoding="utf-8")
    m = re.search(r"const SETTINGS_NAME_RE = (/.+/);", server)
    check("the server route has a name pattern", m is not None)
    check("the server route pattern lets web-data/ through", m is not None and "web-data" in m.group(1), m and m.group(1))
    check("and still allows only one folder level (no nested path in the pattern)", m is not None and m.group(1).count("\\/") == 1, m and m.group(1))
    check("every settings call passes the CLI the web data folder",
          'JARVIS_WEB_DATA: SETTINGS_WEB_DATA_DIR' in server and 'const SETTINGS_WEB_DATA_DIR = path.join(__dirname, "data");' in server)
    js = (REPO / "web" / "public" / "settings.js").read_text(encoding="utf-8")
    m = re.search(r"const FILE_NAME_RE = (/.+/);", js)
    check("the page's file-name pattern lets web-data/ through", m is not None and "web-data" in m.group(1), m and m.group(1))
    check("JARVIS_WEB_DATA is listed as plumbing the web server sets", "JARVIS_WEB_DATA" in [n for n, _h in tunables.PLUMBING_ENV])


# ---- 6. end to end ---------------------------------------------------------------------
def _env(**extra):
    env = dict(os.environ)
    env.update({"HOME": _HOME, "USERPROFILE": _HOME})
    for k in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT", "JARVIS_WEB_DATA"):
        env.pop(k, None)
    env.update(extra)
    return env


def _cli(sub, payload=None, **env):
    proc = subprocess.run([sys.executable, "-m", "jarvis", "settings-admin", sub], input=None if payload is None else json.dumps(payload),
                          capture_output=True, text=True, env=_env(**env), timeout=120, cwd=str(_JARVIS_CLI))
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"_raw": proc.stdout[-300:], "_err": proc.stderr[-300:]}


def test_cli_honours_the_variable():
    _fresh()
    folder = Path(tempfile.mkdtemp(prefix="jarvis-l43wd-cli-")) / "data"
    folder.mkdir(parents=True)
    (folder / "favorites.json").write_text(json.dumps(["from-cli"]), encoding="utf-8")
    code, out = _cli("catalog", JARVIS_WEB_DATA=str(folder))
    names = [f["name"] for f in out.get("files", [])]
    check("the CLI catalog lists web-data files when told where they are",
          code == 0 and out.get("ok") and "web-data/favorites.json" in names and out.get("web_data_dir") == str(folder), out)
    code, out = _cli("read", {"name": "web-data/favorites.json"}, JARVIS_WEB_DATA=str(folder))
    check("the CLI reads the file in that folder", out.get("ok") and json.loads(out["text"]) == ["from-cli"], out)
    code, out2 = _cli("write", {"name": "web-data/favorites.json", "text": json.dumps(["from-cli", "second"]),
                                "base_version": out["version"]}, JARVIS_WEB_DATA=str(folder))
    check("the CLI writes it", out2.get("ok") and out2.get("changed") and json.loads((folder / "favorites.json").read_text(encoding="utf-8")) == ["from-cli", "second"], out2)
    code, out3 = _cli("write", {"name": "web-data/favorites.json", "text": "[]", "base_version": "x"},
                      JARVIS_WEB_DATA=str(folder), JARVIS_CHANNEL_SENDER="intruder")
    check("a chat-triggered process is refused through the CLI too", out3.get("ok") is False and out3.get("code") == "unattended", out3)
    code, out4 = _cli("read", {"name": "web-data/favorites.json"}, JARVIS_WEB_DATA="relative/data")
    check("a relative JARVIS_WEB_DATA is not used (the checkout's own folder answers instead)",
          not (out4.get("ok") and out4.get("text") and "from-cli" in out4["text"]), out4)


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
