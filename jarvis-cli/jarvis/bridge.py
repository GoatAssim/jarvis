"""`jarvis _internal_bridge` -- one warm jarvis that a front-end talks to directly.

WHY THIS EXISTS
---------------
web/server.js used to start a brand-new `jarvis` process for every request: each
ask, each panel refresh, the scheduler's 30-second tick. Starting one costs the
interpreter plus importing the whole tool catalog (over a second on a quiet
machine, more with an antivirus watching), and everything the process then said
came back as two text pipes that the server and the browser had to take apart
again with string matching -- "which stdout lines are the reply bubble?",
"which stderr line is a tool call?".

This is the other way to run jarvis: a single long-lived process that executes
the same requests one after another, in-process, through the same `cli.main()`,
and answers over a typed, line-delimited JSON channel on its own stdin/stdout.

WHAT IT IS NOT
--------------
* It is not required. `jarvis ...` on a terminal never touches this module;
  `cli.py` imports it only to serve the one hidden `_internal_bridge`
  subcommand. The server may not run without jarvis, jarvis runs without the
  server -- and the server itself falls back to starting a process per request
  whenever a worker is missing, busy, stale, or misbehaves.
* It is not a second implementation of anything. A request is `argv` + an
  environment overlay, run through `cli.main()` exactly as a fresh process
  would run it. Execution logic is never copied here.
* It is not shared between requests. One request at a time per worker (the
  process environment, cwd and stdio are process-global); the server keeps a
  small pool and starts a plain process when every worker is busy.

THE WIRE (protocol 1)
---------------------
One JSON object per line, UTF-8, both directions. The worker's own stdout is
the channel, so the worker moves its real stdout/stderr (file descriptors 1 and
2, which is what a grandchild process such as a saved command's shell inherits)
onto private pipes and reports what comes out of them as events; nothing but
protocol is ever written to the real channel.

  server -> worker
    {"op":"run","id":"r1","argv":[...],"env":{"K":"V","DROP":null},
     "stdin":"text"|null,"interactive":bool,"kind":"ask"|"run"|"once"}
    {"op":"answer","id":"r1","line":"y"}        one line for the request's stdin
    {"op":"ping"}  {"op":"shutdown"}

  worker -> server
    {"t":"ready","proto":1,"pid":N,"version":"..."}      once, after warm-up
    {"id":"r1","t":"started","pid":N}          the request began executing
    {"id":"r1","t":"out","line":"..."}                    one stdout line
    {"id":"r1","t":"err","line":"...","cat":"tool-call"}  one stderr line; `cat`
                                               only when the CLI said what it is
    {"id":"r1","t":"stream","ev":{...}}        a live model event (was JARVIS_STREAM)
    {"id":"r1","t":"usage","usage":{...},"raw":"..."}     (was JARVIS_USAGE)
    {"id":"r1","t":"confirm","tool":..,"arguments":..,"risk_note":..,"raw":"..."}
    {"id":"r1","t":"ui","request":{...},"raw":"..."}      (was JARVIS_UI_REQUEST)
    {"id":"r1","t":"media","kind":"screenshot","fields":["a.png"],"raw":"..."}
                                               a card for the front end (was a
                                               bare JARVIS_MEDIA stderr line)
    {"id":"r1","t":"reply","name":"Jarvis","text":"...","ok":true}   the bubble
    {"id":"r1","t":"exit","code":0,"ms":123}   always last for a started request
    {"id":"r1","t":"refuse","why":"..."}       not run: use a normal process
    {"id":"r1","t":"retire","why":"..."}       not run: this worker is stale
    {"t":"pong"}  {"t":"fatal","error":"..."}

`refuse` and `retire` are only ever sent BEFORE `started`, so the caller can
always re-run that request as a normal process without it having done anything
twice. Once `started` has been sent the request is the worker's.

The marker lines the CLI has always printed for a web front-end
(JARVIS_STREAM / JARVIS_USAGE / JARVIS_CONFIRM_REQUEST / JARVIS_UI_REQUEST) are
turned into the typed events above, in order, by the stdout reader below. The
CLI prints them through wire.stream / usage / confirm / ui_request (unhosted:
the same legacy line as ever). A malformed marker stays an ordinary `out` line,
exactly as the server treated it before.

Stdout markers are only believed when the CLI wrote them through
`wire.stream/usage/confirm/ui_request`: hosted, those lines carry an in-band tag
with the per-request nonce (`Host.marker_tag()`), and `Host.on_line` turns a tagged
line into the event above and an UNtagged one into a plain `out` line. The reply is
model output printed on the same stdout, so this is what stops a reply (or a
command's output) from raising a fake confirmation or injecting stream text.

Media cards are the stderr-side version of the same idea. Code calls
`wire.media(kind, *fields)`; while this host is running a request that prints the
legacy `JARVIS_MEDIA<TAB>kind<TAB>...` line behind an in-band tag that carries a
per-request nonce (`Host.media_tag()`, see wire.py), and `Host.on_line` turns it
into the `media` event above, in stderr order. A line with no tag, a wrong nonce
or a malformed body is an ordinary `err` line, so nothing a command or a tool
echoes can draw a card; `raw` is the legacy line for callers that treat the
event as text (a panel refresh, a direct run).

HONEST LIMITS
-------------
A warm process is not a fresh one. Three things keep that from mattering:
  1. STALENESS. Tool discovery and a few other things happen at import. Before
     each request the worker compares a fingerprint of the jarvis source, the
     custom-tool folder and the MCP config with the one it booted with; if they
     differ it answers `retire` and the caller uses a fresh worker. A patch
     applied while the web UI is running therefore takes effect on the next
     request, as it always did.
  2. PROCESS-EXIT WORK. Things that used to happen when each short-lived
     process ended (closing the browser a tool opened, MCP server sessions,
     the Everything app jarvis launched) are run at the end of every request
     (`_end_of_request`). A new such resource needs adding there.
  3. RECYCLING. The caller restarts a worker after a bounded number of
     requests, and workers that sit idle are shut down.
AGENTS.md still holds: never keep state in a module global between calls.
"""

import hashlib
import io
import json
import os
import queue
import secrets
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from . import wire

PROTO = 1

# Commands that are, by nature, a process of their own: a standing loop, a
# microphone, an interactive flow. They are answered `refuse` and the caller
# starts them the old way.
UNHOSTED = frozenset({
    "_internal_bridge", "sched-daemon", "browser-daemon", "daemon-run",
    "discord-daemon", "instagram-serve", "clipboard-watch", "ambient",
    "listen", "speak", "transcribe", "spotify-login", "browser-setup",
})

# A line with no newline in this many bytes is emitted as it is rather than
# buffered without limit.
MAX_PARTIAL_LINE = 8 * 1024 * 1024
# How long the end-of-request barrier waits for both output pipes to drain.
DRAIN_SECONDS = 5.0

_STREAM = "JARVIS_STREAM "
_USAGE = "JARVIS_USAGE "
_CONFIRM = "JARVIS_CONFIRM_REQUEST "
_UI = "JARVIS_UI_REQUEST "


def _dec(raw):
    return raw.decode("utf-8", "replace")


def _exit_code(code):
    """SystemExit's argument as the integer a process would have exited with."""
    if code is None:
        return 0
    if isinstance(code, bool):
        return int(code)
    if isinstance(code, int):
        return code
    try:
        sys.stderr.write(str(code) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return 1


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------

def source_fingerprint(package_dir=None, home=None):
    """A cheap digest of everything a warm process read once, at import.

    Covers: every .py file of this package (so a pulled or patched jarvis is
    picked up), ~/.jarvis/tools/*.py plus that folder's own listing (custom
    tools, personas and tool screens are discovered at import), and the two
    MCP files tools.py reads when it builds the catalog. Sizes and mtimes
    only -- no file is opened.
    """
    parts = []
    pkg = Path(package_dir) if package_dir else Path(__file__).resolve().parent

    def walk(directory):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name != "__pycache__":
                        walk(entry.path)
                elif entry.name.endswith(".py"):
                    st = entry.stat()
                    parts.append((entry.path, st.st_mtime_ns, st.st_size))
            except OSError:
                continue

    walk(pkg)
    jarvis_dir = (Path(home) if home else Path.home()) / ".jarvis"
    tools_dir = jarvis_dir / "tools"
    try:
        for entry in os.scandir(tools_dir):
            if entry.name.endswith(".py") and entry.is_file():
                st = entry.stat()
                parts.append((entry.path, st.st_mtime_ns, st.st_size))
            else:
                parts.append((entry.path, 0, 0))   # names only: a new folder or file counts
    except OSError:
        parts.append((str(tools_dir), -1, -1))
    for name in ("mcp_config.json", "mcp_cache.json"):
        path = jarvis_dir / name
        try:
            st = path.stat()
            parts.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((str(path), -1, -1))
    parts.sort()
    return hashlib.sha1(repr(parts).encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

class _Channel:
    """The protocol side: thread-safe, whole-line writes to the real stdout."""

    def __init__(self, fd):
        self._fd = fd
        self._lock = threading.Lock()
        self.broken = False

    def send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
                + "\n").encode("utf-8", "replace")
        with self._lock:
            if self.broken:
                return False
            try:
                view = memoryview(data)
                while len(view):
                    written = os.write(self._fd, view)
                    view = view[written:]
            except OSError:
                self.broken = True   # the server went away; the control loop sees EOF too
                return False
        return True


class _PipeReader(threading.Thread):
    """Turns one capture pipe (what fd 1 or fd 2 now points at) into lines."""

    def __init__(self, fd, stream, host):
        super().__init__(daemon=True, name="jarvis-bridge-" + stream)
        self._fd = fd
        self._stream = stream
        self._host = host

    def run(self):
        buf = b""
        while True:
            try:
                chunk = os.read(self._fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf = self._consume(buf + chunk)
        if buf:
            self._host.on_line(self._stream, _dec(buf))

    def _consume(self, buf):
        while True:
            token = self._host.eor_token
            at = buf.find(token) if token else -1
            nl = buf.find(b"\n")
            if at != -1 and (nl == -1 or at < nl):
                before = buf[:at]
                if before:
                    self._host.on_line(self._stream, _dec(before))
                buf = buf[at + len(token):]
                self._host.mark_drained(self._stream)
                continue
            if nl == -1:
                break
            line, buf = buf[:nl], buf[nl + 1:]
            self._host.on_line(self._stream, _dec(line))
        if len(buf) > MAX_PARTIAL_LINE:
            self._host.on_line(self._stream, _dec(buf))
            buf = b""
        return buf


class _StdinFeeder:
    """A request's stdin: a real pipe on fd 0, fed by one writer thread.

    What a process spawned with a stdin pipe would have: the request's
    `stdin` text first (then EOF unless the request is interactive), and, for
    an interactive one, each `answer` line as it arrives. A thread does the
    writing because a payload bigger than the pipe's buffer would otherwise
    block the control loop until the command chose to read it.
    """

    def __init__(self):
        self._q = queue.Queue()
        self._w = None
        self._thread = None
        self._devnull = None

    def begin(self, payload, interactive):
        r, w = os.pipe()
        os.dup2(r, 0)
        os.close(r)
        self._w = w
        sys.stdin = io.TextIOWrapper(
            io.BufferedReader(io.FileIO(0, "rb", closefd=False)),
            encoding="utf-8", errors="replace")
        if payload:
            self._q.put(payload.encode("utf-8", "replace"))
        if not interactive:
            self._q.put(None)
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="jarvis-bridge-stdin")
        self._thread.start()

    def push(self, line):
        self._q.put((line + "\n").encode("utf-8", "replace"))

    def _pump(self):
        w = self._w
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                view = memoryview(item)
                while len(view):
                    n = os.write(w, view)
                    view = view[n:]
        except OSError:
            pass   # the request ended and fd 0 went away under us
        finally:
            try:
                os.close(w)
            except OSError:
                pass

    def end(self):
        # Point fd 0 at nothing first: that closes the pipe's read end, which
        # also unblocks a write the command never read.
        try:
            self._devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(self._devnull, 0)
            os.close(self._devnull)
        except OSError:
            pass
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(2.0)
        sys.stdin = io.TextIOWrapper(io.BytesIO(b""), encoding="utf-8")


# --------------------------------------------------------------------------
# The host
# --------------------------------------------------------------------------

class Host:
    def __init__(self, channel, diag_fd):
        self.chan = channel
        self._diag_fd = diag_fd
        self.current = None            # {"id", "kind"} while a request runs
        self._media_nonce = None       # per request; never leaves this process (see media_tag)
        self.boot_err = []             # stderr lines printed while warming up
        self.jobs = queue.Queue()
        self.eor_token = b""
        self._drained = {"out": threading.Event(), "err": threading.Event()}
        self._feeder = None
        self.fp0 = None
        self.served = 0

    # -- diagnostics (the server keeps the tail of the process's real stderr) --
    def diag(self, text):
        try:
            os.write(self._diag_fd, (text.rstrip("\n") + "\n").encode("utf-8", "replace"))
        except OSError:
            pass

    # -- called by the pipe readers --------------------------------------------
    def on_line(self, stream, text):
        cur = self.current
        if cur is None:
            if stream == "err" and len(self.boot_err) < 400:
                self.boot_err.append(text)
            return
        rid = cur["id"]
        if text.endswith("\r"):
            text = text[:-1]   # Windows text mode ends lines "\r\n"; a line is its text
        if stream == "err":
            cat = None
            if text.startswith(wire.CAT_OPEN):
                end = text.find(wire.CAT_CLOSE, 1)
                if end > 0:
                    tag = text[1:end]
                    text = text[end + 1:]
                    if tag == self.media_tag():
                        media = self._media_event(rid, text)
                        if media is not None:
                            self.chan.send(media)
                            return
                    cat = tag if tag in wire.CATEGORIES else None
            ev = {"id": rid, "t": "err", "line": text}
            if cat:
                ev["cat"] = cat
            self.chan.send(ev)
        else:
            # Only a line the CLI itself wrote with wire.stream/usage/confirm/ui_request is
            # a marker. Everything else -- including a model reply or a command's output
            # that happens to start with a marker prefix -- is an ordinary `out` line.
            trusted = False
            if text.startswith(wire.CAT_OPEN):
                end = text.find(wire.CAT_CLOSE, 1)
                if end > 0:
                    trusted = text[1:end] == self.marker_tag()
                    text = text[end + 1:]
            if trusted:
                self.chan.send(self._translate_out(rid, text))
            else:
                self.chan.send({"id": rid, "t": "out", "line": text})

    def marker_tag(self):
        """The in-band tag a `wire.stream/usage/confirm/ui_request` line is written with.

        `marker:<nonce>`, the same per-request nonce as `media_tag()` and kept just as
        private (see there); None between requests.
        """
        nonce = self._media_nonce
        return None if nonce is None else wire.MARKER_TAG + ":" + nonce

    def media_tag(self):
        """The in-band tag a `wire.media()` line is written with, or None.

        `media:<nonce>`, where the nonce is made up when a request starts and
        lives only in this object: it is not in the environment, so a command or
        subprocess a tool starts cannot learn it and cannot write a line this
        host will take for a card. None between requests (nothing is listening
        for a card then, and a line written with no tag is plain stderr).
        """
        nonce = self._media_nonce
        return None if nonce is None else wire.MEDIA_TAG + ":" + nonce

    @staticmethod
    def _media_event(rid, text):
        """`JARVIS_MEDIA<TAB>kind<TAB>field...` as a `media` event, else None.

        None means malformed (wrong prefix, no kind): the caller then reports the
        line as an ordinary stderr line, the same policy as the stdout markers.
        """
        parts = text.split("\t")
        if len(parts) < 2 or parts[0] != wire.MEDIA_PREFIX or not parts[1]:
            return None
        return {"id": rid, "t": "media", "kind": parts[1], "fields": parts[2:], "raw": text}

    @staticmethod
    def _translate_out(rid, line):
        for prefix, kind in ((_STREAM, "stream"), (_USAGE, "usage"),
                             (_CONFIRM, "confirm"), (_UI, "ui")):
            if not line.startswith(prefix):
                continue
            try:
                obj = json.loads(line[len(prefix):])
            except ValueError:
                break
            if not isinstance(obj, dict):
                break
            if kind == "stream":
                return {"id": rid, "t": "stream", "ev": obj}
            if kind == "usage":
                return {"id": rid, "t": "usage", "usage": obj, "raw": line}
            if kind == "confirm":
                return {"id": rid, "t": "confirm", "tool": obj.get("tool"),
                        "arguments": obj.get("arguments") or {},
                        "risk_note": obj.get("risk_note"), "raw": line}
            return {"id": rid, "t": "ui", "request": obj, "raw": line}
        return {"id": rid, "t": "out", "line": line}

    def mark_drained(self, stream):
        self._drained[stream].set()

    # -- called through wire.reply() --------------------------------------------
    def reply(self, name, text, ok):
        cur = self.current
        if cur is not None:
            self.chan.send({"id": cur["id"], "t": "reply", "name": name,
                            "text": text, "ok": ok})

    # -- one request --------------------------------------------------------------
    def _drain(self):
        """Wait until everything the request wrote has been reported.

        Both pipes are ordered, so a unique marker written after the request's
        last byte comes out of each reader after all of that request's lines.
        """
        for fn in (sys.stdout, sys.stderr):
            try:
                fn.flush()
            except Exception:  # noqa: BLE001
                pass
        token = ("\x00JBEOR\x00%x\x00" % time.monotonic_ns()).encode("ascii")
        for ev in self._drained.values():
            ev.clear()
        self.eor_token = token
        for fd in (1, 2):
            try:
                os.write(fd, token)
            except OSError:
                pass
        deadline = time.monotonic() + DRAIN_SECONDS
        for ev in self._drained.values():
            ev.wait(max(0.0, deadline - time.monotonic()))
        self.eor_token = b""

    @staticmethod
    def _end_of_request():
        """What the end of a short-lived process used to do for free."""
        for module, fn in (("jarvis.mcp_client", "close_all"),
                           ("jarvis.browser_tools", "close_browser"),
                           ("jarvis.everything_tools", "_close_if_launched")):
            mod = sys.modules.get(module)
            func = getattr(mod, fn, None) if mod is not None else None
            if callable(func):
                try:
                    func()
                except Exception:  # noqa: BLE001
                    pass
        # Everything.exe is closed only if THIS process launched it; having
        # done so, a later request must not close a copy the user started.
        ev_tools = sys.modules.get("jarvis.everything_tools")
        state = getattr(ev_tools, "_launch_state", None) if ev_tools else None
        if isinstance(state, dict):
            state["launched"] = False

    def _refuse(self, rid, why):
        self.chan.send({"id": rid, "t": "refuse", "why": why})

    def run_job(self, job):
        rid = str(job.get("id") or "")
        argv = [str(a) for a in (job.get("argv") or [])]
        kind = str(job.get("kind") or "once")
        if not rid or not argv:
            return self._refuse(rid, "empty request")
        if self.current is not None:
            return self._refuse(rid, "busy")
        if argv[0] in UNHOSTED:
            return self._refuse(rid, "%s needs a process of its own" % argv[0])
        try:
            fresh = source_fingerprint() == self.fp0
        except Exception:  # noqa: BLE001 -- can't tell: don't trust this process
            fresh = False
        if not fresh:
            self.chan.send({"id": rid, "t": "retire",
                            "why": "jarvis or its tools changed since this worker started"})
            self.jobs.put(None)
            return None

        overlay = {}
        for key, value in (job.get("env") or {}).items():
            overlay[str(key)] = None if value is None else str(value)

        self.current = {"id": rid, "kind": kind}
        self._media_nonce = secrets.token_hex(8)
        began = time.monotonic()
        self.chan.send({"id": rid, "t": "started", "pid": os.getpid()})
        if kind == "ask":
            # What a fresh process printed while loading its tools, every time.
            for text in self.boot_err:
                ev = {"id": rid, "t": "err", "line": text}
                if text.startswith(("[tools]", "[personas]")):
                    ev["cat"] = "discovery"
                self.chan.send(ev)

        saved_env = dict(os.environ)
        saved_cwd = os.getcwd()
        saved_argv = list(sys.argv)
        saved_stdin = sys.stdin
        code = 1
        feeder = _StdinFeeder()
        try:
            try:
                for key, value in overlay.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                payload = job.get("stdin")
                feeder.begin(payload if isinstance(payload, str) else None,
                             bool(job.get("interactive")))
                self._feeder = feeder
                sys.argv = ["jarvis"] + argv
                from . import cli
                try:
                    cli.main()
                    code = 0
                except SystemExit as exc:
                    code = _exit_code(exc.code)
                except KeyboardInterrupt:
                    code = 130
            except BaseException:  # noqa: BLE001 -- what an uncaught error in a process does
                traceback.print_exc()
                code = 1
        finally:
            self._end_of_request()
            self._drain()
            self._feeder = None
            feeder.end()
            os.environ.clear()
            os.environ.update(saved_env)
            try:
                os.chdir(saved_cwd)
            except OSError:
                pass
            sys.argv = saved_argv
            sys.stdin = saved_stdin
            self.served += 1
            self.chan.send({"id": rid, "t": "exit", "code": code,
                            "ms": int((time.monotonic() - began) * 1000)})
            self.current = None
            self._media_nonce = None
        return None

    def answer(self, msg):
        cur, feeder = self.current, self._feeder
        if cur is None or feeder is None or str(msg.get("id") or "") != cur["id"]:
            return
        line = msg.get("line")
        feeder.push("" if line is None else str(line).replace("\r", " ").replace("\n", " "))

    # -- dying ----------------------------------------------------------------------
    def hard_exit(self, code, label=None):
        """Leave now, the way a killed process does -- but record an unanswered
        turn as interrupted first, which is what cli.handle_ai_prompt's own
        signal handler does for a fresh process."""
        if label:
            mod = sys.modules.get("jarvis.ai_client")
            try:
                if mod is not None:
                    mod.abandon_pending_turn(label)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._end_of_request()
        except Exception:  # noqa: BLE001
            pass
        os._exit(code)


def _control_loop(host, stream):
    """Read the server's messages. Runs on its own thread so an `answer` or a
    shutdown is heard while a request is busy on the main thread."""
    try:
        for raw in iter(stream.readline, b""):
            try:
                msg = json.loads(_dec(raw))
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            op = msg.get("op")
            if op == "run":
                host.jobs.put(msg)
            elif op == "answer":
                host.answer(msg)
            elif op == "ping":
                host.chan.send({"t": "pong"})
            elif op == "shutdown":
                break
    except Exception:  # noqa: BLE001
        pass
    # EOF means the server is gone; a shutdown message means it asked nicely.
    if host.current is not None:
        host.hard_exit(0, "interrupted")
    host.jobs.put(None)


def serve():
    """Entry point of `jarvis _internal_bridge`. Never returns normally."""
    proto_out = os.dup(1)      # the real channel; not inherited by grandchildren
    proto_in = os.dup(0)
    diag = os.dup(2)           # the process's real stderr: crash text only
    chan = _Channel(proto_out)
    host = Host(chan, diag)

    # Move fds 0/1/2 onto private pipes (see the module docstring).
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    os.dup2(out_w, 1)
    os.dup2(err_w, 2)
    os.close(out_w)
    os.close(err_w)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    _PipeReader(out_r, "out", host).start()
    _PipeReader(err_r, "err", host).start()

    def on_signal(signum, _frame):
        host.hard_exit(130, "interrupted" if signum == getattr(signal, "SIGTERM", None)
                       else "cancelled")

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass

    began = time.monotonic()
    try:
        from . import cli  # noqa: F401 -- the dispatcher every request runs through
        try:
            from . import ai_client  # noqa: F401 -- the heavy import an ask would pay for
        except Exception:  # noqa: BLE001 -- AI extras missing: asks will say so themselves
            pass
        # Taken AFTER the imports: loading the tool catalog creates ~/.jarvis/tools
        # (and its README) the first time, which must not count as a change.
        host.fp0 = source_fingerprint()
    except BaseException:  # noqa: BLE001
        chan.send({"t": "fatal", "error": traceback.format_exc(limit=3)})
        host.diag(traceback.format_exc())
        os._exit(1)

    wire.install(host)
    version = ""
    try:
        from . import build_info
        version = build_info.version_string()
    except Exception:  # noqa: BLE001
        pass
    chan.send({"t": "ready", "proto": PROTO, "pid": os.getpid(), "version": version,
               "boot_ms": int((time.monotonic() - began) * 1000)})

    threading.Thread(target=_control_loop,
                     args=(host, os.fdopen(proto_in, "rb")),
                     daemon=True, name="jarvis-bridge-control").start()
    while True:
        job = host.jobs.get()
        if job is None:
            break
        try:
            host.run_job(job)
        except BaseException:  # noqa: BLE001 -- the worker outlives any one request
            host.diag(traceback.format_exc())
            if host.current is not None:
                chan.send({"id": host.current["id"], "t": "exit", "code": 1, "ms": 0})
                host.current = None
                host._media_nonce = None
    host.hard_exit(0)
