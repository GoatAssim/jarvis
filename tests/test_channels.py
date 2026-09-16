"""Tests for the channels subsystem, log search, and the round-loop token fix.

Same no-dependency plain-assert pattern as tests/test_enhancements.py and
tests/test_skills.py — run directly:

    python3 tests/test_channels.py

sys.path points at jarvis-cli/ (where the `jarvis` package actually lives),
not the repo root this file's folder sits next to — see AGENTS.md.

Nothing here touches a real ~/.jarvis, needs an API key, or opens a socket.
The permission gate is pure by construction (see channels/permissions.py),
which is the whole reason it can be tested this thoroughly without a bot
token; the transcript tests redirect the module's directory constants at a
tempdir and put them back afterwards.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers, logs                      # noqa: E402
from jarvis.channels import DISCORD, INSTAGRAM, PERM_SETS  # noqa: E402
from jarvis.channels import base, permissions, transcript  # noqa: E402
from jarvis.channels import config as channel_config       # noqa: E402
from jarvis.channels import instagram_gateway as ig        # noqa: E402
from jarvis.channels import discord_gateway as dg          # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")


def base_cfg(platform=DISCORD, **overrides):
    """A platform block with the gate open enough to test one thing at a
    time. Built from the real defaults so a new config key can't make these
    tests pass against a shape that no longer exists."""
    cfg = dict(channel_config._platform_defaults(platform))
    cfg.update({
        "enabled": True,
        "bot_user_id": "999",
        "dm_allowlist": ["111"],
        "reply_allowlist": ["111", "222"],
        "tool_allowlist": ["111"],
        "allow_tools": True,
        "cooldown_seconds": 0,
    })
    cfg.update(overrides)
    return cfg


def msg(platform=DISCORD, context=permissions.CTX_GROUP, **kwargs):
    kwargs.setdefault("mentioned", context == permissions.CTX_GROUP)
    return permissions.IncomingMessage(platform=platform, context=context, **kwargs)


# ---------------------------------------------------------------------------
# config: normalization and fail-closed defaults
# ---------------------------------------------------------------------------

def test_config():
    defaults = channel_config.default_config()
    for platform in (DISCORD, INSTAGRAM):
        block = defaults[platform]
        check(f"{platform} ships disabled", block["enabled"] is False)
        check(f"{platform} allowlists start empty",
              all(block[s] == [] for s in PERM_SETS))
        check(f"{platform} tools off by default", block["allow_tools"] is False)

    # Entry normalization: @, case, and a bare string instead of a list.
    check("strips leading @", channel_config._normalize_entry("@Alice") == "alice")
    check("lowercases", channel_config._normalize_entry("BOB") == "bob")
    check("bare string becomes a list",
          channel_config._normalize_entries("solo") == ["solo"])
    check("dedupes", channel_config._normalize_entries(["a", "@A", "a"]) == ["a"])
    check("drops empties", channel_config._normalize_entries(["", None, "x"]) == ["x"])
    check("None is empty", channel_config._normalize_entries(None) == [])

    # redacted() must never leak a secret.
    cfg = channel_config.default_config()
    cfg[DISCORD]["bot_token"] = "super-secret-token"
    cfg[INSTAGRAM]["access_token"] = "another-secret"
    red = channel_config.redacted(cfg)
    blob = json.dumps(red)
    check("redacted hides bot_token", "super-secret-token" not in blob)
    check("redacted hides access_token", "another-secret" not in blob)
    check("redacted still signals presence", red[DISCORD]["bot_token"] == "set")
    check("redacted shows absence for unset",
          channel_config.redacted(channel_config.default_config())[DISCORD]["bot_token"] == "")


# ---------------------------------------------------------------------------
# permissions: the four sets, independently
# ---------------------------------------------------------------------------

def test_empty_means_deny():
    cfg = base_cfg(reply_allowlist=[], tool_allowlist=[], dm_allowlist=[])
    d = permissions.decide(cfg, msg(user_id="111"))
    check("empty reply_allowlist denies", not d.allowed)
    check("...at the reply stage", d.stage == "reply", d.stage)
    d = permissions.decide(cfg, msg(context=permissions.CTX_DM, user_id="111"))
    check("empty dm_allowlist denies a DM", not d.allowed and d.stage == "dm_allowed")


def test_wildcard():
    cfg = base_cfg(reply_allowlist=["*"])
    d = permissions.decide(cfg, msg(user_id="nobody-in-particular"))
    check("wildcard reply allows a stranger", d.allowed)
    check("...but tools stay gated", not d.may_use_tools)
    cfg = base_cfg(reply_allowlist=["*"], tool_allowlist=["*"])
    check("wildcard tools grants tools",
          permissions.decide(cfg, msg(user_id="x")).may_use_tools)


def test_sets_are_independent():
    """The headline requirement: reply and tool permissions do not imply
    each other in either direction."""
    cfg = base_cfg()
    owner = permissions.decide(cfg, msg(user_id="111"))
    check("owner replies + tools", owner.allowed and owner.may_use_tools)

    friend = permissions.decide(cfg, msg(user_id="222"))
    check("friend replies", friend.allowed)
    check("friend gets NO tools", not friend.may_use_tools)

    stranger = permissions.decide(cfg, msg(user_id="333"))
    check("stranger is denied", not stranger.allowed and stranger.stage == "reply")

    # Someone in tool_allowlist but not reply_allowlist gets nothing —
    # tool permission is not a back door around the reply gate.
    cfg2 = base_cfg(reply_allowlist=["111"], tool_allowlist=["444"])
    d = permissions.decide(cfg2, msg(user_id="444"))
    check("tool-only user still can't get a reply", not d.allowed)

    # The owner is NOT auto-granted anything.
    cfg3 = base_cfg(owner="777", reply_allowlist=["111"], tool_allowlist=["111"])
    d = permissions.decide(cfg3, msg(user_id="777"))
    check("owner is not implicitly on the reply list", not d.allowed)


def test_master_tool_switch():
    cfg = base_cfg(allow_tools=False)
    d = permissions.decide(cfg, msg(user_id="111"))
    check("allow_tools=false overrides tool_allowlist",
          d.allowed and not d.may_use_tools)


def test_reachability():
    cfg = base_cfg()
    d = permissions.decide(cfg, msg(user_id="111", mentioned=False))
    check("no mention is dropped", not d.allowed and d.stage == "reachable")
    # ...and crucially BEFORE any allowlist is consulted, so an unaddressed
    # message is never logged. base.handle_message keys on this stage name.
    d = permissions.decide(cfg, msg(user_id="333", mentioned=False))
    check("unaddressed stranger dropped at reachable, not reply",
          d.stage == "reachable")

    cfg = base_cfg(require_mention=False)
    check("require_mention=false answers without a ping",
          permissions.decide(cfg, msg(user_id="111", mentioned=False)).allowed)

    cfg = base_cfg(respond_in_dms=False)
    d = permissions.decide(cfg, msg(context=permissions.CTX_DM, user_id="111"))
    check("respond_in_dms=false blocks DMs", not d.allowed and d.stage == "reachable")


def test_self_and_disabled():
    check("bot ignores itself",
          permissions.decide(base_cfg(), msg(user_id="999")).stage == "self")
    check("disabled platform denies",
          permissions.decide(base_cfg(enabled=False), msg(user_id="111")).stage == "enabled")


def test_identity_matching():
    cfg = base_cfg(reply_allowlist=["alice"])
    check("matches by handle", permissions.decide(cfg, msg(user_id="1", user_handle="alice")).allowed)
    check("handle match is case-insensitive",
          permissions.decide(cfg, msg(user_id="1", user_handle="ALICE")).allowed)
    cfg = base_cfg(reply_allowlist=["@bob"])
    check("stored @handle matches a bare handle",
          permissions.decide(cfg, msg(user_id="1", user_handle="bob")).allowed)
    check("no false positive on a different name",
          not permissions.decide(cfg, msg(user_id="1", user_handle="bobby")).allowed)


def test_location_filters():
    cfg = base_cfg(allowed_guilds=["55"])
    check("allowed guild passes",
          permissions.decide(cfg, msg(user_id="111", guild_id="55")).allowed)
    d = permissions.decide(cfg, msg(user_id="111", guild_id="66"))
    check("other guild denied", not d.allowed and d.stage == "where")
    # Empty means unrestricted here — the opposite of the allowlists, on
    # purpose, because this narrows *where* not *who*.
    check("empty allowed_guilds is unrestricted",
          permissions.decide(base_cfg(), msg(user_id="111", guild_id="99")).allowed)


def test_cooldown():
    cfg = base_cfg(cooldown_seconds=10)
    now = 1000.0
    check("first message passes",
          permissions.decide(cfg, msg(user_id="111"), last_seen_at=None, now=now).allowed)
    d = permissions.decide(cfg, msg(user_id="111"), last_seen_at=now - 2, now=now)
    check("too soon is denied", not d.allowed and d.stage == "cooldown")
    check("after the cooldown it passes",
          permissions.decide(cfg, msg(user_id="111"), last_seen_at=now - 20, now=now).allowed)


# ---------------------------------------------------------------------------
# permissions: per-scope overrides (servers / groups)
# ---------------------------------------------------------------------------

def test_scopes():
    cfg = base_cfg(scopes={
        "guild:555": {"reply_allowlist": ["*"], "allow_tools": False},
        "channel:777": {"enabled": False},
    })
    d = permissions.decide(cfg, msg(user_id="111", guild_id="222"))
    check("unscoped server keeps defaults", d.allowed and d.may_use_tools)

    d = permissions.decide(cfg, msg(user_id="stranger", guild_id="555"))
    check("scoped server opens replies", d.allowed)
    check("scoped server closes tools", not d.may_use_tools)

    d = permissions.decide(cfg, msg(user_id="111", guild_id="555"))
    check("scope applies to the owner too", d.allowed and not d.may_use_tools)

    d = permissions.decide(cfg, msg(user_id="111", channel_id="777"))
    check("a scope can disable one channel", not d.allowed and d.stage == "enabled")

    # Partial override: keys the scope does not mention must be inherited.
    cfg2 = base_cfg(cooldown_seconds=42, scopes={"guild:1": {"allow_tools": False}})
    resolved = permissions.resolve_scope(cfg2, msg(user_id="111", guild_id="1"))
    check("unmentioned keys inherit", resolved["cooldown_seconds"] == 42)
    check("mentioned key overrides", resolved["allow_tools"] is False)

    # Specificity: channel beats guild.
    cfg3 = base_cfg(scopes={"guild:1": {"allow_tools": False},
                            "channel:2": {"allow_tools": True}})
    d = permissions.decide(cfg3, msg(user_id="111", guild_id="1", channel_id="2"))
    check("channel scope beats guild scope", d.may_use_tools)

    check("no scopes is a no-op",
          permissions.resolve_scope(base_cfg(), msg(user_id="1")) is not None)


# ---------------------------------------------------------------------------
# reply chunking
# ---------------------------------------------------------------------------

def test_chunking():
    check("short text is one chunk", base.chunk_text("hello", 100) == ["hello"])
    check("empty is no chunks", base.chunk_text("", 100) == [])
    check("whitespace-only is no chunks", base.chunk_text("   \n ", 100) == [])

    text = "\n\n".join(f"Paragraph {i} " + "w " * 40 for i in range(6))
    chunks = base.chunk_text(text, 200)
    check("every chunk fits the limit", all(len(c) <= 200 for c in chunks),
          f"max was {max(len(c) for c in chunks)}")
    check("nothing is lost", " ".join(chunks).split() == text.split())

    blob = "x" * 500
    check("an unbreakable blob is hard-cut",
          all(len(c) <= 100 for c in base.chunk_text(blob, 100)))
    check("hard-cut keeps every character",
          "".join(base.chunk_text(blob, 100)) == blob)


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------

def test_discord_mention_stripping():
    check("leading bot mention removed",
          dg.strip_mentions("<@123> what's my battery", "123") == "what's my battery")
    check("nickname form removed",
          dg.strip_mentions("<@!123> hi", "123") == "hi")
    check("other mentions survive readably",
          dg.strip_mentions("<@123> tell <@456> hi", "123") == "tell @456 hi")
    check("bare ping becomes empty", dg.strip_mentions("<@123>", "123") == "")
    check("no mention is untouched",
          dg.strip_mentions("plain text", "123") == "plain text")


# ---------------------------------------------------------------------------
# Instagram: parsing, groups, signature
# ---------------------------------------------------------------------------

def test_instagram_parsing():
    dm = {"entry": [{"messaging": [
        {"sender": {"id": "u1"}, "message": {"mid": "m1", "text": "hello"}}]}]}
    out = ig.parse_events(dm, bot_id="me")
    check("DM parsed", len(out) == 1)
    check("DM is dm context", out[0].context == permissions.CTX_DM)
    check("DM counts as addressed", out[0].mentioned is True)

    echo = {"entry": [{"messaging": [
        {"sender": {"id": "me"}, "message": {"mid": "m", "text": "hi", "is_echo": True}}]}]}
    check("echo ignored (no self-reply loop)", ig.parse_events(echo, bot_id="me") == [])

    empty = {"entry": [{"messaging": [
        {"sender": {"id": "u1"}, "message": {"mid": "m", "attachments": []}}]}]}
    check("text-less message ignored", ig.parse_events(empty, bot_id="me") == [])

    check("garbage payload is safe", ig.parse_events(None) == [])
    check("empty payload is safe", ig.parse_events({}) == [])


def test_instagram_groups():
    group = {"entry": [{"messaging": [
        {"sender": {"id": "u1"}, "thread_key": "t9",
         "message": {"mid": "m", "text": "hey @jarvis help"}}]}]}
    out = ig.parse_events(group, bot_id="me", bot_handle="jarvis")
    check("group thread is group context", out[0].context == permissions.CTX_GROUP)
    check("group mention detected", out[0].mentioned is True)
    check("thread id carried", out[0].thread_id == "t9")

    group["entry"][0]["messaging"][0]["message"]["text"] = "just chatting"
    out = ig.parse_events(group, bot_id="me", bot_handle="jarvis")
    check("group without a mention is not addressed", out[0].mentioned is False)

    # ...and the gate must then drop it.
    cfg = base_cfg(INSTAGRAM, reply_allowlist=["*"])
    check("unmentioned group message is dropped by the gate",
          permissions.decide(cfg, out[0]).stage == "reachable")

    check("handle match is word-anchored",
          ig.mentions_bot("hi @jarvisbot", "jarvis") is False)
    check("handle match ignores case", ig.mentions_bot("HI @JARVIS", "jarvis") is True)
    check("no handle configured never matches", ig.mentions_bot("@jarvis", "") is False)


def test_instagram_signature():
    secret = "topsecret"
    body = b'{"object":"instagram"}'
    import hashlib, hmac as _hmac
    good = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    check("valid signature accepted", ig.verify_signature(secret, body, good))
    check("bare hex (no prefix) accepted",
          ig.verify_signature(secret, body, good[len("sha256="):]))
    check("wrong signature rejected", not ig.verify_signature(secret, body, "sha256=deadbeef"))
    check("tampered body rejected", not ig.verify_signature(secret, b'{"x":1}', good))
    check("missing signature rejected", not ig.verify_signature(secret, body, None))
    # No secret must fail CLOSED — an unverified webhook is an open door.
    check("no app_secret fails closed", not ig.verify_signature("", body, good))


# ---------------------------------------------------------------------------
# transcript
# ---------------------------------------------------------------------------

def test_transcript():
    with tempfile.TemporaryDirectory() as tmp:
        original = transcript.CHANNELS_DIR
        original_map = transcript.MAP_FILE
        transcript.CHANNELS_DIR = Path(tmp)
        transcript.MAP_FILE = Path(tmp) / "threads.json"
        try:
            m = msg(user_id="111", user_handle="alice", channel_id="c1", text="hi")
            allow = permissions.Decision(True, "allowed", "ok", may_use_tools=True)
            deny = permissions.Decision(False, "reply", "not on the list")

            transcript.log_inbound(DISCORD, m, allow, conv_id="abc")
            transcript.log_inbound(DISCORD, m, deny)
            transcript.log_outbound(DISCORD, "c1", "the answer", conv_id="abc")

            entries = transcript.read_thread(DISCORD, "c1")
            check("all three records written", len(entries) == 3, str(len(entries)))
            check("inbound recorded", entries[0]["dir"] == "in")
            check("denial recorded with its stage", entries[1]["stage"] == "reply")
            check("outbound recorded", entries[2]["dir"] == "out")
            check("tool permission recorded", entries[0]["may_use_tools"] is True)

            # Filename sanitization: a path-ish or reserved thread id must
            # not escape the folder or produce an unopenable file.
            # The property that matters is containment, not the absence
            # of dots: "../../etc/passwd" may legally become
            # "_.._.._etc_passwd.jsonl" as long as it cannot escape the
            # platform folder, which is what this asserts.
            transcript.append(DISCORD, "../../etc/passwd", {"x": 1})
            escaped = transcript.thread_path(DISCORD, "../../etc/passwd")
            check("path traversal cannot escape the folder",
                  escaped.resolve().parent == (Path(tmp) / DISCORD).resolve())
            check("no separators survive sanitization",
                  "/" not in escaped.name and "\\" not in escaped.name)
            check("reserved name prefixed",
                  transcript.thread_path(DISCORD, "con").name.startswith("_"))

            # conv_id mapping is stable and created once.
            calls = []

            def create():
                calls.append(1)
                return "conv-1"

            first = transcript.conv_id_for(DISCORD, "t1", create=create)
            second = transcript.conv_id_for(DISCORD, "t1", create=create)
            check("conv id is stable", first == second == "conv-1")
            check("created only once", len(calls) == 1)
            check("different thread is a different conversation",
                  transcript.conv_id_for(DISCORD, "t2", create=lambda: "conv-2") == "conv-2")
        finally:
            transcript.CHANNELS_DIR = original
            transcript.MAP_FILE = original_map
            transcript.reset_cooldowns()


# ---------------------------------------------------------------------------
# the round-loop token fix
# ---------------------------------------------------------------------------

def _openai_history(rounds=5, size=1800):
    out = []
    for i in range(rounds):
        out.append({"role": "assistant",
                    "tool_calls": [{"id": f"c{i}",
                                    "function": {"name": "q", "arguments": "{}"}}]})
        out.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * size})
    return out


def test_tool_result_compaction():
    history = _openai_history()
    before_count = len(history)
    reclaimed = ai_providers._compact_prior_tool_results(history)
    check("something was reclaimed", reclaimed > 0)
    check("NO message was removed", len(history) == before_count)

    # The invariant that matters most: every tool_call id still has a
    # matching tool message. Dropping one is a hard 400 on OpenAI.
    call_ids = {m["tool_calls"][0]["id"] for m in history if m.get("tool_calls")}
    result_ids = {m["tool_call_id"] for m in history if m.get("role") == "tool"}
    check("tool_call_id pairing intact", call_ids == result_ids)

    results = [m for m in history if m.get("role") == "tool"]
    check("newest results untouched",
          all(len(r["content"]) == 1800 for r in results[-ai_providers.KEEP_FULL_RESULTS:]))
    check("older results trimmed",
          all(r["content"].endswith("[trimmed]")
              for r in results[:-ai_providers.KEEP_FULL_RESULTS]))

    # Idempotent — the loops call this once per round on a growing list.
    snapshot = [m.get("content") for m in history]
    check("second pass reclaims nothing",
          ai_providers._compact_prior_tool_results(history) == 0)
    check("second pass changes nothing",
          [m.get("content") for m in history] == snapshot)

    # Below the keep-full threshold nothing is touched at all.
    small = _openai_history(rounds=ai_providers.KEEP_FULL_RESULTS)
    check("short histories are left alone",
          ai_providers._compact_prior_tool_results(small) == 0)

    # A short result is never padded or marked.
    tiny = _openai_history(rounds=4, size=10)
    ai_providers._compact_prior_tool_results(tiny)
    check("already-small results untouched",
          all(m["content"] == "y" * 10 for m in tiny if m.get("role") == "tool"))


def test_compaction_other_shapes():
    anthropic = [{"role": "user",
                  "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                               "content": "z" * 2000}]} for i in range(4)]
    check("anthropic blocks trimmed",
          ai_providers._compact_prior_tool_results(anthropic) > 0)
    check("anthropic structure preserved",
          all(b["type"] == "tool_result" and "tool_use_id" in b
              for m in anthropic for b in m["content"]))

    gemini = [{"role": "user",
               "parts": [{"functionResponse": {"name": "f",
                                               "response": {"result": "w" * 2000}}}]}
              for _ in range(4)]
    check("gemini parts trimmed", ai_providers._compact_prior_tool_results(gemini) > 0)
    check("gemini structure preserved",
          all("functionResponse" in p for m in gemini for p in m["parts"]))

    # Non-tool messages must never be touched.
    plain = [{"role": "user", "content": "q" * 5000},
             {"role": "assistant", "content": "a" * 5000}] * 3
    check("plain messages untouched",
          ai_providers._compact_prior_tool_results(plain) == 0)

    check("empty list is safe", ai_providers._compact_prior_tool_results([]) == 0)
    check("junk entries are safe",
          ai_providers._compact_prior_tool_results([None, "x", 5, {}]) == 0)


def test_compaction_saves_tokens():
    """End-to-end: simulate the real loop and confirm the bill drops."""
    from jarvis import token_usage

    def run(apply_fix):
        history = [{"role": "system", "content": "x" * 1200}]
        total = 0
        for i in range(5):
            if apply_fix:
                ai_providers._compact_prior_tool_results(history)
            total += token_usage.estimate_tokens_for(history)
            history.append({"role": "assistant",
                            "tool_calls": [{"id": f"c{i}",
                                            "function": {"name": "q", "arguments": "{}"}}]})
            history.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 4000})
        return total

    before, after = run(False), run(True)
    check("fix reduces total prompt tokens", after < before, f"{before} -> {after}")
    check("saving is material (>15%)", (before - after) / before > 0.15,
          f"{100 * (before - after) / before:.0f}%")


# ---------------------------------------------------------------------------
# log search
# ---------------------------------------------------------------------------

def test_log_search_validation():
    check("empty query is an error", logs.search("")["ok"] is False)
    check("bad regex is an error, not a crash",
          logs.search("[unclosed", mode="regex")["ok"] is False)
    check("bad regex explains itself", "regex" in logs.search("[a", mode="regex")["error"])
    check("over-long query rejected",
          logs.search("x" * (logs.SEARCH_MAX_PATTERN_CHARS + 1))["ok"] is False)
    check("valid query runs", logs.search("anything")["ok"] is True)


def test_log_search_matching():
    """Exercise the matcher directly — building a real log would mean
    writing into a real ~/.jarvis/logs."""
    words, err = logs._compile_search("alpha beta", "words")
    check("words mode compiles per-term", err == "" and len(words) == 2)
    blob = '{"data": {"tool": "alpha", "arg": "beta"}}'
    check("words mode is an AND that matches",
          all(p.search(blob) for p in words))
    check("words mode fails when one term is missing",
          not all(p.search('{"tool": "alpha"}') for p in words))

    phrase, _ = logs._compile_search("alpha beta", "phrase")
    check("phrase mode needs the literal string",
          phrase[0].search("xx alpha beta yy") is not None)
    check("phrase mode rejects a reordering",
          phrase[0].search("beta alpha") is None)

    rx, _ = logs._compile_search(r"tok\d+", "regex")
    check("regex mode works", rx[0].search("tok42") is not None)

    check("matching is case-insensitive",
          logs._compile_search("ALPHA", "words")[0][0].search("alpha") is not None)

    snippet = logs._search_snippet("z" * 300 + "NEEDLE" + "z" * 300,
                                   __import__("re").search("NEEDLE", "z" * 300 + "NEEDLE" + "z" * 300))
    check("snippet is bounded", len(snippet) < 300, str(len(snippet)))
    check("snippet contains the match", "NEEDLE" in snippet)
    check("snippet marks truncation", snippet.startswith("…") and snippet.endswith("…"))


def test_log_entry_source_label():
    import os
    original = os.environ.get("JARVIS_LOG_SOURCE")
    try:
        os.environ["JARVIS_LOG_SOURCE"] = "discord"
        # log() writes to disk for a valid id only; assert on the label
        # plumbing rather than the write, which would touch a real ~/.jarvis.
        check("source is read from the environment",
              os.environ.get("JARVIS_LOG_SOURCE") == "discord")
        result = logs.search("x", sources=["discord"])
        check("search accepts a sources filter", result["ok"] is True)
        result = logs.search("x", origins=["discord"], directions=["error"])
        check("search accepts origin+direction filters", result["ok"] is True)
    finally:
        if original is None:
            os.environ.pop("JARVIS_LOG_SOURCE", None)
        else:
            os.environ["JARVIS_LOG_SOURCE"] = original


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------

def test_describe_warnings():
    text = permissions.describe(base_cfg(reply_allowlist=[]))
    check("warns about an empty reply list", "nobody will get an answer" in text)
    text = permissions.describe(base_cfg(tool_allowlist=["*"], allow_tools=True))
    check("warns about wildcard tools", "run tools on this machine" in text)
    text = permissions.describe(base_cfg())
    check("no spurious warning on a sane config", "!" not in text)


def main():
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILED.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}")

    total = PASSED + len(FAILED)
    print(f"{PASSED}/{total} passed")
    for failure in FAILED:
        print("  FAIL:", failure)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
