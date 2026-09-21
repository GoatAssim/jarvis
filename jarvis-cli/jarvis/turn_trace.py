"""Per-turn "what did you just do" trace, in plain language.

THE GAP THIS FILLS
------------------
Everything needed to explain a turn already exists, but only in places a
person has to go looking for:

    * the router's decision      -> tool_router.RouteResult.matches, printed
                                    to stderr as one `$ routed: ...` line
    * which tools ran            -> tool_executor.runs, visible as `$ <tool>`
                                    trace lines while streaming
    * token cost                 -> AskResult.usage, visible in the Debug
                                    dashboard
    * raw request/response       -> logs.py, visible in the Logs panel

So the answer to "why did it do that?" is spread across a stderr stream
that scrolls away, a separate panel, and a JSON file. This module collects
those same facts into ONE small structure per turn, renders them as
sentences rather than identifiers, and rides along with the exchange so the
main chat can show it inline — collapsed by default, one click to open.

Nothing here observes anything new. It's a different *presentation* of
signals ai_client already produces, which is deliberate: a trace that could
disagree with what actually happened would be worse than no trace.

WHY IT'S SAVED AS AN EXTRA
--------------------------
conversations.py already persists per-turn `extras` and the web UI already
replays them by type (see ai_client._extras_from_runs and app.js's
renderThreadExtra). Adding a "trace" type means a reload shows the same
trace the live turn did, with no new storage, no new endpoint and no new
replay plumbing — the same reasoning that made console-dump persistence a
four-line change.

PLAIN LANGUAGE IS THE POINT
---------------------------
"routed: playnite (matched 'launch' on playnite_launch_game)" is a
developer's sentence. This module renders "Recognised this as a games
request (from the word 'launch'), so I loaded the Playnite tools." Same
information; one of them is readable by the person who typed the message.
"""

from . import timespec

# Tool-name -> how to describe it in a sentence. Anything missing falls back
# to a generated phrase (see _describe_call), so this table is a quality
# improvement for common tools rather than a registration requirement — a
# new action file needs no entry here to produce a sensible trace.
_VERBS = {
    "search_tools": "looked through my own tool catalogue",
    "search_commands": "searched your saved commands",
    "get_tool_schema": "read the full details of a tool",
    "run_command": "ran a saved command",
    "run_chain": "ran a chain of saved commands",
    "run_custom_command": "ran a shell command",
    "create_command": "saved a new command",
    "update_command": "updated a saved command",
    "web_search": "searched the web",
    "fetch_page": "read a web page",
    "take_screenshot": "took a screenshot",
    "click_on_text": "found and clicked on-screen text",
    "click": "clicked",
    "type_text": "typed",
    "press_key": "pressed a key",
    "hotkey": "used a keyboard shortcut",
    "focus_window": "brought a window to the front",
    "list_windows": "listed the open windows",
    "get_active_window": "checked which window was in focus",
    "search_files": "searched your files",
    "open_file": "opened a file",
    "reveal_in_explorer": "showed a file in Explorer",
    "present_file": "handed you a file",
    "write_file": "wrote a file",
    "memory_save": "saved something to long-term memory",
    "memory_search": "checked long-term memory",
    "memory_forget": "removed something from memory",
    "git_status": "checked the git status",
    "git_commit_all": "committed the changes",
    "package_install": "installed a package",
    "spotify_now": "checked what's playing",
    "spotify_play": "started playback",
    "playnite_launch_game": "launched a game",
    "playnite_search_games": "searched your game library",
    "schedule_task": "scheduled something",
    "list_scheduled": "listed your scheduled jobs",
    "notify_owner": "sent you a message",
    "get_datetime": "checked the time",
    "get_battery": "checked the battery",
    "get_wifi_info": "checked the network",
    "run_diagnostics": "ran a self-check",
    "calendar_events": "looked at your calendar",
    "calendar_add_event": "added a calendar event",
}

# How a router group reads in a sentence.
_GROUP_NAMES = {
    "core": "system info",
    "memory": "long-term memory",
    "capacity": "capacity settings",
    "commands": "saved commands",
    "desktop": "desktop control",
    "files": "file search",
    "web": "web",
    "spotify": "Spotify",
    "playnite": "games (Playnite)",
    "youtube": "YouTube download",
    "system_control": "git and packages",
    "scheduler": "scheduling",
    "channels": "messaging",
    "json": "JSON",
    "present": "file handoff",
    "skills": "skills",
    "mcp": "MCP servers",
    "calendar": "calendar",
    "diagnostics": "diagnostics",
}

MAX_STEPS = 24
MAX_DETAIL_CHARS = 60


_ENDING_LINES = {
    "forced": ("I ran out of steps before the job was finished, so this summary of what ran "
               "is mine, not the model's."),
    "cutoff": ("The model's reply hit its output limit while it was working out the next "
               "step, so I stopped there and summarised what had run."),
    "truncated": "The model's reply hit its output limit, so it stops mid-thought.",
    "no_provider": ("The actions finished, but no provider was left to write a reply about "
                    "them — so this summary is mine, not the model's."),
    "pending_action": ("You said go ahead, so I ran the step I had proposed directly, after "
                       "the usual confirmation."),
}


class TurnTrace:
    """Everything worth explaining about one ask(), collected as it happens.

    Built empty at the top of ask() and filled by the same callbacks that
    already drive the stderr trace, so there is no second code path that
    could observe something different from what ran.
    """

    __slots__ = ("route", "sticky_groups", "cache_seeded", "steps",
                 "attempts", "provider", "thinking_level", "thinking_chars",
                 "skills", "mode", "tool_count", "degraded", "ending")

    def __init__(self):
        self.route = None            # (groups, matches, confident)
        self.sticky_groups = []      # groups carried over from a prior turn
        self.cache_seeded = []       # tool names pre-seeded from discovery_cache
        self.steps = []              # [{name, detail, ok, note}]
        self.attempts = []           # [(label, error)] for failed providers
        self.provider = None         # the provider that actually answered
        self.thinking_level = "off"
        self.thinking_chars = 0
        self.skills = []             # skill names loaded this turn
        self.mode = None             # capacity mode label
        self.tool_count = 0          # tools actually offered to the model
        self.degraded = False        # answered from completed side effects only
        # Why the turn ended when it was NOT the model finishing on its own
        # (same vocabulary as ai_client.AskResult.ending): None, "forced",
        # "cutoff", "truncated", "no_provider", "pending_action".
        self.ending = None

    # -- collection ------------------------------------------------------

    def note_route(self, route):
        if route is None:
            return
        self.route = (
            list(getattr(route, "groups", None) or []),
            list(getattr(route, "matches", None) or []),
            bool(getattr(route, "confident", False)),
        )

    def note_step(self, name, arguments=None, result=None):
        """One tool call. Trimmed immediately — a 200-call runaway turn
        should not be able to grow this structure without bound, and the
        first two dozen calls are where the interesting decisions are."""
        if len(self.steps) >= MAX_STEPS:
            return
        ok = True
        note = ""
        if isinstance(result, dict):
            if result.get("error"):
                ok = False
                note = str(result.get("error"))[:MAX_DETAIL_CHARS]
            elif result.get("needs_clarification"):
                ok = False
                note = "needed more detail"
            elif result.get("blocked") or result.get("cancelled"):
                ok = False
                note = "you declined it"
        self.steps.append({
            "name": name,
            "detail": _detail_for(name, arguments),
            "ok": ok,
            "note": note,
        })

    def note_attempt_failed(self, label, error):
        self.attempts.append((label, str(error or "")[:120]))

    # -- rendering -------------------------------------------------------

    def to_dict(self):
        """The serializable shape stored as a `trace` extra and read by the
        web UI. `summary` is pre-rendered server-side so the browser and a
        plain terminal can never word the same turn differently."""
        groups, matches, confident = self.route or ([], [], False)
        return {
            "summary": self.summary(),
            "lines": self.lines(),
            "groups": groups,
            "confident": confident,
            "matches": [list(m) for m in matches][:8],
            "sticky": list(self.sticky_groups),
            "cacheSeeded": list(self.cache_seeded),
            "steps": list(self.steps),
            "provider": self.provider,
            "attempts": [list(a) for a in self.attempts][:8],
            "thinking": {"level": self.thinking_level, "chars": self.thinking_chars},
            "skills": list(self.skills),
            "mode": self.mode,
            "toolsOffered": self.tool_count,
            "degraded": self.degraded,
            "ending": self.ending,
        }

    def summary(self):
        """One line, for the collapsed state of the trace bubble."""
        bits = []
        groups, _matches, confident = self.route or ([], [], False)
        if confident and groups:
            bits.append(_group_phrase(groups))
        elif self.sticky_groups:
            bits.append(_group_phrase(self.sticky_groups) + " (carried over)")
        else:
            bits.append("no specific toolset")
        ran = [s for s in self.steps if s["ok"]]
        if ran:
            bits.append("%d tool call%s" % (len(ran), "" if len(ran) == 1 else "s"))
        if self.thinking_level != "off":
            bits.append("thought first (%s)" % self.thinking_level)
        if self.provider:
            bits.append("answered by %s" % self.provider)
        return " · ".join(bits)

    def lines(self):
        """The expanded state: a short ordered narrative of the turn."""
        out = []
        groups, matches, confident = self.route or ([], [], False)

        if confident and groups:
            phrase = _group_phrase(groups)
            trigger = _first_match_phrase(matches)
            if trigger:
                out.append('Read this as a %s request (the word "%s" gave it away), '
                           "so I loaded only those tools." % (phrase, trigger))
            else:
                out.append("Read this as a %s request, so I loaded only those tools." % phrase)
        elif self.sticky_groups:
            out.append("Nothing in this message named a toolset, so I kept the %s tools "
                       "from earlier in the conversation." % _group_phrase(self.sticky_groups))
        else:
            out.append("Nothing in this message pointed at a specific toolset, so I started "
                       "with just the two search tools and looked things up as needed.")

        if self.cache_seeded:
            out.append("A similar earlier message had already found %s, so I had those ready "
                       "without searching again." % _join(self.cache_seeded[:4]))

        if self.tool_count:
            out.append("That meant %d tool%s went into the prompt instead of the whole catalogue."
                       % (self.tool_count, "" if self.tool_count == 1 else "s"))

        if self.thinking_level != "off":
            if self.thinking_chars:
                out.append("Thought it through first at the %s setting (%s of reasoning)."
                           % (self.thinking_level, _approx_words(self.thinking_chars)))
            else:
                out.append("Asked the model to think it through first (%s)." % self.thinking_level)

        if self.skills:
            out.append("Loaded your %s skill%s for this."
                       % (_join(self.skills), "" if len(self.skills) == 1 else "s"))

        for step in self.steps:
            verb = _VERBS.get(step["name"]) or _describe_call(step["name"])
            line = verb[0].upper() + verb[1:]
            if step["detail"]:
                line += ": %s" % step["detail"]
            if not step["ok"]:
                line += " — %s" % (step["note"] or "that didn't work")
            out.append(line + ".")

        for label, error in self.attempts:
            out.append("%s didn't answer (%s), so I moved to the next one." % (label, error))

        # Forced endings are reported AS forced (master plan §5 / F.1): the
        # old single sentence claimed "no provider was left" even when the
        # real reason was that the step limit or the output limit was hit.
        ending_line = _ENDING_LINES.get(self.ending)
        if ending_line:
            out.append(ending_line)
        elif self.degraded:
            out.append("The actions finished, but no provider was left to write a reply about "
                       "them — so this summary is mine, not the model's.")

        if self.provider and not self.attempts:
            out.append("Answered by %s." % self.provider)
        elif self.provider:
            out.append("%s answered in the end." % self.provider)

        return out

    def render_for_terminal(self):
        """The `--explain` CLI view. Same sentences, one per line."""
        lines = self.lines()
        if not lines:
            return ""
        return "\n".join("  " + line for line in lines)


# ---------------------------------------------------------------------------
# Phrase helpers
# ---------------------------------------------------------------------------


def _group_phrase(groups):
    names = [_GROUP_NAMES.get(g, (g or "").replace("_", " ")) for g in groups if g]
    return _join(names) or "general"


def _first_match_phrase(matches):
    """The first matched keyword phrase, for "the word X gave it away"."""
    for entry in matches or []:
        try:
            _group, _tool, phrase = entry
        except (TypeError, ValueError):
            continue
        if phrase:
            return str(phrase)
    return ""


def _describe_call(name):
    """Fallback phrasing for a tool with no _VERBS entry — including every
    auto-discovered action file, which is why this has to read well on a
    name it has never seen: `playnite_list_game_actions` becomes "ran
    playnite list game actions"."""
    readable = (name or "something").replace("_", " ")
    return "ran %s" % readable


def _detail_for(name, arguments):
    """The most identifying argument, short. Never the whole args dict —
    a trace that dumps a 4KB file body is a trace nobody reads."""
    if not isinstance(arguments, dict) or not arguments:
        return ""
    for key in ("query", "name", "text", "path", "url", "command", "game",
                "title", "message", "fact", "target", "window", "keys", "when"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if name == "schedule_task" and key == "when":
                try:
                    return timespec.describe(timespec.parse_trigger(value))
                except Exception:  # noqa: BLE001 — a trace never fails a turn
                    pass
            return _clip(value)
    # Nothing recognizable — show the first short scalar rather than nothing,
    # so a tool with unusual argument names still gets a useful line.
    for key, value in arguments.items():
        if isinstance(value, (str, int, float)) and str(value).strip():
            return "%s=%s" % (key, _clip(str(value)))
    return ""


def _clip(text, limit=MAX_DETAIL_CHARS):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _join(items):
    items = [str(i) for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return "%s and %s" % (items[0], items[1])
    return "%s and %s" % (", ".join(items[:-1]), items[-1])


def _approx_words(chars):
    words = max(1, int(chars / 5.5))
    if words < 100:
        return "about %d words" % (round(words / 10) * 10 or 10)
    return "about %d words" % (round(words / 50) * 50)


def from_extra(data):
    """Rebuild a renderable view from a saved `trace` extra.

    Used by `jarvis conv-show --explain` and the exporter. Deliberately
    tolerant: a trace written by an older version with fewer fields should
    still render, because the alternative is a crash while trying to
    explain something.
    """
    trace = TurnTrace()
    if not isinstance(data, dict):
        return trace
    trace.route = (
        list(data.get("groups") or []),
        [tuple(m) for m in (data.get("matches") or []) if isinstance(m, (list, tuple))],
        bool(data.get("confident")),
    )
    trace.sticky_groups = list(data.get("sticky") or [])
    trace.cache_seeded = list(data.get("cacheSeeded") or [])
    trace.steps = [s for s in (data.get("steps") or []) if isinstance(s, dict)]
    trace.attempts = [tuple(a) for a in (data.get("attempts") or []) if isinstance(a, (list, tuple))]
    trace.provider = data.get("provider")
    thinking = data.get("thinking") or {}
    trace.thinking_level = thinking.get("level") or "off"
    trace.thinking_chars = int(thinking.get("chars") or 0)
    trace.skills = list(data.get("skills") or [])
    trace.mode = data.get("mode")
    trace.tool_count = int(data.get("toolsOffered") or 0)
    trace.degraded = bool(data.get("degraded"))
    trace.ending = data.get("ending") or None
    return trace
