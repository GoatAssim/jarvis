"""Download Hub, N22 parts b and c (brief 30): see the Steam library, install a game, start an update.

Covers hub/steam.py's new read-only facts (game_facts, update_pending, find_game, install_url, launch_install,
observe_start) and actions/steam_tools.py (steam_library, steam_install_game, steam_update_game), on a FAKE Steam
folder.  Nothing here starts a real Steam, opens a real steam:// link, or talks to the network: steam.OPEN_URL is
a recording stand-in.

  * update_pending is "yes" only while Steam is fetching an update now, "unknown" otherwise (never "no");
    a game's name from a manifest is cleaned and capped; the raw flags and build ids are passed through.
  * the only URL ever launched is steam://install/<digits>; anything else is refused before anything runs.
  * a chat guest (and an unreadable sender) is refused before anything is read or launched, for all three tools;
    the owner is let through.  Install and update are in TOOL_CONFIRM_REQUIRED, the read-only library is not.
  * install: asks which game when unclear; an installed game is not started again; a running transfer is not
    started again; Steam closed is a plain sentence and nothing is launched; the outcome is what the manifests
    showed within the look (started / changed / nothing).
  * update: one game only ("all" refused), only an installed game; it launches the same install URL.
  * Steam stays read-only: every file of the fake Steam folder is byte-identical after all of it, the provider
    offers no pause / cancel, and the module has no code that writes under a Steam folder.

Run: python tests/test_steam_library_tools.py
"""
import ast
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-steam-tools-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.actions import steam_tools as st  # noqa: E402
from jarvis.hub import steam  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


# --------------------------------------------------------------------------- a fake Steam
STEAM = Path(HOME) / "Steam"
LIB2 = Path(HOME) / "SteamLib2"


def manifest(appid, name, flags, installdir=None, size=0, updated=0, to_dl=0, got=0, extra=""):
    return ('"AppState"\n{\n\t"appid"\t\t"%s"\n\t"name"\t\t"%s"\n\t"StateFlags"\t\t"%d"\n\t"installdir"\t\t"%s"\n'
            '\t"LastUpdated"\t\t"%d"\n\t"SizeOnDisk"\t\t"%d"\n\t"BytesToDownload"\t\t"%d"\n\t"BytesDownloaded"\t\t"%d"\n%s}\n'
            % (appid, name, flags, installdir or name.replace(" ", ""), updated, size, to_dl, got, extra))


def put(library, appid, text):
    apps = Path(library) / "steamapps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / ("appmanifest_%s.acf" % appid)).write_text(text, encoding="utf-8")


(STEAM / "steamapps").mkdir(parents=True)
(LIB2 / "steamapps").mkdir(parents=True)
(STEAM / "steamapps" / "libraryfolders.vdf").write_text(
    '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n'
    % (str(STEAM).replace("\\", "\\\\"), str(LIB2).replace("\\", "\\\\")), encoding="utf-8")
put(STEAM, 105600, manifest(105600, "Terraria", 4, size=60_384_000, updated=1_700_000_000, to_dl=60384, got=60384,
                            extra='\t"buildid"\t\t"1001"\n\t"TargetBuildID"\t\t"1001"\n'))
put(STEAM, 1145360, manifest(1145360, "Hades", 4, size=9_000_000_000, updated=1_690_000_000))
put(STEAM, 1145361, manifest(1145361, "Hades II", 4, size=5_000_000_000, updated=1_690_000_000))
put(LIB2, 220, manifest(220, "Half-Life 2\x1b[31m\n" + "X" * 200, 1030, size=4_000_000_000, updated=1_600_000_000,
                        to_dl=500_000_000, got=100_000_000))                       # an UPDATE running now
put(LIB2, 400, manifest(400, "Portal", 1026, size=0, updated=0, to_dl=900_000_000, got=10_000_000))   # a FIRST install running
put(LIB2, 620, manifest(620, "Portal 2", 6, size=7_000_000_000, updated=1_600_000_000))   # UpdateRequired bit on an idle game
os.environ["JARVIS_STEAM_ROOT"] = str(STEAM)


def tree_hash():
    h = hashlib.sha256()
    for base in (STEAM, LIB2):
        for p in sorted(base.rglob("*")):
            if p.is_file():
                h.update(str(p).encode())
                h.update(p.read_bytes())
    return h.hexdigest()


BEFORE = tree_hash()
launched = []
steam.OPEN_URL = lambda url: launched.append(url)
steam.OBSERVE_SECONDS, steam.OBSERVE_STEP = 0.0, 0.0
running = [True]
steam.steam_running = lambda: running[0]


def sender(**fields):
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps(fields) if not isinstance(fields.get("raw"), str) else fields["raw"]


def no_sender():
    os.environ.pop("JARVIS_CHANNEL_SENDER", None)


# --------------------------------------------------------------------------- the facts
root, libs = steam.find_libraries()
files = steam.read_manifests(libs)
games = steam.game_facts(files)
by_id = {g["appid"]: g for g in games}
check("six games found across two libraries", len(games) == 6 and len(libs) == 2)
check("sorted by name", [g["name"] for g in games] == sorted((g["name"] for g in games), key=str.casefold))
check("an idle fully installed game: unknown (never 'no'), with the reason", by_id["105600"]["update_pending"] == "unknown"
      and "not measured" in by_id["105600"]["update_reason"] and by_id["105600"]["fully_installed"] is True)
check("a game Steam is updating now: yes", by_id["220"]["update_pending"] == "yes" and by_id["220"]["transfer"]["kind"] == "update")
check("a first install running: unknown, and said to be a first install", by_id["400"]["update_pending"] == "unknown"
      and by_id["400"]["transfer"]["kind"] == "install" and "first install" in by_id["400"]["update_reason"])
check("the UpdateRequired bit on an idle game is NOT trusted", by_id["620"]["update_pending"] == "unknown" and by_id["620"]["transfer"] is None)
check("no game ever says 'no'", all(g["update_pending"] in ("yes", "unknown") for g in games))
check("raw numbers are passed through", by_id["105600"]["build_id"] == "1001" and by_id["105600"]["target_build_id"] == "1001"
      and by_id["220"]["state_flags"] == 1030 and by_id["220"]["bytes_to_download"] == 500_000_000)
check("a manifest's name is one clean line, capped", "\x1b" not in by_id["220"]["name"] and "\n" not in by_id["220"]["name"]
      and len(by_id["220"]["name"]) <= 80)
check("a manifest with no buildid says None, not a guess", by_id["1145360"]["build_id"] is None)

found, cands = steam.find_game(games, "105600")
check("find by app id", found and found["name"] == "Terraria")
check("find by exact name, any case", steam.find_game(games, "terraria")[0]["appid"] == "105600")
check("'Hades' is exact (not ambiguous with 'Hades II')", steam.find_game(games, "hades")[0]["appid"] == "1145360")
check("find by one unique piece of a name", steam.find_game(games, "terra")[0]["appid"] == "105600")
amb, cands = steam.find_game(games, "port")
check("two matches: none chosen, both offered", amb is None and len(cands) == 2)
check("no match: none, no candidates", steam.find_game(games, "zzz") == (None, []))
check("an empty query matches nothing", steam.find_game(games, "  ") == (None, []))

# --------------------------------------------------------------------------- the one URL
check("a plain number makes the install URL", steam.install_url("730") == "steam://install/730")
check("anything else makes none", all(steam.install_url(x) is None for x in
      ("", None, "12 3", "1;calc", "../1", "steam://install/1", "-5", "1.5", "１２", "123456789012", "0x1f")))
check("launch_install refuses a bad id without calling anything", steam.launch_install("1;calc") == (False, "not a Steam app id") and launched == [])
check("launch_install hands exactly the URL over", steam.launch_install("730") == (True, "") and launched == ["steam://install/730"])
del launched[:]
steam.OPEN_URL = lambda url: (_ for _ in ()).throw(OSError("no handler for steam://"))
ok, err = steam.launch_install("730")
check("a failing launch is a sentence, never an exception", ok is False and "no handler" in err)
steam.OPEN_URL = lambda url: launched.append(url)

# --------------------------------------------------------------------------- observe_start (a fake clock, no sleeping)
ticks = [0.0]


def fake_clock():
    return ticks[0]


def fake_sleep(sec):
    ticks[0] += sec


check("observe: nothing changes -> 'nothing'", steam.observe_start(libs, "105600", 3, 1, fake_sleep, fake_clock) == "nothing")
ticks[0] = 0.0
state = {"n": 0}


def sleep_then_write(sec):
    ticks[0] += sec
    state["n"] += 1
    if state["n"] == 1:
        put(STEAM, 777, manifest(777, "New Game", 1026, to_dl=1000, got=0))


check("observe: a manifest appears and Steam is fetching -> 'started'",
      steam.observe_start(libs, "777", 5, 1, sleep_then_write, fake_clock) == "started")
(STEAM / "steamapps" / "appmanifest_777.acf").unlink()
ticks[0], state["n"] = 0.0, 0


def sleep_then_flip(sec):
    ticks[0] += sec
    state["n"] += 1
    if state["n"] == 1:
        put(LIB2, 620, manifest(620, "Portal 2", 4, size=7_000_000_000, updated=1_600_000_000))


check("observe: it changed but nothing is fetching -> 'changed'",
      steam.observe_start(libs, "620", 3, 1, sleep_then_flip, fake_clock) == "changed")
put(LIB2, 620, manifest(620, "Portal 2", 6, size=7_000_000_000, updated=1_600_000_000))

# --------------------------------------------------------------------------- the guest gate (all three tools, fail closed)
CALLS = {"steam_library": {}, "steam_install_game": {"game": "105600"}, "steam_update_game": {"game": "105600"}}
for who, setter in (("a chat guest", lambda: sender(platform="discord", user_id="42", is_owner=False)),
                    ("an unreadable sender", lambda: sender(raw="{not json")),
                    ("a sender that is not an object", lambda: sender(raw="[1,2]")),
                    ("a sender nested too deep to read", lambda: sender(raw="[" * 5000 + "]" * 5000))):
    setter()
    del launched[:]
    outs = {n: st.TOOLS[n](a) for n, a in CALLS.items()}
    check("%s is refused by all three tools, with the owner's-only sentence" % who,
          all(o.get("ok") is False and "is the owner's" in o["error"] and n in o["error"] for n, o in outs.items()))
    check("... and nothing was launched" , launched == [])
sender(platform="discord", user_id="1", is_owner=True)
check("the owner on a chat platform is let through", st.TOOLS["steam_library"]({}).get("ok") is True)
no_sender()
check("no chat context (the PC, a scheduled job) is the owner's own", st.TOOLS["steam_library"]({}).get("ok") is True)
check("a sender with no id is not treated as a guest (same rule as the hub)", (sender(platform="", user_id=""), st._is_chat_guest())[1] is False)
no_sender()

# --------------------------------------------------------------------------- steam_library
lib = st.tool_steam_library({})
check("library: ok, counts, honest note", lib["ok"] and lib["count"] == 6 and lib["libraries"] == 2 and "cannot tell yet" in lib["note"])
check("library: a game's facts are there", [g for g in lib["games"] if g["appid"] == "105600"][0]["size_on_disk"] == 60_384_000)
check("library: search by part of a name", [g["appid"] for g in st.tool_steam_library({"search": "terra"})["games"]] == ["105600"])
check("library: search by app id", [g["appid"] for g in st.tool_steam_library({"search": "620"})["games"]] == ["620"])
check("library: updating only", [g["appid"] for g in st.tool_steam_library({"updating": True})["games"]] == ["220"])
lim = st.tool_steam_library({"limit": 2})
check("library: a limit cuts and says how many were left out", len(lim["games"]) == 2 and lim["truncated"] == 4)
check("library: a nonsense limit falls back to the default", st.tool_steam_library({"limit": "many"})["ok"] is True)
os.environ["JARVIS_STEAM_ROOT"] = str(Path(HOME) / "nowhere")
steam.default_roots = lambda: [str(Path(HOME) / "nowhere")]
none = st.tool_steam_library({})
check("library: no Steam folder is a plain error", none["ok"] is False and "wasn't found" in none["error"])
os.environ["JARVIS_STEAM_ROOT"] = str(STEAM)
steam.default_roots = lambda: [str(STEAM)]

# --------------------------------------------------------------------------- steam_install_game
del launched[:]
r = st.tool_steam_install_game({})
check("install: no game -> a question, nothing launched", r.get("needs_clarification") and launched == [])
r = st.tool_steam_install_game({"game": "port"})
check("install: two matches -> asks which, lists both, nothing launched",
      r.get("needs_clarification") and "Portal" in r["question"] and "Portal 2" in r["question"] and launched == [])
r = st.tool_steam_install_game({"game": "Some Unowned Game"})
check("install: an unknown name -> asks for the app id, nothing launched",
      r.get("needs_clarification") and "app id" in r["question"] and launched == [])
r = st.tool_steam_install_game({"game": "Terraria"})
check("install: an installed game is not started again", r["ok"] and r["already_installed"] and r["launched"] is False and launched == [])
r = st.tool_steam_install_game({"game": "220"})
check("install: a game Steam is fetching now is not started again", r["ok"] and r["launched"] is False and r["transfer"]["kind"] == "update" and launched == [])
running[0] = False
r = st.tool_steam_install_game({"game": "730"})
check("install: Steam closed -> a plain sentence, nothing launched",
      r["ok"] is False and "Steam isn't running" in r["error"] and "nothing was started" in r["error"] and launched == [])
running[0] = True
r = st.tool_steam_install_game({"game": "730"})
check("install: a number is launched as steam://install/730", launched == ["steam://install/730"] and r["ok"] and r["launched"])
check("install: no change seen -> says so, and does not claim success",
      r["observed"] == "nothing" and "saw no change" in r["message"] and "may not be on this account" in r["message"])
check("install: the message says where to confirm (Steam's own window)", "Steam normally shows its own window" in r["message"])
del launched[:]
r = st.tool_steam_install_game({"game": "1;calc"})
check("install: a hostile 'app id' launches nothing", launched == [] and (r.get("needs_clarification") or r.get("ok") is False))
r = st.tool_steam_install_game({"game": "../../etc"})
check("install: a path launches nothing", launched == [])

started = {"done": False}


def steam_writes(url):
    launched.append(url)
    put(LIB2, 888, manifest(888, "Fresh Game", 1026, to_dl=5000, got=0))


steam.OPEN_URL = steam_writes
steam.OBSERVE_SECONDS, steam.OBSERVE_STEP = 0.2, 0.0
r = st.tool_steam_install_game({"game": "888"})
check("install: Steam wrote a manifest and is fetching -> started, the row is in Downloads",
      r["observed"] == "started" and "Steam row in Downloads" in r["message"] and "read-only" in r["message"])
(LIB2 / "steamapps" / "appmanifest_888.acf").unlink()
steam.OPEN_URL = lambda url: launched.append(url)
steam.OBSERVE_SECONDS = 0.0
steam.OPEN_URL = lambda url: (_ for _ in ()).throw(OSError("boom"))
r = st.tool_steam_install_game({"game": "730"})
check("install: a launch that fails is an error, not a claim", r["ok"] is False and "boom" in r["error"])
steam.OPEN_URL = lambda url: launched.append(url)

# --------------------------------------------------------------------------- steam_update_game
del launched[:]
r = st.tool_steam_update_game({"game": "all"})
check("update: 'all' is refused with the reason", r["ok"] is False and "'update all' isn't available" in r["error"] and launched == [])
check("update: 'everything' too", st.tool_steam_update_game({"game": "Everything"})["ok"] is False and launched == [])
r = st.tool_steam_update_game({})
check("update: no game -> a question", r.get("needs_clarification") and launched == [])
r = st.tool_steam_update_game({"game": "730"})
check("update: an app id that is not installed is not launched (use install for that)", r.get("needs_clarification") and launched == [])
r = st.tool_steam_update_game({"game": "Some Unowned Game"})
check("update: an unknown name asks, nothing launched", r.get("needs_clarification") and launched == [])
r = st.tool_steam_update_game({"game": "port"})
check("update: two matches ask which", r.get("needs_clarification") and launched == [])
r = st.tool_steam_update_game({"game": "220"})
check("update: a game already updating is not started again", r["launched"] is False and "already being fetched" in r["message"] and launched == [])
r = st.tool_steam_update_game({"game": "Terraria"})
check("update: an installed game launches the install URL for its id", launched == ["steam://install/105600"] and r["launched"] and r["name"] == "Terraria")
check("update: the message says 'update' and does not claim one exists", "update Terraria" in r["message"] and "up to date" not in r["message"])
running[0] = False
del launched[:]
check("update: Steam closed -> nothing launched", st.tool_steam_update_game({"game": "Terraria"})["ok"] is False and launched == [])
running[0] = True

# --------------------------------------------------------------------------- Steam stays read-only; registry rules
put(LIB2, 620, manifest(620, "Portal 2", 6, size=7_000_000_000, updated=1_600_000_000))
for extra in (STEAM / "steamapps" / "appmanifest_777.acf", LIB2 / "steamapps" / "appmanifest_888.acf"):
    if extra.exists():
        extra.unlink()
check("the whole fake Steam folder is byte-identical to how it started", tree_hash() == BEFORE)
check("install and update are confirmed with the owner; the read-only library is not",
      st.TOOL_CONFIRM_REQUIRED == {"steam_install_game", "steam_update_game"} and "steam_library" not in st.TOOL_CONFIRM_REQUIRED)
check("every tool has a schema, a function and a checklist entry",
      set(st.TOOLS) == {s["name"] for s in st.TOOL_SCHEMAS} == set(st.TEST_CHECKLIST) == set(st.TOOL_KEYWORDS))
check("the checklist says the write was Never run against a real Steam", "Never run against a real Steam" in json.dumps(st.TEST_CHECKLIST["steam_install_game"]))
check("every tool's checklist has the guest-refusal line or is the owner-only note",
      all("owner's" in json.dumps(v) for v in st.TEST_CHECKLIST.values()))
prov = steam.PROVIDER
check("the Steam provider still offers no pause / resume / cancel (decision S2)",
      not ({"pause", "resume", "cancel"} & set(getattr(prov, "capabilities", []) or [])))
tree = ast.parse((ROOT / "jarvis-cli" / "jarvis" / "actions" / "steam_tools.py").read_text(encoding="utf-8"))
called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
check("the action module never writes, deletes or kills anything itself",
      not ({"write_text", "write_bytes", "unlink", "rmtree", "remove", "kill", "terminate", "system", "Popen", "startfile"} & called))
src = (ROOT / "jarvis-cli" / "jarvis" / "hub" / "steam.py").read_text(encoding="utf-8")
check("hub/steam.py launches only the install URL (one Popen / startfile, both behind the URL check)",
      src.count("os.startfile(") == 1 and src.count("subprocess.Popen(") == 1 and "steam://validate" not in src.split("N22 b/c")[1])

print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
