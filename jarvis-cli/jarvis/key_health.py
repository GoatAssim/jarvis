"""Which API keys / models are currently unhealthy (master plan F.9).

WHY. Every `jarvis` call is a brand-new process (AGENTS.md), and
ai_config.provider_keys() returns keys in a fixed order with no memory. So a
key that had just returned a 429 — or a model that had just returned a 503 —
was the first thing the very next ask tried again (Case 2b started on the key
that had 503'd in 2a and got another 503). This file is that memory, on disk:

    ~/.jarvis/key_health.json
    {"keys":      {"<provider>:<key-hash>": {"cooldown_until": ts, "last_status": "...",
                                              "last_ok": ts, "last_request": ts}},
     "models":    {"<provider>|<model>":    {"cooldown_until": ts}},
     "last_good": {"<provider>": "<provider>:<key-hash>"}}

Raw keys are never written — only a short hash of each.

POLICY (deliberately conservative — it only ever REORDERS, never removes):
  * 429 / quota:   the key cools down for the delay the provider stated
                   ("retry in 26s", Retry-After, retryDelay), else 60 s.
  * 401/403/402:   the key cools down for an hour (it will not fix itself).
  * 503 / 5xx:     a short cooldown on the MODEL, not the key — the condition
                   is model-wide, so another key rarely helps.
  * A success clears the key's cooldown and makes it the key to start with.
  order_keys() puts ready keys first (last-good leading) and cooling keys last,
  soonest-to-recover first, so a cooling key is still tried if nothing else is
  left. A provider whose model is cooling from an overload goes to the back of
  the provider list the same way — never out of it.
  * pace_key() (K.3.6): a floor between two requests on the SAME key,
    regardless of success/failure — see its own docstring.

Never raises: a corrupt or unwritable file means "no memory", i.e. the
behaviour from before this module existed.
"""

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
HEALTH_FILE = JARVIS_DIR / "key_health.json"

DEFAULT_RATE_LIMIT_COOLDOWN = 60.0      # a 429 that stated no delay
MAX_COOLDOWN = 3600.0                   # never park a key longer than this on a stated delay
BAD_KEY_COOLDOWN = 3600.0               # 401/403/402
OVERLOAD_MODEL_COOLDOWN = 45.0          # a 503 on a model
_PRUNE_AFTER = 86400.0
HOST_REFUSED_COOLDOWN = 60.0           # L.28 #4: a host that refused a connection (e.g. Ollama not running)

# K.3.6/F.9 item 3 ("pace ... or give it a different key" — spread_keys()
# above is the "or"; this is the "pace"). A conservative floor between two
# requests on the SAME key: enough to visibly slow a tight, near-instant
# tool-call loop (the failure mode in the 2026-09-20 log — code_agent's
# inner loop firing round after round on one key) without noticeably
# touching an ordinary ask, whose rounds are normally paced by real model
# latency anyway. Deliberately looser than "safe for a 5-req/min free
# tier" (~12s) — that's a per-provider call the owner can make with
# min_round_interval_seconds, not a default every high-limit provider
# should eat. 0 disables pacing entirely.
DEFAULT_MIN_ROUND_INTERVAL = 3.0

# What providers actually say. Gemini: `"retryDelay": "26s"` and "Please retry in
# 26.06s"; OpenAI/Groq: "try again in 20s" / "in 2m3.5s"; plus the header.
_DELAY_PATTERNS = (
    re.compile(r'retryDelay"?\s*[:=]\s*"?(\d+(?:\.\d+)?)\s*s', re.I),
    re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.I),
    re.compile(r"try again in (?:(\d+)\s*m\s*)?(\d+(?:\.\d+)?)\s*s", re.I),
)


def parse_retry_delay(text=None, headers=None):
    """Seconds a provider said to wait, from a Retry-After header or the error
    body/message; None when it stated nothing."""
    try:
        if headers:
            value = headers.get("Retry-After") or headers.get("retry-after")
            if value is not None and str(value).strip().replace(".", "", 1).isdigit():
                return float(value)
    except Exception:  # noqa: BLE001 — headers may be any mapping-ish thing
        pass
    if not text:
        return None
    t = str(text)
    m = _DELAY_PATTERNS[2].search(t)
    if m:
        return float(m.group(1) or 0) * 60 + float(m.group(2))
    for pattern in _DELAY_PATTERNS[:2]:
        m = pattern.search(t)
        if m:
            return float(m.group(1))
    return None


def key_id(provider_name, key):
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:10]
    return f"{provider_name}:{digest}"


def _load():
    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for section in ("keys", "models", "last_good", "hosts"):
                if not isinstance(data.get(section), dict):
                    data[section] = {}
            return data
    except (OSError, ValueError):
        pass
    return {"keys": {}, "models": {}, "last_good": {}, "hosts": {}}


def _save(state, now):
    # Drop entries that expired long ago so the file can't grow forever.
    for section in ("keys", "models"):
        state[section] = {k: v for k, v in state[section].items()
                          if isinstance(v, dict) and (v.get("cooldown_until", 0) > now - _PRUNE_AFTER
                                                      or v.get("last_ok", 0) > now - _PRUNE_AFTER
                                                      or v.get("last_request", 0) > now - _PRUNE_AFTER)}
    state["hosts"] = {k: v for k, v in (state.get("hosts") or {}).items()
                      if isinstance(v, dict) and (v.get("refused_until", 0) > now)}
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(HEALTH_FILE.parent), prefix=".key_health-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, HEALTH_FILE)
    except OSError:
        pass


def cooldown_until(provider_name, key, now=None):
    now = time.time() if now is None else now
    entry = _load()["keys"].get(key_id(provider_name, key)) or {}
    until = entry.get("cooldown_until") or 0
    return until if until > now else 0


def model_cooling(provider_name, model, now=None):
    now = time.time() if now is None else now
    entry = _load()["models"].get(f"{provider_name}|{model}") or {}
    return (entry.get("cooldown_until") or 0) > now


def order_keys(provider_name, keys, now=None):
    """`keys` reordered: last-good first, other ready keys in their configured
    order, cooling keys last (soonest to recover first). Same keys, same count."""
    if not keys or len(keys) < 2 or None in keys:
        return list(keys)
    now = time.time() if now is None else now
    state = _load()
    ready, cooling = [], []
    for k in keys:
        until = (state["keys"].get(key_id(provider_name, k)) or {}).get("cooldown_until") or 0
        (cooling if until > now else ready).append((until, k))
    cooling.sort(key=lambda t: t[0])
    ordered = [k for _, k in ready]
    good = state["last_good"].get(provider_name)
    for i, k in enumerate(ordered):
        if key_id(provider_name, k) == good and i:
            ordered.insert(0, ordered.pop(i))
            break
    return ordered + [k for _, k in cooling]


def spread_keys(provider_name, keys, now=None):
    """order_keys(), but with the SECOND ready key first when there are two or
    more. For nested agents (code_agent's inner loop): the outer ask is already
    working on the best key, and in the 2026-09-20 log the inner loop's 6
    requests on that same key are what tripped its 5-per-minute limit (F.9
    item 3). subagents.py isolates keys for child agents for the same reason."""
    now = time.time() if now is None else now
    ordered = order_keys(provider_name, keys, now)
    ready = [k for k in ordered if k is not None and not cooldown_until(provider_name, k, now)]
    if len(ready) >= 2 and ordered[0] == ready[0]:
        ordered = ordered[1:2] + ordered[:1] + ordered[2:]
    return ordered


def pace_key(provider_name, key, interval=None, now=None):
    """K.3.6: block until this key's last request (recorded by ANY process —
    see module docstring re: `jarvis` being a new process per call) was at
    least `interval` seconds ago, then record this request's timestamp.
    Called once per round, right before the request goes out, from the
    shared spot in each of ai_providers.py's five adapter loops.

    Distinct from spread_keys(): that gives a nested loop a different key
    up front (when one exists); this paces requests on whichever key is
    ACTUALLY in use, same-key or not, single provider or many keys. A key
    with only one entry in its provider block still gets this — spread_keys
    can't help there (nothing to spread to), pacing still can.

    `key=None` (Ollama; no per-key quota to protect) and `interval<=0`
    (explicitly disabled) are both no-ops — neither sleeps nor touches the
    file, so a keyless/local provider's timing is completely unaffected.
    """
    if key is None:
        return
    interval = DEFAULT_MIN_ROUND_INTERVAL if interval is None else interval
    if interval <= 0:
        return
    now = time.time() if now is None else now
    kid = key_id(provider_name, key)
    state = _load()
    entry = state["keys"].setdefault(kid, {})
    last = entry.get("last_request") or 0
    wait = interval - (now - last)
    if wait > 0:
        time.sleep(wait)
        now += wait
    entry["last_request"] = now
    _save(state, now)


def record_success(provider_name, model, key, now=None):
    if key is None:
        return
    now = time.time() if now is None else now
    state = _load()
    kid = key_id(provider_name, key)
    entry = state["keys"].setdefault(kid, {})
    was_cooling = bool(entry.get("cooldown_until"))
    entry.update({"last_ok": now, "last_status": "ok", "cooldown_until": 0})
    changed = was_cooling or state["last_good"].get(provider_name) != kid
    state["last_good"][provider_name] = kid
    state["models"].pop(f"{provider_name}|{model}", None)
    if changed:  # don't rewrite the file on every healthy ask
        _save(state, now)


def record_failure(provider_name, model, key, kind, error, retry_after=None, now=None):
    """Note a failed attempt. `kind` is an ai_providers.KIND_* value; only the
    key-quota, bad-key and overload kinds leave any state behind."""
    now = time.time() if now is None else now
    err = str(error or "").lower()
    state = _load()
    if kind == "overload":
        state["models"][f"{provider_name}|{model}"] = {"cooldown_until": now + OVERLOAD_MODEL_COOLDOWN}
    elif kind == "key" and key is not None:
        if "rate limited" in err or "quota" in err:
            delay = retry_after if retry_after is not None else parse_retry_delay(error)
            delay = DEFAULT_RATE_LIMIT_COOLDOWN if delay is None else min(max(delay, 5.0), MAX_COOLDOWN)
            status = "rate_limited"
        else:
            delay, status = BAD_KEY_COOLDOWN, "rejected"
        entry = state["keys"].setdefault(key_id(provider_name, key), {})
        entry.update({"cooldown_until": now + delay, "last_status": status})
    else:
        return
    _save(state, now)


def cooling_keys(now=None):
    """[(key-id, seconds-left, status)] for the doctor."""
    now = time.time() if now is None else now
    return [(k, v["cooldown_until"] - now, v.get("last_status", ""))
            for k, v in _load()["keys"].items()
            if isinstance(v, dict) and (v.get("cooldown_until") or 0) > now]


# --- L.28 #4: remember an endpoint that refused a connection ---------------
# ai_client already skips a host that refused a connection earlier in the SAME
# ask. jarvis is a fresh process per call and a chat gateway answers many asks,
# so an Ollama that is simply not running cost a ~4 s connect failure on every
# ask that reached it. This carries the refusal across asks for a short window.

def record_host_refused(host, now=None):
    if not host:
        return
    now = time.time() if now is None else now
    state = _load()
    state["hosts"][str(host)] = {"refused_until": now + HOST_REFUSED_COOLDOWN}
    _save(state, now)


def host_recently_refused(host, now=None):
    """Seconds left on the refusal window for `host`, or 0."""
    if not host:
        return 0.0
    now = time.time() if now is None else now
    entry = _load()["hosts"].get(str(host)) or {}
    until = entry.get("refused_until") or 0
    return float(until - now) if until > now else 0.0


def clear_host_refused(host):
    """A successful call to the host ends the window early."""
    if not host:
        return
    state = _load()
    if state["hosts"].pop(str(host), None) is not None:
        _save(state, time.time())
