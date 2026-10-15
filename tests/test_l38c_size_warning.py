"""L.38 / D-6 -- the raw event log size warning.

The event logs (~/.jarvis/events) are unbounded by design and never pruned. Owner
decision 2026-10-15: add a size warning, no auto-prune. `raw_archive.size_warning()`
says when all the logs together pass RAW_ARCHIVE_WARN_MB (default 500, 0 = off) and
`jarvis doctor` shows it in the conversations group.

Run: python3 tests/test_l38c_size_warning.py   (no key, no network)
"""

import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l38c-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import doctor, raw_archive, tunables  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def put(conv_id, nbytes):
    raw_archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    (raw_archive.ARCHIVE_DIR / f"{conv_id}.jsonl").write_bytes(b"x" * nbytes)


def doctor_check():
    return {c.check_id: c for c in doctor.check_conversations()}.get("conv.events")


MB = 1024 * 1024

# --- registry -----------------------------------------------------------------
spec = tunables.spec("RAW_ARCHIVE_WARN_MB")
check("RAW_ARCHIVE_WARN_MB is a registered tunable, default 500 MB, 0 allowed",
      bool(spec) and spec["default"] == 500 and spec["min"] == 0)
check("the module default equals the registry default", raw_archive.WARN_MB_DEFAULT == 500)

# --- nothing on disk ----------------------------------------------------------
check("no events folder: total_size is zeros, no warning",
      raw_archive.total_size() == {"files": 0, "bytes": 0, "largest": None}
      and raw_archive.size_warning() is None)
check("no event logs: doctor shows no event-log row", doctor_check() is None)

# --- under the limit ----------------------------------------------------------
put("a" * 8, 2000)
put("b" * 8, 3000)
info = raw_archive.total_size()
check("total_size sums every log and names the largest",
      info["files"] == 2 and info["bytes"] == 5000
      and info["largest"] == {"conv_id": "b" * 8, "bytes": 3000}, info)
check("under the default 500 MB there is no warning", raw_archive.size_warning() is None)
c = doctor_check()
check("doctor reports the size as OK under the limit", c is not None and c.status == doctor.OK, c and c.detail)

# --- over a lowered limit -----------------------------------------------------
ok, _ = tunables.set_override("RAW_ARCHIVE_WARN_MB", 1)
check("the limit can be lowered from Settings (tunable accepts 1)", ok)
put("c" * 8, MB + 500)
warn = raw_archive.size_warning()
check("over the limit: size_warning reports bytes, limit, files, largest",
      bool(warn) and warn["limit_bytes"] == MB and warn["files"] == 3
      and warn["bytes"] == 5000 + MB + 500 and warn["largest"]["conv_id"] == "c" * 8, warn)
c = doctor_check()
check("doctor shows a WARN row", c is not None and c.status == doctor.WARN, c and c.status)
check("the row names the largest log and says nothing is deleted automatically",
      c is not None and ("c" * 8) in c.detail and "Nothing is deleted automatically" in c.fix)
check("the fix points at the setting", c is not None and "RAW_ARCHIVE_WARN_MB" in c.fix)
check("the warning never removed or changed a log",
      (raw_archive.ARCHIVE_DIR / ("c" * 8 + ".jsonl")).stat().st_size == MB + 500)

# --- off ---------------------------------------------------------------------
ok, _ = tunables.set_override("RAW_ARCHIVE_WARN_MB", 0)
check("0 is accepted", ok)
check("0 turns the warning off", raw_archive.size_warning() is None and raw_archive.warn_limit_bytes() == 0)
c = doctor_check()
check("with the warning off, doctor goes back to the OK size row",
      c is not None and c.status == doctor.OK)

# --- freeing space clears it --------------------------------------------------
tunables.set_override("RAW_ARCHIVE_WARN_MB", 1)
check("warning is back at 1 MB", raw_archive.size_warning() is not None)
raw_archive.delete("c" * 8)
check("deleting the big conversation's log clears the warning", raw_archive.size_warning() is None)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
