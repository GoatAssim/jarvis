"""Regression tests for K.2.7 — notification importance levels (D.2.1) +
clipboard flood protection, and Part J's burst/digest acceptance cases.

Covers:
  K.2.7.1  the level system itself: normalize_level()'s lookup order,
           per-kind and per-custom-tool-source defaults, legacy word
           aliases, and notify()'s resulting channel/flag resolution.
  K.2.7.1a a custom tool/action's `source` can carry its own default level
           the same way a built-in `kind` does.
  K.2.7.1b schedule_task/remind_me/notify_me/schedule_watch accept an
           optional `level` and thread it through to the fired notification.
  K.2.7.2  clipboard burst coalescing — bounded pushed notifications, a
           truthful coalesced count, an isolated change still arriving
           promptly, level 3+ never suppressed, and every individual event
           still landing in the durable inbox.
  K.2.7.3 / Part J  digest.py consumes the same normalized level as the
           single source of truth (should_batch_level), with levels 1-2
           digest-eligible and 3-5 never batched.

No network, no live model, no real time.sleep.

Run: python3 tests/test_notification_levels.py
"""

import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp(prefix="jarvis_k27_"))

from jarvis import notifier, digest, clipboard_watch as cw, scheduler  # noqa: E402
from jarvis.actions import scheduler_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


# --- K.2.7.1: the level system ----------------------------------------------

def test_explicit_numeric_level_wins():
    check("explicit level 5 resolves to 5", notifier.normalize_level(5) == 5)
    check("explicit level 1 resolves to 1", notifier.normalize_level(1, kind="task") == 1)


def test_legacy_words_map_onto_the_new_scale():
    check("'high' maps to 3 (never-suppressed)", notifier.normalize_level("high") == 3)
    check("'low' maps to 1 (silent)", notifier.normalize_level("low") == 1)
    check("'normal' maps to 2 (standard)", notifier.normalize_level("normal") == 2)
    check("'urgent' maps to 3", notifier.normalize_level("urgent") == 3)


def test_out_of_range_falls_back_to_kind_default():
    check("level 0 is invalid, falls back", notifier.normalize_level(0, kind="reminder") == 2)
    check("level 9 is invalid, falls back", notifier.normalize_level(9, kind="clipboard_watch") == 2)


def test_per_kind_defaults():
    check("reminder default is 2 (standard)", notifier.normalize_level(None, kind="reminder") == 2)
    check("clipboard_watch default is 2 (standard)",
          notifier.normalize_level(None, kind="clipboard_watch") == 2)
    check("ambient default is 1 (silent)", notifier.normalize_level(None, kind="ambient") == 1)
    check("unknown kind falls back to global default (2)",
          notifier.normalize_level(None, kind="totally_unknown_kind") == 2)


def test_custom_tool_source_can_declare_its_own_default():
    config = {"levels": {"my_custom_tool": 4}}
    check("a custom tool's `source` default is honored (K.2.7.1a)",
          notifier.normalize_level(None, kind="notify", source="my_custom_tool", config=config) == 4)
    check("source takes priority over kind when both are configured",
          notifier.normalize_level(None, kind="reminder", source="my_custom_tool", config=config) == 4)


def test_channels_for_level_are_cumulative():
    check("level 1 is inbox only", cw.__name__ and notifier._channels_for_level(1) == ["inbox"])
    lvl2 = notifier._channels_for_level(2)
    check("level 2 adds stream+toast", "stream" in lvl2 and "toast" in lvl2 and "inbox" in lvl2)
    lvl4 = notifier._channels_for_level(4)
    check("level 4 adds cross-channel broadcast",
          "discord" in lvl4 and "instagram" in lvl4 and "toast" in lvl4)


def test_notify_sets_level_flags_on_the_record():
    r1 = notifier.notify("t", "m", level=1)
    check("level 1 record: not persistent, no confirm required",
          r1["level"] == 1 and not r1["persistent"] and not r1["confirm_required"])
    check("level 1 delivers to inbox only", r1["delivered"] == ["inbox"], r1["delivered"])

    r3 = notifier.notify("t", "m", level=3)
    check("level 3 record: persistent, no confirm required",
          r3["persistent"] and not r3["confirm_required"])

    r5 = notifier.notify("t", "m", level=5)
    check("level 5 record: persistent AND confirm required",
          r5["persistent"] and r5["confirm_required"])

    check("level_name is set", r1["level_name"] == "silent" and r5["level_name"] == "confirm")


def test_explicit_channels_still_override_the_level():
    r = notifier.notify("t", "m", level=5, channels=["inbox"])
    check("an explicit channels= list overrides the level-derived set",
          r["delivered"] == ["inbox"], r["delivered"])
    check("but the level's flags are still recorded", r["confirm_required"] is True)


def test_source_is_recorded_on_the_record():
    r = notifier.notify("t", "m", source="my_custom_tool")
    check("source is tagged on the record", r.get("source") == "my_custom_tool")


# --- K.2.7.1b: scheduled reminders/tasks gain a level parameter -------------

def test_scheduler_create_accepts_and_validates_level():
    job = scheduler.create(kind="reminder", title="t", when="in 1 hour",
                            message="hi", level=4, trusted=True)
    check("scheduler.create stores the given level", job.get("level") == 4)

    try:
        scheduler.create(kind="reminder", title="t", when="in 1 hour",
                          message="hi", level=9, trusted=True)
        check("level 9 is rejected by scheduler.create", False, "no error raised")
    except scheduler.SchedulerError:
        check("level 9 is rejected by scheduler.create", True)


def test_scheduler_tools_expose_level_argument():
    for tool_name, schema_name in (
        ("tool_remind_me", "remind_me"), ("tool_notify_me", "notify_me"),
        ("tool_schedule_task", "schedule_task"), ("tool_schedule_watch", "schedule_watch"),
    ):
        schema = next((s for s in scheduler_tools.TOOL_SCHEMAS if s["name"] == schema_name), None)
        check(f"{schema_name}'s schema declares a level property",
              schema is not None and "level" in schema["parameters"]["properties"])

    job = scheduler_tools.tool_remind_me(
        {"message": "call mum", "when": "in 1 hour", "level": 4})
    stored = scheduler.get(job.get("id")) if job.get("id") else None
    check("remind_me's level argument reaches the created job",
          bool(stored) and stored.get("level") == 4, job)


def test_notify_me_immediate_form_passes_level_through():
    result = scheduler_tools.tool_notify_me({"message": "done", "level": 1})
    check("notify_me (immediate) returns ok", result.get("ok") is True, result)
    check("notify_me (immediate) delivered only to inbox at level 1",
          result.get("delivered") == ["inbox"], result)


# --- K.2.7.2 / Part J: clipboard burst coalescing ---------------------------

def test_isolated_change_pushes_immediately():
    decision = cw._burst_decision(level=2, now=1000.0, last_push_at=0.0,
                                   pending=None, coalesce_window=3.0)
    check("a lone change with no recent push arrives promptly (pushes)",
          decision == "push")


def test_burst_is_bounded_to_one_push_per_window():
    """A synthetic burst of many matching changes inside one window must
    produce exactly one live push, with every change still recorded."""
    level = 2
    coalesce_window = 3.0
    last_push_at = 0.0
    pending = None
    pushes = 0
    durable_records = 0

    # First change: isolated, pushes immediately.
    now = 100.0
    decision = cw._burst_decision(level, now, last_push_at, pending, coalesce_window)
    check("first change of a burst pushes", decision == "push")
    pushes += 1
    durable_records += 1
    last_push_at = now
    pending = None

    # Next 9 changes arrive within the same coalescing window.
    for i in range(1, 10):
        now = 100.0 + i * 0.2  # every 200ms — well inside the 3s window
        decision = cw._burst_decision(level, now, last_push_at, pending, coalesce_window)
        check(f"burst change #{i} buffers instead of pushing", decision == "buffer")
        durable_records += 1
        pending = cw._buffer_change(pending, f"change {i}", False, now)

    check("nothing has pushed again yet mid-burst", pushes == 1)

    # Burst goes quiet; window elapses.
    flush_now = now + coalesce_window + 0.5
    flushed = cw._maybe_flush(pending, flush_now, coalesce_window)
    check("the buffered burst flushes exactly once after going quiet", flushed is not None)
    if flushed:
        title, message = cw._flush_title_and_message(flushed)
        pushes += 1
        check("the flush reports a truthful coalesced count",
              str(flushed["count"]) in title or str(flushed["count"]) in message,
              f"title={title!r} message={message!r}")
        check("the flush count matches the number of buffered changes",
              flushed["count"] == 9, flushed["count"])

    check("total pushes for a 10-change burst is bounded to 2 (not 10)",
          pushes == 2, pushes)
    check("every individual change still got a durable record",
          durable_records == 10, durable_records)


def test_level_3_bypasses_coalescing_entirely():
    pending = {"count": 5, "preview": "x", "truncated": False, "since": 100.0}
    decision = cw._burst_decision(level=3, now=100.1, last_push_at=100.0,
                                   pending=pending, coalesce_window=3.0)
    check("level 3+ always pushes, even mid-burst (never suppressed)",
          decision == "push")


def test_coalesced_message_is_truthful_not_just_the_latest():
    pending = cw._buffer_change(None, "first", False, 0)
    pending = cw._buffer_change(pending, "second", False, 1)
    pending = cw._buffer_change(pending, "third", True, 2)
    title, message = cw._flush_title_and_message(pending)
    check("coalesced message names the count", "3" in title)
    check("coalesced message shows the LATEST preview, truncation included",
          "third" in message and "…" in message)


# --- K.2.7.3 / Part J: digest.py shares the normalized level ----------------

def test_should_batch_level_matches_the_old_low_normal_high_contract():
    cfg_on = {"enabled": True}
    cfg_off = {"enabled": False}
    check("level 1 batches when digest is on", digest.should_batch_level(1, cfg_on) is True)
    check("level 2 batches when digest is on", digest.should_batch_level(2, cfg_on) is True)
    check("level 3 never batches (never-suppressed)", digest.should_batch_level(3, cfg_on) is False)
    check("level 4 never batches", digest.should_batch_level(4, cfg_on) is False)
    check("level 5 never batches", digest.should_batch_level(5, cfg_on) is False)
    check("nothing batches when digest is off", digest.should_batch_level(1, cfg_off) is False)


def test_low_level_notification_defers_to_digest_when_enabled():
    digest.save_config({"enabled": True})
    try:
        before = len(digest._load_queue())
        r = notifier.notify("Low prio", "batch me", level=1)
        after = len(digest._load_queue())
        check("a level-1 notification is enqueued to digest when digest is on",
              after == before + 1, f"before={before} after={after}")
        check("it still gets an immediate durable inbox record",
              "inbox" in r["delivered"], r["delivered"])
        check("the record is flagged as deferred to digest",
              r.get("deferred_to_digest") is True)
    finally:
        digest.save_config({"enabled": False})


def test_high_level_notification_never_defers_to_digest():
    digest.save_config({"enabled": True})
    try:
        before = len(digest._load_queue())
        r = notifier.notify("Urgent", "not a batch", level=3)
        after = len(digest._load_queue())
        check("a level-3 notification is never enqueued to digest",
              after == before, f"before={before} after={after}")
        check("it delivers immediately instead", "inbox" in r["delivered"])
        check("it is not marked as deferred", not r.get("deferred_to_digest"))
    finally:
        digest.save_config({"enabled": False})


def test_explicit_channels_bypass_digest_deferral():
    # An explicit channels= override is a deliberate, specific delivery
    # choice — it should not be silently redirected into a digest queue.
    digest.save_config({"enabled": True})
    try:
        before = len(digest._load_queue())
        notifier.notify("t", "m", level=1, channels=["inbox"])
        after = len(digest._load_queue())
        check("explicit channels= is never diverted into the digest queue",
              after == before, f"before={before} after={after}")
    finally:
        digest.save_config({"enabled": False})


_TESTS = [obj for name, obj in list(globals().items()) if name.startswith("test_")]


def main():
    failures = []
    for test in _TESTS:
        before = len(FAIL)
        try:
            test()
        except AssertionError as e:
            failures.append((test.__name__, str(e)))
            continue
        except Exception as e:  # noqa: BLE001 — surface anything unexpected as a failure, not a crash
            failures.append((test.__name__, f"{type(e).__name__}: {e}"))
            print(f"FAILED   {test.__name__}: {type(e).__name__}: {e}")
            continue
        if len(FAIL) > before:
            failures.append((test.__name__, "see FAILED lines above"))
    total = len(PASS) + len(FAIL)
    print(f"\n{len(PASS)}/{total} checks passed across "
          f"{len(_TESTS) - len(failures)}/{len(_TESTS)} tests")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
