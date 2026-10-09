"""L.10 - the live console window for a daemon (`jarvis daemon-viewer <id>`).

This is the process that runs INSIDE the console window `daemons.
open_console_window()` opens. It is a viewer, not the daemon: the supervisor
and the service keep running windowless (H.1.1) and keep writing console.log;
this just follows that file and, for a service that reads stdin, relays what
you type to it through the same queue the panel's input box uses.

  * Shows the last lines already written, then follows new ones. Survives the
    log rotating underneath it (console.log -> console.log.1) and the daemon
    stopping and starting again - the window stays up and keeps following.
  * Ctrl+C or closing the window ends the VIEWER. The daemon is stopped too
    only if the owner switched on "Stop the daemon when this window is
    closed" (`console_window_stop_on_close`) - then either way of leaving asks
    it to stop, politely (the stop flag, never a force-kill), because Windows
    gives a closing console only a few seconds.
  * One window per daemon: the pid is recorded in the daemon's status so a
    second open finds it instead of stacking another.
"""
import codecs
import os
import sys
import threading
import time

BACKLOG_LINES = 200
POLL_SECONDS = 0.25
CTRL_CLOSE_EVENT = 2


class Follower:
    """Follow a text file that grows, gets rotated or truncated, and may not
    exist yet. Pure file logic - no printing - so it is testable on its own.

    start() returns the last `backlog` lines; poll() returns whatever
    complete lines were appended since the previous call. A line is only
    returned once its newline has arrived, so a half-written line is never
    shown in two pieces.
    """

    def __init__(self, path):
        self.path = str(path)
        self._pos = 0
        self._ident = None
        self._partial = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @staticmethod
    def _identity(st):
        # st_ino is 0 on some Windows filesystems; then size alone detects it.
        return (st.st_dev, st.st_ino) if st.st_ino else None

    def _reset(self):
        self._pos = 0
        self._partial = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def start(self, backlog=BACKLOG_LINES):
        try:
            with open(self.path, "rb") as fh:
                st = os.fstat(fh.fileno())
                size = st.st_size
                # Enough bytes for `backlog` ordinary lines; a few long ones
                # just mean we show fewer. Never read a whole huge log.
                window = min(size, max(64 * 1024, backlog * 512))
                fh.seek(size - window)
                data = fh.read(window)
                self._pos = size
                self._ident = self._identity(st)
        except OSError:
            self._reset()
            self._ident = None
            return []
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        if size > window:
            lines = lines[1:]          # the first one is cut mid-line
        if lines and lines[-1] == "":
            lines.pop()
        elif lines:
            # The log ends mid-line: hold it until its newline arrives.
            self._partial = lines.pop()
        return [ln.rstrip("\r") for ln in lines[-backlog:]]

    def poll(self):
        try:
            st = os.stat(self.path)
        except OSError:
            # Gone (between rotation and the next write): nothing to show,
            # and whatever shows up next is a new file.
            if self._pos or self._ident:
                self._reset()
                self._ident = None
            return []
        ident = self._identity(st)
        rotated = st.st_size < self._pos or (
            self._ident is not None and ident is not None and ident != self._ident)
        if rotated:
            self._reset()
        self._ident = ident
        if st.st_size == self._pos:
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
                self._pos = fh.tell()
        except OSError:
            return []
        text = self._partial + self._decoder.decode(chunk)
        parts = text.split("\n")
        self._partial = parts.pop()
        return [p.rstrip("\r") for p in parts]


def _console_setup(title):
    """Windows console: a title, and UTF-8 so a service's non-ASCII output
    is not turned into question marks or an encoding error."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.SetConsoleTitleW(title)
        kernel.SetConsoleOutputCP(65001)
        kernel.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001 - cosmetic
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _finish(did, me, stop_on_close, reason, out):
    """Everything that must happen when the viewer ends, exactly once."""
    from . import daemons
    try:
        if daemons._read_status(did).get("console_window_pid") == me:
            daemons.note_console_window(did, None)
    except Exception:  # noqa: BLE001
        pass
    if stop_on_close:
        try:
            ok, message = daemons.request_stop(did)
            print(f"[{reason}] {message}", file=out, flush=True)
        except Exception:  # noqa: BLE001
            pass


def run_viewer(daemon_id, out=None, inp=None, poll=POLL_SECONDS,
               install_close_handler=True, backlog=BACKLOG_LINES):
    """Run the viewer until it is closed. Returns an exit code."""
    from . import daemons
    out = out or sys.stdout
    inp = inp or sys.stdin
    did = daemons.normalize_id(daemon_id)
    entry = daemons.get(did)
    if not entry:
        print(f"no daemon '{did}'", file=out, flush=True)
        return 1

    other = daemons._console_window_pid(did)
    if other:
        print(f"A console window for '{did}' is already open (pid {other}).",
              file=out, flush=True)
        return 1

    me = os.getpid()
    _console_setup(f"Jarvis - {entry.get('name') or did}")
    daemons.note_console_window(did, me)

    done = threading.Event()
    finished = threading.Lock()
    state = {"over": False}

    def end(reason):
        # Called from the main thread, or from Windows' close handler thread.
        with finished:
            if state["over"]:
                return
            state["over"] = True
        current = daemons.get(did) or {}
        _finish(did, me, bool(current.get("console_window_stop_on_close")),
                reason, out)

    if install_close_handler and os.name == "nt":
        try:
            import ctypes
            handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

            def on_ctrl(ctrl_type):
                if ctrl_type == CTRL_CLOSE_EVENT:
                    end("window closed")
                    done.set()
                    return 1
                return 0               # Ctrl+C etc: the normal KeyboardInterrupt

            handler = handler_type(on_ctrl)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
            run_viewer._keep = handler   # must outlive this call or it is collected
        except Exception:  # noqa: BLE001
            pass

    follower = Follower(daemons.console_path(did))
    try:
        for line in follower.start(backlog):
            print(line, file=out)
        out.flush()
    except Exception:  # noqa: BLE001
        pass

    accepts = bool(entry.get("supports_stdin"))
    banner = ("Type a line and press Enter to send it to the service. "
              if accepts else "Read-only - this service does not read input. ")
    ends = ("Closing this window (or Ctrl+C) will stop the service."
            if entry.get("console_window_stop_on_close")
            else "Closing this window leaves the service running.")
    print(f"--- {entry.get('name') or did} - live console. {banner}{ends}",
          file=out, flush=True)

    def tail():
        last_state = None
        while not done.is_set():
            try:
                for line in follower.poll():
                    print(line, file=out)
                out.flush()
                now = daemons.status(did)
                current = "running" if now.get("running") else "not running"
                if last_state is not None and current != last_state:
                    print(f"--- service is now {current}", file=out, flush=True)
                last_state = current
            except Exception:  # noqa: BLE001 - a hiccup must not end the window
                pass
            done.wait(poll)

    thread = threading.Thread(target=tail, daemon=True)
    thread.start()

    reason = "viewer closed"
    try:
        if accepts:
            while not done.is_set():
                line = inp.readline()
                if line == "":          # input closed
                    reason = "input closed"
                    break
                text = line.rstrip("\r\n")
                current = daemons.get(did) or {}
                if not current.get("supports_stdin"):
                    print("--- this service does not read input", file=out, flush=True)
                    continue
                ok, message = daemons.send_input(did, text)
                if not ok:
                    print(f"--- not sent: {message}", file=out, flush=True)
        else:
            while not done.wait(0.5):
                pass
    except KeyboardInterrupt:
        reason = "Ctrl+C"
    finally:
        done.set()
        end(reason)
    return 0
