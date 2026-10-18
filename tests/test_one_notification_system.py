"""E5 -- the lock on "one notification system".

Static only: reads source files, runs nothing, needs no browser.

The rule (AGENTS.md): every message shown to the person goes through
`notifier.notify()` and the one toast renderer. This test fails when someone adds ANOTHER way
to tell the person something:

  * a second place that calls the browser's `new Notification(...)` or asks for its permission;
  * a second toast host element (`jui-toasts`, `ntf-stack`) or a revival of the old `#toast`;
  * a new file that defines its own `toast()` helper, or a helper that does not hand off to
    `JarvisUI.toast` / the host;
  * a new native `confirm()` / `alert()` / `prompt()`;
  * another use of the run-confirm popup element;
  * a native OS notification (PowerShell toast, osascript, notify-send) outside notifier.py;
  * a new direct DM to the owner (`outbound.dm_owner / notify_owner / dm_person`);
  * a `kind=` passed to `notifier.notify()` that is not in notify_registry.KINDS.

It is a RATCHET, not a wish: the numbers below are what the code has TODAY, including the debt
that E3 / E4b will remove. A count that goes UP fails ("you added one"). A count that goes DOWN
also fails, with the new number to write here, so the baseline tightens as the debt is paid and
nobody can quietly put it back. When an E3 or E4b patch removes one of these, lower the number
in the same patch.

Does not touch the Download Hub (jarvis/hub/, actions/download_hub.py, actions/hub_ui/): those
files are skipped, not scanned.

Run: python3 tests/test_one_notification_system.py
"""

import ast
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

HOME = tempfile.mkdtemp(prefix="jarvis_e5_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")

from jarvis import notify_registry as reg  # noqa: E402  (after HOME is redirected)

WEB = ROOT / "web" / "public"
PKG = ROOT / "jarvis-cli" / "jarvis"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


# ---------------------------------------------------------------------------
# What the code has today (the baseline). Lower a number when you remove one.
# ---------------------------------------------------------------------------

# Web: pattern -> {file: count}. `vendor/` and test-checklist-data.js are not scanned.
JS_COUNTS = {
    # The ONE browser-notification call. app.js used to have its own (E3 removed it).
    "browser Notification object": (
        r"new\s+(?:window\.|global\.)?Notification\s*\(",
        {"notifications.js": 1}),
    "browser Notification permission prompt": (
        r"Notification\.requestPermission",
        {"notifications.js": 1}),
    # E3 (done): the old single #toast element is gone; app.js toast() hands off to
    # JarvisUI.toast. This stays at zero so nobody draws it again.
    "the old #toast element": (
        r"qs\(\"#toast\"\)|getElementById\(\"toast\"\)|querySelector\(\"#toast\"\)",
        {}),
    # C10 debt: native confirm() on deleting a command / skill / log / conversation, and the
    # settings.js fallback used when JarvisUI is missing. ui-kit.js defines JarvisUI.confirm.
    "native confirm / alert / prompt": (
        r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\(",
        {"app.js": 4, "settings.js": 1}),
}
JS_COUNT_EXEMPT = {"native confirm / alert / prompt": {"ui-kit.js"}}

# Web: a token that may appear in these files ONLY.
JS_FILES_ONLY = {
    "the JarvisUI toast host (jui-toasts)": ("jui-toasts", {"ui-kit.js"}),
    "the notifier toast stack (ntf-stack)": ("ntf-stack", {"notifications.js"}),
    "the run-confirm popup (E3 debt)": ("run-confirm-popup", {"app.js"}),
}

# Web: the files that define a `toast()` helper. Every one except the two exempt ones must hand
# off to JarvisUI.toast / the host (a `.toast(` within 400 characters of the definition).
LOCAL_TOASTS = {
    "app.js", "backlog.js", "calendar.js", "channels-panel.js", "daemons.js", "logsearch.js",
    "mcp-servers.js", "schedules.js", "settings.js", "slash-palette.js", "test-checklist.js",
    "tool-manager.js", "tool-ui.js", "ui-kit.js",
}
LOCAL_TOAST_EXEMPT = {
    "ui-kit.js": "this IS the JarvisUI host",
}
TOAST_DEF = re.compile(r"(?m)^[ \t]*(?:function\s+toast\s*\(|const\s+toast\s*=)")

# Python (jarvis-cli/jarvis, hub excluded).
OS_TOAST = re.compile(r"BurntToast|ToastNotification|display notification|NotifyIcon|\[\"notify-send\"")
OS_TOAST_FILES = {"notifier.py"}

# Direct DMs to a person: file -> calls. E4b debt except the notifier's own transport and
# send_dm (a different thing: the owner telling Jarvis to message a contact). Lower a number
# when a caller moves onto notifier.record_owner_dm() / notifier.notify().
DM_CALLS = {
    "actions/notify_owner.py": ["notify_owner"],      # records via notifier.record_owner_dm (E4)
    "actions/send_dm.py": ["dm_person"],              # owner-approved DM to a contact, not a notice
    "ambient.py": ["notify_owner"],                   # paired with a notifier.notify() just above
    "channels/awaiting.py": ["notify_owner"],         # E4b: urgent reply DM has no inbox record
    "channels/base.py": ["notify_owner", "notify_owner"],   # E4b: new-sender ping, rule notice
    "channels/instagram_private.py": ["notify_owner"],      # E4b: default notifier hook
    "channels_cli.py": ["notify_owner"],              # `jarvis channels-test`: a diagnostic
    "digest.py": ["notify_owner"],                    # the digest summary of records already in the inbox
    "notifier.py": ["dm_owner"],                      # level 4 transport
}

# A `kind=` string passed to notifier.notify() that is not a registered kind yet (E4b).
KNOWN_UNREGISTERED_KINDS = {"reply_digest"}


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _js_files():
    for p in sorted(WEB.glob("*.js")):
        if p.name == "test-checklist-data.js":
            continue
        yield p.name, p.read_text(encoding="utf-8")


def _skip_hub(rel):
    return (rel.startswith("hub/") or rel == "hub_client.py"
            or rel.startswith("actions/download_hub") or rel.startswith("actions/hub_ui"))


def _py_files():
    for p in sorted(PKG.rglob("*.py")):
        rel = p.relative_to(PKG).as_posix()
        if not _skip_hub(rel):
            yield rel, p.read_text(encoding="utf-8")


def count_by_file(pattern, files, exempt=()):
    rx = re.compile(pattern)
    out = {}
    for name, text in files:
        if name in exempt:
            continue
        n = len(rx.findall(text))
        if n:
            out[name] = n
    return out


def compare_counts(observed, expected):
    """Sentences for every difference between what is there and the baseline. [] = same."""
    problems = []
    for name in sorted(set(observed) | set(expected)):
        have, want = observed.get(name, 0), expected.get(name, 0)
        if have > want:
            problems.append(f"{name}: {have} found, baseline is {want} -- a new one was added. "
                            f"Use the existing path (see AGENTS.md, notifications).")
        elif have < want:
            problems.append(f"{name}: {have} found, baseline is {want} -- one was removed, "
                            f"lower the baseline in this test to {have}.")
    return problems


def compare_files(observed_files, allowed):
    extra = sorted(set(observed_files) - set(allowed))
    return [f"{name}: not one of the files allowed to use this" for name in extra]


# ---------------------------------------------------------------------------
# The scanner itself (a rule that cannot see anything would pass forever)
# ---------------------------------------------------------------------------

def test_the_scanner_notices_a_new_one():
    check("equal counts: no problem", compare_counts({"a.js": 1}, {"a.js": 1}) == [])
    check("a count that went up is a problem",
          len(compare_counts({"a.js": 2}, {"a.js": 1})) == 1)
    check("a new file is a problem", len(compare_counts({"a.js": 1, "b.js": 1}, {"a.js": 1})) == 1)
    check("a count that went down is a problem too (lower the baseline)",
          "lower the baseline" in compare_counts({}, {"a.js": 1})[0])
    check("a file outside the allowed set is flagged",
          len(compare_files({"x.js"}, {"ui-kit.js"})) == 1 and compare_files({"ui-kit.js"}, {"ui-kit.js"}) == [])
    fake = [("new-panel.js", "const n = new Notification('hi');\nconst ok = confirm('sure?');")]
    check("the Notification pattern sees a new Notification()",
          count_by_file(JS_COUNTS["browser Notification object"][0], fake) == {"new-panel.js": 1})
    check("the confirm pattern sees confirm() and window.confirm(), not UI().confirm(",
          count_by_file(JS_COUNTS["native confirm / alert / prompt"][0],
                        [("a.js", "confirm('x'); window.confirm('y'); await UI().confirm({}); doconfirm(1);")])
          == {"a.js": 2})
    check("the toast-definition pattern sees both spellings",
          len(TOAST_DEF.findall("  function toast(m) {}\n  const toast = (m) => 1;\n  toast('x')")) == 2)
    check("the OS-toast pattern sees BurntToast and notify-send",
          len(OS_TOAST.findall('Import-Module BurntToast\nsubprocess.run(["notify-send", t])')) == 2)


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------

def test_web_counts_match_the_baseline():
    files = list(_js_files())
    check("web/public has scripts to scan", len(files) > 20, len(files))
    for label, (pattern, expected) in JS_COUNTS.items():
        observed = count_by_file(pattern, files, JS_COUNT_EXEMPT.get(label, ()))
        problems = compare_counts(observed, expected)
        check(f"web: {label}", not problems, "; ".join(problems))


def test_web_tokens_stay_in_their_own_file():
    files = list(_js_files())
    for label, (token, allowed) in JS_FILES_ONLY.items():
        found = {name for name, text in files if token in text}
        check(f"web: {label} only in {sorted(allowed)}", not compare_files(found, allowed)
              and found == allowed, f"found in {sorted(found)}")


def test_every_local_toast_helper_hands_off():
    files = dict(_js_files())
    defining = {name for name, text in files.items() if TOAST_DEF.search(text)}
    check("web: the files that define a toast() helper are the known ones",
          defining == LOCAL_TOASTS,
          f"new: {sorted(defining - LOCAL_TOASTS)}, gone: {sorted(LOCAL_TOASTS - defining)} -- "
          "call JarvisUI.toast directly instead of defining another helper")
    for name in sorted(defining & LOCAL_TOASTS):
        if name in LOCAL_TOAST_EXEMPT:
            continue
        text = files[name]
        ok = all(".toast(" in text[m.start():m.start() + 400] for m in TOAST_DEF.finditer(text))
        check(f"web: {name}'s toast() hands off to JarvisUI / the host", ok)


def test_the_old_toast_element_is_gone():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    check("index.html has no id=\"toast\" element (E3 removed it)",
          len(re.findall(r'id="toast"', html)) == 0)
    css = (WEB / "style.css").read_text(encoding="utf-8")
    check("style.css has no `.toast{` rule for it either", not re.search(r"(?m)^\.toast\s*\{", css))
    check("index.html has exactly one run-confirm popup element (E3 debt)",
          len(re.findall(r'id="run-confirm-popup"', html)) == 1)


def test_app_js_no_longer_calls_the_browser_api():
    src = (WEB / "app.js").read_text(encoding="utf-8")
    check("app.js has no `Notification.` call of its own",
          not re.search(r"(?<![\w.])Notification\.(?:permission|requestPermission)", src)
          and "new Notification" not in src)
    check("app.js hands the away-notice to JarvisNotifications.osNotice",
          "JarvisNotifications" in src and ".osNotice(" in src and ".requestPermission()" in src)


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

def test_native_os_toast_only_in_the_notifier():
    files = list(_py_files())
    found = set(count_by_file(OS_TOAST.pattern, files))
    check("python: the native OS toast is raised only from notifier.py",
          found == OS_TOAST_FILES, f"found in {sorted(found)}")


def _ast_calls(text, rel):
    try:
        return ast.parse(text)
    except SyntaxError as exc:
        raise AssertionError(f"{rel} does not parse: {exc}")


def test_direct_owner_dms_match_the_baseline():
    seen = {}
    for rel, text in _py_files():
        for node in ast.walk(_ast_calls(text, rel)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "outbound"
                    and node.func.attr in ("dm_owner", "notify_owner", "dm_person")):
                seen.setdefault(rel, []).append(node.func.attr)
    observed = {rel: len(calls) for rel, calls in seen.items()}
    expected = {rel: len(calls) for rel, calls in DM_CALLS.items()}
    problems = compare_counts(observed, expected)
    check("python: direct DMs to the owner / a person are the known ones",
          not problems, "; ".join(problems))
    kinds = {rel: sorted(calls) for rel, calls in seen.items()}
    check("python: each known file makes the same kind of call",
          all(kinds.get(rel) == sorted(calls) for rel, calls in DM_CALLS.items()), kinds)


def test_the_notify_owner_tool_records_in_the_notifier():
    src = (PKG / "actions" / "notify_owner.py").read_text(encoding="utf-8")
    check("actions/notify_owner.py calls notifier.record_owner_dm (E4)",
          "record_owner_dm" in src and "from .. import notifier" in src)
    check("and it still takes no recipient (AGENTS.md invariant)",
          '"recipient"' not in src and '"to"' not in src and '"user_id"' not in src)


def test_every_notify_kind_is_registered():
    registered = {k["id"] for k in reg.KINDS}
    used = {}
    for rel, text in _py_files():
        for node in ast.walk(_ast_calls(text, rel)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "notifier"
                    and node.func.attr == "notify"):
                for kw in node.keywords:
                    if kw.arg == "kind" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str):
                        used.setdefault(kw.value.value, set()).add(rel)
    unregistered = set(used) - registered
    check("python: every kind passed to notifier.notify() is in notify_registry.KINDS "
          "(or a known E4b leftover)", unregistered <= KNOWN_UNREGISTERED_KINDS,
          f"add a row to KINDS for: {sorted(unregistered - KNOWN_UNREGISTERED_KINDS)}")
    check("python: the known-unregistered list has no stale entry",
          not (KNOWN_UNREGISTERED_KINDS & registered)
          and KNOWN_UNREGISTERED_KINDS <= set(used),
          f"registered now or unused: {sorted(KNOWN_UNREGISTERED_KINDS - set(used)) or sorted(KNOWN_UNREGISTERED_KINDS & registered)}")


if __name__ == "__main__":
    for name in [n for n in sorted(globals()) if n.startswith("test_")]:
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAIL.append((name, repr(exc)))
            print(f"FAILED  {name}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
