"""File-host resolvers: turn a hoster's PAGE link into the real file link (link-resolver plan).

Discovery works like `hub/engines`: every module in this folder that doesn't start with "_" (and isn't one of the
shared helpers below) is imported and may export

    RESOLVER     one `base.Resolver` instance, or
    RESOLVERS    a list of them (the generic XFileSharing module serves several hosts)

Adding a host is therefore a new file here and nothing else.  A module that fails to import is recorded in
LOAD_ERRORS (hub_doctor and hoster_doctor print it) and skipped: one broken resolver can't take the others down.

Which links the hub treats as hoster links is decided here, by HOST: a resolver's own `hosts`, plus the owner's
`hosters.extra_hosts` (a host moved to a new domain needs no code change), minus any resolver the owner switched
off (`hosters.enabled.<id>`).  `find(url, settings)` is the one question the engine's route rule asks.
"""

import importlib
import pkgutil
import re
from urllib.parse import urlsplit

from . import base
from .base import ResolveContext, Resolved, Resolver  # noqa: F401 - re-exported for the engine and the tests

RESOLVERS = {}
LOAD_ERRORS = {}
_HELPERS = frozenset(["base", "fetch"])
_loaded = False


def load(force=False):
    global _loaded
    if _loaded and not force:
        return
    RESOLVERS.clear()
    LOAD_ERRORS.clear()
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        name = info.name
        if name.startswith("_") or name in _HELPERS:
            continue
        try:
            module = importlib.import_module("%s.%s" % (__name__, name))
            found = []
            if getattr(module, "RESOLVER", None) is not None:
                found.append(module.RESOLVER)
            found.extend(getattr(module, "RESOLVERS", None) or [])
            for resolver in found:
                rid = getattr(resolver, "id", "")
                if not re.fullmatch(r"[a-z0-9]{2,24}", rid or ""):
                    LOAD_ERRORS[name] = "a resolver has no valid id (letters and digits, 2 to 24)"
                    continue
                if rid in RESOLVERS:
                    LOAD_ERRORS[name] = "resolver id %r is already taken" % rid
                    continue
                RESOLVERS[rid] = resolver
        except Exception as exc:  # noqa: BLE001 - a broken resolver must not stop the others
            LOAD_ERRORS[name] = "%s: %s" % (type(exc).__name__, exc)
    _loaded = True


def ids():
    load()
    return sorted(RESOLVERS)


def get(resolver_id):
    load()
    return RESOLVERS.get(resolver_id)


def resolvers():
    load()
    return [RESOLVERS[i] for i in sorted(RESOLVERS)]


def _block(settings):
    block = (settings or {}).get("hosters")
    return block if isinstance(block, dict) else {}


def is_enabled(resolver_id, settings=None):
    """On unless the owner switched it off (`hosters.enabled.<id>` = false)."""
    enabled = _block(settings).get("enabled")
    if isinstance(enabled, dict) and enabled.get(resolver_id) is False:
        return False
    return True


def hosts_of(resolver, settings=None):
    """The resolver's own page hosts plus the owner's extra ones (`hosters.extra_hosts.<id>`), lower-case."""
    out = [h.lower() for h in resolver.hosts]
    extra = _block(settings).get("extra_hosts")
    if isinstance(extra, dict):
        more = extra.get(resolver.id)
        if isinstance(more, (list, tuple)):
            out += [str(h).strip().lower() for h in more if str(h).strip()]
    return out


def _host_of(url):
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return None, ""
    if (parts.scheme or "").lower() not in ("http", "https"):
        return None, ""
    return parts, (parts.hostname or "").lower().rstrip(".")


def _host_matches(resolver, host, hosts):
    for h in hosts:
        if host == h or host == "www." + h:
            return True
        if resolver.subdomains and host.endswith("." + h):
            return True
    return False


def find(url, settings=None):
    """The enabled resolver that owns this link's host and claims its path, or None.  Never raises.

    A link on a host the resolver doesn't own (a CDN name like download123.example.com) is not a page: it falls
    through to the plain HTTP engine, which is what a direct file link wants."""
    parts, host = _host_of(url)
    if parts is None or not host:
        return None
    for resolver in resolvers():
        if not is_enabled(resolver.id, settings):
            continue
        if not _host_matches(resolver, host, hosts_of(resolver, settings)):
            continue
        try:
            if resolver.claims(str(url).strip()):
                return resolver
        except Exception:  # noqa: BLE001 - a broken claims() must not break routing for everyone
            continue
    return None


def user_agent(settings=None):
    from .fetch import DEFAULT_USER_AGENT
    ua = str(_block(settings).get("user_agent") or "").strip()
    return ua or DEFAULT_USER_AGENT


def make_context(settings=None, stop=None, fetcher=None, log=None, dns=None):
    """A `ResolveContext` over the owner's settings: the SSRF allow-list, the User-Agent, the stop callback."""
    from .fetch import Fetcher
    settings = settings or {}
    ua = user_agent(settings)
    fetch = fetcher or Fetcher(allow_private_hosts=settings.get("allow_private_hosts") or [], user_agent=ua,
                               timeout=float((settings.get("http") or {}).get("connect_timeout", 15)), dns=dns)
    return ResolveContext(settings, fetch, ua, stop=stop, log=log)


def health(settings, resolver_id):
    """What `hoster_doctor` last found for a resolver: {at, ok, note} or None.  Never a claim made in advance."""
    record = (_block(settings).get("health") or {}).get(resolver_id)
    return record if isinstance(record, dict) else None


__all__ = ["RESOLVERS", "LOAD_ERRORS", "load", "ids", "get", "resolvers", "is_enabled", "hosts_of", "find",
           "user_agent", "make_context", "health", "base"]
