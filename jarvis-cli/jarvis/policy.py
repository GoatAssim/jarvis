"""Capability policy — risk scoring, rules as data, dry-run.

WHAT WAS WRONG WITH THE BOOLEAN GATE
------------------------------------
tool_safety.json answers one question: does this tool name need
confirmation? That is a good answer to the wrong question, because the name
is the least informative part of a call. All of these are `write_file`:

    write_file("~/notes/todo.md", "...")              harmless
    write_file("~/.ssh/authorized_keys", "...")       catastrophic
    write_file("C:/Windows/System32/drivers/etc/hosts", "...")  bad

and all of these are `run_custom_command`:

    run_custom_command("git status")                  read-only
    run_custom_command("rm -rf ~/projects")           irreversible

A per-name boolean has to pick one answer for all of them, so it picks
"confirm" and the user learns to click through. Worse, the same boolean
applies whether a human is watching or not: a scheduled job at 3am gaining
`write_on_screen` access to whatever window happens to be focused is a
*categorically* different risk from the same call in an interactive session,
and the current model cannot tell them apart.

THE MODEL HERE
--------------
Every call is scored from three independent inputs:

    tool      what it can do at all         (base risk, reversibility)
    arguments what it's about to do to what (paths, scope, force flags)
    context   who can see it happening      (interactive / scheduled /
                                             chat channel / unattended)

The score maps to a decision: allow, confirm, review (a second AI looks
first), or deny. Rules can override any of it.

RULES ARE DATA
--------------
~/.jarvis/policy.json is a list of readable rules, evaluated in order, first
match wins:

    {"when": {"tool_group": "files", "path_outside": "~/Documents"},
     "then": "confirm", "because": "writes outside my documents folder"}

    {"when": {"context": "scheduled", "tool": "write_on_screen"},
     "then": "deny", "because": "nothing unattended should type into whatever
                                 window happens to be focused"}

    {"when": {"source": "mcp", "trusted": false},
     "then": "review", "because": "tools from servers I didn't write"}

The point of data over code is that a policy you cannot read is a policy you
cannot audit, and a policy you cannot edit without a rebuild is one you will
not edit.

FAIL CLOSED, AND SAY WHY
------------------------
A malformed rules file does not disable the policy — it falls back to the
built-in defaults and reports the parse error, because a typo silently
turning off every guard is the worst possible failure for a security
component. Every decision carries a `because` string, so a confirmation
prompt can say *"writes outside ~/Documents"* rather than *"this tool is
flagged"*.

RELATIONSHIP TO tool_safety.py
------------------------------
This does not replace it and does not weaken it. tool_safety's
confirm_required is still checked, and a tool flagged there is still gated
no matter what this says — policy can only ever escalate, never downgrade
below that floor. That ordering is deliberate: a policy bug should be able
to make Jarvis more cautious, never less.
"""

import fnmatch
import json
import os
import re
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
POLICY_FILE = JARVIS_DIR / "policy.json"
ENCODING = "utf-8"

# Decisions, least to most restrictive. Order matters: escalate() takes the
# max, which is what enforces "policy can only tighten".
ALLOW, CONFIRM, REVIEW, DENY = "allow", "confirm", "review", "deny"
_RANK = {ALLOW: 0, CONFIRM: 1, REVIEW: 2, DENY: 3}

# Execution contexts. Not cosmetic — see the module docstring on why the
# same call is a different risk in each.
CTX_INTERACTIVE = "interactive"   # a human is at the CLI or web UI
CTX_SCHEDULED = "scheduled"       # a scheduler tick; nobody is watching
CTX_CHAT = "chat"                 # Discord/Instagram; a remote human
CTX_UNATTENDED = "unattended"     # daemon, startup task

# Score thresholds.
THRESHOLD_CONFIRM = 40
THRESHOLD_REVIEW = 65
THRESHOLD_DENY = 90

# Base risk per tool. Anything unlisted scores by category (see _base_risk),
# so a new or auto-discovered tool is never silently treated as harmless.
_TOOL_RISK = {
    "run_custom_command": 70, "run_command": 45, "run_chain": 50,
    "write_file": 55, "dev_agent": 65, "code_agent": 60, "edit_file": 55,
    "run_shell": 70,
    "package_install": 60, "package_uninstall": 65,
    "git_run": 45, "git_commit_all": 30, "git_push": 45,
    "type_text": 40, "press_key": 35, "hotkey": 40, "click": 35,
    "click_on_text": 35, "drag": 35, "write_on_screen": 50,
    "reveal_in_explorer": 10, "open_file": 20, "present_file": 5,
    "memory_forget": 30, "memory_save": 10,
    "schedule_task": 25, "cancel_scheduled": 20,
    "notify_owner": 15, "send_digest": 5,
    "spotify_play": 5, "playnite_launch_game": 20,
    "radio_set": 30, "set_capacity_mode": 10,
}

# Context multipliers. Unattended execution roughly doubles the risk of
# anything that touches the desktop or the shell, because the failure has no
# witness and no chance of being interrupted.
_CONTEXT_FACTOR = {
    CTX_INTERACTIVE: 1.0,
    CTX_CHAT: 1.35,
    CTX_SCHEDULED: 1.6,
    CTX_UNATTENDED: 1.8,
}

# Argument patterns that raise the score, with why. The `because` text ends
# up in front of the user, so it is written for them, not for a log.
_DANGEROUS_ARG_PATTERNS = [
    (re.compile(r"\brm\s+-[rf]{1,2}\b|\bdel\s+/[sqf]\b|Remove-Item.*-Recurse", re.I),
     45, "deletes a whole directory tree"),
    (re.compile(r"\b(format|mkfs|diskpart)\b", re.I), 60, "formats a disk"),
    (re.compile(r":\(\)\{.*\};:|\bdd\s+if=", re.I), 60, "is a known destructive pattern"),
    (re.compile(r"\b(shutdown|reboot|halt)\b", re.I), 30, "shuts the machine down"),
    (re.compile(r"\bcurl\b.*\|\s*(ba)?sh|\bwget\b.*\|\s*(ba)?sh|iwr.*\|.*iex", re.I),
     55, "pipes something off the internet straight into a shell"),
    (re.compile(r"\b(sudo|runas|Start-Process.*-Verb\s+RunAs)\b", re.I),
     35, "asks for administrator rights"),
    (re.compile(r"--force\b|-f\b(?!\w)|--hard\b", re.I), 20, "uses a force flag"),
    (re.compile(r"\b(git\s+push\s+.*--force|git\s+reset\s+--hard)\b", re.I),
     30, "rewrites git history"),
    (re.compile(r"\bchmod\s+777\b|icacls.*\/grant\s+everyone", re.I),
     30, "opens permissions to everyone"),
]

# Commands that only READ. A policy that flags `git status` teaches people to
# click through every prompt, at which point it protects nothing — crying
# wolf is a security failure, not a conservative default. So a command whose
# entire pipeline is recognisably read-only has its base risk cut rather than
# merely not raised.
#
# Anchored and whole-pipeline: every segment between |, && and ; must match,
# so "git status && rm -rf /" does NOT qualify. That check is the reason this
# is a reduction and not an allowlist — a partial match must buy nothing.
_READONLY_COMMAND_RE = re.compile(
    r"^\s*(git\s+(status|log|diff|show|branch|remote|config\s+--get|rev-parse|describe)"
    r"|ls|dir|pwd|cd|cat|type|head|tail|less|more|wc|find|where|which|whoami|hostname"
    r"|echo|date|uptime|df|du|free|ps|top|env|printenv|node\s+-v|python\s+--version"
    r"|pip\s+(list|show|freeze)|npm\s+(ls|list|view)|docker\s+ps|systemctl\s+status"
    r"|grep|rg|ag|sort|uniq|awk|sed\s+-n|jq|curl\s+-s?I)\b",
    re.I)

_PIPE_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|\||;|\n)\s*")


def _is_readonly_command(text):
    """Every segment of the pipeline is a known read-only command."""
    text = (text or "").strip()
    if not text:
        return False
    segments = [seg for seg in _PIPE_SPLIT_RE.split(text) if seg.strip()]
    if not segments:
        return False
    return all(_READONLY_COMMAND_RE.match(seg) for seg in segments)


# Paths nothing should be writing to without an explicit yes, regardless of
# which tool is asking.
_SENSITIVE_PATHS = [
    ("~/.ssh", 55, "your SSH keys"),
    ("~/.aws", 50, "your AWS credentials"),
    ("~/.jarvis/ai_config.json", 45, "Jarvis's own API keys"),
    ("~/.jarvis/tool_safety.json", 60, "Jarvis's own safety settings"),
    ("~/.jarvis/policy.json", 60, "this policy file itself"),
    ("~/.gnupg", 55, "your GPG keys"),
    ("/etc", 45, "system configuration"),
    ("C:/Windows", 50, "the Windows directory"),
    ("C:/Program Files", 40, "installed programs"),
]

DEFAULT_POLICY = {
    "enabled": True,
    # Where mutating filesystem work is expected to happen. Anything outside
    # is not forbidden, it's just not assumed.
    "safe_roots": ["~/Documents", "~/Downloads", "~/Desktop", "~/projects", "~/code"],
    "rules": [
        {
            "when": {"context": "scheduled", "tool_group": "desktop"},
            "then": "deny",
            "because": "an unattended job typing or clicking into whatever window "
                       "happens to be focused can do anything, to anything",
        },
        {
            "when": {"context": "chat", "tool_group": ["desktop", "system_control", "files"]},
            "then": "review",
            "because": "a request arriving over a chat channel is asking this PC "
                       "to do something on someone else's say-so",
        },
        {
            "when": {"source": "mcp", "trusted": False},
            "then": "review",
            "because": "this tool comes from an MCP server, not from Jarvis itself",
        },
        {
            "when": {"tool": "run_custom_command", "context": ["scheduled", "unattended"]},
            "then": "deny",
            "because": "arbitrary shell with nobody watching",
        },
    ],
    # Log every decision to ~/.jarvis/policy_log.jsonl. Off by default (it's
    # a privacy consideration: it records arguments), on when you're
    # debugging why something was blocked.
    "audit": False,
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_policy():
    """Policy from disk merged over defaults. Returns (policy, problems)."""
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    problems = []
    try:
        raw = json.loads(POLICY_FILE.read_text(encoding=ENCODING))
    except FileNotFoundError:
        return policy, problems
    except (ValueError, UnicodeDecodeError) as exc:
        # The critical branch. A broken policy file must NOT mean "no
        # policy" — that would turn a typo into a silent removal of every
        # guard. Defaults stay in force and the problem is reported.
        problems.append("policy.json didn't parse (%s) — built-in defaults are in "
                        "force until it's fixed" % exc)
        return policy, problems
    except OSError as exc:
        problems.append("couldn't read policy.json: %s" % exc)
        return policy, problems

    if not isinstance(raw, dict):
        problems.append("policy.json must be an object — defaults in force")
        return policy, problems

    for key in ("enabled", "audit"):
        if key in raw:
            policy[key] = bool(raw[key])
    if isinstance(raw.get("safe_roots"), list):
        policy["safe_roots"] = [str(r) for r in raw["safe_roots"]]

    if "rules" in raw:
        if not isinstance(raw["rules"], list):
            problems.append('"rules" must be a list — using the built-in rules')
        else:
            valid, bad = [], 0
            for i, rule in enumerate(raw["rules"]):
                if _rule_is_valid(rule):
                    valid.append(rule)
                else:
                    bad += 1
                    problems.append("rule %d is malformed and was skipped" % (i + 1))
            # A user's rules REPLACE the defaults when any are given —
            # otherwise there'd be no way to remove a default rule, and
            # "why does it still block that?" with no visible cause is
            # exactly the unreadable-policy problem this file exists to fix.
            policy["rules"] = valid
            if bad and not valid:
                problems.append("every rule was malformed — built-in rules restored")
                policy["rules"] = DEFAULT_POLICY["rules"]
    return policy, problems


def _rule_is_valid(rule):
    if not isinstance(rule, dict):
        return False
    if not isinstance(rule.get("when"), dict):
        return False
    return rule.get("then") in (ALLOW, CONFIRM, REVIEW, DENY)


def ensure_policy():
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        if not POLICY_FILE.exists():
            from . import atomic_io
            atomic_io.write_json(POLICY_FILE, DEFAULT_POLICY)
    except (OSError, ImportError):
        pass
    return POLICY_FILE


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _base_risk(tool_name):
    if tool_name in _TOOL_RISK:
        return _TOOL_RISK[tool_name]
    name = tool_name or ""
    # Unknown tools score by shape, never zero. An auto-discovered tool
    # nobody has classified should not be the cheapest thing in the catalog.
    if name.startswith(("get_", "list_", "search_", "read_", "check_")):
        return 5
    if any(k in name for k in ("delete", "remove", "uninstall", "kill", "wipe",
                               "reset", "destroy", "erase", "purge", "obliterate",
                               "drop", "truncate", "format", "shred", "revoke")):
        return 60
    if any(k in name for k in ("write", "edit", "create", "install", "launch", "run", "exec")):
        return 45
    if any(k in name for k in ("send", "post", "publish", "notify", "message")):
        return 30
    return 25


def _expand(path):
    try:
        return os.path.abspath(os.path.expanduser(str(path)))
    except Exception:  # noqa: BLE001
        return str(path)


def _paths_in(arguments):
    """Anything in the arguments that looks like a filesystem path."""
    found = []
    for key, value in (arguments or {}).items():
        if not isinstance(value, str) or len(value) > 500:
            continue
        if key in ("path", "file", "filename", "target", "folder", "directory",
                   "dest", "destination", "src", "source", "output"):
            found.append(value)
        elif re.search(r"(^|[\s\"'])([a-zA-Z]:[\\/]|[~/]|\.{1,2}/)", value):
            for match in re.findall(r"[a-zA-Z]:[\\/][^\s\"';|]+|[~/][^\s\"';|]+", value):
                found.append(match)
    return found


def _outside_safe_roots(path, safe_roots):
    target = _expand(path)
    for root in safe_roots or []:
        root_abs = _expand(root)
        if target == root_abs or target.startswith(root_abs + os.sep):
            return False
    return True


def score_call(tool_name, arguments, context=CTX_INTERACTIVE, policy=None,
               source="builtin"):
    """Risk score plus the reasons that produced it.

    Returning the reasons is as important as the number: a score with no
    explanation is a magic constant nobody can argue with, and the reasons
    are what a confirmation prompt shows the user.
    """
    policy = policy or load_policy()[0]
    arguments = arguments or {}
    reasons = []

    score = _base_risk(tool_name)
    reasons.append(("tool", score, "%s is a %s operation" % (
        tool_name, "read-only" if score < 15 else "high-impact" if score >= 55 else "mutating")))

    blob = " ".join(str(v) for v in arguments.values() if isinstance(v, (str, int, float)))

    command = arguments.get("command") or arguments.get("cmd") or ""
    if command and _is_readonly_command(command):
        # Checked BEFORE the dangerous-pattern scan, and it only lowers the
        # base — the patterns below can still raise it back up, so a command
        # that looks read-only but smuggles something in is not let through
        # by this branch.
        reduction = min(score - 5, 55)
        if reduction > 0:
            score -= reduction
            reasons[0] = ("tool", score, "%s, but this command only reads" % tool_name)

    for pattern, weight, why in _DANGEROUS_ARG_PATTERNS:
        if pattern.search(blob):
            score += weight
            reasons.append(("argument", weight, "it " + why))

    safe_roots = policy.get("safe_roots") or []
    for path in _paths_in(arguments):
        expanded = _expand(path)
        for sensitive, weight, label in _SENSITIVE_PATHS:
            sens = _expand(sensitive)
            if expanded == sens or expanded.startswith(sens + os.sep) \
                    or fnmatch.fnmatch(expanded.replace("\\", "/"),
                                       sens.replace("\\", "/") + "*"):
                score += weight
                reasons.append(("path", weight, "it touches %s" % label))
                break
        else:
            if _base_risk(tool_name) >= 40 and _outside_safe_roots(path, safe_roots):
                score += 15
                reasons.append(("path", 15,
                                "it writes outside your usual folders (%s)" % path))

    factor = _CONTEXT_FACTOR.get(context, 1.0)
    if factor != 1.0:
        before = score
        score = int(score * factor)
        reasons.append(("context", score - before,
                        "it's running %s, where nobody can stop it mid-way" % (
                            "on a schedule" if context == CTX_SCHEDULED
                            else "unattended" if context == CTX_UNATTENDED
                            else "from a chat message")))

    if source == "mcp":
        score += 15
        reasons.append(("source", 15, "it comes from an external MCP server"))

    return max(0, min(100, int(score))), reasons


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _matches(condition, tool_name, arguments, context, source, trusted, group):
    """Does one rule's `when` block match this call? All keys must match."""
    def as_list(value):
        return value if isinstance(value, list) else [value]

    for key, expected in (condition or {}).items():
        if key == "tool":
            if tool_name not in as_list(expected):
                return False
        elif key == "tool_group":
            if group not in as_list(expected):
                return False
        elif key == "context":
            if context not in as_list(expected):
                return False
        elif key == "source":
            if source not in as_list(expected):
                return False
        elif key == "trusted":
            if bool(trusted) != bool(expected):
                return False
        elif key == "tool_matches":
            if not any(fnmatch.fnmatch(tool_name or "", pat) for pat in as_list(expected)):
                return False
        elif key == "arg_matches":
            blob = " ".join(str(v) for v in (arguments or {}).values())
            if not any(re.search(pat, blob, re.I) for pat in as_list(expected)):
                return False
        elif key == "path_outside":
            roots = as_list(expected)
            paths = _paths_in(arguments)
            if not paths or not any(_outside_safe_roots(p, roots) for p in paths):
                return False
        elif key == "path_under":
            roots = [_expand(r) for r in as_list(expected)]
            paths = [_expand(p) for p in _paths_in(arguments)]
            if not any(p == r or p.startswith(r + os.sep) for p in paths for r in roots):
                return False
        elif key == "min_score":
            pass  # handled by the caller, which has the score
        else:
            return False  # unknown key: never match, rather than match everything
    return True


def escalate(current, proposed):
    """Take the stricter of two decisions. Policy tightens, never loosens."""
    return current if _RANK.get(current, 0) >= _RANK.get(proposed, 0) else proposed


def decide(tool_name, arguments=None, context=CTX_INTERACTIVE, source="builtin",
           trusted=True, policy=None, baseline=None):
    """The whole decision for one call.

    `baseline` is what tool_safety.py already decided (CONFIRM when the tool
    is flagged). The result can only ever be that or stricter — see the
    module docstring on why policy is allowed to tighten and not loosen.
    """
    policy, problems = (policy, []) if policy else load_policy()
    group = _group_of(tool_name)
    score, reasons = score_call(tool_name, arguments, context, policy, source)

    if not policy.get("enabled", True):
        return {"decision": baseline or ALLOW, "score": score, "reasons": reasons,
                "because": "policy is disabled", "matched_rule": None,
                "problems": problems}

    if score >= THRESHOLD_DENY:
        decision, because = DENY, "the risk score is %d/100" % score
    elif score >= THRESHOLD_REVIEW:
        decision, because = REVIEW, "the risk score is %d/100" % score
    elif score >= THRESHOLD_CONFIRM:
        decision, because = CONFIRM, "the risk score is %d/100" % score
    else:
        decision, because = ALLOW, "nothing about this looked risky"

    matched = None
    for rule in policy.get("rules") or []:
        condition = rule.get("when") or {}
        min_score = condition.get("min_score")
        if min_score is not None and score < min_score:
            continue
        if _matches(condition, tool_name, arguments, context, source, trusted, group):
            matched = rule
            decision = rule.get("then", decision)
            because = rule.get("because") or because
            break  # first match wins — the order in the file is the priority

    if baseline:
        decision = escalate(decision, baseline)

    result = {
        "decision": decision, "score": score, "reasons": reasons,
        "because": because, "matched_rule": (matched or {}).get("because"),
        "context": context, "group": group, "problems": problems,
    }
    if policy.get("audit"):
        _audit(tool_name, arguments, result)
    return result


def _group_of(tool_name):
    try:
        from . import tool_registry
        return tool_registry.group_of(tool_name) or "misc"
    except Exception:  # noqa: BLE001 — during partial init
        return "misc"


def _audit(tool_name, arguments, result):
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        line = json.dumps({
            "at": datetime.now().replace(microsecond=0).isoformat(),
            "tool": tool_name, "args": arguments,
            "decision": result["decision"], "score": result["score"],
            "because": result["because"],
        }, default=str)
        with (JARVIS_DIR / "policy_log.jsonl").open("a", encoding=ENCODING) as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def explain(result):
    """A sentence a person can act on, for a confirmation prompt."""
    if not isinstance(result, dict):
        return ""
    decision = result.get("decision")
    lead = {
        ALLOW: "Allowed",
        CONFIRM: "Needs your OK",
        REVIEW: "Needs a second opinion first",
        DENY: "Blocked",
    }.get(decision, decision)
    because = result.get("matched_rule") or result.get("because") or ""
    bits = [r[2] for r in (result.get("reasons") or []) if r[1] >= 15][:3]
    detail = ("; ".join(bits)) if bits else ""
    out = "%s — %s" % (lead, because)
    if detail and detail not in because:
        out += " (%s)" % detail
    return out


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def dry_run(tool_name, arguments=None, context=CTX_INTERACTIVE):
    """What WOULD happen, without doing it.

    Valuable mainly for scheduled jobs: "show me what the 3am cleanup will
    actually do" is otherwise unanswerable until 3am, and by then it's done
    it. Describes the effect where the tool's own shape makes that knowable,
    and says so plainly where it doesn't — an invented description would be
    worse than none.
    """
    verdict = decide(tool_name, arguments, context=context)
    effect = _describe_effect(tool_name, arguments)
    return {
        "tool": tool_name,
        "arguments": arguments or {},
        "context": context,
        "would": effect["summary"],
        "reversible": effect["reversible"],
        "targets": effect["targets"],
        "decision": verdict["decision"],
        "score": verdict["score"],
        "explanation": explain(verdict),
        "note": "Nothing was executed.",
    }


def _describe_effect(tool_name, arguments):
    arguments = arguments or {}
    paths = _paths_in(arguments)
    name = tool_name or ""

    if name in ("run_custom_command", "run_shell"):
        return {"summary": "run this shell command: %s" % (arguments.get("command") or "?"),
                "reversible": False, "targets": paths}
    if name in ("write_file", "edit_file"):
        body = arguments.get("content") or arguments.get("text") or ""
        return {"summary": "write %d characters to %s"
                           % (len(str(body)), arguments.get("path") or "?"),
                "reversible": False, "targets": paths}
    if name in ("package_install", "package_uninstall"):
        return {"summary": "%s the package %s"
                           % (name.split("_")[1], arguments.get("package") or "?"),
                "reversible": True, "targets": []}
    if name in ("type_text", "write_on_screen"):
        return {"summary": "type %r into whatever window is focused"
                           % (arguments.get("text") or "")[:60],
                "reversible": False, "targets": ["the focused window"]}
    if name.startswith(("get_", "list_", "search_", "read_")):
        return {"summary": "read something and report back; change nothing",
                "reversible": True, "targets": paths}
    return {"summary": "run %s with %s"
                       % (name, json.dumps(arguments, default=str)[:120]),
            "reversible": None, "targets": paths}


def context_from_env():
    """Infer the execution context. Conservative when unsure.

    Unsure resolves to `unattended`, not `interactive` — guessing that a
    human is watching when none is is the mistake with consequences.
    """
    if os.environ.get("JARVIS_CONTEXT") in (CTX_INTERACTIVE, CTX_SCHEDULED,
                                            CTX_CHAT, CTX_UNATTENDED):
        return os.environ["JARVIS_CONTEXT"]
    if os.environ.get("JARVIS_SCHEDULED"):
        return CTX_SCHEDULED
    if os.environ.get("JARVIS_CHANNEL"):
        return CTX_CHAT
    if (os.environ.get("JARVIS_UI") or "").lower() == "web":
        return CTX_INTERACTIVE
    try:
        import sys
        if sys.stdin.isatty():
            return CTX_INTERACTIVE
    except Exception:  # noqa: BLE001
        pass
    return CTX_UNATTENDED
