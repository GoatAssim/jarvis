"""What a download row may say, and how it may change (plan sections 4.3, 5.3, 5.4).

Pure functions, no I/O, so the store, the engines, hub_client and the tests all share ONE
definition of "a sane row".  Everything a provider (or an engine) reports is untrusted
text and untrusted numbers: it is clamped, length-capped and stripped of control
characters here before it can reach the database, and the panel then renders it with
`textContent` only.
"""

import math
import re
import time
from urllib.parse import urlsplit

STATES = (
    "queued", "probing", "connecting", "downloading", "postprocessing",
    "paused", "stalled", "failed", "cancelled", "done", "unknown",
)
# A worker (a thread in the daemon) is holding the row.
ACTIVE_STATES = ("probing", "connecting", "downloading", "postprocessing")
# Nothing is running, but the scheduler may pick it up (stalled once its retry time comes).
WAITING_STATES = ("queued", "stalled")
FINAL_STATES = ("done", "failed", "cancelled")
# A provider row in one of these may be dismissed (hidden; no file is touched).  `unknown` is
# not final, but nothing is running for it either.  A hub row may be hidden once it has FAILED or been
# CANCELLED (nothing is running and nothing was kept); a finished one is removed from History instead.
DISMISSABLE_STATES = FINAL_STATES + ("unknown",)
HUB_DISMISSABLE_STATES = ("failed", "cancelled")
# States in which a speed / ETA makes no sense: they are cleared on entry.
NO_RATE_STATES = ("queued", "paused", "stalled", "failed", "cancelled", "done", "unknown")

KINDS = ("video", "audio", "file", "torrent", "game", "other")
CAPABILITIES = ("pause", "resume", "cancel", "retry", "prioritize", "remove", "open", "reveal", "play")

# Hub-owned rows follow this table.  Provider-owned rows are a mirror of what the provider
# said, so any state is accepted from them (a tool that restarts a file may go backwards).
_TRANSITIONS = {
    "queued": ("probing", "connecting", "downloading", "paused", "cancelled", "failed"),
    "probing": ("connecting", "downloading", "postprocessing", "queued", "paused",
                "stalled", "failed", "cancelled", "done"),
    "connecting": ("probing", "downloading", "postprocessing", "queued", "paused",
                   "stalled", "failed", "cancelled", "done"),
    "downloading": ("probing", "connecting", "postprocessing", "queued", "paused",
                    "stalled", "failed", "cancelled", "done"),
    "postprocessing": ("downloading", "queued", "paused", "stalled", "failed", "cancelled", "done"),
    "paused": ("queued", "cancelled"),
    "stalled": ("queued", "probing", "connecting", "downloading", "paused", "failed", "cancelled"),
    "failed": ("queued",),
    "cancelled": ("queued",),
    "done": (),
    "unknown": ("queued", "cancelled", "failed", "done"),
}

PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Names the hub keeps for itself: a provider can't squat on an engine's or the hub's id.
RESERVED_PROVIDERS = frozenset({"hub", "http", "ytdlp", "stremio", "engine", "engines"})

MAX_TITLE = 300
MAX_SUBTITLE = 200
MAX_TEXT = 500
MAX_DETAIL_ROWS = 20
MAX_CHILDREN = 200
MAX_LINKS = 6
MAX_THUMB_BYTES = 256 * 1024
DEFAULT_LINK_SCHEMES = ("steam", "https")

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")


def is_final(state):
    return state in FINAL_STATES


def is_active(state):
    return state in ACTIVE_STATES


def is_dismissable(owner, state):
    """May this row be hidden?  A provider row that is unknown or finished; a hub row that failed or was cancelled."""
    if owner == "hub":
        return state in HUB_DISMISSABLE_STATES
    return state in DISMISSABLE_STATES


def can_transition(old, new, owner="hub"):
    """May a row go from `old` to `new`?  Same state is always fine (a no-op write)."""
    if new not in STATES:
        return False
    if old == new:
        return True
    if owner != "hub":
        return True
    return new in _TRANSITIONS.get(old, ())


# --------------------------------------------------------------------------- numbers

def clean_number(value, lo=0.0, hi=None):
    """A finite number >= lo (and <= hi when given), else None.  Booleans are not numbers."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(num) or num < lo:
        return None
    if hi is not None and num > hi:
        return float(hi)
    return num


def clean_int(value, lo=0, hi=None):
    num = clean_number(value, lo, hi)
    return None if num is None else int(num)


def clean_percent(value):
    """Clamped to 0-100; None for anything that isn't a finite number."""
    num = clean_number(value, -1e18, None)
    if num is None:
        return None
    return max(0.0, min(100.0, num))


def derive_percent(done, total):
    if done is None or not total or total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * float(done) / float(total)))


def smooth_speed(previous, instant, dt, tau=10.0):
    """Exponentially smoothed speed over roughly `tau` seconds (plan: ~10 s).

    Time-based on purpose: reports arrive at uneven intervals (a provider is throttled to
    one a second, a poll may be 3 s), so a fixed per-sample weight would over-react to
    a late report and under-react to a burst.
    """
    if instant is None:
        return previous
    if previous is None or dt <= 0:
        return instant
    alpha = 1.0 - math.exp(-dt / tau)
    return previous + alpha * (instant - previous)


class RateTracker:
    """Speed and ETA for an engine that knows its own byte count.

    The engine measures the bytes it really moved, so the numbers it reports are the
    engine's own, not the hub's guess (no "≈" in the panel).
    """

    def __init__(self, tau=10.0, clock=time.monotonic):
        self._tau = tau
        self._clock = clock
        self._t = None
        self._done = None
        self.speed = None

    def update(self, done, now=None):
        now = self._clock() if now is None else now
        if self._t is None:
            self._t, self._done = now, done
            return self.speed
        dt = now - self._t
        if dt < 0.25:
            return self.speed
        if done >= self._done:
            self.speed = smooth_speed(self.speed, (done - self._done) / dt, dt, self._tau)
        self._t, self._done = now, done
        return self.speed

    @staticmethod
    def eta(total, done, speed):
        if not total or speed is None or speed <= 0 or done is None:
            return None
        return max(0.0, (total - done) / speed)


def backoff_seconds(attempt, base=5.0, cap=300.0):
    """Wait before retry number `attempt` (1-based): 5, 10, 20 ... capped."""
    try:
        n = max(1, int(attempt))
    except (TypeError, ValueError):
        n = 1
    return float(min(cap, base * (2 ** (n - 1))))


# --------------------------------------------------------------------------- text

def clean_text(value, limit=MAX_TEXT):
    """Untrusted text -> one short printable line.  None stays None (a clear)."""
    if value is None:
        return None
    text = _CONTROL_RE.sub("", str(value))
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def clean_detail(value):
    """[{label, value}] (or a dict) -> at most MAX_DETAIL_ROWS capped pairs."""
    pairs = []
    if isinstance(value, dict):
        value = [{"label": k, "value": v} for k, v in value.items()]
    if not isinstance(value, (list, tuple)):
        return []
    for row in value:
        if len(pairs) >= MAX_DETAIL_ROWS:
            break
        if not isinstance(row, dict):
            continue
        label = clean_text(row.get("label"), 60)
        val = clean_text(row.get("value"), 200)
        if label and val is not None:
            pairs.append({"label": label, "value": val})
    return pairs


def clean_children(value):
    """A download's sub-files: shown in the detail drawer, never as top-level rows."""
    out = []
    if not isinstance(value, (list, tuple)):
        return out
    for row in value:
        if len(out) >= MAX_CHILDREN:
            break
        if not isinstance(row, dict):
            continue
        name = clean_text(row.get("name") or row.get("title"), 200)
        if not name:
            continue
        child = {"name": name}
        for key in ("bytes_done", "bytes_total"):
            num = clean_int(row.get(key))
            if num is not None:
                child[key] = num
        pct = clean_percent(row.get("percent")) if row.get("percent") is not None else None
        if pct is not None:
            child["percent"] = pct
        state = row.get("state")
        if state in STATES:
            child["state"] = state
        out.append(child)
    return out


def clean_capabilities(value):
    if not isinstance(value, (list, tuple, set)):
        return []
    seen = []
    for cap in value:
        if cap in CAPABILITIES and cap not in seen:
            seen.append(cap)
    return seen


def clean_controls(value):
    """capability -> the tool (of the provider's own file) that performs it."""
    out = {}
    if not isinstance(value, dict):
        return out
    for cap, tool in value.items():
        if cap in CAPABILITIES and isinstance(tool, str) and TOOL_NAME_RE.match(tool):
            out[cap] = tool
    return out


def clean_links(value, allowed_schemes=DEFAULT_LINK_SCHEMES):
    """Extra buttons a provider may add.  `url` links only use schemes the owner allows
    (default steam, https); `file:` / `javascript:` and anything else is dropped."""
    out = []
    if not isinstance(value, (list, tuple)):
        return out
    allowed = {str(s).lower() for s in (allowed_schemes or ())}
    for row in value:
        if len(out) >= MAX_LINKS:
            break
        if not isinstance(row, dict):
            continue
        label = clean_text(row.get("label"), 40)
        kind = row.get("kind")
        target = row.get("target")
        if not label or kind not in ("tool", "url") or not isinstance(target, str):
            continue
        if kind == "tool":
            if not TOOL_NAME_RE.match(target):
                continue
        else:
            target = target.strip()
            if len(target) > 300 or _CONTROL_RE.search(target):
                continue
            try:
                scheme = urlsplit(target).scheme.lower()
            except ValueError:
                continue
            if scheme not in allowed:
                continue
        out.append({"label": label, "kind": kind, "target": target})
    return out


_THUMB_RE = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")


def clean_thumb(value):
    """Only a small base64 image data-URI.  A path would have to be served back by an
    id-based route (panel option B, phase 9); until then it is simply not accepted."""
    if not isinstance(value, str) or len(value) > MAX_THUMB_BYTES:
        return None
    return value if _THUMB_RE.match(value) else None


# --------------------------------------------------------------------------- reports

_TEXT_FIELDS = {
    "title": MAX_TITLE, "subtitle": MAX_SUBTITLE, "phase": 60,
    "message": MAX_TEXT, "error": MAX_TEXT,
}
_NUMBER_FIELDS = ("bytes_done", "bytes_total", "peers", "seeds", "phase_index", "phase_count")
# percent disagreeing with bytes by more than this is logged (plan 5.4 item 1)
PERCENT_DISAGREE = 5.0


def sanitize_report(fields, prev=None, now=None, allow_derive=True):
    """Make one report safe and complete.

    `fields` is what an engine or provider said (any subset of the row shape); `prev` is
    the row as stored (a dict) or None for a new row.  Returns (clean, notes, derived):

      clean    only keys that should be written (unknown keys are dropped)
      notes    short strings for the event log ("percent went backwards", ...)
      derived  which of percent / speed / eta the HUB computed (the panel shows "≈")

    Rules (plan 5.4): percent is the provider's word and is only derived from bytes when it
    is omitted; an explicit None means indeterminate; percent is clamped to 0-100 and
    non-finite / negative numbers are dropped; `done` forces 100; a percent that goes
    backwards is accepted but noted.
    """
    now = time.time() if now is None else now
    prev = prev or {}
    notes = []
    derived = set()
    clean = {}
    fields = fields if isinstance(fields, dict) else {}

    for key, limit in _TEXT_FIELDS.items():
        if key in fields:
            clean[key] = clean_text(fields[key], limit)
    if "state" in fields:
        if fields["state"] in STATES:
            clean["state"] = fields["state"]
        else:
            notes.append("ignored unknown state %r" % (clean_text(fields["state"], 30),))
    if "kind" in fields and fields["kind"] in KINDS:
        clean["kind"] = fields["kind"]
    # None clears a value on purpose; garbage (negative, NaN, text) is IGNORED, never stored
    # and never allowed to wipe what the row already knows.
    for key in _NUMBER_FIELDS:
        if key in fields:
            value = None if fields[key] is None else clean_int(fields[key])
            if fields[key] is not None and value is None:
                notes.append("ignored invalid %s" % key)
            else:
                clean[key] = value
    for key in ("speed_bps", "eta_s"):
        if key in fields:
            value = None if fields[key] is None else clean_number(fields[key])
            if fields[key] is not None and value is None:
                notes.append("ignored invalid %s" % key)
            else:
                clean[key] = value

    state = clean.get("state", prev.get("state"))
    done = clean["bytes_done"] if "bytes_done" in clean else prev.get("bytes_done")
    total = clean["bytes_total"] if "bytes_total" in clean else prev.get("bytes_total")
    if done is not None and total and done > total:
        done = total
        clean["bytes_done"] = total
        notes.append("bytes_done was above bytes_total; clamped")

    # --- percent: the provider's own wins; derive only when omitted.
    pct_given = "percent" in fields
    if pct_given and fields["percent"] is not None and clean_percent(fields["percent"]) is None:
        notes.append("ignored invalid percent")
        pct_given = False
        fields = {k: v for k, v in fields.items() if k != "percent"}
    if pct_given:
        pct = clean_percent(fields["percent"])  # None = indeterminate, kept as None
        clean["percent"] = pct
        calc = derive_percent(done, total)
        if pct is not None and calc is not None and abs(calc - pct) > PERCENT_DISAGREE:
            notes.append("percent %.1f disagrees with bytes (%.1f); kept the reported percent" % (pct, calc))
    elif allow_derive and ("bytes_done" in clean or "bytes_total" in clean):
        calc = derive_percent(done, total)
        if calc is not None:
            clean["percent"] = calc
            derived.add("percent")
    if state == "done":
        clean["percent"] = 100.0
        derived.discard("percent")

    old_pct = prev.get("percent")
    new_pct = clean.get("percent")
    if old_pct is not None and new_pct is not None and new_pct < old_pct - 0.5:
        notes.append("percent went backwards (%.1f -> %.1f)" % (old_pct, new_pct))

    # --- speed / eta: derive only when the reporter didn't supply them.
    last_ts = prev.get("last_report_at")
    if (allow_derive and "speed_bps" not in fields and "bytes_done" in clean
            and prev.get("bytes_done") is not None and last_ts):
        dt = now - last_ts
        if dt >= 0.2 and clean["bytes_done"] >= prev["bytes_done"]:
            inst = (clean["bytes_done"] - prev["bytes_done"]) / dt
            clean["speed_bps"] = smooth_speed(prev.get("speed_bps"), inst, dt)
            derived.add("speed")
    speed = clean["speed_bps"] if "speed_bps" in clean else prev.get("speed_bps")
    if allow_derive and "eta_s" not in fields and total and done is not None and speed:
        eta = RateTracker.eta(total, done, speed)
        if eta is not None:
            clean["eta_s"] = eta
            derived.add("eta")

    if state in NO_RATE_STATES:
        clean["speed_bps"] = None
        clean["eta_s"] = 0.0 if state == "done" else None
        derived.discard("speed")
        derived.discard("eta")
    return clean, notes, derived
