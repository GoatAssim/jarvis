"""Regression tests for H.1.6 — owner-reported, 2026-09-29: the Daemons panel
never showed any daemon as running; every one read "crashed" or "stopped",
including daemons that were alive.

Root cause (backend, confirmed by reading daemons.py; tests below reproduce
the mechanism on any host): `pid_alive()` was `os.kill(pid, 0)` on every
platform. On POSIX that is the standard existence probe. On Windows it is
not — signal 0 is `signal.CTRL_C_EVENT`, so CPython turns it into
GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid), which targets a console process
*group*. For an ordinary pid it raises OSError, which the old code read as
"no such process". Every pid therefore looked dead, so status() reported a
STARTING daemon as crashed (supervisor "gone") and a RUNNING one as stopped
(child and supervisor both "gone"), on every poll.

Fix: on Windows, probe with OpenProcess + GetExitCodeProcess and never send
a signal. POSIX behaviour is unchanged.

These tests cannot run real Win32 calls on a non-Windows host, so the
Windows branch is driven through a fake kernel32; what they pin down is
(a) os.kill is never used for the probe on Windows, (b) the exit-code /
last-error logic, and (c) that status() reports running/starting correctly
once the probe is right. A live process on the host OS is also started for
real. Validation on an actual Windows machine is still owed — see the
master plan's H.1.6.

Run: python3 tests/test_h16_daemon_pid_alive.py
"""

import ctypes
import os
import sys
import tempfile
import time
from pathlib import Path

_CLI = str(Path(__file__).resolve().parent.parent / "jarvis-cli")
sys.path.insert(0, _CLI)
# The supervisor is spawned as `python -m jarvis daemon-run <id>`; make that
# importable from the child even when the package isn't installed.
os.environ["PYTHONPATH"] = _CLI + os.pathsep + os.environ.get("PYTHONPATH", "")

# daemons.py keeps its registry under Path.home()/".jarvis" and ignores
# JARVIS_HOME, so isolating this test means giving it its own home directory —
# before the import below, and inherited by the spawned supervisor. Without
# this it would share ~/.jarvis with every other daemon test and race them
# when the runner uses --jobs.
_HOME = tempfile.mkdtemp(prefix="jarvis_h16_")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_HOME"] = _HOME

from jarvis import daemons  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


class FakeKernel32:
    """Stands in for kernel32. `procs` maps pid -> exit code (259 = running)."""

    def __init__(self, procs, denied=()):
        self.procs = dict(procs)
        self.denied = set(denied)
        self.closed = []
        self.last_error = 0
        self.open_calls = []

    def OpenProcess(self, access, inherit, pid):
        self.open_calls.append(pid)
        if pid in self.denied:
            self.last_error = 5
            return 0
        if pid not in self.procs:
            self.last_error = 87    # ERROR_INVALID_PARAMETER: no such pid
            return 0
        return 1000 + pid

    def GetExitCodeProcess(self, handle, ptr):
        pid = handle - 1000
        ptr.contents.value = self.procs[pid]
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


class WindowsMode:
    """Force the Windows branch of daemons.pid_alive() on any host."""

    def __init__(self, fake, kill_forbidden=True):
        self.fake = fake
        self.kill_forbidden = kill_forbidden
        self.kill_calls = []

    def __enter__(self):
        self._saved = (daemons._on_windows, daemons._win_kernel32,
                       daemons._win_last_error, daemons.os.kill)
        daemons._on_windows = lambda: True
        daemons._win_kernel32 = lambda: self.fake
        daemons._win_last_error = lambda: self.fake.last_error

        def _kill(pid, sig):
            self.kill_calls.append((pid, sig))
            raise AssertionError("os.kill must never be used to probe a pid on Windows")
        if self.kill_forbidden:
            daemons.os.kill = _kill
        return self

    def __exit__(self, *exc):
        (daemons._on_windows, daemons._win_kernel32,
         daemons._win_last_error, daemons.os.kill) = self._saved


# --- pid_alive: the Windows branch ------------------------------------------

fake = FakeKernel32({111: 259, 222: 1, 333: 0}, denied={444})
with WindowsMode(fake) as wm:
    check("windows: a running process (STILL_ACTIVE) is alive", daemons.pid_alive(111) is True)
    check("windows: an exited process is NOT alive even though its handle still opens",
          daemons.pid_alive(222) is False and daemons.pid_alive(333) is False)
    check("windows: an unknown pid is not alive", daemons.pid_alive(555) is False)
    check("windows: access-denied means it exists", daemons.pid_alive(444) is True)
    check("windows: os.kill was never called", wm.kill_calls == [])
    check("windows: every opened handle was closed", sorted(fake.closed) == [1111, 1222, 1333],
          str(fake.closed))
    check("windows: junk pids are rejected before probing",
          daemons.pid_alive(None) is False and daemons.pid_alive("x") is False
          and daemons.pid_alive(0) is False and daemons.pid_alive(-4) is False)


class ExplodingKernel32(FakeKernel32):
    def OpenProcess(self, *a):
        raise OSError("kernel32 unavailable")


with WindowsMode(ExplodingKernel32({})):
    check("windows: a probe that cannot run assumes alive (never a false 'dead')",
          daemons.pid_alive(4242) is True)


class UnreadableKernel32(FakeKernel32):
    def GetExitCodeProcess(self, handle, ptr):
        return 0


with WindowsMode(UnreadableKernel32({7: 259})):
    check("windows: opened but unreadable exit code is treated as alive",
          daemons.pid_alive(7) is True)

# --- pid_alive: POSIX behaviour unchanged -----------------------------------

if os.name != "nt":
    check("posix: own pid is alive", daemons.pid_alive(os.getpid()) is True)
    check("posix: a pid that cannot exist is not alive", daemons.pid_alive(2 ** 22 + 12345) is False)
    check("posix: string pids are accepted", daemons.pid_alive(str(os.getpid())) is True)

# --- status(): the reported symptom, end to end through the Windows branch ---


def _fresh(suffix):
    did = f"h16test_{suffix}"
    if daemons.get(did):
        daemons.remove(did)
    daemons.add(did, "echo hi", name=f"H16 {suffix}")
    return daemons.normalize_id(did)


did = _fresh("running")
daemons._write_status(did, status=daemons.STATUS_RUNNING, child_pid=111,
                      supervisor_pid=222, started_at=time.time(), stop_requested=False)
with WindowsMode(FakeKernel32({111: 259, 222: 259})):
    s = daemons.status(did)
check("status(): a live child reads RUNNING, not stopped/crashed (the reported bug)",
      s["status"] == daemons.STATUS_RUNNING and s["running"] is True and s["pid"] == 111,
      str(s))

did = _fresh("starting")
daemons._write_status(did, status=daemons.STATUS_STARTING, supervisor_pid=222,
                      child_pid=None, stop_requested=False)
with WindowsMode(FakeKernel32({222: 259})):
    s = daemons.status(did)
check("status(): a STARTING daemon with a live supervisor is not called crashed",
      s["status"] == daemons.STATUS_STARTING, str(s))

did = _fresh("gone")
daemons._write_status(did, status=daemons.STATUS_RUNNING, child_pid=111,
                      supervisor_pid=222, stop_requested=False)
with WindowsMode(FakeKernel32({})):
    s = daemons.status(did)
check("status(): child and supervisor genuinely gone still reconciles to stopped (H.1.2 kept)",
      s["status"] == daemons.STATUS_STOPPED and s["running"] is False, str(s))

did = _fresh("crash")
daemons._write_status(did, status=daemons.STATUS_RUNNING, child_pid=111,
                      supervisor_pid=222, stop_requested=False)
with WindowsMode(FakeKernel32({222: 259})):
    s = daemons.status(did)
check("status(): supervisor alive but child genuinely gone is still an immediate crash (H.1.1 kept)",
      s["status"] == daemons.STATUS_CRASHED, str(s))

# --- a real process on this host, through the real supervisor ---------------

if os.name != "nt":
    did = _fresh("live")
    daemons.edit(did, argv=f'{sys.executable} -u -c "import time;print(1,flush=True);time.sleep(30)"')
    ok, msg = daemons.start(did)
    check("live: start() accepted", ok, msg)
    seen = []
    deadline = time.time() + 12
    while time.time() < deadline:
        seen.append(daemons.status(did)["status"])
        if seen[-1] == daemons.STATUS_RUNNING:
            break
        time.sleep(0.3)
    check("live: a really-running daemon reports RUNNING", seen and seen[-1] == daemons.STATUS_RUNNING,
          str(seen))
    check("live: it never reported crashed while starting up", daemons.STATUS_CRASHED not in seen,
          str(seen))
    # still running a moment later (no self-inflicted stop from the status polling)
    time.sleep(1.0)
    check("live: still RUNNING after repeated status polls", daemons.status(did)["running"] is True)
    ok, msg = daemons.stop(did)
    check("live: stop() works", ok, msg)
    time.sleep(0.5)
    check("live: reads stopped afterwards", daemons.status(did)["status"] == daemons.STATUS_STOPPED)

for did in list(daemons._load_registry()):
    if did.startswith("h16test_"):
        daemons.remove(did)

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
sys.exit(1 if FAIL else 0)
