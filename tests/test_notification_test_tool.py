"""L.30 WP-F: the notification_test tool (actions/notification_test.py) and its Menu panel.

Covers:
  * it goes through notifier.notify() only, with source "notification_test" and a registered kind;
  * the preview comes from notifier.resolve_delivery() and matches the record a real send produces;
  * every list it offers is read from the registry (describe), nothing is typed in the tool;
  * it never takes a recipient, and refuses the types that message the owner's phone (the `dm` rows);
  * validation: level range, unknown type / kind / position, count 1-5;
  * the tool is reachable: TOOL_UI points at an existing folder, TOOL_KEYWORDS clear the router's minimum
    score, TEST_CHECKLIST is present, and the module imports nothing circular at module level;
  * the panel keeps no list of its own and writes strings with textContent only.

No network, no live model. Delivery is stubbed so nothing is shown or sent.

Run: python3 tests/test_notification_test_tool.py
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

HOME = tempfile.mkdtemp(prefix="jarvis_nt_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")

from jarvis import notify_registry as reg  # noqa: E402
from jarvis import notifier  # noqa: E402
from jarvis import tool_router  # noqa: E402
from jarvis.actions import notification_test as nt  # noqa: E402

notifier.JARVIS_DIR = Path(HOME) / ".jarvis"
notifier.INBOX_FILE = notifier.JARVIS_DIR / "notifications.json"
notifier.CONFIG_FILE = notifier.JARVIS_DIR / "notify_config.json"

PASS, FAIL = [], []
SRC = ROOT / "jarvis-cli" / "jarvis" / "actions" / "notification_test.py"
UI = ROOT / "jarvis-cli" / "jarvis" / "actions" / "notification_test_ui"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


class _stubbed:
    """Replace every deliverer except the inbox with a recorder, so nothing is drawn or sent."""

    def __enter__(self):
        self.saved = dict(notifier._DELIVERERS)
        self.calls = []
        for name in list(notifier._DELIVERERS):
            if name != "inbox":
                notifier._DELIVERERS[name] = (lambda n: lambda record, config: self.calls.append(n) or True)(name)
        for f in (notifier.CONFIG_FILE, notifier.INBOX_FILE):
            try:
                f.unlink()
            except OSError:
                pass
        return self

    def __exit__(self, *exc):
        notifier._DELIVERERS.clear()
        notifier._DELIVERERS.update(self.saved)


# --- the tool goes through the real notifier ---------------------------------------------------------

def test_it_goes_through_notify_only():
    seen = []
    real = notifier.notify

    def spy(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    notifier.notify = spy
    try:
        with _stubbed():
            r = nt.tool_notification_test({"level": 2, "types": ["small_toast"]})
    finally:
        notifier.notify = real
    check("ok and one result", r.get("ok") is True and r.get("sent") == 1, r)
    check("notify() was called once, with source notification_test and a registered kind",
          len(seen) == 1 and seen[0].get("source") == "notification_test" and reg.kind(seen[0].get("kind")) is not None, seen)
    check("the explicit types went as the channels, like a scheduled job's", seen and seen[0].get("channels") == ["small_toast"], seen)
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "notify"]
    check("the module has exactly one notify() call and no delivery of its own", len(calls) == 1
          and "wire.media" not in src and "_deliver" not in src.replace("_deliverers", ""), len(calls))


def test_the_preview_matches_the_record():
    cases = [
        {"level": 2, "types": ["small_toast"]},
        {"level": 2, "types": ["small_toast"], "failed": True},
        {"level": 2, "types": ["small_toast"], "persistent": True},
        {"level": 1},
        {"level": 3},
        {"types": ["desktop"]},
        {"level": 2, "types": ["stream", "small_toast"]},
    ]
    for args in cases:
        with _stubbed():
            pre = nt.tool_notification_test(dict(args, preview=True))
            sent = nt.tool_notification_test(dict(args))
        rec = (sent.get("results") or [{}])[0]
        pv = pre.get("preview") or {}
        check(f"preview == record for {args}: look, level and channels",
              pre.get("ok") and sent.get("ok") and rec.get("look") == pv.get("look")
              and rec.get("level") == pv.get("level")
              and (rec.get("deferred_to_digest") or rec.get("delivered") == pv.get("channels")),
              (pv, rec))
        check(f"forced_card / muted flags are reported for {args}",
              sent.get("forced_card") == pv.get("forced_card") and sent.get("muted") == pv.get("muted"), sent)
    with _stubbed():
        pre = nt.tool_notification_test({"preview": True})
    check("a preview sends nothing", not os.path.exists(str(notifier.INBOX_FILE)) or notifier.INBOX_FILE.read_text(encoding="utf-8").strip() in ("", "[]"))


def test_a_failed_small_toast_comes_back_as_a_card():
    with _stubbed():
        r = nt.tool_notification_test({"types": ["small_toast"], "failed": True})
    res = r["results"][0]
    check("look is card, forced_card is true", res["look"] == "card" and r["forced_card"] is True, r)
    check("stream carried it, not small_toast", "stream" in res["delivered"] and "small_toast" not in res["delivered"], res)


def test_level_one_is_muted():
    with _stubbed():
        r = nt.tool_notification_test({"level": 1, "types": ["small_toast"]})
    check("muted and look none", r["muted"] is True and r["results"][0]["look"] == "none", r)


def test_burst_and_default_message():
    with _stubbed():
        r = nt.tool_notification_test({"level": 2, "count": 3})
    check("three results", r["sent"] == 3 and len(r["results"]) == 3 and len({x["id"] for x in r["results"]}) == 3, r)
    check("count above the maximum is refused", nt.tool_notification_test({"count": nt.MAX_COUNT + 1}).get("ok") is False)
    check("a non-number count is refused", nt.tool_notification_test({"count": "many"}).get("ok") is False)
    pre = nt.tool_notification_test({"preview": True, "level": 2, "types": ["small_toast"]})
    check("the default title says what was requested", "level 2" in pre["preview"]["default_title"]
          and "small toast" in pre["preview"]["default_title"].lower(), pre)


# --- it never reaches a person ---------------------------------------------------------------------------

def test_no_recipient_and_dm_types_are_refused():
    schema = nt.TOOL_SCHEMAS[0]["parameters"]["properties"]
    check("no recipient-like parameter anywhere in the schema",
          not any(re.search(r"recipient|to_user|person|contact|channel_id|chat_id|dm", k, re.I) for k in schema), list(schema))
    for dm in reg.dm_channel_ids():
        with _stubbed() as st:
            r = nt.tool_notification_test({"types": [dm]})
        check(f"{dm} (a dm row) is refused with a pointer to channels-test",
              r.get("ok") is False and "channels-test" in r.get("error", "") and not st.calls, r)
    for level in range(1, reg.MAX_LEVEL + 1):
        with _stubbed() as st:
            r = nt.tool_notification_test({"level": level})
        wanted_dm = [c for c in reg.channels_for_level(level) if c in reg.dm_channel_ids()]
        check(f"level {level}: no phone channel is ever delivered through, and the preview says which were left out",
              r.get("ok") is True and not [c for c in st.calls if c in reg.dm_channel_ids()]
              and not [c for c in r["results"][0]["delivered"] if c in reg.dm_channel_ids()]
              and r["preview"]["skipped_dm"] == wanted_dm, (r, st.calls))
    with _stubbed():
        r = nt.tool_notification_test({"level": 5})
    check("a skipped phone channel does not skip the level's flags (level 5 is still persistent + confirm)",
          r["results"] and r["preview"]["flags"] == {"persistent": True, "confirm_required": True}, r)


def test_validation():
    check("an unknown type is refused and lists the types", "small_toast" in (nt.tool_notification_test({"types": ["nope"]}).get("error") or ""))
    check("an unknown kind is refused", nt.tool_notification_test({"kind": "nope"}).get("ok") is False)
    check("an unknown position is refused", nt.tool_notification_test({"position": "middle"}).get("ok") is False)
    check("a level above the maximum is refused", nt.tool_notification_test({"level": reg.MAX_LEVEL + 1}).get("ok") is False)
    check("level 0 is refused", nt.tool_notification_test({"level": 0}).get("ok") is False)
    check("a non-number level is refused", nt.tool_notification_test({"level": "loud"}).get("ok") is False)
    with _stubbed():
        ok = nt.tool_notification_test({"types": ["toast"], "preview": True})
    check("the legacy name `toast` is still accepted for the desktop notification", ok.get("ok") is True and "desktop" in ok["preview"]["channels"], ok)


def test_dismiss_removes_the_test_rows():
    with _stubbed():
        r = nt.tool_notification_test({"level": 2})
        rid = r["results"][0]["id"]
        d = nt.tool_notification_test({"dismiss": [rid]})
    check("dismiss deletes the record from the inbox", d.get("ok") is True and d.get("dismissed") == 1, d)


# --- everything it offers comes from the registry --------------------------------------------------------

def test_describe_is_the_registry_catalog():
    r = nt.tool_notification_test({"describe": True})
    check("describe returns the registry catalog, same version", r.get("ok") and r["catalog"]["version"] == reg.catalog()["version"], r.get("catalog", {}).get("version"))
    saved = reg.PRESETS
    extra = dict(saved[-1], level=len(saved) + 1, id="panic", name="Panic", adds=())
    try:
        reg.PRESETS = saved + (extra,)
        r2 = nt.tool_notification_test({"describe": True})
        check("a level added to the registry appears in describe with no edit to the tool",
              [p["level"] for p in r2["catalog"]["presets"]][-1] == len(saved) + 1)
        with _stubbed():
            ok = nt.tool_notification_test({"preview": True, "level": len(saved) + 1})
        check("and is accepted as a level", ok.get("ok") is True, ok)
    finally:
        reg.PRESETS = saved


# --- reachable ------------------------------------------------------------------------------------------

def test_the_tool_is_reachable():
    check("TOOL_GROUP, TOOLS and TOOL_SCHEMAS agree",
          nt.TOOL_GROUP == "notifications" and set(nt.TOOLS) == {s["name"] for s in nt.TOOL_SCHEMAS} == {"notification_test"})
    weights = nt.TOOL_KEYWORDS["notification_test"]
    check("the new group has keywords that clear the router's minimum score",
          max(weights.values()) >= tool_router.MIN_SCORE and sum(1 for w in weights.values() if w >= tool_router.MIN_SCORE) >= 3, weights)
    check("a TEST_CHECKLIST entry exists for it with run and ask steps",
          "notification_test" in nt.TEST_CHECKLIST
          and any("run" in s for s in nt.TEST_CHECKLIST["notification_test"]["steps"])
          and any("ask" in s for s in nt.TEST_CHECKLIST["notification_test"]["steps"]))
    steps = nt.TEST_CHECKLIST["notification_test"]["steps"]
    check("the checklist covers a small toast, a failed run, a persistent run and a burst of three",
          any(s.get("run", {}).get("types") == ["small_toast"] and not s["run"].get("failed") and not s["run"].get("persistent") for s in steps)
          and any(s.get("run", {}).get("failed") for s in steps) and any(s.get("run", {}).get("persistent") for s in steps)
          and any(s.get("run", {}).get("count") == 3 for s in steps))
    ui = nt.TOOL_UI[0]
    check("TOOL_UI is a menu panel pointing at a folder with tool.html, tool.js and tool.css",
          ui["mode"] == "menu" and ui["tool"] == "notification_test"
          and all((UI / n).is_file() for n in ("tool.html", "tool.js", "tool.css")), ui)
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    top = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            top.append((node.module or "") + "|" + ",".join(a.name for a in node.names))
        elif isinstance(node, ast.Import):
            top.append(",".join(a.name for a in node.names))
    check("no module-level import of ai_client, tool_router or tool_registry (circular: the tool would vanish)",
          not any(re.search(r"ai_client|tool_router|tool_registry", t) for t in top), top)
    import jarvis.tools as T  # noqa: E402
    check("the tool is discovered by the loader", "notification_test" in T._tool_index())


# --- the panel ------------------------------------------------------------------------------------------

def test_the_panel_keeps_no_lists_and_writes_text_only():
    js = (UI / "tool.js").read_text(encoding="utf-8")
    html = (UI / "tool.html").read_text(encoding="utf-8")
    check("the panel never assigns HTML", not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function", js))
    check("it reads its lists from the tool's describe answer", 'describe: true' in js and "S.catalog" in js)
    ids = [a["id"] for a in reg.ACTIONS]
    quoted = [i for i in ids if re.search(r"""["'`]%s["'`]""" % re.escape(i), js)]
    check("no notification type id is typed in the panel script", not quoted, quoted)
    check("no level label is typed in the panel script", not re.search(r"Silent|Standard|Broadcast", js))
    used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', js))
    check("every element the script looks up exists in tool.html", all(f'id="{i}"' in html for i in used), [i for i in used if f'id="{i}"' not in html])
    check("it redraws only when the catalog version changed", "S.version === r.catalog.version" in js)
    check("it re-checks the catalog when the window regains focus", '"focus"' in js)
    check("the phone-messaging types are drawn disabled", "disabled: a.dm" in js)


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
