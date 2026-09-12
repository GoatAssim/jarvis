"""Interactive (and scriptable) inspector for the whole token-optimization
pipeline — not a pass/fail test like test_schemas_for_tools.py /
test_enhancements.py, but a human-readable "what would actually happen for
this message" report. For any message, it shows:

  - which TOOL_KEYWORDS phrases matched (and any that were cancelled by an
    exclusion — enhancement #3), with their weight and group
  - the router's decision: matched groups + per-group accumulated score,
    trimmed the same way tool_router.route() trims (enhancement #1), and
    the final tools/groups/confident RouteResult
  - whether a discovery_cache hit (enhancement #2) would pre-seed
    active_schemas, and from what prior query
  - the actual schemas that would be offered to the model this round
    (mirrors ai_client.ask()'s active_schemas construction) vs. the full
    catalog, tool-for-tool
  - a rough token estimate (chars/4 — no tokenizer dependency, so treat
    this as an order-of-magnitude signal, not an exact count) for both,
    and the resulting savings

Three ways to run it:

    python3 tests/interactive_inspector.py
        REPL — type a message, see its report, repeat. Ctrl-D/`quit`/
        `exit` to stop. Reads/writes the REAL ~/.jarvis/discovery_cache.json
        (via discovery_cache.py, unmodified) so what you see matches what
        a real `jarvis ...` invocation would actually do right now.

    python3 tests/interactive_inspector.py "download this youtube video"
        Single-shot — print one report for the given message and exit.
        Same real-cache behavior as the REPL. Handy for scripts/CI (e.g.
        pipe a message in from another tool and grep the output) or a
        quick one-off check without dropping into the REPL.

    python3 tests/interactive_inspector.py --examples
        Batch/automatic mode — runs a curated set of messages chosen to
        exercise each enhancement (near-tie, margin-drop, cap-trim,
        exclusion-cancel, a cache pre-seed hit) and prints a report for
        each, back to back. Isolated to a throwaway discovery_cache file
        (like tests/test_enhancements.py's _isolated_cache) so it never
        touches your real cache — good for a quick "did I break anything
        eyeball-able" pass after changing tool_router.py/tool_registry.py.

This file intentionally mirrors (rather than imports/reuses) two pieces of
internal logic that aren't exposed as standalone functions elsewhere:
tool_router.route()'s scoring/trim algorithm (to also capture per-phrase
match detail for display, which RouteResult doesn't carry — see
enhancement #10 in jarvis-token-optimization-enhancements.md for a real
fix to that) and ai_client.ask()'s active_schemas construction (including
the enhancement #2 cache pre-seed). Every report cross-checks its own
mirrored route computation against a real tool_router.route() call and
prints a loud MIRROR DRIFT warning (not a crash) if they ever disagree —
that's the safety net against this file quietly going stale if
tool_router.py's algorithm changes. If you ever see that warning, trust
tool_router.route() and go fix this file, not the other way around.
"""

import json
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import discovery_cache  # noqa: E402
from jarvis import tool_registry  # noqa: E402
from jarvis import tool_router  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402

_RULE = "-" * 72


@contextmanager
def _isolated_cache():
    """Same isolation helper as tests/test_enhancements.py — used only by
    --examples mode so the batch demo never touches a real ~/.jarvis."""
    original_dir = discovery_cache.JARVIS_DIR
    original_file = discovery_cache.CACHE_FILE
    with tempfile.TemporaryDirectory() as tmp:
        discovery_cache.JARVIS_DIR = Path(tmp)
        discovery_cache.CACHE_FILE = Path(tmp) / "discovery_cache.json"
        try:
            yield
        finally:
            discovery_cache.JARVIS_DIR = original_dir
            discovery_cache.CACHE_FILE = original_file


def _explain_keyword_matches(text):
    """Mirrors tool_router.route()'s scan loop, but keeps every phrase that
    word-boundary-matched (whether it ended up counted or excluded) instead
    of discarding that detail. Returns (matches, group_scores,
    matched_groups_in_first_seen_order) — the last two feed _trim_groups()
    below, kept separate from route() itself per the module docstring."""
    lowered = (text or "").lower()
    matches = []
    group_scores = {}
    matched_groups = []
    seen_groups = set()

    for name, keywords in tool_registry.TOOL_KEYWORDS.items():
        group = None
        for phrase, value in keywords.items():
            weight = tool_registry.keyword_weight(value)
            if weight < tool_router.MIN_SCORE:
                continue
            if not re.search(rf"\b{re.escape(phrase)}\b", lowered):
                continue
            excluded_by = [
                term for term in tool_registry.keyword_exclusions(value)
                if re.search(rf"\b{re.escape(term)}\b", lowered)
            ]
            tool_group = tool_registry.group_of(name)
            matches.append({
                "tool": name, "phrase": phrase, "weight": weight,
                "group": tool_group, "excluded_by": excluded_by,
            })
            if excluded_by:
                continue
            group = group or tool_group
            if group:
                group_scores[group] = group_scores.get(group, 0) + weight
        if group and group not in seen_groups:
            seen_groups.add(group)
            matched_groups.append(group)

    return matches, group_scores, matched_groups


def _trim_groups(matched_groups, group_scores):
    """Mirrors tool_router.route()'s post-scan trim (enhancement #1):
    top-scoring group, then others within ROUTER_GROUP_MARGIN, capped at
    ROUTER_MAX_GROUPS."""
    if len(matched_groups) <= 1:
        return list(matched_groups)
    ranked = sorted(matched_groups, key=lambda g: group_scores.get(g, 0), reverse=True)
    top_score = group_scores.get(ranked[0], 0)
    kept = [ranked[0]]
    for group in ranked[1:]:
        if len(kept) >= tool_router.ROUTER_MAX_GROUPS:
            break
        if top_score - group_scores.get(group, 0) <= tool_router.ROUTER_GROUP_MARGIN:
            kept.append(group)
    return kept


def _active_schemas_for(route, cache_query):
    """Mirrors ai_client.ask()'s active_schemas construction, including the
    enhancement #2 cache pre-seed on a not-confident route. Returns
    (schemas, cache_hits) where cache_hits maps kind -> the found names,
    for the reasons pre-seeding happened (empty dict if none)."""
    if route.confident:
        return system_tools.schemas_for_tools(route.tools), {}

    active = list(system_tools.DISCOVERY_AND_COMMANDS_SCHEMAS)
    seeded = {s.get("name") for s in active}
    cache_hits = {}
    for kind in ("tools", "commands"):
        hit = discovery_cache.cache_lookup(cache_query, kind)
        if not hit:
            continue
        cache_hits[kind] = hit
        for name in hit:
            if name in seeded:
                continue
            group = tool_registry.group_of(name)
            group_names = tool_registry.tools_in_group(group) if group else [name]
            for gname in group_names:
                if gname and gname not in seeded:
                    seeded.add(gname)
                    active.extend(system_tools.schemas_for_tools([gname]))
    return active, cache_hits


def _token_estimate(schemas):
    """Rough chars/4 estimate — no tokenizer dependency. Treat as an
    order-of-magnitude signal (comparing active vs. full catalog), not an
    exact count for any real provider's tokenizer."""
    chars = len(json.dumps(schemas))
    return chars, chars // 4


_FULL_CATALOG_CACHE = None


def _full_catalog():
    global _FULL_CATALOG_CACHE
    if _FULL_CATALOG_CACHE is None:
        _FULL_CATALOG_CACHE = system_tools.tool_schemas_for_session()
    return _FULL_CATALOG_CACHE


def report(text):
    """Print the full diagnostic report for one message."""
    print(_RULE)
    print(f"MESSAGE: {text!r}")
    print(_RULE)

    matches, group_scores, matched_groups = _explain_keyword_matches(text)
    trimmed_groups = _trim_groups(matched_groups, group_scores)
    real_route = tool_router.route(text)

    if set(trimmed_groups) != set(real_route.groups):
        print(
            "  !! MIRROR DRIFT: this file's trim mirror disagrees with the "
            "real tool_router.route() — trust route(), go fix this file. "
            f"mirror={trimmed_groups!r} real={real_route.groups!r}"
        )

    print("\nKEYWORD MATCHES")
    if not matches:
        print(f"  (none reached MIN_SCORE={tool_router.MIN_SCORE})")
    else:
        for m in sorted(matches, key=lambda m: (-m["weight"], m["tool"])):
            status = (
                f"EXCLUDED (matched: {', '.join(m['excluded_by'])})"
                if m["excluded_by"] else "counted"
            )
            print(
                f"  {m['tool']:<22} {m['phrase']!r:<22} weight={m['weight']:<3} "
                f"group={m['group'] or '(none)':<15} {status}"
            )

    print("\nGROUP SCORES (before ROUTER_MAX_GROUPS/ROUTER_GROUP_MARGIN trim)")
    if not group_scores:
        print("  (no group scored — router has no opinion)")
    else:
        for group, score in sorted(group_scores.items(), key=lambda kv: -kv[1]):
            kept = "kept" if group in trimmed_groups else "trimmed"
            print(f"  {group:<18} score={score:<4} {kept}")

    print("\nROUTE RESULT")
    print(f"  confident: {real_route.confident}")
    print(f"  groups:    {real_route.groups}")
    print(f"  tools:     {len(real_route.tools)} -> {real_route.tools}")

    cache_query = (text or "").strip().lower()
    active_schemas, cache_hits = _active_schemas_for(real_route, cache_query)

    print("\nDISCOVERY CACHE")
    if real_route.confident:
        print("  (n/a — router is confident, cache pre-seed only applies "
              "when it has no opinion)")
    elif cache_hits:
        for kind, names in cache_hits.items():
            print(f"  HIT  kind={kind:<8} pre-seeded from a prior "
                  f"similar query -> {names}")
    else:
        print(f"  miss for kind=tools/commands, query={cache_query!r} — "
              "would fall back to search_tools/search_commands")

    print("\nRELEVANT INFO")
    for group in real_route.groups:
        instruction = tool_registry.pack_instruction(group)
        if instruction:
            print(f"  [{group}] {instruction}")
    if not real_route.groups:
        print("  (no matched group, so no pack instruction would be injected)")

    print("\nSCHEMAS THAT WOULD BE OFFERED THIS ROUND (active_schemas)")
    active_names = [s.get("name") for s in active_schemas]
    print(f"  {len(active_names)} tool(s): {active_names}")

    full_catalog = _full_catalog()
    full_names = [s.get("name") for s in full_catalog]

    active_chars, active_tokens = _token_estimate(active_schemas)
    full_chars, full_tokens = _token_estimate(full_catalog)
    savings_pct = (
        100 * (1 - (active_tokens / full_tokens)) if full_tokens else 0
    )

    print("\nTOKEN ESTIMATE (rough, chars/4 — not a real tokenizer)")
    print(f"  active_schemas: {len(active_names):>3} tools, "
          f"{active_chars:>6} chars, ~{active_tokens:>5} tokens")
    print(f"  full catalog:   {len(full_names):>3} tools, "
          f"{full_chars:>6} chars, ~{full_tokens:>5} tokens")
    print(f"  savings this round: ~{savings_pct:.0f}%")
    print()


_EXAMPLES = [
    # Single group, common case — baseline sanity.
    "commit my changes",
    # Enhancement #1: near-tie keeps both groups.
    "what's my battery status, can I also commit this",
    # Enhancement #1: weak group dropped beyond ROUTER_GROUP_MARGIN.
    "what is my battery, wifi status, disk space, and please press a key for me",
    # Enhancement #1: ROUTER_MAX_GROUPS caps a third, otherwise-in-margin group.
    "what's my battery, commit my changes, and download this youtube video",
    # Enhancement #3: exclusion keyword cancels the match entirely.
    "please take a screen shot recording",
    # No opinion at all — the discovery fallback case.
    "run the tts thing",
]


def _run_examples():
    print("Running curated examples in an isolated (throwaway) discovery "
          "cache — your real ~/.jarvis is not touched.\n")
    with _isolated_cache():
        # Pre-seed one cache entry so the "run the tts thing" example (last
        # in _EXAMPLES) has something to demonstrate a cache HIT with —
        # mirrors a user having asked something similar moments earlier in
        # an actual jarvis process (see enhancement #2's own docstring).
        discovery_cache.cache_store("run the tts thing", "commands", ["run_command"])
        for text in _EXAMPLES:
            report(text)


def _repl():
    print("Interactive router/discovery inspector. Type a message and see "
          "its full report. 'quit'/'exit' or Ctrl-D to stop.\n"
          "(Reading/writing your real ~/.jarvis/discovery_cache.json — use "
          "--examples for an isolated demo instead.)\n")
    while True:
        try:
            text = input("jarvis-inspect> ")
        except EOFError:
            print()
            break
        if text.strip().lower() in ("quit", "exit"):
            break
        if not text.strip():
            continue
        report(text)


def main():
    args = sys.argv[1:]
    if args and args[0] == "--examples":
        _run_examples()
    elif args:
        report(" ".join(args))
    else:
        _repl()


if __name__ == "__main__":
    main()
