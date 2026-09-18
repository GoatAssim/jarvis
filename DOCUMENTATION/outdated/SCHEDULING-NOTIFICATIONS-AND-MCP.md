# Scheduling, notifications, conversation search, and MCP

This document covers four additions plus three bug fixes, in the order they
build on each other. It's written for whoever picks the codebase up next —
the *why* matters more than the *what*, since the what is readable from the
source.

---

## 1. Scheduled tasks, notifications and reminders

### One engine, three faces

"Run the backup at 2am", "tell me when the backup finishes", and "remind me
to call mum at 6" are the same mechanism wearing three hats. Splitting them
into three subsystems would mean three stores to keep consistent, three
places to fix a DST bug, and — the real problem — no way to chain them,
since "when the backup task is done, notify me" needs the notification to be
able to listen to the task.

So there is **one record type**, a job, with two independent halves:

| half | values |
|---|---|
| `trigger` (when) | `at` · `every` · `event` · `startup` |
| `action` (what) | `notify` · `ask` · `command` · `tool` |

`kind` (`task` / `notify` / `reminder`) is **purely labelling**. It steers
defaults and which list a job appears in, and nothing else. A reminder *is* a
notify-action job with `kind="reminder"`. That's what "reminders use the
notifying engine" means in practice: there was never a second one.

```
jarvis/timespec.py   parse "every weekday at 08:30" -> a trigger dict
jarvis/scheduler.py  the store, the trigger maths, the tick loop
jarvis/notifier.py   delivery: inbox, stream, toast, voice, playnite
jarvis/actions/scheduler_tools.py   the six AI-facing tools
```

### The hard constraint: there is no daemon

Jarvis is a brand-new OS process on every invocation. Nothing runs between
your commands, so **nothing can wake up at 9am on its own**. Pretending
otherwise would produce a scheduler that silently never fires — the worst
possible outcome for a reminder.

Everything is therefore driven by an explicit **tick**, and any of these
drivers is enough:

| driver | covers |
|---|---|
| `web/server.js` | every 30s while the web console is open, plus one `--startup` tick on boot. The usual case. |
| `jarvis sched-tick` under Task Scheduler / cron | firing while the browser is closed |
| the Scheduled panel's **Run due now** button | manual |
| `jarvis ask` | *drains notifications only* — no execution |

That last one is deliberate. Running due jobs on the ask path would put an
unbounded amount of work (a 300-second command, a spawned `ask`) on the
latency path of every single reply. Reading a JSON file is microseconds;
firing is `sched-tick`'s job.

Concurrent ticks are safe: a lock file (`scheduler._claim_lock`) makes a
second tick a no-op rather than a double-fire, because "your 9am reminder
arrived twice" and "your backup ran twice" are both real damage. A lock older
than 10 minutes is treated as abandoned and stolen — a wedged scheduler that
never fires again is far worse than a rare double-run after a crash.

### Missed runs

Asleep for three days with a daily job? Firing three times in a row is never
what anyone wants. The default (`catch_up=False`) fires **once** and advances
to the next future slot. `catch_up=True` opts into make-up runs, capped at
`MAX_CATCHUP_RUNS` (5) so a month offline can't spawn 30 processes.

### The approval gate

A scheduled job runs with nobody watching, which is exactly the situation
`tool_safety.py` exists for. So the confirmation moves from *call* time to
*creation* time: a job whose action would run a command, a confirm-gated
tool, or a full `ask` is created with `status="needs_approval"` and will not
run until a human clears it out of band:

```
jarvis sched-approve <id>          # or the web panel's Approve button
```

The model **cannot** approve its own job — same principle as not letting it
fill in a `confirm: true` parameter for itself. Jobs created from the CLI or
the web panel pass `trusted=True` and skip the gate, because a human is
literally typing the command. A plain notification is never gated.

### Notification delivery

| channel | behaviour |
|---|---|
| `inbox` | **durable, always written.** The queue the web console and terminal both drain. |
| `stream` | a `JARVIS_MEDIA` line, so an open web chat shows it live |
| `toast` | native OS notification (BurntToast → NotifyIcon on Windows, `osascript`, `notify-send`) |
| `voice` | spoken aloud. Off by default — a job talking out loud in a quiet room is an unwelcome surprise. |
| `playnite` | reuses the existing `playnite_notify` tool |

The inbox is the load-bearing part. Every other channel is fire-and-forget: a
toast pops whether or not anyone is there, and a WebSocket broadcast reaches
only the tabs open *right now*. A reminder firing into a closed browser would
otherwise be indistinguishable from never having fired.

Acknowledgement is tracked **per consumer**, not as one delivered flag,
because a browser and a terminal are genuinely different places a person
might be looking. A reminder the web console showed still prints in a
terminal session that hasn't seen it.

### Chaining — "when something is done"

```
schedule_task(when="every night at 2am", emit_on_done="backup_done", ...)
notify_me(when="when backup_done", message="Backup finished")
```

Every completed job also announces `job_<id>_done` automatically, so a
listener can be written before the job it listens to exists. `signal_event`
lets the model (or `jarvis sched-signal`) announce anything by name; event
names normalize loosely, so `Backup Done!` and `backup_done` are the same
event — if they weren't, chaining would silently never fire.

### The six tools

| tool | for |
|---|---|
| `remind_me` | "remind me to call mum at 6" |
| `notify_me` | immediate or triggered notification |
| `schedule_task` | work that actually runs (`ask` / `command` / `tool`) |
| `list_scheduled` | what's set up, with ids |
| `cancel_scheduled` | cancel / pause / resume / snooze |
| `signal_event` | "X is done" → fires whatever was waiting |

Three creating tools rather than one `schedule(kind=...)` because
`tool_router.route()` matches keywords to **tool names**, and "remind me" /
"notify me" / "schedule" are three genuinely different phrasings. Three
narrow tools each get their own keyword block and worked examples, which is
what actually steers correct selection. The shared engine makes the split
nearly free.

### CLI

```
jarvis sched-list [--text] [--all]        jarvis sched-tick [--startup]
jarvis sched-add <when> <message> [kind]  jarvis sched-show <id>
jarvis sched-cancel|pause|resume|approve <id>
jarvis sched-snooze <id> [delay]          jarvis sched-signal <event> [detail]
jarvis sched-clear                        jarvis notify-send <message> [title] [channels]
jarvis notify-list|ack|clear|config
```

### Time expressions

Deterministic, no new dependencies, 50 tests. The model passes the user's own
wording through rather than doing date arithmetic in its head against a
"now" it only knows from a prompt string — it gets that wrong in exactly the
cases that matter most ("in 20 minutes" at 23:52, "friday" on a Friday, DST
boundaries).

```
in 20 minutes · in 1 hour 30 minutes · in an hour
at 9am · 9:30pm · 17:00 · noon · midnight
tomorrow at 9am · today at 5pm · tonight
monday at 8am · next friday at 14:00
2026-09-16T09:00 · 2026-09-16 09:00 · 2026-09-20
every 30 minutes · every day at 9am · daily at 08:00
every monday at 8 · every weekday at 09:15 · every weekend · every 3 days
on startup · when <event_name>
```

Unreadable input **raises** rather than defaulting. A reminder that fires at
the wrong hour is worse than one that was never created, because the user
believes it's set.

Everything is naive local time on purpose: storing UTC would mean converting
twice for display and getting "every day at 9am" subtly wrong across a DST
change (a fixed UTC offset drifts by an hour; a naive wall-clock time
doesn't, which is what people expect from a daily alarm).

---

## 2. Conversation search

`conversations.other_conversations_context()` deliberately gives the model
only a **title and one-line gist** of every other conversation, never their
messages, so unrelated chats can't leak into the current one. That's the
right default, but it left a real gap: searching for a phrase you remember
*saying* found nothing unless it happened to land in a generated title.

`jarvis/conv_search.py` reads the transcripts themselves. It's a **grep, not
an embedding index**: no model call, no background job to keep warm, no extra
state that can go stale against the files it describes. On a few hundred
conversations that's milliseconds, and it can never disagree with what's on
disk — which matters more here than raw speed, because a search index that
silently lags is worse than no search at all.

| mode | behaviour |
|---|---|
| `words` (default) | every term appears somewhere in the same turn, any order |
| `phrase` | one contiguous substring |
| `regex` | a real regular expression, length-capped |

Snippets wrap the match in `«»` so callers can highlight without re-running
the regex. Tool arguments and results are searchable but **opt-in** —
otherwise "find where I talked about X" matches any file path that happened
to contain X.

```
jarvis conv-search "postgres index" --text
jarvis conv-search "composite indexes" --phrase --text
jarvis conv-search "B-?tree" --regex --in <conv-id> --since 2026-09-01
```

The `search_conversations` tool joins the existing **memory** group, because
"what did we say about X" and `memory_search` are the same intent from the
model's point of view. It's the deliberate, on-request exception to the
title-only rule — the user has to ask, and it shows up in the tool trace
rather than being folded invisibly into the system prompt.

---

## 3. MCP client support

An MCP server is a separate program exposing tools over JSON-RPC. This makes
those tools land in jarvis's catalog next to the built-ins, callable
identically.

### Why it reads a cache

Every other MCP client is a long-running host: it starts its servers once and
lists tools from memory. Jarvis is a fresh process per invocation, so doing
that here would mean spawning every configured server and handshaking with
each one on **every** `jarvis ask`, before the model is even called. With
three servers that's seconds of latency on questions that may not involve MCP
at all.

So discovery and execution are split:

- the **tool list** is cached to `~/.jarvis/mcp_cache.json` and read at
  catalog-build time — no subprocess, microseconds
- a **tool call** connects for real, pooled per process, so an ask calling
  three tools on one server pays startup once

Same trade the hybrid catalog tier makes for built-in tools: a little
staleness to avoid paying startup every turn. A tool removed since the last
refresh fails with a clear message rather than hanging.

### The elegant part

`actions/mcp_tools.py` **builds** its `TOOL_SCHEMAS` from that cache at import
time. Because `tool_loader` only cares that a module exposes the contract, a
foreign protocol plugs in with **zero changes** to `tools.py`,
`tool_loader.py`, `tool_safety.py`, or anything else. Tools are named
`mcp_<server>_<tool>`, routed on the server name and the remote tool's own
words at weight 6 — enough to clear `MIN_SCORE`, not enough to outbid a
first-party tool that does the same job.

### Transports

- **stdio** — a local command. We own its lifetime and talk newline-delimited
  JSON-RPC over its pipes. Its **stderr is drained on a daemon thread**,
  because a server that logs chattily will otherwise fill the pipe buffer and
  block forever on its next write — which looks exactly like a hung handshake
  and is miserable to debug.
- **http** — a remote server over JSON-RPC POST, using `requests`.

Non-JSON banner lines on stdout and unsolicited notification frames are both
skipped rather than treated as errors, since real servers emit both.

### Security

1. Servers can **only** be added by editing `~/.jarvis/mcp_config.json`, never
   by a model tool. Nothing the model says should be able to introduce a new
   executable into the loop.
2. MCP tools are **confirm-gated by default**. A server marked
   `"trusted": true` opts out, per server, as a human choice.

```
jarvis mcp-config     # print the config path
jarvis mcp-refresh    # reconnect to every server, rewrite the cache
jarvis mcp-status     # what's connected, tool counts, errors, staleness
jarvis mcp-call <server> <tool> '<json>'   # prove a server works, no model
```

> **Known rough edge:** after `mcp-refresh` adds new tools, Jarvis needs a
> restart for them to appear, since discovery runs at import time. The web
> panel's toast says so.

A server that fails is recorded **with its error** rather than dropped, so
`mcp-status` shows `github: command not found` instead of silently showing
nothing — the difference between a diagnosable problem and a mystery.

---

## 4. Conversation-saving bugs (fixed)

Three separate causes, all of which looked to a user like "my conversation
didn't save properly".

### a. Nothing was saved unless the ask fully succeeded

`conversations.append_exchange()` was called from exactly one place: inside
`if result.ok:` in `ai_client.ask()`. Anything that stopped the process first
discarded the user's message entirely — and the web Stop button does exactly
that (`killTree` → `taskkill /F` on Windows, `SIGTERM` elsewhere, which skips
every `finally` block and `atexit` hook). On a first message this also meant
no title was ever generated, so the whole conversation looked like it had
never happened.

Persistence is now three steps:

```
begin_exchange()     writes the user's half BEFORE any provider is contacted
complete_exchange()  upgrades it in place on success
abandon_exchange()   marks it interrupted when it dies
```

plus a `SIGTERM`/`SIGINT` handler in `cli.py` so an abort records *why*
rather than leaving a turn pending forever. `taskkill /F` can't be caught at
all, which is precisely why the `begin_exchange()` write happens up front
rather than relying on the handler.

**Knock-on:** an unanswered turn is real history the UI should show, but an
assistant message with empty content is a hard 400 on Anthropic and silently
degrades the others. `conversation_messages()` now filters those at the
single point where exchanges become prompt messages.

### b. `present_file` embeds never persisted

`present_file` was the **only** media-producing tool with no entry in
`ai_client._extras_from_runs` — screenshots, downloads, organize_json and
dev_agent all had one. Its card therefore existed only in the live
`JARVIS_MEDIA` stream and vanished on reload. The handler wasn't even
returning `job_id`/`download_filename`, so there was nothing to persist if
the entry had existed.

Fixed on both sides. The extra's field names match `showAskPresentFile(info)`
exactly, so replay hands data to the **same renderer** the live path uses
rather than a near-copy that can drift. A `TOOL_RESULT_SPECS` entry drops the
two replay-only fields from the model's view at medium/low verbosity —
`extras` is built from the full pre-shaping result, so replay keeps them.

### c. Work done before an abort was discarded

A turn aborted halfway may already have taken a screenshot or written a file.
That happened whether or not a reply arrived, so `abandon_exchange()` accepts
and stores `extras` too.

---

## Tests

New standalone suites, same convention as the rest of `tests/` (no pytest,
run individually from `jarvis-cli/`):

| file | tests |
|---|---|
| `test_timespec.py` | 50 |
| `test_scheduler.py` | 62 |
| `test_conversation_persistence.py` | 34 |
| `test_mcp_client.py` | 33 |
| `test_conv_search.py` | 30 |
| | **209** |

`test_mcp_client.py` talks to a **real** stdio MCP server over a real pipe —
the fake server is written to a temp file and spawned as a subprocess, and it
deliberately misbehaves (non-JSON banner, 50 stderr lines, a stray
notification frame). Mocking the transport would defeat the purpose: every
bug this client is likely to have lives in the transport.

Pre-existing failures in `test_dev_agent_events.py`, `test_enhancements.py`
and part of `test_provider_override.py` (6/10) were verified against a
pristine extraction of the original archive and are unchanged.

---

## Files

**New**

```
jarvis-cli/jarvis/timespec.py                    jarvis-cli/jarvis/scheduler.py
jarvis-cli/jarvis/notifier.py                    jarvis-cli/jarvis/conv_search.py
jarvis-cli/jarvis/mcp_client.py
jarvis-cli/jarvis/actions/scheduler_tools.py     jarvis-cli/jarvis/actions/mcp_tools.py
jarvis-cli/jarvis/actions/conv_search_tools.py
tests/test_timespec.py       tests/test_scheduler.py    tests/test_conv_search.py
tests/test_mcp_client.py     tests/test_conversation_persistence.py
```

**Modified**

```
jarvis-cli/jarvis/cli.py                  signal handler, notification drain, 22 subcommands
jarvis-cli/jarvis/ai_client.py            begin/complete/abandon, present_file extras
jarvis-cli/jarvis/conversations.py        the three-step persistence primitives
jarvis-cli/jarvis/present_tools.py        return job_id/download_filename
jarvis-cli/jarvis/tool_result_shaping.py  present_file spec
web/server.js                             tick loop, REST routes, WS push
web/public/app.js                         notifications, presentFile replay, Scheduled panel
web/public/index.html, style.css          panel markup and styling
```

## Config files created on first use

```
~/.jarvis/scheduled.json        jobs + event log
~/.jarvis/notifications.json    the durable inbox
~/.jarvis/notify_config.json    per-kind default channels, voice toggle
~/.jarvis/mcp_config.json       MCP servers (edit by hand to add one)
~/.jarvis/mcp_cache.json        cached tool lists
```
