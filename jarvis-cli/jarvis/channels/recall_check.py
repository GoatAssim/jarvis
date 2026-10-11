"""Does a reply's "I remember..." trace back to something saved? (master plan L.21)

THE RULE BEING CHECKED
----------------------
The memory block that reaches the model lists every saved item as
`[id] (date, provenance) text` and says anything not listed is unknown
(person_memory.HEADER / NO_HISTORY). The plan's wording was "recalled
statements must carry an id": a sentence in which Jarvis claims to remember
something ("you told me...", "last time we talked about...") has to be
traceable to an item in that block, or to what the person just said. A recall
sentence that is neither is made-up history.

`check_reply` makes that checkable WITHOUT a model call and without asking the
model to print ids at a guest (an id in a chat reply would be noise). It splits
the reply into sentences, picks out the ones that claim a memory, and resolves
each to the ids of the block items it matches, or to the person's own message,
or to nothing. "Nothing" is an ungrounded recall.

It is deliberately lexical and conservative:
  * only sentences that CLAIM a memory are looked at (a plain answer is not);
  * a sentence that says Jarvis does NOT remember is fine, and is how an empty
    block should be answered;
  * grounded means the sentence shares content words with one saved item (two,
    or all of them when the item has fewer), or with the person's message.
It can be fooled by a hallucination that happens to reuse a saved item's words;
it is a tripwire for the common failure, not a proof.

TWO USES
--------
  * tests: golden replies with known-good and known-bad recall;
  * runtime AUDIT (`audit`): after a non-owner's reply, one line with counts
    and the ids cited -- never the reply or the message -- so the owner can see
    whether made-up history happens (`jarvis channels-memory recall-audit`).
    It changes nothing about what is sent.
"""

import json
import re
import time

from . import person_memory
from .directory import CHANNELS_DIR

AUDIT_LOG = CHANNELS_DIR / "recall_audit.jsonl"
MAX_LOG_BYTES = 300_000

# A sentence that CLAIMS a memory of the person or of an earlier conversation.
_CLAIM = re.compile(
    r"\b(you\s+(?:told|said|mentioned|shared|wrote|asked|once)|"
    r"you(?:'ve|\s+have)\s+(?:told|said|mentioned|shared)|"
    r"i\s+(?:remember|recall)|if\s+i\s+(?:remember|recall)|"
    r"last\s+time|the\s+other\s+day|previously|earlier\s+you|"
    r"as\s+you\s+(?:said|mentioned|told)|"
    r"we\s+(?:talked|spoke|discussed|chatted|went\s+over)|"
    r"we(?:'ve|\s+have)\s+(?:talked|spoken|discussed|chatted)|"
    r"before,?\s+you)\b", re.I)

# "I don't remember / I have nothing saved": a correct answer to an empty
# block, never a violation.
_NOT_KNOWING = re.compile(
    r"(?:don'?t|do\s+not|can'?t|cannot|can\s+not|won'?t|didn'?t|haven'?t|"
    r"have\s+not)\s+(?:currently\s+|really\s+|actually\s+|seem\s+to\s+)?"
    r"(?:remember|recall|know|have|see|find|keep)|"
    r"\bno\s+(?:earlier|saved|record|memory|recollection|history)|"
    r"\bnothing\s+(?:saved|about|earlier|on\s+file|stored)|"
    r"\bnot\s+sure\s+(?:i\s+)?(?:remember|recall)|"
    r"\bi\s+have\s+no\s+(?:record|memory|recollection)", re.I)

# Words that make a sentence a recall sentence but carry no content.
_NOISE_WORDS = ("told said mention mentioned shared wrote asked once remember "
                "recall talk talked spoke speak discussed chat chatted last "
                "time other day previously earlier before went over know").split()
# Raw words AND their stems: "shared" stems to "shar", and the stems are what
# the comparison sees.
_RECALL_NOISE = frozenset(_NOISE_WORDS + [person_memory._stem(w) for w in _NOISE_WORDS])

_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")


def _stems(text):
    return {s for s in person_memory._stems(text) if s not in _RECALL_NOISE}


def _sentences(reply):
    return [s.strip() for s in _SENTENCES.split(str(reply or "")) if s and s.strip()]


def _need(record_stems):
    return min(2, len(record_stems)) or 1


def check_reply(reply, records, message=""):
    """Resolve every recall claim in `reply`.

    `records` are the saved items that were IN the block for this turn (dicts
    with id/text), `message` is what the person just said. Returns
      {"statements": [{"text", "ids", "via", "grounded"}],
       "recalled": n, "grounded": n, "ungrounded": n, "cited_ids": [ids]}
    where `via` is 'record', 'message' or None."""
    prepared = []
    for rec in records or []:
        stems = _stems(rec.get("text"))
        if stems:
            prepared.append((rec.get("id"), stems))
    said = _stems(message)
    statements, cited = [], []
    for sentence in _sentences(reply):
        if not _CLAIM.search(sentence):
            continue
        if _NOT_KNOWING.search(sentence):
            continue
        stems = _stems(sentence)
        ids = [rid for rid, rec_stems in prepared
               if len(stems & rec_stems) >= _need(rec_stems)]
        via = None
        if ids:
            via = "record"
            cited.extend(i for i in ids if i not in cited)
        elif said and len(stems & said) >= 2:
            via = "message"
        statements.append({"text": sentence[:200], "ids": ids, "via": via,
                           "grounded": via is not None})
    grounded = sum(1 for s in statements if s["grounded"])
    return {"statements": statements, "recalled": len(statements),
            "grounded": grounded, "ungrounded": len(statements) - grounded,
            "cited_ids": cited}


def records_for(keys, ids):
    """The saved items with these ids, read from the given people's files only
    (the same keys the block was built from)."""
    wanted = {str(i) for i in ids or []}
    out = []
    if not wanted:
        return out
    for key in keys or []:
        for rec in person_memory._read(key):
            if str(rec.get("id")) in wanted:
                out.append(rec)
    return out


def audit(person_key, keys, ids_in_block, reply, message):
    """Write one audit line if the reply claimed any memory. Counts and ids
    only. Returns the check result (or None). Never raises."""
    try:
        result = check_reply(reply, records_for(keys, ids_in_block), message)
        if not result["recalled"]:
            return result
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        if AUDIT_LOG.exists() and AUDIT_LOG.stat().st_size > MAX_LOG_BYTES:
            AUDIT_LOG.replace(AUDIT_LOG.with_suffix(".old.jsonl"))
        with AUDIT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "person": person_key,
                "in_block": len(ids_in_block or []),
                "recalled": result["recalled"],
                "ungrounded": result["ungrounded"],
                "cited": result["cited_ids"]}) + "\n")
        return result
    except Exception:  # noqa: BLE001 -- an audit never breaks a reply
        return None


def read_audit(limit=50):
    out = []
    try:
        with AUDIT_LOG.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out[-limit:]
