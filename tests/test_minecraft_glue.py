"""Minecraft wave-1 glue: the four small fixes that join the four merged patches.

Pins:
  G1  list_optifine_versions and download_optifine are in tools.OWNER_ONLY_TOOLS, are loaded tools, and a chat guest is
      not offered them (download_mc_file / make_modpack_file are NOT pinned here: they join the set in the patch that
      adds them, and tests/test_l28b_followups.py requires every name in the set to be loaded).
  G2  web/server.js SETTINGS_NAME_RE accepts the one extra prefix `modhosts/` (the read-only modhosts/cache.json that
      settings_admin registers) and nothing broader; it stays in step with the page's own FILE_NAME_RE.
  G3  the group's ONE TOOL_PACK_INSTRUCTION carries the OptiFine "site changed" sentence, exactly once, and the
      OptiFine file declares no pack instruction of its own.
  G4  the split_dest refusal for a protected folder says what is protected (home folder itself, OS folders, ~/.jarvis).

No network, no real home folder.  Run: python tests/test_minecraft_glue.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# HOME is redirected BEFORE any jarvis module is imported (the hub and tool modules read it at import time).
HOME = tempfile.mkdtemp(prefix="jarvis-minecraft-glue-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER",
             "JARVIS_TOOL_MAKER_CONTEXT_FILE"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import tools  # noqa: E402
from jarvis.actions import minecraft_lookup as lk  # noqa: E402
from jarvis.actions import minecraft_optifine as mo  # noqa: E402
from jarvis.hub import service  # noqa: E402

PASS, FAIL = [], []
OPTIFINE_PAIR = ("list_optifine_versions", "download_optifine")
SENTENCE = ("If an OptiFine answer says the site changed, give the official optifine.net page link and nothing "
            "else; never look for another source.")
GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


class Sender:
    """Set JARVIS_CHANNEL_SENDER for a block (the same way tests/test_l28b_followups.py does)."""
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get("JARVIS_CHANNEL_SENDER")
        os.environ["JARVIS_CHANNEL_SENDER"] = self.value
        return self

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("JARVIS_CHANNEL_SENDER", None)
        else:
            os.environ["JARVIS_CHANNEL_SENDER"] = self.old


def _server_regex_source():
    """The text between the slashes of `const SETTINGS_NAME_RE = /.../;` in web/server.js (or None)."""
    server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
    m = re.search(r"^const SETTINGS_NAME_RE = /(.+)/;\s*$", server, re.M)
    return m.group(1) if m else None


def _as_python(js_source):
    # JS `$` (no m flag) means end of text; Python's `$` also matches before a trailing newline, so use \Z.
    assert js_source.endswith("$"), js_source
    return re.compile(js_source[:-1] + r"\Z")


def _prefixes(js_source):
    m = re.match(r"\^\(\(([^)]*)\)\\/\)\?", js_source)
    return sorted(m.group(1).split("|")) if m else None


# ---- G1 ----------------------------------------------------------------------------------------------------------
def test_g1_optifine_tools_are_owner_only_and_loaded():
    index = tools._tool_index()
    for name in OPTIFINE_PAIR:
        check("%s is in OWNER_ONLY_TOOLS" % name, name in tools.OWNER_ONLY_TOOLS)
        check("%s is a loaded tool (the l28b rule: a name in the set must exist)" % name, name in index)
        check("%s is declared by actions/minecraft_optifine.py" % name, name in mo.TOOLS)
    check("the two lookups stay in the set", {"search_mc_mods", "get_mc_project"} <= set(tools.OWNER_ONLY_TOOLS))
    # The set-wide "every name is a loaded tool" rule is tests/test_l28b_followups.py's job; it is not repeated here
    # because it also depends on the Stremio files (an unrelated loader rejection there must not fail this test).
    minecraft = {"search_mc_mods", "get_mc_project"} | set(OPTIFINE_PAIR)
    check("the four Minecraft names in the set are all loaded", all(n in index for n in minecraft),
          sorted(n for n in minecraft if n not in index))
    with Sender(GUEST):
        hidden = set(tools.hidden_from_sender())
    check("a chat guest is not offered the OptiFine pair", set(OPTIFINE_PAIR) <= hidden, sorted(hidden)[:6])
    with Sender(OWNER):
        hidden = set(tools.hidden_from_sender())
    check("the owner is", not (set(OPTIFINE_PAIR) & hidden))


# ---- G2 ----------------------------------------------------------------------------------------------------------
def test_g2_settings_name_regex_accepts_modhosts_and_nothing_broader():
    src = _server_regex_source()
    check("web/server.js has the SETTINGS_NAME_RE line", src is not None)
    if src is None:
        return
    rx = _as_python(src)
    for good in ("modhosts/cache.json", "channels/people.json", "web-data/favorites.json", "ai_config.json",
                 "curseforge.json"):
        check("accepted: %s" % good, rx.match(good) is not None)
    for bad in ("modhosts/../cache.json", "../modhosts/cache.json", "modhosts/../../x.json", "modhosts/sub/cache.json",
                "modhosts//cache.json", "modhosts/.json", "modhosts/cache.txt", "modhosts\\cache.json",
                "modhosts/cache.json\n", "Modhosts/cache.json", "modhosts2/cache.json", "optifine/cache.json",
                "/modhosts/cache.json", "modhosts/", "modhosts", "../ai_config.json", ".jarvis/x.json"):
        check("refused: %r" % bad, rx.match(bad) is None)
    check("exactly the prefixes channels, web-data and modhosts (one folder level, nothing broader)",
          _prefixes(src) == ["channels", "modhosts", "web-data"], _prefixes(src))
    check("the pattern still has one escaped slash (tests/test_l43_web_data.py counts it)", src.count("\\/") == 1, src)
    js = (ROOT / "web" / "public" / "settings.js").read_text(encoding="utf-8")
    m = re.search(r"const FILE_NAME_RE = /(.+)/;", js)
    page = re.match(r"\^\(\?:\(\?:([^)]*)\)\\/\)\?", m.group(1)) if m else None
    check("the page's FILE_NAME_RE names the same three prefixes (what the page sends, the server accepts)",
          page is not None and sorted(page.group(1).split("|")) == ["channels", "modhosts", "web-data"],
          m and m.group(1))


def test_g2_the_real_regex_agrees_when_node_is_available():
    node = shutil.which("node")
    src = _server_regex_source()
    if node is None or src is None:
        print("SKIP    node not found: the Python reading of the regex above is the only check")
        return
    names = ["modhosts/cache.json", "channels/people.json", "web-data/favorites.json", "ai_config.json",
             "modhosts/../cache.json", "modhosts/sub/cache.json", "modhosts/cache.json\n", "other/cache.json"]
    script = "const re=/%s/; const names=%s; process.stdout.write(JSON.stringify(names.map(n=>re.test(n))));" % (
        src, json.dumps(names))
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    try:
        got = json.loads(proc.stdout)
    except ValueError:
        got = None
    check("node agrees: only the first four names pass", got == [True, True, True, True, False, False, False, False],
          (got, proc.stderr[-200:]))


def test_g2_modhosts_is_still_gated_by_the_cli():
    # The server regex only decides what may be ASKED; settings_admin decides what exists.
    from jarvis import settings_admin as sa
    check("modhosts/cache.json is a known, read-only file", "modhosts/cache.json" in sa._FILES
          and sa._split_name("modhosts/cache.json") == ("modhosts", "cache.json"))
    check("any other file under modhosts/ is still not reachable", sa._split_name("modhosts/other.json") is None
          and sa._split_name("modhosts/../ai_config.json") is None)


# ---- G3 ----------------------------------------------------------------------------------------------------------
def test_g3_pack_instruction_has_the_optifine_sentence_once():
    text = lk.TOOL_PACK_INSTRUCTION
    check("the sentence is in the group's pack instruction", SENTENCE in text)
    check("...exactly once", text.count(SENTENCE) == 1)
    check("it is appended after the earlier rules (the instruction still ends the sentence list)", text.endswith(SENTENCE))
    registered = tools.AUTO_TOOL_PACK_INSTRUCTIONS.get("minecraft", "")
    check("the registry carries the instruction, with the sentence once", registered.count(SENTENCE) == 1)
    check("only minecraft_lookup.py declares the pack instruction", not hasattr(mo, "TOOL_PACK_INSTRUCTION"))
    check("both files are in group minecraft", lk.TOOL_GROUP == "minecraft" and mo.TOOL_GROUP == "minecraft")
    for old in ("never 'installed'", "dry_run", "never look for another source"):
        check("the earlier wording is untouched (%s)" % old, old in text)
    check("it is plain text for the per-turn tail (no newline, no template braces)", "\n" not in text and "{" not in text)


# ---- G4 ----------------------------------------------------------------------------------------------------------
def test_g4_split_dest_refusal_names_the_protected_folders():
    lib = Path(HOME) / "lib"
    cases = {"home folder itself": "~", "~/.jarvis": "~/.jarvis/x"}
    if os.name != "nt":
        cases["an OS folder"] = "/etc"
    messages = set()
    for label, spec in cases.items():
        where, problem = service.split_dest(spec, lib, allow_absolute=True)
        check("%s is refused even after the owner said yes" % label, where == {} and isinstance(problem, str) and problem,
              (where, problem))
        messages.add(problem)
    check("the message is the same for every refusal (one place)", len(messages) == 1, messages)
    msg = next(iter(messages)) if messages else ""
    low = msg.lower()
    check("it says what is protected: home folder, OS folder, ~/.jarvis, drive roots and '..'",
          "home folder" in low and "os folder" in low and "~/.jarvis" in msg and "drive root" in low and "'..'" in msg, msg)
    check("it keeps the phrase tests/test_hub_bulk.py looks for", "usable full path" in msg, msg)
    check("the old, untrue wording is gone", "(no drive roots, no '..')" not in msg, msg)
    check("it stays short (one line, under 140 characters)", "\n" not in msg and len(msg) < 140, len(msg))
    where, problem = service.split_dest("~", lib, allow_absolute=False)
    check("without the owner's yes it is still just needs_absolute", where == {} and problem == "needs_absolute", problem)


# --- runner (keep BELOW every test: it reads globals() when it executes) ----------------------------------------

if __name__ == "__main__":
    for name in sorted(n for n in list(globals()) if n.startswith("test_")):
        print(f"\n== {name}")
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append((name, f"raised {exc!r}"))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for item in FAIL:
        print("  FAILED:", item)
    sys.exit(1 if FAIL else 0)
