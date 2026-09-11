"""Phase 3 of the token-optimization plan (see new_plan.md): a local,
keyword-based router that guesses which tools are relevant to a user
message using tool_registry.TOOL_KEYWORDS/TOOL_GROUPS — entirely in
Python, no model round trip and no extra tokens spent.

Deliberately conservative (per plan section on Phase 3): route() only
returns tools when it has real keyword signal, and it activates whole
*groups* rather than single tool names — a message that clearly names
one tool in a group ("commit my changes" -> git_run, in the
system_control group) still gets that group's sibling tools, since a
follow-up call almost always needs a neighbor (git_run's siblings are
the package tools it groups with; spotify_search's siblings are
spotify_play/spotify_control/etc.) and the whole point is to avoid a
second full-schema round trip mid-workflow.

On no match (e.g. "hi", or anything ambiguous), route() returns an
empty, non-confident result. Callers must treat that as "the router
has no opinion", not as "no tools needed" — ai_client.ask() (Phase 4)
falls back to the full catalog in that case, which is the exact
pre-Phase-4 behavior. That fallback is what keeps this phase safe:
routing can only ever narrow what gets offered, never break a request
the old code would have handled.
"""

import re

from .tool_registry import TOOL_GROUPS, TOOL_KEYWORDS, group_of

# A keyword only counts as a real signal at this weight or above (see
# TOOL_KEYWORDS in tool_registry.py) — low-weight entries like "power": 4
# on get_battery are there for future, smarter scoring (e.g. Phase 5's
# search_tools) but are too generic on their own to safely narrow the
# catalog today (e.g. "power" alone shouldn't silently hide every other
# tool for a message that's actually about something else).
MIN_SCORE = 5


class RouteResult:
    """tools: ordered, deduped tool names to offer (schemas_for_tools()
    input). groups: which TOOL_GROUPS keys matched, in match order —
    kept mainly for logging/debugging the router's decisions. confident:
    shorthand for bool(tools); callers branch on this, not on len(groups),
    since a matched group with no tools in it (shouldn't happen, but
    tool_registry's consistency check is what actually guarantees that)
    must still be treated as "no opinion".
    """

    __slots__ = ("tools", "groups", "confident")

    def __init__(self, tools, groups):
        self.tools = tools
        self.groups = groups
        self.confident = bool(tools)

    def __repr__(self):
        return f"RouteResult(tools={self.tools!r}, groups={self.groups!r})"


def route(user_text):
    """Score user_text (case-insensitive substring match) against every
    tool's keywords. Any tool whose matched keyword weight >= MIN_SCORE
    activates its whole group. Returns a RouteResult; see class docstring
    and module docstring for how callers should treat a non-confident
    result.
    """
    text = (user_text or "").lower()
    if not text.strip():
        return RouteResult([], [])

    matched_groups = []
    seen_groups = set()
    for name, keywords in TOOL_KEYWORDS.items():
        group = None
        for phrase, weight in keywords.items():
            if weight < MIN_SCORE:
                continue
            # Word-boundary match, not raw substring containment — a plain
            # `phrase in text` check let "commanded" match the keyword
            # "command" and misfire the commands group (see handoff doc,
            # Bug 1). \b works for both single-word and multi-word phrases
            # already in TOOL_KEYWORDS.
            if re.search(rf"\b{re.escape(phrase)}\b", text):
                group = group or group_of(name)
                break
        if group and group not in seen_groups:
            seen_groups.add(group)
            matched_groups.append(group)

    if not matched_groups:
        return RouteResult([], [])

    tools = []
    seen_tools = set()
    for group in matched_groups:
        for name in TOOL_GROUPS.get(group, []):
            if name not in seen_tools:
                seen_tools.add(name)
                tools.append(name)
    return RouteResult(tools, matched_groups)
