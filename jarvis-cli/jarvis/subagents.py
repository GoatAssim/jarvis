"""Specialized subagents with their own API keys.

WHAT THIS IS
------------
Today every tool call is Jarvis doing the work itself, serially, in one
context window. This module makes Jarvis a dispatcher: given a large task,
it hands scoped pieces to specialized child processes, each with its own
budget, its own system prompt, its own tool allowance, and — the part that
actually required new plumbing — **its own API keys**.

Nothing here is a new execution model. A subagent is a `tasks.Task` with an
`agent` field, advanced by the same `task_runner` loop as any other task.
That reuse is the point: subagents inherit checkpointing, crash recovery,
budgets and leases for free, and a subagent that dies mid-step resumes the
same way its parent would.

KEY ISOLATION IS THE HARD REQUIREMENT
-------------------------------------
A subagent must never spend the main Jarvis key, and must never spend
another subagent's key. Rate-limiting one role must not take out the
assistant you're talking to.

That is enforced by *construction*, not by convention. `key_env()` builds a
JSON blob naming exactly one provider and exactly that role's key list, and
`ai_client._eligible_providers()` — the single chokepoint where "which
providers and keys may this process use" is decided — replaces its entire
provider list with that blob when it's present. A subagent process therefore
has no path to the main key: it isn't merely deprioritized, it isn't in the
list at all. Failover inside a subagent walks that role's keys in order and
stops at the end of them.

POOL SIZE
---------
`max_concurrent` caps how many subagent tasks are runnable at once. Without
it, "decompose this into eight pieces" means eight simultaneous model calls
on eight key pools, which is both a spend spike and an excellent way to get
every key rate-limited at the same moment.

ESCALATION
----------
`aggregate()` collects finished children and decides whether the parent can
proceed alone. Disagreement between children, or any child ending blocked,
is surfaced rather than averaged away — the handoff's "only escalate to the
human when subagents disagree or something needs a decision" is a real
design constraint and silently picking a winner would defeat it.
"""

import json
import os
import time
from pathlib import Path

from . import atomic_io, tasks

CONFIG_FILE = Path.home() / ".jarvis" / "subagents.json"
_SPAWN_LOCK_FILE = Path.home() / ".jarvis" / "subagents.spawn.lock"

# The env var a spawned subagent process reads to find out which keys it is
# allowed to use. Consumed in ai_client._eligible_providers().
KEY_ENV = "JARVIS_SUBAGENT_KEYS"
ROLE_ENV = "JARVIS_SUBAGENT_ROLE"

MAX_CONCURRENT_DEFAULT = 3
MAX_AGENTS = 24


# The three roles the handoff names, shipped as defaults so the feature does
# something useful before anyone writes a config. Each is a system-prompt
# fragment plus a budget, not new code — "a coding subagent" is a prompt and
# a tool allowance, and pretending otherwise is how you end up with three
# near-identical execution paths to maintain.
BUILTIN_AGENTS = {
    "coder": {
        "description": "Writes and edits code, runs builds and tests, iterates until they pass.",
        "prompt": (
            "You are a coding subagent. You have one scoped engineering task. "
            "Read before you write, make the smallest change that works, and "
            "run the tests or build after every edit. Report what you changed "
            "and what the build/test output was — never claim something passes "
            "without having actually run it."
        ),
        "tools": ["dev_agent", "code_agent", "read_file", "edit_file", "list_dir",
                  "search_code", "run_shell", "write_file", "git_run"],
        "max_steps": 20,
        "think": "medium",
    },
    "research": {
        "description": "Searches and reads sources, produces a structured report. Changes nothing.",
        "prompt": (
            "You are a research subagent. Gather information and report it. "
            "You do not change anything on this machine — no file writes, no "
            "commands, no settings. Search, fetch 2-5 real sources, and "
            "produce a structured summary with the source URLs. Say plainly "
            "when the sources disagree or when you could not find something, "
            "rather than filling the gap with a guess."
        ),
        "tools": ["web_search", "web_fetch", "search_conversations"],
        "max_steps": 12,
        "think": "low",
    },
    "monitor": {
        "description": "Watches for a condition and reports when it changes. The grown-up form of schedule_watch.",
        "prompt": (
            "You are a monitoring subagent. Check the condition you were "
            "given, report its current state in one line, and say clearly "
            "whether it changed since the last check. Do not take corrective "
            "action unless you were explicitly told to — report, don't fix."
        ),
        "tools": ["web_search", "web_fetch", "read_file", "list_dir", "run_shell"],
        "max_steps": 8,
        "think": "off",
    },
    # Generic debate roles. Not specific to any one skill — "advocate" and
    # "skeptic" are useful any time a decision benefits from two people
    # arguing opposite sides rather than one person hedging. The `consult`
    # skill (see skills.py's ensure_builtin_skills()) is what actually spawns
    # exactly these two and synthesizes their disagreement; that pairing is
    # markdown, not Python, on purpose — see this module's docstring.
    "advocate": {
        "description": "Argues FOR a position as persuasively and honestly as the evidence allows.",
        "prompt": (
            "You are the advocate in a two-sided review. Argue FOR the "
            "position you were given. Make the strongest honest case: cite "
            "real reasons and, where useful, search for supporting evidence. "
            "Do not pretend uncertainty you don't have, but do not overstate "
            "the case either — an advocate who ignores real weaknesses is "
            "not useful to the person reading this. End with your verdict "
            "in one sentence, then your key supporting reasons as a short list."
        ),
        "tools": ["web_search", "web_fetch"],
        "max_steps": 6,
        "think": "medium",
    },
    "skeptic": {
        "description": "Argues AGAINST a position, actively looking for the weakest points.",
        "prompt": (
            "You are the skeptic in a two-sided review. Argue AGAINST the "
            "position you were given, or surface its strongest objections. "
            "Actively look for what could go wrong, what's unproven, or "
            "what a proponent would gloss over — search for counterevidence "
            "where useful. Do not manufacture objections that don't hold up; "
            "a skeptic who nitpicks in bad faith is not useful either. End "
            "with your verdict in one sentence, then your key objections as "
            "a short list."
        ),
        "tools": ["web_search", "web_fetch"],
        "max_steps": 6,
        "think": "medium",
    },
}

DEFAULT_CONFIG = {
    "max_concurrent": MAX_CONCURRENT_DEFAULT,
    # role -> {"provider": "<name from ai_config>", "api_keys": [...]}
    # Empty out of the box: there is no sane default for "a key that isn't
    # the main one", and inventing one by copying the main key would break
    # the isolation guarantee this module exists to provide.
    "key_pools": {},
    # role -> agent definition; merged over BUILTIN_AGENTS.
    "agents": {},
}


class SubagentError(Exception):
    """Bad configuration or bad input — shown to the user, not a bug."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config():
    """Never raises. A missing or corrupt config means "no key pools", which
    degrades to "subagents are unavailable" rather than to "subagents
    silently use the main key"."""
    cfg = dict(DEFAULT_CONFIG)
    cfg["key_pools"] = {}
    cfg["agents"] = {}
    try:
        raw = atomic_io.read_json(CONFIG_FILE, default=None)
    except Exception:
        raw = None
    if isinstance(raw, dict):
        try:
            cfg["max_concurrent"] = max(1, int(raw.get("max_concurrent")
                                               or MAX_CONCURRENT_DEFAULT))
        except (TypeError, ValueError):
            cfg["max_concurrent"] = MAX_CONCURRENT_DEFAULT
        if isinstance(raw.get("key_pools"), dict):
            cfg["key_pools"] = raw["key_pools"]
        if isinstance(raw.get("agents"), dict):
            cfg["agents"] = raw["agents"]
    return cfg


def save_config(cfg):
    return atomic_io.write_json(CONFIG_FILE, cfg)


def agents(cfg=None):
    """Every known agent definition: builtins with user overrides merged on
    top, plus any wholly user-defined ones."""
    cfg = cfg or load_config()
    out = {}
    for name, spec in BUILTIN_AGENTS.items():
        out[name] = dict(spec)
        out[name]["builtin"] = True
    for name, spec in (cfg.get("agents") or {}).items():
        name = str(name).strip().lower()
        if not name or len(out) >= MAX_AGENTS:
            continue
        if not isinstance(spec, dict):
            continue
        merged = dict(out.get(name) or {})
        merged.update(spec)
        merged["builtin"] = name in BUILTIN_AGENTS
        out[name] = merged
    return out


def get_agent(name, cfg=None):
    spec = agents(cfg).get(str(name or "").strip().lower())
    if not spec:
        raise SubagentError(
            "no subagent %r — known: %s" % (name, ", ".join(sorted(agents(cfg)))))
    return spec


def key_pool(role, cfg=None):
    """This role's dedicated (provider, keys), or None if it has none.

    Falls back to a pool literally named "default" — one shared spare pool
    for every role that hasn't been given its own is a reasonable setup, and
    it is still not the main key.
    """
    cfg = cfg or load_config()
    pools = cfg.get("key_pools") or {}
    # BUGFIX: `pools.get(role) or pools.get("default")` treated an
    # explicitly-empty {} entry the same as the role being absent, silently
    # falling back to the shared pool. `role in pools` distinguishes "not
    # configured" from "configured, but empty" — only the former falls back.
    entry = pools[role] if role in pools else pools.get("default")
    if not isinstance(entry, dict):
        return None
    provider = str(entry.get("provider") or "").strip()
    keys = [k for k in (entry.get("api_keys") or [])
            if isinstance(k, str) and k.strip()]
    if not provider or not keys:
        return None
    return {"provider": provider, "api_keys": keys}


def key_env(role, cfg=None):
    """The env fragment that pins a child process to this role's keys.

    Returns {} when the role has no pool — and callers treat that as a hard
    error rather than falling through, because a child spawned with no
    JARVIS_SUBAGENT_KEYS would inherit the normal config, i.e. the main key.
    That is the one failure mode this module must never have.
    """
    pool = key_pool(role, cfg)
    if not pool:
        return {}
    return {
        KEY_ENV: json.dumps(pool, separators=(",", ":")),
        ROLE_ENV: str(role),
    }


def describe_pools(cfg=None):
    """Config summary for `jarvis subagents`. Keys are never printed — only
    how many there are and how they end, enough to tell two pools apart
    without putting a live credential in a terminal or a log."""
    cfg = cfg or load_config()
    out = {}
    for role, entry in (cfg.get("key_pools") or {}).items():
        if not isinstance(entry, dict):
            continue
        keys = [k for k in (entry.get("api_keys") or []) if isinstance(k, str) and k.strip()]
        out[role] = {
            "provider": entry.get("provider"),
            "key_count": len(keys),
            "keys": ["\u2026" + k[-4:] for k in keys],
        }
    return out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------



# How old the lockfile itself must be before a waiter assumes its holder
# died mid-critical-section rather than being a legitimate (if slow) hold.
# Independent from any one caller's wait `timeout` on purpose — see the
# BUGFIX note in _acquire_spawn_lock for why conflating the two is wrong.
_SPAWN_LOCK_STALE_AFTER = 10.0


def _acquire_spawn_lock(timeout=5.0):
    """Best-effort mutual exclusion around the running_count()-then-create
    window in spawn(), below.

    BUGFIX (the race): that window used to be unprotected — two
    near-simultaneous spawns (an interactive ask and a daemon tick, say, or
    two tool calls in the same ask) could each call running_count(), each
    see one slot free, and both create a task, silently exceeding
    max_concurrent. That is exactly the "spend spike... every key
    rate-limited at the same moment" failure this module's docstring names
    as the reason the cap exists, so unlike tasks.claim()'s
    deliberately-accepted lease race (whose worst case is a harmlessly
    duplicated step), this one gets an actual lock.

    BUGFIX (this function, caught by its own test): the first version used
    the caller's own `timeout` argument to also decide when the lockfile
    counts as stale. A short-timeout caller (a quick poll) would then treat
    a lock that a NORMAL, still-in-progress spawn had held for only a few
    hundred milliseconds as abandoned, delete it, and barge in — reopening
    the exact race this function exists to close. Staleness is about how
    long the critical section could plausibly still be running (seconds, at
    most, for a file read and a small write), not about how long this one
    caller feels like waiting, so it's judged against a fixed constant
    instead of `timeout`.

    A plain O_CREAT|O_EXCL lockfile rather than fcntl/msvcrt: this project
    also ships audio_tools.py for Windows, so anything here has to work
    identically on POSIX and Windows with no extra dependency either way.
    """
    _SPAWN_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(_SPAWN_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(_SPAWN_LOCK_FILE) > _SPAWN_LOCK_STALE_AFTER:
                    os.remove(_SPAWN_LOCK_FILE)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(0.05)


def _release_spawn_lock():
    try:
        os.remove(_SPAWN_LOCK_FILE)
    except OSError:
        pass


def running_count(parent_id=None):
    """How many subagent tasks are currently active."""
    n = 0
    for task in tasks.all_tasks():
        if not task.get("agent"):
            continue
        if parent_id and task.get("parent_id") != parent_id:
            continue
        if task.get("status") in tasks.ACTIVE_STATUSES:
            n += 1
    return n


def spawn(role, goal, parent_id=None, notes=None, conv_id=None,
          max_steps=None, cfg=None):
    """Create one subagent task. Returns the task dict.

    The subagent is *not* run here — it's persisted as a runnable task and
    the supervisor picks it up. That's what makes a fan-out survive the
    parent process dying halfway through issuing it.

    FEATURE: `conv_id` now defaults to a freshly-minted, real conversation
    rather than staying unset. Every step of a task is its own `jarvis ask`
    subprocess (see task_runner.py's docstring), and task_runner only wires
    JARVIS_CONVERSATION_ID into that subprocess's env when task["conv_id"]
    is set — nothing before this ever set it, so every step of every
    subagent silently opened (and immediately orphaned) a brand-new
    anonymous conversation instead of accumulating one real transcript.
    tasks.py's own docstring for "history" anticipates exactly this fix
    ("summarizes outcomes... a transcript is what conversations.py already
    does better") — this is that connection actually made. Minting it here,
    at the single choke point every spawn path goes through, means the
    text, tool calls and thinking of every one of a subagent's steps land
    in the one place the rest of this app already knows how to display
    (GET /api/conversations/:id, the same call the Ask panel makes),
    instead of only the one-line step summaries tasks.record_step() keeps.
    """
    cfg = cfg or load_config()
    role = str(role or "").strip().lower()
    spec = get_agent(role, cfg)

    if not key_pool(role, cfg):
        raise SubagentError(
            "subagent %r has no API key pool. Subagents never use the main "
            "Jarvis key — add one with: jarvis subagent-keys %s <provider> <key> [key2 ...]"
            % (role, role))

    if not conv_id:
        from . import conversations
        conv_id = conversations.new_conversation(
            title="[%s] %s" % (role, goal[:160]),
            make_current=False, origin="subagent", origin_detail=role,
        )

    limit = int(cfg.get("max_concurrent") or MAX_CONCURRENT_DEFAULT)

    # The count check and the task creation below are one critical section —
    # see _acquire_spawn_lock's docstring for why this needs an actual lock
    # rather than the tasks.claim()-style accepted race.
    if not _acquire_spawn_lock():
        raise SubagentError("subagent spawn is busy right now — try again in a moment")
    try:
        if running_count() >= limit:
            raise SubagentError(
                "subagent pool is full (%d running, max_concurrent=%d)" % (running_count(), limit))

        combined_notes = spec.get("prompt") or ""
        if notes:
            combined_notes = (combined_notes + "\n\n" + str(notes)).strip()

        task = tasks.create(
            goal=goal,
            title="[%s] %s" % (role, goal),
            notes=combined_notes,
            conv_id=conv_id,
            parent_id=parent_id,
            agent=role,
            think=spec.get("think"),
            max_steps=max_steps or spec.get("max_steps"),
        )
        # Tool allowance rides JARVIS_ALLOWED_TOOLS, which the web UI already
        # uses — one mechanism for "restrict this ask's tools", not two.
        allowed = spec.get("tools")
        if allowed:
            task["allowed_tools"] = list(allowed)
            tasks.save(task)
        return task
    finally:
        _release_spawn_lock()


def children(parent_id):
    return [t for t in tasks.all_tasks(include_terminal=True)
            if t.get("parent_id") == parent_id]


def aggregate(parent_id):
    """Collect finished children and decide whether a human is needed.

    `escalate` is True when anything needs a decision: a child ended
    blocked, a child failed, or the children disagree. Everything else is
    reported as-is for the parent to fold into its own next step.
    """
    kids = children(parent_id)
    done, failed, blocked, pending = [], [], [], []
    for kid in kids:
        status = kid.get("status")
        entry = {
            "id": kid.get("id"),
            "agent": kid.get("agent"),
            "goal": kid.get("goal"),
            "status": status,
            "result": kid.get("result"),
            "error": kid.get("last_error"),
            "steps": (kid.get("budget") or {}).get("steps_used"),
        }
        if status == tasks.STATUS_DONE:
            done.append(entry)
        elif status == tasks.STATUS_BLOCKED:
            blocked.append(entry)
        elif status in (tasks.STATUS_FAILED, tasks.STATUS_CANCELLED):
            failed.append(entry)
        else:
            pending.append(entry)

    disagree = _disagreement(done)
    reasons = []
    if blocked:
        reasons.append("%d subagent(s) need a decision" % len(blocked))
    if failed:
        reasons.append("%d subagent(s) failed" % len(failed))
    if disagree:
        reasons.append("subagents disagree")

    return {
        "parent_id": parent_id,
        "total": len(kids),
        "done": done,
        "failed": failed,
        "blocked": blocked,
        "pending": pending,
        "all_finished": not pending,
        "escalate": bool(reasons),
        "escalation_reasons": reasons,
    }


# Words that flip a verdict. Crude on purpose: this is a *trigger for asking
# a human*, not a judgment. A false positive costs one question; a false
# negative means two subagents quietly contradicted each other and the
# parent picked one at random.
_NEGATIVE = ("no", "not", "cannot", "can't", "won't", "fail", "failed", "failing",
             "broken", "unsafe", "don't", "should not", "shouldn't", "incorrect",
             "wrong", "disagree", "unavailable", "missing")
_POSITIVE = ("yes", "works", "working", "passes", "passed", "safe", "correct",
             "succeeded", "success", "available", "confirmed", "fine")


def _disagreement(done_entries):
    """True when finished children point in opposite directions.

    Only meaningful when more than one child was asked something comparable,
    so a single child never "disagrees" with itself.
    """
    if len(done_entries) < 2:
        return False
    stances = set()
    for entry in done_entries:
        text = (str(entry.get("result") or "")).lower()
        if not text:
            continue
        neg = sum(1 for w in _NEGATIVE if _word_in(w, text))
        pos = sum(1 for w in _POSITIVE if _word_in(w, text))
        if neg > pos:
            stances.add("negative")
        elif pos > neg:
            stances.add("positive")
    return len(stances) > 1


def _word_in(word, text):
    """Word-boundary containment, the same trap tool_router.route() already
    fell into once ("commanded" matching "command") and fixed the same way."""
    import re
    return re.search(r"\b%s\b" % re.escape(word), text) is not None


def summary_for_parent(parent_id):
    """A compact text digest a parent step can be handed directly."""
    agg = aggregate(parent_id)
    lines = ["Subagent results (%d total):" % agg["total"]]
    for bucket in ("done", "blocked", "failed", "pending"):
        for entry in agg[bucket]:
            lines.append("  [%s] %s (%s): %s" % (
                entry["status"], entry["agent"], entry["id"],
                (entry.get("result") or entry.get("error") or "")[:400]))
    if agg["escalate"]:
        lines.append("")
        lines.append("NEEDS A HUMAN: " + "; ".join(agg["escalation_reasons"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Consumed by ai_client._eligible_providers()
# ---------------------------------------------------------------------------


def providers_from_env(all_providers):
    """If this process is a subagent, the ONLY providers it may use.

    Returns None when JARVIS_SUBAGENT_KEYS isn't set (the normal case — every
    ordinary ask), so the caller carries on unchanged.

    When it IS set, the returned list is a single synthetic provider: the
    named provider's own config (type, model, endpoint) with its `api_keys`
    replaced wholesale by the pool's. Replaced, not extended — extending
    would leave the main key reachable on failover, which is exactly the
    thing that must not happen.
    """
    raw = os.environ.get(KEY_ENV)
    if not raw:
        return None
    try:
        pool = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(pool, dict):
        return None
    name = str(pool.get("provider") or "").strip().lower()
    keys = [k for k in (pool.get("api_keys") or []) if isinstance(k, str) and k.strip()]
    if not name or not keys:
        # A malformed pool means "no usable providers", not "fall back to the
        # main key". An empty list makes ask() report "no providers
        # configured", which is loud and correct.
        return []

    for provider in all_providers or []:
        if not isinstance(provider, dict):
            continue
        if str(provider.get("name") or "").strip().lower() != name:
            continue
        clone = dict(provider)
        clone["api_keys"] = list(keys)
        clone.pop("api_key", None)   # the legacy singular field is a key too
        clone["enabled"] = True
        return [clone]
    return []
