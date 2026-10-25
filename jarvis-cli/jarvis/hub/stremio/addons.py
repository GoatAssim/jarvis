"""Stremio addons: which ones are installed, and the guarded way to ask them anything (plan 9.1).

Where the installed addons come from, in this order:

  1. the owner's Stremio account (`AddonCollectionGet` on the cloud API; needs `account.auth_key`
     in ~/.jarvis/stremio.json) -- the exact set Stremio itself uses;
  2. a list the owner wrote by hand: `"addons": {"urls": ["https://.../manifest.json", ...]}` in
     ~/.jarvis/stremio.json ("paste an addon URL", plan 15.4);
  3. Cinemeta (the default metadata addon, `DEFAULT_META_MANIFEST`), used for titles and episode
     lists ONLY when nothing above can answer.  It has no streams.

Every request goes through `netguard.check_url` (first hop and every redirect), with the owner's
`allow_private_hosts` as the only way past it, so an addon URL that points at the owner's own
network is refused unless they allow-listed it.  Replies are size-capped and parsed as JSON;
errors carry a sentence the owner can act on and NEVER a URL: addon URLs often carry the owner's
debrid key in the path, and the account key is only ever read inside `cloud_call`.

Not run against a real Stremio account or a real addon (no network where this was written); the
protocol shapes are the standard addon protocol in plan 9.1.  `tests/addon_checks.py` records the
real answers.
"""

import json
import re
import threading
from urllib.parse import quote, urljoin, urlsplit

from .. import netguard, states
from . import config

DEFAULT_META_MANIFEST = "https://v3-cinemeta.strem.io/manifest.json"
CLOUD_API = "https://api.strem.io/api/"
USER_AGENT = "Jarvis-Hub/1"
MAX_REPLY_BYTES = 6 * 1024 * 1024
MAX_CLOUD_BYTES = 24 * 1024 * 1024
ADDON_TIMEOUT = (4, 8)               # connect, read: "8 s timeout each" (plan 9.3)
MAX_REDIRECTS = 3
MAX_ADDONS = 40
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
_ID_RE = re.compile(r"^[A-Za-z0-9:._-]{1,64}$")
_CATALOG_ID_RE = re.compile(r"^[A-Za-z0-9:._-]{1,80}$")


class AddonError(Exception):
    """A call to an addon or the cloud failed.  The message is safe to show: no URL, no key.

    `kind` and `status` are optional and only say WHAT went wrong, for `addon_health` (N31): kind is
    "local_refused" (the SSRF guard refused a private or local address), "refused" (any other guard refusal),
    "network" (a timeout, a refused connection, a name that did not resolve), "http" (with `status`),
    "json", "size", "redirect", or None when nobody said.  Code that raises it without them is unchanged."""

    def __init__(self, message="", *, kind=None, status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status


# --------------------------------------------------------------------------- ids and URLs

def valid_type(value):
    text = str(value or "").strip().lower()
    return text if _TYPE_RE.match(text) else None


def valid_id(value):
    """An id an addon understands: IMDb (tt0000000), Kitsu (kitsu:123) or an episode of either
    (tt0000000:2:5, kitsu:123:5).  Whatever the addons catalogued, within a safe alphabet."""
    text = str(value or "").strip()
    return text if _ID_RE.match(text) else None


def id_kind(value):
    text = str(value or "")
    if re.match(r"^tt\d{5,10}(:\d+:\d+)?$", text):
        return "imdb"
    if text.startswith("kitsu:"):
        return "kitsu"
    return "other"


def base_of(manifest_url):
    """https://host/path/manifest.json -> https://host/path (no trailing slash), else None."""
    text = str(manifest_url or "").strip()
    if not text or len(text) > 2048:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    path = parts.path
    if path.endswith("/manifest.json"):
        path = path[:-len("/manifest.json")]
    return "%s://%s%s" % (parts.scheme.lower(), parts.netloc, path.rstrip("/"))


def resource_url(base, resource, type_, item_id, extra=None):
    """<base>/<resource>/<type>/<id>[/<extra>].json, built here from checked pieces, never from
    addon text.  `extra` is "search=<query>" for catalogs."""
    type_, item_id = valid_type(type_), valid_id(item_id)
    if not type_ or not item_id or resource not in ("meta", "stream", "catalog", "subtitles"):
        raise AddonError("not a valid addon request")
    tail = "/%s" % quote(extra, safe="=&") if extra else ""
    return "%s/%s/%s/%s%s.json" % (base, resource, quote(type_, safe=""), quote(item_id, safe=":"), tail)


def host_label(base):
    """What may be shown of an addon's address: the host only (the path can hold a key)."""
    try:
        return (urlsplit(base).hostname or "")[:60]
    except ValueError:
        return ""


# --------------------------------------------------------------------------- guarded HTTP

class Net:
    """One guarded HTTP client.  `allow` is the owner's allow_private_hosts; `session` and
    `resolver` are injectable for the tests."""

    def __init__(self, allow=(), session=None, resolver=None):
        self.allow = list(allow or [])
        self._session = session
        self.resolver = resolver
        self._lock = threading.Lock()

    def sess(self):
        with self._lock:
            if self._session is None:
                import requests
                self._session = requests.Session()
                self._session.trust_env = False          # loopback addons must not be routed to a proxy
                self._session.headers["User-Agent"] = USER_AGENT
            return self._session

    def _read(self, resp, limit):
        data = bytearray()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            data += chunk
            if len(data) > limit:
                raise AddonError("the reply was larger than %d MB" % (limit // (1024 * 1024)), kind="size")
        return bytes(data)

    def request(self, method, url, label, *, json_body=None, limit=MAX_REPLY_BYTES, timeout=ADDON_TIMEOUT):
        """-> parsed JSON.  Raises AddonError(sentence naming `label`, never the URL)."""
        current = url
        resp = None
        try:
            for _ in range(MAX_REDIRECTS + 1):
                try:
                    netguard.check_url(current, self.allow, self.resolver)
                except netguard.GuardError as exc:
                    local = "allow_private_hosts" in str(exc)          # netguard's own sentence for a private or local address
                    raise AddonError("%s was refused: %s" % (label, str(exc).replace(url, "the address")),
                                     kind="local_refused" if local else "refused")
                try:
                    if method == "POST":
                        resp = self.sess().post(current, json=json_body, stream=True, allow_redirects=False, timeout=timeout)
                    else:
                        resp = self.sess().get(current, stream=True, allow_redirects=False, timeout=timeout,
                                               headers={"Accept": "application/json"})
                except Exception as exc:  # noqa: BLE001 - requests' errors carry the URL: say only what happened
                    raise AddonError("%s didn't answer (%s)" % (label, type(exc).__name__), kind="network")
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    if method == "POST":                      # never replay a body (the account key) elsewhere
                        raise AddonError("%s redirected a request that carries a key; refused" % label, kind="redirect")
                    nxt = urljoin(current, resp.headers["Location"])
                    resp.close()
                    resp = None
                    current = nxt
                    continue
                break
            else:
                raise AddonError("%s redirected too many times" % label, kind="redirect")
            if resp.status_code != 200:
                raise AddonError("%s answered HTTP %d" % (label, resp.status_code), kind="http", status=resp.status_code)
            raw = self._read(resp, limit)
            try:
                return json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                raise AddonError("%s didn't answer with JSON" % label, kind="json")
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass

    def get_json(self, url, label, **kw):
        return self.request("GET", url, label, **kw)


# --------------------------------------------------------------------------- one addon

def _types_of(manifest):
    return [t for t in (valid_type(x) for x in (manifest.get("types") or [])) if t]


def _resources_of(manifest):
    """-> {resource name: {"types": [...] or None, "prefixes": [...] or None}}"""
    out = {}
    for entry in manifest.get("resources") or []:
        if isinstance(entry, str):
            out[entry] = {"types": None, "prefixes": None}
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            types = [t for t in (valid_type(x) for x in (entry.get("types") or [])) if t] or None
            prefixes = [str(p) for p in (entry.get("idPrefixes") or []) if isinstance(p, str)] or None
            out[entry["name"]] = {"types": types, "prefixes": prefixes}
    return out


class Addon:
    """A parsed manifest plus where it lives.  `base` is never shown; `host` is."""

    def __init__(self, base, manifest, source):
        self.base = base
        self.manifest = manifest if isinstance(manifest, dict) else {}
        self.source = source
        self.name = states.clean_text(self.manifest.get("name") or host_label(base) or "addon", 60) or "addon"
        self.addon_id = states.clean_text(self.manifest.get("id") or "", 80) or ""
        self.version = states.clean_text(self.manifest.get("version") or "", 20) or ""
        self.description = states.clean_text(self.manifest.get("description") or "", 140) or ""
        self.types = _types_of(self.manifest)
        self.resources = _resources_of(self.manifest)
        self.prefixes = [str(p) for p in (self.manifest.get("idPrefixes") or []) if isinstance(p, str)] or None
        self.host = host_label(base)

    def supports(self, resource, type_=None, item_id=None):
        entry = self.resources.get(resource)
        if entry is None:
            return False
        types = entry["types"] or self.types
        if type_ and types and valid_type(type_) not in types:
            return False
        prefixes = entry["prefixes"] or self.prefixes
        if item_id and prefixes and not any(str(item_id).startswith(p) for p in prefixes):
            return False
        return True

    def search_catalogs(self, type_=None):
        """[(type, catalog id)] of the catalogs that can be searched, optionally for one type."""
        out = []
        for catalog in self.manifest.get("catalogs") or []:
            if not isinstance(catalog, dict):
                continue
            cat_type, cat_id = valid_type(catalog.get("type")), catalog.get("id")
            if not cat_type or not isinstance(cat_id, str) or not _CATALOG_ID_RE.match(cat_id):
                continue
            if type_ and cat_type != type_:
                continue
            extras = catalog.get("extra") or []
            names = {e.get("name") for e in extras if isinstance(e, dict)}
            names |= {e for e in (catalog.get("extraSupported") or []) if isinstance(e, str)}
            required = {e.get("name") for e in extras if isinstance(e, dict) and e.get("isRequired")}
            if "search" in names and required <= {"search"}:
                out.append((cat_type, cat_id))
        return out

    def public(self):
        """What a tool result may say about an addon."""
        return {"name": self.name, "version": self.version, "host": self.host, "source": self.source,
                "types": self.types[:8], "resources": sorted(self.resources)[:8],
                "description": self.description,
                "streams": self.supports("stream"), "meta": self.supports("meta"),
                "catalogs": len(self.manifest.get("catalogs") or [])}


# --------------------------------------------------------------------------- the account

def auth_key():
    """The account key, from the owner's config file.  Read here and nowhere else in the new
    code, passed straight to `cloud_call`, never returned, logged or put in a message."""
    try:
        key = ((config.load().get("account") or {}).get("auth_key") or "").strip()
    except Exception:  # noqa: BLE001
        key = ""
    return key if key and len(key) <= 200 and re.match(r"^[A-Za-z0-9_\-+=/.]+$", key) else ""


def account_ready():
    return bool(auth_key())


def cloud_call(net, method, body):
    """POST https://api.strem.io/api/<method> with the account key.  -> the `result` field.

    The method and body shapes are the plan's expectation (9.1): `POST /api/<method>` with
    `{"authKey": ...}`.  NOT run against the real API (it needs the owner's key); the reply is
    read defensively and every failure is a sentence without the key."""
    key = auth_key()
    if not key:
        raise AddonError("the Stremio account isn't set up: put account.auth_key in ~/.jarvis/stremio.json "
                         "(Stremio's own login key; Jarvis never prints it)")
    if not re.match(r"^[A-Za-z]{3,40}$", method):
        raise AddonError("not a valid cloud call")
    payload = dict(body or {})
    payload["authKey"] = key
    try:
        data = net.request("POST", CLOUD_API + method, "the Stremio cloud", json_body=payload, limit=MAX_CLOUD_BYTES,
                           timeout=(5, 20))
    except AddonError as exc:
        raise AddonError(str(exc).replace(key, "***"))
    if not isinstance(data, dict):
        raise AddonError("the Stremio cloud answered something unexpected")
    err = data.get("error")
    if err:
        text = err.get("message") if isinstance(err, dict) else err
        raise AddonError("the Stremio cloud said: %s" % (states.clean_text(str(text).replace(key, "***"), 160) or "an error"))
    return data.get("result")


def _cloud_error(data, *secrets):
    err = data.get("error") if isinstance(data, dict) else None
    if not err:
        return None
    text = str(err.get("message") if isinstance(err, dict) else err)
    for secret in secrets:
        if secret and len(secret) >= 3:
            text = text.replace(secret, "***")
    return "the Stremio cloud said: %s" % (states.clean_text(text, 160) or "an error")


def login(net, email, password):
    """Sign in to the owner's Stremio account: POST /api/login -> (auth_key, email).  The password is used for this one call
    and goes nowhere else (not stored, not logged, not in an error).  NOT run against the real API (no network where this was
    written); the reply is read defensively."""
    email, password = str(email or "").strip(), str(password or "")
    if not re.match(r"^[^@\s]{1,100}@[^@\s]{1,150}$", email) or not password or len(password) > 200:
        raise AddonError("enter the email and password of your Stremio account")
    body = {"type": "Login", "email": email, "password": password, "facebook": False}
    try:
        data = net.request("POST", CLOUD_API + "login", "the Stremio cloud", json_body=body, limit=MAX_CLOUD_BYTES, timeout=(5, 20))
    except AddonError as exc:
        raise AddonError(str(exc).replace(password, "***") if len(password) >= 3 else str(exc))
    problem = _cloud_error(data, password)
    if problem:
        raise AddonError(problem)
    result = data.get("result") if isinstance(data, dict) else None
    key = str((result or {}).get("authKey") or "").strip() if isinstance(result, dict) else ""
    if not re.match(r"^[A-Za-z0-9_\-+=/.]{8,200}$", key):
        raise AddonError("the Stremio cloud answered without a login key")
    user = result.get("user") if isinstance(result.get("user"), dict) else {}
    return key, str(user.get("email") or email)[:150]


def account_email(net):
    """The email of the saved key's account (GetUser), or raises AddonError (the key is dead, or the cloud is unreachable)."""
    result = cloud_call(net, "getUser", {"type": "GetUser"})
    return str((result or {}).get("email") or "")[:150] if isinstance(result, dict) else ""


def remote_logout(net):
    """Best effort: tell Stremio the saved key is finished.  Never raises."""
    try:
        cloud_call(net, "logout", {"type": "Logout"})
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- which addons are installed

def _from_manifest_entry(entry, source):
    if not isinstance(entry, dict):
        return None
    base = base_of(entry.get("transportUrl") or entry.get("transport_url"))
    manifest = entry.get("manifest")
    if not base or not isinstance(manifest, dict):
        return None
    return Addon(base, manifest, source)


def installed(net, *, use_cloud=True, manual=True, include_default=True):
    """-> ([Addon], [note]).  Never raises: what could not be read is a note."""
    found, notes, seen = [], [], set()

    def add(addon):
        if addon and addon.base not in seen and len(found) < MAX_ADDONS:
            seen.add(addon.base)
            found.append(addon)

    if use_cloud and account_ready():
        try:
            result = cloud_call(net, "addonCollectionGet", {"type": "AddonCollectionGet", "update": True})
            entries = result.get("addons") if isinstance(result, dict) else result
            for entry in entries or []:
                add(_from_manifest_entry(entry, "account"))
        except AddonError as exc:
            notes.append(str(exc))
    elif use_cloud:
        notes.append("no Stremio account key in ~/.jarvis/stremio.json, so the addon list comes from the "
                     "'addons.urls' list there, if any")
    if manual:
        cfg = config.load().get("addons")
        urls = cfg.get("urls") if isinstance(cfg, dict) else None
        for raw in (urls or [])[:MAX_ADDONS]:
            base = base_of(raw)
            if not base or base in seen:
                continue
            try:
                add(Addon(base, net.get_json(base + "/manifest.json", "an addon from your list"), "list"))
            except AddonError as exc:
                notes.append(str(exc))
    if include_default and not any(a.supports("meta") or a.search_catalogs() for a in found):
        base = base_of(DEFAULT_META_MANIFEST)
        try:
            add(Addon(base, net.get_json(DEFAULT_META_MANIFEST, "Cinemeta"), "default"))
            notes.append("no installed addon can look titles up, so Cinemeta (Stremio's default) was used")
        except AddonError as exc:
            notes.append(str(exc))
    return found, notes
