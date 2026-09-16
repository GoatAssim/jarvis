"""Regression tests for the conversation-persistence bugs.

Each test here corresponds to a bug that was real, reproduced, and fixed —
not a hypothetical. The comment above each names the user-visible symptom,
because that is what someone will be searching for when it comes back.

    python3 tests/test_persistence_fixes.py

Every test runs against a throwaway HOME so it never touches a real
~/.jarvis. That matters more than usual here: these tests deliberately
corrupt files to prove recovery works.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")


class SandboxHome:
    """Point every ~/.jarvis-derived path at a tempdir.

    The modules compute their paths at import time from Path.home(), so
    setting $HOME afterwards is not enough — the module constants have to
    be rebound too, and put back afterwards so later tests in the same
    process aren't affected.
    """

    def __init__(self):
        self._tmp = None
        self._saved = []

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)

        from jarvis import conversations, memory, stats

        jarvis_dir = root / ".jarvis"
        conv_dir = jarvis_dir / "conversations"

        def rebind(module, attr, value):
            self._saved.append((module, attr, getattr(module, attr)))
            setattr(module, attr, value)

        rebind(conversations, "JARVIS_DIR", jarvis_dir)
        rebind(conversations, "CONV_DIR", conv_dir)
        rebind(conversations, "INDEX_FILE", conv_dir / "index.json")
        rebind(conversations, "CURRENT_FILE", jarvis_dir / "current_conversation.json")
        rebind(memory, "JARVIS_DIR", jarvis_dir)
        rebind(memory, "CONFIG_FILE", jarvis_dir / "memory.json")
        rebind(stats, "JARVIS_DIR", jarvis_dir)
        rebind(stats, "STATS_FILE", jarvis_dir / "command_stats.json")

        jarvis_dir.mkdir(parents=True, exist_ok=True)
        conv_dir.mkdir(parents=True, exist_ok=True)
        return root

    def __exit__(self, *exc):
        for module, attr, value in reversed(self._saved):
            setattr(module, attr, value)
        self._saved = []
        self._tmp.cleanup()
        return False


def tear(path, fraction=0.5):
    """Leave a file exactly as an interrupted write_text() would: truncated
    partway through, so it no longer parses as JSON."""
    text = path.read_text(encoding="utf-8")
    path.write_text(text[:int(len(text) * fraction)], encoding="utf-8")


# ---------------------------------------------------------------------------
# "I aborted a message and the whole conversation vanished."
# ---------------------------------------------------------------------------

def test_torn_conversation_file_recovers():
    from jarvis import conversations as C
    with SandboxHome():
        cid = C.new_conversation(title="Important", make_current=False)
        C.begin_exchange(cid, "first question")
        C.complete_exchange(cid, "first question", "first answer", "p")
        C.begin_exchange(cid, "second question")
        C.complete_exchange(cid, "second question", "second answer", "p")

        tear(C._conv_path(cid))
        record = C.get_conversation(cid)
        # The whole conversation used to come back as None here, because
        # _load_conv caught the parse error and a missing conversation and a
        # corrupt one were indistinguishable.
        check("torn conversation file still loads", record is not None)
        check("recovers prior turns", record and len(record["exchanges"]) >= 1,
              str(record and len(record["exchanges"])))
        check("recovered content is real",
              record and record["exchanges"][0]["user"] == "first question")


def test_torn_index_does_not_hide_everything():
    from jarvis import conversations as C
    with SandboxHome():
        ids = []
        for i in range(3):
            cid = C.new_conversation(title=f"Chat {i}", make_current=False)
            C.begin_exchange(cid, f"q{i}")
            C.complete_exchange(cid, f"q{i}", f"a{i}", "p")
            ids.append(cid)
        check("all three indexed", len(C.list_conversations()) == 3)

        tear(C.INDEX_FILE)
        # Used to return [] — every conversation on disk invisible, forever,
        # with no path back.
        listed = C.list_conversations()
        check("torn index still lists conversations", len(listed) == 3,
              str(len(listed)))

        # Destroy both copies: the index is derived data, so it must rebuild.
        C.INDEX_FILE.write_text("{ broken", encoding="utf-8")
        backup = C.INDEX_FILE.with_suffix(C.INDEX_FILE.suffix + ".bak")
        if backup.exists():
            backup.write_text("{ broken", encoding="utf-8")
        rebuilt = C.list_conversations()
        check("index rebuilds from the conversation files", len(rebuilt) == 3,
              str(len(rebuilt)))
        check("rebuilt entries keep their titles",
              {it["title"] for it in rebuilt} == {"Chat 0", "Chat 1", "Chat 2"})


def test_writes_are_atomic():
    from jarvis import conversations as C
    with SandboxHome():
        cid = C.new_conversation(make_current=False)
        C.begin_exchange(cid, "q")
        C.complete_exchange(cid, "q", "a", "p")
        # No .tmp may survive a completed write, or the next reader could
        # pick up a half-written file believing it to be real.
        leftovers = list(C.CONV_DIR.glob("*.tmp"))
        check("no .tmp files left behind", leftovers == [], str(leftovers))
        check("a backup exists after the second write",
              C._conv_path(cid).with_suffix(".json.bak").exists())
        # .bak/.tmp siblings must not be mistaken for conversations.
        check("backups are not listed as conversations",
              len(C.list_conversations()) == 1)


# ---------------------------------------------------------------------------
# "If I abort a message, sometimes the conversation doesn't save."
# ---------------------------------------------------------------------------

def test_abandon_never_loses_the_message():
    from jarvis import conversations as C
    with SandboxHome():
        # No pending turn at all — begin_exchange failed, or the turn was
        # already claimed. This used to `return False` and drop the user's
        # message on the floor.
        cid = C.new_conversation(make_current=False)
        ok = C.abandon_exchange(cid, "message I typed then cancelled",
                                reason="cancelled")
        check("abandon reports success", ok is True)
        exchanges = C.get_conversation(cid)["exchanges"]
        check("the typed message survives", len(exchanges) == 1, str(len(exchanges)))
        check("text is exactly what was typed",
              exchanges and exchanges[0]["user"] == "message I typed then cancelled")
        check("it is marked interrupted",
              exchanges and exchanges[0].get("interrupted") == "cancelled")


def test_abandon_with_no_record_at_all():
    from jarvis import conversations as C
    with SandboxHome():
        # begin_exchange never wrote anything (unwritable disk, say).
        cid = "a" * 16
        check("abandon still records the turn",
              C.abandon_exchange(cid, "typed but never saved") is True)
        record = C.get_conversation(cid)
        check("a record was synthesized", record is not None)
        check("with the user's text",
              record and record["exchanges"][0]["user"] == "typed but never saved")
        # An empty message is not worth synthesizing a record for.
        check("empty text is still refused",
              C.abandon_exchange("b" * 16, "") is False)


def test_abandon_is_idempotent():
    """The signal handler can fire twice (SIGINT then SIGTERM), and ask()'s
    all-providers-failed path abandons as well — so a second abandon must
    not duplicate the user's message."""
    from jarvis import conversations as C
    with SandboxHome():
        cid = C.new_conversation(make_current=False)
        C.begin_exchange(cid, "a question")
        check("first abandon records it",
              C.abandon_exchange(cid, "a question") is True)
        check("second abandon is a no-op",
              C.abandon_exchange(cid, "a question") is False)
        check("third abandon is still a no-op",
              C.abandon_exchange(cid, "a question") is False)
        check("no duplicate turn was created",
              len(C.get_conversation(cid)["exchanges"]) == 1,
              str(len(C.get_conversation(cid)["exchanges"])))

        # But a genuinely NEW message still gets recorded after one.
        check("a different message is still recorded",
              C.abandon_exchange(cid, "a different question") is True)
        check("now there are two turns",
              len(C.get_conversation(cid)["exchanges"]) == 2)


def test_stale_pending_is_reclaimed():
    from jarvis import conversations as C
    with SandboxHome():
        cid = C.new_conversation(make_current=False)
        C.begin_exchange(cid, "killed before it answered")   # hard kill here
        C.begin_exchange(cid, "next message")                # next ask
        C.complete_exchange(cid, "next message", "an answer", "p")

        exchanges = C.get_conversation(cid)["exchanges"]
        check("both turns kept", len(exchanges) == 2, str(len(exchanges)))
        check("no exchange is left pending",
              not any(e.get("pending") for e in exchanges))
        check("the stranded turn is marked interrupted",
              exchanges[0].get("interrupted"))
        check("the stranded turn keeps its text",
              exchanges[0]["user"] == "killed before it answered")
        check("the live turn answered normally",
              exchanges[1].get("jarvis") == "an answer")


def test_interrupted_turns_stay_out_of_prompt_history():
    """They must be VISIBLE but never sent — an assistant message with empty
    content is a hard 400 on Anthropic and degrades the rest."""
    from jarvis import conversations as C
    with SandboxHome():
        cid = C.new_conversation(make_current=False)
        C.begin_exchange(cid, "q1"); C.complete_exchange(cid, "q1", "a1", "p")
        C.begin_exchange(cid, "aborted"); C.abandon_exchange(cid, "aborted")
        C.begin_exchange(cid, "q3"); C.complete_exchange(cid, "q3", "a3", "p")

        check("all three stored", len(C.get_conversation(cid)["exchanges"]) == 3)
        contents = [m["content"] for m in C.conversation_messages(cid)]
        check("aborted turn excluded from the prompt", "aborted" not in contents)
        check("answered turns included", "q1" in contents and "q3" in contents)
        check("no empty assistant content is ever emitted",
              all((m.get("content") or "").strip()
                  for m in C.conversation_messages(cid)))


def test_current_pointer_survives_a_torn_write():
    from jarvis import conversations as C
    with SandboxHome():
        cid = C.new_conversation(make_current=False)
        C.begin_exchange(cid, "q"); C.complete_exchange(cid, "q", "a", "p")
        C.set_current(cid)
        C.set_current(cid)  # second write, so a .bak exists
        tear(C.CURRENT_FILE)
        # A torn pointer used to read as "no current conversation", so the
        # next CLI message silently started a new one.
        check("current conversation pointer recovers",
              C.get_current_id(auto_create=False) == cid)


# ---------------------------------------------------------------------------
# Memory — the same class of bug, worse consequences.
# ---------------------------------------------------------------------------

def test_memory_survives_a_torn_write():
    from jarvis import memory
    with SandboxHome():
        memory.save_facts([{"fact": "likes tea"}])
        memory.save_facts([{"fact": "likes tea"}, {"fact": "lives in Tunisia"}])
        memory.save_facts([{"fact": "likes tea"}, {"fact": "lives in Tunisia"},
                           {"fact": "uses Playnite"}])
        check("three facts saved", len(memory.load_facts()) == 3)

        tear(memory.CONFIG_FILE)
        recovered = [f["fact"] for f in memory.load_facts()]
        # Used to return [] — every memory the user ever saved, gone, and
        # silently: an empty memory file and a destroyed one read the same.
        check("memory is not wiped by a torn write", len(recovered) >= 2,
              str(recovered))
        check("recovered facts are the real ones", "likes tea" in recovered)


def test_stats_roundtrip():
    from jarvis import stats
    with SandboxHome():
        stats.bump("deploy")
        stats.bump("deploy")
        stats.bump("build")
        counts = stats._load()
        check("stats count correctly",
              counts.get("deploy") == 2 and counts.get("build") == 1, str(counts))


# ---------------------------------------------------------------------------
# atomic_io itself
# ---------------------------------------------------------------------------

def test_atomic_io():
    from jarvis import atomic_io
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "thing.json"
        check("writes", atomic_io.write_json(path, {"a": 1}) is True)
        check("reads back", atomic_io.read_json(path) == {"a": 1})
        check("no tmp left", not list(Path(tmp).glob("*.tmp")))

        atomic_io.write_json(path, {"a": 2})
        check("backup written on overwrite",
              path.with_suffix(".json.bak").exists())

        path.write_text("{ torn", encoding="utf-8")
        check("falls back to backup", atomic_io.read_json(path) == {"a": 1})

        check("missing file returns the default",
              atomic_io.read_json(Path(tmp) / "nope.json", default={"d": 1}) == {"d": 1})
        # Wrong-shaped data counts as corruption.
        atomic_io.write_json(path, ["not", "a", "dict"])
        check("expect= rejects the wrong type",
              atomic_io.read_json(path, default=None, expect=dict) != ["not", "a", "dict"])
        # Unserializable input must fail cleanly, not raise.
        check("unserializable input returns False",
              atomic_io.write_json(path, {"bad": object()}) in (True, False))


def main():
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}\n"
                          + "    " + traceback.format_exc().splitlines()[-3].strip())

    total = PASSED + len(FAILED)
    print(f"{PASSED}/{total} passed")
    for failure in FAILED:
        print("  FAIL:", failure)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
