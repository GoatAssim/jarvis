"""Download Hub, brief 31: `play_control` is owner-only, and `play_episode` never crashes on a title lookup.

Covers:
  * play_control is in tools.OWNER_ONLY_TOOLS, so a chat guest is not OFFERED it (hidden_from_sender(),
    schemas_for_tools, get_tool_schema, search_tools); the owner on a chat platform, and no chat context at
    all (the PC, a scheduled job), still get it; the handler still refuses a guest that calls it anyway and
    sends nothing to mpv.  play_episode / play_local are NOT in the set (brief 31 leaves them; see its report).
  * the Test Checklist entry no longer says the tool is "still OFFERED to a guest".
  * play_episode given a title, when nothing can look titles up (no addon at all, every addon down, the addon
    list call raising, the guarded client failing to build): one plain sentence, ok False, no exception text,
    and NOTHING started: no browser, no player, no request to the Stremio server, no download row.  This is
    the regression test for the old `.strip()`-on-a-tuple crash ("play_episode failed: 'tuple' object has no
    attribute 'strip'").
    Both places that look a title up are covered (a bare title; a known stream whose player route failed).
  * a positive control: with a working fake addon the same title still resolves.

Everything here runs against the FAKES (_hub_fake_addon.py, _hub_fake_stremio.py, a fake browser opener and a
fake process launcher).  Nothing ran against a real Stremio, a real addon, a real mpv or a real chat guest.
No network.  Run: python tests/test_hub_play_gates.py
"""
import json
import os
import sys
import tempfile
import webbrowser
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-play-gates-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER", "JARVIS_TOOL_MAKER_CONTEXT_FILE"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_addon as fake_addon  # noqa: E402
import _hub_fake_stremio as fake  # noqa: E402
from jarvis import tool_registry, tools  # noqa: E402
from jarvis.actions import play_media  # noqa: E402
from jarvis.actions import stremio_discovery as disc  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import addons as A  # noqa: E402
from jarvis.hub.stremio import config, mpv, player  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
GUEST_NO_FLAG = json.dumps({"platform": "discord", "user_id": "42"})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})


def set_sender(raw):
    if raw is None:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    else:
        os.environ["JARVIS_CHANNEL_SENDER"] = raw


# ------------------------------------------------------------------ play_control: owner-only, not offered to a guest
check("play_control is in OWNER_ONLY_TOOLS", "play_control" in tools.OWNER_ONLY_TOOLS)
check("play_control is a real tool", "play_control" in tools.TOOLS and "play_control" in tool_registry.TOOL_INDEX)
check("play_episode and play_local are not in the set (left alone on purpose; the report says why)",
      not ({"play_episode", "play_local"} & tools.OWNER_ONLY_TOOLS))
check("the read-only hub tools are still open to a guest", not ({"dl_list", "dl_status"} & tools.OWNER_ONLY_TOOLS))

for label, raw in (("a chat guest", GUEST), ("a sender with no is_owner flag", GUEST_NO_FLAG)):
    set_sender(raw)
    check(f"{label}: hidden_from_sender() lists play_control", "play_control" in tools.hidden_from_sender())
    check(f"{label}: schemas_for_tools hands no play_control schema", tools.schemas_for_tools(["play_control"]) == [])
    check(f"{label}: get_tool_schema says it is owner-only", "owner-only" in json.dumps(tools.tool_get_tool_schema({"name": "play_control"})))
    found = [m["name"] for m in tools.tool_search_tools({"query": "play_control"}).get("matches", [])]
    check(f"{label}: search_tools does not list it", "play_control" not in found)

for label, raw in (("the owner on a chat platform", OWNER), ("no chat context (the PC, a scheduled job)", None)):
    set_sender(raw)
    check(f"{label}: play_control is not hidden", "play_control" not in tools.hidden_from_sender())
    got = tools.schemas_for_tools(["play_control"])
    check(f"{label}: schemas_for_tools hands the play_control schema", [s.get("name") for s in got] == ["play_control"])
    check(f"{label}: get_tool_schema does not call it owner-only", "owner-only" not in json.dumps(tools.tool_get_tool_schema({"name": "play_control"})))
    found = [m["name"] for m in tools.tool_search_tools({"query": "play_control"}).get("matches", [])]
    check(f"{label}: search_tools lists it", "play_control" in found)
set_sender(None)

# the handler is still the real gate: a guest who calls it anyway is refused and nothing is sent to mpv
sent = []
_orig_mpv_run = mpv.run
mpv.run = lambda *a, **k: (sent.append(a) or {"ok": True, "done": "paused"})
try:
    for label, raw in (("a chat guest", GUEST), ("a sender with no is_owner flag", GUEST_NO_FLAG), ("an unreadable sender", "{not json"),
                       ("a sender too deep to read", "[" * 200000)):
        set_sender(raw)
        res = play_media.tool_play_control({"action": "pause"})
        check(f"the handler still refuses {label}, in the owner's words, and sends nothing to mpv",
              res.get("ok") is False and "owner" in res.get("error", "") and "failed:" not in res.get("error", "") and sent == [])
    set_sender(OWNER)
    check("the handler lets the owner through", play_media.tool_play_control({"action": "pause"}).get("ok") is True and len(sent) == 1)
finally:
    mpv.run = _orig_mpv_run
    set_sender(None)

entry = play_media.TEST_CHECKLIST["play_control"]
text = json.dumps(entry)
check("the play_control checklist entry no longer says it is still offered to a guest", "OFFERED" not in text and "Open point 4" not in text)
check("...and says it is owner only", "OWNER_ONLY_TOOLS" in text and "owner" in text)

# ------------------------------------------------------------------ play_episode: no way to look a title up
FAKE, BASE, ST = fake.start()
os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
ABASE, AST, ASERVER = fake_addon.start()
A.DEFAULT_META_MANIFEST = "http://127.0.0.1:9/manifest.json"      # Cinemeta's stand-in: nothing listens on port 9
Store().set_settings({"allow_private_hosts": ["127.0.0.1"]})        # lets the loopback fakes through the guard
cfg_path = config.config_path()
cfg_path.parent.mkdir(parents=True, exist_ok=True)

opened, launched = [], []
webbrowser.open = lambda url: (opened.append(url) or True)


class FakeProc:
    pid = 77


player.subprocess.Popen = lambda argv, **kw: (launched.append(argv) or FakeProc())
player.find_one = lambda kind: "/fake/%s" % kind               # a player IS installed: if anything reached tier 3 it would launch


def write_cfg(addons):
    body = {"web": {"url": BASE}}
    if addons:
        body["addons"] = {"urls": [ABASE + "/cat/manifest.json", ABASE + "/str/manifest.json"]}
    cfg_path.write_text(json.dumps(body))


def snapshot():
    """Everything a title lookup must leave alone."""
    return (list(opened), list(launched), list(ST.registered), len(ST.log),
            len(Store().list_items(include_disabled=True, include_dismissed=True)))


ARGS = [
    ("a series title with season and episode", {"title": "Spirit Hunters", "season": 2, "episode": 3}),
    ("a title alone (looked up first, it may be a film)", {"title": "Spirit Hunters"}),
    ("a film by title", {"title": "Paper Lanterns", "type": "movie"}),
    ("a title with via=player", {"title": "Spirit Hunters", "season": 2, "episode": 3, "via": "player"}),
    ("a title with open=false", {"title": "Spirit Hunters", "season": 2, "episode": 3, "open": False}),
]
real_net, real_addons = disc._net, disc._addons


def boom_net():
    raise RuntimeError("boom http://127.0.0.1:1/secret-path")


def boom_addons(net, need=None):
    raise RuntimeError("boom http://127.0.0.1:1/secret-path")


def scenario_none():
    write_cfg(addons=False)                 # no addons list, no account: only the (dead) default is left
    AST.fail.clear()


def scenario_all_down():
    write_cfg(addons=True)
    AST.fail.clear()
    AST.fail.update({"/cat", "/str"})        # every addon answers 500


def scenario_list_raises():
    write_cfg(addons=True)
    AST.fail.clear()
    disc._addons = boom_addons


def scenario_net_raises():
    write_cfg(addons=True)
    AST.fail.clear()
    disc._net = boom_net


SCENARIOS = [
    ("no addon installed and Cinemeta unreachable", scenario_none, "no addon can look titles up"),
    ("every addon down (500 to everything)", scenario_all_down, "no addon can look titles up"),
    ("the addon list call raising", scenario_list_raises, "title lookup couldn't start"),
    ("the guarded client failing to build", scenario_net_raises, "title lookup couldn't start"),
]

try:
    for sname, setup, words in SCENARIOS:
        disc._net, disc._addons = real_net, real_addons
        setup()
        for aname, args in ARGS:
            before = snapshot()
            res = play_media.tool_play_episode(dict(args))
            err = res.get("error") if isinstance(res, dict) else None
            tag = f"{sname} / {aname}"
            check(f"{tag}: ok is False with one error sentence", isinstance(res, dict) and res.get("ok") is False and isinstance(err, str) and err)
            check(f"{tag}: it says why and what to do ({words!r}, the title, 'IMDb id')",
                  isinstance(err, str) and words in err and "IMDb id" in err and args["title"] in err)
            check(f"{tag}: no exception text (no 'failed:', no 'tuple', no 'Traceback', no 'boom', no address)",
                  isinstance(err, str) and not any(w in err for w in ("failed:", "tuple", "Traceback", "boom", "secret-path", "127.0.0.1")))
            check(f"{tag}: not a question, nothing to retry blindly", isinstance(res, dict) and not res.get("needs_clarification") and not res.get("link"))
            check(f"{tag}: nothing started (no browser, no player, no server request, no download row)", snapshot() == before)
finally:
    disc._net, disc._addons = real_net, real_addons

# the SECOND place a title is looked up: a stream is known, the player route failed (no mpv or VLC installed), so only
# the web tiers are left and they need an IMDb id.  Same lookup, same sentence; the answer also lists what was tried.
scenario_none()
player.find_one = lambda kind: ""
try:
    before = snapshot()
    res = play_media.tool_play_episode({"infohash": "11" * 20, "title": "Spirit Hunters", "season": 2, "episode": 3})
    err = res.get("error") or ""
    check("a known stream + a title, no player installed, no addon: ok False, the same plain sentence, and what was tried",
          res.get("ok") is False and "no addon can look titles up" in err and "IMDb id" in err and isinstance(res.get("tried"), list) and res["tried"])
    check("...no exception text, and nothing started", not any(w in err for w in ("failed:", "tuple", "Traceback")) and snapshot() == before)
finally:
    player.find_one = lambda kind: "/fake/%s" % kind

# the exact sentence of the fixed branch, and the helper's own shape
scenario_none()
res = play_media.tool_play_episode({"title": "Spirit Hunters", "season": 2, "episode": 3})
check("the old crash is gone: the answer starts with the intended sentence and carries no trailing space",
      res["error"].startswith("no addon can look titles up, so 'Spirit Hunters' can't be found; give its IMDb id (tt...).")
      and res["error"] == res["error"].strip())
got = play_media._imdb_from_title("Spirit Hunters", None)
check("_imdb_from_title returns (None, None, None, answer) with an ok False answer",
      isinstance(got, tuple) and len(got) == 4 and got[:3] == (None, None, None) and got[3].get("ok") is False)

# ------------------------------------------------------------------ positive control: a working addon still resolves
write_cfg(addons=True)
AST.fail.clear()
before = snapshot()
res = play_media.tool_play_episode({"title": "Spirit Hunters", "season": 2, "episode": 3, "open": False})
SH = fake_addon.SHOW
check("with a working fake addon the same title resolves to its episode link (nothing opened: open=false)",
      res.get("ok") is True and res.get("tier") == 1 and res["link"].endswith("#/detail/series/%s/%s:2:3" % (SH, SH)) and snapshot() == before)

fake.stop(FAKE)
ASERVER.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
