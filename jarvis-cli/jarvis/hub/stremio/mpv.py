"""Control of the mpv that Jarvis started (brief 22).

`player.launch` starts mpv with `--input-ipc-server=<endpoint>` and writes a record (`player.json`: pid, player,
ipc, started_at).  This module talks to that endpoint, and to nothing else:

  * ONLY the player Jarvis started: the record must exist, say `mpv`, name the endpoint `player.ipc_endpoint()`
    returns, and its process must still be alive.  A stale record (the process is gone) is refused, and no other
    program, pid or socket is ever touched.
  * ONLY the commands in ACTIONS.  The model passes an action name and a number, never a command: every command
    sent to mpv is built here from the table below.  Properties set: `pause` and `volume`, nothing else.
  * One connection per call, one JSON command with a request id, read until the reply with the SAME request id
    (mpv's own events have none and are skipped, as is a line that isn't JSON), a timeout of 3 seconds, closed
    after.  A reply that never comes is a plain sentence, not a hang.

mpv's real replies look like {"error":"success","data":...,"request_id":N} (that is what the tests' fake mpv
sends; nothing here has run against a real mpv).

The Unix socket branch (Linux, macOS) is tested against a fake mpv on Linux.  The Windows named-pipe branch
(`_exchange_pipe`) is WRITTEN BUT NOT RUN: nothing in this module has run on Windows.
"""

import json
import math
import os
import socket
import sys
import threading
import time

from . import player

TIMEOUT_S = 3.0
MAX_SEEK_S = 24 * 3600
ACTIONS = ("pause", "resume", "toggle", "seek", "stop", "volume", "status")


class MpvError(Exception):
    """A plain sentence for the owner."""


# --------------------------------------------------------------------------- is it OUR mpv, and alive?

def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):                      # written, not run
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259                                # STILL_ACTIVE
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)                                      # signal 0: only asks whether the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def our_player():
    """-> (record, None) for the live mpv Jarvis started, else (None, one sentence)."""
    try:
        from ... import atomic_io
        record = atomic_io.read_json(player.state_path(), default=None)
    except Exception:  # noqa: BLE001
        record = None
    if not isinstance(record, dict) or not record:
        return None, ("Nothing is playing that I started. play_control only controls the mpv that play_episode "
                      "or play_local opened.")
    if record.get("player") != "mpv":
        return None, "The player I started is %s, and only mpv can be controlled." % (str(record.get("player") or "not mpv")[:20])
    if not record.get("ipc") or record.get("ipc") != player.ipc_endpoint():
        return None, "The player I started has no control endpoint, so I can't control it."
    if not pid_alive(record.get("pid")):
        return None, "The player I started is no longer running."
    return record, None


# --------------------------------------------------------------------------- one command, one connection

def _decode_reply(line, request_id):
    """A line from mpv -> the reply for our request id, or None (an event, or garbage: keep reading)."""
    try:
        message = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("request_id") != request_id:
        return None
    return message


def _exchange_unix(endpoint, payload, request_id, timeout):
    deadline = time.monotonic() + timeout
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(endpoint)
        sock.sendall(payload)
        buffer = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise MpvError("mpv didn't reply within %d seconds." % int(timeout))
            sock.settimeout(left)
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                raise MpvError("mpv didn't reply within %d seconds." % int(timeout))
            if not chunk:
                raise MpvError("mpv closed the connection without answering (it may be closing).")
            buffer += chunk
            if len(buffer) > 1024 * 1024:
                raise MpvError("mpv sent more than expected; I stopped listening.")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                reply = _decode_reply(line, request_id)
                if reply is not None:
                    return reply
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _exchange_pipe(endpoint, payload, request_id, timeout):
    """The Windows named pipe.  WRITTEN BUT NOT RUN (no Windows here).  A pipe has no portable read timeout, so
    the exchange runs on a daemon thread and the caller waits for it with the timeout."""
    result = {}

    def work():
        try:
            with open(endpoint, "r+b", buffering=0) as pipe:
                pipe.write(payload)
                buffer = b""
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        result["error"] = "mpv closed the connection without answering (it may be closing)."
                        return
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        reply = _decode_reply(line, request_id)
                        if reply is not None:
                            result["reply"] = reply
                            return
        except OSError as exc:
            result["error"] = "I couldn't reach mpv's control pipe (%s)." % type(exc).__name__

    thread = threading.Thread(target=work, name="mpv-pipe", daemon=True)
    thread.start()
    thread.join(timeout)
    if "reply" in result:
        return result["reply"]
    raise MpvError(result.get("error") or "mpv didn't reply within %d seconds." % int(timeout))


_next_id = [0]


def send(endpoint, command, timeout=TIMEOUT_S):
    """Send ONE command (a list) to mpv at `endpoint`; -> the reply's `data`.  Raises MpvError with a sentence.
    Not for the model: callers pass commands built from the table in `run()`."""
    _next_id[0] += 1
    request_id = _next_id[0]
    payload = (json.dumps({"command": command, "request_id": request_id}) + "\n").encode("utf-8")
    try:
        if sys.platform.startswith("win"):
            reply = _exchange_pipe(endpoint, payload, request_id, timeout)
        else:
            reply = _exchange_unix(endpoint, payload, request_id, timeout)
    except MpvError:
        raise
    except (ConnectionRefusedError, FileNotFoundError):
        raise MpvError("mpv isn't answering on its control socket (it may have been closed).")
    except OSError as exc:
        raise MpvError("I couldn't talk to mpv (%s)." % type(exc).__name__)
    error = str(reply.get("error") or "")
    if error != "success":
        raise MpvError("mpv said: %s." % (error[:80] or "no"))
    return reply.get("data")


# --------------------------------------------------------------------------- the actions

def _number(value, label):
    if isinstance(value, bool):
        raise MpvError("%s must be a number." % label)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MpvError("%s must be a number." % label)
    if not math.isfinite(number):
        raise MpvError("%s must be a number." % label)
    return number


def _seconds(value):
    return None if value is None else round(float(value), 1)


def run(action, seconds=None, volume=None, timeout=TIMEOUT_S):
    """Do one named action on the mpv Jarvis started.  -> {ok, ...} or {ok: False, error}.  Never raises."""
    try:
        action = str(action or "").strip().lower()
        if action not in ACTIONS:
            return {"ok": False, "error": "action must be one of: %s." % ", ".join(ACTIONS)}
        # the arguments are checked BEFORE anything is sent
        command = None
        if action == "seek":
            if seconds is None:
                raise MpvError("seek needs seconds (positive to skip ahead, negative to go back).")
            amount = _number(seconds, "seconds")
            if abs(amount) > MAX_SEEK_S:
                raise MpvError("seconds must be within a day either way.")
            command = ["seek", amount, "relative"]
        elif action == "volume":
            if volume is None:
                raise MpvError("volume needs a number from 0 to 100.")
            level = _number(volume, "volume")
            if not 0 <= level <= 100:
                raise MpvError("volume must be from 0 to 100.")
            command = ["set_property", "volume", int(round(level))]
        elif action == "pause":
            command = ["set_property", "pause", True]
        elif action == "resume":
            command = ["set_property", "pause", False]
        elif action == "toggle":
            command = ["cycle", "pause"]
        elif action == "stop":
            command = ["quit"]

        record, problem = our_player()
        if record is None:
            return {"ok": False, "error": problem}
        endpoint = record["ipc"]

        if action == "status":
            out = {"ok": True, "action": "status"}
            for key, prop in (("title", "media-title"), ("position", "time-pos"), ("duration", "duration"), ("paused", "pause")):
                try:
                    out[key] = send(endpoint, ["get_property", prop], timeout)
                except MpvError as exc:
                    if "property" in str(exc) or "unavailable" in str(exc):
                        out[key] = None                       # nothing loaded yet: that one value is just unknown
                    else:
                        raise
            out["title"] = str(out["title"])[:120] if out.get("title") is not None else None
            out["position"], out["duration"] = _seconds(out.get("position")), _seconds(out.get("duration"))
            return out
        send(endpoint, command, timeout)
        done = {"pause": "paused", "resume": "resumed", "toggle": "toggled pause", "stop": "stopped (mpv was closed)",
                "seek": "skipped %+g seconds" % (command[1] if action == "seek" else 0),
                "volume": "volume set to %s" % (command[2] if action == "volume" else "")}[action]
        return {"ok": True, "action": action, "done": done}
    except MpvError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool never raises
        return {"ok": False, "error": "play_control failed: %s" % type(exc).__name__}
