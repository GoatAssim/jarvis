"""Tests for editing `allowed_channels` from the panel / CLI (L.36-P18).

    python3 tests/test_allowed_channels.py

The twin of tests/test_allowed_guilds.py, for the channel list.

  B1  add                     the happy path, normalisation, idempotence, junk ids
  B2  preview-first           adding to an EMPTY list, or an unseen id, writes
                              nothing until confirmed; the preview names which
                              channels go quiet (and in which server)
  B3  remove                  narrows; removing the LAST entry is refused even
                              with confirm (an empty list means every channel)
  B4  the REAL gate           permissions.decide() ignores a channel that is not
                              on the list the moment it is written; a thread
                              counts as its parent; DMs are untouched; a channel
                              whose server is filtered out stays silent
  B5  invariants              only `allowed_channels` is ever written; servers.py
                              still never writes either list; a hand-typed bare
                              id still loads; a corrupt file reads as no filter
  B6  change log              a real change is logged, a no-op / preview is not
  B7  the CLI                 channels-channels: view, preview, --yes, refusal,
                              usage; registered everywhere a command must be
  B8  server.js + the panel   the route validates ids / booleans and passes
                              argv; the panel is wired to it (static checks:
                              server.js needs express, the panel needs a browser)

No bot token, no socket, no model, no real ~/.jarvis: HOME is redirected to a
temp dir BEFORE any jarvis module is imported (AGENTS.md).
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-chans-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME  # Windows spelling of the same thing
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import (DISCORD, INSTAGRAM, allowed_channels, allowed_guilds,  # noqa: E402
                             changelog, people, permissions, servers, transcript)
from jarvis.channels import config as channel_config                                # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND = "100000000000000001", "200000000000000002"
G1, G2, G3 = "900000000000000001", "900000000000000002", "900000000000000003"
# Distinct channel ids per server (a real channel id is unique across servers).
C1, C2 = "800000000000000001", "800000000000000002"      # both in G1
C3 = "800000000000000003"                                # in G2
C4 = "800000000000000004"                                # in G3
C_UNSEEN = "800000000000000099"
THREAD = "700000000000000001"                            # a thread under C1
LAYOUT = {G1: [(C1, "general"), (C2, "random")], G2: [(C3, "chat")], G3: [(C4, "lobby")]}


def wipe():
    root = Path(_HOME) / ".jarvis"
    if root.exists():
        import shutil
        shutil.rmtree(root)
    servers._cache.clear()
    transcript.reset_cooldowns()


def reset(extra=None, layout=None):
    wipe()
    for uid, handle in ((OWNER, "boss"), (FRIEND, "friend")):
        people.touch(DISCORD, uid, handle=handle)
    cfg = channel_config.load_config()
    cfg[DISCORD].update({
        "enabled": True, "owner": OWNER, "allow_tools": True, "cooldown_seconds": 0,
        "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND],
        "tool_allowlist": [OWNER, FRIEND], "require_mention": True,
        "bot_token": "dummy-token",
    })
    cfg[DISCORD].update(extra or {})
    channel_config.save_config(cfg)
    for i, (gid, chans) in enumerate((layout or LAYOUT).items(), 1):
        servers.note_guild(DISCORD, gid, f"Server {i}",
                           channels=[(cid, name, "text") for cid, name in chans])


def cfg():
    return channel_config.platform_config(DISCORD)


def group_msg(guild, channel, thread=None):
    return permissions.IncomingMessage(
        DISCORD, permissions.CTX_GROUP, user_id=FRIEND, user_handle="friend", text="hi",
        mentioned=True, guild_id=guild, channel_id=channel, message_id="m1",
        thread_id=thread or channel)


def decide(guild, channel, thread=None):
    return permissions.decide(channel_config.platform_config(DISCORD), group_msg(guild, channel, thread))


# ===================================================================== B1

def test_add_basic():
    reset({"allowed_channels": [C1]})
    r = allowed_channels.set_allowed(DISCORD, C2)
    check("add to a non-empty list for a seen channel applies at once",
          r["ok"] and r["applied"] and not r["needs_confirm"], str(r))
    check("...and is written", cfg()["allowed_channels"] == [C1, C2], str(cfg()["allowed_channels"]))
    again = allowed_channels.set_allowed(DISCORD, C2)
    check("re-adding is a no-op, not an error", again["ok"] and not again["applied"]
          and cfg()["allowed_channels"] == [C1, C2])
    check("surrounding whitespace is tolerated", allowed_channels.set_allowed(DISCORD, f"  {C3} ")["applied"]
          and cfg()["allowed_channels"] == [C1, C2, C3])


def test_add_rejects_junk():
    reset({"allowed_channels": [C1]})
    for bad in ("", "abc", "-1", "12 34", "1" * 26, "@boss", "123;rm", None):
        r = allowed_channels.set_allowed(DISCORD, bad)
        check(f"junk id {bad!r} is refused", not r["ok"] and not r["applied"], str(r))
    check("nothing was written", cfg()["allowed_channels"] == [C1])
    r = allowed_channels.set_allowed(INSTAGRAM, C2)
    check("a platform without channels is refused", not r["ok"] and "no channels" in r["error"], str(r))
    check("unknown platform is refused", not allowed_channels.set_allowed("irc", C2)["ok"])


# ===================================================================== B2

def test_first_entry_is_preview_first():
    reset()  # no filter at all, four channels seen across three servers
    r = allowed_channels.set_allowed(DISCORD, C1)
    check("turning the filter ON only previews", r["ok"] and r["dry_run"] and r["needs_confirm"]
          and not r["applied"], str(r))
    check("...and writes nothing", cfg()["allowed_channels"] == [])
    names = sorted(x["id"] for x in r["would_stop"])
    check("...and names the OTHER channels that would go quiet", names == [C2, C3, C4], str(r["would_stop"]))
    check("...with a total", r["would_stop_total"] == 3)
    check("...each with its server, so the panel can say where",
          {x["id"]: x["guild_name"] for x in r["would_stop"]} == {C2: "Server 1", C3: "Server 2", C4: "Server 3"},
          str(r["would_stop"]))
    check("the preview does not list the channel being added", C1 not in [x["id"] for x in r["would_stop"]])
    check("the preview names what is being added",
          r.get("channel_name") == "general" and r.get("guild_name") == "Server 1", str(r))
    c = allowed_channels.set_allowed(DISCORD, C1, confirm=True)
    check("confirm applies it", c["ok"] and c["applied"] and cfg()["allowed_channels"] == [C1], str(c))


def test_first_entry_with_nothing_else_seen_needs_no_confirm():
    reset(layout={G1: [(C1, "general")]})
    r = allowed_channels.set_allowed(DISCORD, C1)
    check("the only known channel: nobody would go quiet, so no second step",
          r["ok"] and r["applied"] and cfg()["allowed_channels"] == [C1], str(r))


def test_unseen_id_is_preview_first_even_on_a_non_empty_list():
    reset({"allowed_channels": [C1]})
    r = allowed_channels.set_allowed(DISCORD, C_UNSEEN)
    check("an id the bot has never seen only previews", r["dry_run"] and r["unknown"]
          and not r["applied"], str(r))
    check("...and says what to do about a thread", "thread" in r["note"], r["note"])
    check("...and nothing was written", cfg()["allowed_channels"] == [C1])
    c = allowed_channels.set_allowed(DISCORD, C_UNSEEN, confirm=True)
    check("confirmed, it is written", c["applied"] and C_UNSEEN in cfg()["allowed_channels"], str(c))


def test_a_thread_id_is_unseen_not_silently_accepted():
    reset({"allowed_channels": [C1]})
    r = allowed_channels.set_allowed(DISCORD, THREAD)
    check("a thread's own id is just an id nobody has seen", r["unknown"] and r["needs_confirm"], str(r))


def test_left_servers_are_not_counted_as_going_quiet():
    reset()
    servers.note_guild(DISCORD, G3, "Server 3", left=True)
    r = allowed_channels.set_allowed(DISCORD, C1)
    check("a channel of a server the bot has left is not listed as 'would stop answering'",
          sorted(x["id"] for x in r["would_stop"]) == [C2, C3], str(r["would_stop"]))


def test_preview_list_is_capped():
    ids = [str(800000000000001000 + i) for i in range(40)]
    reset(layout={G1: [(cid, f"c{i}") for i, cid in enumerate(ids)]})
    r = allowed_channels.set_allowed(DISCORD, ids[0])
    check("the named list is capped but the total is honest",
          len(r["would_stop"]) == allowed_channels.MAX_LISTED and r["would_stop_total"] == 39,
          f"{len(r['would_stop'])} {r.get('would_stop_total')}")


# ===================================================================== B3

def test_remove_narrows():
    reset({"allowed_channels": [C1, C2]})
    r = allowed_channels.set_allowed(DISCORD, C2, remove=True)
    check("removing one of two applies at once", r["ok"] and r["applied"]
          and cfg()["allowed_channels"] == [C1], str(r))
    gone = allowed_channels.set_allowed(DISCORD, C3, remove=True)
    check("removing something not listed is a harmless no-op", gone["ok"] and not gone["applied"]
          and cfg()["allowed_channels"] == [C1])


def test_removing_the_last_entry_is_always_refused():
    reset({"allowed_channels": [C1]})
    for confirm in (False, True):
        r = allowed_channels.set_allowed(DISCORD, C1, remove=True, confirm=confirm)
        check(f"last entry refused (confirm={confirm})", not r["ok"] and not r["applied"]
              and "EVERY channel" in r["error"], str(r))
    check("...the list is untouched", cfg()["allowed_channels"] == [C1])
    check("the error says how to do it on purpose", "channels-set discord allowed_channels []"
          in allowed_channels.set_allowed(DISCORD, C1, remove=True)["error"])
    reset({"allowed_channels": [C1, C2]})
    allowed_channels.set_allowed(DISCORD, C1, remove=True)
    r = allowed_channels.set_allowed(DISCORD, C2, remove=True)
    check("two removals in a row still stop at one entry", not r["ok"] and cfg()["allowed_channels"] == [C2])


# ===================================================================== B4

def test_the_real_gate_follows_the_list():
    reset()
    check("no filter: a channel is let through", decide(G2, C3).allowed)
    allowed_channels.set_allowed(DISCORD, C1, confirm=True)
    check("on the list: still let through", decide(G1, C1).allowed)
    d = decide(G2, C3)
    check("off the list: turned away at the 'where' stage",
          not d.allowed and d.stage == "where", f"{d.allowed} {d.stage} {d.reason}")
    check("a sibling channel in the same server is off the list too", not decide(G1, C2).allowed)
    allowed_channels.set_allowed(DISCORD, C3)
    check("adding it lets it through again", decide(G2, C3).allowed)
    allowed_channels.set_allowed(DISCORD, C3, remove=True)
    check("removing it turns it away again", not decide(G2, C3).allowed)


def test_a_thread_counts_as_its_parent_channel():
    # discord_gateway._where reports the PARENT as channel_id and keeps the thread
    # in thread_id, so that is exactly what the gate sees.
    reset()
    allowed_channels.set_allowed(DISCORD, C1, confirm=True)
    check("a message in a thread under an allowed channel is let through",
          decide(G1, C1, thread=THREAD).allowed)
    check("a message in a thread under another channel is not", not decide(G2, C3, thread=THREAD).allowed)


def test_dm_is_not_affected_by_the_channel_filter():
    reset()
    allowed_channels.set_allowed(DISCORD, C1, confirm=True)
    dm = permissions.IncomingMessage(DISCORD, permissions.CTX_DM, user_id=FRIEND, user_handle="friend",
                                     text="hi", mentioned=True, message_id="m2", thread_id="dm1")
    check("a DM has no channel filter", permissions.decide(channel_config.platform_config(DISCORD), dm).allowed)


def test_channel_of_a_filtered_out_server_stays_silent_and_says_so():
    reset({"allowed_guilds": [G2]})
    r = allowed_channels.set_allowed(DISCORD, C1, confirm=True)
    check("the add applies", r["ok"] and r["applied"], str(r))
    check("...and warns that the server is not allowed", r["guild_blocked"] is True and "allowed_guilds" in r["note"],
          str(r))
    check("allowed_guilds is not touched by the channel editor", cfg()["allowed_guilds"] == [G2])
    d = decide(G1, C1)
    check("the gate still turns it away (server filter)", not d.allowed and d.stage == "where", f"{d.stage} {d.reason}")
    ok = allowed_channels.set_allowed(DISCORD, C3)
    check("a channel of an allowed server carries no warning", ok["applied"] and ok["guild_blocked"] is False, str(ok))


# ===================================================================== B5

def test_other_settings_are_untouched():
    reset({"allowed_guilds": [G1], "scopes": {f"channel:{C1}": {"allow_tools": False}},
           "owner": OWNER, "bot_token": "dummy-token"})
    before = channel_config.load_config()[DISCORD]
    ig_before = channel_config.load_config()[INSTAGRAM]
    allowed_channels.set_allowed(DISCORD, C1, confirm=True)
    allowed_channels.set_allowed(DISCORD, C2)
    allowed_channels.set_allowed(DISCORD, C2, remove=True)
    after = channel_config.load_config()[DISCORD]
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    check("exactly one key changed across add/add/remove", changed == ["allowed_channels"], str(changed))
    check("the other platform block is untouched", channel_config.load_config()[INSTAGRAM] == ig_before)


def test_server_switches_and_the_guild_editor_never_write_the_channel_list():
    reset({"allowed_channels": [C1]})
    for sw, val in (("enabled", "off"), ("tools", "off"), ("mention", "on"), ("enabled", "inherit")):
        servers.set_switch(DISCORD, "channel", C1, sw, val)
        servers.set_switch(DISCORD, "guild", G1, sw, val)
    allowed_guilds.set_allowed(DISCORD, G1, confirm=True)
    check("servers.set_switch / allowed_guilds leave allowed_channels alone", cfg()["allowed_channels"] == [C1])
    src = (REPO / "jarvis-cli" / "jarvis" / "channels" / "servers.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    check("servers.py has no code path that assigns allowed_channels",
          not re.search(r"allowed_channels[\"']\]\s*=|\[[\"']allowed_channels[\"']\]\s*=", code))
    mine = (REPO / "jarvis-cli" / "jarvis" / "channels" / "allowed_channels.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", mine, flags=re.S)
    check("the channel editor never names allowed_guilds or scopes in a write",
          not re.search(r"\[[\"'](allowed_guilds|scopes|bot_token)[\"']\]\s*=", code))


def test_bare_hand_typed_id_still_works_with_the_editor():
    reset()
    raw = json.loads(channel_config.CONFIG_FILE.read_text(encoding="utf-8"))
    raw[DISCORD]["allowed_channels"] = int(C1)        # no brackets: the classic hand-edit
    channel_config.CONFIG_FILE.write_text(json.dumps(raw), encoding="utf-8")
    v = allowed_channels.view(DISCORD)
    check("a bare int reads as a one-entry list", v["ok"] and v["allowed_channels"] == [C1], str(v))
    r = allowed_channels.set_allowed(DISCORD, C2)
    check("adding to it works and rewrites a proper list", r["applied"] and cfg()["allowed_channels"] == [C1, C2], str(r))
    raw = json.loads(channel_config.CONFIG_FILE.read_text(encoding="utf-8"))
    check("the file now holds a real list", raw[DISCORD]["allowed_channels"] == [C1, C2])
    r = allowed_channels.set_allowed(DISCORD, C1, remove=True)
    check("and the last-entry rule still holds afterwards",
          r["applied"] and not allowed_channels.set_allowed(DISCORD, C2, remove=True)["ok"])


def test_corrupt_config_does_not_crash_or_open_anything():
    reset()
    channel_config.CONFIG_FILE.write_text("{ not json", encoding="utf-8")
    v = allowed_channels.view(DISCORD)
    check("a corrupt file reads as 'no filter' without raising", v["ok"] and v["allowed_channels"] == [], str(v))


def test_view_names_and_flags_entries():
    reset({"allowed_channels": [C1, C_UNSEEN]})
    servers.note_guild(DISCORD, G1, "Server 1", left=True)
    v = allowed_channels.view(DISCORD)
    by = {e["id"]: e for e in v["entries"]}
    check("restricted flag", v["restricted"] is True)
    check("a known entry carries its channel and server name",
          by[C1]["name"] == "general" and by[C1]["guild_name"] == "Server 1" and by[C1]["known"] and by[C1]["left"],
          str(by[C1]))
    check("an unseen entry is flagged unknown", by[C_UNSEEN]["known"] is False and by[C_UNSEEN]["name"] == "")
    check("instagram has no channels", not allowed_channels.view(INSTAGRAM)["ok"])


def test_servers_view_reports_what_the_panel_needs():
    reset({"allowed_channels": [C1]})
    v = servers.view(DISCORD)
    chans = {c["id"]: c for s in v["servers"] for c in s["channels"]}
    check("view exposes allowed_channels", v["allowed_channels"] == [C1])
    check("a channel off the list is shown as not answering",
          chans[C2]["answering"] is False and "not in allowed_channels" in chans[C2]["why_not"], str(chans[C2]))
    check("a channel on the list is answering", chans[C1]["answering"] is True)


# ===================================================================== B6

def test_change_log():
    reset({"allowed_channels": [C1]})
    n0 = len(changelog.read_all())
    allowed_channels.set_allowed(DISCORD, C_UNSEEN)                  # preview: nothing written
    allowed_channels.set_allowed(DISCORD, C1)                        # no-op
    allowed_channels.set_allowed(DISCORD, C3, remove=True)           # no-op
    allowed_channels.set_allowed(DISCORD, C1, remove=True)           # refused
    check("previews, no-ops and refusals write no log line", len(changelog.read_all()) == n0)
    allowed_channels.set_allowed(DISCORD, C2)
    allowed_channels.set_allowed(DISCORD, C2, remove=True)
    lines = [e for e in changelog.read_all()[n0:] if e.get("list") == "allowed_channels"]
    check("an add and a remove are each logged once", len(lines) == 2 and [e.get("on") for e in lines] == [True, False],
          str(lines))
    check("the line holds the channel id and nothing else sensitive",
          all(e["ident"] == C2 and "token" not in json.dumps(e).lower() for e in lines))
    check("it reads as a sentence", "allowed-channels filter" in changelog.describe(lines[0]), changelog.describe(lines[0]))


# ===================================================================== B7

def cli(*argv):
    from jarvis import channels_cli
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            channels_cli.handle(list(argv))
        except SystemExit as exc:
            code = exc.code or 0
    try:
        return code, json.loads(buf.getvalue())
    except ValueError:
        return code, {"raw": buf.getvalue()}


def test_cli():
    reset()
    code, out = cli("channels-channels", DISCORD)
    check("no action = show the filter", code == 0 and out["ok"] and out["restricted"] is False, str(out))
    code, out = cli("channels-channels", DISCORD, "add", C1)
    check("add to an empty list previews and exits 0", code == 0 and out["needs_confirm"] and not out["applied"]
          and "--yes" in out.get("hint", ""), str(out))
    check("...and wrote nothing", cfg()["allowed_channels"] == [])
    code, out = cli("channels-channels", DISCORD, "add", C1, "--yes")
    check("--yes applies it", code == 0 and out["applied"] and cfg()["allowed_channels"] == [C1], str(out))
    code, out = cli("channels-channels", DISCORD, "add", C2)
    check("a second seen channel applies without --yes", code == 0 and out["applied"], str(out))
    code, out = cli("channels-channels", DISCORD, "remove", C2)
    check("remove works", code == 0 and out["applied"] and cfg()["allowed_channels"] == [C1], str(out))
    code, out = cli("channels-channels", DISCORD, "remove", C1, "--yes")
    check("removing the last entry exits 1 with ok false, even with --yes",
          code == 1 and out["ok"] is False and cfg()["allowed_channels"] == [C1], str(out))
    code, out = cli("channels-channels", DISCORD, "add", "nope")
    check("a bad id exits 1", code == 1 and out["ok"] is False, str(out))
    for argv in (("channels-channels",), ("channels-channels", "irc"), ("channels-channels", DISCORD, "add"),
                 ("channels-channels", DISCORD, "frob", C1), ("channels-channels", DISCORD, "add", C1, "--force"),
                 ("channels-channels", DISCORD, "add", C1, C2)):
        code, out = cli(*argv)
        check(f"usage error: {' '.join(argv[1:]) or '(none)'}", code == 1 and out.get("ok") is False, str(out))
    code, out = cli("channels-channels", INSTAGRAM)
    check("instagram: no channels", code == 1 and out["ok"] is False)


def test_command_is_registered_everywhere():
    from jarvis import channels_cli, reserved_names
    names = getattr(reserved_names, "RESERVED_NAMES", None) or set()
    check("dispatched", "channels-channels" in channels_cli.COMMANDS)
    check("reserved", "channels-channels" in names)
    check("covered by the slash palette data",
          '"channels-channels"' in (REPO / "web" / "public" / "slash-commands-data.js").read_text(encoding="utf-8"))
    check("documented in the usage text", "channels-channels" in channels_cli.USAGE)


# ===================================================================== B8

def test_server_route_is_strict():
    src = (REPO / "web" / "server.js").read_text(encoding="utf-8")
    m = re.search(r'app\.post\("/api/channels/servers/:platform/allowed-channels".*?\n\}\);', src, flags=re.S)
    check("the POST route exists", bool(m))
    body = m.group(0) if m else ""
    check("it checks the platform", "CHANNEL_PLATFORMS.has(platform)" in body)
    check("the id must be digits (CHANNEL_SNOWFLAKE)", "CHANNEL_SNOWFLAKE.test(id)" in body)
    check("remove / confirm must be real booleans", 'typeof req.body[flag] !== "boolean"' in body)
    check("--yes is only ever sent for an add", 'req.body?.confirm === true && req.body?.remove !== true' in body)
    check("argv array, no shell string", 'runJarvisOnce(args, ' in body and "exec(" not in body)
    check("the id reaches the CLI only after validation",
          "CHANNEL_SNOWFLAKE.test(id)" in body and body.index("CHANNEL_SNOWFLAKE.test(id)") < body.index("channels-channels"))
    check("a GET route exists too", 'app.get("/api/channels/servers/:platform/allowed-channels"' in src)


def test_panel_is_wired_to_the_route():
    js = (REPO / "web" / "public" / "channels-panel.js").read_text(encoding="utf-8")
    check("the panel posts to the allowed-channels route",
          '"/api/channels/servers/discord/allowed-channels"' in js)
    check("the block is rendered in the servers section", "allowedChannelsBlock(v)" in js
          and js.index("allowedGuildsBlock(v));") < js.index("allowedChannelsBlock(v));"))
    check("the old 'edited by hand' line is gone", "allowed_channels is edited by hand" not in js)
    check("the pure helpers are exported for the node test",
          all(n in js for n in ("channelChoices", "channelLabel", "channelPick", "channelInfo")))


# -------------------------------------------------------------- runner
# (Defined ABOVE the runner on purpose: AGENTS.md - a test appended below it
# would silently never run.)

if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{name} raised {exc!r}\n{traceback.format_exc()}")
    for line in FAILED:
        print("FAILED:", line)
    print(f"{PASSED} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
