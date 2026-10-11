"""Tests for the L.22 items that were still open: the post-check on `hard`
instructions, conflict detection between rules, and the rules panel's route
and wiring.

    python3 tests/test_l22_hard_conflicts.py

No model, no network, no real ~/.jarvis: HOME is redirected to a temp dir BEFORE
any jarvis module is imported (AGENTS.md). The two model calls the feature can
make go through channels/cheap_call.py and are replaced by stubs. Topics are
random where the point is "nothing is hard-coded".

  H1  the hard flag            person_memory.add_instruction(hard=...)
  H2  the post-check           channels/rule_check.py
  H3  end to end through cheap_call (the rewrite never sees the leaking draft)
  H4  wiring in base.py
  C1  conflict detection       person_memory.find_conflicts
  C2  the owner tool + CLI     person_instruct / `channels-memory rule|rules`
  S1  server.js rules routes   (argv built by channelRuleArgs, run under node)
  S2  the panel is wired
"""

import contextlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l22-hard-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "jarvis-cli"))

from jarvis import channels_cli                                          # noqa: E402
from jarvis.actions import person_memory_tools as tools                  # noqa: E402
from jarvis.channels import (DISCORD, cheap_call, people, person_memory,  # noqa: E402
                             rule_check)
from jarvis.channels import config as channel_config                     # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


RNG = random.Random(20261011)


def word():
    letters, vowels = "bcdfghjlmnpqrtvwxy", "aeiou"
    return "".join(RNG.choice(letters) + RNG.choice(vowels) for _ in range(3)) + RNG.choice("kz")


def reset():
    shutil.rmtree(person_memory.PEOPLE_DIR, ignore_errors=True)
    try:
        person_memory.APPLIED_LOG.unlink()
    except OSError:
        pass
    for f in (people.PEOPLE_FILE, channel_config.CONFIG_FILE):
        for suffix in ("", ".bak", ".tmp"):
            try:
                Path(str(f) + suffix).unlink()
            except OSError:
                pass
    cfg = channel_config.load_config()
    cfg[DISCORD].update({"enabled": True, "owner": "100"})
    channel_config.save_config(cfg)


def set_created(person, rid, ts):
    """Give a record a known creation time: two rules made back to back can
    share one clock tick on some systems, and 'newest wins' needs a newest."""
    records = person_memory._read(person)
    for rec in records:
        if rec["id"] == rid:
            rec["created"] = ts
    person_memory._write(person, records)


def run_cli(*args):
    buf, code = io.StringIO(), 0
    with contextlib.redirect_stdout(buf):
        try:
            channels_cli._channels_memory(list(args))
        except SystemExit as exc:
            code = exc.code or 0
    out = buf.getvalue().strip()
    try:
        return code, json.loads(out) if out else None
    except ValueError:
        return code, None


A, B = "discord:555", "discord:556"
NOW = time.time()

# ======================================================================= H1
reset()
soft, err = person_memory.add_instruction(A, "surprise party", "don_t_discuss")
hard, err2 = person_memory.add_instruction(A, "surprise party", "don_t_discuss", hard=True)
check("H1 a rule is soft unless asked", soft and not err and soft["instruction"]["hard"] is False)
check("H1 a hard rule is stored as hard", hard and not err2 and hard["instruction"]["hard"] is True)
check("H1 a hard rule does not collapse into its soft twin", soft["id"] != hard["id"]
      and len(person_memory.instructions(A)) == 2)
check("H1 ask_owner_first can be hard",
      person_memory.add_instruction(A, "the lease", "ask_owner_first", hard=True)[0] is not None)
for directive in ("tone", "custom", "notify_owner", "only_discuss"):
    rec, problem = person_memory.add_instruction(A, "x" + word(), directive, text="be calm", hard=True)
    check(f"H1 {directive} cannot be hard", rec is None and "hard" in problem, problem)
check("H1 hard is allowed for anyone too",
      person_memory.add_instruction("anyone", word() + " " + word(), "don_t_discuss", hard=True)[0] is not None)

# ======================================================================= H2
reset()
w1, w2 = word(), word()
rule_h, _ = person_memory.add_instruction(A, f"{w1} {w2}", "don_t_discuss", hard=True)
rule_s, _ = person_memory.add_instruction(A, f"{word()} {word()}", "don_t_discuss")
matched_h = [(rule_h, "lexical")]
check("H2 hard_rules picks the hard ones", rule_check.hard_rules([(rule_h, "lexical"), (rule_s, "lexical")]) == [rule_h])
check("H2 junk in the matched list is ignored", rule_check.hard_rules([("x", "y"), (None, "z")]) == [])

asked, rewrote = [], []


def ask(verdict):
    def fn(topic, draft):
        asked.append((topic, draft))
        return verdict
    return fn


def rewrite(reply):
    def fn(topics, message, check_back):
        rewrote.append((topics, message, check_back))
        if isinstance(reply, Exception):
            raise reply
        return reply
    return fn


def fresh():
    del asked[:]
    del rewrote[:]


fresh()
out = rule_check.apply(A, [(rule_s, "lexical")], "anything", "msg", ask=ask(True), rewrite=rewrite("x"))
check("H2 a soft rule is never post-checked", out["outcome"] == "none" and not out["changed"] and not asked)
fresh()
out = rule_check.apply(A, matched_h, "Sure, how was your weekend?", "msg", ask=ask(False), rewrite=rewrite("x"))
check("H2 a clean draft goes out unchanged, with no rewrite call",
      out["outcome"] == "clean" and out["text"] == "Sure, how was your weekend?" and not out["changed"] and not rewrote)
check("H2 the model was asked about the rule's topic", asked and asked[0][0] == f"{w1} {w2}")

fresh()
good = "Let's talk about something else - how was your weekend?"
out = rule_check.apply(A, matched_h, f"It's about {w1} {w2}, they said", "msg", ask=ask(True), rewrite=rewrite(good))
check("H2 a leaking draft is replaced by the model's deflection",
      out["outcome"] == "rewritten" and out["text"] == good and out["changed"] and out["rules"] == [rule_h["id"]], str(out))
check("H2 the rewrite is told to steer away, not to check back", rewrote and rewrote[0][2] is False)
check("H2 the rewrite is given the topic and the message, not the draft",
      rewrote[0][0] == [f"{w1} {w2}"] and rewrote[0][1] == "msg")

for label, bad in (("repeats a topic word", f"I can't talk about {w1} though."),
                   ("explains itself", "The owner told me not to say."),
                   ("mentions a rule", "That's against the rules."),
                   ("is empty", "   "),
                   ("is far too long", "word " * 200),
                   ("is not text", None)):
    fresh()
    out = rule_check.apply(A, matched_h, f"{w1} {w2} is happening", "msg", ask=ask(True), rewrite=rewrite(bad))
    check(f"H2 a deflection that {label} is refused -> fixed sentence",
          out["outcome"] == "fallback" and out["text"] == rule_check.FALLBACK["don_t_discuss"], str(out))
fresh()
out = rule_check.apply(A, matched_h, f"{w1} {w2}", "msg", ask=ask(True), rewrite=rewrite(RuntimeError("down")))
check("H2 a failing rewrite call -> fixed sentence", out["outcome"] == "fallback")

# the model gives no answer about the draft
fresh()
out = rule_check.apply(A, matched_h, f"it is {w1} indeed", "msg", ask=ask(None), rewrite=rewrite(good))
check("H2 no answer + a topic word in the draft = leak", out["outcome"] == "rewritten")
fresh()
out = rule_check.apply(A, matched_h, "Sure, how was your weekend?", "msg", ask=ask(None), rewrite=rewrite(good))
check("H2 no answer + no topic word = left alone", out["outcome"] == "clean" and out["text"] == "Sure, how was your weekend?")


def boom(topic, draft):
    raise RuntimeError("provider exploded")


fresh()
out = rule_check.apply(A, matched_h, f"it is {w2} indeed", "msg", ask=boom, rewrite=rewrite(good))
check("H2 a raising check counts as 'no answer'", out["outcome"] == "rewritten")

# a long draft: the model sees the first part, the tail is scanned lexically
fresh()
long_draft = "blah " * 700 + f"and the {w1} {w2} is on friday"
check("H2 the fixture draft really is longer than the clip", len(long_draft) > rule_check.DRAFT_CLIP)
out = rule_check.apply(A, matched_h, long_draft, "msg", ask=ask(False), rewrite=rewrite(good))
check("H2 a leak beyond what the model was shown is still caught", out["outcome"] == "rewritten", str(out["outcome"]))
check("H2 the model was shown at most the clip", len(asked[0][1]) <= rule_check.DRAFT_CLIP)

# ask_owner_first
reset()
w3 = word()
rule_a, _ = person_memory.add_instruction(A, f"{w3} lease", "ask_owner_first", hard=True)
fresh()
out = rule_check.apply(A, [(rule_a, "lexical")], f"the {w3} lease ends in june", "msg", ask=ask(True), rewrite=rewrite(None))
check("H2 ask_owner_first falls back to 'I'll check and get back to you'",
      out["text"] == rule_check.FALLBACK["ask_owner_first"] and rewrote[0][2] is True, str(out))

# two hard rules leaking at once
reset()
r1, _ = person_memory.add_instruction(A, f"{w1} {w2}", "don_t_discuss", hard=True)
r2, _ = person_memory.add_instruction(A, f"{w3} lease", "ask_owner_first", hard=True)
fresh()
out = rule_check.apply(A, [(r1, "lexical"), (r2, "lexical")], f"{w1} {w2} and {w3} lease", "msg",
                       ask=ask(True), rewrite=rewrite(f"A deflection mentioning {w3}."))
check("H2 every leaking rule's words are barred from the deflection",
      out["outcome"] == "fallback" and out["text"] == rule_check.FALLBACK["ask_owner_first"], str(out))
check("H2 both topics reach the rewrite call", len(rewrote[0][0]) == 2 and rewrote[0][2] is True)
check("H2 both leaking rules are reported", set(out["rules"]) == {r1["id"], r2["id"]})

# the model is asked about at most MAX_MODEL_CHECKS rules
reset()
many = [person_memory.add_instruction(A, f"{word()} {word()}", "don_t_discuss", hard=True)[0] for _ in range(5)]
fresh()
out = rule_check.apply(A, [(r, "lexical") for r in many], "a perfectly ordinary reply", "msg",
                       ask=ask(False), rewrite=rewrite("x"))
check("H2 at most MAX_MODEL_CHECKS model calls per reply",
      len(asked) == rule_check.MAX_MODEL_CHECKS and out["outcome"] == "clean", str(len(asked)))

# never raises; a hard rule fails toward withholding
reset()
r1, _ = person_memory.add_instruction(A, f"{w1} {w2}", "don_t_discuss", hard=True)
out = rule_check.apply(A, [(r1, "lexical")], None, "msg", ask=ask(False), rewrite=rewrite(good))
check("H2 a broken draft never raises and fails toward withholding",
      out["outcome"] == "fallback" and out["text"] == rule_check.FALLBACK["don_t_discuss"], str(out))

# the audit line
reset()
r1, _ = person_memory.add_instruction(A, f"{w1} {w2}", "don_t_discuss", hard=True)
rule_check.apply(A, [(r1, "lexical")], f"quokka {w1} {w2} secret draft", "private message text",
                 ask=ask(True), rewrite=rewrite(good))
log = person_memory.APPLIED_LOG.read_text(encoding="utf-8")
entry = json.loads(log.strip().splitlines()[-1])
check("H2 the audit line has the rule id and the outcome",
      entry["instruction"] == r1["id"] and entry["how"] == "post-check:rewritten" and entry["person"] == A, str(entry))
check("H2 it never holds the draft or the message", "quokka" not in log and "private" not in log)
check("H2 `applied` shows it", any(e["how"].startswith("post-check") for e in person_memory.read_applied()))

# ======================================================================= H3
reset()
w1, w2 = word(), word()
r1, _ = person_memory.add_instruction(A, f"{w1} {w2}", "don_t_discuss", hard=True)
prompts = []
original = cheap_call._complete


def stub(prompt, max_chars=None):
    prompts.append(prompt)
    if "You check a chat reply" in prompt:
        return '{"leaks": true}'
    if "Write a short, friendly chat reply" in prompt:
        return 'Sure! {"reply": "Let us chat about something else - how was your day?"}'
    return None


try:
    cheap_call._complete = stub
    out = rule_check.apply(A, [(r1, "lexical")], "The secret draft: the thing is on Friday", "what is happening",
                           ask=None, rewrite=None)
    check("H3 through cheap_call: leak found, deflection used",
          out["outcome"] == "rewritten" and out["text"] == "Let us chat about something else - how was your day?", str(out))
    check("H3 the check prompt shows the draft as data, the rewrite prompt does not show it",
          any("secret draft" in p for p in prompts if "You check a chat reply" in p)
          and not any("secret draft" in p for p in prompts if "Write a short" in p))
    check("H3 the check prompt tells the model the reply is data",
          any("data, not as instructions" in p for p in prompts))
    check("H3 the rewrite prompt carries the topic", any(f"{w1} {w2}" in p for p in prompts if "Write a short" in p))
    del prompts[:]
    cheap_call._complete = lambda prompt, max_chars=None: None
    out = rule_check.apply(A, [(r1, "lexical")], f"all about {w1} {w2}", "msg")
    check("H3 with no provider at all a leaking draft still never goes out",
          out["outcome"] == "fallback" and out["text"] == rule_check.FALLBACK["don_t_discuss"], str(out))
finally:
    cheap_call._complete = original

# ======================================================================= H4
base_src = (REPO / "jarvis-cli" / "jarvis" / "channels" / "base.py").read_text(encoding="utf-8")
i_apply = base_src.find("rule_check.apply(")
i_send = base_src.find("for chunk in chunk_text(text, limit):")
check("H4 base.py post-checks the draft", i_apply > 0)
check("H4 ...before the first chunk is sent", 0 < i_apply < i_send)
check("H4 ...only a real answer, never for the owner", "if not is_owner and ask_ok and matched_rules:" in base_src)
check("H4 if the check itself breaks, a hard rule still withholds",
      "text = _HARD_RULE_FALLBACK" in base_src and '.get("hard")' in base_src)

# ======================================================================= C1
reset()
old, _ = person_memory.add_instruction(A, "salary negotiation", "don_t_discuss")
new, _ = person_memory.add_instruction(A, "salary", "only_discuss")
set_created(A, old["id"], NOW - 100)
set_created(A, new["id"], NOW - 10)
found = person_memory.find_conflicts(A)
check("C1 'don't discuss X' vs 'only discuss X' is a contradiction",
      len(found) == 1 and found[0]["kind"] == "contradiction", str(found))
check("C1 the newer rule wins, as matches() will have it",
      found and found[0]["winner"] == new["id"] and found[0]["loser"] == old["id"] and found[0]["cross"] is False)
check("C1 the texts are there for the owner to read",
      found and found[0]["winner_text"] and found[0]["loser_text"] and found[0]["reason"])
got = person_memory.matches(A, "tell me about the salary negotiation", classify=lambda t, m: False)
check("C1 matches() puts the same rule first", got and got[0][0]["id"] == found[0]["winner"], str([g[0]["id"] for g in got]))

other, _ = person_memory.add_instruction(A, "garden hose", "don_t_discuss")
check("C1 an unrelated rule conflicts with nothing", person_memory.conflicts_involving(A, other["id"]) == [])
check("C1 conflicts_involving finds the rule's own conflicts",
      len(person_memory.conflicts_involving(A, new["id"])) == 1)

reset()
mine, _ = person_memory.add_instruction(A, "salary negotiation", "don_t_discuss")
everyone, _ = person_memory.add_instruction("anyone", "salary negotiation", "don_t_discuss")
found = person_memory.find_conflicts(A)
check("C1 the same rule for the person and for everyone is a duplicate",
      len(found) == 1 and found[0]["kind"] == "duplicate" and found[0]["cross"] is True, str(found))
check("C1 the person's own rule wins over the one for everyone", found and found[0]["winner"] == mine["id"])
check("C1 another person sees only the rule for everyone, so no conflict", person_memory.find_conflicts(B) == [])
check("C1 conflicts among 'anyone' rules only are found for anyone",
      person_memory.find_conflicts("anyone") == [])

reset()
a1, _ = person_memory.add_instruction(A, "the lease", "don_t_discuss")
a2, _ = person_memory.add_instruction(A, "the lease", "ask_owner_first")
a3, _ = person_memory.add_instruction(A, "the lease", "notify_owner")
kinds = {(c["kind"]) for c in person_memory.find_conflicts(A)}
check("C1 refusing vs checking first is an overlap", "overlap" in kinds, str(kinds))
check("C1 withholding vs 'answer normally' is a contradiction",
      any(c["kind"] == "contradiction" and a3["id"] in (c["winner"], c["loser"])
          for c in person_memory.find_conflicts(A)))

reset()
person_memory.add_instruction(A, "", "tone", text="be formal")
person_memory.add_instruction(A, "", "tone", text="be casual")
person_memory.add_instruction(A, "", "custom", text="call them captain")
person_memory.add_instruction(A, "", "custom", text="sign off with a pun")
found = person_memory.find_conflicts(A)
check("C1 two tone rules may pull apart (an overlap, not a guess about meaning)",
      len(found) == 1 and found[0]["kind"] == "overlap", str(found))
check("C1 free-text custom rules are never compared", all("custom" not in str(c["reason"]) for c in found))

for trial in range(5):
    reset()
    topic = f"{word()} {word()}"
    x, _ = person_memory.add_instruction(A, topic, "don_t_discuss")
    y, _ = person_memory.add_instruction(A, topic, "only_discuss")
    found = person_memory.find_conflicts(A)
    check(f"C1 [{trial}] a random topic, said twice, conflicts exactly once",
          len(found) == 1 and found[0]["kind"] == "contradiction", str(found))

reset()
ended, _ = person_memory.add_instruction(A, "salary negotiation", "don_t_discuss")
person_memory.add_instruction(A, "salary", "only_discuss")
person_memory.set_status(A, ended["id"], "retired")
check("C1 a retired rule no longer conflicts", person_memory.find_conflicts(A) == [])
expired, _ = person_memory.add_instruction(A, "salary negotiation", "don_t_discuss", until=time.time() - 5)
check("C1 an expired rule no longer conflicts", person_memory.find_conflicts(A) == [])

# ======================================================================= C2
reset()
people.touch(DISCORD, "555", handle="robin")
people.set_name(DISCORD, "555", "Robin", manual=True)
os.environ.pop(tools.SENDER_ENV, None)
os.environ.pop("JARVIS_SCHEDULED", None)
os.environ.pop("JARVIS_CONTEXT", None)

first = tools.tool_person_instruct({"person": "Robin", "directive": "only_discuss", "topic": "salary"})
check("C2 the tool makes a rule", first.get("ok") is True and first["hard"] is False and "conflicts" not in first, str(first))
set_created(A, first["id"], NOW - 100)
second = tools.tool_person_instruct({"person": "Robin", "directive": "don_t_discuss",
                                     "topic": "salary negotiation", "hard": True})
check("C2 the tool can make a hard rule", second.get("ok") is True and second["hard"] is True, str(second))
check("C2 the tool says when a new rule contradicts an old one, and that it wins",
      second.get("conflicts") and second["conflicts"][0]["kind"] == "contradiction"
      and second["conflicts"][0]["with"] == first["id"] and second["conflicts"][0]["wins"] is True, str(second))
bad = tools.tool_person_instruct({"person": "Robin", "directive": "tone", "text": "be calm", "hard": True})
check("C2 a hard tone rule is refused", bad.get("ok") is False and "hard" in bad["error"], str(bad))
listing = tools.tool_person_recall({"person": "Robin"})
check("C2 person_recall marks the hard rule", any(i.get("hard") for i in listing["items"]), str(listing))
schema = [s for s in tools.TOOL_SCHEMAS if s["name"] == "person_instruct"][0]
check("C2 the schema offers `hard`", schema["parameters"]["properties"]["hard"]["type"] == "boolean")

reset()
people.touch(DISCORD, "555", handle="robin")
code, out = run_cli("discord", "555", "rule", "don_t_discuss", "--hard", "--days", "7", "salary", "negotiation")
rec = out and out.get("record")
check("C2 CLI: a hard rule with an end date", code == 0 and rec and rec["instruction"]["hard"] is True, str(out))
until = rec["instruction"]["scope"]["until"] if rec else 0
check("C2 CLI: --days 7 ends it in about a week", abs(until - (time.time() + 7 * 86400)) < 120, str(until))
check("C2 CLI: the topic is the words after the flags", rec and rec["instruction"]["topic"] == "salary negotiation")
check("C2 CLI: the result carries a (here empty) conflicts list", out and out.get("conflicts") == [])
code, out = run_cli("discord", "555", "rule", "only_discuss", "salary")
check("C2 CLI: a contradicting rule reports the conflict", code == 0 and out["conflicts"] and out["conflicts"][0]["kind"] == "contradiction", str(out))
for args, why in ((("tone", "--hard", "be", "brief"), "hard on a tone rule"),
                  (("don_t_discuss", "--days", "abc", "topic", "x"), "a non-numeric --days"),
                  (("don_t_discuss", "--days", "0", "topic", "x"), "--days 0"),
                  (("don_t_discuss", "--nope", "topic", "x"), "an unknown option")):
    code, out = run_cli("discord", "555", "rule", *args)
    check(f"C2 CLI refuses {why}", code == 1 and out and out["ok"] is False, str(out))

code, out = run_cli("discord", "555", "rule", "tone", "keep", "answers", "to", "two", "lines")
rec = out["record"]
check("C2 CLI: a tone rule is wording only (no topic to cap)",
      rec["instruction"]["topic"] == "" and "keep answers to two lines" in rec["text"], str(rec))
check("C2 ...and it applies to every message",
      any(r["id"] == rec["id"] for r, _how in person_memory.matches(A, "anything at all", classify=lambda t, m: False)))
long_text = "x" * 230
code, out = run_cli("discord", "555", "rule", "custom", long_text)
check("C2 CLI: a long custom rule is kept whole", out["record"]["text"].endswith(long_text), out["record"]["text"][-40:])

run_cli("anyone", "rule", "don_t_discuss", "salary", "negotiation")
code, out = run_cli("discord", "555", "rules")
check("C2 CLI rules: own rules, the ones for everyone, and conflicts",
      code == 0 and out["items"] and len(out["everyone"]) == 1 and isinstance(out["conflicts"], list), str(out.keys()))
code, out = run_cli("anyone", "rules")
check("C2 CLI anyone rules: no 'everyone' key", code == 0 and "everyone" not in out and len(out["items"]) == 1)
rid = out["items"][0]["id"]
code, out = run_cli("anyone", "retire", rid)
check("C2 CLI: a rule for everyone can be ended", code == 0 and run_cli("anyone", "rules")[1]["items"] == [])

# ======================================================================= S1
server = (REPO / "web" / "server.js").read_text(encoding="utf-8")
for label, needle in (("GET person rules", 'app.get("/api/channels/people/:platform/:id/rules"'),
                      ("POST person rules", 'app.post("/api/channels/people/:platform/:id/rules"'),
                      ("GET anyone rules", 'app.get("/api/channels/rules/anyone"'),
                      ("POST anyone rules", 'app.post("/api/channels/rules/anyone"')):
    check(f"S1 route exists: {label}", needle in server)
for label, needle in (("directives are whitelisted", "CHANNEL_RULE_DIRECTIVES.has(directive)"),
                      ("hard is whitelisted per directive", "CHANNEL_RULE_HARD.has(directive)"),
                      ("hard must be a real boolean", 'typeof body.hard !== "boolean"'),
                      ("days must be a whole number in range", "Number.isInteger(body.days)"),
                      ("a record id is 8 hex characters", "CHANNEL_MEMORY_ID.test(record)"),
                      ("no shell string anywhere in the rules routes", "exec(")):
    if label.startswith("no shell"):
        seg = server[server.index("function channelRuleArgs"):server.index('app.get("/api/channels/people/:platform/:id/usage"')]
        check("S1 " + label, needle not in seg)
    else:
        check("S1 " + label, needle in server)

m_id = re.search(r"const CHANNEL_MEMORY_ID = [^\n]+\n", server)
m_fn = re.search(r"const CHANNEL_RULE_DIRECTIVES.*?\nfunction channelRuleArgs\(.*?\n\}\n", server, re.S)
check("S1 the validator can be extracted for a node run", bool(m_id and m_fn))
node = shutil.which("node")
if m_id and m_fn and node:
    js = m_id.group(0) + m_fn.group(0) + r'''
const P = ["channels-memory", "discord", "555"];
const long = (n) => "x".repeat(n);
const cases = {
  hard: channelRuleArgs(P, {action: "add", directive: "don_t_discuss", topic: " surprise party ", hard: true, days: 7}),
  plain: channelRuleArgs(P, {action: "add", directive: "ask_owner_first", topic: "the lease"}),
  toneHard: channelRuleArgs(P, {action: "add", directive: "tone", topic: "be calm", hard: true}),
  dash: channelRuleArgs(P, {action: "add", directive: "don_t_discuss", topic: "-x"}),
  long: channelRuleArgs(P, {action: "add", directive: "don_t_discuss", topic: long(121)}),
  toneLong: channelRuleArgs(P, {action: "add", directive: "tone", topic: long(240)}),
  toneTooLong: channelRuleArgs(P, {action: "add", directive: "tone", topic: long(241)}),
  control: channelRuleArgs(P, {action: "add", directive: "custom", topic: "a\nb"}),
  empty: channelRuleArgs(P, {action: "add", directive: "custom", topic: "   "}),
  days0: channelRuleArgs(P, {action: "add", directive: "don_t_discuss", topic: "t", days: 0}),
  days366: channelRuleArgs(P, {action: "add", directive: "don_t_discuss", topic: "t", days: 366}),
  daysFrac: channelRuleArgs(P, {action: "add", directive: "don_t_discuss", topic: "t", days: 1.5}),
  daysStr: channelRuleArgs(P, {action: "add", directive: "don_t_discuss", topic: "t", days: "7"}),
  hardStr: channelRuleArgs(P, {action: "add", directive: "don_t_discuss", topic: "t", hard: "yes"}),
  badDirective: channelRuleArgs(P, {action: "add", directive: "obey", topic: "t"}),
  badAction: channelRuleArgs(P, {action: "delete", record: "abcdef12"}),
  retire: channelRuleArgs(P, {action: "retire", record: "abcdef12"}),
  badRecord: channelRuleArgs(P, {action: "forget", record: "xyz"}),
  noBody: channelRuleArgs(P, undefined),
};
console.log(JSON.stringify(cases));
'''
    try:
        run = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
        res = json.loads(run.stdout) if run.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        res = None
    check("S1 the validator runs under node", res is not None, (run.stderr if res is None else "")[:300])
    if res:
        check("S1 a hard rule builds the argv in the order the CLI parses",
              res["hard"].get("args") == ["channels-memory", "discord", "555", "rule", "don_t_discuss",
                                         "--hard", "--days", "7", "surprise party"], str(res["hard"]))
        check("S1 a plain rule has no flags",
              res["plain"].get("args") == ["channels-memory", "discord", "555", "rule", "ask_owner_first", "the lease"])
        for key in ("toneHard", "dash", "long", "toneTooLong", "control", "empty", "days0", "days366",
                    "daysFrac", "daysStr", "hardStr", "badDirective", "badAction", "badRecord", "noBody"):
            check(f"S1 refused: {key}", "error" in res[key] and "args" not in res[key], str(res[key]))
        check("S1 a tone rule may be 240 characters", "args" in res["toneLong"])
        check("S1 retire builds [..., retire, id]",
              res["retire"].get("args") == ["channels-memory", "discord", "555", "retire", "abcdef12"])
else:
    print("(S1 node not found: the argv checks were skipped; run them where node is installed)")

# ======================================================================= S2
panel = (REPO / "web" / "public" / "channels-panel.js").read_text(encoding="utf-8")
check("S2 the panel reads the rules route", 'personUrl(p, "rules")' in panel)
check("S2 it writes through the person route and the one for everyone",
      'personPost(p, "rules", body)' in panel and '"/api/channels/rules/anyone"' in panel)
check("S2 it can end and delete a rule", "End rule" in panel and "Rule deleted." in panel)
check("S2 it offers the hard switch only for the two directives that can have it",
      "RULE_HARD_OK" in panel and "body.hard = true" in panel)
check("S2 it lists conflicts and says which rule wins", "Rules that conflict" in panel and "wins over" in panel)
check("S2 the old read-only line is gone", "To change one, tell Jarvis or use jarvis channels-memory." not in panel)
check("S2 a rule draft is dropped with the rest of the person's panel data",
      panel.count("state.ruleDraft") >= 3 and "ruleDraft.clear()" in panel)

print(f"{PASSED} passed, {len(FAILED)} failed")
for line in FAILED:
    print("FAILED:", line)
sys.exit(1 if FAILED else 0)
