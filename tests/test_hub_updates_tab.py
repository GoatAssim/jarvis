"""Download Hub round 5 (H2): the Updates TAB is gone from Menu > Downloads; Updates live in Menu > Updates.

This file used to pin the Updates tab (its pane, its pure helpers, its CSS, its three routes) together with the hub_admin ops
behind it.  The tab no longer exists, so it now pins the NEW state of the Downloads panel instead:
  * tool.html: three tabs (Downloads, Sources, History), no Updates pane, and one small link where the fourth tab was;
  * tool.js: none of the tab's code is left (no loader, no poll timer, no renderer, no tab-only helper), the link opens the
    new panel, the arrow-key handler only reacts to a real tab, the Sources tab leaves out the providers that only report and
    apply updates (`updates_only`), and the panel's only remaining read of the updates answer is that one;
  * tool.css: none of the tab's rules are left, the link has its own;
  * the new panel is the only owner of the six update routes' writes.
The hub_admin ops behind the Updates answer (updates, updates_check, updates_dismiss and the round-5 ones) are tested by the hub
engine's own files (tests/test_hub_updates_gates.py and its neighbours), not here.
No network, no daemon, no browser, no jarvis import.   Run: python tests/test_hub_updates_tab.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-upd-tab-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "jarvis-cli" / "jarvis" / "actions" / "hub_ui"
PASS, FAIL = [], []


def check(name, ok, extra=None):
    (PASS if ok else FAIL).append(name)
    if not ok:
        print("FAIL: %s%s" % (name, "  -> %r" % (extra,) if extra is not None else ""))


html, js, css = (UI / "tool.html").read_text(encoding="utf-8"), (UI / "tool.js").read_text(encoding="utf-8"), (UI / "tool.css").read_text(encoding="utf-8")

# --- tool.html -----------------------------------------------------------------------------------
check("three tabs, each with a pane: Downloads, Sources, History",
      len(re.findall(r'role="tab"', html)) == 3 and all(('data-tab="%s"' % t) in html for t in ("downloads", "sources", "history")) and 'data-tab="updates"' not in html)
check("the Updates pane and every id it had are gone", not re.search(r'updatesPane|tabUpdates|updCount|updList|updSummary|updCheck', html))
check("a link sits in the tab strip where the fourth tab was, between Sources and History",
      re.search(r'id="tabSources".*id="updatesLink".*id="tabHistory"', html, re.S) is not None and "Updates are now in Menu &gt; Updates" in html)
check("the link is a button (keyboard and screen reader friendly), not a tab", re.search(r'<button type="button" class="tab-link" id="updatesLink"', html) is not None)

# --- tool.js: nothing of the tab is left --------------------------------------------------------------
gone = ["loadUpdates", "scheduleUpdates", "updatesPollDelay", "renderUpdates", "updatesSignature", "updateGroups", "updateHealthText", "updateSummaryText",
        "updateVersionText", "updateSizeText", "updateMetaParts", "updateBadge", "updateCheckNotice", "UPDATES_NOTE", "setUpdChecking", "ignoreUpdate",
        "S.updTimer", "S.updFastUntil", "S.updBusy", "S.updAsking", "S.updLoaded", "#updatesPane", "#updList", "#updSummary", "#updCheck", "#updCount", "upd__"]
left = [n for n in gone if n in js]
check("none of the Updates tab's code, state or helper names is left in tool.js", not left, left)
check("the tab names are three everywhere (setTab and the arrow keys)", js.count('["downloads", "sources", "history"]') >= 2 and '"updates"' not in js.replace('"/api/hub/updates"', ""))
check("the arrow keys only react to a real tab, so the link beside the tabs is not part of the tablist's keyboard model",
      "e.target.closest('[role=\"tab\"]'" in js)
check("the link opens the new panel through window.JarvisUpdates, and says where Updates are when that is missing",
      re.search(r'\$\("#updatesLink"\)\.addEventListener\("click"', js) is not None and "window.JarvisUpdates" in js and "Menu > Updates" in js)
check("the Sources tab leaves out the providers that only report and apply updates",
      "visibleSourceProviders(r.providers, only)" in js and "p.updates_only !== true" in js and "visibleSourceProviders," in js)
# DH-A (updates inside the Downloads list) changed this on purpose: the panel now also reads the answer for its update rows and names the dismiss
# route for its Hide button.  The Update button goes through the Updates page's own flow (dry run, apply), so those routes are still not named here,
# and nothing in this file asks for a check.  The new behaviour is pinned in tests/verify_hub_updates_rows.js.
check("the reads of the updates answer are the Sources tab's list of those providers and the Downloads list's update rows; the only route written here is dismiss (no check, apply, dry-run or set)",
      js.count('"/api/hub/updates"') == 2 and js.count('"/api/hub/updates/dismiss"') == 1 and not re.search(r'/api/hub/updates/(?!dismiss")', js))
check("that read is kept for a minute, and a failed read keeps what there was", "60000" in js and "keep what we had" in js)
check("the panel still sends its writes with the hub header and closes cleanly without the Updates timer",
      '"X-Jarvis-Hub": "1"' in js and "S.updTimer" not in js and "clearTimeout(histTimer);" in js)

# --- tool.css ----------------------------------------------------------------------------------------
check("none of the Updates tab's rules are left in tool.css", not re.search(r"\.upd__|\.upd-|#updatesPane|tabUpdates", css))
check("the link has its own rules, with a visible focus", ".tab-link" in css and ".tab-link:focus-visible" in css)

# --- the new panel owns the update writes --------------------------------------------------------------
panel = (ROOT / "web" / "public" / "updates-panel.js").read_text(encoding="utf-8")
check("the new panel is the one place that names the check, dismiss, apply, dry-run and set routes",
      all(('"/api/hub/updates/%s"' % r) in panel for r in ("check", "dismiss", "apply", "dry-run", "set")))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
