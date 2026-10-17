"""The SSRF guard (plan section 14: "important for a universal URL downloader").

A prompt-injected model could ask for `http://192.168.1.1/admin` or
`http://127.0.0.1:...` and have the hub fetch it from inside the owner's network.  So the
HTTP engine resolves the host and REFUSES loopback, private, link-local, shared-address,
multicast, unspecified and reserved addresses -- on the first request and again after every
redirect -- unless the owner allow-listed the host (`allow_private_hosts`, which is what
lets the Stremio server on 127.0.0.1:11470 through later).

Known limit, on purpose documented rather than hidden: the check resolves the name and
`requests` resolves it again when it connects, so a DNS server that answers differently the
second time (DNS rebinding) could slip a private address past the guard.  Closing that needs
a connection pinned to the checked address; it is not done in phases 1-2.  The redirect
re-check, the scheme allow-list and "no credentials in the URL" are done.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}


class GuardError(Exception):
    """The URL may not be fetched.  The message is safe to show the owner."""


def parse_allow(entries):
    """["127.0.0.1:11470", "nas.local"] -> {("127.0.0.1", 11470), ("nas.local", None)}.

    A bare host allows any port; `host:port` only that port.  Anything unparseable is
    skipped (an allow-list that can't be read must allow LESS, never more).
    """
    out = set()
    if isinstance(entries, str):
        entries = [entries]
    for raw in entries or []:
        text = str(raw).strip().lower()
        if not text:
            continue
        host, port = text, None
        if text.startswith("["):  # [::1] or [::1]:8080
            end = text.find("]")
            if end == -1:
                continue
            host = text[1:end]
            rest = text[end + 1:]
            if rest.startswith(":") and rest[1:].isdigit():
                port = int(rest[1:])
            elif rest:
                continue
        elif text.count(":") == 1:
            host, _, p = text.partition(":")
            if not p.isdigit():
                continue
            port = int(p)
        if host:
            out.add((host, port))
    return out


def _is_public(ip):
    """True only for a globally routable address."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped  # ::ffff:127.0.0.1 is still loopback
    return bool(addr.is_global) and not addr.is_multicast


def _port_of(parts):
    try:
        port = parts.port
    except ValueError:
        raise GuardError("the URL has an invalid port")
    return port if port is not None else _DEFAULT_PORTS.get(parts.scheme)


def check_url(url, allow_private_hosts=(), resolver=None):
    """Raise GuardError unless `url` may be fetched.  Returns the split URL on success.

    `resolver(host, port)` returns a list of address strings; it defaults to
    `socket.getaddrinfo` and is injectable so the tests never touch DNS.
    """
    text = str(url or "").strip()
    if not text or len(text) > 4096:
        raise GuardError("empty or oversized URL")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise GuardError("not a valid URL")
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise GuardError("only http and https links can be downloaded (got %r)" % (scheme or "none"))
    host = (parts.hostname or "").lower()
    if not host:
        raise GuardError("the URL has no host")
    if parts.username or parts.password:
        # Credentials in a URL end up in logs, the panel and the model's context.
        raise GuardError("URLs with a user name or password in them are refused")
    port = _port_of(parts)

    allowed = parse_allow(allow_private_hosts)
    if (host, port) in allowed or (host, None) in allowed:
        return parts

    # A literal address needs no DNS; a name is resolved and EVERY answer must be public.
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            if resolver is not None:
                addresses = list(resolver(host, port))
            else:
                infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                addresses = [info[4][0] for info in infos]
        except (OSError, UnicodeError):
            raise GuardError("could not resolve %s" % host)
    if not addresses:
        raise GuardError("could not resolve %s" % host)
    for ip in addresses:
        if not _is_public(ip):
            raise GuardError(
                "%s points at a private or local address (%s); refused. If that is intended, "
                "add it to allow_private_hosts with dl_settings." % (host, ip))
    return parts


def mask_url(url):
    """A URL that is safe to show: no credentials, and the query string hidden (it is where
    tokens live)."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return str(url or "")[:80]
    host = parts.hostname
    if ":" in host:
        host = "[%s]" % host
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host + (":%d" % port if port else "")
    text = "%s://%s%s" % (parts.scheme, netloc, parts.path or "")
    if parts.query:
        text += "?…"
    return text[:300]
