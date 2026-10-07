"""Tests for editing `allowed_guilds` from the panel / CLI (L.36-P17).

    python3 tests/test_allowed_guilds.py

  A1  add                     the happy path, normalisation, idempotence
  A2  preview-first           adding to an EMPTY list, or an unseen id, writes
                              nothing until confirmed; the preview names who goes quiet
  A3  remove                  narrows; removing the LAST entry is refused even
                              with confirm (an empty list means every server)
  A4  the REAL gate           permissions.decide() ignores a server that is not
                              on the list the moment it is written
  A5  invariants              servers.set_switch still never writes the list;
                              allowed_channels / scopes / tokens are untouched;
                              a hand-typed bare id still loads
  A6  change log              a real change is logged, a no-op / preview is not
  A7  the CLI                 channels-guilds: view, preview, --yes, refusal, usage;
                              registered everywhere a command must be
  A8  server.js               the route validates ids / booleans and passes argv
                              (static check: server.js needs express to run)

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

_HOME = tempfile.mkdtemp(prefix="jarvis-guilds-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME  # Windows spelling of the same thing
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import (DISCORD, INSTAGRAM, allowed_guilds, changelog,  # noqa: E402
                             people, permissions, servers, transcript)
from jarvis.channels import config as channel_config                          # noqa: E402

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
G_UNSEEN = "900000000000000099"
C1 = "800000000000000001"


def wipe():
    root = Path(_HOME) / ".jarvis"
    if root.exists():
        import shutil
        shutil.rmtree(root)
    servers._cache.clear()
    transcript.reset_cooldowns()


def reset(extra=None, seen=(G1, G2, G3)):
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
    for i, gid in enumerate(seen, 1):
        servers.note_guild(DISCORD, gid, f"Server {i}", channels=[(C1, "general", "text")])


def cfg():
    return channel_config.platform_config(DISCORD)


def group_msg(guild):
    return permissions.IncomingMessage(
        DISCORD, permissions.CTX_GROUP, user_id=FRIEND, user_handle="friend", text="hi",
        mentioned=True, guild_id=guild, channel_id=C1, message_id="m1", thread_id=C1)


# ===================================================================== A1

def test_add_basic():
    reset({"allowed_guilds": [G1]})
    r = allowed_guilds.set_allowed(DISCORD, G2)
    check("add to a non-empty list for a seen server applies at once",
          r["ok"] and r["applied"] and not r["needs_confirm"], str(r))
    check("...and is written", cfg()["allowed_guilds"] == [G1, G2], str(cfg()["allowed_guilds"]))
    again = allowed_guilds.set_allowed(DISCORD, G2)
    check("re-adding is a no-op, not an error", again["ok"] and not again["applied"]
          and cfg()["allowed_guilds"] == [G1, G2])
    check("surrounding whitespace is tolerated", allowed_guilds.set_allowed(DISCORD, f"  {G3} ")["applied"]
          and cfg()["allowed_guilds"] == [G1, G2, G3])


def test_add_rejects_junk():
    reset({"allowed_guilds": [G1]})
    for bad in ("", "abc", "-1", "12 34", "1" * 26, "@boss", "123;rm", None):
        r = allowed_guilds.set_allowed(DISCORD, bad)
        check(f"junk id {bad!r} is refused", not r["ok"] and not r["applied"], str(r))
    check("nothing was written", cfg()["allowed_guilds"] == [G1])
    r = allowed_guilds.set_allowed(INSTAGRAM, G2)
    check("a platform without servers is refused", not r["ok"] and "no servers" in r["error"], str(r))
    check("unknown platform is refused", not allowed_guilds.set_allowed("irc", G2)["ok"])


# ===================================================================== A2

def test_first_entry_is_preview_first():
    reset()  # no filter at all, three servers seen
    r = allowed_guilds.set_allowed(DISCORD, G1)
    check("turning the filter ON only previews", r["ok"] and r["dry_run"] and r["needs_confirm"]
          and not r["applied"], str(r))
    check("...and writes nothing", cfg()["allowed_guilds"] == [])
    names = sorted(x["id"] for x in r["would_stop"])
    check("...and names the OTHER servers that would go quiet", names == [G2, G3], str(r["would_stop"]))
    check("...with a total", r["would_stop_total"] == 2)
    check("the preview does not list the server being added",
          G1 not in [x["id"] for x in r["would_stop"]])
    c = allowed_guilds.set_allowed(DISCORD, G1, confirm=True)
    check("confirm applies it", c["ok"] and c["applied"] and cfg()["allowed_guilds"] == [G1], str(c))


def test_first_entry_with_nothing_else_seen_needs_no_confirm():
    reset(seen=(G1,))
    r = allowed_guilds.set_allowed(DISCORD, G1)
    check("the only known server: nobody would go quiet, so no second step",
          r["ok"] and r["applied"] and cfg()["allowed_guilds"] == [G1], str(r))


def test_unseen_id_is_preview_first_even_on_a_non_empty_list():
    reset({"allowed_guilds": [G1]})
    r = allowed_guilds.set_allowed(DISCORD, G_UNSEEN)
    check("an id the bot has never seen only previews", r["dry_run"] and r["unknown"]
          and not r["applied"], str(r))
    check("...and nothing was written", cfg()["allowed_guilds"] == [G1])
    c = allowed_guilds.set_allowed(DISCORD, G_UNSEEN, confirm=True)
    check("confirmed, it is written", c["applied"] and G_UNSEEN in cfg()["allowed_guilds"], str(c))


def test_left_servers_are_not_counted_as_going_quiet():
    reset()
    servers.note_guild(DISCORD, G3, "Server 3", left=True)
    r = allowed_guilds.set_allowed(DISCORD, G1)
    check("a server the bot has left is not listed as 'would stop answering'",
          [x["id"] for x in r["would_stop"]] == [G2], str(r["would_stop"]))


def test_preview_list_is_capped():
    ids = [str(900000000000001000 + i) for i in range(40)]
    reset(seen=tuple(ids))
    r = allowed_guilds.set_allowed(DISCORD, G1)
    check("the named list is capped but the total is honest",
          len(r["would_stop"]) == allowed_guilds.MAX_LISTED and r["would_stop_total"] == 40, str(len(r["would_stop"])))


# ===================================================================== A3

def test_remove_narrows():
    reset({"allowed_guilds": [G1, G2]})
    r = allowed_guilds.set_allowed(DISCORD, G2, remove=True)
    check("removing one of two applies at once", r["ok"] and r["applied"]
          and cfg()["allowed_guilds"] == [G1], str(r))
    gone = allowed_guilds.set_allowed(DISCORD, G3, remove=True)
    check("removing something not listed is a harmless no-op", gone["ok"] and not gone["applied"]
          and cfg()["allowed_guilds"] == [G1])


def test_removing_the_last_entry_is_always_refused():
    reset({"allowed_guilds": [G1]})
    for confirm in (False, True):
        r = allowed_guilds.set_allowed(DISCORD, G1, remove=True, confirm=confirm)
        check(f"last entry refused (confirm={confirm})", not r["ok"] and not r["applied"]
              and "EVERY server" in r["error"], str(r))
    check("...the list is untouched", cfg()["allowed_guilds"] == [G1])
    check("the error says how to do it on purpose", "channels-set discord allowed_guilds []"
          in allowed_guilds.set_allowed(DISCORD, G1, remove=True)["error"])
    # an empty list can never be produced through this module
    reset({"allowed_guilds": [G1, G2]})
    allowed_guilds.set_allowed(DISCORD, G1, remove=True)
    r = allowed_guilds.set_allowed(DISCORD, G2, remove=True)
    check("two removals in a row still stop at one entry", not r["ok"] and cfg()["allowed_guilds"] == [G2])


def test_confirm_never_applies_a_removal_that_empties_the_list():
    reset({"allowed_guilds": [G1]})
    r = allowed_guilds.set_allowed(DISCORD, "900000000000000001", remove=True, confirm=True)
    check("confirm is ignored for removals", not r["ok"] and cfg()["allowed_guilds"] == [G1])


# ===================================================================== A4

def test_the_real_gate_follows_the_list():
    reset()
    check("no filter: a server is let through",
          permissions.decide(channel_config.platform_config(DISCORD), group_msg(G2)).allowed)
    allowed_guilds.set_allowed(DISCORD, G1, confirm=True)
    c = channel_config.platform_config(DISCORD)
    check("on the list: still let through", permissions.decide(c, group_msg(G1)).allowed)
    d = permissions.decide(c, group_msg(G2))
    check("off the list: turned away at the 'where' stage",
          not d.allowed and d.stage == "where", f"{d.allowed} {d.stage} {d.reason}")
    allowed_guilds.set_allowed(DISCORD, G2)
    check("adding it lets it through again",
          permissions.decide(channel_config.platform_config(DISCORD), group_msg(G2)).allowed)
    allowed_guilds.set_allowed(DISCORD, G2, remove=True)
    check("removing it turns it away again",
          not permissions.decide(channel_config.platform_config(DISCORD), group_msg(G2)).allowed)


def test_dm_is_not_affected_by_the_server_filter():
    reset()
    allowed_guilds.set_allowed(DISCORD, G1, confirm=True)
    dm = permissions.IncomingMessage(DISCORD, permissions.CTX_DM, user_id=FRIEND, user_handle="friend",
                                     text="hi", mentioned=True, message_id="m2", thread_id="dm1")
    check("a DM has no guild, so the server filter does not touch it",
          permissions.decide(channel_config.platform_config(DISCORD), dm).allowed)


# ===================================================================== A5

def test_other_settings_are_untouched():
    reset({"allowed_channels": [C1], "scopes": {f"guild:{G1}": {"allow_tools": False}},
           "owner": OWNER, "bot_token": "dummy-token"})
    before = channel_config.load_config()[DISCORD]
    ig_before = channel_config.load_config()[INSTAGRAM]
    allowed_guilds.set_allowed(DISCORD, G1, confirm=True)
    allowed_guilds.set_allowed(DISCORD, G2)
    allowed_guilds.set_allowed(DISCORD, G2, remove=True)
    after = channel_config.load_config()[DISCORD]
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    check("exactly one key changed across add/add/remove", changed == ["allowed_guilds"], str(changed))
    check("the other platform block is untouched",
          channel_config.load_config()[INSTAGRAM] == ig_before)


def test_set_switch_still_never_writes_the_list():
    reset({"allowed_guilds": [G1]})
    for sw, val in (("enabled", "off"), ("tools", "off"), ("mention", "on"), ("enabled", "inherit")):
        servers.set_switch(DISCORD, "guild", G1, sw, val)
    check("servers.set_switch leaves allowed_guilds alone", cfg()["allowed_guilds"] == [G1])
    src = (REPO / "jarvis-cli" / "jarvis" / "channels" / "servers.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    check("servers.py has no code path that assigns allowed_guilds",
          not re.search(r"allowed_guilds[\"']\]\s*=|\[[\"']allowed_guilds[\"']\]\s*=", code))


def test_bare_hand_typed_id_still_works_with_the_editor():
    reset()
    raw = json.loads(channel_config.CONFIG_FILE.read_text(encoding="utf-8"))
    raw[DISCORD]["allowed_guilds"] = int(G1)        # no brackets: the classic hand-edit
    channel_config.CONFIG_FILE.write_text(json.dumps(raw), encoding="utf-8")
    v = allowed_guilds.view(DISCORD)
    check("a bare int reads as a one-entry list", v["ok"] and v["allowed_guilds"] == [G1], str(v))
    r = allowed_guilds.set_allowed(DISCORD, G2)
    check("adding to it works and rewrites a proper list", r["applied"] and cfg()["allowed_guilds"] == [G1, G2], str(r))
    raw = json.loads(channel_config.CONFIG_FILE.read_text(encoding="utf-8"))
    check("the file now holds a real list", raw[DISCORD]["allowed_guilds"] == [G1, G2])
    r = allowed_guilds.set_allowed(DISCORD, G1, remove=True)
    check("and the last-entry rule still holds afterwards",
          r["applied"] and not allowed_guilds.set_allowed(DISCORD, G2, remove=True)["ok"])


def test_corrupt_config_does_not_crash_or_open_anything():
    reset()
    channel_config.CONFIG_FILE.write_text("{ not json", encoding="utf-8")
    v = allowed_guilds.view(DISCORD)
    check("a corrupt file reads as 'no filter' without raising", v["ok"] and v["allowed_guilds"] == [], str(v))


def test_view_names_and_flags_entries():
    reset({"allowed_guilds": [G1, G_UNSEEN]})
    servers.note_guild(DISCORD, G1, "Server 1", left=True)
    v = allowed_guilds.view(DISCORD)
    by = {e["id"]: e for e in v["entries"]}
    check("restricted flag", v["restricted"] is True)
    check("a known entry carries its name", by[G1]["name"] == "Server 1" and by[G1]["known"] and by[G1]["left"])
    check("an unseen entry is flagged unknown", by[G_UNSEEN]["known"] is False and by[G_UNSEEN]["name"] == "")
    check("instagram has no servers", not allowed_guilds.view(INSTAGRAM)["ok"])


def test_servers_view_reports_what_the_panel_needs():
    reset({"allowed_guilds": [G1]})
    v = servers.view(DISCORD)
    by = {s["id"]: s for s in v["servers"]}
    check("view exposes allowed_guilds", v["allowed_guilds"] == [G1])
    check("a server off the list is shown as not answering",
          by[G2]["answering"] is False and "not in allowed_guilds" in by[G2]["why_not"])
    check("known flag is what the picker filters on", by[G1]["known"] and not by[G1]["left"])


# ===================================================================== A6

def test_change_log():
    reset({"allowed_guilds": [G1]})
    n0 = len(changelog.read_all())
    allowed_guilds.set_allowed(DISCORD, G_UNSEEN)                  # preview: nothing written
    allowed_guilds.set_allowed(DISCORD, G1)                        # no-op
    allowed_guilds.set_allowed(DISCORD, G2, remove=True)           # no-op
    allowed_guilds.set_allowed(DISCORD, G1, remove=True)           # refused
    check("previews, no-ops and refusals write no log line", len(changelog.read_all()) == n0)
    allowed_guilds.set_allowed(DISCORD, G2)
    allowed_guilds.set_allowed(DISCORD, G2, remove=True)
    lines = [e for e in changelog.read_all()[n0:] if e.get("list") == "allowed_guilds"]
    check("an add and a remove are each logged once", len(lines) == 2 and [e.get("on") for e in lines] == [True, False],
          str(lines))
    check("the line holds the server id and nothing else sensitive",
          all(e["ident"] == G2 and "token" not in json.dumps(e).lower() for e in lines))
    check("it reads as a sentence", "allowed-servers filter" in changelog.describe(lines[0]), changelog.describe(lines[0]))


# ===================================================================== A7

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
    code, out = cli("channels-guilds", DISCORD)
    check("no action = show the filter", code == 0 and out["ok"] and out["restricted"] is False, str(out))
    code, out = cli("channels-guilds", DISCORD, "add", G1)
    check("add to an empty list previews and exits 0", code == 0 and out["needs_confirm"] and not out["applied"]
          and "--yes" in out.get("hint", ""), str(out))
    check("...and wrote nothing", cfg()["allowed_guilds"] == [])
    code, out = cli("channels-guilds", DISCORD, "add", G1, "--yes")
    check("--yes applies it", code == 0 and out["applied"] and cfg()["allowed_guilds"] == [G1], str(out))
    code, out = cli("channels-guilds", DISCORD, "add", G2)
    check("a second seen server applies without --yes", code == 0 and out["applied"], str(out))
    code, out = cli("channels-guilds", DISCORD, "remove", G2)
    check("remove works", code == 0 and out["applied"] and cfg()["allowed_guilds"] == [G1], str(out))
    code, out = cli("channels-guilds", DISCORD, "remove", G1, "--yes")
    check("removing the last entry exits 1 with ok false, even with --yes",
          code == 1 and out["ok"] is False and cfg()["allowed_guilds"] == [G1], str(out))
    code, out = cli("channels-guilds", DISCORD, "add", "nope")
    check("a bad id exits 1", code == 1 and out["ok"] is False, str(out))
    for argv in (("channels-guilds",), ("channels-guilds", "irc"), ("channels-guilds", DISCORD, "add"),
                 ("channels-guilds", DISCORD, "frob", G1), ("channels-guilds", DISCORD, "add", G1, "--force"),
                 ("channels-guilds", DISCORD, "add", G1, G2)):
        code, out = cli(*argv)
        check(f"usage error: {' '.join(argv[1:]) or '(none)'}", code == 1 and out.get("ok") is False, str(out))
    code, out = cli("channels-guilds", INSTAGRAM)
    check("instagram: no servers", code == 1 and out["ok"] is False)


def test_command_is_registered_everywhere():
    from jarvis import channels_cli, reserved_names
    names = getattr(reserved_names, "RESERVED_NAMES", None) or set()
    check("dispatched", "channels-guilds" in channels_cli.COMMANDS)
    check("reserved", "channels-guilds" in names)
    check("covered by the slash palette data",
          '"channels-guilds"' in (REPO / "web" / "public" / "slash-commands-data.js").read_text(encoding="utf-8"))
    check("documented in the usage text", "channels-guilds" in channels_cli.USAGE)


# ===================================================================== A8

def test_server_route_is_strict():
    src = (REPO / "web" / "server.js").read_text(encoding="utf-8")
    m = re.search(r'app\.post\("/api/channels/servers/:platform/allowed-guilds".*?\n\}\);', src, flags=re.S)
    check("the POST route exists", bool(m))
    body = m.group(0) if m else ""
    check("it checks the platform", "CHANNEL_PLATFORMS.has(platform)" in body)
    check("the id must be digits (CHANNEL_SNOWFLAKE)", "CHANNEL_SNOWFLAKE.test(id)" in body)
    check("remove / confirm must be real booleans", 'typeof req.body[flag] !== "boolean"' in body)
    check("--yes is only ever sent for an add", 'req.body?.confirm === true && req.body?.remove !== true' in body)
    check("argv array, no shell string", 'runJarvisOnce(args, ' in body and "exec(" not in body)
    check("the id reaches the CLI only after validation", body.index("CHANNEL_SNOWFLAKE.test(id)") < body.index("channels-guilds"))
    check("a GET route exists too", 'app.get("/api/channels/servers/:platform/allowed-guilds"' in src)
    check("it does not shadow the older two-segment routes",
          src.index('"/api/channels/servers/:platform/allowed-guilds"') > 0
          and '"/api/channels/:platform/:set"' in src)


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
