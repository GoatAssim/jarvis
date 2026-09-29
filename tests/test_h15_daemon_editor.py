"""Regression tests for H.1.5 (Daemons full rework, 2026-09-28)'s two small
backend additions, both needed by the new Edit form in daemons.js:

  1. `daemon-edit --description D` — daemons.edit() already accepted
     "description" (it's in _EDITABLE and not in _BUILTIN_LOCKED), but
     workspace_cli.py's daemon-edit handler never read a --description flag,
     so there was no way to set it after a daemon was registered. The web
     PATCH route now forwards it too (web/server.js).

  2. `daemon-edit --env K=V --env-replace` — daemon-edit's existing --env
     always MERGED onto the stored environment (add or overwrite one key),
     which can never remove one. The Edit form always submits the full,
     current set of variables (it has to: a row the user deleted has to
     actually disappear), so it needs a real replace. --env-replace makes
     the given --env pairs (zero or more) the WHOLE environment. Zero
     --env flags plus --env-replace is a real case, not a no-op: it means
     "now there are no variables at all," which a plain merge could never
     express either. See workspace_cli.py's own comment on this, and
     web/server.js's PATCH /api/daemons/:id (`envReplace`).

Run: python3 tests/test_h15_daemon_editor.py
"""

import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp(prefix="jarvis_h15_"))

from jarvis import daemons, workspace_cli  # noqa: E402
import io  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _fresh_daemon(suffix, **kw):
    did = f"h15test_{suffix}"
    if daemons.get(did):
        daemons.remove(did)
    daemons.add(did, "echo hi", name=f"H15 Test {suffix}", **kw)
    return daemons.normalize_id(did)


def run_cli(argv):
    """workspace_cli.handle() prints JSON and may sys.exit(1) on failure —
    both _emit() and _fail() do — so this captures stdout and the exit
    instead of letting a "failed" edit kill the test process."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            workspace_cli.handle(argv)
        code = 0
    except SystemExit as exc:
        code = exc.code or 0
    return code, json.loads(buf.getvalue())


# --- description ------------------------------------------------------------

did = _fresh_daemon("desc")
code, out = run_cli(["daemon-edit", did, "--description", "a test service"])
check("daemon-edit --description exits ok", code == 0, str(out))
check("description is stored", daemons.get(did).get("description") == "a test service")

code, out = run_cli(["daemon-edit", did, "--description", ""])
check("an empty --description clears it", daemons.get(did).get("description") == "")

# A built-in's description is user-editable (not in _BUILTIN_LOCKED) — this
# is the one non-argv/shell/stdin field the editor lets you change on a
# built-in, so it has to actually persist through the CLI too.
code, out = run_cli(["daemon-edit", "scheduler", "--description", "custom note"])
check("a built-in's description can be edited", code == 0, str(out))
check("the built-in's description is stored",
      daemons.get("scheduler").get("description") == "custom note")
run_cli(["daemon-edit", "scheduler", "--description",
         daemons.BUILTINS["scheduler"]["description"]])  # restore

# --- env: default merge, unchanged ------------------------------------------

did = _fresh_daemon("merge", env={"A": "1", "B": "2"})
code, out = run_cli(["daemon-edit", did, "--env", "B=9", "--env", "C=3"])
check("plain --env exits ok", code == 0, str(out))
env = daemons.get(did).get("env") or {}
check("plain --env merges: A survives untouched", env.get("A") == "1", str(env))
check("plain --env merges: B is overwritten", env.get("B") == "9", str(env))
check("plain --env merges: C is added", env.get("C") == "3", str(env))

# --- env: --env-replace ------------------------------------------------------

did = _fresh_daemon("replace", env={"A": "1", "B": "2", "KEEP": "yes"})
code, out = run_cli(["daemon-edit", did, "--env", "KEEP=yes", "--env", "NEW=1", "--env-replace"])
check("--env-replace exits ok", code == 0, str(out))
env = daemons.get(did).get("env") or {}
check("--env-replace drops a key missing from the new set", "A" not in env, str(env))
check("--env-replace drops a second missing key", "B" not in env, str(env))
check("--env-replace keeps a key that was resubmitted", env.get("KEEP") == "yes", str(env))
check("--env-replace adds a new key", env.get("NEW") == "1", str(env))
check("--env-replace touches nothing else", set(env) == {"KEEP", "NEW"}, str(env))

# --- env: --env-replace with NO --env at all clears everything -------------
# The edge case the merge path can't express: "there are now zero variables."
# A naive `if "env" in flags` guard would silently no-op here since no --env
# flag is present at all — see workspace_cli.py's own comment on this.

did = _fresh_daemon("clear", env={"A": "1", "B": "2"})
code, out = run_cli(["daemon-edit", did, "--env-replace"])
check("--env-replace alone (no --env) exits ok", code == 0, str(out))
env = daemons.get(did).get("env")
check("--env-replace alone clears every variable", env == {}, repr(env))

# --- env: default merge with NO --env is a true no-op (nothing to change) --
# Confirms the fix above didn't turn "no env flags at all" into a spurious
# edit when --env-replace isn't requested — the pre-existing daemon-add/edit
# fields still need at least one flag or the command correctly refuses.

did = _fresh_daemon("noop")
code, out = run_cli(["daemon-edit", did])
check("daemon-edit with no flags at all is refused", code != 0)
check("...and reports nothing to change", "nothing to change" in str(out.get("error", "")))

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed across this file")
if FAIL:
    sys.exit(1)
