/* ============================================================================
 * slash-commands-data.js — the registry behind the "/" command palette
 * (Menu-free: typed straight into the Ask composer). Master plan Part I.2.
 *
 * THIS FILE IS THE REGISTRY. One entry per typeable verb (35 of them — see
 * `verbs`), plus a disposition for every OTHER canonical name
 * `jarvis-cli/jarvis/reserved_names.py` reserves (`passthrough` + `notExposed`,
 * 159 total) so the palette can show and risk-tier every CLI subcommand
 * that exists, not just the ones it can actually run (see "SCOPE DECISION"
 * below). slash-palette.js (loaded right after this file) is pure engine +
 * UI over this data; it hardcodes no verb names, aliases, or argument
 * sources of its own.
 *
 * FORMAT
 * ------
 * Everything between the JSON-BEGIN / JSON-END markers is strict JSON
 * (double quotes, no trailing commas, no comments) — tests/test_slash_
 * coverage.py parses it, and so does the browser.
 *
 *   groups:      [{id, label}, ...] — Level 1's section headers, in display
 *                order.
 *   verbs:       one entry per typeable command:
 *     verb         canonical typed name (no leading "/")
 *     aliases      alternate names that resolve to the same entry
 *     group        a `groups[].id`
 *     menu         (panel openers only) the id of the Menu item this verb is
 *                  the typed twin of, e.g. "menu-item-guides".
 *                  test_slash_coverage.py requires every menu-item-* in
 *                  index.html to be claimed by exactly one verb, so a new
 *                  Menu panel can't ship without a decision here.
 *     summary      one line, shown in Level 1 and as the row's hover title
 *     riskTier     "safe" | "caution" | "dangerous" — badge + hover copy
 *     instant      true  = takes no argument (nothing to fill in)
 *                  false = the first Enter/Tab completes the argument,
 *                          the second runs it (see slash-palette.js's
 *                          isComplete())
 *                  Even an instant verb only runs on the FIRST Enter when
 *                  it's plain "safe", unconfirmed, and outside the "chat"
 *                  group (panel openers, /skills, /config, /skin...). /new, /stop,
 *                  /clear, /redo, /copy complete first and run on the
 *                  second, so a half-typed prefix can never fire one
 *                  (slash-palette.js's runsOnFirstEnter()).
 *     whileReplying  may it run while Jarvis is still replying to a
 *                    previous ask? (a few handlers double-guard this
 *                    themselves regardless — see slash-palette.js)
 *     confirm      false = never confirm · true = always confirm before
 *                  running · [...] = only when the FIRST argument token
 *                  is one of these (case-insensitive) — e.g. /daemon only
 *                  confirms "stop"/"restart", not "start"/"status"
 *     args         [{name, source, required, hint}, ...] — `source` is one
 *                  of slash-palette.js's ARG_SOURCES keys (a live list to
 *                  fetch, or "freeText" for anything that isn't drawn from
 *                  a fixed set, like a file path or a --var value)
 *     covers       which reserved_names.py names this verb is the reachable
 *                  surface for (test_slash_coverage.py checks this against
 *                  the real RESERVED_NAMES set)
 *     example      one realistic full example, shown in the empty-state /
 *                  hover-help view
 *   passthrough: { name: {summary, riskTier, usage}, ... } — see below.
 *   notExposed:  { name: {reason, riskTier}, ... } — see below.
 *   (a verb's `preview` is the one line the footer shows for what Enter will
 *    do; {0}/{1} are filled from the arguments typed so far)
 *
 * WHAT "EVERY COMMAND" MEANS HERE (master plan D-I4 / K.6.5 / I.2.10)
 * --------------------------------------------------------------------------
 * The owner decided the palette exposes every jarvis-cli subcommand, each
 * with a risk tier, rather than a curated subset. Every one of the reserved
 * names (159, counting skills-loaded) therefore lands in EXACTLY ONE of:
 *
 *   verbs[].covers   reached through a typed verb (the 35 in `verbs`) —
 *                    a panel opener, a picker, or a direct action. Most
 *                    of the 98 names here are the plumbing a panel already
 *                    drives; the verb is how a user gets to them.
 *   passthrough      RUNNABLE as `/<cli-name> [args]`: anything that can run
 *                    one-shot and headless. Shown with its tier; `dangerous`
 *                    ones route through the same confirm modal as /clear.
 *                    The output is shown to the user (a toast if it's a
 *                    line, a dialog if it's more). The server runs ONLY names
 *                    listed here (web/server.js reads this file), with
 *                    an argv array and no shell.
 *   notExposed       cannot be run from a chat box for a TECHNICAL reason —
 *                    a long-running supervisor, an interactive terminal flow,
 *                    microphone hardware, an internal hook, or a syntax token.
 *                    Listed (greyed, with the reason) so nothing is invisible.
 *                    This is deliberately NOT a policy list: a command being
 *                    destructive puts it in `passthrough` as `dangerous`, it
 *                    does not hide it.
 *
 * Adding a subcommand: put its name in `reserved_names.py`, then here in
 * one of the three places above. tests/test_slash_coverage.py fails otherwise.
 *
 * Each `passthrough` entry: {summary, riskTier, usage}. `usage` is shown on
 * hover/in the footer as the arguments it takes ("" = none).
 *
 * WHO EDITS IT
 * ------------
 * Whoever adds, renames, or removes a `jarvis-cli` subcommand. Forgetting a
 * name here is caught by tests/test_slash_coverage.py, the same "computed
 * set vs. hand-maintained set" mechanism test_reserved_names.py already
 * uses for reserved_names.py itself (I-B2) — every name in RESERVED_NAMES
 * must appear in exactly one of `covers` (across all verbs) or
 * `notExposed`, never both, never neither.
 * ============================================================================
 */
window.JARVIS_SLASH_COMMANDS = /*JSON-BEGIN*/
{
  "groups": [
    {
      "id": "chat",
      "label": "Chat"
    },
    {
      "id": "model",
      "label": "Model"
    },
    {
      "id": "skills",
      "label": "Skills"
    },
    {
      "id": "run",
      "label": "Run"
    },
    {
      "id": "panels",
      "label": "Panels"
    },
    {
      "id": "view",
      "label": "View"
    }
  ],
  "verbs": [
    {
      "verb": "new",
      "aliases": [],
      "group": "chat",
      "summary": "Start a new conversation",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "conv-new"
      ],
      "example": "/new",
      "preview": "Starts a new chat."
    },
    {
      "verb": "chat",
      "aliases": [
        "switch"
      ],
      "group": "chat",
      "summary": "Switch to another conversation",
      "riskTier": "safe",
      "instant": false,
      "whileReplying": true,
      "confirm": false,
      "args": [
        {
          "name": "chat",
          "source": "chats",
          "required": true,
          "hint": "title or id \u2014 type to search"
        }
      ],
      "covers": [
        "conv-switch",
        "conv-list",
        "conv-show",
        "conv-search"
      ],
      "example": "/chat weekend trip",
      "preview": "Switches to {0}."
    },
    {
      "verb": "clear",
      "aliases": [],
      "group": "chat",
      "summary": "Clear this conversation's history",
      "riskTier": "dangerous",
      "instant": true,
      "whileReplying": false,
      "confirm": true,
      "args": [],
      "covers": [
        "ai-clear"
      ],
      "example": "/clear",
      "preview": "Clears this chat's history. Asks first; it can't be undone."
    },
    {
      "verb": "stop",
      "aliases": [],
      "group": "chat",
      "summary": "Cancel the reply that's in progress",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [],
      "example": "/stop",
      "preview": "Stops the reply that's in progress."
    },
    {
      "verb": "redo",
      "aliases": [],
      "group": "chat",
      "summary": "Re-run your last prompt",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": false,
      "confirm": false,
      "args": [],
      "covers": [
        "ai-drop-from"
      ],
      "example": "/redo",
      "preview": "Re-runs your last prompt."
    },
    {
      "verb": "copy",
      "aliases": [],
      "group": "chat",
      "summary": "Copy Jarvis's last reply",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [],
      "example": "/copy",
      "preview": "Copies Jarvis's last reply."
    },
    {
      "verb": "provider",
      "aliases": [],
      "group": "model",
      "summary": "Set which AI provider(s) to try, in order",
      "riskTier": "safe",
      "instant": false,
      "whileReplying": true,
      "confirm": false,
      "args": [
        {
          "name": "provider",
          "source": "providers",
          "required": true,
          "hint": "comma-separated, in try-order \u2014 or \"auto\""
        }
      ],
      "covers": [],
      "example": "/provider anthropic,openai",
      "preview": "Uses {0} for your next replies."
    },
    {
      "verb": "think",
      "aliases": [],
      "group": "model",
      "summary": "Set the thinking level, or show/hide its trace",
      "riskTier": "safe",
      "instant": false,
      "whileReplying": true,
      "confirm": false,
      "args": [
        {
          "name": "level",
          "source": "thinkLevels",
          "required": true,
          "hint": "off / low / medium / high / show / hide"
        }
      ],
      "covers": [
        "think"
      ],
      "example": "/think high",
      "preview": "Sets thinking to {0}."
    },
    {
      "verb": "capacity",
      "aliases": [],
      "group": "model",
      "summary": "Switch the prompt's capacity mode",
      "riskTier": "safe",
      "instant": false,
      "whileReplying": true,
      "confirm": false,
      "args": [
        {
          "name": "mode",
          "source": "capacityModes",
          "required": true,
          "hint": "e.g. full / compact / ultra"
        }
      ],
      "covers": [
        "mode",
        "mode-set"
      ],
      "example": "/capacity compact",
      "preview": "Switches the prompt capacity to {0}."
    },
    {
      "verb": "skillload",
      "aliases": [],
      "group": "skills",
      "summary": "Force a skill into context",
      "riskTier": "safe",
      "instant": false,
      "whileReplying": true,
      "confirm": false,
      "args": [
        {
          "name": "skill",
          "source": "installedSkills",
          "required": true,
          "hint": "an installed skill's name"
        }
      ],
      "covers": [
        "skillload"
      ],
      "example": "/skillload pdf",
      "preview": "Loads {0} for this chat until you unload it."
    },
    {
      "verb": "skillunload",
      "aliases": [],
      "group": "skills",
      "summary": "Drop a manually-loaded skill",
      "riskTier": "safe",
      "instant": false,
      "whileReplying": true,
      "confirm": false,
      "args": [
        {
          "name": "skill",
          "source": "loadedSkills",
          "required": true,
          "hint": "a currently-loaded skill \u2014 or --all"
        }
      ],
      "covers": [
        "skillunload"
      ],
      "example": "/skillunload pdf",
      "preview": "Unloads {0} for this chat."
    },
    {
      "verb": "skillmake",
      "aliases": [],
      "group": "skills",
      "summary": "Open the skill manager to write a new skill",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "skillmake",
        "skills-create"
      ],
      "example": "/skillmake",
      "preview": "Opens the skill manager to write a new skill."
    },
    {
      "verb": "skilladd",
      "aliases": [],
      "group": "skills",
      "summary": "Open the skill manager to install one",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "skilladd",
        "skills-add"
      ],
      "example": "/skilladd",
      "preview": "Opens the skill manager to install one."
    },
    {
      "verb": "skills",
      "aliases": [],
      "group": "skills",
      "menu": "menu-item-skills",
      "summary": "Open the skill manager",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "skills-list",
        "skills-get",
        "skills-save",
        "skills-remove"
      ],
      "example": "/skills",
      "preview": "Opens the skill manager."
    },
    {
      "verb": "run",
      "aliases": [],
      "group": "run",
      "summary": "Run a saved command",
      "riskTier": "caution",
      "instant": false,
      "whileReplying": false,
      "confirm": false,
      "args": [
        {
          "name": "command",
          "source": "commands",
          "required": true,
          "hint": "a saved command's name"
        },
        {
          "name": "vars",
          "source": "freeText",
          "required": false,
          "hint": "--var value ... (if it takes any)"
        }
      ],
      "covers": [],
      "example": "/run nightly-backup --target /mnt/data",
      "preview": "Runs the saved command {0}."
    },
    {
      "verb": "daemon",
      "aliases": [],
      "group": "run",
      "summary": "Start, stop, restart, or check one service",
      "riskTier": "caution",
      "instant": false,
      "whileReplying": true,
      "confirm": [
        "stop",
        "restart"
      ],
      "args": [
        {
          "name": "action",
          "source": "daemonActions",
          "required": true,
          "hint": "start / stop / restart / status"
        },
        {
          "name": "id",
          "source": "daemonIds",
          "required": true,
          "hint": "the service's id"
        }
      ],
      "covers": [
        "daemon-start",
        "daemon-stop",
        "daemon-restart",
        "daemon-status"
      ],
      "example": "/daemon restart discord",
      "preview": "Sends {0} to {1}. Stop and restart ask first."
    },
    {
      "verb": "organize-json",
      "aliases": [],
      "group": "run",
      "summary": "Clean up a messy JSON file",
      "riskTier": "safe",
      "instant": false,
      "whileReplying": true,
      "confirm": false,
      "args": [
        {
          "name": "path",
          "source": "freeText",
          "required": true,
          "hint": "path to the file"
        }
      ],
      "covers": [
        "organize-json"
      ],
      "example": "/organize-json ~/Downloads/messy.json",
      "preview": "Cleans up {0}, a local JSON file."
    },
    {
      "verb": "guides",
      "aliases": [],
      "group": "panels",
      "menu": "menu-item-guides",
      "summary": "Open Guides",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [],
      "example": "/guides",
      "preview": "Open Guides."
    },
    {
      "verb": "debug",
      "aliases": [
        "tools"
      ],
      "group": "panels",
      "menu": "menu-item-debug",
      "summary": "Open the Debug dashboard",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "tools-list",
        "tool-run",
        "tool-preview",
        "tool-safety-set"
      ],
      "example": "/debug",
      "preview": "Open the Debug dashboard."
    },
    {
      "verb": "checklist",
      "aliases": [
        "tests"
      ],
      "group": "panels",
      "menu": "menu-item-testchecklist",
      "summary": "Open the Test Checklist",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [],
      "example": "/checklist",
      "preview": "Open the Test Checklist."
    },
    {
      "verb": "schedule",
      "aliases": [
        "sched"
      ],
      "group": "panels",
      "menu": "menu-item-scheduled",
      "summary": "Open Scheduled tasks & reminders",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "sched-list",
        "sched-add",
        "sched-show",
        "sched-cancel",
        "sched-pause",
        "sched-resume",
        "sched-snooze",
        "sched-approve",
        "sched-budget"
      ],
      "example": "/schedule",
      "preview": "Open Scheduled tasks & reminders."
    },
    {
      "verb": "mcp",
      "aliases": [],
      "group": "panels",
      "menu": "menu-item-mcp",
      "summary": "Open MCP servers & tools",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "mcp-status",
        "mcp-refresh",
        "mcp-tools",
        "mcp-config",
        "mcp-edit"
      ],
      "example": "/mcp",
      "preview": "Open MCP servers & tools."
    },
    {
      "verb": "ctools",
      "aliases": [
        "toolmanager"
      ],
      "group": "panels",
      "menu": "menu-item-ctools",
      "summary": "Open the Tool Manager",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "ctools-list",
        "ctools-show",
        "ctools-write",
        "ctools-check",
        "ctools-delete",
        "ctools-run",
        "ctools-toggle",
        "ctools-templates",
        "ctools-suggest",
        "ctools-agent",
        "ctools-drafts",
        "tool-ui",
        "tool-disable-set",
        "command-disable-set",
        "disabled-list",
        "disabled-dependents"
      ],
      "example": "/ctools",
      "preview": "Open the Tool Manager."
    },
    {
      "verb": "channels",
      "aliases": [],
      "group": "panels",
      "menu": "menu-item-channels",
      "summary": "Open Channels",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "channels-status",
        "channels-set",
        "channels-test",
        "channels-whoami",
        "channels-log",
        "channels-directory",
        "channels-people",
        "channels-config",
        "channels-allow",
        "channels-deny",
        "channels-follow",
        "channels-block",
        "channels-users",
        "channels-user",
        "channels-user-tools",
        "channels-add-person",
        "channels-rename",
        "channels-remove-person",
        "channels-note",
        "channels-forget",
        "channels-link",
        "channels-unlink",
        "channels-conversation",
        "channels-usage",
        "channels-user-test",
        "channels-history",
        "channels-handle",
        "channels-tools-for",
        "channels-instruction",
        "channels-presets",
        "channels-preset",
        "channels-bulk",
        "channels-servers",
        "channels-server-set",
        "channels-guilds",
        "channels-channels",
        "channels-master-tools",
        "channels-send",
        "channels-denied"
      ],
      "example": "/channels",
      "preview": "Open Channels."
    },
    {
      "verb": "daemons",
      "aliases": [],
      "group": "panels",
      "menu": "menu-item-daemons",
      "summary": "Open Daemons",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "daemons",
        "daemon-console",
        "daemon-input",
        "daemon-schedule",
        "daemon-add",
        "daemon-edit",
        "daemon-remove",
        "daemon-window",
        "daemons-sync"
      ],
      "example": "/daemons",
      "preview": "Open Daemons."
    },
    {
      "verb": "backlog",
      "aliases": [],
      "group": "panels",
      "menu": "menu-item-backlog",
      "summary": "Open the Backlog",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "backlog",
        "backlog-add",
        "backlog-done",
        "backlog-update",
        "backlog-remove",
        "backlog-board"
      ],
      "example": "/backlog",
      "preview": "Open the Backlog."
    },
    {
      "verb": "logsearch",
      "aliases": [],
      "group": "panels",
      "menu": "menu-item-logsearch",
      "summary": "Open Log Search",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "logs-search",
        "logs-files",
        "logs-tail",
        "logs-sets"
      ],
      "example": "/logsearch",
      "preview": "Open Log Search."
    },
    {
      "verb": "setup",
      "aliases": [],
      "group": "panels",
      "menu": "menu-item-setup",
      "summary": "Open Setup",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "onboard"
      ],
      "example": "/setup",
      "preview": "Open Setup."
    },
    {
      "verb": "subagents",
      "aliases": [],
      "group": "panels",
      "summary": "Open Subagents",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "subagents",
        "subagent-status",
        "subagent-cancel"
      ],
      "example": "/subagents",
      "preview": "Open Subagents."
    },
    {
      "verb": "notifications",
      "aliases": [],
      "group": "panels",
      "summary": "Open Notifications",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "notify-list",
        "notify-history",
        "notify-ack",
        "notify-config",
        "notify-summary",
        "notify-read"
      ],
      "example": "/notifications",
      "preview": "Open Notifications."
    },
    {
      "verb": "logs",
      "aliases": [],
      "group": "panels",
      "summary": "Open this conversation's raw Logs",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "logs",
        "logs-list",
        "logs-show"
      ],
      "example": "/logs",
      "preview": "Open this conversation's raw Logs."
    },
    {
      "verb": "config",
      "aliases": [],
      "group": "view",
      "summary": "Open Config",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "config",
        "ai-config",
        "playnite-config",
        "spotify-config",
        "memory-config",
        "everything-config",
        "voice-config"
      ],
      "example": "/config",
      "preview": "Open Config."
    },
    {
      "verb": "skin",
      "aliases": ["settings"],
      "group": "view",
      "summary": "Open Settings (Skin section)",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [],
      "covers": [
        "personas-list",
        "settings-admin"
      ],
      "example": "/settings",
      "preview": "Open Settings: skin, AI, layout, notifications, tools and safety, memory, About and Advanced."
    },
    {
      "verb": "layout",
      "aliases": [],
      "group": "view",
      "summary": "Switch between classic and focus layout",
      "riskTier": "safe",
      "instant": false,
      "whileReplying": true,
      "confirm": false,
      "args": [
        {
          "name": "mode",
          "source": "layouts",
          "required": true,
          "hint": "classic / focus"
        }
      ],
      "covers": [
        "ui-mode"
      ],
      "example": "/layout focus",
      "preview": "Switches to the {0} layout."
    },
    {
      "verb": "help",
      "aliases": [],
      "group": "view",
      "summary": "List every command, or explain one",
      "riskTier": "safe",
      "instant": true,
      "whileReplying": true,
      "confirm": false,
      "args": [
        {
          "name": "verb",
          "source": "helpVerbs",
          "required": false,
          "hint": "a verb, to see just that one"
        }
      ],
      "covers": [],
      "example": "/help run",
      "preview": "Lists every command, or explains one."
    }
  ],
  "notExposed": {
    "speak": {
      "reason": "needs the microphone/speaker; it's behind the mic and Speak buttons",
      "riskTier": "safe"
    },
    "listen": {
      "reason": "needs the microphone; it's behind the mic button",
      "riskTier": "safe"
    },
    "transcribe": {
      "reason": "internal plumbing behind the mic button",
      "riskTier": "safe"
    },
    "spotify-login": {
      "reason": "an interactive OAuth flow that needs a terminal",
      "riskTier": "caution"
    },
    "browser-setup": {
      "reason": "an interactive setup that needs a terminal",
      "riskTier": "caution"
    },
    "browser-daemon": {
      "reason": "a long-running supervisor; start it with /daemon or /daemons",
      "riskTier": "caution"
    },
    "discord-daemon": {
      "reason": "a long-running supervisor; start it with /daemon or /daemons",
      "riskTier": "caution"
    },
    "instagram-serve": {
      "reason": "a long-running supervisor; start it with /daemon or /daemons",
      "riskTier": "caution"
    },
    "clipboard-watch": {
      "reason": "a long-running supervisor; start it with /daemon or /daemons",
      "riskTier": "caution"
    },
    "ambient": {
      "reason": "a long-running supervisor; start it with /daemon or /daemons",
      "riskTier": "caution"
    },
    "ambient-tick": {
      "reason": "internal supervisor hook that runs on a timer",
      "riskTier": "caution"
    },
    "daemon-run": {
      "reason": "runs a service in the foreground until it exits; it would hang a chat box",
      "riskTier": "caution"
    },
    "daemons-tick": {
      "reason": "internal supervisor hook that runs on a timer",
      "riskTier": "caution"
    },
    "daemon-viewer": {
      "reason": "the live console window's own process; it stays open until the window is closed",
      "riskTier": "caution"
    },
    "sched-daemon": {
      "reason": "a long-running scheduler loop, not a one-shot command",
      "riskTier": "caution"
    },
    "sched-tick": {
      "reason": "internal scheduler hook that runs on a timer",
      "riskTier": "caution"
    },
    "sched-signal": {
      "reason": "internal scheduler hook, not a user-facing command",
      "riskTier": "safe"
    },
    "sched-ask-log": {
      "reason": "internal scheduler hook, not a user-facing command",
      "riskTier": "safe"
    },
    "logs-append-run": {
      "reason": "the server's own logging hook",
      "riskTier": "safe"
    },
    "console-append-run": {
      "reason": "the server's own console-store hook",
      "riskTier": "safe"
    },
    "commands-check-name": {
      "reason": "internal name-validation check used when saving a command",
      "riskTier": "safe"
    },
    "_internal_retitle": {
      "reason": "internal plumbing, not a user-facing command",
      "riskTier": "safe"
    },
    "skills-loaded": {
      "reason": "internal read behind /skillunload's own argument list",
      "riskTier": "safe"
    },
    "subagent-spawn": {
      "reason": "the model's own subagent-dispatch mechanism",
      "riskTier": "caution"
    },
    "subagent-run": {
      "reason": "the model's own subagent-dispatch mechanism",
      "riskTier": "caution"
    },
    "then": {
      "reason": "a chain separator between saved-command names, not a command",
      "riskTier": "safe"
    },
    "and": {
      "reason": "a parallel-batch separator between saved-command names, not a command",
      "riskTier": "safe"
    },
    "-h": {
      "reason": "an argparse flag, not a command",
      "riskTier": "safe"
    },
    "--help": {
      "reason": "an argparse flag, not a command",
      "riskTier": "safe"
    },
    "-v": {
      "reason": "an argparse flag, not a command",
      "riskTier": "safe"
    },
    "--version": {
      "reason": "an argparse flag, not a command",
      "riskTier": "safe"
    }
  },
  "passthrough": {
    "doctor": {
      "summary": "Check the install for problems. Exit 1 means warnings, 2 means something is broken.",
      "riskTier": "safe",
      "usage": "[--deep] [--json] [--verbose] [group ...]",
      "exitCodes": {
        "1": {
          "label": "warnings",
          "level": "warn"
        },
        "2": {
          "label": "problems found",
          "level": "error"
        }
      }
    },
    "version": {
      "summary": "Print the jarvis version.",
      "riskTier": "safe",
      "usage": ""
    },
    "memory-ns": {
      "summary": "List memory namespaces, or switch the active one.",
      "riskTier": "caution",
      "usage": "[namespace]"
    },
    "memory-list": {
      "summary": "List stored memories. --auto shows only the ones saved automatically.",
      "riskTier": "safe",
      "usage": "[--auto]"
    },
    "memory-recall": {
      "summary": "Search stored memories.",
      "riskTier": "safe",
      "usage": "<what you remember>"
    },
    "memory-stats": {
      "summary": "Show memory store statistics.",
      "riskTier": "safe",
      "usage": ""
    },
    "memory-consolidate": {
      "summary": "Merge and tidy stored memories (default: the last 7 days).",
      "riskTier": "caution",
      "usage": "[--days N] [--review | --dry-run] [--no-model]"
    },
    "memory-reindex": {
      "summary": "Rebuild the memory search index. --embeddings also rebuilds the embeddings.",
      "riskTier": "caution",
      "usage": "[--embeddings]"
    },
    "calendar-list": {
      "summary": "List calendar entries.",
      "riskTier": "safe",
      "usage": ""
    },
    "calendar-events": {
      "summary": "Show upcoming calendar events.",
      "riskTier": "safe",
      "usage": ""
    },
    "calendar-add": {
      "summary": "Add an ICS calendar feed and check that it can be reached.",
      "riskTier": "caution",
      "usage": "<name> <ics-url>"
    },
    "calendar-remove": {
      "summary": "Remove a calendar feed by name.",
      "riskTier": "caution",
      "usage": "<name>"
    },
    "digest-status": {
      "summary": "Show whether the daily digest is on.",
      "riskTier": "safe",
      "usage": ""
    },
    "digest-preview": {
      "summary": "Preview the next digest without sending it.",
      "riskTier": "safe",
      "usage": ""
    },
    "digest-on": {
      "summary": "Turn the notification digest on (default: daily).",
      "riskTier": "caution",
      "usage": "[schedule] [time]"
    },
    "digest-off": {
      "summary": "Turn the daily digest off.",
      "riskTier": "caution",
      "usage": ""
    },
    "digest-now": {
      "summary": "Send the digest right now.",
      "riskTier": "caution",
      "usage": ""
    },
    "policy": {
      "summary": "Show the active permission policy.",
      "riskTier": "safe",
      "usage": ""
    },
    "policy-check": {
      "summary": "Check whether a tool call would be allowed.",
      "riskTier": "safe",
      "usage": "<tool> [--args JSON] [--context CTX]"
    },
    "policy-dry-run": {
      "summary": "Dry-run the policy against a tool call.",
      "riskTier": "safe",
      "usage": "<tool> [--args JSON] [--context CTX]"
    },
    "console-read": {
      "summary": "Read one conversation's stored console output.",
      "riskTier": "safe",
      "usage": "<conversation-id> [--since N] [--kinds a,b] [--turn T] [--limit N] [--surface S] [--after-last-clear]"
    },
    "conv-export": {
      "summary": "Export a conversation to a file (default: the current one).",
      "riskTier": "caution",
      "usage": "[conversation-id] [--format F] [--out DIR] [--tools] [--thinking] [--trace]"
    },
    "clipboard-watch-config": {
      "summary": "Show or change the clipboard-watch settings.",
      "riskTier": "caution",
      "usage": "[--pattern REGEX | --clear-pattern] [--poll-seconds N]"
    },
    "subagent-keys": {
      "summary": "List the subagent key pools (keys are hidden). To set or clear a pool, run jarvis subagent-keys in a terminal.",
      "riskTier": "caution",
      "usage": "",
      "maxArgs": 0,
      "noArgsHint": "Setting keys from the chat box isn't supported, so nothing you typed was sent or saved. Use jarvis subagent-keys in a terminal.",
      "secret": true
    },
    "conv-delete": {
      "summary": "Delete an entire conversation. Irreversible.",
      "riskTier": "dangerous",
      "usage": "<conversation-id>"
    },
    "logs-clear": {
      "summary": "Delete one conversation's stored log file. Irreversible.",
      "riskTier": "dangerous",
      "usage": "<conversation-id>"
    },
    "console-clear": {
      "summary": "Mark one conversation's live console as cleared. Nothing is deleted.",
      "riskTier": "caution",
      "usage": "<conversation-id>"
    },
    "sched-clear": {
      "summary": "Remove every finished scheduled job (done, cancelled, error). Active jobs are kept. Irreversible.",
      "riskTier": "dangerous",
      "usage": "",
      "maxArgs": 0,
      "noArgsHint": "/sched-clear takes no options; it always removes every finished job and keeps the active ones."
    },
    "notify-clear": {
      "summary": "Empty the notification inbox. With a consumer name, only marks everything seen for that consumer.",
      "riskTier": "dangerous",
      "usage": "[consumer]"
    },
    "notify-dismiss": {
      "summary": "Delete notifications for good: by id, every one already read (read), or the whole inbox (all).",
      "riskTier": "dangerous",
      "usage": "<id[,id,...]|read|all>"
    },
    "notify-send": {
      "summary": "Send a message to every configured channel.",
      "riskTier": "dangerous",
      "usage": "<message> [title] [channels,csv]"
    },
    "mcp-call": {
      "summary": "Run an arbitrary MCP tool by name.",
      "riskTier": "dangerous",
      "usage": "<server> <tool> [json-arguments]"
    }
  }
}
/*JSON-END*/;
