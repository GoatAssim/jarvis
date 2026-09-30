"""Guards web/public/slash-commands-data.js (the "/" command palette's
registry, master plan Part I.2) against drifting out of sync with
jarvis-cli/jarvis/reserved_names.py, the same "computed set vs. hand-
maintained set" mechanism test_reserved_names.py already uses for
reserved_names.py itself (I-B2).

Every one of RESERVED_NAMES must appear in exactly one of:
  - some verb's "covers" list (reached through a typed verb),
  - the "passthrough" table (runnable as /<cli-name>, with a risk tier), or
  - the "notExposed" table (can't run from a chat box, with a reason),
never two, never none. Every verb, passthrough and notExposed entry must
also carry a well-formed riskTier, since that is the whole point of D-I4's
"every command gets a risk tier" decision — a command with no tier at all
would silently fail to show a risk badge instead of failing this test.

Run with `python3 tests/test_slash_coverage.py`. No API key or network needed.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JARVIS_CLI = REPO_ROOT / "jarvis-cli"
DATA_FILE = REPO_ROOT / "web" / "public" / "slash-commands-data.js"
sys.path.insert(0, str(JARVIS_CLI))

from jarvis.reserved_names import RESERVED_NAMES  # noqa: E402

pass_count = 0
fail_count = 0

VALID_TIERS = {"safe", "caution", "dangerous"}


def check(name, cond, detail=""):
    global pass_count, fail_count
    if cond:
        pass_count += 1
        print("ok  " + name)
    else:
        fail_count += 1
        print("FAIL " + name + (f": {detail}" if detail else ""))


def load_registry():
    text = DATA_FILE.read_text(encoding="utf-8")
    m = re.search(r"/\*JSON-BEGIN\*/(.*)/\*JSON-END\*/", text, re.S)
    assert m, "slash-commands-data.js lost its /*JSON-BEGIN*/ ... /*JSON-END*/ markers"
    return json.loads(m.group(1))


def test_every_reserved_name_is_accounted_for_exactly_once():
    registry = load_registry()
    verbs = registry["verbs"]
    not_exposed = registry["notExposed"]
    passthrough = registry["passthrough"]

    covered = []
    for spec in verbs:
        covered.extend(spec.get("covers") or [])
    covered_set = set(covered)
    not_exposed_set = set(not_exposed)
    passthrough_set = set(passthrough)

    dupes_within_covered = sorted({n for n in covered if covered.count(n) > 1})
    check(
        "no reserved name is covered by two different verbs",
        not dupes_within_covered,
        f"covered more than once: {dupes_within_covered}",
    )

    overlap = sorted((covered_set & not_exposed_set) | (covered_set & passthrough_set) | (passthrough_set & not_exposed_set))
    check(
        "no name is in more than one of covers / passthrough / notExposed",
        not overlap,
        f"in more than one: {overlap}",
    )

    accounted = covered_set | not_exposed_set | passthrough_set
    missing = sorted(RESERVED_NAMES - accounted)
    check(
        "every RESERVED_NAMES entry is covered by a verb, in passthrough, or in notExposed",
        not missing,
        f"missing from slash-commands-data.js entirely: {missing}. "
        "Add each one to some verb's `covers` list, to `passthrough` (if it can "
        "run one-shot and headless), or to `notExposed` (with the technical reason).",
    )

    extra = sorted(accounted - RESERVED_NAMES)
    check(
        "slash-commands-data.js doesn't invent names reserved_names.py doesn't have",
        not extra,
        f"not in RESERVED_NAMES: {extra}. Remove these, or add them to reserved_names.py first.",
    )


def test_every_verb_is_well_formed():
    registry = load_registry()
    verbs = registry["verbs"]
    group_ids = {g["id"] for g in registry["groups"]}

    names_seen = set()
    for spec in verbs:
        verb = spec.get("verb", "<unnamed>")
        check(
            f'"{verb}" has a valid riskTier',
            spec.get("riskTier") in VALID_TIERS,
            f"got {spec.get('riskTier')!r}",
        )
        check(
            f'"{verb}" belongs to a real group',
            spec.get("group") in group_ids,
            f"got {spec.get('group')!r}, not one of {sorted(group_ids)}",
        )
        check(
            f'"{verb}" declares instant/whileReplying as real booleans',
            isinstance(spec.get("instant"), bool) and isinstance(spec.get("whileReplying"), bool),
        )
        for token in [verb] + list(spec.get("aliases") or []):
            check(
                f'"{token}" is not reused by another verb or alias',
                token not in names_seen,
                f'"{token}" already used',
            )
            names_seen.add(token)
        for arg in spec.get("args") or []:
            check(
                f'"{verb}" arg "{arg.get("name")}" declares required as a real boolean',
                isinstance(arg.get("required"), bool),
            )


def test_every_not_exposed_entry_is_well_formed():
    registry = load_registry()
    for name, entry in registry["notExposed"].items():
        check(
            f'notExposed["{name}"] has a valid riskTier',
            entry.get("riskTier") in VALID_TIERS,
            f"got {entry.get('riskTier')!r}",
        )
        check(
            f'notExposed["{name}"] has a non-empty reason',
            bool((entry.get("reason") or "").strip()),
        )


def test_every_passthrough_entry_is_well_formed():
    registry = load_registry()
    for name, entry in registry["passthrough"].items():
        check(
            f'passthrough["{name}"] has a valid riskTier',
            entry.get("riskTier") in VALID_TIERS,
            f"got {entry.get('riskTier')!r}",
        )
        check(
            f'passthrough["{name}"] has a summary and a usage string',
            bool((entry.get("summary") or "").strip()) and isinstance(entry.get("usage"), str),
        )
        check(
            f'passthrough["{name}"] is not a flag, separator or leading-underscore internal',
            not name.startswith("-") and not name.startswith("_") and name not in {"then", "and"},
        )


def test_destructive_names_are_tiered_dangerous_not_hidden():
    # I.2.10: the tiering "replaces the exclude-list ... they're now included
    # but flagged, not hidden". These are the plan's own examples of
    # `dangerous`; if one of them ever slides to a milder tier, or back into
    # notExposed, that is a decision and should fail loudly.
    registry = load_registry()
    for name in ("logs-clear", "sched-clear", "notify-clear", "notify-send", "mcp-call", "conv-delete"):
        entry = registry["passthrough"].get(name)
        check(
            f'"{name}" is runnable passthrough and tiered dangerous',
            bool(entry) and entry.get("riskTier") == "dangerous",
            f"got {entry!r}",
        )


def test_not_exposed_is_only_for_technical_reasons():
    # notExposed is not a policy list: a long-running supervisor, an
    # interactive flow, hardware, an internal hook, or a syntax token. A
    # destructive command belongs in passthrough as dangerous.
    registry = load_registry()
    technical = re.compile(
        r"supervisor|foreground|terminal|interactive|microphone|mic\b|internal|hook|plumbing|"
        r"long-running|scheduler loop|separator|argparse|dispatch|logging|console-store|name-validation",
        re.I,
    )
    for name, entry in registry["notExposed"].items():
        check(
            f'notExposed["{name}"] gives a technical reason',
            bool(technical.search(entry.get("reason") or "")),
            f"reason was: {entry.get('reason')!r}",
        )


KNOWN_ARG_SOURCES = {
    "freeText", "chats", "commands", "providers", "installedSkills", "loadedSkills",
    "daemonIds", "daemonActions", "capacityModes", "thinkLevels", "layouts", "helpVerbs",
}
TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]*$")
INDEX_HTML = REPO_ROOT / "web" / "public" / "index.html"


def test_every_menu_panel_is_claimed_by_exactly_one_verb():
    # I.2.9 (4): add a Menu panel and forget the palette, and this fails.
    html = INDEX_HTML.read_text(encoding="utf-8")
    menu_ids = sorted(set(re.findall(r'id="(menu-item-[a-z0-9-]+)"', html)))
    check("index.html has Menu items to check", len(menu_ids) >= 10, f"found {menu_ids}")
    claims = {}
    for spec in load_registry()["verbs"]:
        if spec.get("menu"):
            claims.setdefault(spec["menu"], []).append(spec["verb"])
    for mid in menu_ids:
        owners = claims.get(mid, [])
        check(
            f"{mid} is claimed by exactly one verb",
            len(owners) == 1,
            f"claimed by {owners}. Give a verb a `menu` field of \"{mid}\" (or, if it is "
            "deliberately not typeable, say why in this test).",
        )
    for mid, owners in claims.items():
        check(f'verb(s) {owners} point at a Menu item that exists', mid in menu_ids, f"{mid} is not in index.html")


def test_verbs_and_aliases_are_well_formed_tokens():
    # I.2.9 (3): the grammar splits on whitespace and rejects a slash, so a
    # verb can only be lowercase letters, digits and hyphens.
    for spec in load_registry()["verbs"]:
        for token in [spec["verb"]] + list(spec.get("aliases") or []):
            check(f'"{token}" matches ^[a-z][a-z0-9-]*$', bool(TOKEN_RE.match(token)))
    for name in load_registry()["passthrough"]:
        check(f'passthrough "{name}" is typeable after a slash', bool(TOKEN_RE.match(name)), f"{name!r}")


def test_every_verb_has_a_summary_and_known_argument_sources():
    # I.2.9 (6)
    for spec in load_registry()["verbs"]:
        verb = spec["verb"]
        check(f'"{verb}" has a summary and a preview', bool((spec.get("summary") or "").strip()) and bool((spec.get("preview") or "").strip()))
        for arg in spec.get("args") or []:
            check(
                f'"{verb}" arg "{arg.get("name")}" has a known source',
                arg.get("source") in KNOWN_ARG_SOURCES,
                f"got {arg.get('source')!r}; slash-palette.js has no provider for it",
            )


def test_confirm_gated_verbs_are_never_marked_safe():
    # A verb the owner decided should ask "are you sure?" before running
    # (see D-I7) is definitionally not the lowest risk tier — catches the
    # data file drifting a badge without also dropping its confirm gate,
    # or vice versa.
    registry = load_registry()
    for spec in registry["verbs"]:
        confirm = spec.get("confirm")
        gated = confirm is True or (isinstance(confirm, list) and confirm)
        if gated:
            check(
                f'"{spec["verb"]}" is confirm-gated and not tiered "safe"',
                spec.get("riskTier") != "safe",
                f"riskTier is {spec.get('riskTier')!r} but confirm is {confirm!r}",
            )


# ---------------------------------------------------------------------------
# Passthrough `usage` must describe what the CLI really accepts (I-B14)
# ---------------------------------------------------------------------------
# The registry's `usage` and `summary` are what the footer, the hover text and
# the dangerous-command confirm dialog show. They drifted from the CLI once
# (calendar-add was documented as "<when> <title>" but takes "<name> <ics-url>";
# sched-clear advertised a --all flag the CLI never parsed; console-clear was
# described as wiping data it only marks), and nothing noticed, because the
# coverage test only looked at names and tiers.
#
# What this can and cannot check. The CLI keeps no machine-readable argument
# spec, so there are two mechanical checks and one rule:
#   1. where cli.py prints its own "usage: jarvis <name> ..." line, the registry
#      `usage` must be that text exactly;
#   2. every --flag named in a `usage` must appear as a quoted literal somewhere
#      in jarvis-cli/jarvis/*.py (catches a flag that does not exist anywhere;
#      it cannot prove the flag belongs to THAT command);
#   3. positional shapes for commands with no usage line in the CLI are
#      hand-verified against the handlers, and this test does not see them.
# If you change a command's arguments in cli.py, update the registry by hand.

CLI_SOURCES = sorted((JARVIS_CLI / "jarvis").glob("*.py"))
CLI_TEXT = "\n".join(p.read_text(encoding="utf-8") for p in CLI_SOURCES)
USAGE_LITERAL_RE = re.compile(r'usage: jarvis ([a-z][a-z0-9-]*) ([^"\\\n]*)')


def cli_usage_literals():
    found = {}
    for name, tail in USAGE_LITERAL_RE.findall(CLI_TEXT):
        found.setdefault(name, set()).add(tail.strip())
    return found


def test_passthrough_usage_matches_the_clis_own_usage_line():
    literals = cli_usage_literals()
    for name, entry in load_registry()["passthrough"].items():
        if name not in literals:
            continue
        if entry.get("maxArgs") == 0:
            continue   # deliberately narrower than the CLI (subagent-keys, sched-clear)
        check(
            f'passthrough["{name}"] usage equals the CLI\'s own usage line',
            entry["usage"].strip() in literals[name],
            f"registry says {entry['usage']!r}; cli.py says {sorted(literals[name])!r}",
        )


def test_passthrough_usage_flags_exist_in_the_cli():
    for name, entry in load_registry()["passthrough"].items():
        for flag in re.findall(r"--[a-z][a-z0-9-]*", entry["usage"] + " " + entry["summary"]):
            check(
                f'passthrough["{name}"] mentions {flag}, which the CLI knows',
                f'"{flag}"' in CLI_TEXT or f"'{flag}'" in CLI_TEXT,
                f"{flag} is not a quoted literal anywhere in jarvis-cli/jarvis/*.py",
            )


def test_passthrough_argument_limits_and_exit_codes_are_well_formed():
    for name, entry in load_registry()["passthrough"].items():
        if "maxArgs" in entry:
            check(f'passthrough["{name}"] maxArgs is a non-negative integer',
                  isinstance(entry["maxArgs"], int) and not isinstance(entry["maxArgs"], bool) and entry["maxArgs"] >= 0)
            check(f'passthrough["{name}"] with maxArgs explains the refusal in noArgsHint',
                  bool((entry.get("noArgsHint") or "").strip()))
        if entry.get("maxArgs") == 0:
            check(f'passthrough["{name}"] takes no arguments, so its usage is empty',
                  entry["usage"] == "", f"usage is {entry['usage']!r}")
        if "secret" in entry:
            check(f'passthrough["{name}"] secret is a real boolean', isinstance(entry["secret"], bool))
        if entry.get("secret"):
            check(f'passthrough["{name}"] takes secrets, so it must accept no arguments from the palette',
                  entry.get("maxArgs") == 0)
        for code, meaning in (entry.get("exitCodes") or {}).items():
            check(f'passthrough["{name}"] exitCodes key {code!r} is a positive integer string',
                  code.isdigit() and int(code) > 0)
            check(f'passthrough["{name}"] exitCode {code} has a label and a valid level',
                  bool((meaning.get("label") or "").strip())
                  and meaning.get("level") in {"info", "success", "warn", "error"},
                  f"{meaning!r}")


def test_commands_that_take_secrets_or_change_state_are_not_tiered_safe():
    # I-B15 / I-B16. `jarvis memory-ns <name>` switches the active namespace, so
    # a registry entry that advertises an argument must not be `safe`.
    registry = load_registry()["passthrough"]
    ns = registry["memory-ns"]
    check('"memory-ns" accepts a namespace to switch to and is not tiered "safe"',
          "[namespace]" in ns["usage"] and ns["riskTier"] != "safe", f"got {ns!r}")
    keys = registry["subagent-keys"]
    check('"subagent-keys" is list-only from the palette and flagged secret',
          keys.get("maxArgs") == 0 and keys.get("secret") is True, f"got {keys!r}")
    check('"doctor" names exit codes 1 and 2 instead of reporting warnings as a failure',
          set((registry["doctor"].get("exitCodes") or {}).keys()) == {"1", "2"}
          and registry["doctor"]["exitCodes"]["1"]["level"] != "error")


if __name__ == "__main__":
    test_every_reserved_name_is_accounted_for_exactly_once()
    test_every_verb_is_well_formed()
    test_every_not_exposed_entry_is_well_formed()
    test_every_passthrough_entry_is_well_formed()
    test_destructive_names_are_tiered_dangerous_not_hidden()
    test_not_exposed_is_only_for_technical_reasons()
    test_every_menu_panel_is_claimed_by_exactly_one_verb()
    test_verbs_and_aliases_are_well_formed_tokens()
    test_every_verb_has_a_summary_and_known_argument_sources()
    test_confirm_gated_verbs_are_never_marked_safe()
    test_passthrough_usage_matches_the_clis_own_usage_line()
    test_passthrough_usage_flags_exist_in_the_cli()
    test_passthrough_argument_limits_and_exit_codes_are_well_formed()
    test_commands_that_take_secrets_or_change_state_are_not_tiered_safe()
    print(f"\n{pass_count} passed, {fail_count} failed")
    if fail_count:
        sys.exit(1)
