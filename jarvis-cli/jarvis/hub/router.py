"""The source router (plan section 8): what does `dl_add` do with an input?

Engines declare their own rules (`ROUTE_RULES` in `engines/<name>.py`); this module sorts
them by priority and asks each in turn, so a site-specific engine added in a later phase
claims its links before the generic HTTP engine without anyone editing this file.

Inputs the hub already knows it will handle but has no engine for yet (a magnet link, a
YouTube page) get a plain "not installed yet" answer instead of being fed to the HTTP engine
as if they were a file -- a wrong guess here would download a web page and call it a video.

The router only picks HUB engines.  A tool-supplied download is started by its own tool and
appears on its own (plan section 8, last paragraph).
"""

import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from . import engines

_MAGNET = re.compile(r"^magnet:\?", re.I)
_INFOHASH = re.compile(r"^[0-9a-fA-F]{40}$")
_YOUTUBE_HOSTS = frozenset(["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                            "youtu.be", "www.youtu.be"])


def _is_youtube(text):
    try:
        host = (urlsplit(text).hostname or "").lower()
    except ValueError:
        return False
    return host in _YOUTUBE_HOSTS


# (engine, predicate, what it is, kind, which phase brings the engine)
_PLANNED = (
    ("stremio", lambda t: bool(_MAGNET.match(t) or _INFOHASH.match(t)), "a magnet link", "torrent", 4),
    ("ytdlp", _is_youtube, "a YouTube page", "video", 3),
)


def canonical_url(url):
    """Lower-case scheme and host, default port and fragment dropped.  Used for dedupe."""
    try:
        parts = urlsplit(str(url).strip())
        host = (parts.hostname or "").lower()
        if ":" in host:
            host = "[%s]" % host
        port = parts.port
    except ValueError:
        return str(url).strip()
    scheme = parts.scheme.lower()
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = "%s:%d" % (host, port)
    return urlunsplit((scheme, host, parts.path, parts.query, ""))


def options_hash(options):
    keep = {k: (options or {}).get(k) for k in ("subdir", "filename") if (options or {}).get(k)}
    return hashlib.sha1(json.dumps(keep, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def dedupe_key(engine, source, options=None):
    return "%s|%s|%s" % (engine, source, options_hash(options))


def _result(ok, engine=None, reason="", kind="file", source="", available=False, problem=None):
    return {"ok": ok, "engine": engine, "reason": reason, "kind": kind, "source": source,
            "available": available, "problem": problem}


def route(text, engine=None, settings=None):
    """Decide the engine for one input.  Never raises.

    Returns {ok, engine, reason, kind, source, available, problem}.  `ok` means "queue it
    now"; otherwise `problem` says why not in words the owner can act on.  `reason` is the
    one-line explanation shown beside the decision ("direct link (HTTP engine)") so a wrong
    guess is visible and can be overridden with `engine=`.
    """
    text = str(text if text is not None else "").strip()
    if not text:
        return _result(False, problem="nothing to download: give a link")
    if len(text) > 4096:
        return _result(False, problem="that input is too long to be a link")
    rules = sorted(engines.route_rules(), key=lambda r: -r.priority)

    if not engine:
        # Input the hub knows it will handle but has no engine for yet must be refused HERE:
        # the generic HTTP rule would happily take a YouTube page and save the web page as a
        # "video".  Once the engine exists (a later phase) this branch no longer fires and the
        # engine's own, higher-priority rules claim the input.
        for name, predicate, what, kind, phase in _PLANNED:
            if predicate(text) and engines.get(name) is None:
                return _result(False, engine=name, kind=kind, source=text,
                               problem="that is %s, which needs the %s engine; it isn't installed yet (phase %d "
                                       "of the download hub plan)" % (what, name, phase))

    if engine:
        known = engines.get(engine)
        if known is None:
            return _result(False, problem="unknown engine %r (installed: %s)"
                           % (str(engine)[:30], ", ".join(engines.names()) or "none"))
        for rule in rules:
            if rule.engine == engine:
                hit = _safe_match(rule, text, settings)
                if hit:
                    return _finish(engine, hit, text, forced=True)
        return _result(False, engine=engine, problem="the %s engine can't take that input" % engine)

    for rule in rules:
        hit = _safe_match(rule, text, settings)
        if hit:
            return _finish(rule.engine, hit, text)

    return _result(False, problem="that doesn't look like something I can download: expected an "
                                  "http(s) link (magnet links and YouTube pages arrive with later phases)")


def _safe_match(rule, text, settings):
    try:
        return rule.match(text, settings or {})
    except Exception:  # noqa: BLE001 - a broken rule must not break routing for everyone
        return None


def _finish(engine_name, hit, text, forced=False):
    engine = engines.get(engine_name)
    ok, why = (engine.available() if engine else (False, "engine not loaded"))
    reason = hit.get("reason") or engine_name
    if forced:
        reason = "you chose the %s engine" % engine_name
    if not ok:
        return _result(False, engine=engine_name, reason=reason, kind=hit.get("kind", "file"),
                       source=hit.get("source", text),
                       problem="the %s engine isn't ready: %s" % (engine_name, why or "unavailable"))
    return _result(True, engine=engine_name, reason=reason, kind=hit.get("kind", "file"),
                   source=hit.get("source", text), available=True)
