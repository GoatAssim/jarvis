"""Brief 22: control of the mpv Jarvis started (hub/stremio/mpv.py and the play_control tool).

EVERYTHING HERE RUNS AGAINST A FAKE MPV: a small server on a Unix socket in this file that answers the way mpv's JSON
IPC does ({"error":"success","data":...,"request_id":N}; events carry no request_id).  No real mpv ran.  Linux only: the
Windows named-pipe branch (mpv._exchange_pipe) is written but NOT run, and nothing here ran on Windows or macOS.

Covers: every action; events, a stale reply and a garbled line skipped; a reply that never comes (a timeout, not a hang);
a dead socket; a stale record (the process is gone); a record that is not ours (VLC, another endpoint); bad arguments
(nothing is sent); that only the named commands and the properties `pause` and `volume` are ever sent; one connection per
call; the guest refusal (fails closed); the tool's declarations.

Run: python tests/test_hub_stremio_mpv.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-mpv-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_CHANNEL_SENDER", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import atomic_io, checklist_schema, tool_registry  # noqa: E402
from jarvis.actions import play_media, stremio_stack  # noqa: E402
from jarvis.hub import paths  # noqa: E402
from jarvis.hub.stremio import mpv, player  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


class FakeMpv(object):
    """mode: normal (events, a stale reply and a garbled line before each real reply) | silent | garbage | closing"""

    def __init__(self, path, mode="normal"):
        self.path, self.mode = path, mode
        self.received, self.connections = [], 0
        self.props = {"pause": False, "volume": 100, "time-pos": 12.5, "duration": 3000.0, "media-title": "Sintel"}
        self.no_media = False
        self.quit = False
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self._stop = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(5)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            msg = json.loads(buf.split(b"\n", 1)[0])
            self.received.append(msg["command"])
            if self.mode == "silent":
                time.sleep(8)
                return
            if self.mode == "closing":
                return
            if self.mode == "garbage":
                conn.sendall(b"this is not json\n{broken\n")
                time.sleep(8)
                return
            rid = msg["request_id"]
            cmd = msg["command"]
            out = [{"event": "property-change", "id": 1, "name": "time-pos", "data": 1.0},      # an event: no request_id
                   {"error": "success", "data": "WRONG", "request_id": rid + 1000}]              # someone else's reply
            raw = b"".join(json.dumps(o).encode() + b"\n" for o in out) + b"not json at all\n"
            if cmd[0] == "set_property":
                self.props[cmd[1]] = cmd[2]
                reply = {"error": "success", "request_id": rid}
            elif cmd[0] == "cycle":
                self.props["pause"] = not self.props["pause"]
                reply = {"error": "success", "request_id": rid}
            elif cmd[0] == "seek":
                self.props["time-pos"] += cmd[1]
                reply = {"error": "success", "request_id": rid}
            elif cmd[0] == "quit":
                self.quit = True
                reply = {"error": "success", "request_id": rid}
            elif cmd[0] == "get_property":
                if self.no_media and cmd[1] in ("time-pos", "duration"):
                    reply = {"error": "property unavailable", "request_id": rid}
                else:
                    reply = {"error": "success", "data": self.props.get(cmd[1]), "request_id": rid}
            else:
                reply = {"error": "invalid parameter", "request_id": rid}
            conn.sendall(raw + json.dumps(reply).encode() + b"\n")
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


def write_record(pid=None, kind="mpv", ipc=None):
    paths.hub_dir().mkdir(parents=True, exist_ok=True)
    atomic_io.write_json(player.state_path(), {"pid": os.getpid() if pid is None else pid, "player": kind,
                                               "ipc": player.ipc_endpoint() if ipc is None else ipc, "started_at": time.time(),
                                               "target": "file"})


def clear_record():
    try:
        player.state_path().unlink()
    except OSError:
        pass


def start_fake(mode="normal"):
    paths.hub_dir().mkdir(parents=True, exist_ok=True)
    try:
        os.unlink(player.ipc_endpoint())
    except OSError:
        pass
    return FakeMpv(player.ipc_endpoint(), mode)


def call(**args):
    return play_media.tool_play_control(args)


check("the timeout is 3 seconds", mpv.TIMEOUT_S == 3.0)

# ---- no record / not ours: nothing is sent, one sentence
fake = start_fake()
clear_record()
res = call(action="pause")
check("no record: ok false, one sentence saying nothing is playing that Jarvis started",
      res["ok"] is False and "Nothing is playing that I started" in res["error"] and not fake.received and fake.connections == 0)
write_record(kind="vlc")
res = call(action="pause")
check("a VLC record: only mpv can be controlled, nothing sent", res["ok"] is False and "only mpv" in res["error"] and fake.connections == 0)
write_record(ipc="/tmp/some-other-program.sock")
res = call(action="pause")
check("a record naming another endpoint is refused, nothing connected", res["ok"] is False and "no control endpoint" in res["error"] and fake.connections == 0)
gone = subprocess.Popen([sys.executable, "-c", "pass"])
gone.wait()
write_record(pid=gone.pid)
res = call(action="pause")
check("a stale record (its process is gone): refused, and the socket is never touched",
      res["ok"] is False and "no longer running" in res["error"] and fake.connections == 0 and not fake.received)
write_record(pid=0)
check("a record with pid 0 or junk is not alive", call(action="pause")["ok"] is False and fake.connections == 0)
write_record(pid="abc")
check("...nor a pid that is not a number", call(action="pause")["ok"] is False and fake.connections == 0)

# ---- a record written by player.launch itself (a fake Popen), then every action
clear_record()


class FakeProc(object):
    pid = os.getpid()


launched = player.launch("mpv", "/fake/mpv", str(Path(HOME) / "x.mp4"), popen=lambda argv, **kw: FakeProc())
check("player.launch records the endpoint the control uses", launched["ok"] and launched["ipc"] == player.ipc_endpoint()
      and json.loads(player.state_path().read_text())["ipc"] == player.ipc_endpoint())
fake.received.clear()
fake.connections = 0

res = call(action="pause")
check("pause: ok, sets the property pause to true", res["ok"] and fake.received == [["set_property", "pause", True]] and fake.props["pause"] is True)
res = call(action="resume")
check("resume: sets pause to false", res["ok"] and fake.received[-1] == ["set_property", "pause", False] and fake.props["pause"] is False)
res = call(action="toggle")
check("toggle: cycles pause", res["ok"] and fake.received[-1] == ["cycle", "pause"] and fake.props["pause"] is True)
call(action="resume")
res = call(action="seek", seconds=-30)
check("seek: relative seconds, negative goes back", res["ok"] and fake.received[-1] == ["seek", -30.0, "relative"] and fake.props["time-pos"] == 12.5 - 30)
fake.props["time-pos"] = 12.5
res = call(action="seek", seconds="45")
check("seek accepts a number sent as text", res["ok"] and fake.received[-1] == ["seek", 45.0, "relative"])
res = call(action="volume", volume=40)
check("volume: sets the property volume", res["ok"] and fake.received[-1] == ["set_property", "volume", 40] and fake.props["volume"] == 40)
before = fake.connections
res = call(action="status")
check("status: title, position, duration, paused", res["ok"] and res["title"] == "Sintel" and res["position"] == 57.5
      and res["duration"] == 3000.0 and res["paused"] is False)
check("status made four calls, one connection each", fake.connections - before == 4 and
      [c[1] for c in fake.received[-4:]] == ["media-title", "time-pos", "duration", "pause"])
fake.no_media = True
res = call(action="status")
check("status with nothing loaded: the unknown values are null, not an error", res["ok"] and res["position"] is None and res["duration"] is None and res["title"] == "Sintel")
fake.no_media = False
check("events, a stale reply for another request id and a garbled line were skipped (the answers above are the real ones)",
      call(action="status")["title"] == "Sintel")
res = call(action="stop")
check("stop: quits mpv", res["ok"] and fake.received[-1] == ["quit"] and fake.quit)

# ---- only the named commands, ever
sent = fake.received
check("every command sent was one of the named ones",
      all(c[0] in ("set_property", "cycle", "seek", "quit", "get_property") for c in sent))
check("the only properties ever SET are pause and volume", {c[1] for c in sent if c[0] == "set_property"} <= {"pause", "volume"})
check("the only properties read are the four status ones", {c[1] for c in sent if c[0] == "get_property"} <= {"media-title", "time-pos", "duration", "pause"})
check("no command opens a file or runs anything", not any(c[0] in ("loadfile", "run", "subprocess", "script-message", "load-script", "osd-overlay") for c in sent))
count = len(sent)
res = call(action="pause", command=["quit"], property="input-ipc-server", value="x")
check("a raw command passed by the model is ignored (only 'pause' was sent)", res["ok"] and fake.received[count:] == [["set_property", "pause", True]])
schema = next(s for s in play_media.TOOL_SCHEMAS if s["name"] == "play_control")
check("the schema has no argument that could carry a command, path or URL",
      set(schema["parameters"]["properties"]) == {"action", "seconds", "volume"} and schema["parameters"]["required"] == ["action"])

# ---- bad arguments: refused before anything is sent
count, conns = len(fake.received), fake.connections
for label, args in (("seek with no seconds", dict(action="seek")), ("seek with words", dict(action="seek", seconds="abc")),
                    ("seek by a week", dict(action="seek", seconds=7 * 86400)), ("seek by infinity", dict(action="seek", seconds=float("inf"))),
                    ("seek with a boolean", dict(action="seek", seconds=True)), ("volume with none", dict(action="volume")),
                    ("volume 300", dict(action="volume", volume=300)), ("volume -1", dict(action="volume", volume=-1)),
                    ("volume as a boolean", dict(action="volume", volume=True)), ("an unknown action", dict(action="loadfile")),
                    ("a shell-looking action", dict(action="pause; rm -rf /"))):
    res = call(**args)
    check("%s: ok false with a sentence" % label, res.get("ok") is False and res.get("error"))
check("none of those sent anything or connected", len(fake.received) == count and fake.connections == conns)
check("no action at all asks what to do", call().get("needs_clarification"))

# ---- the guest gate (fails closed)
count = len(fake.received)
os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
res = call(action="pause")
check("a chat guest is refused", res["ok"] is False and "owner" in res["error"])
os.environ["JARVIS_CHANNEL_SENDER"] = "{not json"
check("an unreadable sender value counts as a guest", call(action="pause")["ok"] is False)
os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps(["x"])
check("a sender value that is not an object counts as a guest", call(action="pause")["ok"] is False)
check("nothing was sent for any guest", len(fake.received) == count)
os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})
check("the owner in a chat is let through", call(action="pause")["ok"] is True)
os.environ.pop("JARVIS_CHANNEL_SENDER")
check("no chat context (the PC) is the owner's", call(action="resume")["ok"] is True)
fake.close()

# ---- a dead socket, a socket nobody answers on, a reply that never comes, a garbled line only
write_record()
try:
    os.unlink(player.ipc_endpoint())
except OSError:
    pass
res = call(action="pause")
check("no socket file: one sentence, no hang", res["ok"] is False and "isn't answering" in res["error"])
stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
stale.bind(player.ipc_endpoint())
stale.close()                                            # the file stays, nobody listens: what a crashed mpv leaves
res = call(action="pause")
check("a socket file with nobody behind it (mpv crashed): the same sentence", res["ok"] is False and "isn't answering" in res["error"])
os.unlink(player.ipc_endpoint())

for mode, word in (("silent", "didn't reply"), ("garbage", "didn't reply"), ("closing", "closed the connection")):
    fake = start_fake(mode)
    started = time.monotonic()
    res = mpv.run("pause", timeout=0.8)
    took = time.monotonic() - started
    check("mpv that %s: a plain sentence (%r) in about the timeout, not a hang" % ({"silent": "never replies", "garbage": "sends only garbage",
                                                                               "closing": "closes without a reply"}[mode], word),
          res["ok"] is False and word in res["error"] and took < 2.5)
    fake.close()

fake = start_fake()
fake.props["pause"] = False
started = time.monotonic()
fake.mode = "silent"
res = call(action="pause")
took = time.monotonic() - started
check("the tool's own call times out at about 3 seconds", res["ok"] is False and "didn't reply within 3 seconds" in res["error"] and 2.5 < took < 5.5)
fake.close()

# ---- the declarations
check("play_control is in TOOLS and TOOL_SCHEMAS, group stremio", "play_control" in play_media.TOOLS
      and "play_control" in {s["name"] for s in play_media.TOOL_SCHEMAS} and play_media.TOOL_GROUP == "stremio")
entries, _meta, problems = checklist_schema.extract_supplied(play_media.TEST_CHECKLIST, getattr(play_media, "TEST_CHECKLIST_GROUP", None),
                                                             list(play_media.TOOLS), "stremio")
check("it has a well-formed Test Checklist entry", "play_control" in entries and not problems)
check("it has keywords", play_media.TOOL_KEYWORDS.get("play_control") and "pause the video" in play_media.TOOL_KEYWORDS["play_control"])
text = tool_registry.pack_instruction("stremio")
check("the group's one instruction names play_control and is still one text under the old limit",
      "play_control" in text and text == stremio_stack.TOOL_PACK_INSTRUCTION and len(text) <= 806 + 444 + 404)
check("the Windows pipe branch exists (written, not run)", callable(mpv._exchange_pipe))

shutil.rmtree(HOME, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
