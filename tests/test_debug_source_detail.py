"""L.18 -- Debug panel: filter tools by the module / file / MCP server they load from.

tools_list_payload() (GET /api/tools) now adds `source_detail` to every item,
next to the four-bucket `source` (§3, tests/test_tool_source_filter.py). Covers:
every item carries a well-formed source_detail; its kind agrees with `source`;
labels cannot collide across the four buckets; the per-label counts add up to the
catalogue and to every bucket (the numbers the Debug panel's chips show); a label
never returns another module's tool; a source is NOT a group (one group can span
several sources); the helpers resolve a built-in's module (also through
functools.partial / wraps), an MCP server from the record's own tag (not the name
prefix), a user file's path; and `source` itself is unchanged.

No test framework needed:

    python3 tests/test_debug_source_detail.py
"""

import functools
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import tools as system_tools  # noqa: E402

BUCKETS = ("builtin", "auto", "user", "mcp")
KIND_FOR = {"builtin": "module", "auto": "file", "user": "file", "mcp": "server"}


def _items():
    return system_tools.tools_list_payload()


def test_every_tool_has_a_well_formed_source_detail():
    bad = []
    for item in _items():
        d = item.get("source_detail")
        if not isinstance(d, dict):
            bad.append(f"{item['name']}: no source_detail")
            continue
        if not (isinstance(d.get("label"), str) and d["label"].strip()):
            bad.append(f"{item['name']}: empty label")
        if not isinstance(d.get("module"), str):
            bad.append(f"{item['name']}: module is not a string")
        if d.get("kind") not in ("module", "file", "server"):
            bad.append(f"{item['name']}: kind {d.get('kind')!r}")
    assert not bad, "\n".join(bad)


def test_source_is_unchanged_and_kind_agrees_with_it():
    bad = []
    for item in _items():
        if item.get("source") not in BUCKETS:
            bad.append(f"{item['name']}: source {item.get('source')!r}")
        elif item["source_detail"]["kind"] != KIND_FOR[item["source"]]:
            bad.append(f"{item['name']}: source {item['source']} but kind {item['source_detail']['kind']}")
    assert not bad, "\n".join(bad)


def test_labels_cannot_collide_across_buckets():
    # "tools.py" (built-in) vs "actions/x.py" (shipped) vs "~/.jarvis/tools/x.py"
    # (yours) vs "MCP: x": the label is the filter key, so one label must never
    # belong to two buckets.
    owners = defaultdict(set)
    for item in _items():
        owners[item["source_detail"]["label"]].add(item["source"])
    clash = {label: sorted(b) for label, b in owners.items() if len(b) > 1}
    assert not clash, f"labels shared by more than one source bucket: {clash}"
    for item in _items():
        label, src = item["source_detail"]["label"], item["source"]
        if src == "auto":
            assert label.startswith("actions/"), (item["name"], label)
        elif src == "user":
            assert label.startswith("~/.jarvis/tools/"), (item["name"], label)
        elif src == "mcp":
            assert label.startswith("MCP: "), (item["name"], label)
        else:
            assert label.endswith(".py") and not label.startswith(("actions/", "~", "MCP")), (item["name"], label)


def test_per_label_counts_add_up_to_the_catalogue_and_to_each_bucket():
    items = _items()
    per_label = Counter(i["source_detail"]["label"] for i in items)
    assert sum(per_label.values()) == len(items)
    for bucket in BUCKETS:
        in_bucket = [i for i in items if i["source"] == bucket]
        labels = Counter(i["source_detail"]["label"] for i in in_bucket)
        assert sum(labels.values()) == len(in_bucket), bucket
        # ...and a bucket's labels belong to that bucket alone.
        for label in labels:
            assert per_label[label] == labels[label], (bucket, label)


def test_a_label_returns_only_its_own_tools():
    by_label = defaultdict(set)
    for item in _items():
        by_label[item["source_detail"]["label"]].add(item["name"])
    names = [n for v in by_label.values() for n in v]
    assert len(names) == len(set(names)), "a tool appears under two labels"


def test_a_source_is_not_a_group():
    # The filter must not be a re-skin of the router's TOOL_GROUPS: at least one
    # group spans more than one source.
    groups = defaultdict(set)
    for item in _items():
        groups[item.get("group") or ""].add(item["source_detail"]["label"])
    spanning = [g for g, labels in groups.items() if g and len(labels) > 1]
    assert spanning, "no group spans more than one source -- is source_detail just the group again?"


def test_known_tools_resolve_to_their_files():
    by_name = {i["name"]: i for i in _items()}
    assert by_name["calendar_events"]["source_detail"]["label"] == "actions/calendar_tools.py"
    assert by_name["calendar_events"]["source_detail"]["module"] == "calendar_tools.py"
    from jarvis import screenshot_tools
    name = next(iter(screenshot_tools.SCREENSHOT_TOOLS))
    if name in by_name and by_name[name]["source"] == "builtin":
        assert by_name[name]["source_detail"]["label"] == "screenshot_tools.py"
    # get_battery is a hand-wired built-in defined in tools.py itself.
    assert by_name["get_battery"]["source_detail"]["label"] == "tools.py"


def test_mcp_helper_without_a_server():
    d = _items_by_name()["mcp_list_servers"]["source_detail"]
    assert d["kind"] == "server" and d["label"] == "MCP: built-in helper" and d["server"] == ""


def _items_by_name():
    return {i["name"]: i for i in _items()}


def test_mcp_server_comes_from_the_records_tag_not_the_name():
    d = system_tools._tool_source_detail("mcp_odd_name_x", "mcp", "mcp_tools.py", "[MCP: github] Open an issue.")
    assert d == {"kind": "server", "module": "mcp_tools.py", "server": "github", "label": "MCP: github"}
    # No tag: still a clean MCP entry, never a crash.
    d2 = system_tools._tool_source_detail("mcp_zzz", "mcp", "mcp_tools.py", "")
    assert d2["label"] == "MCP: built-in helper"


def test_user_file_carries_its_path():
    old = system_tools.__dict__.get("_USER_DIR")
    system_tools._USER_DIR = Path("/tmp/jarvis-l18/tools")
    try:
        d = system_tools._tool_source_detail("mine", "user", "mine.py", "")
    finally:
        if old is None:
            system_tools.__dict__.pop("_USER_DIR", None)
        else:
            system_tools._USER_DIR = old
    assert d["kind"] == "file" and d["label"] == "~/.jarvis/tools/mine.py" and d["module"] == "mine.py"
    assert d["path"].replace("\\", "/").endswith("jarvis-l18/tools/mine.py")


def test_builtin_module_sees_through_wrappers_and_never_shows_stdlib():
    def inner(args):
        return {}
    inner.__module__ = "jarvis.screenshot_tools"
    wrapped = functools.wraps(inner)(lambda args: inner(args))
    for probe, expect in (
        (inner, "screenshot_tools.py"),
        (functools.partial(inner), "screenshot_tools.py"),
        (wrapped, "screenshot_tools.py"),
        (json.dumps, "tools.py"),          # a stdlib handler: fall back, don't leak "json"
        (None, "tools.py"),
    ):
        system_tools.TOOLS["__l18_probe__"] = probe
        try:
            got = system_tools._builtin_module_label("__l18_probe__")
        finally:
            system_tools.TOOLS.pop("__l18_probe__", None)
        assert got == expect, (probe, got, expect)
    assert system_tools._builtin_module_label("no_such_tool_anywhere") == "tools.py"


def test_subpackage_modules_read_as_paths():
    def h(args):
        return {}
    h.__module__ = "jarvis.channels.outbound"
    system_tools.TOOLS["__l18_probe__"] = h
    try:
        assert system_tools._builtin_module_label("__l18_probe__") == "channels/outbound.py"
    finally:
        system_tools.TOOLS.pop("__l18_probe__", None)


def test_payload_is_json_serialisable():
    json.dumps(_items())


# --- runner: keep test functions ABOVE this block (AGENTS.md > Testing) ---
if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in fns:
        try:
            f()
            print(f"ok   {n}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {n}\n{e}")
    print(f"{len(fns) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
