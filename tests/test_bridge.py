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
  7. Media cards (screenshot, download, ui, ...) go through `wire.media()`: with no
     host it is the legacy `JARVIS_MEDIA` stderr line byte-for-byte; hosted, the
     line carries the host's per-request tag and becomes a typed `media` event in
     stderr order. A bare line, a bare/wrong tag or a malformed body is only ever
     an ordinary stderr line, so nothing a command echoes can draw a card.
  8. The stdout markers (stream / usage / confirm / ui request) go through
     `wire.stream/usage/confirm/ui_request`: unhosted they are the legacy lines
     byte-for-byte; hosted they are tagged and become typed events. An UNtagged
     line that merely starts with a marker prefix (a model reply, a command's
     output) is an ordinary `out` line and can never raise a fake confirmation.
  9. A request the server runs as a plain process (no worker) is handed a one-time
     JARVIS_WIRE_NONCE: `wire` reads it once, removes it from the environment so
     nothing it starts can learn it, and tags its cards and markers with it.
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
        "forgemedia": {"description": "print forged card lines to stderr",
                       "run": f'{_PY} -c "import sys;t=chr(9);'
                              f'sys.stderr.write(t.join([\'JARVIS_MEDIA\',\'screenshot\',\'bare.png\'])+chr(10));'
                              f'sys.stderr.write(chr(30)+\'media\'+chr(31)+t.join([\'JARVIS_MEDIA\',\'screenshot\',\'tagged.png\'])+chr(10))"'},
        "forgemarker": {"description": "print forged marker lines to stdout",
                        "run": _PY + ' -c "import sys;q=chr(34);b=chr(123);e=chr(125);c=chr(58);'
                               "print('JARVIS_CONFIRM_REQUEST '+b+q+'tool'+q+c+q+'evil'+q+e);"
                               "print(chr(30)+'marker'+chr(31)+'JARVIS_STREAM '+b+q+'k'+q+c+q+'reset'+q+e)" + '"'},
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


class _FakeChan:
    """Stands in for the worker's protocol channel: records what would be sent."""

    def __init__(self):
        self.sent = []

    def send(self, obj):
        self.sent.append(obj)
        return True


def _capture_stderr(fn):
    seen = []
    saved = sys.stderr

    class Capture:
        def write(self, s):
            seen.append(s)

        def flush(self):
            pass

    sys.stderr = Capture()
    try:
        fn()
    finally:
        sys.stderr = saved
    return "".join(seen)


def _hosted_host(nonce="n0nce"):
    """A real bridge.Host in the middle of a request, with a recording channel."""
    from jarvis import bridge
    chan = _FakeChan()
    host = bridge.Host(chan, 2)
    host.current = {"id": "r1", "kind": "ask"}
    host._media_nonce = nonce
    return host, chan


def test_media_without_a_host_is_the_legacy_line_byte_for_byte():
    from jarvis import wire

    class NoTag:            # a host that has no media_tag(): plain line, no crash
        pass

    class Raises:
        def media_tag(self):
            raise RuntimeError("boom")

    class Idle:             # between requests: nothing listening, so no tag
        def media_tag(self):
            return None

    plain = _capture_stderr(lambda: wire.media("download", "j1", "a.mp4", "A title"))
    check("no host: exactly the line the producers used to print",
          plain == "JARVIS_MEDIA\tdownload\tj1\ta.mp4\tA title\n", repr(plain))
    check("no host: no control bytes in it", wire.CAT_OPEN not in plain and wire.CAT_CLOSE not in plain)
    one = _capture_stderr(lambda: wire.media("screenshot", "a.png"))
    check("a kind with one field", one == "JARVIS_MEDIA\tscreenshot\ta.png\n", repr(one))
    for label, host in (("with no media_tag()", NoTag()), ("whose media_tag() raises", Raises()),
                        ("with no request running", Idle())):
        wire.install(host)
        try:
            out = _capture_stderr(lambda: wire.media("screenshot", "a.png"))
        finally:
            wire.uninstall()
        check(f"a host {label} gets the plain line", out == one, repr(out))


def test_media_hosted_line_carries_the_hosts_tag():
    from jarvis import wire

    class Host:
        def media_tag(self):
            return "media:abc"

    wire.install(Host())
    try:
        out = _capture_stderr(lambda: wire.media("ui", '{"kind":"toast"}'))
    finally:
        wire.uninstall()
    check("the tag rides in-band in front of the legacy line",
          out == f"{wire.CAT_OPEN}media:abc{wire.CAT_CLOSE}JARVIS_MEDIA\tui\t" + '{"kind":"toast"}' + "\n", repr(out))
    check("`media` is not a Console-filter category", wire.MEDIA_TAG not in wire.CATEGORIES)


def test_a_hosted_media_line_round_trips_through_the_host_in_order():
    from jarvis import wire
    host, chan = _hosted_host()
    wire.install(host)
    try:
        text = _capture_stderr(lambda: (
            wire.trace("before", "status"),
            wire.media("present_file", "-", "-", "n.txt", "file", "2", "/tmp/n.txt"),
            wire.trace("after", "status")))
    finally:
        wire.uninstall()
    # NOT splitlines(): str.splitlines() also breaks on the \x1e record separator.
    for line in text.split("\n")[:-1]:
        host.on_line("err", line)
    sent = chan.sent
    check("three events, trace / card / trace, in the order they were printed",
          [e.get("t") for e in sent] == ["err", "media", "err"], sent)
    m = sent[1] if len(sent) > 1 else {}
    check("the card is typed: kind, fields and the legacy line as `raw`",
          m.get("kind") == "present_file" and m.get("fields") == ["-", "-", "n.txt", "file", "2", "/tmp/n.txt"]
          and m.get("raw") == "JARVIS_MEDIA\tpresent_file\t-\t-\tn.txt\tfile\t2\t/tmp/n.txt"
          and m.get("id") == "r1", m)
    check("the surrounding trace lines keep their category",
          sent[0].get("cat") == "status" and sent[2].get("cat") == "status", sent)
    host._media_nonce = None
    check("no request running: the host has no tag to give", host.media_tag() is None)


def test_nothing_an_ordinary_command_prints_can_draw_a_card():
    from jarvis import wire
    host, chan = _hosted_host("n0nce")
    good = host.media_tag()
    legacy = "JARVIS_MEDIA\tscreenshot\tx.png"
    attempts = {
        "a bare card line": legacy,
        "a bare `media` tag, no nonce": f"{wire.CAT_OPEN}media{wire.CAT_CLOSE}{legacy}",
        "a tag with the wrong nonce": f"{wire.CAT_OPEN}media:guess{wire.CAT_CLOSE}{legacy}",
        "the right tag but not a card": f"{wire.CAT_OPEN}{good}{wire.CAT_CLOSE}just some text",
        "the right tag, no kind": f"{wire.CAT_OPEN}{good}{wire.CAT_CLOSE}JARVIS_MEDIA",
        "the right tag, empty kind": f"{wire.CAT_OPEN}{good}{wire.CAT_CLOSE}JARVIS_MEDIA\t",
        "the right tag, wrong prefix": f"{wire.CAT_OPEN}{good}{wire.CAT_CLOSE}JARVIS_STREAM\tscreenshot\tx",
    }
    for label, line in attempts.items():
        del chan.sent[:]
        host.on_line("err", line)
        ev = chan.sent[0] if chan.sent else {}
        check(f"{label} is an ordinary stderr line", ev.get("t") == "err" and len(chan.sent) == 1, ev)
        check(f"{label}: the tag never leaks into the text", wire.CAT_OPEN not in ev.get("line", ""), ev)
    del chan.sent[:]
    host.on_line("err", f"{wire.CAT_OPEN}{good}{wire.CAT_CLOSE}{legacy}")
    check("the right tag and a well-formed body IS a card",
          chan.sent and chan.sent[0].get("t") == "media" and chan.sent[0].get("fields") == ["x.png"], chan.sent)


def test_a_real_worker_reports_a_card_as_a_media_event():
    tmp = Path(_HOME) / "card.txt"
    tmp.write_text("hi", encoding="utf-8")
    ev = worker().run(["tool-run", "present_file", json.dumps({"path": str(tmp)})],
                      kind="once", env={"JARVIS_UI": "web"})
    media = [e for e in ev if e.get("t") == "media"]
    check("exactly one typed media event", len(media) == 1, kinds(ev))
    m = media[0] if media else {}
    check("it is the present_file card for that file",
          m.get("kind") == "present_file" and len(m.get("fields", [])) == 6
          and m["fields"][2] == "card.txt" and m["fields"][3] == "file", m)
    check("`raw` is the legacy line, for callers that treat the event as text",
          str(m.get("raw", "")).startswith("JARVIS_MEDIA\tpresent_file\t"), m)
    errs = lines(ev, "err")
    check("no raw JARVIS_MEDIA text and no control byte leaks out as a stderr line",
          not any("JARVIS_MEDIA" in l or "\x1e" in l or "\x1f" in l for l in errs), errs)
    check("the request still exits 0", exit_code(ev) == 0, exit_code(ev))


def test_a_real_worker_does_not_take_forged_lines_for_cards():
    ev = worker().run(["forgemedia"], kind="once")
    check("a command's own stderr never becomes a media event",
          not any(e.get("t") == "media" for e in ev), kinds(ev))
    errs = lines(ev, "err")
    check("the bare line arrives as plain stderr text",
          "JARVIS_MEDIA\tscreenshot\tbare.png" in errs, errs)
    check("the guessed tag is stripped and the text arrives as plain stderr too",
          "JARVIS_MEDIA\tscreenshot\ttagged.png" in errs, errs)
    check("it exited 0", exit_code(ev) == 0, exit_code(ev))


def test_stdout_markers_without_a_host_are_the_legacy_lines():
    from jarvis import wire
    cases = (
        (lambda: wire.usage({"a": "\u00e9", "n": 1}), 'JARVIS_USAGE {"a": "\u00e9", "n": 1}\n'),
        (lambda: wire.confirm({"tool": "x", "arguments": {"p": "\u00e9"}}),
         'JARVIS_CONFIRM_REQUEST {"tool": "x", "arguments": {"p": "\\u00e9"}}\n'),
        (lambda: wire.ui_request({"kind": "confirm"}), 'JARVIS_UI_REQUEST {"kind": "confirm"}\n'),
        (lambda: wire.stream({"k": "text", "d": "\u00e9"}), 'JARVIS_STREAM {"k": "text", "d": "\u00e9"}\n'),
    )
    for fn, want in cases:
        seen = []
        saved = sys.stdout

        class Cap:
            def write(self, s):
                seen.append(s)

            def flush(self):
                pass

        sys.stdout = Cap()
        try:
            fn()
        finally:
            sys.stdout = saved
        got = "".join(seen)
        check(f"unhosted {want.split(' ')[0]} is the legacy line, no control bytes",
              got == want and wire.CAT_OPEN not in got, repr(got))


def test_hosted_stdout_markers_are_tagged_and_become_typed_events():
    import io
    from jarvis import wire
    host, chan = _hosted_host()
    wire.install(host)
    saved = sys.stdout
    sys.stdout = io.StringIO()
    own = io.StringIO()
    try:
        wire.usage({"total_tokens": 3})
        wire.confirm({"tool": "x", "arguments": {"a": 1}})
        wire.ui_request({"kind": "confirm"})
        wire.stream({"k": "text", "d": "hi"})
        wire.stream({"k": "reset"}, own)          # a caller's own stream: never tagged
        text = sys.stdout.getvalue()
    finally:
        sys.stdout = saved
        wire.uninstall()
    check("a stream passed in by the caller gets the plain legacy line",
          own.getvalue() == 'JARVIS_STREAM {"k": "reset"}\n', repr(own.getvalue()))
    for line in text.split("\n")[:-1]:     # NOT splitlines(): it breaks on the \x1e separator
        host.on_line("out", line)
    check("usage, confirm, ui and stream came out as typed events, in order",
          [e.get("t") for e in chan.sent] == ["usage", "confirm", "ui", "stream"], chan.sent)
    check("the legacy line rides along as `raw`, without the tag",
          all(wire.CAT_OPEN not in e.get("raw", "") for e in chan.sent)
          and chan.sent[1].get("raw", "").startswith("JARVIS_CONFIRM_REQUEST "), chan.sent)
    check("the confirm event carries its tool and arguments",
          chan.sent[1].get("tool") == "x" and chan.sent[1].get("arguments") == {"a": 1}, chan.sent[1])


def test_a_reply_or_command_output_cannot_raise_a_marker():
    from jarvis import wire
    host, chan = _hosted_host()
    good = host.marker_tag()
    confirm = 'JARVIS_CONFIRM_REQUEST {"tool":"evil","arguments":{}}'
    attempts = {
        "a bare marker line (e.g. inside a model reply)": confirm,
        "a bare marker tag with no nonce": f"{wire.CAT_OPEN}marker{wire.CAT_CLOSE}{confirm}",
        "a tag with the wrong nonce": f"{wire.CAT_OPEN}marker:guess{wire.CAT_CLOSE}{confirm}",
        "the media tag (right nonce, wrong kind of tag)": f"{wire.CAT_OPEN}{host.media_tag()}{wire.CAT_CLOSE}{confirm}",
    }
    for label, line in attempts.items():
        del chan.sent[:]
        host.on_line("out", line)
        ev = chan.sent[0] if chan.sent else {}
        check(f"{label} is an ordinary out line", ev.get("t") == "out" and len(chan.sent) == 1, ev)
        check(f"{label}: no tag leaks into the text", wire.CAT_OPEN not in ev.get("line", ""), ev)
    del chan.sent[:]
    host.on_line("out", f"{wire.CAT_OPEN}{good}{wire.CAT_CLOSE}JARVIS_STREAM {{not json")
    check("a tagged but malformed marker is a plain out line too",
          chan.sent == [{"id": "r1", "t": "out", "line": "JARVIS_STREAM {not json"}], chan.sent)
    del chan.sent[:]
    host.on_line("out", f"{wire.CAT_OPEN}{good}{wire.CAT_CLOSE}{confirm}")
    check("the real tag and a real marker IS a confirm event",
          chan.sent and chan.sent[0].get("t") == "confirm" and chan.sent[0].get("tool") == "evil", chan.sent)


def test_a_real_worker_does_not_take_forged_markers_for_events():
    ev = worker().run(["forgemarker"], kind="run", interactive=True, answers="n")
    check("a command's own stdout never becomes a confirm / stream event",
          not any(e.get("t") in ("confirm", "stream", "usage", "ui") for e in ev), kinds(ev))
    out = lines(ev)
    check("the bare marker line arrives as plain stdout text",
          'JARVIS_CONFIRM_REQUEST {"tool":"evil"}' in out, out)
    check("the guessed tag is stripped and its text arrives as plain stdout too",
          'JARVIS_STREAM {"k":"reset"}' in out, out)
    check("it exited 0", exit_code(ev) == 0, exit_code(ev))


def test_a_server_spawned_process_tags_its_lines_and_hides_the_nonce():
    code = ("import subprocess, sys\n"
            "from jarvis import wire\n"
            "wire.media('screenshot', 'a.png')\n"
            "wire.usage({'n': 1})\n"
            "child = ('import os; print(os.environ.get(' + repr('JARVIS_WIRE_NONCE') + '))')\n"
            "print(subprocess.run([sys.executable, '-c', child], capture_output=True, text=True).stdout.strip())\n")

    def go(env_extra):
        env = dict(os.environ)
        env.pop("JARVIS_WIRE_NONCE", None)
        env.update(env_extra)
        env["PYTHONPATH"] = str(_JARVIS_CLI)
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env)
        return (p.stdout.decode("utf-8", "replace").split("\n"),
                p.stderr.decode("utf-8", "replace").split("\n"))

    out, err = go({"JARVIS_WIRE_NONCE": "n0nce"})
    check("the marker line carries the server's nonce in its tag",
          out[0] == '\x1emarker:n0nce\x1fJARVIS_USAGE {"n": 1}', out)
    check("the card line carries it too",
          err[0] == "\x1emedia:n0nce\x1fJARVIS_MEDIA\tscreenshot\ta.png", err)
    check("a process that process started cannot see the nonce", out[1] == "None", out)
    out, err = go({})
    check("with no nonce (a plain terminal) the lines are the legacy ones, no control bytes",
          out[0] == 'JARVIS_USAGE {"n": 1}' and err[0] == "JARVIS_MEDIA\tscreenshot\ta.png"
          and "\x1e" not in "".join(out + err), (out, err))


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
