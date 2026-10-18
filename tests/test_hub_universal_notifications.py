"""Download Hub -> the ONE notification system (E5 follow-up).

Two things used to be separate:
  * the hub daemon's messages (started / finished / failed ...): already `notifier.notify()`, but under
    a `kind="download"` that was not a registered kind, and the whole hub was skipped by the E5 lock;
  * the hub PANEL's one-line answers to a click ("Demo downloads started", "Scheduled", "That didn't
    work"): drawn by the generic `host.toast` -> `JarvisUI.toast`, so they never reached the inbox and
    ignored the owner's notification settings.

Now both are `notifier.notify()` records of kind `download`.  This test pins the pieces:

  1. the `download` kind is registered (label, Settings row, standard level);
  2. `jarvis notify-send` takes --kind / --source / --failed (checked, so a typo is an error), and a
     real run files an inbox record with that kind and source and only the channels asked for;
  3. the web server has ONE endpoint for a page to raise a notification, which can only ask for the
     in-page channels (no desktop / voice / phone), a registered kind, a short source and a level 1-5,
     and which never lets a message be read as a CLI flag;
  4. `JarvisNotifications.send` and `host.notify` (menu panels AND sandboxed frames) lead there, with a
     plain toast only as the fallback when the notifier can't be reached;
  5. the hub panel says nothing any other way.

Run: python3 tests/test_hub_universal_notifications.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI_DIR))

HOME = tempfile.mkdtemp(prefix="jarvis_hubnotif_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")

from jarvis import cli, notify_registry as reg  # noqa: E402  (after HOME is redirected)

WEB = ROOT / "web" / "public"
HUB_UI = CLI_DIR / "jarvis" / "actions" / "hub_ui"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def _run_cli(*args):
    env = dict(os.environ, HOME=HOME, USERPROFILE=HOME, JARVIS_HOME=os.environ["JARVIS_HOME"],
               PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(CLI_DIR), env=env,
                          capture_output=True, text=True, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"_raw": proc.stdout, "_err": proc.stderr[-400:]}


# --- 1. the kind -----------------------------------------------------------------------------------------

def test_the_download_kind_is_registered():
    row = next((k for k in reg.KINDS if k["id"] == "download"), None)
    check("`download` is a row of notify_registry.KINDS", row is not None)
    check("it is called Downloads, is standard level and has a Settings row",
          bool(row) and row["label"] == "Downloads" and row["level"] == 2 and row["settings"] is True, row)
    check("the registry is still consistent", reg.problems(
        __import__("jarvis.notifier", fromlist=["_DELIVERERS"])._DELIVERERS.keys()) == [], reg.problems())


# --- 2. notify-send ----------------------------------------------------------------------------------------

def test_notify_send_extras_parse():
    f = cli.parse_notify_send_extras
    rest, kind, source, failed, err = f(["Demo downloads started", "Download Hub", "stream",
                                         "--kind", "download", "--source=download_hub", "--failed", "--level", "3"])
    check("kind, source and failed are peeled off",
          (kind, source, failed, err) == ("download", "download_hub", True, None), (kind, source, failed, err))
    check("everything else is left for the old parser, in order",
          rest == ["Demo downloads started", "Download Hub", "stream", "--level", "3"], rest)
    words, position, level, bad = cli.parse_notify_send_args(rest)
    check("and the old parser still reads it the way it always did",
          (words, position, level, bad) == (["Demo downloads started", "Download Hub", "stream"], None, "3", None))
    check("no extras: nothing changes", f(["hi", "T"]) == (["hi", "T"], None, None, False, None))
    check("an unknown kind is an error that lists the real ones",
          "download" in (f(["hi", "--kind", "nope"])[4] or "") and f(["hi", "--kind", "nope"])[0] is None)
    check("--kind with no value is an error", bool(f(["hi", "--kind"])[4]))
    check("a source with odd characters is an error", bool(f(["hi", "--source", "a b!"])[4]))
    check("a registered kind other than download works too", f(["hi", "--kind", "task"])[1] == "task")


def test_notify_send_files_a_real_record():
    code, rec = _run_cli("notify-send", "Demo downloads started", "Download Hub", "stream",
                         "--kind", "download", "--source", "download_hub")
    check("notify-send exits cleanly", code == 0, rec)
    check("the record has the download kind and source",
          rec.get("kind") == "download" and rec.get("source") == "download_hub", rec)
    check("the title and message are as sent",
          rec.get("title") == "Download Hub" and rec.get("message") == "Demo downloads started")
    check("only the inbox and the in-page toast delivered it (no desktop notification, voice or phone)",
          rec.get("delivered") == ["inbox", "stream"] and rec.get("failed_channels") == [], rec.get("delivered"))
    check("it is not marked failed", rec.get("failed") is False)

    code, bad = _run_cli("notify-send", "x", "--failed", "--kind", "download", "--level", "2")
    check("--failed marks the record failed", bad.get("failed") is True and bad.get("level") == 2, bad)

    code, history = _run_cli("notify-history", "10")
    items = history.get("notifications", []) if isinstance(history, dict) else []
    mine = [n for n in items if n.get("kind") == "download"]
    check("both are in the durable inbox (what the Notifications panel lists)", len(mine) >= 2, history)

    code, err = _run_cli("notify-send", "x", "--kind", "not_a_kind")
    check("a bad kind fails and files nothing", code != 0 and "error" in err, err)


def test_a_message_that_looks_like_a_flag_is_just_text():
    code, rec = _run_cli("notify-send", " --kind task", "T", "stream", "--kind", "download")
    check("a message that starts like a flag stays the message",
          rec.get("kind") == "download" and rec.get("message") == "--kind task", rec)


# --- 3. the server endpoint ---------------------------------------------------------------------------------

def test_the_server_endpoint():
    src = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
    start = src.find('app.post("/api/notifications/send"')
    check("server.js has POST /api/notifications/send", start > 0)
    block = src[start:start + 2400]
    check("it runs notify-send through the notifier (no delivery of its own)",
          '"notify-send"' in block and "runJarvisOnce(" in block)
    check("only the in-page channels can be asked for",
          'NOTIFY_SEND_CHANNELS = new Set(["inbox", "stream"])' in src and "NOTIFY_SEND_CHANNELS.has(c)" in block)
    check("kind and source are checked before they become arguments",
          "a-z0-9_]{0,40}$/.test(body.kind)" in block and "A-Za-z0-9_.:-]{1,100}$/.test(body.source)" in block)
    check("the level is a whole number 1-5", "level >= 1 && level <= 5" in block)
    check("the page is pushed the new record at once (the same push every notification takes)",
          "pushNotifications()" in block)
    check("an empty message is refused", "needs a message" in block)
    check("route is behind requireJarvis", "requireJarvis" in src[start:start + 80])
    # The text cleaner, run for real when node is around.
    node = shutil.which("node")
    if node:
        js = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
        i, j = js.find("function cleanNoticeText"), js.find('app.post("/api/notifications/send"')
        probe = ("const f=new Function(%s+';return cleanNoticeText;')();"
                 "console.log(JSON.stringify([f('a\\u0000b',50),f('--level 5',50),f(null,50),f('x'.repeat(300),10)]));"
                 % json.dumps(js[i:j]))
        out = subprocess.run([node, "-e", probe], capture_output=True, text=True, timeout=30)
        got = json.loads(out.stdout.strip() or "[]")
        check("cleanNoticeText drops control characters, defuses a leading --, and caps the length",
              got == ["a b", " --level 5", "", "xxxxxxxxxx"], got or out.stderr[-300:])
    else:
        print("skipped  cleanNoticeText run (node not installed)")


# --- 4. the page side ---------------------------------------------------------------------------------------

def test_the_page_side_leads_to_the_notifier():
    notif = (WEB / "notifications.js").read_text(encoding="utf-8")
    check("JarvisNotifications.send posts to /api/notifications/send",
          'send: "/api/notifications/send"' in notif and "async function send(" in notif
          and 'api("POST", API.send, body)' in notif)
    check("send defaults to the in-page channels and draws nothing itself",
          'body.channels = Array.isArray(x.channels) && x.channels.length ? x.channels : ["stream"]' in notif)
    check("send is exported", re.search(r"markRead, send, _pure", notif) is not None)

    tui = (WEB / "tool-ui.js").read_text(encoding="utf-8")
    check("menu panels get host.notify", "notify: (m, l, o) => notify(item, m, l, o)," in tui)
    check("sandboxed frames get host.notify too (same host in both modes)",
          "notify:function(m,l,o)" in tui and 'else if (m.op === "notify") notify(item, m.message, m.level, m.opts);' in tui)
    check("host.notify sends through JarvisNotifications.send", "N.send({" in tui)
    check("a problem line is marked failed and never quieter than standard", "failed: bad, level: bad ? 2 : undefined" in tui)
    check("the plain toast is only the fallback when the notifier can't be reached", "if (!sent) toast(message, lvl);" in tui)
    check("the documented host API lists it", "host.notify(message, level, o)" in tui)


# --- 5. the hub panel ---------------------------------------------------------------------------------------

def test_every_hub_panel_message_is_a_notification():
    js = (HUB_UI / "tool.js").read_text(encoding="utf-8")
    check("hub tool.js never calls host.toast", "host.toast(" not in js)
    check("its one helper calls host.notify with kind download and source download_hub",
          'host.notify(String(msg), level || "info", { kind: "download", source: "download_hub"' in js)
    calls = re.findall(r"(?<![\w.])toast\(", js)
    check("it still says everything it said before (the helper is called from many places)", len(calls) >= 20, len(calls))
    check("the 'Demo downloads started' line is one of them", '"Demo downloads started"' in js)


if __name__ == "__main__":
    for name in [n for n in sorted(globals()) if n.startswith("test_")]:
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAIL.append((name, repr(exc)))
            print(f"FAILED  {name}: {exc!r}")
            traceback.print_exc()
    shutil.rmtree(HOME, ignore_errors=True)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
