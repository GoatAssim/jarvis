"""present_download_status: show a live download card in the chat (Download Hub, phase 8 item 17.3-4).

The model calls this once after it queues something ("here's your download") and the web chat
draws ONE card per download (or one for a whole batch) that keeps moving: a bar, speed, ETA,
Pause / Resume / Cancel, and Open / Show in folder when it is done.  The card is read-only data
from the hub; its buttons go through the same `dl_*` tools and the same confirmations as the
Downloads panel.  Nothing here writes to the hub.

How the card reaches the page: the tool prints one `download_status` card line through
`wire.media` (the one seam every card uses).  The line carries the ids to follow and the last
known status, so a reloaded conversation can draw the card again before the live stream answers.
Without a web page (CLI, Discord, scheduled jobs) the card line goes nowhere and the model's
text result below is all anyone sees, which is why that text is a complete one-line status.

Pure helpers (`summary_line`, `card_line_payload`) are tested without a database.
"""

import json

TOOL_GROUP = "downloads"

TOOL_KEYWORDS = {
    "present_download_status": {
        "download progress card": 9,
        "show download progress": 9,
        "download status card": 9,
        "show me the download": 8,
        "how is my download": 7,
    },
}

TOOL_PACK_INSTRUCTION = (
    "present_download_status(ids|group|query): after you queue a download with dl_add, call this ONCE to show "
    "the user a live progress card (bar, speed, Pause/Cancel, Open when done). Pass the ids dl_add returned "
    "(or the group id for a batch). Do not call it again to 'refresh' - the card updates itself. "
    "It is read-only and does not start, stop or change anything."
)

TOOL_SCHEMAS = [{
    "name": "present_download_status",
    "description": (
        "Show a live status card in the chat for one or more Download Hub downloads: progress bar, speed, "
        "time left, and Pause / Resume / Cancel buttons (Open / Show in folder once finished). Call it once, "
        "right after dl_add, with the id(s) dl_add returned, or with `group` for a whole batch, or `query` to "
        "find a download by title. The card keeps updating by itself; never call it repeatedly to refresh. "
        "Read-only: it changes nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "items": {"type": "string"}, "description": "Download ids (at most 8), as dl_add / dl_list returned them."},
            "group": {"type": "string", "description": "A batch's group id: one card with the counts and an overall bar."},
            "query": {"type": "string", "description": "Part of a download's title, when you don't have an id."},
        },
        "required": [],
    },
}]


def _fmt_bytes(n):
    if not isinstance(n, (int, float)) or n < 0:
        return None
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return None


def summary_line(card):
    """One plain sentence about a card (an item row or a group card): the text result, and what
    a channel without a web page can say.  Pure."""
    title = (card.get("title") or "download").strip() or "download"
    state = card.get("state") or "unknown"
    pct = card.get("percent")
    pct_text = ("%d%%" % round(pct)) if isinstance(pct, (int, float)) else None
    if card.get("card") == "group":
        c = card.get("counts") or {}
        text = "%s: %d of %d done" % (title, c.get("done", 0), c.get("total", 0))
        if c.get("failed"):
            text += ", %d failed" % c["failed"]
        if c.get("running"):
            text += ", %d running" % c["running"]
        return text + (" (%s)" % pct_text if pct_text else "")
    bits = [state]
    if pct_text and state not in ("done",):
        bits.append(pct_text)
    speed = _fmt_bytes(card.get("speed_bps"))
    if speed and state == "downloading":
        bits.append(speed + "/s")
    if state in ("failed", "stalled") and card.get("error"):
        bits.append(str(card["error"])[:120])
    return "%s: %s" % (title, ", ".join(bits))


def card_line_payload(cards, title=None):
    """The JSON the page gets: which ids / group to follow and the last known status.  Only
    fields the card draws are kept, so the line stays small and nothing private rides along."""
    keep = ("id", "card", "title", "state", "percent", "speed_bps", "eta_s", "bytes_done", "bytes_total",
            "engine", "kind", "provider", "provider_name", "owner", "error", "stale", "final", "counts",
            "capabilities", "has_thumb", "phase_label", "subtitle", "attention", "age_s", "retry_in_s")
    slim = [{k: c[k] for k in keep if k in c} for c in cards[:8]]
    return {"v": 1, "title": title or None, "cards": slim,
            "ids": [c["id"] for c in slim if c.get("card") == "item"],
            "groups": [c["id"] for c in slim if c.get("card") == "group"]}


def tool_present_download_status(ids=None, group=None, query=None):
    """Draw the card; the returned text is the status in words."""
    from .. import hub_admin, wire
    ids = [str(i) for i in ids] if isinstance(ids, list) else ([str(ids)] if isinstance(ids, str) and ids else [])
    if not (ids or group or query):
        return json.dumps({"ok": False, "problem": "Say which download: pass the id(s) dl_add returned, a group id, or part of the title."})
    result = hub_admin.card_payload(ids=ids, group=group, query=query)
    if not result.get("ok"):
        return json.dumps({"ok": False, "problem": result.get("error") or "couldn't read that download"})
    cards = result["cards"]
    payload = card_line_payload(cards)
    wire.media("download_status", json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
    return json.dumps({
        "ok": True,
        "shown": len(payload["cards"]),
        "card": payload,
        "status": [summary_line(c) for c in cards],
        "note": "A live card is now in the chat and updates by itself. Don't call this again to refresh it.",
    })


TOOLS = {"present_download_status": tool_present_download_status}

# The card payload is for the page (and the reload replay, which reads the unshaped result); the model
# only needs the status sentences.
TOOL_RESULT_SPECS = {
    "present_download_status": {"drop_fields": {"medium": ["card"], "low": ["card"]}},
}

TEST_CHECKLIST = {
    "present_download_status": {
        "does": "Draws a live download card in the web chat for downloads the hub already has; read-only.",
        "steps": [
            {"run": {"ids": ["<an id from Menu > Downloads or dl_list>"]},
             "expect": "ok: true, shown 1, a one-line status; in the web chat a card appears with a bar that keeps moving "
                       "while the download runs, and Pause / Cancel work from it."},
            {"run": {"ids": ["<the id of a finished download>"]},
             "expect": "The card shows Done with Open and Show in folder; after a page reload it is drawn again from the saved status."},
            {"run": {"group": "<a group id from dl_list>"},
             "expect": "One card for the whole batch: counts, one overall bar."},
            {"run": {"ids": ["no-such-id"]},
             "expect": "ok: false, problem says there is no such download; no card is drawn."},
            {"run": {},
             "expect": "ok: false, problem asks which download."},
        ],
    },
}
