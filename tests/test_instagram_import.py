"""Tests for the Instagram data-export importer (channel spaces S9b).

    python3 tests/test_instagram_import.py

`channels/instagram_import.py` reads a "Download your information" messages export and
writes the GROUP chats in it (3 or more people) as transcript lines plus a spaces.json
entry. It grants nothing: no person, no permission, no conversation, no usage line.

The export here is built by the test from Meta's documented layout (it is NOT a real
export; see the module docstring's FORMAT RISK). No network, no model, no real ~/.jarvis:
HOME is redirected to a temp dir BEFORE any jarvis module is imported (AGENTS.md).
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-igimport-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
_CLI = str(Path(__file__).resolve().parent.parent / "jarvis-cli")
sys.path.insert(0, _CLI)

from jarvis.channels import INSTAGRAM, config as channel_config                # noqa: E402
from jarvis.channels import (denied, instagram_import, people, space,          # noqa: E402
                             space_view, spaces, transcript, user_perms)

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


STATE = Path(_HOME) / ".jarvis"
EXPORT = Path(_HOME) / "export"
T0 = 1_600_000_000_000          # 2020-09-13 UTC, in ms
SENTINELS = {
    "people": people.PEOPLE_FILE, "channels": channel_config.CONFIG_FILE,
    "perms": user_perms.PERMS_FILE,
}
NEVER_CREATED = (transcript.MAP_FILE, STATE / "channels" / "usage.jsonl",
                 STATE / "channels" / "changes.jsonl")


def mojibake(text):
    """What Meta's JSON does to non-ASCII text: UTF-8 bytes shown as Latin-1 characters."""
    return text.encode("utf-8").decode("latin-1")


def msg(sender, text, offset_s, **extra):
    out = {"sender_name": mojibake(sender), "timestamp_ms": T0 + offset_s * 1000,
           "type": "Generic"}
    if text is not None:
        out["content"] = mojibake(text)
    out.update(extra)
    return out


def write_chat(folder, participants, files, title=""):
    """files: [[message, ...], ...], each list NEWEST FIRST like the real export."""
    base = EXPORT / "your_instagram_activity" / "messages" / "inbox" / folder
    base.mkdir(parents=True, exist_ok=True)
    for index, messages in enumerate(files, 1):
        data = {"participants": [{"name": mojibake(p)} for p in participants],
                "messages": messages, "title": mojibake(title),
                "is_still_participant": True, "thread_path": f"inbox/{folder}"}
        (base / f"message_{index}.json").write_text(json.dumps(data), encoding="utf-8")
    return base


def build_export():
    shutil.rmtree(EXPORT, ignore_errors=True)
    write_chat("weekendcrew_1111", ["Alice", "Bob", "Chloé"], [
        [msg("Chloé", "café at 5? ☕", 300),
         msg("Bob", None, 250, photos=[{"uri": "x.jpg"}]),           # photo only: no text
         msg("Alice", "Alice started a call", 200, type="Call"),      # system event
         msg("Alice", "see you 😀", 100),
         {"sender_name": "Bob", "timestamp_ms": "garbage", "content": "bad time"}],
        [msg("Bob", "hello everyone", 50)],
    ], title="Weekend Crew")
    write_chat("bigclub_3333", ["Dan", "Eve", "Frank", "Gina"],
               [[msg("Gina", "meeting moved", 900), msg("Dan", "ok", 800)]], title="Big Club 🎉")
    write_chat("alice_2222", ["Me", "Alice"],
               [[msg("Alice", "secret dm text", 10), msg("Me", "hi", 5)]])          # a DM
    (EXPORT / "your_instagram_activity" / "messages" / "message_requests").mkdir(parents=True,
                                                                              exist_ok=True)
    write_chat("../message_requests/stranger_4444", ["Me", "Stranger"],
               [[msg("Stranger", "dm from a stranger", 20)]])


def reset():
    shutil.rmtree(STATE, ignore_errors=True)
    transcript.MAX_BYTES = 2_000_000
    build_export()


def tree(root):
    """{relative path: sha256} of every file under root (None for a missing root)."""
    root = Path(root)
    if not root.exists():
        return None
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def state_files():
    return sorted((tree(STATE) or {}).keys())


def lines(thread):
    return transcript._read_records(transcript._thread_files(INSTAGRAM, thread))


def by_title(result, title):
    return next((c for c in result["chats"] if c["title"] == title), None)


def test_text_and_ids():
    fix = instagram_import._fix_text
    check("double-encoded text is repaired", fix(mojibake("café ☕ 😀")) == "café ☕ 😀")
    check("correct text is left alone", fix("café") == "café")
    check("text with a character above U+00FF is left alone", fix("naïve — ok") == "naïve — ok")
    check("ascii and non-strings", fix("plain") == "plain" and fix(None) == "" and fix(5) == "")
    tid = instagram_import.thread_id_for("weekendcrew_1111")
    check("thread id shape", len(tid) == 15 and tid.startswith("import-")
          and all(c in "0123456789abcdef" for c in tid[7:]), tid)
    check("thread id is stable and per-folder",
          tid == instagram_import.thread_id_for("weekendcrew_1111")
          and tid != instagram_import.thread_id_for("bigclub_3333"))
    check("thread id is a safe transcript name", transcript._safe_name(tid) == tid)
    uid = instagram_import.user_id_for("Alice")
    check("user id is name:<name>", uid == "name:Alice", uid)
    a = instagram_import.user_id_for("X" * 60 + "one")
    b = instagram_import.user_id_for("X" * 60 + "two")
    check("a long name stays within the id limit and two long names stay two people",
          len(a) <= space.MAX_ID_CHARS and len(b) <= space.MAX_ID_CHARS and a != b, f"{a} / {b}")


def test_preview_writes_nothing():
    reset()
    out = instagram_import.run(EXPORT)
    check("preview ok", out["ok"] and out["mode"] == "preview", str(out))
    check("preview wrote nothing, not even the channels folder",
          not (STATE / "channels").exists() and not spaces.registry_file().exists(),
          str(state_files()))
    check("two group chats found", out["totals"]["chats"] == 2, str(out["totals"]))
    check("the DM and the message request were skipped, not read",
          out["totals"]["skipped_chats"] == 2
          and {c["folder"] for c in out["skipped_chats"]} == {"alice_2222", "stranger_4444"},
          str(out["skipped_chats"]))
    crew = by_title(out, "Weekend Crew")
    check("per-chat counts", crew and crew["messages"] == 3 and crew["new"] == 3
          and crew["people"] == 3, str(crew))
    check("system, empty and bad-time lines are counted as skipped",
          crew["skipped"] == {"no_text": 1, "system": 1, "bad_time": 1}, str(crew["skipped"]))
    check("date range", crew["first_at"] < crew["last_at"]
          and crew["first_at"].startswith("2020-09-1"), str(crew))
    check("emoji title survives", by_title(out, "Big Club 🎉") is not None)
    check("the secret DM text is nowhere in the result", "secret dm text" not in json.dumps(out))


def test_import_writes_group_lines_only():
    reset()
    for key, path in SENTINELS.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sentinel": key}), encoding="utf-8")
    before = {k: p.read_bytes() for k, p in SENTINELS.items()}
    out = instagram_import.run(EXPORT, confirm=True)
    check("import ok", out["ok"] and out["mode"] == "import", str(out))
    crew = by_title(out, "Weekend Crew")
    check("lines were written", crew and crew["written"] == 3 and out["totals"]["written"] == 5,
          str(out["totals"]))
    recs = lines(crew["thread_id"])
    check("one line per importable message, oldest first",
          [r["text"] for r in recs] == ["hello everyone", "see you 😀", "café at 5? ☕"],
          str([r["text"] for r in recs]))
    first = recs[1]
    check("record shape", first["dir"] == "in" and first["context"] == "group"
          and first["space"] == "group" and first["imported"] is True
          and first["source"] == "export" and first["mentioned"] is False
          and first["user_id"] == "name:Alice" and first["author_name"] == "Alice"
          and first["user_handle"] == "" and first["thread_name"] == "Weekend Crew", str(first))
    check("the original time is kept", datetime.fromisoformat(first["at"]).timestamp()
          == (T0 + 100 * 1000) // 1000 and first["ts_ms"] == T0 + 100 * 1000, str(first))
    check("no gate decision, no conversation on an imported line",
          not any(k in first for k in ("allowed", "stage", "reason", "may_use_tools", "conv_id")))
    check("non-ASCII text is stored as real characters", recs[2]["author_name"] == "Chloé"
          and "Chloé" in transcript.thread_path(INSTAGRAM, crew["thread_id"])
          .read_text(encoding="utf-8"))
    reg = spaces.get(INSTAGRAM, crew["thread_id"])
    check("registered as an imported group", reg and reg["kind"] == "group"
          and reg["imported"] is True and reg["title"] == "Group 'Weekend Crew'"
          and set(reg["speakers"]) == {"name:Alice", "name:Bob", "name:Chloé"}, str(reg))
    check("registry holds no message text", "café" not in json.dumps(reg))
    check("nothing but the two thread logs and spaces.json was written",
          state_files() == sorted(
              ["channels/spaces.json"]
              + [f"channels/instagram/{c['thread_id']}.jsonl" for c in out["chats"]]
              + [str(p.relative_to(STATE)).replace(os.sep, "/") for p in SENTINELS.values()]),
          str(state_files()))
    check("people, channels and permissions files are byte-identical",
          all(p.read_bytes() == before[k] for k, p in SENTINELS.items()))
    check("no usage, change log or conversation map", not any(p.exists() for p in NEVER_CREATED))
    check("the DM text never reached disk", not any(
        "secret dm text" in (STATE / rel).read_text(encoding="utf-8", errors="ignore")
        or "dm from a stranger" in (STATE / rel).read_text(encoding="utf-8", errors="ignore")
        for rel in state_files()))
    check("nobody was turned into a person", people.get(INSTAGRAM, "name:Alice") is None)


def test_idempotent_and_gap_filling():
    reset()
    first = instagram_import.run(EXPORT, confirm=True)
    snapshot = tree(STATE)
    again = instagram_import.run(EXPORT, confirm=True)
    check("a second run adds nothing", again["totals"]["written"] == 0
          and again["totals"]["new"] == 0
          and again["totals"]["duplicates"] == first["totals"]["messages"], str(again["totals"]))
    check("and changes no file", tree(STATE) == snapshot)
    preview = instagram_import.run(EXPORT)
    check("a preview after an import reports everything as already there",
          preview["totals"]["new"] == 0)
    # The export grows an OLDER message (a gap) and a newer one.
    write_chat("weekendcrew_1111", ["Alice", "Bob", "Chloé"], [
        [msg("Chloé", "café at 5? ☕", 300), msg("Alice", "see you 😀", 100),
         msg("Bob", "late addition", 400)],
        [msg("Bob", "hello everyone", 50), msg("Alice", "older still", 10)],
    ], title="Weekend Crew")
    grown = instagram_import.run(EXPORT, confirm=True)
    crew = by_title(grown, "Weekend Crew")
    check("only the two new lines are written", crew["new"] == 2 and crew["written"] == 2, str(crew))
    texts = [r["text"] for r in lines(crew["thread_id"])]
    check("no line is stored twice", sorted(texts) == sorted(set(texts)) and len(texts) == 5,
          str(texts))
    reg = spaces.get(INSTAGRAM, crew["thread_id"])
    check("the registry merged: earliest first_seen, latest last_seen",
          reg["first_seen"] == crew["first_at"] and reg["last_seen"] == crew["last_at"]
          and reg["first_seen"] < reg["last_seen"], str(reg))
    shown = [e["text"] for e in space_view.view(INSTAGRAM, crew["thread_id"])["entries"]]
    check("the viewer shows a filled gap in time order, not file order",
          shown == ["older still", "hello everyone", "see you 😀", "café at 5? ☕", "late addition"],
          str(shown))
    check("the file itself is only ever appended to: the first run's lines are untouched",
          [r["text"] for r in lines(crew["thread_id"])]
          == ["hello everyone", "see you 😀", "café at 5? ☕", "older still", "late addition"])


def test_group_threshold_uses_everyone_seen():
    reset()
    shutil.rmtree(EXPORT, ignore_errors=True)
    # Participants list says two, but three different people wrote: a group.
    write_chat("sparse_5555", ["Me", "Ann"], [[msg("Ann", "a", 1), msg("Bob", "b", 2),
                                              msg("Me", "c", 3)]], title="Sparse")
    # Two people, however many messages: a DM.
    write_chat("pair_6666", ["Me", "Ann"], [[msg("Ann", "x", 1), msg("Me", "y", 2)]])
    out = instagram_import.run(EXPORT)
    check("a chat is a group when 3+ people are seen anywhere in it",
          [c["title"] for c in out["chats"]] == ["Sparse"], str(out["chats"]))
    check("the pair is skipped", [c["folder"] for c in out["skipped_chats"]] == ["pair_6666"])
    check("MIN_PEOPLE is three", instagram_import.MIN_PEOPLE == 3)


def test_listing_order_is_real_time():
    reset()
    transcript.append(INSTAGRAM, "dm-live", {"dir": "in", "context": "dm", "user_id": "42",
                                              "text": "hi", "allowed": True})
    instagram_import.run(EXPORT, confirm=True)
    names = [t["thread_id"] for t in transcript.list_threads(platform=INSTAGRAM)]
    check("a real recent DM still sorts first after a big import", names[0] == "dm-live", str(names))
    crew = next(t for t in transcript.list_threads(platform=INSTAGRAM)
                if t["thread_id"] == instagram_import.thread_id_for("weekendcrew_1111"))
    check("an imported thread's modified time is its newest message, not the import time",
          crew["modified"].startswith("2020-09-1"), crew["modified"])


def test_rotation_never_loses_lines():
    reset()
    transcript.MAX_BYTES = 300          # force a rotation every couple of lines, inside one second
    shutil.rmtree(EXPORT, ignore_errors=True)
    many = [msg(["Ann", "Bob", "Cy"][i % 3], f"message number {i} " + "x" * 40, i) for i in range(60)]
    write_chat("busy_7777", ["Ann", "Bob", "Cy"], [list(reversed(many))], title="Busy")
    out = instagram_import.run(EXPORT, confirm=True)
    tid = out["chats"][0]["thread_id"]
    files = transcript._thread_files(INSTAGRAM, tid)
    check("the log really rotated", len(files) > 3, str(len(files)))
    texts = [r["text"] for r in lines(tid)]
    expected = [f"message number {i} " + "x" * 40 for i in range(60)]
    check("every line survived, in order", texts == expected, str(len(texts)))
    again = instagram_import.run(EXPORT, confirm=True)
    check("rotated files count for idempotency", again["totals"]["written"] == 0)
    view = space_view.view(INSTAGRAM, tid, limit=500)
    check("the read side sees the rotated history", view["total"] == 60, str(view["total"]))
    transcript.MAX_BYTES = 2_000_000


def test_rotation_does_not_overwrite_an_existing_rotated_file():
    reset()
    transcript.MAX_BYTES = 50
    path = transcript.thread_path(INSTAGRAM, "rot-room")
    for i in range(12):
        transcript.append(INSTAGRAM, "rot-room", {"dir": "in", "text": f"line {i} " + "y" * 30})
    seen = [r["text"] for r in transcript._read_records(transcript._thread_files(INSTAGRAM, "rot-room"))]
    check("twelve rapid appends, twelve lines kept", len(seen) == 12 and len(set(seen)) == 12, str(len(seen)))
    check("rotation happened", len(list(path.parent.glob("rot-room.*.jsonl"))) >= 2)
    transcript.MAX_BYTES = 2_000_000


def test_spaces_rebuild_keeps_imported_rooms():
    reset()
    out = instagram_import.run(EXPORT, confirm=True)
    crew = by_title(out, "Weekend Crew")
    before = spaces.get(INSTAGRAM, crew["thread_id"])
    spaces.rebuild(INSTAGRAM)
    after = spaces.get(INSTAGRAM, crew["thread_id"])
    check("a rebuild keeps the room, its title and its imported mark",
          after and after["imported"] is True and after["kind"] == "group"
          and after["title"] == before["title"], str(after))
    check("and its speakers", set(after["speakers"]) == set(before["speakers"]))
    os.remove(spaces.registry_file())
    rebuilt = spaces.rebuild(INSTAGRAM)
    check("a lost registry is rebuilt from the lines, still marked imported",
          rebuilt.get(crew["thread_id"], {}).get("imported") is True, str(rebuilt))


def test_other_readers_are_unaffected():
    reset()
    instagram_import.run(EXPORT, confirm=True)
    view = denied.denied_view(INSTAGRAM)
    check("imported lines are not turned-away senders", view.get("senders") == [], str(view))
    crew_id = instagram_import.thread_id_for("weekendcrew_1111")
    found = space_view.view(INSTAGRAM, crew_id)
    check("the viewer counts the speakers", {p["user_id"]: p["messages"]
          for p in found["participants"]} == {"name:Alice": 1, "name:Bob": 1, "name:Chloé": 1},
          str(found["participants"]))
    check("the viewer marks the lines imported", found["imported"] is True
          and all(e.get("imported") for e in found["entries"]))
    check("nothing is counted as denied or unattributed", found["denied"] == 0
          and found["unattributed_replies"] == 0)
    check("no imported thread is a DM", all(
        t["space"] != "dm" for t in transcript.list_threads(platform=INSTAGRAM, details=True)))


def test_bad_input_is_reported_not_raised():
    reset()
    out = instagram_import.run(Path(_HOME) / "nowhere")
    check("a missing path is a clean failure", out["ok"] is False and out["problems"], str(out))
    zipped = Path(_HOME) / "export.zip"
    zipped.write_bytes(b"PK\x03\x04 not really")
    out = instagram_import.run(zipped)
    check("a zip is refused with a hint", out["ok"] is False and "unzip" in " ".join(out["problems"]),
          str(out))
    empty = Path(_HOME) / "empty"
    empty.mkdir()
    out = instagram_import.run(empty)
    check("a folder with no message files", out["ok"] is False)
    # One broken file must not stop the other chats.
    bad = EXPORT / "your_instagram_activity" / "messages" / "inbox" / "broken_9999"
    bad.mkdir(parents=True)
    (bad / "message_1.json").write_text("{not json", encoding="utf-8")
    (bad / "message_2.json").write_text(json.dumps({"messages": "nope"}), encoding="utf-8")
    out = instagram_import.run(EXPORT, confirm=True)
    check("the good chats still import", out["ok"] and out["totals"]["chats"] == 2, str(out["totals"]))
    check("the broken files are named", sum("broken_9999" in p for p in out["problems"]) == 2,
          str(out["problems"]))
    one = instagram_import.run(EXPORT / "your_instagram_activity" / "messages" / "inbox"
                               / "bigclub_3333" / "message_1.json")
    check("a single message_N.json works", one["ok"] and one["totals"]["chats"] == 1, str(one))
    junk = Path(_HOME) / "notes.json"
    junk.write_text("[]", encoding="utf-8")
    check("a file that is not a message file is refused", instagram_import.run(junk)["ok"] is False)


def _cli(*args):
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONPATH=_CLI, JARVIS_RAW_ARCHIVE="0")
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], capture_output=True, text=True,
                          env=env, timeout=120)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"raw": proc.stdout + proc.stderr}


def test_cli():
    reset()
    channels_dir = STATE / "channels"
    code, out = _cli("channels-import-instagram", str(EXPORT))
    check("the CLI previews by default", code == 0 and out.get("mode") == "preview"
          and "--yes" in out.get("hint", ""), str(out)[:300])
    check("and writes no log", not (channels_dir / "instagram").exists()
          and not (channels_dir / "spaces.json").exists())
    code, out = _cli("channels-import-instagram", str(EXPORT), "--yes")
    check("--yes imports", code == 0 and out.get("mode") == "import"
          and out["totals"]["written"] == 5, str(out)[:300])
    check("and the files exist", len(list((channels_dir / "instagram").glob("import-*.jsonl"))) == 2)
    code, out = _cli("channels-import-instagram", str(EXPORT), "--yes")
    check("the CLI is idempotent", code == 0 and out["totals"]["written"] == 0, str(out)[:300])
    code, out = _cli("channels-import-instagram")
    check("no path prints usage", code != 0 and "usage" in json.dumps(out))
    code, out = _cli("channels-import-instagram", str(EXPORT), "--force")
    check("an unknown flag is refused", code != 0 and "usage" in json.dumps(out))
    code, out = _cli("channels-import-instagram", str(Path(_HOME) / "nope"))
    check("a missing path exits non-zero", code != 0 and out.get("ok") is False)
    code, out = _cli("channels-spaces", "instagram", "--kind", "group")
    check("the imported rooms show in channels-spaces", code == 0 and out["count"] == 2
          and all(r["imported"] for r in out["spaces"]), str(out)[:300])
    src = (Path(__file__).resolve().parent.parent / "jarvis-cli" / "jarvis"
           / "reserved_names.py").read_text()
    check("the command is a reserved name", "channels-import-instagram" in src)


def test_module_grants_nothing_by_construction():
    src = Path(instagram_import.__file__).read_text(encoding="utf-8")
    for needle in ("import people", "user_perms", "config", "permissions.decide", "conversations",
                   "spaces.note", "usage", "ai_client", "requests", "urllib", "socket"):
        check(f"instagram_import does not touch '{needle}'",
              needle not in src.split('"""', 2)[2], needle)


def main():
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}\n"
                          + traceback.format_exc(limit=4))
    total = PASSED + len(FAILED)
    print(f"{PASSED}/{total} passed")
    for failure in FAILED:
        print("  FAIL:", failure)
    shutil.rmtree(_HOME, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
