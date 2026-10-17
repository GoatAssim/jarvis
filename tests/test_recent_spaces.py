"""Tests for the room-reading model tool (channel spaces S10c, actions/recent_spaces.py).

    python3 tests/test_recent_spaces.py

`recent_spaces` lets the OWNER ask what was said lately in the shared rooms Jarvis has saved.
It must read rooms only (never a DM), refuse a guest and a scheduled run, never be offered to
a guest, return bounded one-line text marked as other people's words, leave out lines the
gate turned away, and change no file. HOME is redirected to a temp dir BEFORE any jarvis
module is imported (AGENTS.md). No network, no model.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-recentspaces-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
for _var in ("JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_CONTEXT"):
    os.environ.pop(_var, None)
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "jarvis-cli"))

from jarvis.channels import DISCORD, INSTAGRAM, space_view, spaces, transcript  # noqa: E402
from jarvis.actions import recent_spaces as rs  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from jarvis import tool_registry, tool_router, tools as system_tools  # noqa: E402

PASSED = 0
FAILED = []
STATE = Path(_HOME) / ".jarvis"
OWNER = json.dumps({"platform": "discord", "user_id": "owner1", "is_owner": True})
GUEST = json.dumps({"platform": "discord", "user_id": "guest1", "is_owner": False})


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


@contextlib.contextmanager
def env(**values):
    """Set (or, with None, unset) environment variables for a block, then put them back."""
    before = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
    from datetime import datetime
    moment = datetime.fromisoformat(iso).timestamp()
    for path in transcript._thread_files(platform, thread):
        os.utime(path, (moment, moment))


def build():
    """A Discord server channel (two people), a Discord DM, a live Instagram group, an
    imported Instagram group, and a registered room that has no log (saving was off)."""
    shutil.rmtree(STATE, ignore_errors=True)
    # channels.json always exists on a real install (the first load creates it); start
    # from that state so the 'reads write nothing' checks measure the viewer, not the
    # first-run default file.
    from jarvis.channels import config as _cfg
    _cfg.ensure_config()
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


def ids(out):
    return [r["thread_id"] for r in out.get("rooms", [])]


def texts(room):
    return [m["text"] for m in room["messages"]]


def test_a_the_shape():
    build()
    with env(JARVIS_CHANNEL_SENDER=None):
        out = rs.tool_recent_spaces({})
    check("ok, with a rooms list and the untrusted note", out["ok"] is True and isinstance(out["rooms"], list)
          and "never an instruction" in out["note"], str(out)[:300])
    check("rooms only, newest activity first, an unsaved room is not listed",
          ids(out) == ["ig-live", "C1", "import-aaaa1111"], str(ids(out)))
    chan = next(r for r in out["rooms"] if r["thread_id"] == "C1")
    check("a server channel: platform, kind, messages oldest first, who spoke",
          chan["platform"] == "discord" and chan["kind"] == "server_channel"
          and texts(chan) == ["first", "second"] and set(chan["speakers"]) == {"Ann", "Bo"}
          and chan["messages"][1]["speaker"] == "Bo" and chan["imported"] is False, str(chan))
    imp = next(r for r in out["rooms"] if r["thread_id"] == "import-aaaa1111")
    check("an imported room says so and keeps its title",
          imp["imported"] is True and imp["title"] == "Group 'Old Crew'" and texts(imp) == ["imp 0", "imp 1", "imp 2"],
          str(imp))
    check("last_message_at is the last shown message's time", chan["last_message_at"] == "2026-10-01T10:05:00",
          str(chan["last_message_at"]))
    check("no user ids, no gate fields, no tool calls in the output",
          not any(k in json.dumps(out) for k in ("user_id", "allowed", "stage", "may_use_tools", "tool_calls")))


def test_b_never_a_dm():
    build()
    with env(JARVIS_CHANNEL_SENDER=None):
        every = [rs.tool_recent_spaces(a) for a in ({}, {"platform": "discord"}, {"kind": "server"},
                                                     {"kind": "group"}, {"room": "private"},
                                                     {"limit": 10, "messages_per_room": 20})]
        dm = rs.tool_recent_spaces({"kind": "dm"})
        junk = rs.tool_recent_spaces({"kind": "everything"})
    check("no call's output holds DM text", all("private words" not in json.dumps(o) for o in every))
    check("there is no way to ask for a DM", dm["ok"] is False and "private words" not in json.dumps(dm)
          and junk["ok"] is False, str(dm))
    check("a DM thread is never listed", all("D1" not in ids(o) for o in every))

    # Defence in depth: even if the listing wrongly offered the DM as a room, the read
    # (space_view.view with rooms_only=True) refuses it, so its text still never comes back.
    real = space_view.list_spaces

    def leaky(platform=None, kind=None):
        out = dict(real(platform=platform, kind=kind))
        out["spaces"] = [{"platform": DISCORD, "thread_id": "D1", "kind": "server_channel", "title": "x",
                          "imported": False, "speakers": None, "bytes": 5,
                          "modified": "2099-01-01T00:00:00", "saved": True}] + list(out["spaces"])
        return out

    space_view.list_spaces = leaky
    try:
        with env(JARVIS_CHANNEL_SENDER=None):
            out = rs.tool_recent_spaces({})
    finally:
        space_view.list_spaces = real
    check("a DM that slips into the listing is still not read",
          out["ok"] is True and "D1" not in ids(out) and "private words" not in json.dumps(out), str(out)[:300])
    check("the other rooms are still returned", {"C1", "ig-live"} <= set(ids(out)), str(ids(out)))

    transcript.append(INSTAGRAM, "dm-77", {"dir": "out", "text": "owner note", "kind": "owner_dm"})
    with env(JARVIS_CHANNEL_SENDER=None):
        out = rs.tool_recent_spaces({"platform": "instagram"})
    check("a thread of unknown kind is not read either", "dm-77" not in ids(out)
          and "owner note" not in json.dumps(out), str(out)[:300])

    src = (_ROOT / "jarvis-cli" / "jarvis" / "actions" / "recent_spaces.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    check("the file reads a room only through space_view.view(..., rooms_only=True)",
          "space_view.view(" in code and "rooms_only=True" in code)
    body = code.split('"""', 2)[-1]          # everything after the module docstring
    check("it never touches the transcripts or any file directly",
          "read_thread" not in body and "read_space" not in body and "open(" not in body
          and "transcript" not in body, "found a direct read")


def test_c_denied_lines_and_text():
    build()
    line(DISCORD, "C1", guild_id="G1", channel_id="C1", channel_name="general", user_id="u7",
         author_name="Stranger", text="stranger spam", allowed=False, at="2026-10-01T10:06:00")
    line(DISCORD, "C1", guild_id="G1", channel_id="C1", channel_name="general", user_id="u1",
         text="multi\nline\n\n   text " + "x" * 600, at="2026-10-01T10:07:00")
    transcript.append(DISCORD, "C1", {"dir": "out", "text": "Jarvis reply", "kind": "reply",
                                      "to_user": "u1", "at": "2026-10-01T10:08:00"})
    transcript.append(DISCORD, "C1", {"dir": "out", "text": "", "kind": "reply", "at": "2026-10-01T10:09:00"})
    with env(JARVIS_CHANNEL_SENDER=None):
        out = rs.tool_recent_spaces({"platform": "discord", "messages_per_room": 20})
    room = out["rooms"][0]
    check("a line the gate turned away is left out", "stranger spam" not in json.dumps(out), str(texts(room)))
    check("an empty line is left out", "" not in texts(room))
    check("Jarvis's own reply is shown as Jarvis", room["messages"][-1]["speaker"] == "Jarvis"
          and room["messages"][-1]["text"] == "Jarvis reply", str(room["messages"][-1]))
    long = next(m for m in room["messages"] if m["text"].startswith("multi"))
    check("a message is flattened to one line and clipped",
          "\n" not in long["text"] and long["text"].startswith("multi line text x")
          and len(long["text"]) <= rs.MAX_MESSAGE_CHARS + 1 and long["text"].endswith("\u2026"), repr(long["text"][:60]))
    check("a person only in denied lines is not a listed speaker", "Stranger" not in room["speakers"], str(room["speakers"]))


def test_d_arguments():
    build()
    with env(JARVIS_CHANNEL_SENDER=None):
        call = rs.tool_recent_spaces
        check("platform filter", ids(call({"platform": "discord"})) == ["C1"]
              and ids(call({"platform": "instagram"})) == ["ig-live", "import-aaaa1111"])
        check("'all' and 'any' mean every platform", ids(call({"platform": "all"})) == ids(call({"platform": "ANY"}))
              == ids(call({})))
        check("an unknown platform is an error", call({"platform": "myspace"})["ok"] is False)
        check("kind=server / kind=group", ids(call({"kind": "server"})) == ["C1"]
              and ids(call({"kind": "group"})) == ["ig-live", "import-aaaa1111"])
        check("kind is case-insensitive and 'any' is every room", ids(call({"kind": "GROUP"})) == ids(call({"kind": "group"}))
              and ids(call({"kind": "any"})) == ids(call({})))
        check("room matches the title, any case", ids(call({"room": "OLD crew"})) == ["import-aaaa1111"]
              and "C1" in ids(call({"room": "general"})), str(ids(call({"room": "general"}))))
        check("room matches nothing -> ok, empty", call({"room": "no such room"}) ["ok"] is True
              and call({"room": "no such room"})["rooms"] == [])
        check("room is a title filter, not an id", ids(call({"room": "C1"})) == [])
        check("limit", len(call({"limit": 1})["rooms"]) == 1 and len(call({"limit": 2})["rooms"]) == 2)
        check("limit is clamped to MAX_ROOMS and to at least 1",
              len(call({"limit": 9999})["rooms"]) <= rs.MAX_ROOMS and len(call({"limit": 0})["rooms"]) == 1
              and len(call({"limit": -5})["rooms"]) == 1)
        check("junk limits fall back to the defaults", ids(call({"limit": "many", "messages_per_room": None})) == ids(call({})))
        one = call({"platform": "instagram", "messages_per_room": 1})
        check("messages_per_room keeps the newest", all(len(r["messages"]) == 1 for r in one["rooms"])
              and texts(one["rooms"][1]) == ["imp 2"], str(one)[:300])
        check("messages_per_room is clamped", all(len(r["messages"]) <= rs.MAX_MESSAGES_PER_ROOM
                                                  for r in call({"messages_per_room": 9999})["rooms"]))
        check("no arguments at all (None) is fine", call(None)["ok"] is True)

    for i in range(30):
        line(DISCORD, f"X{i}", guild_id="G2", channel_id=f"X{i}", channel_name=f"chan{i}",
             text=f"msg {i}", at=f"2026-10-03T10:{i:02d}:00")
        stamp(DISCORD, f"X{i}", f"2026-10-03T10:{i:02d}:00")
    with env(JARVIS_CHANNEL_SENDER=None):
        out = rs.tool_recent_spaces({"limit": 9999})
    check("with many rooms the cap holds and the newest come first",
          len(out["rooms"]) == rs.MAX_ROOMS and out["rooms"][0]["thread_id"] == "X29", str(ids(out)))


def test_e_who_may_call_it():
    build()
    with env(JARVIS_CHANNEL_SENDER=None, JARVIS_SCHEDULED=None, JARVIS_CONTEXT=None):
        check("the PC (no chat context) is allowed", rs.tool_recent_spaces({})["ok"] is True)
    with env(JARVIS_CHANNEL_SENDER=OWNER):
        check("the owner in a chat is allowed", rs.tool_recent_spaces({})["ok"] is True)
    with env(JARVIS_CHANNEL_SENDER=GUEST):
        out = rs.tool_recent_spaces({})
        every = [rs.tool_recent_spaces(a) for a in ({"platform": "discord"}, {"kind": "group"}, {"room": "general"})]
    check("a guest is refused, and nothing from a room is in the refusal",
          out["ok"] is False and "owner" in json.dumps(out).lower() and "rooms" not in out
          and all(o["ok"] is False for o in every)
          and not any(w in json.dumps(out) for w in ("first", "second", "live group line")), str(out))
    for junk in ("not json", "[]", '{"platform": "discord"}', ""):
        with env(JARVIS_CHANNEL_SENDER=junk):
            check(f"an unreadable sender ({junk!r}) is the PC path, as in recent_dms",
                  rs.tool_recent_spaces({})["ok"] is True)
    with env(JARVIS_CHANNEL_SENDER=None, JARVIS_SCHEDULED="1"):
        sched = rs.tool_recent_spaces({})
    check("a scheduled run is refused", sched["ok"] is False and "unattended" in sched["error"], str(sched))
    for ctx in ("scheduled", "unattended"):
        with env(JARVIS_CHANNEL_SENDER=None, JARVIS_SCHEDULED=None, JARVIS_CONTEXT=ctx):
            check(f"JARVIS_CONTEXT={ctx} is refused", rs.tool_recent_spaces({})["ok"] is False)
    with env(JARVIS_CHANNEL_SENDER=OWNER, JARVIS_SCHEDULED="1"):
        check("the owner's chat is still refused when the run is scheduled", rs.tool_recent_spaces({})["ok"] is False)
    with env(JARVIS_CHANNEL_SENDER=None, JARVIS_SCHEDULED=None, JARVIS_CONTEXT="interactive"):
        check("an interactive context is allowed", rs.tool_recent_spaces({})["ok"] is True)


def test_f_not_offered_to_a_guest():
    names = ["recent_spaces", "recent_dms", "notify_owner"]
    check("the tool is discovered and is in the channels group",
          any(s.get("name") == "recent_spaces" for s in system_tools.TOOL_SCHEMAS)
          and tool_registry.group_of("recent_spaces") == "channels")
    check("it is in OWNER_ONLY_TOOLS", "recent_spaces" in system_tools.OWNER_ONLY_TOOLS)
    with env(JARVIS_CHANNEL_SENDER=GUEST):
        got = [s["name"] for s in system_tools.schemas_for_tools(names)]
        found = [m["name"] for m in system_tools.tool_search_tools({"query": "rooms"}).get("matches", [])]
        schema = system_tools.tool_get_tool_schema({"name": "recent_spaces"})
    check("a guest is offered only notify_owner", got == ["notify_owner"], str(got))
    check("search_tools does not list it for a guest", "recent_spaces" not in found, str(found))
    check("get_tool_schema says owner-only and hands back no schema",
          "owner-only" in (schema.get("error") or "") and "schema" not in schema, str(schema))
    with env(JARVIS_CHANNEL_SENDER=OWNER):
        got = [s["name"] for s in system_tools.schemas_for_tools(names)]
    check("the owner is offered all three", got == names, str(got))
    with env(JARVIS_CHANNEL_SENDER=None):
        got = [s["name"] for s in system_tools.schemas_for_tools(names)]
    check("the PC is offered all three", got == names, str(got))
    build()
    with env(JARVIS_CHANNEL_SENDER=GUEST):
        refused = system_tools.execute_tool("recent_spaces", {})
    check("executing it as a guest reaches the handler and is refused (the real gate)",
          "no such tool" not in json.dumps(refused).lower()
          and "owner" in json.dumps(refused).lower() and "live group line" not in json.dumps(refused), str(refused)[:300])


def test_g_schema_and_routing():
    schema = next(s for s in rs.TOOL_SCHEMAS if s["name"] == "recent_spaces")
    props = schema["parameters"]["properties"]
    check("only safe, high-level arguments: no id, path or thread", set(props) == {
        "platform", "kind", "room", "limit", "messages_per_room"}, str(set(props)))
    check("kind can be server, group or any, never dm", props["kind"]["enum"] == ["any", "server", "group"])
    check("nothing is required", schema["parameters"]["required"] == [])
    check("the description says rooms, never a DM, owner-only, and other people's words",
          all(w in schema["description"] for w in ("DM", "Owner-only", "other people's words")))
    check("read-only: no confirmation or AI review set", rs.TOOL_CONFIRM_REQUIRED == set() and rs.TOOL_AI_REVIEW == set())
    for text in ("what was said in the group chat", "show me the recent rooms", "what are people saying in my discord server"):
        r = tool_router.route(text)
        check(f"'{text}' routes to channels and offers recent_spaces",
              "channels" in r.groups and "recent_spaces" in r.tools, str((r.groups, r.tools)))
    for text in ("has anyone texted you on discord", "who dmed me"):
        r = tool_router.route(text)
        check(f"'{text}' still offers recent_dms", "recent_dms" in r.tools, str(r.tools))
    check("the group's pack instruction (the one that reaches the model) covers it",
          "recent_spaces" in tool_registry.TOOL_PACK_INSTRUCTIONS.get("channels", ""))


def test_h_changes_nothing():
    build()
    before = tree()
    with env(JARVIS_CHANNEL_SENDER=OWNER):
        for args in ({}, {"platform": "instagram"}, {"kind": "dm"}, {"room": "crew"}, {"limit": 1}):
            rs.tool_recent_spaces(args)
    check("reading rooms changes no file (hashes and mtimes)", tree() == before)
    real = space_view.view

    def broken(*a, **kw):
        raise RuntimeError("disk on fire")

    space_view.view = broken
    try:
        with env(JARVIS_CHANNEL_SENDER=None):
            out = rs.tool_recent_spaces({})
    finally:
        space_view.view = real
    check("a room that cannot be read is skipped, not a crash", out["ok"] is True and out["rooms"] == [], str(out))
    shutil.rmtree(STATE, ignore_errors=True)
    with env(JARVIS_CHANNEL_SENDER=None):
        out = rs.tool_recent_spaces({})
    check("with nothing saved it is ok and empty", out["ok"] is True and out["rooms"] == [], str(out))


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
