"""Tests for per-person memory, instructions and the remember_sender gate
(master plan L.21, L.22, D-I10).

    python3 tests/test_person_memory.py

No model, no network, no real ~/.jarvis: HOME is redirected to a temp dir
BEFORE any jarvis module is imported (AGENTS.md). The one model call the
feature can make (channels/cheap_call.py) is replaced by a stub. Topics and
people in the rule tests are RANDOM: nothing in the feature may know one.
"""

import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-person-memory-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.actions import channel_people, person_memory_tools as tools   # noqa: E402
from jarvis.channels import (DISCORD, cheap_call, people, person_memory,   # noqa: E402
                             sender_gate, user_admin, user_perms)
from jarvis.channels import config as channel_config                      # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


RNG = random.Random(20261009)


def word():
    """A made-up word that no stop list or stemmer touches (ends in k/z)."""
    letters = "bcdfghjlmnpqrtvwxy"
    vowels = "aeiou"
    body = "".join(RNG.choice(letters) + RNG.choice(vowels) for _ in range(3))
    return body + RNG.choice("kz")


def reset():
    for f in (user_perms.PERMS_FILE, people.PEOPLE_FILE, channel_config.CONFIG_FILE):
        for suffix in ("", ".bak", ".tmp"):
            try:
                Path(str(f) + suffix).unlink()
            except OSError:
                pass
    import shutil
    shutil.rmtree(person_memory.PEOPLE_DIR, ignore_errors=True)
    cfg = channel_config.load_config()
    cfg[DISCORD].update({"enabled": True, "owner": "100"})
    channel_config.save_config(cfg)


def env(**sender):
    os.environ[channel_people.SENDER_ENV] = json.dumps(sender)


def clear_env():
    os.environ.pop(channel_people.SENDER_ENV, None)
    os.environ.pop("JARVIS_SCHEDULED", None)
    os.environ.pop("JARVIS_CONTEXT", None)


A, B = "discord:555", "discord:556"

# ----------------------------------------------------------------- the store
reset()
rec, err = person_memory.add(A, "fact", "has a dog named Biscuit")
check("a fact is stored", rec and not err and rec["status"] == "active" and rec["provenance"] == "owner_stated")
again, _ = person_memory.add(A, "fact", "HAS a dog named biscuit")
check("the same fact is not stored twice", again["id"] == rec["id"] and len(person_memory.export(A)) == 1)
check("text over the cap is refused", person_memory.add(A, "fact", "x" * 301)[1] != "")
check("empty text is refused", person_memory.add(A, "fact", "   ")[1] != "")
check("an unknown kind is refused", person_memory.add(A, "secret", "x")[1] != "")
check("an unknown provenance is refused", person_memory.add(A, "fact", "y", provenance="vibes")[1] != "")
multi, _ = person_memory.add(A, "fact", "line one\nline two\r\nline three")
check("newlines are flattened (this text lands in a system prompt)", "\n" not in multi["text"])
check("the file is JSONL, one record per line",
      len(person_memory.path_for(A).read_text(encoding="utf-8").strip().splitlines()) == 2)
check("another person has their own file", person_memory.path_for(A) != person_memory.path_for(B))

# ----------------------------------------------------------- isolation
reset()
person_memory.add(A, "fact", "has a dog named Biscuit")
person_memory.add(B, "fact", "is allergic to cats")
out = person_memory.context_block([A], "how is my dog doing")
check("the relevant fact is shown", "Biscuit" in out["block"] and out["ids"], out["block"])
check("the other person's fact is never shown", "allergic" not in out["block"])
out = person_memory.context_block([A], "tell me about allergic cats")
check("even when the message names their topic", "allergic" not in out["block"])
check("a person with no records, asking about the past, gets the 'say you don't know' sentence",
      person_memory.context_block(["discord:999"], "do you remember me?")["block"] == person_memory.NO_HISTORY
      and person_memory.context_block(["discord:999"], "do you remember me?")["empty"])
check("...but an ordinary message from them costs no tokens at all",
      person_memory.context_block(["discord:999"], "hello")["block"] == "")
quiet = person_memory.context_block([A], "zzz qqq xxx")
check("records that match nothing add nothing to the prompt", quiet["block"] == "" and not quiet["empty"])

# ------------------------------------------------- status, expiry, provenance
reset()
prop, _ = person_memory.add(A, "fact", "works at the harbour", provenance="person_stated", status="proposed")
check("a proposed item is not shown", "harbour" not in person_memory.context_block([A], "harbour work")["block"])
person_memory.set_status(A, prop["id"], "active")
check("approving shows it", "harbour" in person_memory.context_block([A], "harbour work")["block"])
person_memory.set_status(A, prop["id"], "retired")
check("a retired item is not shown", "harbour" not in person_memory.context_block([A], "harbour work")["block"])
person_memory.add(A, "fact", "sails boats on weekends", expires=1.0)
check("an expired item is not shown", "sails" not in person_memory.context_block([A], "sails boats")["block"])
person_memory.add(A, "fact", "paints fences", provenance="inferred")
check("an inferred item reads as 'possibly'", "possibly: paints fences" in person_memory.context_block([A], "paints fences")["block"])
check("ids appear so the owner can name an item", "[" in person_memory.context_block([A], "paints fences")["block"])
check("forget deletes one record", person_memory.forget(A, prop["id"]) and person_memory.get(A, prop["id"]) is None)
check("forgetting something absent says so", person_memory.forget(A, "nothere") is False)

# ----------------------------------------------------------------- budget
reset()
for i in range(30):
    person_memory.add(A, "fact", f"garden note number {i} " + "plants " * 12)
out = person_memory.context_block([A], "garden plants", budget_tokens=200)
check("the block stays inside the budget", out["tokens"] <= 200, str(out["tokens"]))
check("and not every record is included", 0 < len(out["ids"]) < 30, str(len(out["ids"])))

# --------------------------------------------- skip_texts (note already shown)
reset()
person_memory.add(A, "fact", "likes strong tea")
check("an item equal to an identity note is not said twice",
      "strong tea" not in person_memory.context_block([A], "tea", skip_texts=["Likes strong tea"])["block"])

# --------------------------------------------------- linked accounts share
reset()
person_memory.add(B, "fact", "plays the cello")
check("a linked account's facts can be passed along",
      "cello" in person_memory.context_block([A, B], "cello music")["block"])

# ----------------------------------------------------------- instructions
reset()
calls = []


def classifier(verdict):
    def fn(topic, message):
        calls.append((topic, message))
        return verdict
    return fn


for trial in range(6):
    w1, w2, w3 = word(), word(), word()
    person = f"discord:{600 + trial}"
    topic = f"{w1} {w2} {w3}"
    rec, err = person_memory.add_instruction(person, topic, "don_t_discuss")
    check(f"[{trial}] a rule is created for a random topic", rec and not err, err)
    check(f"[{trial}] keywords are derived from the owner's words, not hard-coded",
          set(rec["instruction"]["keywords"]) == {w1, w2, w3}, str(rec["instruction"]["keywords"]))
    del calls[:]
    got = person_memory.matches(person, f"what about {w1} {w2} {w3} please", classify=classifier(False))
    check(f"[{trial}] a message with the whole topic matches without a model call",
          len(got) == 1 and got[0][1] == "lexical" and not calls, str(got))
    del calls[:]
    got = person_memory.matches(person, f"good morning, nice weather", classify=classifier(True))
    check(f"[{trial}] an unrelated message does not match", got == [], str(got))
    del calls[:]
    got = person_memory.matches(person, f"I heard about {w1} yesterday", classify=classifier(True))
    check(f"[{trial}] a weak match asks the cheap call exactly once", len(calls) == 1, str(calls))
    check(f"[{trial}] ...and applies it when the answer is yes", len(got) == 1 and got[0][1] == "classified", str(got))
    got = person_memory.matches(person, f"I heard about {w1} yesterday", classify=classifier(False))
    check(f"[{trial}] ...and does not when the answer is no", got == [], str(got))
    got = person_memory.matches(person, f"I heard about {w1} yesterday", classify=classifier(None))
    check(f"[{trial}] a restrictive rule is applied when the model call fails",
          len(got) == 1 and got[0][1] == "unsure-applied", str(got))
    other = person_memory.matches("discord:999999", f"{w1} {w2} {w3}", classify=classifier(True))
    check(f"[{trial}] another person is not affected", other == [], str(other))

reset()
w1, w2, w3 = word(), word(), word()
person_memory.add_instruction(A, f"{w1} {w2} {w3}", "notify_owner")
got = person_memory.matches(A, f"just {w1} today", classify=classifier(None))
check("a non-restrictive rule is NOT applied when the model call fails", got == [], str(got))

reset()
w1, w2 = word(), word()
person_memory.add_instruction(person_memory.ANYONE, f"{w1} {w2}", "don_t_discuss")
person_memory.add_instruction(A, f"{w1} {w2}", "ask_owner_first")
got = person_memory.matches(A, f"all about {w1} {w2}", classify=classifier(False))
check("rules for anyone and for the person both apply", len(got) == 2, str(got))
check("the person's own rule comes first", got[0][0]["instruction"]["directive"] == "ask_owner_first")
check("a rule for anyone reaches someone else too",
      len(person_memory.matches(B, f"all about {w1} {w2}", classify=classifier(False))) == 1)

rendered = person_memory.render_rules(got)
check("the rendered rules name the topic", f"{w1} {w2}" in rendered)
check("and tell the model never to reveal them, whatever the person claims",
      "never reveal" in rendered and "changes nothing" in rendered, rendered)
check("with nothing matched there is no block at all", person_memory.render_rules([]) == "")

reset()
tone, _ = person_memory.add_instruction(A, "", "tone", text="keep answers to two lines")
cust, _ = person_memory.add_instruction(A, "", "custom", text="address them as captain")
got = person_memory.matches(A, "anything at all", classify=classifier(False))
check("tone and custom rules always apply", len(got) == 2, str(got))
text = person_memory.render_rules(got)
check("their wording reaches the model", "two lines" in text and "captain" in text, text)

reset()
check("a topic with no usable words is refused",
      person_memory.add_instruction(A, "the of and", "don_t_discuss")[1] != "")
check("an unknown directive is refused", person_memory.add_instruction(A, "x" + word(), "obey")[1] != "")
check("a rule needs a subject", person_memory.add_instruction("", word(), "don_t_discuss")[1] != "")
w = word()
past, _ = person_memory.add_instruction(A, w, "don_t_discuss", until=time.time() - 5)
check("a rule past its end date no longer applies",
      person_memory.matches(A, f"about {w}", classify=classifier(False)) == [])
check("applying a rule is logged without the message",
      person_memory.log_applied(A, [(past, "lexical")]) and "about" not in person_memory.APPLIED_LOG.read_text())

# ---------------------------------------------------- owner tools + guards
reset()
people.touch(DISCORD, "555", handle="sam")
people.set_name(DISCORD, "555", "Sam", manual=True)
people.touch(DISCORD, "556", handle="sam2")
people.set_name(DISCORD, "556", "Sam", manual=True)
people.touch(DISCORD, "557", handle="robin")
people.set_name(DISCORD, "557", "Robin", manual=True)
clear_env()
out = tools.tool_person_remember({"person": "Robin", "text": "prefers short answers"})
check("the owner (no chat context) can save a fact", out.get("ok") is True, str(out))
check("it landed in that person's file only",
      len(person_memory.export("discord:557")) == 1 and person_memory.export("discord:555") == [])
out = tools.tool_person_remember({"person": "Sam", "text": "x"})
check("two people with one name is an error that lists them, not a pick",
      out.get("ok") is False and len(out.get("candidates", [])) == 2, str(out))
out = tools.tool_person_remember({"person": "nobody-here", "text": "x"})
check("an unknown person is an error", out.get("ok") is False and "known_contacts" in out, str(out))
people.set_follow(DISCORD, "557", people.FOLLOW_BLOCKED)
check("a blocked person can still be remembered about",
      tools.tool_person_remember({"person": "Robin", "text": "second fact"}).get("ok") is True)
out = tools.tool_person_recall({"person": "Robin"})
check("recall lists ids and status", out["count"] == 2 and all("id" in i for i in out["items"]), str(out))
rid = out["items"][0]["id"]
check("forget removes one item", tools.tool_person_forget({"person": "Robin", "id": rid}).get("ok") is True
      and tools.tool_person_recall({"person": "Robin"})["count"] == 1)
ins = tools.tool_person_instruct({"person": "Robin", "directive": "don_t_discuss", "topic": word() + " " + word()})
check("the owner can give a rule", ins.get("ok") is True and ins["keywords"], str(ins))
check("a rule for anyone", tools.tool_person_instruct(
    {"person": "anyone", "directive": "tone", "text": "be calm"}).get("ok") is True)
prop, _ = person_memory.add("discord:557", "fact", "from a guest", provenance="person_stated", status="proposed")
check("review approves", tools.tool_person_review(
    {"person": "Robin", "id": prop["id"], "decision": "approve"}).get("now") == "active")
check("review refuses a wrong decision", tools.tool_person_review(
    {"person": "Robin", "id": prop["id"], "decision": "maybe"}).get("ok") is False)

env(platform=DISCORD, user_id="555", handle="sam", is_owner=False)
for name, fn, args in (("person_remember", tools.tool_person_remember, {"person": "Robin", "text": "x"}),
                       ("person_forget", tools.tool_person_forget, {"person": "Robin", "id": "x"}),
                       ("person_instruct", tools.tool_person_instruct, {"person": "Robin", "directive": "tone", "text": "x"}),
                       ("person_recall", tools.tool_person_recall, {"person": "Robin"}),
                       ("person_review", tools.tool_person_review, {"person": "Robin", "id": "x", "decision": "approve"}),
                       ("await_reply", tools.tool_await_reply, {"person": "Robin"})):
    out = fn(args)
    check(f"{name} refuses a chat guest", out.get("ok") is False and "owner" in json.dumps(out).lower(), str(out))
clear_env()
os.environ["JARVIS_SCHEDULED"] = "1"
check("a scheduled run is refused", tools.tool_person_remember({"person": "Robin", "text": "x"}).get("ok") is False)
clear_env()
check("OWNER_ONLY_TOOLS hides every person tool from guests",
      {"person_remember", "person_forget", "person_instruct", "person_recall",
       "person_review", "await_reply"} <= set(__import__("jarvis.tools", fromlist=["x"]).OWNER_ONLY_TOOLS))

# --------------------------------------------------------- forget a person
reset()
people.touch(DISCORD, "700", handle="gone")
person_memory.add("discord:700", "fact", "will be forgotten")
person_memory.add_instruction("discord:700", word() + " " + word(), "don_t_discuss")
dry = user_admin.forget_person(DISCORD, "700", dry_run=True)
check("the preview counts the saved records", dry[0] and dry[2]["memory_records"] == 2, str(dry))
check("the preview deletes nothing", len(person_memory.export("discord:700")) == 2)
ok, err, report = user_admin.forget_person(DISCORD, "700")
check("forgetting a person succeeds", ok, err)
check("and removes every saved record", person_memory.export("discord:700") == []
      and not person_memory.path_for("discord:700").parent.exists())

# ------------------------------------------------------------ D-I10 gate
check("introducing themselves passes the free pre-filter",
      sender_gate.prefilter({"name": "", "messages": 1}, "hi, my name is Sam")[0])
check("small talk does not", not sender_gate.prefilter({"name": "Sam", "messages": 9}, "lol ok")[0])
check("a short reply early on, with no name saved, might be a name",
      sender_gate.prefilter({"name": "", "messages": 2}, "Sam")[0])
check("...but not once a name is saved", not sender_gate.prefilter({"name": "Sam", "messages": 2}, "Sam")[0])
check("...and not deep into the conversation", not sender_gate.prefilter({"name": "", "messages": 40}, "Sam")[0])
check("a tools-off turn is only offered the tool when the pre-filter passes",
      sender_gate.offer_remember_sender({"name": "", "messages": 1}, "call me Sam")
      and not sender_gate.offer_remember_sender({"name": "Sam", "messages": 5}, "what's up"))
check("a second name needs them to ask for it",
      not sender_gate.name_allowed({"name": "Sam"}, "what's the weather")[0]
      and sender_gate.name_allowed({"name": "Sam"}, "actually call me Robin")[0])
check("a name the owner locked is passed to set_name, which keeps it",
      sender_gate.name_allowed({"name": "Sam", "name_locked": True}, "hello")[0])
check("a note must come from the message",
      not sender_gate.note_supported("owns a yacht", "my name is Sam") and
      sender_gate.note_supported("likes tea", "i like tea"))

original = cheap_call._complete
asked = []


def stub(answer):
    def fn(prompt):
        asked.append(prompt)
        return answer
    return fn


try:
    cheap_call._complete = stub('{"keep": true}')
    check("worth_remembering parses a yes", sender_gate.worth_remembering("likes tea", "i like tea") is True)
    cheap_call._complete = stub('Sure! {"keep": false}')
    check("...a no, even with words around it", sender_gate.worth_remembering("x", "y") is False)
    cheap_call._complete = stub("not json")
    check("...and nonsense as 'could not ask'", sender_gate.worth_remembering("x", "y") is None)
    cheap_call._complete = stub(None)
    check("...and no answer as 'could not ask'", sender_gate.worth_remembering("x", "y") is None)
    del asked[:]
    cheap_call._complete = stub('{"keep": true}')
    sender_gate.worth_remembering('a "quoted" note\nwith lines', "text")
    check("quotes and newlines are stripped from what the model is shown",
          '"quoted"' not in asked[0] and "a 'quoted' note with lines" in asked[0])

    # remember_sender end to end, as a stranger
    reset()
    people.touch(DISCORD, "800", handle="stranger")
    env(platform=DISCORD, user_id="800", handle="stranger", is_owner=False, text="my name is Alex, i like tea")
    del asked[:]
    out = channel_people.tool_remember_sender({"name": "Alex", "note": "likes tea"})
    check("an introduction is saved", out.get("ok") is True and people.get(DISCORD, "800")["name"] == "Alex", str(out))
    check("the note went through one model call", len(asked) == 1, str(len(asked)))
    check("and is mirrored as PROPOSED for the owner",
          [r["status"] for r in person_memory.export("discord:800")] == ["proposed"])
    check("a proposed note is not shown to the model",
          person_memory.context_block(["discord:800"], "tea please")["block"] == "")

    env(platform=DISCORD, user_id="800", handle="stranger", is_owner=False, text="what time is it")
    del asked[:]
    out = channel_people.tool_remember_sender({"note": "owns a boat"})
    check("small talk is refused with no model call", out.get("ok") is False and not asked, str(out))
    check("and nothing is stored", len(person_memory.export("discord:800")) == 1
          and "boat" not in json.dumps(people.get(DISCORD, "800")["notes"]))

    env(platform=DISCORD, user_id="800", handle="stranger", is_owner=False, text="remember that i work at the harbour")
    cheap_call._complete = stub(None)
    out = channel_people.tool_remember_sender({"note": "works at the harbour"})
    check("if the yes/no call can't be made the note is NOT stored (fails closed)",
          out.get("ok") is False and "harbour" not in json.dumps(people.get(DISCORD, "800")["notes"]), str(out))

    env(platform=DISCORD, user_id="800", handle="stranger", is_owner=False, text="remember that i like jazz")
    cheap_call._complete = stub('{"keep": false}')
    check("a 'no' from the model is not stored either",
          channel_people.tool_remember_sender({"note": "likes jazz"}).get("ok") is False)

    people.touch(DISCORD, "100", handle="owner", is_owner=True)
    env(platform=DISCORD, user_id="100", handle="owner", is_owner=True, text="whatever")
    del asked[:]
    out = channel_people.tool_remember_sender({"name": "Boss", "note": "is the owner"})
    check("the owner is never gated", out.get("ok") is True and not asked, str(out))
finally:
    cheap_call._complete = original
    clear_env()

print(f"{PASSED} passed, {len(FAILED)} failed")
for line in FAILED:
    print("FAILED:", line)
sys.exit(1 if FAILED else 0)
