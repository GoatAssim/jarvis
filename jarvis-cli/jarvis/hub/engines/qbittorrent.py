"""The qBittorrent engine (plan section 7.4, phase 9): torrents moved by qBittorrent, shown by the hub.

qBittorrent does the transfer (its piece picking, its hash-checked resume, its peers); this engine
only adds the torrent through qBittorrent's Web API, watches it, and reports the numbers.  It sits
behind the same engine contract as `http` and `ytdlp`, so the tools and the panel did not change.

SETUP (nothing here is on until the owner does it)
  1. qBittorrent > Tools > Options > Web UI: switch "Web User Interface" on, keep it on 127.0.0.1.
  2. Create `~/.jarvis/qbittorrent.json` (the same shape as `~/.jarvis/stremio.json`):

         {
           "address": "http://127.0.0.1:8080",
           "username": "admin",
           "password": "the Web UI password",
           "prefer_for_magnets": false
         }

     Every other key is optional (defaults in DEFAULT_CONFIG): `category`, `tag` ("jarvis"),
     `save_subfolder` ("Torrents", a plain sub-folder of the library root), `after_done`
     ("keep" | "pause" | "remove"), `cancel_deletes_data`, `metadata_timeout_s`, `stall_minutes`,
     `poll_s`, `enabled`.  If the Web UI has "Bypass authentication for clients on localhost"
     switched on, leave username and password out.

SAFETY (plan section 14)
  * the password lives only in that file: never in a tool argument, a log line, an event or a
    result; every message this module builds is free of it;
  * the Web UI must be on this PC (127.0.0.1 / localhost / ::1): the engine tells qBittorrent where
    to write, and that only makes sense for a local one.  Redirects are never followed;
  * the magnet qBittorrent receives is REBUILT here from the infohash, the display name and the
    trackers that pass a check -- `xs=`, `ws=`, `as=` (addresses qBittorrent would fetch) are dropped,
    and so is any tracker that points at this PC or the local network;
  * everything is saved under the library root (`save_subfolder`, or the download's own `subdir` /
    `dest_abs` option, exactly like the other engines); a path qBittorrent reports back is only
    stored when it sits inside a folder the hub may write to;
  * the hub only ever removes a torrent it added itself, and only deletes data of a CANCELLED,
    incomplete torrent inside the library root (the same promise as a cancelled `.part`).  A torrent
    that was already in qBittorrent is paused on cancel, never removed.

ROUTING
  `dl_add(engine="qbittorrent")` always works for a magnet link or a 40-hex infohash.  By default this
  engine does NOT take magnets for itself (the Stremio engine does); set `"prefer_for_magnets": true`
  to make it the default for magnets while qBittorrent is reachable (when it is not, the magnet falls
  through to the next engine instead of failing).

WHAT THE ENGINE REPORTS
  percent / bytes / speed / ETA / peers / seeds come from qBittorrent itself (not derived by the hub);
  `phase` says what it is doing ("fetching metadata", "checking files", "waiting for peers" ...);
  the detail drawer gets the info hash, qBittorrent's version and the save path; the torrent's files
  appear as children.

LIFE CYCLE
  * the infohash is saved in the row's `source_ref`, so a restart of the daemon (or a resume after a
    pause) re-attaches to the same torrent: no duplicate, no restart from zero;
  * pause -> qBittorrent stops the torrent; resume -> it is started again;
  * shutdown -> `Outcome.requeue()`; the torrent keeps running inside qBittorrent;
  * done -> the row finishes; qBittorrent keeps seeding under its own ratio rules unless `after_done`
    says otherwise.
"""

import base64
import binascii
import ipaddress
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from .. import naming, paths, states
from .. import settings as settings_mod
from .base import Engine, Outcome, RouteRule

CONFIG_NAME = "qbittorrent.json"
DEFAULT_CONFIG = {
    "enabled": True,
    "address": "http://127.0.0.1:8080",
    "username": "",
    "password": "",
    "category": "",
    "tag": "jarvis",
    "save_subfolder": "Torrents",
    "after_done": "keep",
    "prefer_for_magnets": False,
    "cancel_deletes_data": True,
    "metadata_timeout_s": 900,
    "stall_minutes": 0,
    "poll_s": 2.0,
}
AFTER_DONE = ("keep", "pause", "remove")
MIN_POLL_S = 0.5               # tests lower this; a real config is never polled faster
MIN_METADATA_S = 60            # likewise
MAX_POLL_FAILS = 5             # consecutive failed polls before the run gives up (retryable)
CHILDREN_EVERY_S = 10.0
MAX_TRACKERS = 20
FREE_SPACE_MARGIN = 1.10       # plan section 9.3: need 10 % more than the torrent
ETA_NEVER = 8640000            # qBittorrent's "infinity"
AVAILABLE_TTL_S = 5.0

# qBittorrent torrent states (Web API 2.x; 5.x renamed paused* to stopped*)
_UP_STATES = frozenset(["uploading", "stalledUP", "queuedUP", "forcedUP", "checkingUP",
                        "pausedUP", "stoppedUP"])
_PAUSED_STATES = frozenset(["pausedDL", "pausedUP", "stoppedDL", "stoppedUP"])
_META_STATES = frozenset(["metaDL", "forcedMetaDL"])
_CHECK_STATES = frozenset(["checkingDL", "checkingUP", "checkingResumeData"])
_ERROR_STATES = frozenset(["error", "missingFiles"])

_BTIH = re.compile(r"^urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})$")
_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")


def _short(exc):
    return states.clean_text("%s: %s" % (type(exc).__name__, exc), 160)


def _sleep(seconds):
    time.sleep(seconds)


# --------------------------------------------------------------------------- config

def config_path():
    return paths.jarvis_dir() / CONFIG_NAME


def _is_loopback(host):
    host = (host or "").lower()
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def parse_address(text):
    """(base_url, None) for a usable Web UI address, or (None, what is wrong)."""
    raw = str(text or "").strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None, "address in %s isn't a valid URL" % CONFIG_NAME
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None, "address in %s must look like http://127.0.0.1:8080" % CONFIG_NAME
    if parts.username or parts.password:
        return None, "put the user name and password in their own fields in %s, not in the address" % CONFIG_NAME
    if parts.query or parts.fragment:
        return None, "address in %s must not have a query or a fragment" % CONFIG_NAME
    if not _is_loopback(host):
        return None, ("the qBittorrent Web UI must be on this PC (127.0.0.1 or localhost); %s is not; "
                      "the engine tells it where to save files, which only works locally" % states.clean_text(host, 60))
    netloc = ("[%s]" % host) if ":" in host else host
    if port:
        netloc += ":%d" % port
    return "%s://%s%s" % (scheme, netloc, parts.path.rstrip("/")), None


def _clean_config(raw):
    cfg = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg
    for key in ("address", "username", "category", "tag", "save_subfolder"):
        value = raw.get(key)
        if isinstance(value, str):
            cfg[key] = value.strip()[:300]
    if isinstance(raw.get("password"), str):
        cfg["password"] = raw["password"][:200]          # not stripped: spaces can be part of it
    for key in ("enabled", "prefer_for_magnets", "cancel_deletes_data"):
        if isinstance(raw.get(key), bool):
            cfg[key] = raw[key]
    if raw.get("after_done") in AFTER_DONE:
        cfg["after_done"] = raw["after_done"]
    for key, lo, hi in (("metadata_timeout_s", MIN_METADATA_S, 86400), ("stall_minutes", 0, 10080), ("poll_s", MIN_POLL_S, 30)):
        value = states.clean_number(raw.get(key))
        if value is not None:
            cfg[key] = max(lo, min(hi, value))
    if cfg["poll_s"] < MIN_POLL_S:
        cfg["poll_s"] = MIN_POLL_S
    return cfg


def load_config(path=None):
    """(config, None) or (None, a one-line problem the owner can act on).  Never raises, and the
    problem never contains the password."""
    target = Path(path) if path else config_path()
    if not target.exists():
        return None, "not set up: create %s with the Web UI address (see the top of engines/qbittorrent.py)" % target
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return None, "can't read %s: %s" % (CONFIG_NAME, type(exc).__name__)
    cfg = _clean_config(raw)
    if isinstance(raw, dict) and raw.get("enabled") is False:
        return None, "switched off in %s (\"enabled\": false)" % CONFIG_NAME
    base, problem = parse_address(cfg["address"])
    if problem:
        return None, problem
    cfg["base"] = base
    return cfg, None


# --------------------------------------------------------------------------- magnets

def _tracker_ok(url):
    """http / https / udp trackers only, never one that points at this PC or the local network."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return False
    if (parts.scheme or "").lower() not in ("http", "https", "udp") or not host:
        return False
    if parts.username or parts.password or len(url) > 400:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:                                   # a name: no DNS here, but obvious local names go
        return "." in host and not host.endswith((".local", ".lan", ".internal", ".localdomain", ".home"))
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return bool(addr.is_global) and not addr.is_multicast


def parse_magnet(text):
    """A magnet link or a bare 40-hex infohash -> {infohash, name, trackers, magnet} or None.

    `magnet` is rebuilt from scratch (infohash, name, checked trackers): nothing else in the
    original survives, so qBittorrent is never handed an address to fetch (`xs=`, `ws=`, `as=`)."""
    raw = str(text if text is not None else "").strip()
    if not raw or len(raw) > 4096:
        return None
    name = None
    trackers = []
    if _HEX40.match(raw):
        digest = raw.lower()
    else:
        try:
            parts = urlsplit(raw)
        except ValueError:
            return None
        if (parts.scheme or "").lower() != "magnet":
            return None
        query = parse_qs(parts.query, keep_blank_values=False)
        digest = None
        for value in query.get("xt", []):
            m = _BTIH.match(value.strip())
            if not m:
                continue
            code = m.group(1)
            if len(code) == 40:
                digest = code.lower()
            else:
                try:
                    digest = binascii.hexlify(base64.b32decode(code.upper())).decode("ascii")
                except (binascii.Error, ValueError):
                    continue
            break
        if not digest:
            return None
        if query.get("dn"):
            name = states.clean_text(query["dn"][0], 200) or None
        for url in query.get("tr", []):
            url = url.strip()
            if url not in trackers and _tracker_ok(url):
                trackers.append(url)
            if len(trackers) >= MAX_TRACKERS:
                break
    magnet = "magnet:?xt=urn:btih:%s" % digest
    if name:
        magnet += "&dn=%s" % quote(name, safe="")
    for url in trackers:
        magnet += "&tr=%s" % quote(url, safe="")
    return {"infohash": digest, "name": name, "trackers": trackers, "magnet": magnet}


# --------------------------------------------------------------------------- the Web API

class QbError(Exception):
    """`kind`: down (not reachable), auth (login needed or refused), api (it answered, but oddly)."""

    def __init__(self, kind, message):
        Exception.__init__(self, message)
        self.kind = kind


class Client:
    """A small qBittorrent Web API v2 client.  One instance per run; never logs the password."""

    def __init__(self, base, username="", password="", timeout=(3.0, 10.0), session=None):
        import requests
        self.base = base
        self.username = username or ""
        self._password = password or ""
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.trust_env = False                    # no system proxy for a loopback service
        origin = "%s://%s" % (urlsplit(base).scheme, urlsplit(base).netloc)
        # qBittorrent's CSRF check wants a Referer / Origin that matches the Host it was sent to.
        self._headers = {"Referer": base + "/", "Origin": origin, "User-Agent": "Jarvis-Hub/1"}
        self._modern = None                               # True: stop/start (5.x); False: pause/resume (4.x)

    def close(self):
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass

    # -- plumbing ---------------------------------------------------------------
    def _call(self, method, path, params=None, fields=None, _retry=True):
        import requests
        kwargs = {"headers": self._headers, "timeout": self.timeout, "allow_redirects": False}
        if params:
            kwargs["params"] = params
        if fields is not None:
            kwargs["files"] = {k: (None, str(v)) for k, v in fields.items()}
        try:
            resp = self.session.request(method, "%s/api/v2/%s" % (self.base, path), **kwargs)
        except requests.exceptions.RequestException:
            raise QbError("down", "can't reach qBittorrent at %s (is it running, with its Web UI switched on?)"
                          % self.base) from None
        if resp.status_code in (401, 403) and path != "auth/login":
            if self.username and _retry:
                self.login()
                return self._call(method, path, params, fields, _retry=False)
            raise QbError("auth", "qBittorrent wants a login: put username and password in %s "
                                  "(or switch on \"Bypass authentication for clients on localhost\")" % CONFIG_NAME)
        return resp

    @staticmethod
    def _ok(resp):
        if resp.status_code != 200:
            raise QbError("api", "qBittorrent answered HTTP %d" % resp.status_code)
        return resp

    def login(self):
        import requests
        try:
            resp = self.session.post("%s/api/v2/auth/login" % self.base, headers=self._headers,
                                     data={"username": self.username, "password": self._password},
                                     timeout=self.timeout, allow_redirects=False)
        except requests.exceptions.RequestException:
            raise QbError("down", "can't reach qBittorrent at %s (is it running, with its Web UI switched on?)"
                          % self.base) from None
        body = (resp.text or "").strip()
        if resp.status_code == 200 and body == "Ok.":
            return
        if resp.status_code == 403:
            raise QbError("auth", "qBittorrent has banned this address after too many failed logins "
                                  "(wait a while, then check the password in %s)" % CONFIG_NAME)
        if resp.status_code == 200:
            raise QbError("auth", "qBittorrent rejected the user name or password in %s" % CONFIG_NAME)
        raise QbError("api", "qBittorrent's login answered HTTP %d" % resp.status_code)

    # -- reading ----------------------------------------------------------------
    def version(self):
        return (self._ok(self._call("GET", "app/version")).text or "").strip()[:30]

    def web_api_version(self):
        return (self._ok(self._call("GET", "app/webapiVersion")).text or "").strip()[:20]

    def _json(self, resp):
        try:
            return self._ok(resp).json()
        except ValueError:
            raise QbError("api", "qBittorrent sent an answer that isn't JSON") from None

    def info(self, infohash):
        rows = self._json(self._call("GET", "torrents/info", params={"hashes": infohash}))
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and str(row.get("hash", "")).lower() == infohash:
                return row
        return None

    def tagged_count(self, tag):
        rows = self._json(self._call("GET", "torrents/info", params={"tag": tag}))
        return len(rows) if isinstance(rows, list) else 0

    def files(self, infohash):
        rows = self._json(self._call("GET", "torrents/files", params={"hash": infohash}))
        return rows if isinstance(rows, list) else []

    # -- acting -----------------------------------------------------------------
    def add(self, magnet, savepath, category="", tag=""):
        fields = {"urls": magnet, "savepath": savepath, "autoTMM": "false",
                  "paused": "false", "stopped": "false"}          # 4.x says paused, 5.x says stopped
        if category:
            fields["category"] = category
        if tag:
            fields["tags"] = tag
        return (self._ok(self._call("POST", "torrents/add", fields=fields)).text or "").strip()

    def _verb(self, modern, legacy, infohash):
        order = (modern, legacy) if self._modern is not False else (legacy, modern)
        resp = self._call("POST", "torrents/" + order[0], fields={"hashes": infohash})
        if resp.status_code == 404:                              # the other generation of qBittorrent
            resp = self._call("POST", "torrents/" + order[1], fields={"hashes": infohash})
            used = order[1]
        else:
            used = order[0]
        if resp.status_code == 200:
            self._modern = (used == modern)
        self._ok(resp)

    def stop(self, infohash):
        self._verb("stop", "pause", infohash)

    def start(self, infohash):
        self._verb("start", "resume", infohash)

    def delete(self, infohash, delete_files):
        self._ok(self._call("POST", "torrents/delete",
                            fields={"hashes": infohash, "deleteFiles": "true" if delete_files else "false"}))

    def set_download_limit(self, infohash, bytes_per_s):
        self._ok(self._call("POST", "torrents/setDownloadLimit",
                            fields={"hashes": infohash, "limit": int(bytes_per_s) if bytes_per_s > 0 else -1}))


# --------------------------------------------------------------------------- helpers for one run

def _num(value):
    num = states.clean_number(value)
    return None if num is None else int(num)


def _classify(t):
    """What a torrent row is doing, as (kind, phase label).  kind: error | complete | paused |
    metadata | queued | checking | moving | stalled | active."""
    state = str(t.get("state") or "")
    progress = states.clean_number(t.get("progress"), 0.0, 1.0) or 0.0
    if state in _ERROR_STATES:
        return "error", state
    if state == "moving":
        return "moving", "moving files"
    if progress >= 1.0 or state in _UP_STATES:
        return "complete", None
    if state in _PAUSED_STATES:
        return "paused", "paused in qBittorrent"
    if state in _META_STATES:
        return "metadata", "fetching metadata"
    if state == "queuedDL":
        return "queued", "queued in qBittorrent"
    if state in _CHECK_STATES:
        return "checking", "checking files"
    if state == "stalledDL":
        return "stalled", "waiting for peers"
    if state == "allocating":
        return "active", "allocating disk space"
    return "active", None


def _report_fields(t, kind, phase):
    size = _num(t.get("size")) or 0
    progress = states.clean_number(t.get("progress"), 0.0, 1.0) or 0.0
    fields = {"phase": phase}
    if size > 0 and kind != "metadata":
        done = _num(t.get("completed"))
        fields["bytes_total"] = size
        fields["bytes_done"] = min(size, done if done is not None else int(size * progress))
        fields["percent"] = progress * 100.0
    else:
        fields["percent"] = None                          # no size yet: an indeterminate bar
    speed = states.clean_number(t.get("dlspeed"))
    eta = states.clean_number(t.get("eta"))
    active = kind in ("active", "stalled")
    fields["speed_bps"] = speed if active else None
    fields["eta_s"] = eta if (active and eta is not None and eta < ETA_NEVER) else None
    seeds = _num(t.get("num_seeds"))
    leech = _num(t.get("num_leechs"))
    if seeds is not None:
        fields["seeds"] = seeds
    if seeds is not None or leech is not None:
        fields["peers"] = (seeds or 0) + (leech or 0)      # connected peers, seeds included
    return fields


def _children(rows):
    out = []
    for row in rows[: states.MAX_CHILDREN]:
        if not isinstance(row, dict):
            continue
        size = _num(row.get("size"))
        progress = states.clean_number(row.get("progress"), 0.0, 1.0) or 0.0
        child = {"name": row.get("name")}
        if size is not None:
            child["bytes_total"] = size
            child["bytes_done"] = int(size * progress)
        child["percent"] = progress * 100.0
        out.append(child)
    return states.clean_children(out)


def _dest_dir(ctx, opts, cfg):
    """The folder qBittorrent is told to save into, or None when the owner's choice isn't allowed."""
    if opts.get("dest_abs") or opts.get("subdir"):
        folder, _source = settings_mod.resolve_dest_dir(ctx.settings, "", "file", opts)
        return folder
    return naming.safe_subdir(ctx.library_root(), cfg.get("save_subfolder", ""))


def _inside(roots, path):
    return bool(path) and naming.under_any(roots, path)


class _Stop(Exception):
    """Internal: the loop ends with this Outcome."""

    def __init__(self, outcome):
        Exception.__init__(self, outcome.kind)
        self.outcome = outcome


# --------------------------------------------------------------------------- the engine

class QBittorrentEngine(Engine):
    name = "qbittorrent"
    label = "qBittorrent"
    supports_pause = True

    def __init__(self):
        self._avail = None            # (checked_at, config_mtime, (ok, reason))

    # -- availability / doctor ---------------------------------------------------
    def available(self):
        try:
            import requests  # noqa: F401
        except ImportError:
            return False, "the `requests` package is missing"
        target = config_path()
        try:
            mtime = target.stat().st_mtime
        except OSError:
            mtime = None
        now = time.monotonic()
        if self._avail and now - self._avail[0] < AVAILABLE_TTL_S and self._avail[1] == mtime:
            return self._avail[2]
        result = self._probe()
        self._avail = (now, mtime, result)
        return result

    def _probe(self):
        cfg, problem = load_config()
        if problem:
            return False, problem
        client = Client(cfg["base"], cfg["username"], cfg["password"], timeout=(2.0, 4.0))
        try:
            client.version()
            return True, ""
        except QbError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, "check failed: %s" % _short(exc)
        finally:
            client.close()

    def doctor(self):
        cfg, problem = load_config()
        if problem:
            return []                                   # `available()` already says it, once
        client = Client(cfg["base"], cfg["username"], cfg["password"], timeout=(2.0, 4.0))
        try:
            version = client.version()
            api = client.web_api_version()
            mine = client.tagged_count(cfg["tag"]) if cfg["tag"] else 0
        except QbError as exc:
            return [{"name": "qbittorrent web api", "status": "warn", "detail": str(exc)}]
        finally:
            client.close()
        detail = "qBittorrent %s (Web API %s) at %s; %d torrent(s) tagged %r" % (
            version or "?", api or "?", cfg["base"], mine, cfg["tag"])
        if cfg["prefer_for_magnets"]:
            detail += "; preferred for magnet links"
        return [{"name": "qbittorrent web api", "status": "ok", "detail": detail}]

    # -- the run ----------------------------------------------------------------
    def run(self, item, ctx):
        try:
            return self._run(item, ctx)
        except _Stop as stop:
            return stop.outcome
        except QbError as exc:
            if exc.kind == "down":
                return Outcome.failed(str(exc), retryable=True)
            return Outcome.failed(str(exc))
        except Exception as exc:  # noqa: BLE001 - an engine never raises past run
            return Outcome.failed("unexpected error: %s" % _short(exc))

    def _run(self, item, ctx):
        cfg, problem = load_config()
        if problem:
            return Outcome.failed("the qBittorrent engine isn't set up: %s" % problem)
        parsed = parse_magnet(item.get("source_url"))
        if parsed is None:
            return Outcome.failed("the qBittorrent engine takes magnet links and 40-character info hashes only")
        infohash = parsed["infohash"]
        opts = item.get("options") or {}
        roots = settings_mod.write_roots(ctx.settings, opts)
        ref = dict(item.get("source_ref") or {})
        if ref.get("infohash") != infohash:
            ref = {}                                    # a leftover from some other source is never trusted

        ctx.set_state("connecting", phase="contacting qBittorrent")
        client = Client(cfg["base"], cfg["username"], cfg["password"])
        try:
            version = client.version()
            torrent = client.info(infohash)
            if torrent is None:
                torrent, ref = self._add(ctx, client, cfg, parsed, opts, ref)
            ref.setdefault("added_by_hub", False)
            ref["infohash"] = infohash
            ctx.update(source_ref=ref, kind="torrent")
            save_path = str(torrent.get("save_path") or ref.get("save_path") or "")
            detail = [{"label": "Info hash", "value": infohash}, {"label": "qBittorrent", "value": version}]
            if save_path:
                detail.append({"label": "Save path", "value": save_path})
            ctx.update(detail=states.clean_detail(detail))
            if str(torrent.get("state") or "") in _PAUSED_STATES and _classify(torrent)[0] != "complete":
                client.start(infohash)
                torrent = client.info(infohash) or torrent
            return self._track(ctx, client, cfg, infohash, ref, roots, torrent)
        finally:
            client.close()

    def _add(self, ctx, client, cfg, parsed, opts, ref):
        folder = _dest_dir(ctx, opts, cfg)
        if folder is None:
            raise _Stop(Outcome.failed("the destination folder isn't allowed (a sub-folder of the library root, "
                                       "or a full path you approved); check save_subfolder in %s" % CONFIG_NAME))
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _Stop(Outcome.failed("can't create %s: %s" % (folder, _short(exc))))
        infohash = parsed["infohash"]
        answer = client.add(parsed["magnet"], str(folder), cfg["category"], cfg["tag"])
        torrent = None
        for _ in range(20):                              # it shows up within a moment
            torrent = client.info(infohash)
            if torrent is not None:
                break
            _sleep(0.5)
        if torrent is None:
            raise _Stop(Outcome.failed("qBittorrent didn't accept that magnet link (it answered %r)"
                                       % states.clean_text(answer, 40), retryable=(answer == "Ok.")))
        ctx.event("info", "added to qBittorrent (%s)" % infohash[:8])
        ref = {"infohash": infohash, "added_by_hub": True, "save_path": str(folder)}
        return torrent, ref

    def _wait(self, ctx, seconds):
        """Sleep in small slices so a pause or cancel is answered quickly.  Returns the stop reason."""
        end = time.monotonic() + seconds
        while True:
            stop = ctx.stop_requested()
            if stop:
                return stop
            left = end - time.monotonic()
            if left <= 0:
                return None
            _sleep(min(0.2, left))

    def _track(self, ctx, client, cfg, infohash, ref, roots, torrent):
        started = time.monotonic()
        missing = fails = 0
        last_state = None
        last_bytes = None
        last_move = started
        last_children = float("-inf")
        last_title = None
        applied_limit = 0
        checked_space = False
        running_seen = False

        while True:
            stop = ctx.stop_requested()
            if stop:
                return self._stopped(ctx, client, cfg, infohash, ref, roots, stop, torrent)

            if torrent is None:                              # every pass starts with one fresh look
                try:
                    torrent = client.info(infohash)
                    fails = 0
                except QbError as exc:
                    if exc.kind == "auth":
                        raise
                    fails += 1
                    if fails >= MAX_POLL_FAILS:
                        return Outcome.failed("lost the connection to qBittorrent: %s" % exc, retryable=True)
                    self._wait(ctx, cfg["poll_s"])
                    continue
                if torrent is None:
                    missing += 1
                    if missing >= 3:                         # not one odd empty answer: really gone
                        ctx.event("warn", "the torrent is gone from qBittorrent; treating that as a cancel")
                        return Outcome.cancelled()
                    self._wait(ctx, cfg["poll_s"])
                    continue
            missing = 0
            t, torrent = torrent, None
            now = time.monotonic()
            kind, phase = _classify(t)

            # A torrent the owner paused inside qBittorrent: the row follows.  A short grace at the
            # start, because qBittorrent's state can lag a moment behind our own `start` call.
            if kind == "paused":
                if running_seen or now - started > 10:
                    ctx.event("info", "paused in qBittorrent")
                    return Outcome.paused()
            else:
                running_seen = True

            if kind == "error":
                return self._fail_and_stop(client, infohash,
                                           "qBittorrent reports an error for this torrent (%s); often a full disk "
                                           "or a locked file: look at its log" % t.get("state"))
            if kind == "complete":
                return self._finish(ctx, client, cfg, infohash, ref, roots, t)

            name = states.clean_text(t.get("name"), states.MAX_TITLE)
            if name and name != last_title and name.lower() != infohash:
                last_title = name
                ctx.update(title=name)

            fields = _report_fields(t, kind, phase)
            if not ctx.update(**fields):                     # the row was deleted under us
                return self._stopped(ctx, client, cfg, infohash, ref, roots, "cancel", t)

            hub_state = {"metadata": "connecting", "queued": "connecting",
                         "checking": "postprocessing", "moving": "postprocessing"}.get(kind, "downloading")
            if hub_state != last_state:
                ctx.set_state(hub_state)
                last_state = hub_state

            done = fields.get("bytes_done")
            if done is not None and done != last_bytes:
                last_bytes, last_move = done, now
            if not checked_space and fields.get("bytes_total"):
                checked_space = True
                bad = self._space_problem(t, ref, fields["bytes_total"] - (fields.get("bytes_done") or 0))
                if bad:
                    return self._fail_and_stop(client, infohash, bad)
            if kind == "metadata" and now - started > cfg["metadata_timeout_s"]:
                return self._fail_and_stop(client, infohash,
                                           "couldn't fetch the torrent's file list in %g minutes (no peers?)"
                                           % round(cfg["metadata_timeout_s"] / 60.0, 1))
            if cfg["stall_minutes"] and kind in ("active", "stalled") and now - last_move > cfg["stall_minutes"] * 60:
                return self._fail_and_stop(client, infohash, "no data for %g minutes" % cfg["stall_minutes"])

            limit = ctx.rate_limit_bps()
            if limit != applied_limit:
                applied_limit = limit                        # one try per change; not worth hammering
                try:
                    client.set_download_limit(infohash, limit)
                except QbError:
                    pass

            if kind in ("active", "stalled") and now - last_children >= CHILDREN_EVERY_S:
                last_children = now
                try:
                    ctx.update(children=_children(client.files(infohash)))
                except QbError:
                    pass

            stop = self._wait(ctx, cfg["poll_s"])
            if stop:
                return self._stopped(ctx, client, cfg, infohash, ref, roots, stop, t)

    # -- endings ------------------------------------------------------------------
    def _space_problem(self, t, ref, left):
        folder = t.get("save_path") or ref.get("save_path")
        if not folder or left <= 0:
            return None
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            return None
        need = int(left * FREE_SPACE_MARGIN)
        if free < need:
            return "not enough free space in %s: needs %.1f GB, %.1f GB free" % (
                states.clean_text(folder, 80), need / 1024 ** 3, free / 1024 ** 3)
        return None

    def _fail_and_stop(self, client, infohash, message):
        """A failure the hub will not retry by itself: stop the torrent so nothing keeps running
        behind a red row (a manual retry starts it again)."""
        try:
            client.stop(infohash)
        except QbError:
            pass
        return Outcome.failed(message)

    def _stopped(self, ctx, client, cfg, infohash, ref, roots, stop, t):
        if stop == "pause":
            try:
                client.stop(infohash)
            except QbError as exc:
                ctx.event("warn", "couldn't pause it in qBittorrent: %s" % exc)
            return Outcome.paused()
        if stop == "cancel":
            self._cancel_cleanup(ctx, client, cfg, infohash, ref, roots, t)
            return Outcome.cancelled()
        return Outcome.requeue()                         # shutdown: the torrent keeps running in qBittorrent

    def _cancel_cleanup(self, ctx, client, cfg, infohash, ref, roots, t):
        try:
            if not ref.get("added_by_hub"):
                client.stop(infohash)                    # the owner's own torrent: pause it, never remove it
                ctx.event("info", "it was already in qBittorrent, so it was only paused there")
                return
            current = client.info(infohash) or t or {}
            complete = _classify(current)[0] == "complete"
            where = current.get("save_path") or ref.get("save_path")
            wipe = bool(cfg["cancel_deletes_data"]) and not complete and _inside(roots, where)
            client.delete(infohash, wipe)
            ctx.event("info", "removed from qBittorrent%s" % (" with its partial data" if wipe else ""))
        except QbError as exc:
            ctx.event("warn", "couldn't clean up in qBittorrent (%s); the torrent may still be there "
                              "(tag %r)" % (exc, cfg.get("tag")))

    def _finish(self, ctx, client, cfg, infohash, ref, roots, t):
        size = _num(t.get("size")) or _num(t.get("total_size"))
        fields = {}
        if size:
            fields["bytes_total"] = size
            fields["bytes_done"] = size
        where = str(t.get("save_path") or "")
        content = str(t.get("content_path") or "")
        if not content and where and t.get("name"):
            content = str(Path(where) / naming.sanitize_filename(t.get("name"), default="torrent"))
        if _inside(roots, where):
            fields["dest_dir"] = where
        dest = content if _inside(roots, content) else None
        if dest:
            fields["dest_file"] = dest
        fields["phase"] = None
        ctx.update(**fields)
        try:
            if cfg["after_done"] == "pause":
                client.stop(infohash)
            elif cfg["after_done"] == "remove" and ref.get("added_by_hub"):
                client.delete(infohash, False)           # the files stay; only the torrent entry goes
        except QbError as exc:
            ctx.event("warn", "couldn't apply after_done=%s: %s" % (cfg["after_done"], exc))
        return Outcome.done(dest)


# --------------------------------------------------------------------------- routing

def prefers_magnets(settings=None):
    cfg, _problem = load_config()
    if cfg and cfg["prefer_for_magnets"]:
        return True
    block = (settings or {}).get("qbittorrent")
    return bool(isinstance(block, dict) and block.get("prefer_for_magnets") is True)


def _match_explicit(text, settings):
    """Any magnet or info hash: this is what makes `dl_add(engine="qbittorrent")` work.  Its priority is
    the lowest there is, below every other engine's rule, so it never wins a magnet by default."""
    parsed = parse_magnet(text)
    if parsed is None:
        return None
    return {"reason": "magnet link (qBittorrent engine)", "kind": "torrent", "source": parsed["magnet"]}


def _match_preferred(text, settings):
    """Only when the owner opted in AND qBittorrent answers; otherwise None, so the magnet falls through
    to the next engine instead of failing."""
    parsed = parse_magnet(text)
    if parsed is None or not prefers_magnets(settings):
        return None
    ok, _why = ENGINE.available()
    if not ok:
        return None
    return {"reason": "magnet link (qBittorrent, preferred in %s)" % CONFIG_NAME, "kind": "torrent",
            "source": parsed["magnet"]}


ENGINE = QBittorrentEngine()
ROUTE_RULES = [
    RouteRule("qbittorrent", 1, _match_explicit),
    RouteRule("qbittorrent", 500, _match_preferred),
]
