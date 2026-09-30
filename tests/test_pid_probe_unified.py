"""Regression tests for the H.1.6 follow-up: the same broken liveness probe
in sched_daemon, channels/discord_gateway and doctor.

H.1.6 fixed daemons.pid_alive() (Windows: `os.kill(pid, 0)` is not an
existence check there, because signal 0 is CTRL_C_EVENT). It recorded, but did
not fix, an identical private `_pid_alive` in three other modules. On Windows
those read every live pid as dead, so: `jarvis sched-daemon --status` said
"not running", sched_daemon.stop_running() never signalled a live scheduler,
discord_gateway._claim_pid_file() let a second gateway start on the same token
(every message answered twice), and `jarvis doctor` flagged a live daemon as a
stale pid file.

Fix: each module's `_pid_alive` now delegates to daemons.pid_alive(). These
tests pin that down. The Windows branch is driven through a fake kernel32, as
in test_h16_daemon_pid_alive.py, so no real Windows run is needed to prove the
three modules no longer use os.kill() as a probe there.

Run: python3 tests/test_pid_probe_unified.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_CLI = str(Path(__file__).resolve().parent.parent / "jarvis-cli")
sys.path.insert(0, _CLI)

# daemons.py keeps its registry under Path.home()/".jarvis"; isolate it.
_HOME = tempfile.mkdtemp(prefix="jarvis_pidprobe_")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_HOME"] = _HOME

from jarvis import daemons, doctor, sched_daemon  # noqa: E402
from jarvis.channels import discord_gateway  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


MODULES = [("sched_daemon", sched_daemon), ("discord_gateway", discord_gateway),
           ("doctor", doctor)]


class FakeKernel32:
    def __init__(self, procs, denied=()):
        self.procs, self.denied = dict(procs), set(denied)
        self.last_error = 0

    def OpenProcess(self, access, inherit, pid):
        if pid in self.denied:
            self.last_error = 5
            return 0
        if pid not in self.procs:
            self.last_error = 87
            return 0
        return 1000 + pid

    def GetExitCodeProcess(self, handle, ptr):
        ptr.contents.value = self.procs[handle - 1000]
        return 1

    def CloseHandle(self, handle):
        return 1


# --- 1. one implementation: each module delegates to daemons.pid_alive ------

for name, mod in MODULES:
    seen = []
    saved = daemons.pid_alive
    daemons.pid_alive = lambda pid, _s=seen: (_s.append(pid), "sentinel")[1]
    try:
        out = mod._pid_alive(4242)
    finally:
        daemons.pid_alive = saved
    check(f"{name}._pid_alive delegates to daemons.pid_alive",
          seen == [4242] and out == "sentinel", f"seen={seen} out={out!r}")

# --- 2. Windows: a live pid is alive, and os.kill is never used to probe ----

kill_calls = []
saved = (daemons._on_windows, daemons._win_kernel32, daemons._win_last_error,
         os.kill)
fake = FakeKernel32({111: 259, 222: 1}, denied={444})
daemons._on_windows = lambda: True
daemons._win_kernel32 = lambda: fake
daemons._win_last_error = lambda: fake.last_error


def _no_kill(pid, sig):
    kill_calls.append((pid, sig))
    raise OSError("os.kill must not be used as a liveness probe on Windows")


os.kill = _no_kill
try:
    for name, mod in MODULES:
        check(f"windows: {name} sees a running pid as alive", mod._pid_alive(111) is True)
        check(f"windows: {name} sees an exited pid as dead", mod._pid_alive(222) is False)
        check(f"windows: {name} sees an unknown pid as dead", mod._pid_alive(555) is False)
        check(f"windows: {name} treats access-denied as alive", mod._pid_alive(444) is True)
    check("windows: os.kill was never called by any of the three", kill_calls == [],
          str(kill_calls))

    # The consumers, not just the helper: a live daemon must be seen as live.
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_pidprobe_files_"))
    sched_daemon.PID_FILE = tmp / "sched_daemon.pid"
    sched_daemon.PID_FILE.write_text('{"pid": 111, "started": "x"}', encoding="utf-8")
    st = sched_daemon.status()
    check("windows: sched_daemon.status() reports a live scheduler as running",
          st.get("running") is True and st.get("pid") == 111, str(st))

    discord_gateway.PID_FILE = tmp / "discord_daemon.pid"
    discord_gateway.PID_FILE.write_text("111", encoding="utf-8")
    ok, why = discord_gateway._claim_pid_file()
    check("windows: a second discord gateway is refused while one is live",
          ok is False and "111" in why, f"ok={ok} why={why!r}")
    check("windows: the live gateway's pid file was left alone",
          discord_gateway.PID_FILE.read_text(encoding="utf-8") == "111")

    pf = tmp / "x.pid"
    pf.write_text("111", encoding="utf-8")
    check("windows: doctor._pid_alive_from() sees a live pid file as alive",
          doctor._pid_alive_from(pf) is True)
finally:
    (daemons._on_windows, daemons._win_kernel32, daemons._win_last_error,
     os.kill) = saved

# --- 3. the host OS, with a real process -----------------------------------

proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
try:
    for name, mod in MODULES:
        check(f"host: {name} sees a real live process as alive",
              mod._pid_alive(proc.pid) is True)
        check(f"host: {name} sees this process as alive", mod._pid_alive(os.getpid()) is True)
finally:
    proc.kill()
    proc.wait()
for name, mod in MODULES:
    check(f"host: {name} sees a reaped process as dead", mod._pid_alive(proc.pid) is False)
    check(f"host: {name} rejects junk pids",
          mod._pid_alive(None) is False and mod._pid_alive(0) is False
          and mod._pid_alive(-1) is False and mod._pid_alive("x") is False)

# --- 4. no private os.kill(pid, 0) probe is left in these modules ----------

import ast  # noqa: E402


def _signal0_kills(path):
    """Calls of the form os.kill(<anything>, 0) in real code (not docstrings)."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "kill" and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value == 0):
            hits.append(node.lineno)
    return hits


for name, mod in MODULES:
    check(f"{name}: no os.kill(<pid>, 0) probe left in the code",
          _signal0_kills(mod.__file__) == [], str(_signal0_kills(mod.__file__)))

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
