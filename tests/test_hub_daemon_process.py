"""Download Hub, phase 1 acceptance: a REAL daemon process, killed mid-file, resumes the file.

Starts the daemon the way Menu > Daemons does (`python jarvis/actions/_hub_daemon.py`, its
HOME pointing at a throwaway folder), queues a slow 3 MB download from a local server, waits for
the .part file to grow, then:

  scenario "kill"   SIGKILL the process (a crash / power cut: nothing gets to clean up)
  scenario "term"   SIGTERM it (a normal stop; POSIX only: on Windows terminate() is a hard kill)

and starts it again.  Asserts what the plan promises: the row is not lost, the .part survives,
the restarted daemon asks the server for `Range: bytes=<what it had>-` instead of starting over,
the finished file is byte-identical, the row says done / 100 %, and the second start refuses
to run next to a live first one.  Also checks the log file exists.

Slow by nature (a few seconds per scenario).  Run: python tests/test_hub_daemon_process.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-proc-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_testserver as srv  # noqa: E402
from jarvis.hub import doctor, paths, service, states  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

RUNNER = ROOT / "jarvis-cli" / "jarvis" / "actions" / "_hub_daemon.py"
SIZE = 3_000_000

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def wait_for(cond, timeout=30.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001 - the database may be mid-write
            pass
        time.sleep(step)
    return False


def start_daemon():
    env = dict(os.environ, HOME=HOME, USERPROFILE=HOME, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.Popen([sys.executable, str(RUNNER)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def part_files(lib):
    return list(lib.rglob("*.part")) if lib.exists() else []


def scenario(kind, server_state, base):
    lib = Path(HOME) / ("lib-" + kind)
    store = Store()
    store.set_settings({"library_root": str(lib), "allow_private_hosts": ["127.0.0.1"]})
    procs = []
    try:
        d1 = start_daemon()
        procs.append(d1)
        check("[%s] the daemon starts and writes its heartbeat" % kind, wait_for(lambda: store.daemon_info()["alive"], 20))
        res = service.add_items(["%s/slow.bin?size=%d&delay=0.05&k=%s" % (base, SIZE, kind)], store=store)
        item_id = res["added"][0]["id"]
        check("[%s] the link is queued" % kind, res["ok"])
        check("[%s] the daemon picks it up and the .part grows" % kind,
              wait_for(lambda: any(p.stat().st_size > 400_000 for p in part_files(lib)), 30))
        check("[%s] the row shows it downloading with a live percent" % kind,
              store.get_item(item_id)["state"] == "downloading")

        if kind == "kill":
            d1.kill()
        else:
            d1.terminate()
        d1.wait(30)
        parts = part_files(lib)
        check("[%s] the .part survived the stop and the file is not finished" % kind, len(parts) == 1 and 0 < parts[0].stat().st_size < SIZE)
        offset = parts[0].stat().st_size
        row = store.get_item(item_id)
        if kind == "kill":
            check("[kill] the row still says running (a crash cleans nothing up)", row["state"] in states.ACTIVE_STATES)
            check("[kill] its heartbeat goes stale on its own (nobody was left to clear it)", wait_for(lambda: not store.daemon_info()["alive"], 25))
            report = doctor.run(store)
            check("[kill] hub_doctor reports the dead daemon as a failure", not report["ok"] and any(c["name"] == "daemon" and c["status"] == "fail" for c in report["checks"]))
        else:
            check("[term] a normal stop hands the item back to the queue", row["state"] == "queued" and "restart" in (row["message"] or ""))
            check("[term] and marks the heartbeat dead", not store.daemon_info()["alive"])

        server_state.reset()
        d2 = start_daemon()
        procs.append(d2)
        check("[%s] the restarted daemon writes a fresh heartbeat" % kind, wait_for(lambda: store.daemon_info()["alive"], 20))
        check("[%s] and finishes the download" % kind, wait_for(lambda: store.get_item(item_id)["state"] == "done", 60))
        row = store.get_item(item_id)
        final = Path(row["dest_file"])
        check("[%s] the file is byte-identical to the server's" % kind, final.exists() and final.read_bytes() == srv.payload(SIZE))
        check("[%s] the .part is gone" % kind, part_files(lib) == [])
        ranges = [r[1] for r in server_state.log if r[0] == "/slow.bin"]
        check("[%s] it resumed with Range: bytes=%d- (it did not start over)" % (kind, offset), ranges and ranges[0] == "bytes=%d-" % offset)
        check("[%s] and asked for nothing it already had" % kind, len(ranges) == 1)
        check("[%s] the row finished at 100 percent after two attempts" % kind, row["percent"] == 100.0 and row["attempts"] == 2 and row["bytes_total"] == SIZE)
        check("[%s] its event trail tells the story" % kind, any("queued" in e["message"] and "restart" in e["message"] for e in store.list_events(item_id))
              or any("attempt 2" in e["message"] for e in store.list_events(item_id)))

        if kind == "kill":
            d3 = start_daemon()
            procs.append(d3)
            time.sleep(0.5)
            check("[kill] a second daemon refuses to run beside a live one", d3.wait(20) == 3)
            check("[kill] and the first one is unaffected", d2.poll() is None and store.daemon_info()["alive"])
            check("[kill] the daemon logged to ~/.jarvis/hub/hub.log", paths.log_path().exists() and "download hub daemon starting" in paths.log_path().read_text(encoding="utf-8", errors="replace"))
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                try:
                    p.wait(10)
                except Exception:  # noqa: BLE001
                    pass


server, base, state = srv.start()
try:
    scenario("kill", state, base)
    if os.name != "nt":
        scenario("term", state, base)
    else:
        print("skipped [term]: terminate() is a hard kill on Windows")
finally:
    srv.stop(server)
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
