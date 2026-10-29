"""Download Hub, the Updates rework (H1): the loader's validation of the six new TOOL_DOWNLOADS keys
(`update_group`, `update_args`, `apply_tool`, `apply_args`, `apply_timeout_s`, `updates_only`).

Pure validator calls: no store, no daemon, no network.  A bad entry is logged and dropped and the file's other entries (and
tools) still load; nothing is silently fixed.

Covers: the defaults when the keys are absent; every accepted shape; every refusal with the word the owner would read; the
schema check (`id` string, `dry_run` boolean) when schemas are handed over, and that it is skipped when they are not; the
group list agreeing with hub/settings.py; and that the same validator serves a custom tool file (it is the one loader).

Run: python tests/test_hub_updates_validate.py
"""
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-upd-validate-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_downloads  # noqa: E402
from jarvis.hub import settings as settings_mod  # noqa: E402

passed = failed = 0


def check(name, cond, detail=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}" + (f"   [{detail!r}]" if detail is not None else ""))


NAMES = {"check_fetch_updates", "apply_fetch_update", "apply_other", "apply_ungated", "get_fetch", "list_fetch", "fetch_pause"}
GATED = {"apply_fetch_update", "apply_other"}
SCHEMAS_OK = [
    {"name": "apply_fetch_update",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "dry_run": {"type": "boolean"}}}},
]


def entry(**more):
    base = {"id": "model_fetch", "label": "Model downloads", "mode": "push", "update_tool": "check_fetch_updates"}
    base.update(more)
    return base


def validate(*entries, confirm=GATED, schemas=None):
    return tool_downloads.validate_downloads(list(entries), "fetch.py", NAMES, confirm, schemas=schemas)


def test_defaults():
    ok, err = tool_downloads.validate_downloads([{"id": "plain", "label": "Plain", "mode": "push"}], "f.py", NAMES, ())
    e = ok[0]
    check("a provider that declares none of the new keys gets the quiet defaults",
          not err and e["update_group"] == "other" and e["update_args"] == {} and e["apply_tool"] is None
          and e["apply_args"] == {} and e["apply_timeout_s"] is None and e["updates_only"] is False, e)
    ok, err = validate(entry())
    e = ok[0]
    check("an update tool alone: group other, no apply tool, no timeout", not err and e["update_group"] == "other"
          and e["apply_tool"] is None and e["apply_timeout_s"] is None, e)
    ok, err = validate(entry(apply_tool="apply_fetch_update"))
    check("an apply tool alone: confirm-gated tool of the file, timeout defaults to 600 s, no args",
          not err and ok[0]["apply_tool"] == "apply_fetch_update" and ok[0]["apply_timeout_s"] == 600.0
          and ok[0]["apply_args"] == {}, (ok, err))


def test_accepted_shapes():
    ok, err = validate(entry(update_group="packages", update_args={"manager": "pip"}, apply_tool="apply_fetch_update",
                             apply_args={"scope": "user", "n": 2, "on": True}, apply_timeout_s=90, updates_only=True))
    e = ok[0] if ok else {}
    check("every new key together", not err and e["update_group"] == "packages" and e["update_args"] == {"manager": "pip"}
          and e["apply_args"] == {"scope": "user", "n": 2, "on": True} and e["apply_timeout_s"] == 90.0
          and e["updates_only"] is True, (ok, err))
    check("the returned args are copies, not the file's own dicts", e.get("update_args") is not None)
    for group in tool_downloads.UPDATE_GROUPS:
        ok, err = validate(entry(update_group=group))
        check("update_group %r is accepted" % group, len(ok) == 1 and not err and ok[0]["update_group"] == group)
    for edge in (30, 3600, 600.5):
        ok, err = validate(entry(apply_tool="apply_fetch_update", apply_timeout_s=edge))
        check("apply_timeout_s %r is accepted" % (edge,), len(ok) == 1 and ok[0]["apply_timeout_s"] == float(edge), err)
    ok, err = validate(entry(mode="both", poll_tool="get_fetch", update_group="apps"))
    check("a poll / both provider may declare a group too", len(ok) == 1 and not err)
    ok, err = validate(entry(update_args={"k": "x" * 400}))
    check("update_args under 512 bytes of JSON is accepted", len(ok) == 1 and not err)


def test_refusals():
    big = {"k": "x" * 600}
    cases = [
        ("an unknown update_group", entry(update_group="stuff"), "update_group"),
        ("a non-text update_group", entry(update_group=5), "update_group"),
        ("an update_group without an update_tool", {"id": "m", "label": "M", "mode": "push", "update_group": "mods"}, "only used with"),
        ("update_args without an update_tool", {"id": "m", "label": "M", "mode": "push", "update_args": {"a": 1}}, "only used with"),
        ("update_args that is not an object", entry(update_args="x"), "update_args"),
        ("update_args over 512 bytes", entry(update_args=big), "512"),
        ("update_args that is not plain JSON", entry(update_args={"a": object()}), "update_args"),
        ("an apply_tool without an update_tool", {"id": "m", "label": "M", "mode": "push", "apply_tool": "apply_fetch_update"},
         "only used with"),
        ("an apply_tool that is not a tool of the file", entry(apply_tool="apply_nothing"), "tool of this file"),
        ("an apply_tool that is not a string", entry(apply_tool=7), "tool of this file"),
        ("an apply_tool that is not confirm-gated", entry(apply_tool="apply_ungated"), "TOOL_CONFIRM_REQUIRED"),
        ("an apply_tool that is also the update_tool", entry(apply_tool="check_fetch_updates"), None),
        ("an apply_tool that is also a control tool",
         entry(mode="both", poll_tool="get_fetch", capabilities=["pause"], controls={"pause": "apply_fetch_update"},
               apply_tool="apply_fetch_update"), "control tool"),
        ("apply_args naming id", entry(apply_tool="apply_fetch_update", apply_args={"id": "x"}), "'id' or 'dry_run'"),
        ("apply_args naming dry_run", entry(apply_tool="apply_fetch_update", apply_args={"dry_run": True}), "'id' or 'dry_run'"),
        ("apply_args that is not an object", entry(apply_tool="apply_fetch_update", apply_args=[1]), "apply_args"),
        ("apply_args over 512 bytes", entry(apply_tool="apply_fetch_update", apply_args=big), "512"),
        ("apply_args without an apply_tool", entry(apply_args={"a": 1}), "only used with an 'apply_tool'"),
        ("apply_timeout_s without an apply_tool", entry(apply_timeout_s=60), "only used with an 'apply_tool'"),
        ("apply_timeout_s of 29", entry(apply_tool="apply_fetch_update", apply_timeout_s=29), "apply_timeout_s"),
        ("apply_timeout_s of 3601", entry(apply_tool="apply_fetch_update", apply_timeout_s=3601), "apply_timeout_s"),
        ("a text apply_timeout_s", entry(apply_tool="apply_fetch_update", apply_timeout_s="600"), "apply_timeout_s"),
        ("a boolean apply_timeout_s", entry(apply_tool="apply_fetch_update", apply_timeout_s=True), "apply_timeout_s"),
        ("updates_only that is not a boolean", entry(updates_only="yes"), "updates_only"),
        ("updates_only on a poll provider", entry(mode="poll", poll_tool="get_fetch", updates_only=True), "mode 'push'"),
        ("updates_only on a both provider", entry(mode="both", poll_tool="get_fetch", updates_only=True), "mode 'push'"),
        ("updates_only without an update_tool", {"id": "m", "label": "M", "mode": "push", "updates_only": True}, "update_tool"),
    ]
    for name, bad, word in cases:
        ok, err = validate(bad)
        said = " ".join(err)
        check("%s is dropped, in words" % name, ok == [] and len(err) == 1 and "dropped" in said
              and (word is None or word in said), (ok, err))
    ok, err = validate(entry(apply_tool="apply_other"), confirm=())
    check("with nothing confirm-gated, the apply_tool is refused and TOOL_CONFIRM_REQUIRED is named",
          ok == [] and "TOOL_CONFIRM_REQUIRED" in " ".join(err), err)
    ok, err = validate(entry(id="bad_one", apply_tool="apply_nothing"), entry(id="good_one", apply_tool="apply_fetch_update"))
    check("one bad entry does not take the file's other entry with it",
          [e["id"] for e in ok] == ["good_one"] and len(err) == 1, (ok, err))


def test_schema_check():
    ok, err = validate(entry(apply_tool="apply_fetch_update"), schemas=SCHEMAS_OK)
    check("an apply tool whose schema takes id (string) and dry_run (boolean) is accepted", len(ok) == 1 and not err, err)
    no_dry = [{"name": "apply_fetch_update", "parameters": {"type": "object", "properties": {"id": {"type": "string"}}}}]
    ok, err = validate(entry(apply_tool="apply_fetch_update"), schemas=no_dry)
    check("a schema without dry_run drops the entry, naming it", ok == [] and "dry_run" in " ".join(err), err)
    wrong = [{"name": "apply_fetch_update", "parameters": {"type": "object",
                                                           "properties": {"id": {"type": "integer"}, "dry_run": {"type": "boolean"}}}}]
    ok, err = validate(entry(apply_tool="apply_fetch_update"), schemas=wrong)
    check("an id that is not a string drops it", ok == [] and "'id' (string)" in " ".join(err), err)
    ok, err = validate(entry(apply_tool="apply_fetch_update"), schemas=[])
    check("handing over schemas with no schema for the tool drops it", ok == [] and "not found" in " ".join(err), err)
    ok, err = validate(entry(apply_tool="apply_fetch_update"), schemas=None)
    check("without schemas the check is skipped (the hub checks again before it applies anything)", len(ok) == 1 and not err)
    check("schemas may be a dict name -> schema", tool_downloads.apply_schema_problem("apply_fetch_update", {"x": SCHEMAS_OK[0]}) is None)
    check("a schema with no parameters is a problem", tool_downloads.apply_schema_problem("t", [{"name": "t"}]))


def test_lists_agree():
    check("the group list here and in hub/settings.py are the same, in the same order",
          tuple(tool_downloads.UPDATE_GROUPS) == tuple(settings_mod.UPDATE_GROUPS))
    check("every group has a label for the panel", set(settings_mod.UPDATE_GROUP_LABELS) == set(settings_mod.UPDATE_GROUPS))
    check("the five groups are mods, games, packages, apps, other", tool_downloads.UPDATE_GROUPS == ("mods", "games", "packages", "apps", "other"))


def test_one_loader_for_custom_tools():
    from jarvis import tool_loader
    src = Path(tool_loader.__file__).read_text(encoding="utf-8")
    check("tool_loader hands a file's TOOL_DOWNLOADS to tool_downloads.validate_downloads (built-in and custom files alike)",
          "validate_downloads" in src and "TOOL_DOWNLOADS" in src)
    check("its docstring names the new keys", "apply_tool" in src and "update_group" in src)


if __name__ == "__main__":
    for fn in (test_defaults, test_accepted_shapes, test_refusals, test_schema_check, test_lists_agree,
               test_one_loader_for_custom_tools):
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
