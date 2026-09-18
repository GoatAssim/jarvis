"""Tests for the workspace subsystems: daemons, raw log search, backlog,
ambient monitoring, tool diagnosis and onboarding.

    python3 tests/test_workspace.py

HOME is redirected to a temp dir before any jarvis import, because most of
these modules resolve Path.home() at import time.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

_TMP = tempfile.mkdtemp(prefix="jarvis-workspace-test-")
os.environ["HOME"] = _TMP
os.environ["USERPROFILE"] = _TMP
Path(_TMP, ".jarvis").mkdir(parents=True, exist_ok=True)

from jarvis import ambient, backlog, daemons, log_files, onboarding  # noqa: E402
from jarvis import tool_diagnosis  # noqa: E402


# --- daemons ---------------------------------------------------------------

def test_builtins_always_present_even_with_no_registry_file():
    ids = [d["id"] for d in daemons.list_daemons()]
    for expected in ("scheduler", "discord", "instagram"):
        assert expected in ids, ids


def test_builtin_argv_cannot_be_rewritten():
    """A built-in's argv is what 'start the scheduler' actually runs. If it
    were editable, a config edit would be arbitrary code execution."""
    ok, err = daemons.edit("scheduler", argv=["rm", "-rf", "/"])
    assert ok is False
    assert "built-in" in err, err
    assert daemons.get("scheduler")["argv"][0] == daemons.JARVIS_TOKEN


def test_builtin_user_fields_are_editable_and_persist():
    ok, err = daemons.edit("discord", enabled=False)
    assert ok, err
    assert daemons.get("discord")["enabled"] is False
    daemons.edit("discord", enabled=True)
    assert daemons.get("discord")["enabled"] is True


def test_builtin_cannot_be_removed_or_shadowed():
    ok, err = daemons.remove("scheduler")
    assert ok is False and "built-in" in err
    ok, err = daemons.add("discord", "node evil.js")
    assert ok is False and "built-in" in err


def test_add_edit_remove_a_custom_daemon():
    ok, err = daemons.add("testsrv", "python3 -c print(1)", name="Test",
                          supports_stdin=True)
    assert ok, err
    entry = daemons.get("testsrv")
    assert entry["argv"] == ["python3", "-c", "print(1)"], entry["argv"]
    assert entry["supports_stdin"] is True
    assert entry["builtin"] is False

    ok, err = daemons.add("testsrv", "whatever")
    assert ok is False and "already exists" in err

    ok, err = daemons.edit("testsrv", name="Renamed")
    assert ok, err
    assert daemons.get("testsrv")["name"] == "Renamed"

    ok, err = daemons.remove("testsrv")
    assert ok, err
    assert daemons.get("testsrv") is None


def test_add_rejects_an_empty_command():
    ok, err = daemons.add("empty", "")
    assert ok is False, err


def test_normalize_id_strips_unsafe_characters():
    assert daemons.normalize_id("My Web Server!") == "my-web-server"
    assert daemons.normalize_id("../../etc/passwd") == "etcpasswd"
    assert daemons.normalize_id("!!!") == ""


def test_status_of_something_never_started_is_stopped():
    state = daemons.status("scheduler")
    assert state["running"] is False
    assert state["status"] in (daemons.STATUS_STOPPED, daemons.STATUS_SCHEDULED)


def test_stale_running_status_is_reconciled_against_the_real_os():
    """A hard kill never writes 'stopped', so a status file claiming a dead
    pid is the normal case and must be corrected on read, not trusted."""
    daemons.add("ghost", "python3 -c pass")
    daemons._write_status("ghost", status=daemons.STATUS_RUNNING,
                          child_pid=999999, stop_requested=False)
    state = daemons.status("ghost")
    assert state["running"] is False
    # Not asked to stop and it isn't there -> that's a crash, not a stop.
    assert state["status"] == daemons.STATUS_CRASHED, state
    daemons.remove("ghost")


def test_a_requested_stop_reads_as_stopped_not_crashed():
    daemons.add("ghost2", "python3 -c pass")
    daemons._write_status("ghost2", status=daemons.STATUS_RUNNING,
                          child_pid=999999, stop_requested=True)
    assert daemons.status("ghost2")["status"] == daemons.STATUS_STOPPED
    daemons.remove("ghost2")


def test_send_input_refuses_a_daemon_that_does_not_read_stdin():
    daemons.add("nostdin", "python3 -c pass", supports_stdin=False)
    ok, err = daemons.send_input("nostdin", "hello")
    assert ok is False
    assert "console input" in err, err
    daemons.remove("nostdin")


def test_schedule_parses_a_human_time_and_clears():
    daemons.add("later", "python3 -c pass")
    ok, result = daemons.schedule("later", "in 2 hours")
    assert ok, result
    assert daemons.get("later")["next_start"]
    ok, _ = daemons.schedule("later", "")
    assert ok
    assert not daemons.get("later").get("next_start")
    daemons.remove("later")


def test_schedule_rejects_nonsense():
    daemons.add("bad", "python3 -c pass")
    ok, err = daemons.schedule("bad", "sometime when the vibes are right")
    assert ok is False, err
    daemons.remove("bad")


def test_due_now_ignores_an_unparseable_stored_time():
    """One corrupt entry must not wedge the tick for every other daemon."""
    daemons.add("corrupt", "python3 -c pass")
    daemons.edit("corrupt", next_start="not a timestamp")
    assert "corrupt" not in daemons.due_now()
    daemons.remove("corrupt")


def test_due_now_finds_a_past_start_time():
    daemons.add("duenow", "python3 -c pass")
    from jarvis import timespec
    from datetime import datetime, timedelta
    past = timespec.to_iso(datetime.now() - timedelta(minutes=5))
    daemons.edit("duenow", next_start=past)
    assert "duenow" in daemons.due_now()
    daemons.remove("duenow")


def test_supervisor_runs_a_real_process_and_captures_its_output():
    """The whole point: start it, and the output is readable from a
    completely different process afterwards."""
    daemons.add("echo", [sys.executable, "-c",
                         "print('hello-from-daemon')"])
    code = daemons.run_supervisor("echo")
    assert code == 0, code
    lines = daemons.read_console("echo", lines=50)
    assert any("hello-from-daemon" in ln for ln in lines), lines
    assert any("=== starting" in ln for ln in lines)
    assert any("exited with code 0" in ln for ln in lines)
    assert daemons.status("echo")["status"] == daemons.STATUS_STOPPED
    daemons.remove("echo")


def test_supervisor_records_a_nonzero_exit_as_a_crash():
    daemons.add("failer", [sys.executable, "-c", "import sys; sys.exit(3)"])
    daemons.run_supervisor("failer")
    state = daemons.status("failer")
    assert state["status"] == daemons.STATUS_CRASHED, state
    assert state["exit_code"] == 3, state
    assert "failed to start" in (state["last_error"] or ""), state["last_error"]
    daemons.remove("failer")


def test_supervisor_refuses_a_missing_working_directory():
    daemons.add("badcwd", f"{sys.executable} -c pass", cwd="/no/such/place")
    assert daemons.run_supervisor("badcwd") == 1
    daemons.remove("badcwd")


def test_console_rotation_keeps_the_log_bounded():
    path = daemons.console_path("rotate")
    path.write_text("x" * (daemons.MAX_CONSOLE_BYTES + 10), encoding="utf-8")
    daemons._rotate_console(path)
    assert not path.exists() or path.stat().st_size == 0
    assert daemons.console_backups("rotate"), "a backup should exist"


# --- log_files -------------------------------------------------------------

def _write_log(name, text):
    path = Path(_TMP, ".jarvis", "logs", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_search_finds_a_line_and_reports_where():
    _write_log("conv1.jsonl", "line one\nsomething BROKE here\nline three\n")
    out = log_files.search("broke", sets=["conversations"])
    assert out["ok"], out
    assert out["results"], out
    hit = out["results"][0]
    assert hit["line"] == 2, hit
    assert "BROKE" in hit["text"]


def test_search_finds_a_line_that_is_not_valid_json():
    """The killer case for logs.search(): a crash mid-write leaves one
    unparseable line, and that is frequently the interesting one."""
    _write_log("conv2.jsonl", '{"ok": true}\n{"broken": tru\n')
    out = log_files.search("broken", sets=["conversations"])
    assert any("broken" in r["text"] for r in out["results"]), out


def test_words_mode_requires_every_term_on_one_line():
    _write_log("conv3.jsonl", "alpha here\nbeta there\nalpha and beta\n")
    out = log_files.search("alpha beta", sets=["conversations"])
    texts = [r["text"] for r in out["results"]]
    assert texts == ["alpha and beta"], texts


def test_phrase_and_regex_modes():
    _write_log("conv4.jsonl", "error code 500\nerror code 404\n")
    phrase = log_files.search("code 500", mode="phrase", sets=["conversations"])
    assert len(phrase["results"]) == 1
    regex = log_files.search(r"code \d{3}", mode="regex", sets=["conversations"])
    assert len(regex["results"]) == 2


def test_a_bad_regex_is_an_error_not_an_exception():
    out = log_files.search("(unclosed", mode="regex")
    assert out["ok"] is False
    assert "bad regex" in out["error"]


def test_empty_query_is_rejected():
    assert log_files.search("   ")["ok"] is False


def test_context_lines_are_returned_when_asked():
    _write_log("conv5.jsonl", "a\nb\nTARGET\nd\ne\n")
    out = log_files.search("TARGET", sets=["conversations"], context=1)
    ctx = out["results"][0]["context"]
    assert [c["text"] for c in ctx] == ["b", "TARGET", "d"], ctx


def test_binary_files_are_skipped_not_mangled():
    path = Path(_TMP, ".jarvis", "logs", "binary.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x01\x02 findme \x00")
    out = log_files.search("findme", sets=["conversations"])
    assert not any("binary" in r["file"] for r in out["results"])
    assert any(s["why"] == "binary" for s in out["skipped"]), out["skipped"]


def test_explicit_paths_are_searched():
    extra = Path(_TMP, "app.log")
    extra.write_text("unrelated\nNEEDLE in a custom file\n", encoding="utf-8")
    out = log_files.search("NEEDLE", sets=[], paths=[str(extra)])
    assert len(out["results"]) == 1, out
    assert out["results"][0]["line"] == 2


def test_daemon_consoles_are_searchable():
    daemons.add("logged", f"{sys.executable} -c pass")
    daemons.console_path("logged").write_text(
        "boot\nTraceback (most recent call last)\n", encoding="utf-8")
    out = log_files.search("traceback", mode="phrase", sets=["daemons"])
    assert out["results"], out
    daemons.remove("logged")


def test_tail_reads_the_end_of_a_file():
    path = Path(_TMP, "big.log")
    path.write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
    out = log_files.tail(str(path), lines=5)
    assert out["ok"]
    assert out["lines"][-1] == "line 499", out["lines"]
    assert len(out["lines"]) == 5


def test_tail_of_a_missing_file_is_an_error_not_a_crash():
    assert log_files.tail("/no/such/file.log")["ok"] is False


# --- backlog ---------------------------------------------------------------

def test_add_and_list():
    item, err = backlog.add("rewrite the auth module", project="api",
                            priority="high")
    assert item is not None, err
    assert item["state"] == backlog.TODO
    assert item["priority"] == "high"
    assert any(i["id"] == item["id"] for i in backlog.items())


def test_a_title_that_is_too_short_is_refused():
    item, err = backlog.add("x")
    assert item is None and err


def test_state_aliases_are_accepted():
    for word, expected in (("wip", backlog.DOING), ("waiting", backlog.BLOCKED),
                           ("finished", backlog.DONE), ("someday", backlog.IDEA)):
        assert backlog.normalize_state(word) == expected, word


def test_find_by_partial_title():
    backlog.add("deploy the staging environment", project="ops")
    item, err = backlog.find("staging")
    assert item is not None, err
    assert "staging" in item["title"]


def test_find_reports_ambiguity_instead_of_guessing():
    backlog.add("review the parser docs")
    backlog.add("review the parser tests")
    item, err = backlog.find("review the parser")
    assert item is None
    assert "matches several" in err, err


def test_marking_done_sets_done_at_and_reopening_clears_it():
    item, _ = backlog.add("something finishable")
    done, err = backlog.update(item["id"], state="done")
    assert done["state"] == backlog.DONE and done["done_at"], err
    reopened, _ = backlog.update(item["id"], state="todo")
    assert reopened["done_at"] is None


def test_blocked_on_implies_blocked_state():
    item, _ = backlog.add("waiting on someone")
    updated, err = backlog.update(item["id"], blocked_on="Ana's review")
    assert updated["state"] == backlog.BLOCKED, err
    assert updated["blocked_on"] == "Ana's review"


def test_unblocking_clears_the_stale_blocked_on():
    """Otherwise 'what's blocked' keeps reporting a reason for something
    that isn't blocked any more."""
    item, _ = backlog.add("temporarily stuck thing")
    backlog.update(item["id"], blocked_on="the thing")
    updated, _ = backlog.update(item["id"], state="doing")
    assert updated["blocked_on"] == "", updated


def test_summary_reports_blocked_and_projects():
    data = backlog.summary()
    assert "counts" in data and "blocked" in data
    assert isinstance(data["projects"], list)
    assert data["open"] >= 1


def test_update_of_something_missing_is_an_error():
    item, err = backlog.update("definitely-not-a-real-item-xyz", state="done")
    assert item is None and err


def test_board_groups_by_state():
    grouped = backlog.board()
    assert set(grouped) <= set(backlog.STATES)


# --- tool diagnosis --------------------------------------------------------

def test_a_successful_result_is_untouched():
    result = {"ok": True, "value": 1}
    assert tool_diagnosis.annotate("anything", result) is result


def test_needs_clarification_is_not_treated_as_a_failure():
    result = {"needs_clarification": True, "message": "which one?"}
    assert tool_diagnosis.annotate("x", result) is result


def test_a_declined_confirmation_is_not_a_failure():
    result = {"ok": False, "cancelled": True, "message": "declined"}
    assert tool_diagnosis.annotate("run_command", result) is result


def test_missing_python_package_produces_the_pip_command():
    result = {"ok": False, "error": "ModuleNotFoundError: No module named 'yt_dlp'"}
    out = tool_diagnosis.annotate("ytdl_download", result)
    assert "diagnosis" in out, out
    assert "pip install yt-dlp" in out["diagnosis"]["fix"], out["diagnosis"]


def test_missing_binary_produces_the_platform_install_command():
    result = {"ok": False, "error": "[WinError 2] The system cannot find the file specified"}
    out = tool_diagnosis.annotate("ytdl_download", result)
    assert "diagnosis" in out, out
    assert out["diagnosis"]["cause"]
    assert "ffmpeg" in str(out["diagnosis"]).lower(), out["diagnosis"]


def test_a_401_is_explained_as_a_rejected_key():
    out = tool_diagnosis.annotate("web_fetch", {"ok": False, "error": "HTTP 401 Unauthorized"})
    assert "rejected" in out["diagnosis"]["cause"].lower(), out["diagnosis"]


def test_a_connection_refused_points_at_the_service():
    out = tool_diagnosis.annotate("playnite_query_games",
                                  {"ok": False, "error": "[WinError 10061] Connection refused"})
    assert "listening" in out["diagnosis"]["cause"].lower(), out["diagnosis"]


def test_an_unrecognized_error_adds_nothing():
    result = {"ok": False, "error": "the flux capacitor is misaligned"}
    assert tool_diagnosis.annotate("x", result) is result


def test_diagnosis_never_raises_on_a_weird_result():
    for junk in (None, "a string", 42, [], {"ok": False, "error": None}):
        tool_diagnosis.annotate("x", junk)


# --- ambient ---------------------------------------------------------------

def test_observe_returns_observations_without_notifying():
    found = ambient.observe(ambient.load_config())
    assert isinstance(found, list)
    for obs in found:
        assert obs.key and obs.severity in (
            ambient.SEVERITY_INFO, ambient.SEVERITY_WARN, ambient.SEVERITY_ALERT)


def test_a_repeat_observation_is_not_reported_twice():
    """The entire design: a monitor that repeats itself gets ignored."""
    cfg = ambient.load_config()
    obs = ambient.Observation("test.thing", ambient.SEVERITY_WARN, "same", value=50)
    now = time.time()
    previous = {"severity": ambient.SEVERITY_WARN, "reported_at": now, "value": 50}
    assert ambient._worth_reporting(obs, None, 3600, now) is True
    assert ambient._worth_reporting(obs, previous, 3600, now) is False


def test_an_escalation_reports_even_inside_the_cooldown():
    now = time.time()
    worse = ambient.Observation("t", ambient.SEVERITY_ALERT, "worse", value=95)
    previous = {"severity": ambient.SEVERITY_WARN, "reported_at": now, "value": 86}
    assert ambient._worth_reporting(worse, previous, 3600, now) is True


def test_tick_is_disabled_by_config():
    cfg = dict(ambient.load_config())
    cfg["enabled"] = False
    out = ambient.tick(cfg=cfg, notify=False)
    assert out.get("skipped") == "disabled"


def test_tick_runs_and_persists_state():
    out = ambient.tick(notify=False)
    assert "new" in out and "recovered" in out
    assert ambient.status()["ok"] is True


# --- onboarding ------------------------------------------------------------

def test_ui_mode_defaults_and_round_trips():
    assert onboarding.ui_mode() == onboarding.DEFAULT_UI_MODE
    ok, result = onboarding.set_ui_mode("focus")
    assert ok and result == "focus"
    assert onboarding.ui_mode() == "focus"
    ok, err = onboarding.set_ui_mode("neon")
    assert ok is False and "unknown" in err
    assert onboarding.ui_mode() == "focus", "a bad set must not clobber"
    onboarding.set_ui_mode("classic")


def test_steps_all_report_a_status():
    for step in onboarding.steps():
        assert step["status"] in (onboarding.STATUS_OK, onboarding.STATUS_TODO,
                                  onboarding.STATUS_OPTIONAL), step
        assert step["title"] and step["detail"] is not None
        assert "check" not in step, "the predicate must not leak to callers"


def test_placeholder_keys_do_not_count_as_configured():
    for fake in ("your-api-key-here", "sk-...", "xxxx", "paste key here"):
        assert onboarding._looks_placeholder(fake) is True, fake
    assert onboarding._looks_placeholder("sk-proj-9f8a7b6c5d4e3f2a1b0c") is False


def test_skip_stops_the_prompt_coming_back():
    assert onboarding.should_prompt() in (True, False)
    onboarding.skip()
    assert onboarding.should_prompt() is False


def test_render_for_terminal_is_plain_text():
    text = onboarding.render_for_terminal()
    assert "Jarvis setup" in text
    assert "Pick a layout" in text



# --- daemon customization --------------------------------------------------

def test_custom_daemon_accepts_the_full_option_set():
    ok, err = daemons.add(
        "fullopt", [sys.executable, "-c", "pass"], name="Full",
        cwd=str(Path(_TMP)), env={"PORT": "8080"}, supports_stdin=True,
        shell=False, restart=daemons.RESTART_ON_FAILURE, restart_delay=2,
        max_restarts=3, stop_signal="INT", stop_timeout=20,
        autostart=True, description="a fully specified service")
    assert ok, err
    e = daemons.get("fullopt")
    assert e["restart"] == daemons.RESTART_ON_FAILURE
    assert e["restart_delay"] == 2 and e["max_restarts"] == 3
    assert e["stop_signal"] == "INT" and e["stop_timeout"] == 20
    assert e["env"] == {"PORT": "8080"}
    assert e["autostart"] is True and e["supports_stdin"] is True
    daemons.remove("fullopt")


def test_an_unknown_restart_policy_falls_back_on_add_and_is_rejected_on_edit():
    daemons.add("pol", [sys.executable, "-c", "pass"], restart="whenever")
    assert daemons.get("pol")["restart"] == daemons.RESTART_NEVER
    ok, err = daemons.edit("pol", restart="whenever")
    assert ok is False and "restart must be one of" in err
    daemons.remove("pol")


def test_numeric_edits_are_coerced_not_stored_as_strings():
    """A stored "2" compares fine and then explodes at time.sleep()."""
    daemons.add("num", [sys.executable, "-c", "pass"])
    ok, err = daemons.edit("num", restart_delay="7", max_restarts="2")
    assert ok, err
    e = daemons.get("num")
    assert e["restart_delay"] == 7 and isinstance(e["restart_delay"], int)
    assert e["max_restarts"] == 2 and isinstance(e["max_restarts"], int)
    ok, err = daemons.edit("num", restart_delay="soon")
    assert ok is False and "must be a number" in err
    daemons.remove("num")


def test_an_unknown_stop_signal_is_rejected():
    daemons.add("sig", [sys.executable, "-c", "pass"])
    ok, err = daemons.edit("sig", stop_signal="BANANA")
    assert ok is False and "stop_signal" in err
    daemons.remove("sig")


def test_shell_mode_cannot_be_turned_on_for_a_builtin():
    """shell=True re-interprets the stored command every start. On a built-in
    that would be arbitrary execution via a config edit."""
    ok, err = daemons.edit("scheduler", shell=True)
    assert ok is False and "built-in" in err


def test_builtin_overrides_persist_across_reads():
    ok, err = daemons.edit("discord", restart=daemons.RESTART_ALWAYS,
                           stop_timeout=30)
    assert ok, err
    e = daemons.get("discord")
    assert e["restart"] == daemons.RESTART_ALWAYS and e["stop_timeout"] == 30
    # ...but the argv is still the shipped one, not a frozen copy.
    assert e["argv"][0] == daemons.JARVIS_TOKEN
    daemons.edit("discord", restart=daemons.RESTART_NEVER, stop_timeout=10)


def test_restart_on_failure_respawns_then_gives_up_at_the_limit():
    """The whole point of a restart policy, end to end: a service that keeps
    dying comes back, and then stops coming back."""
    daemons.add("flappy", [sys.executable, "-c", "import sys; sys.exit(1)"],
                restart=daemons.RESTART_ON_FAILURE, restart_delay=1,
                max_restarts=2)
    daemons.run_supervisor("flappy")
    lines = daemons.read_console("flappy", lines=200)
    starts = sum(1 for ln in lines if "=== starting" in ln)
    assert starts == 3, f"1 initial + 2 restarts expected, saw {starts}"
    assert any("restart limit reached" in ln for ln in lines), lines[-4:]
    assert daemons.status("flappy")["status"] == daemons.STATUS_CRASHED
    daemons.remove("flappy")


def test_restart_on_failure_does_not_respawn_a_clean_exit():
    daemons.add("cleanexit", [sys.executable, "-c", "pass"],
                restart=daemons.RESTART_ON_FAILURE, restart_delay=1)
    daemons.run_supervisor("cleanexit")
    lines = daemons.read_console("cleanexit", lines=100)
    assert sum(1 for ln in lines if "=== starting" in ln) == 1, lines
    daemons.remove("cleanexit")


def test_restart_never_is_the_default_and_does_not_loop():
    daemons.add("once", [sys.executable, "-c", "import sys; sys.exit(2)"])
    assert daemons.get("once")["restart"] == daemons.RESTART_NEVER
    daemons.run_supervisor("once")
    lines = daemons.read_console("once", lines=100)
    assert sum(1 for ln in lines if "=== starting" in ln) == 1, lines
    daemons.remove("once")


def test_shell_mode_runs_through_a_shell():
    daemons.add("shelly", f'{sys.executable} -c "print(1)"', shell=True)
    assert daemons.get("shelly")["shell"] is True
    code = daemons.run_supervisor("shelly")
    assert code == 0, daemons.read_console("shelly", lines=30)
    daemons.remove("shelly")


def test_env_reaches_the_child_process():
    daemons.add("envtest",
                [sys.executable, "-c",
                 "import os; print('SAW=' + os.environ.get('JARVIS_TEST_VAR', ''))"],
                env={"JARVIS_TEST_VAR": "hello"})
    daemons.run_supervisor("envtest")
    lines = daemons.read_console("envtest", lines=50)
    assert any("SAW=hello" in ln for ln in lines), lines
    daemons.remove("envtest")


def test_cwd_reaches_the_child_process():
    workdir = Path(_TMP, "workdir")
    workdir.mkdir(exist_ok=True)
    daemons.add("cwdtest", [sys.executable, "-c", "import os; print(os.getcwd())"],
                cwd=str(workdir))
    daemons.run_supervisor("cwdtest")
    lines = daemons.read_console("cwdtest", lines=50)
    assert any("workdir" in ln for ln in lines), lines
    daemons.remove("cwdtest")


def test_stdin_queue_reaches_a_running_child():
    """The supervisor's reason for existing: a THIRD process can feed stdin.

    Note the ordering. run_supervisor() deliberately CLEARS the queue when it
    starts, so input written before the child exists is discarded rather than
    replayed into a fresh process — queuing here has to happen after the
    supervisor is up, which is also how it works in reality (you type into a
    console that is already running).
    """
    script = ("import sys\n"
              "for line in sys.stdin:\n"
              "    print('GOT:' + line.strip(), flush=True)\n")
    daemons.add("stdintest", [sys.executable, "-u", "-c", script],
                supports_stdin=True)
    import threading
    thread = threading.Thread(target=daemons.run_supervisor,
                              args=("stdintest",), daemon=True)
    thread.start()
    for _ in range(40):                       # wait for the child to be up
        if daemons.status("stdintest")["running"]:
            break
        time.sleep(0.25)
    assert daemons.status("stdintest")["running"], "supervisor never started"

    ok, err = daemons.send_input("stdintest", "hello there")
    assert ok, err
    for _ in range(40):
        if any("GOT:hello there" in ln
               for ln in daemons.read_console("stdintest", lines=40)):
            break
        time.sleep(0.25)
    lines = daemons.read_console("stdintest", lines=40)
    assert any("GOT:hello there" in ln for ln in lines), lines
    assert any("<<< hello there" in ln for ln in lines), "input should be echoed"

    daemons.stop("stdintest", timeout=8)
    thread.join(timeout=10)
    daemons.remove("stdintest")


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
