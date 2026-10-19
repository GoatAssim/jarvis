"""The Stremio streaming server's HTTP API (plan sections 2, 9.2 and 0).

The server is closed-source and undocumented, so every call here is defensive: a short
timeout, `trust_env=False` (a proxy variable must never route loopback traffic away), JSON
that might not be JSON, and one `ServerError` carrying a sentence the owner can act on.

Endpoints used (all confirmed by the owner's 2026-10-18 experiments, section 0):
    GET  /settings                    values.serverVersion, cacheRoot, cacheSize, bt* ...
    POST /settings                    the whole `values` object back (core's own round trip)
    POST /<hash>/create               registers a torrent; answers with its stats
    POST /create  {"blob": "<hex>"}   registers a .torrent file's bytes
    GET  /<hash>/stats.json           files[], peers, downloaded, downloadSpeed ...  (null when unknown)
    GET  /stats.json                  every registered torrent, keyed by infohash
"""

import socket
import threading
import time
from urllib.parse import urlsplit

from . import config, links

DEFAULT_TIMEOUT = 5.0
PROBE_TIMEOUT = 1.5
_PROBE_TTL = 3.0


class ServerError(Exception):
    """A call to the streaming server failed; the message is safe to show the owner."""


class ServerBusy(ServerError):
    """The server did not answer in time.  For `create` this is NOT a failure: a torrent with no
    peers keeps the request open, yet it is registered (section 0, experiment 2)."""


class StremioServer:
    def __init__(self, base_url=None, session=None, timeout=DEFAULT_TIMEOUT):
        self.base = (base_url or config.server_url()).rstrip("/")
        self.timeout = timeout
        self._session = session

    # ------------------------------------------------------------------ plumbing

    def _sess(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.trust_env = False            # loopback never goes through a proxy
            self._session.headers["User-Agent"] = "Jarvis-Hub/1"
        return self._session

    def close(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    def _request(self, method, path, timeout=None, **kwargs):
        import requests
        url = self.base + path
        try:
            resp = self._sess().request(method, url, timeout=timeout or self.timeout, allow_redirects=False, **kwargs)
        except requests.exceptions.ReadTimeout:
            raise ServerBusy("the Stremio server didn't answer %s in time" % path.split("?")[0][:40])
        except requests.exceptions.ConnectionError:
            raise ServerError("the Stremio server isn't answering at %s (is it running? try stack_start)" % self.base)
        except requests.exceptions.RequestException as exc:
            raise ServerError("couldn't reach the Stremio server: %s" % type(exc).__name__)
        return resp

    def _json(self, method, path, timeout=None, **kwargs):
        resp = self._request(method, path, timeout=timeout, **kwargs)
        try:
            if resp.status_code >= 400:
                raise ServerError("the Stremio server answered HTTP %d for %s" % (resp.status_code, path.split("?")[0][:40]))
            try:
                return resp.json()
            except ValueError:
                raise ServerError("the Stremio server's answer to %s wasn't JSON" % path.split("?")[0][:40])
        finally:
            resp.close()

    # ------------------------------------------------------------------ settings

    def settings(self, timeout=None):
        """{'values': {...}, 'options': [...]} from GET /settings."""
        data = self._json("GET", "/settings", timeout=timeout)
        if not isinstance(data, dict) or not isinstance(data.get("values"), dict):
            raise ServerError("the Stremio server's /settings has an unexpected shape")
        return data

    def values(self, timeout=None):
        return self.settings(timeout)["values"]

    def version(self, timeout=None):
        return str(self.values(timeout).get("serverVersion") or "unknown")[:40]

    def is_up(self, timeout=PROBE_TIMEOUT):
        """(True, version) or (False, reason)."""
        try:
            return True, self.version(timeout)
        except ServerError as exc:
            return False, str(exc)

    def set_settings(self, changes, timeout=None):
        """Read-modify-write, the way stremio-core does it, then read back and compare.

        Returns {'ok', 'changed': {key: [old, new]}, 'mismatch': {key: seen}}.  Only keys the
        server already has are written (a typo can't invent a setting)."""
        current = self.values(timeout)
        merged = dict(current)
        changed = {}
        for key, value in changes.items():
            if key not in current:
                raise ServerError("this Stremio server has no setting called %r" % str(key)[:40])
            if current[key] != value:
                changed[key] = [current[key], value]
            merged[key] = value
        if not changed:
            return {"ok": True, "changed": {}, "mismatch": {}}
        self._json("POST", "/settings", timeout=timeout, json=merged)
        after = self.values(timeout)
        mismatch = {k: after.get(k) for k in changed if after.get(k) != changes[k]}
        return {"ok": not mismatch, "changed": changed, "mismatch": mismatch}

    # ------------------------------------------------------------------ torrents

    def create(self, infohash, trackers=(), timeout=(5, 15)):
        """Register a torrent.  Returns its stats dict.  Raises ServerBusy when the server keeps
        the request open (no peers) -- the torrent is registered anyway; poll `stats()`."""
        digest = links.normalize_infohash(infohash)
        if not digest:
            raise ServerError("that isn't an infohash")
        data = self._json("POST", "/%s/create" % digest, timeout=timeout, json=links.create_body(digest, trackers))
        return data if isinstance(data, dict) else {}

    def create_from_blob(self, torrent_bytes, timeout=(5, 30)):
        """Register a .torrent file (hex in a JSON body).  Returns the stats dict, which carries
        the infohash."""
        data = self._json("POST", "/create", timeout=timeout, json={"blob": bytes(torrent_bytes).hex()})
        return data if isinstance(data, dict) else {}

    def stats(self, infohash, timeout=None):
        """The torrent's stats dict, or None when the server doesn't know it."""
        digest = links.normalize_infohash(infohash)
        if not digest:
            return None
        data = self._json("GET", "/%s/stats.json" % digest, timeout=timeout)
        return data if isinstance(data, dict) and data else None

    def all_stats(self, timeout=None):
        data = self._json("GET", "/stats.json", timeout=timeout)
        return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- helpers

def default():
    return StremioServer()


_probe_lock = threading.Lock()
_probe_cache = {"key": None, "at": 0.0, "result": (False, "not checked")}


def probe(base_url=None, max_age=_PROBE_TTL):
    """(up, version-or-reason), cached for a few seconds so a router decision, a doctor run and a
    worker start don't each wait on the network."""
    base = (base_url or config.server_url()).rstrip("/")
    with _probe_lock:
        now = time.monotonic()
        if _probe_cache["key"] == base and now - _probe_cache["at"] < max_age:
            return _probe_cache["result"]
    result = StremioServer(base).is_up()
    with _probe_lock:
        _probe_cache.update(key=base, at=time.monotonic(), result=result)
    return result


def forget_probe():
    with _probe_lock:
        _probe_cache.update(key=None, at=0.0)


def port_answers(url, timeout=1.0):
    """A bare TCP connect to the URL's host:port (is anything listening?)."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname or "127.0.0.1", parts.port or 80
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def active_torrents(stats_by_hash):
    """Hashes whose download speed is non-zero right now: the heuristic for 'something is playing
    or downloading' (plan H8)."""
    out = []
    for digest, stat in (stats_by_hash or {}).items():
        try:
            if isinstance(stat, dict) and float(stat.get("downloadSpeed") or 0) > 0:
                out.append(str(digest))
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- Jarvis daemons

SERVER_DAEMON = "stremio_stack-server"
WEB_DAEMON = "stremio_stack-web"


def daemon_state(daemon_id):
    """A one-word state plus detail for one of our registered daemons: {'registered', 'running', 'status', 'pid'}."""
    try:
        from ... import daemons
        entry = daemons.get(daemon_id)
        if not entry:
            return {"registered": False, "running": False, "status": "not registered", "pid": None}
        state = daemons.status(daemon_id)
        return {"registered": True, "running": bool(state.get("running")), "status": state.get("status") or "",
                "pid": state.get("pid"), "enabled": bool(entry.get("enabled", True))}
    except Exception as exc:  # noqa: BLE001
        return {"registered": False, "running": False, "status": "unknown: %s" % type(exc).__name__, "pid": None}
