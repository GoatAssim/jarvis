"""Tests for the read side of the room registry (channel spaces S10a / S10b).

    python3 tests/test_space_view.py

`channels/space_view.py` lists the rooms Jarvis has saved and shows one room's
conversation. It must never write anything. HOME is redirected to a temp dir BEFORE any
jarvis module is imported (AGENTS.md). No network, no model.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-spaceview-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
_ROOT = Path(__file__).resolve().parent.parent
_CLI = str(_ROOT / "jarvis-cli")
sys.path.insert(0, _CLI)

from jarvis.channels import DISCORD, INSTAGRAM, space, space_view, spaces, transcript  # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


STATE = Path(_HOME) / ".jarvis"


def tree():
    if not STATE.exists():
        return {}
    return {str(p.relative_to(STATE)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(STATE.rglob("*")) if p.is_file()}


def line(platform, thread, **rec):
    base = {"dir": "in", "context": "group", "user_id": "u1", "author_name": "Ann",
            "text": "hello", "allowed": True, "at": "2026-10-01T10:00:00"}
    base.update(rec)
    transcript.append(platform, thread, base)


def stamp(platform, thread, iso):
    """A log's modified time is what listings sort by; make it the time of its last line."""
    from datetime import datetime
    moment = datetime.fromisoformat(iso).timestamp()
    for path in transcript._thread_files(platform, thread):
        os.utime(path, (moment, moment))


def build():
    shutil.rmtree(STATE, ignore_errors=True)
    # a Discord server channel (two people), a Discord DM, a live Instagram group,
    # an imported Instagram group, and a registered room that has no log at all.
    line(DISCORD, "C1", guild_id="G1", guild_name="Guild", channel_id="C1", channel_name="general",
         text="first", at="2026-10-01T10:00:00")
    line(DISCORD, "C1", guild_id="G1", guild_name="Guild", channel_id="C1", channel_name="general",
         user_id="u2", author_name="Bo", text="second", at="2026-10-01T10:05:00")
    line(DISCORD, "D1", context="dm", user_id="u9", text="private words")
    line(INSTAGRAM, "ig-live", user_id="i1", author_name="Cy", text="live group line",
         at="2026-10-02T09:00:00")
    for i, who in enumerate(["Ann", "Bob", "Cy"]):
        line(INSTAGRAM, "import-aaaa1111", user_id=f"name:{who}", author_name=who, text=f"imp {i}",
             imported=True, source="export", at=f"2020-09-13T12:0{i}:00", allowed=None)
    stamp(DISCORD, "C1", "2026-10-01T10:05:00")
    stamp(DISCORD, "D1", "2026-10-01T10:00:00")
    stamp(INSTAGRAM, "ig-live", "2026-10-02T09:00:00")
    stamp(INSTAGRAM, "import-aaaa1111", "2020-09-13T12:02:00")
    spaces.register_imported(INSTAGRAM, "import-aaaa1111", "Old Crew", "2020-09-13T12:00:00",
                             "2020-09-13T12:02:00", ["name:Ann", "name:Bob", "name:Cy"])
    spaces.register_imported(INSTAGRAM, "import-nolog00", "Empty Crew", "2020-01-01T00:00:00",
                             "2020-01-02T00:00:00", ["name:Zed"])


def by_id(result, thread):
    return next((r for r in result["spaces"] if r["thread_id"] == thread), None)


def test_empty_and_bad_arguments():
    shutil.rmtree(STATE, ignore_errors=True)
    out = space_view.list_spaces()
    check("an empty state lists nothing", out["ok"] and out["count"] == 0 and out["spaces"] == [], str(out))
    check("an unknown platform is an error", space_view.list_spaces("myspace")["ok"] is False)
    check("an unknown kind is an error", space_view.list_spaces(kind="everything")["ok"] is False)
    check("view of nothing is an error, not a crash",
          space_view.view(INSTAGRAM, "nope")["ok"] is False
          and space_view.view("myspace", "x")["ok"] is False
          and space_view.view(INSTAGRAM, "")["ok"] is False)
    check("nothing was created by looking", not STATE.exists() or not (STATE / "channels").exists())


def test_listing():
    build()
    out = space_view.list_spaces()
    ids = [r["thread_id"] for r in out["spaces"]]
    check("rooms only by default: no DM", "D1" not in ids and set(ids) == {
        "C1", "ig-live", "import-aaaa1111", "import-nolog00"}, str(ids))
    stamps = [r["modified"] for r in out["spaces"]]
    check("newest activity first", stamps == sorted(stamps, reverse=True)
          and ids == ["ig-live", "C1", "import-aaaa1111", "import-nolog00"],
          str(list(zip(ids, stamps))))
    chan = by_id(out, "C1")
    check("a server channel row", chan and chan["kind"] == "server_channel"
          and chan["platform"] == "discord" and chan["saved"] is True and chan["bytes"] > 0
          and chan["imported"] is False, str(chan))
    imp = by_id(out, "import-aaaa1111")
    check("an imported room row", imp and imp["kind"] == "group" and imp["imported"] is True
          and imp["title"] == "Group 'Old Crew'" and imp["speakers"] == 3, str(imp))
    empty = by_id(out, "import-nolog00")
    check("a registered room with no log is listed as unsaved", empty and empty["saved"] is False
          and empty["bytes"] == 0 and empty["modified"] == "2020-01-02T00:00:00", str(empty))
    live = by_id(out, "ig-live")
    check("a live group has no registry, so no speaker count", live and live["kind"] == "group"
          and live["speakers"] is None, str(live))
    check("the listing carries no message text", "first" not in json.dumps(out)
          and "live group line" not in json.dumps(out))


def test_filters():
    build()
    ids = lambda **kw: {r["thread_id"] for r in space_view.list_spaces(**kw)["spaces"]}   # noqa: E731
    check("platform filter", ids(platform=DISCORD) == {"C1"}
          and ids(platform=INSTAGRAM) == {"ig-live", "import-aaaa1111", "import-nolog00"})
    check("kind=server", ids(kind="server") == {"C1"})
    check("kind=group", ids(kind="group") == {"ig-live", "import-aaaa1111", "import-nolog00"})
    check("kind is case-insensitive", ids(kind="GROUP") == ids(kind="group"))
    check("kind=dm is the only way to list DMs", ids(kind="dm") == {"D1"})
    check("exact kinds work", ids(kind="server_channel") == {"C1"} and ids(kind="server_thread") == set())
    check("both filters together", ids(platform=INSTAGRAM, kind="server") == set())


def test_view():
    build()
    out = space_view.view(DISCORD, "C1")
    check("a room's conversation", out["ok"] and out["total"] == 2 and out["kind"] == "server_channel"
          and [e["text"] for e in out["entries"]] == ["first", "second"], str(out)[:300])
    check("with who spoke", {p["user_id"] for p in out["participants"]} == {"u1", "u2"})
    check("limit takes the newest lines", [e["text"] for e in space_view.view(DISCORD, "C1", 1)["entries"]]
          == ["second"])
    imp = space_view.view(INSTAGRAM, "import-aaaa1111")
    check("an imported room says so", imp["ok"] and imp["imported"] is True and imp["known"] is True
          and imp["title"] == "Group 'Old Crew'", str(imp)[:300])
    empty = space_view.view(INSTAGRAM, "import-nolog00")
    check("a registered room with no log is ok and empty", empty["ok"] and empty["total"] == 0
          and empty["kind"] == "group" and empty["known"] is True, str(empty)[:300])
    check("a path-like id cannot leave the transcript folder",
          space_view.view(INSTAGRAM, "../../people")["ok"] is False
          and space_view.view(INSTAGRAM, "..\\..\\channels")["ok"] is False)


def test_rooms_only_never_returns_a_dm():
    build()
    check("a DM is viewable by the owner's CLI", space_view.view(DISCORD, "D1")["ok"] is True)
    refused = space_view.view(DISCORD, "D1", rooms_only=True)
    check("but refused in rooms_only mode, with no content", refused["ok"] is False
          and "private words" not in json.dumps(refused), str(refused))
    transcript.append(INSTAGRAM, "dm-77", {"dir": "out", "text": "owner note", "kind": "owner_dm"})
    check("a thread of unknown kind is refused too",
          space_view.view(INSTAGRAM, "dm-77", rooms_only=True)["ok"] is False)
    check("a room is fine in rooms_only mode", space_view.view(DISCORD, "C1", rooms_only=True)["ok"] is True)


def test_reading_writes_nothing():
    build()
    before = tree()
    for _ in range(2):
        space_view.list_spaces()
        space_view.list_spaces(INSTAGRAM, "group")
        space_view.list_spaces(kind="dm")
        space_view.view(DISCORD, "C1")
        space_view.view(INSTAGRAM, "import-aaaa1111", 500)
        space_view.view(INSTAGRAM, "import-nolog00")
        space_view.view(INSTAGRAM, "does-not-exist")
    check("no file was created, changed or even touched", tree() == before)


def _cli(*args):
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONPATH=_CLI, JARVIS_RAW_ARCHIVE="0")
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], capture_output=True, text=True,
                          env=env, timeout=120)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"raw": proc.stdout + proc.stderr}


def test_cli():
    build()
    code, out = _cli("channels-spaces")
    check("channels-spaces lists rooms", code == 0 and out["count"] == 4, str(out)[:200])
    code, out = _cli("channels-spaces", "instagram", "--kind", "group")
    check("platform and --kind", code == 0 and out["count"] == 3)
    code, out = _cli("channels-spaces", "--kind=dm")
    check("--kind=dm", code == 0 and [r["thread_id"] for r in out["spaces"]] == ["D1"], str(out)[:200])
    for args in (("channels-spaces", "myspace"), ("channels-spaces", "--kind"), ("channels-spaces", "--bogus"),
                 ("channels-spaces", "instagram", "discord")):
        code, out = _cli(*args)
        check(f"bad arguments refused: {' '.join(args[1:])}", code != 0 and out.get("ok") is False, str(out)[:200])
    code, out = _cli("channels-spaces", "--kind", "nonsense")
    check("an unknown kind exits non-zero", code != 0 and out.get("ok") is False)
    code, out = _cli("channels-space", "discord", "C1")
    check("channels-space shows a room", code == 0 and out["total"] == 2 and out["ok"] is True, str(out)[:200])
    code, out = _cli("channels-space", "discord", "C1", "1")
    check("with a limit", code == 0 and out["shown"] == 1, str(out)[:200])
    for args in (("channels-space",), ("channels-space", "discord"), ("channels-space", "discord", "C1", "x"),
                 ("channels-space", "discord", "-x"), ("channels-space", "myspace", "C1")):
        code, out = _cli(*args)
        check(f"bad arguments refused: {' '.join(args[1:]) or '(none)'}", code != 0 and out.get("ok") is False,
              str(out)[:200])
    code, out = _cli("channels-space", "discord", "nothing-here")
    check("an unknown room exits non-zero", code != 0 and out.get("ok") is False)


def test_registration_and_web_routes():
    reserved = (_ROOT / "jarvis-cli" / "jarvis" / "reserved_names.py").read_text(encoding="utf-8")
    slash = (_ROOT / "web" / "public" / "slash-commands-data.js").read_text(encoding="utf-8")
    cli = (_ROOT / "jarvis-cli" / "jarvis" / "channels_cli.py").read_text(encoding="utf-8")
    for name in ("channels-spaces", "channels-space", "channels-import-instagram"):
        check(f"{name} is reserved, in the palette and in the CLI",
              f'"{name}"' in reserved and f'"{name}"' in slash and f'"{name}"' in cli)
    server = (_ROOT / "web" / "server.js").read_text(encoding="utf-8")
    spaces_get = re.search(r'app\.get\("/api/channels/spaces",.*?\n\}\);', server, re.S)
    conv_get = re.search(r'app\.get\("/api/channels/spaces/:platform/:threadId/conversation",.*?\n\}\);',
                         server, re.S)
    check("both GET routes exist", bool(spaces_get and conv_get))
    for label, block in (("list", spaces_get), ("conversation", conv_get)):
        text = block.group(0) if block else ""
        check(f"the {label} route only runs the read-only commands",
              all(cmd not in text for cmd in ("channels-space-set", "channels-import", "channels-send",
                                              "channels-server-set", "channels-person"))
              and ("channels-spaces" in text or "channels-space" in text))
    conv = conv_get.group(0) if conv_get else ""
    check("the conversation route checks platform, id and limit",
          "CHANNEL_PLATFORMS.has(platform)" in conv and "CHANNEL_SPACE_ID.test(threadId)" in conv
          and "channelInt(req.query.limit, 100, 1, 500)" in conv)
    lst = spaces_get.group(0) if spaces_get else ""
    check("the list route checks platform and kind against fixed sets",
          "CHANNEL_PLATFORMS.has(req.query.platform)" in lst and "SPACE_KIND_FILTERS.has(req.query.kind)" in lst)
    check("no POST was added for this", 'app.post("/api/channels/spaces",' not in server
          and "app.post(\"/api/channels/spaces/:platform/:threadId/conversation" not in server)


def main():
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=4))
    total = PASSED + len(FAILED)
    print(f"{PASSED}/{total} passed")
    for failure in FAILED:
        print("  FAIL:", failure)
    shutil.rmtree(_HOME, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
