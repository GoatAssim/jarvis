"""A backlog Jarvis can read and write. Deliberately not a scheduler.

WHY THIS ISN'T scheduler.py OR tasks.py
---------------------------------------
Those two both answer "what should happen at a particular moment":
scheduler.py fires a job when its trigger comes due, tasks.py drives a
long-running job to completion. Both are about *time* or *execution*.

None of that fits "I should really rewrite the auth module at some point",
"blocked on Ana's review", or "what am I in the middle of?". That work has
no trigger, may never be executed by Jarvis at all, and the useful
question about it is what state it's in — not when it runs. Filing it in
the scheduler would mean either inventing a fake due date or accumulating
jobs that never fire, and both make the scheduler's own list useless.

So: a flat, boring store of items with a status, keyed by nothing clever.

STATES
------
    idea -> todo -> doing -> blocked -> done

`blocked` is a first-class state rather than a tag because "what's blocked
right now" is the single most valuable question this store answers, and a
blocked item carries a `blocked_on` note explaining what it's waiting for.
An item can go back and forth; there's no enforced transition graph,
because real work doesn't have one.

PROJECTS ARE JUST A FIELD
-------------------------
No project objects, no hierarchy, no separate registry to keep in sync. An
item has a `project` string; a project "exists" when something is in it and
stops existing when nothing is. Everything a hierarchy would buy here is
available from a group-by.

STORAGE
-------
    ~/.jarvis/backlog.json

One file, rewritten atomically. Unlike tasks.py's file-per-task this is
written at human speed (a few items a day, not a checkpoint per step), so
there is no read-modify-write race worth designing around.
"""

import secrets
import time
from pathlib import Path

from . import atomic_io

JARVIS_DIR = Path.home() / ".jarvis"
BACKLOG_FILE = JARVIS_DIR / "backlog.json"

IDEA = "idea"
TODO = "todo"
DOING = "doing"
BLOCKED = "blocked"
DONE = "done"
STATES = (IDEA, TODO, DOING, BLOCKED, DONE)
OPEN_STATES = (IDEA, TODO, DOING, BLOCKED)

PRIORITIES = ("low", "normal", "high")

MAX_ITEMS = 500
MAX_TITLE = 200
MAX_NOTE = 1000
# Finished work is kept so "what did I get done this month" works, but not
# forever — past this many, the oldest done items are dropped on write.
MAX_DONE_KEPT = 120

_STATE_ALIASES = {
    "backlog": TODO, "later": IDEA, "someday": IDEA, "maybe": IDEA,
    "next": TODO, "open": TODO, "pending": TODO,
    "in progress": DOING, "in-progress": DOING, "wip": DOING,
    "started": DOING, "active": DOING, "doing": DOING,
    "stuck": BLOCKED, "waiting": BLOCKED, "on hold": BLOCKED,
    "complete": DONE, "completed": DONE, "finished": DONE, "closed": DONE,
}


def normalize_state(value, default=TODO):
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in STATES:
        return text
    return _STATE_ALIASES.get(text, default)


def normalize_priority(value):
    text = str(value or "").strip().lower()
    if text in PRIORITIES:
        return text
    if text in ("urgent", "critical", "p0", "p1"):
        return "high"
    if text in ("minor", "p3", "nice to have"):
        return "low"
    return "normal"


def _clean(text, limit):
    return " ".join(str(text or "").split())[:limit].strip()


def _load():
    data = atomic_io.read_json(BACKLOG_FILE, default={}, expect=dict)
    items = data.get("items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _save(items):
    # Trim finished work first so the cap doesn't evict live items in
    # favour of things that are already done.
    done = [i for i in items if i.get("state") == DONE]
    if len(done) > MAX_DONE_KEPT:
        done.sort(key=lambda i: i.get("updated") or 0)
        drop = {id(i) for i in done[: len(done) - MAX_DONE_KEPT]}
        items = [i for i in items if id(i) not in drop]
    atomic_io.write_json(BACKLOG_FILE, {"items": items[-MAX_ITEMS:]})


def _new_id():
    return "b_" + secrets.token_hex(3)


def add(title, project="", state=TODO, priority="normal", note="", tags=None):
    title = _clean(title, MAX_TITLE)
    if len(title) < 2:
        return None, "give the item a title"
    now = time.time()
    item = {
        "id": _new_id(),
        "title": title,
        "project": _clean(project, 60).lower(),
        "state": normalize_state(state),
        "priority": normalize_priority(priority),
        "note": _clean(note, MAX_NOTE),
        "tags": [_clean(t, 24).lower() for t in (tags or [])][:6],
        "blocked_on": "",
        "created": now,
        "updated": now,
        "done_at": None,
    }
    items = _load()
    items.append(item)
    _save(items)
    return item, ""


def find(query):
    """Resolve an id, or the best title match. Returns (item, error).

    Matching a partial title matters more here than anywhere else in the
    codebase: the model is relaying what a person said out loud ("mark the
    auth thing as done"), and making it call a list tool first to look up
    an opaque id is a wasted round trip for something a substring match
    gets right nearly every time.
    """
    raw = str(query or "").strip().lower()
    if not raw:
        return None, "which item?"
    items = _load()
    for item in items:
        if item.get("id") == raw:
            return item, ""

    open_items = [i for i in items if i.get("state") != DONE]
    for pool in (open_items, items):
        exact = [i for i in pool if (i.get("title") or "").lower() == raw]
        if len(exact) == 1:
            return exact[0], ""
        partial = [i for i in pool if raw in (i.get("title") or "").lower()]
        if len(partial) == 1:
            return partial[0], ""
        if len(partial) > 1:
            names = ", ".join(f"{i['id']} ({i['title'][:40]})" for i in partial[:5])
            return None, f"'{query}' matches several items — {names}"
    return None, f"nothing in the backlog matches '{query}'"


def update(query, **fields):
    item, err = find(query)
    if item is None:
        return None, err
    items = _load()
    target = next((i for i in items if i.get("id") == item["id"]), None)
    if target is None:
        return None, "item disappeared"

    for key, value in fields.items():
        if value is None:
            continue
        if key == "state":
            new_state = normalize_state(value, default=target.get("state") or TODO)
            target["state"] = new_state
            # done_at is set on the transition, not on every save, so
            # re-saving a done item doesn't keep moving its completion
            # date forward.
            if new_state == DONE and not target.get("done_at"):
                target["done_at"] = time.time()
            if new_state != DONE:
                target["done_at"] = None
            # Leaving a stale blocked_on behind on an unblocked item is how
            # "what's blocked" ends up lying to you.
            if new_state != BLOCKED:
                target["blocked_on"] = ""
        elif key == "priority":
            target["priority"] = normalize_priority(value)
        elif key == "title":
            target["title"] = _clean(value, MAX_TITLE) or target["title"]
        elif key == "project":
            target["project"] = _clean(value, 60).lower()
        elif key in ("note", "blocked_on"):
            target[key] = _clean(value, MAX_NOTE)
        elif key == "tags":
            target["tags"] = [_clean(t, 24).lower() for t in (value or [])][:6]
    # A blocked_on without the blocked state is the obvious thing to type
    # and clearly means "this is blocked", so honour the intent.
    if fields.get("blocked_on") and target["state"] != BLOCKED:
        target["state"] = BLOCKED
    target["updated"] = time.time()
    _save(items)
    return target, ""


def remove(query):
    item, err = find(query)
    if item is None:
        return None, err
    items = [i for i in _load() if i.get("id") != item["id"]]
    _save(items)
    return item, ""


def _sort_key(item):
    order = {"high": 0, "normal": 1, "low": 2}
    state_order = {DOING: 0, BLOCKED: 1, TODO: 2, IDEA: 3, DONE: 4}
    return (state_order.get(item.get("state"), 9),
            order.get(item.get("priority"), 1),
            -(item.get("updated") or 0))


def items(state=None, project=None, tag=None, include_done=False, limit=200):
    out = []
    wanted = normalize_state(state, default=None) if state else None
    project = _clean(project, 60).lower() if project else None
    tag = _clean(tag, 24).lower() if tag else None
    for item in _load():
        if wanted and item.get("state") != wanted:
            continue
        if not wanted and not include_done and item.get("state") == DONE:
            continue
        if project and (item.get("project") or "") != project:
            continue
        if tag and tag not in (item.get("tags") or []):
            continue
        out.append(item)
    out.sort(key=_sort_key)
    return out[:limit]


def board():
    """Everything grouped by state — the kanban view."""
    grouped = {state: [] for state in STATES}
    for item in _load():
        grouped.setdefault(item.get("state") or TODO, []).append(item)
    for state in grouped:
        grouped[state].sort(key=_sort_key)
    grouped[DONE] = grouped[DONE][:20]
    return grouped


def projects():
    """Every project with open/blocked/done counts, busiest first."""
    counts = {}
    for item in _load():
        name = item.get("project") or "(none)"
        entry = counts.setdefault(name, {"project": name, "open": 0,
                                         "blocked": 0, "done": 0, "total": 0})
        entry["total"] += 1
        state = item.get("state")
        if state == DONE:
            entry["done"] += 1
        else:
            entry["open"] += 1
            if state == BLOCKED:
                entry["blocked"] += 1
    return sorted(counts.values(), key=lambda e: -e["open"])


def summary():
    """One-glance state of play, for the digest and `jarvis backlog`."""
    all_items = _load()
    by_state = {state: 0 for state in STATES}
    for item in all_items:
        by_state[item.get("state") or TODO] = by_state.get(item.get("state") or TODO, 0) + 1
    blocked = [i for i in all_items if i.get("state") == BLOCKED]
    doing = [i for i in all_items if i.get("state") == DOING]
    # "Stale" is the thing a backlog is actually bad at surfacing: an item
    # you started and silently abandoned looks identical to one you're
    # working on right now.
    now = time.time()
    stale = [i for i in doing if now - (i.get("updated") or now) > 7 * 86400]
    return {
        "counts": by_state,
        "open": sum(by_state.get(s, 0) for s in OPEN_STATES),
        "blocked": [{"id": i["id"], "title": i["title"],
                     "blocked_on": i.get("blocked_on") or ""} for i in blocked],
        "doing": [{"id": i["id"], "title": i["title"]} for i in doing],
        "stale_doing": [{"id": i["id"], "title": i["title"],
                         "days": int((now - (i.get("updated") or now)) / 86400)}
                        for i in stale],
        "projects": projects(),
    }


def render(items_list):
    """Plain-text lines for the CLI."""
    if not items_list:
        return "(nothing)"
    marks = {IDEA: "~", TODO: " ", DOING: ">", BLOCKED: "!", DONE: "x"}
    lines = []
    for item in items_list:
        mark = marks.get(item.get("state"), " ")
        bits = [f"[{mark}] {item['id']}  {item['title']}"]
        meta = []
        if item.get("project"):
            meta.append(item["project"])
        if item.get("priority") == "high":
            meta.append("HIGH")
        if item.get("blocked_on"):
            meta.append(f"waiting on {item['blocked_on']}")
        if meta:
            bits.append("(" + ", ".join(meta) + ")")
        lines.append("  ".join(bits))
    return "\n".join(lines)
