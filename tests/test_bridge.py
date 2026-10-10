"""The direct channel between the web server and jarvis: `jarvis _internal_bridge`
(jarvis-cli/jarvis/bridge.py) and the one-line seam the CLI uses to feed it
(wire.py).

These start the REAL worker as a subprocess against a throwaway HOME and talk to
it the way web/bridge.js does -- one JSON object per line -- so they check the
protocol, not a mock of it. No network and no API key: asks run with no providers
configured, saved commands only `echo`.

What is pinned:

  1. The worker says `ready`, runs a request through the real `cli.main()`, and a
     second request reuses the SAME process (the whole point).
  2. The environment overlay applies to one request and is gone for the next;
     a grandchild process's output (a saved command's shell) is captured too,
     because stdout/stderr are redirected at the file-descriptor level.
  3. A request's stdin text reaches a grandchild; a confirmation round-trips
     (`confirm` event out, `answer` line in, both yes and no).
  4. An ask reports its reply as a typed `reply` event next to the ordinary
     stdout line, and trace lines the CLI categorised carry their `cat`.
  5. Nothing is ever half-done: `refuse` and `retire` come only BEFORE `started`,
     and a changed custom-tool folder retires the worker (staleness guard).
  6. The CLI never depends on any of this: importing `jarvis.cli` does not load
     the bridge, `wire` with no host is a plain stderr print and a no-op, and the
     plain `jarvis config` still works with no worker anywhere.

Run: python3 tests/test_bridge.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-bridge-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

_JARVIS_CLI = Path(__file__).resolve().parent.parent / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("ok  " + name)
    else:
        FAIL.append((name, detail))
        print("FAIL " + name + (f": {detail}" if detail else ""))


# A saved-commands file whose commands exercise the grandchild/stdio paths. The
# shell runs `{py}`, so these work on any platform without relying on `cat`.
_PY = f'"{sys.executable}"'
_COMMANDS = {
    "commands": {
        "showenv": {"description": "print an env var",
                    "run": f'{_PY} -c "import os;print(os.environ.get(\'JARVIS_TEST_X\'))"'},
        "readin": {"description": "echo stdin",
                   "run": f'{_PY} -c "import sys;print(\'got:\'+sys.stdin.read().strip())"'},
        "guarded": {"description": "needs a yes", "confirm_required": True,
                    "run": f'{_PY} -c "print(\'guarded ran\')"'},
    }
}


def _write_home_files():
    jd = Path(_HOME) / ".jarvis"
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "commands.json").write_text(json.dumps(_COMMANDS), encoding="utf-8")
    # No providers: an ask answers "none configured" instead of calling anything.
    (jd / "ai_config.json").write_text(json.dumps(
        {"persona": {"assistant_name": "Tester"}, "defaults": {}, "providers": []}), encoding="utf-8")


class Worker:
    """What web/bridge.js does, in a dozen lines."""

    def __init__(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_JARVIS_CLI)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "jarvis", "_internal_bridge"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.diag = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.ready = self._next()
        self._n = 0

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self.diag.append(line.decode("utf-8", "replace"))

    def _next(self):
        line = self.proc.stdout.readline()
        return json.loads(line.decode("utf-8")) if line else None

    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def run(self, argv, kind="once", env=None, stdin=None, interactive=False, answers=None):
        """Send one request and collect its events up to exit/refuse/retire.
        `answers` maps a confirm event to the line to answer it with."""
        self._n += 1
        rid = f"t{self._n}"
        self._send({"op": "run", "id": rid, "argv": argv, "kind": kind, "env": env or {},
                    "stdin": stdin, "interactive": interactive})
        events = []
        while True:
            ev = self._next()
            if ev is None:
                events.append({"t": "eof"})
                return events
            events.append(ev)
            if ev.get("t") == "confirm" and answers is not None:
                self._send({"op": "answer", "id": rid, "line": answers})
            if ev.get("t") in ("exit", "refuse", "retire"):
                return events

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def kinds(events):
    return [e.get("t") for e in events]


def lines(events, t="out"):
    return [e["line"] for e in events if e.get("t") == t]


def exit_code(events):
    return next((e["code"] for e in events if e.get("t") == "exit"), None)


_worker = None


def worker():
    global _worker
    if _worker is None:
        _write_home_files()
        _worker = Worker()
    return _worker


# ---------------------------------------------------------------------------

def test_worker_says_ready_and_speaks_protocol_one():
    w = worker()
    check("first message is `ready`", w.ready and w.ready.get("t") == "ready", w.ready)
    check("it names protocol 1 and its pid", w.ready.get("proto") == 1 and isinstance(w.ready.get("pid"), int))


def test_a_request_runs_through_the_real_cli_and_is_framed():
    ev = worker().run(["config"])
    check("started comes first and exit last", kinds(ev)[0] == "started" and kinds(ev)[-1] == "exit", kinds(ev))
    check("`jarvis config` printed the commands.json path on stdout",
          lines(ev) == [str(Path(_HOME) / ".jarvis" / "commands.json")], lines(ev))
    check("exit code 0", exit_code(ev) == 0, ev)


def test_the_second_request_reuses_the_same_process():
    w = worker()
    a = w.run(["config"])
    b = w.run(["config"])
    pa = next(e["pid"] for e in a if e.get("t") == "started")
    pb = next(e["pid"] for e in b if e.get("t") == "started")
    check("both requests ran in the process that said ready", pa == pb == w.ready["pid"], (pa, pb, w.ready))


def test_env_overlay_applies_to_one_request_and_is_restored():
    w = worker()
    with_env = w.run(["showenv"], env={"JARVIS_TEST_X": "hello-overlay"})
    without = w.run(["showenv"])
    check("a grandchild saw the overlay (and its stdout was captured)",
          "hello-overlay" in lines(with_env), (lines(with_env), lines(with_env, "err")))
    check("the next request does not", "hello-overlay" not in lines(without) and "None" in lines(without),
          lines(without))
    check("the worker's own environment was restored", "JARVIS_TEST_X" not in os.environ)


def test_request_stdin_reaches_a_grandchild():
    ev = worker().run(["readin"], stdin="from the server")
    check("stdin text was readable by the command's own process", "got:from the server" in lines(ev), lines(ev))


def test_confirm_round_trip_yes():
    ev = worker().run(["guarded"], kind="run", interactive=True, answers="y")
    conf = [e for e in ev if e.get("t") == "confirm"]
    check("a typed `confirm` event came out, not a JARVIS_CONFIRM_REQUEST stdout line",
          len(conf) == 1 and conf[0]["tool"] == "guarded" and not any(
              l.startswith("JARVIS_CONFIRM_REQUEST") for l in lines(ev)), kinds(ev))
    check("answering `y` let the command run", "guarded ran" in lines(ev) and exit_code(ev) == 0,
          (lines(ev), exit_code(ev)))


def test_confirm_round_trip_no():
    ev = worker().run(["guarded"], kind="run", interactive=True, answers="n")
    check("answering `n` stopped it", "guarded ran" not in lines(ev) and exit_code(ev) == 1,
          (lines(ev), exit_code(ev)))
    check("and the CLI said it cancelled", any("Cancelled" in l for l in lines(ev, "err")), lines(ev, "err"))


def test_an_ask_reports_a_typed_reply_alongside_the_stdout_line():
    ev = worker().run(["hello there"], kind="ask", interactive=True)
    replies = [e for e in ev if e.get("t") == "reply"]
    check("exactly one typed reply", len(replies) == 1, kinds(ev))
    r = replies[0] if replies else {}
    check("it carries the assistant's name and says the ask did not succeed",
          r.get("name") == "Tester" and r.get("ok") is False, r)
    check("the same words are still the stdout line, with the name in front (browsers read that today)",
          any(l.startswith("Tester: ") and r.get("text", "?") in l for l in lines(ev)), lines(ev))
    check("the ask exited 1, like a process would", exit_code(ev) == 1, exit_code(ev))
    check("tool-discovery chatter from loading is replayed for an ask, tagged `discovery`",
          any(e.get("cat") == "discovery" for e in ev if e.get("t") == "err"))


def test_trace_lines_carry_the_category_the_cli_gave_them():
    # Run in-process with a stub host, so no provider call is needed to reach a trace line.
    from jarvis import wire

    seen = []
    saved_err = sys.stderr

    class Capture:
        def write(self, s):
            seen.append(s)

        def flush(self):
            pass

    sys.stderr = Capture()
    try:
        wire.trace("plain line")                       # no host: untouched
        wire.install(object())
        wire.trace("a tool call", "tool-call")         # hosted: tagged
        wire.trace("made-up category", "no-such-cat")  # unknown ids are dropped, not shipped
        wire.uninstall()
    finally:
        sys.stderr = saved_err
    text = "".join(seen)
    check("with no host a trace line is exactly the text", text.startswith("plain line\n"), repr(text[:40]))
    check("with a host the category rides in-band in front of the text",
          f"{wire.CAT_OPEN}tool-call{wire.CAT_CLOSE}a tool call" in text)
    check("an unknown category is not written", "made-up category\n" in text and "no-such-cat" not in text)


def test_refuse_comes_before_started():
    ev = worker().run(["sched-daemon"])
    check("a standing-loop command is handed back, never run",
          kinds(ev) == ["refuse"], kinds(ev))


def test_markers_become_typed_events_in_order():
    from jarvis import bridge
    t = bridge.Host._translate_out
    s = t("r1", 'JARVIS_STREAM {"k":"text","d":"hi"}')
    u = t("r1", 'JARVIS_USAGE {"total_tokens":3}')
    c = t("r1", 'JARVIS_CONFIRM_REQUEST {"tool":"x","arguments":{"a":1}}')
    i = t("r1", 'JARVIS_UI_REQUEST {"kind":"confirm"}')
    bad = t("r1", "JARVIS_STREAM {not json")
    plain = t("r1", "just a line")
    check("stream", s == {"id": "r1", "t": "stream", "ev": {"k": "text", "d": "hi"}}, s)
    check("usage keeps the raw line for callers that treat it as text",
          u["t"] == "usage" and u["usage"]["total_tokens"] == 3 and u["raw"].startswith("JARVIS_USAGE "), u)
    check("confirm", c["t"] == "confirm" and c["tool"] == "x" and c["arguments"] == {"a": 1}, c)
    check("ui", i["t"] == "ui" and i["request"] == {"kind": "confirm"}, i)
    check("a malformed marker stays a plain stdout line, as the server always treated it",
          bad == {"id": "r1", "t": "out", "line": "JARVIS_STREAM {not json"}, bad)
    check("an ordinary line is an `out` event", plain == {"id": "r1", "t": "out", "line": "just a line"}, plain)


def test_fingerprint_notices_custom_tools_but_not_drafts():
    from jarvis import bridge
    home = tempfile.mkdtemp(prefix="jarvis-fp-")
    tools = Path(home) / ".jarvis" / "tools"
    (tools / "_drafts").mkdir(parents=True)
    base = bridge.source_fingerprint(home=home)
    check("stable when nothing changed", base == bridge.source_fingerprint(home=home))
    (tools / "_drafts" / "x.json").write_text("{}", encoding="utf-8")
    check("a draft being saved does not count", base == bridge.source_fingerprint(home=home))
    (tools / "mine.py").write_text("x = 1\n", encoding="utf-8")
    after_new = bridge.source_fingerprint(home=home)
    check("a new custom tool file does", after_new != base)
    time.sleep(0.01)
    (tools / "mine.py").write_text("x = 22\n", encoding="utf-8")
    check("editing it does", bridge.source_fingerprint(home=home) != after_new)
    (Path(home) / ".jarvis" / "mcp_config.json").write_text("{}", encoding="utf-8")
    check("so does the MCP config", bridge.source_fingerprint(home=home) != after_new)


def test_a_changed_tool_folder_retires_the_worker_before_running_anything():
    w = worker()
    tools = Path(_HOME) / ".jarvis" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "added_after_boot.py").write_text("# a tool the worker has not loaded\n", encoding="utf-8")
    ev = w.run(["config"])
    check("answered `retire`, nothing else (so the caller can safely run it elsewhere)",
          kinds(ev) == ["retire"], kinds(ev))
    w.close()


def test_the_cli_does_not_depend_on_the_bridge():
    code = (
        "import sys; import jarvis.cli; "
        "print('bridge' in ' '.join(m for m in sys.modules if m.startswith('jarvis.')) and "
        "'jarvis.bridge' in sys.modules)"
    )
    env = dict(os.environ, PYTHONPATH=str(_JARVIS_CLI))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    check("importing the CLI does not load the bridge", r.stdout.strip() == "False", (r.stdout, r.stderr[-300:]))
    r = subprocess.run([sys.executable, "-m", "jarvis", "config"], capture_output=True, text=True, env=env)
    check("`jarvis config` works with no worker, no server and no node",
          r.returncode == 0 and r.stdout.strip().endswith("commands.json"), (r.returncode, r.stderr[-300:]))
    from jarvis import wire
    check("wire is inert without a host", wire.active() is False and wire.reply("n", "t") is None)
    src = (_JARVIS_CLI / "jarvis" / "bridge.py").read_text(encoding="utf-8")
    check("the bridge imports nothing from the web side",
          "server.js" not in src.split('"""', 2)[2] and "express" not in src)


def test_trace_categories_are_the_consoles_own_filter_ids():
    import re
    from jarvis import wire
    app = (Path(__file__).resolve().parent.parent / "web" / "public" / "app.js").read_text(encoding="utf-8")
    block = app.split("const ASK_CONSOLE_CATEGORIES = [", 1)[1].split("];", 1)[0]
    ids = set(re.findall(r'id:\s*"([a-z-]+)"', block))
    check("every category the CLI may tag a line with is a filter the Ask console offers",
          wire.CATEGORIES <= ids, sorted(wire.CATEGORIES - ids))


def test_reserved_and_hidden_like_the_other_internal_entry_point():
    from jarvis.reserved_names import RESERVED_NAMES
    check("_internal_bridge is a reserved name (a saved command can't shadow it)",
          "_internal_bridge" in RESERVED_NAMES)


_TESTS = [obj for name, obj in list(globals().items()) if name.startswith("test_")]


def main():
    try:
        for test in _TESTS:
            test()
    finally:
        if _worker is not None:
            _worker.close()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
