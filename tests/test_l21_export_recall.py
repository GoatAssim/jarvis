"""Tests for the L.21 items that were still open: the memory export, the
"recalled statements carry an id" check, the golden "no hallucinated history"
fixtures and the 1,000-record budget test.

    python3 tests/test_l21_export_recall.py

No model, no network, no real ~/.jarvis: HOME is redirected to a temp dir
BEFORE any jarvis module is imported (AGENTS.md).

  E1  export document / text   channels/person_memory.py export_document, export_text
  E2  export through the CLI   `jarvis channels-memory ... export [text]`
  E3  export through the panel server.js route + channels-panel.js (static checks)
  R1  recall check, golden     channels/recall_check.py against fixtures/l21_recall_golden.json
  R2  every listed item has an id; the audit log holds counts and ids, never text
  R3  the audit is wired into the reply path before anything is sent
  B1  budget at scale          1,000 records, the block stays inside its budget
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l21-export-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "jarvis-cli"))

from jarvis import channels_cli                                          # noqa: E402
from jarvis.channels import (DISCORD, people, person_memory,             # noqa: E402
                             recall_check)
from jarvis.channels import config as channel_config                     # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


def reset():
    import shutil
    shutil.rmtree(person_memory.PEOPLE_DIR, ignore_errors=True)
    for path in (person_memory.APPLIED_LOG, recall_check.AUDIT_LOG):
        try:
            path.unlink()
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


def run_cli(*args):
    """(exit code, parsed JSON or None) for `jarvis channels-memory <args>`."""
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

# ======================================================================= E1
reset()
fact, _ = person_memory.add(A, "fact", "has a dog named Biscuit")
waiting, _ = person_memory.add(A, "fact", "works at the harbour", provenance="person_stated",
                               status="proposed")
gone, _ = person_memory.add(A, "fact", "used to play the cello")
person_memory.set_status(A, gone["id"], "retired")
rule, _ = person_memory.add_instruction(A, "surprise party", "don_t_discuss", hard=True)
episode, _ = person_memory.add(A, "episode", "Asked about ferry times.", provenance="observed",
                               status="proposed",
                               source={"thread": "t1", "ts": ["2026-10-01T10:00:00", "2026-10-01T10:20:00"]})
person_memory.add(B, "fact", "is allergic to cats")

doc = person_memory.export_document(A, person={"platform": "discord", "user_id": "555", "name": "Sam"})
check("E1 the export names its format and version",
      doc["format"] == person_memory.EXPORT_FORMAT and doc["version"] == person_memory.EXPORT_VERSION)
check("E1 every record is in it, whatever its status",
      {r["id"] for r in doc["items"]} == {fact["id"], waiting["id"], gone["id"], rule["id"], episode["id"]},
      str([r["id"] for r in doc["items"]]))
check("E1 a retired record is included (the export is not 'what Jarvis uses')",
      any(r["id"] == gone["id"] and r["status"] == "retired" for r in doc["items"]))
check("E1 the count and the per-kind counts add up",
      doc["count"] == 5 and sum(doc["counts"].values()) == 5 and doc["counts"].get("fact/active") == 1,
      str(doc["counts"]))
check("E1 the person is identified, key always included",
      doc["person"]["key"] == A and doc["person"]["name"] == "Sam")
check("E1 another person's records are not in it",
      "allergic" not in json.dumps(doc))
check("E1 the document is plain JSON", json.loads(json.dumps(doc, default=str))["count"] == 5)
check("E1 records are verbatim (the rule keeps its hard flag)",
      any(r["id"] == rule["id"] and r["instruction"]["hard"] is True for r in doc["items"]))

text = person_memory.export_text(doc)
check("E1 the text export has the three sections",
      "== Facts (3) ==" in text and "== Conversation summaries (1) ==" in text
      and "== Standing rules (1) ==" in text, text)
check("E1 the text export shows every id and status",
      all(r["id"] in text for r in doc["items"]) and "retired" in text and "proposed" in text)
check("E1 a hard rule says so in the text", "hard" in text.split("== Standing rules")[1])
check("E1 a summary shows the day the conversation happened", "2026-10-01" in text)
empty = person_memory.export_text(person_memory.export_document("discord:999"))
check("E1 an empty export says nothing is saved", "Nothing is saved for this person." in empty, empty)
check("E1 exporting someone with no file creates no file",
      not person_memory.path_for("discord:999").exists())

# ======================================================================= E2
reset()
people.touch(DISCORD, "555", handle="sam")
people.set_name(DISCORD, "555", "Sam", manual=True)
person_memory.add(A, "fact", "has a dog named Biscuit")
gone, _ = person_memory.add(A, "fact", "retired thing")
person_memory.set_status(A, gone["id"], "retired")

code, out = run_cli("discord", "555", "export")
check("E2 the CLI exports JSON", code == 0 and out and out["ok"] is True and out["count"] == 2, str(out))
check("E2 the records are under 'items' (the shape the CLI always had)",
      out and len(out["items"]) == 2 and out["format"] == person_memory.EXPORT_FORMAT)
check("E2 the person's name and handle are in it",
      out and out["person"].get("name") == "Sam" and out["person"].get("handle") == "sam", str(out and out.get("person")))
code, out = run_cli("discord", "555", "export", "text")
check("E2 'export text' returns readable text and a file name",
      code == 0 and out and "Biscuit" in out["text"]
      and re.fullmatch(r"jarvis-memory-discord-555-\d{4}-\d{2}-\d{2}\.txt", out["filename"] or ""), str(out))
code, out = run_cli("discord", "555", "export", "bogus")
check("E2 an unknown export format is an error, not a silent JSON", code == 1 and out and out["ok"] is False)
code, out = run_cli("discord", "777", "export")
check("E2 exporting someone who isn't registered is an error", code == 1 and out and out["ok"] is False)
code, out = run_cli("anyone", "export")
check("E2 the rules for everyone can be exported too", code == 0 and out and out["person"]["key"] == "anyone", str(out))

# ======================================================================= E3
server = (REPO / "web" / "server.js").read_text(encoding="utf-8")
panel = (REPO / "web" / "public" / "channels-panel.js").read_text(encoding="utf-8")
route = re.search(r'app\.get\("/api/channels/people/:platform/:id/memory/export".*?\n\}\);', server, re.S)
check("E3 the export route exists", bool(route))
body = route.group(0) if route else ""
check("E3 it checks the person like every other route", "channelPersonArgs(req, res)" in body)
check("E3 the format is whitelisted", 'format !== "json" && format !== "text"' in body)
check("E3 it is never cached (it holds what guests said)", 'Cache-Control", "no-store"' in body)
check("E3 it goes through the CLI with an argv array", '"export"' in body and "runJarvisOnce(args" in body)
check("E3 the panel asks that route and downloads the result",
      "memory/export?format=" in panel and "URL.createObjectURL" in panel and "function exportMem" in panel)
check("E3 the panel offers both formats", 'exportMem(p, "json")' in panel and 'exportMem(p, "text")' in panel)

# ======================================================================= R1
GOLDEN_FILE = REPO / "tests" / "fixtures" / "l21_recall_golden.json"
if not GOLDEN_FILE.exists():   # tests/fixtures is kept out of the shared copy (private conversations)
    print("SKIPPED: tests/fixtures/l21_recall_golden.json is not in this copy (the golden recall cases only)")
else:
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    check("R1 the fixture has cases", len(golden["cases"]) >= 4)
    for case in golden["cases"]:
        records = case["records"]
        known = {r["id"] for r in records}
        for item in case["good"]:
            res = recall_check.check_reply(item["reply"], records, case["message"])
            label = f"R1 [{case['name']}] good: {item['reply'][:50]}"
            check(label + " -> every recall claim is grounded", res["ungrounded"] == 0, json.dumps(res["statements"]))
            check(label + " -> cites only ids from the block", set(res["cited_ids"]) <= known, str(res["cited_ids"]))
            if "expect_ids" in item:
                check(label + " -> cites exactly the expected ids", res["cited_ids"] == item["expect_ids"], str(res["cited_ids"]))
        for reply in case["bad"]:
            res = recall_check.check_reply(reply, records, case["message"])
            check(f"R1 [{case['name']}] bad: {reply[:50]} -> flagged", res["ungrounded"] >= 1, json.dumps(res["statements"]))

# a saved item that was NOT in the block cannot ground a claim
reset()
r1, _ = person_memory.add(A, "fact", "has a dog named Biscuit")
r2, _ = person_memory.add(A, "fact", "lives in Lisbon")
in_block = recall_check.records_for([A], [r1["id"]])
check("R1 records_for returns only the ids it was given", [r["id"] for r in in_block] == [r1["id"]])
check("R1 ...and reads only the keys it was given", recall_check.records_for([B], [r1["id"]]) == [])
res = recall_check.check_reply("I remember you live in Lisbon.", in_block, "hello there")
check("R1 an item that was not in this turn's block does not ground a claim", res["ungrounded"] == 1, str(res))
res = recall_check.check_reply("Last time you mentioned your dog Biscuit.", in_block, "hello there")
check("R1 an item that WAS in the block does", res["ungrounded"] == 0 and res["cited_ids"] == [r1["id"]], str(res))
check("R1 an empty reply and an empty block are fine",
      recall_check.check_reply("", [], "")["recalled"] == 0)
check("R1 a plain answer makes no recall claim",
      recall_check.check_reply("The ferry leaves at nine.", [], "when is the ferry")["recalled"] == 0)

# ======================================================================= R2
reset()
for i in range(6):
    person_memory.add(A, "fact", f"likes number {i} garden plants and herbs")
person_memory.add(B, "fact", "only B knows the word zanzibar")
block = person_memory.context_block([A], "tell me about garden plants", budget_tokens=500)
listed = re.findall(r"\[([0-9a-f]{8})\]", block["block"])
check("R2 every item in the block carries its id", listed and set(listed) == set(block["ids"]),
      f"{listed} vs {block['ids']}")
check("R2 each listed line starts with its id",
      all(re.match(r"\[[0-9a-f]{8}\] \(", line) for line in block["block"].splitlines()[1:]))
check("R2 the header tells the model anything not listed is unknown",
      "anything not listed" in block["block"] and "you do not remember" in block["block"])
check("R2 B's item is not in A's block", "zanzibar" not in block["block"])

dog, _ = person_memory.add(A, "fact", "has a dog named Biscuit")
reply_bad = "Last time you told me about your quokka rescue."
res = recall_check.audit(A, [A], [dog["id"]], reply_bad, "hello")
check("R2 the audit finds the made-up memory", res and res["ungrounded"] == 1, str(res))
lines = recall_check.AUDIT_LOG.read_text(encoding="utf-8").strip().splitlines()
entry = json.loads(lines[-1])
check("R2 the audit line has counts and ids",
      entry["recalled"] == 1 and entry["ungrounded"] == 1 and entry["in_block"] == 1 and entry["person"] == A, str(entry))
check("R2 the audit line never holds the reply or the message",
      "quokka" not in lines[-1] and "hello" not in lines[-1])
before = len(lines)
recall_check.audit(A, [A], [dog["id"]], "The ferry leaves at nine.", "when is the ferry")
check("R2 a reply with no recall claim writes nothing",
      len(recall_check.AUDIT_LOG.read_text(encoding="utf-8").strip().splitlines()) == before)
check("R2 read_audit returns the entries", recall_check.read_audit()[-1]["ungrounded"] == 1)
check("R2 an audit never raises, even on junk",
      recall_check.audit(None, None, None, None, None) is not None)
code, out = run_cli("recall-audit")
check("R2 the CLI lists the audit", out and out["ok"] and len(out["audit"]) == 1, str(out))

# ======================================================================= R3
base_src = (REPO / "jarvis-cli" / "jarvis" / "channels" / "base.py").read_text(encoding="utf-8")
i_audit = base_src.find("recall_check.audit(")
i_send = base_src.find("for chunk in chunk_text(text, limit):")
check("R3 base.py runs the recall audit", i_audit > 0)
check("R3 ...before the first chunk of the reply is sent", 0 < i_audit < i_send)
check("R3 ...only for someone who is not the owner",
      "if not is_owner and ask_ok and text:" in base_src)

# ======================================================================= B1
reset()
now = time.time()
records = []
for i in range(1000):
    records.append({
        "id": f"{i:08x}", "kind": "fact", "text": f"garden note number {i}: the plants need water and light",
        "provenance": "owner_stated", "source": {}, "confidence": None, "tags": [],
        "created": now - i, "expires": None, "status": "active"})
check("B1 the 1,000-record file is written", person_memory._write(A, records))
check("B1 it reads back whole", len(person_memory._read(A)) == 1000)
check("B1 add() still refuses to grow a person past the cap",
      person_memory.add(A, "fact", "one more")[1] != "")
started = time.time()
out = person_memory.context_block([A], "how are the garden plants", budget_tokens=500)
elapsed = time.time() - started
check("B1 the block stays inside the default budget at 1,000 records",
      out["tokens"] <= 500 and out["block"] != "", str(out["tokens"]))
check("B1 ...and the block's own text is not over it either",
      len(out["block"]) <= 500 * 4 + 100, str(len(out["block"])))   # ~4 chars a token, plus the newlines
check("B1 ...and only some of the records are in it", 0 < len(out["ids"]) < 1000, str(len(out["ids"])))
small = person_memory.context_block([A], "how are the garden plants", budget_tokens=120)
check("B1 a smaller budget is respected too", small["tokens"] <= 120 and small["ids"], str(small["tokens"]))
check("B1 it returns in a sane time (loose bound; the number is printed)", elapsed < 20, f"{elapsed:.2f}s")
check("B1 someone else's block is still empty", person_memory.context_block([B], "garden plants")["block"] == "")
person_memory._write(A, records + [{
    "id": "deadbeef", "kind": "fact", "text": "has a dog named Biscuit", "provenance": "owner_stated",
    "source": {}, "confidence": None, "tags": [], "created": now - 5000, "expires": None, "status": "active"}])
needle = person_memory.context_block([A], "how is my dog Biscuit doing", budget_tokens=500)
check("B1 one relevant record is found among 1,001", "deadbeef" in needle["ids"], str(needle["ids"][:5]))
print(f"(B1 block over 1,000 records took {elapsed:.3f}s)")

print(f"{PASSED} passed, {len(FAILED)} failed")
for line in FAILED:
    print("FAILED:", line)
sys.exit(1 if FAILED else 0)
