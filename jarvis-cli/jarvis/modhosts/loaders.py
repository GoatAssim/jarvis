"""Which loader version a pack should use: the "latest by default" resolver (owner answer 8).

    latest_loader_version(loader, mc_version)
        -> {"ok": True, "loader_version": "0.20.1", "how": "recommended" | "stable" | "latest"[, "note": str]}
         | {"ok": False, "reason": "no_loader_version" | "offline" | "vendor_changed" | "unsupported", "error": str}
    valid_loader_version(text)            -> bool   (letters, digits, dots, dashes; 40 characters at most)
    mrpack_dependency_key(loader)         -> "fabric-loader" | "quilt-loader" | "forge" | "neoforge" | None
    curseforge_loader_id(loader, version) -> "fabric-0.16.9" etc., or None for an unknown loader / bad version

What each loader answers (every fact is from the owner's probe of 25 Oct 2026; tests/data/modhosts/hand_loader_*.json
are small hand-written copies of those shapes):

  * Fabric   GET meta.fabricmc.net/v2/versions/loader/<mc>.  Entries carry `loader.version` and `loader.stable`.
             The newest entry flagged stable.  An unknown Minecraft version answers HTTP 400 with the body `[]`.
  * Quilt    GET meta.quiltmc.org/v3/versions/loader/<mc>.  No stable flag: a version with an alpha / beta / rc / pre
             tag is not stable.  An unknown Minecraft version answers HTTP 404 with a JSON body whose `code` is
             "not_found".
  * Forge    files.minecraftforge.net promotions_slim.json: `promos["<mc>-recommended"]` wins (how "recommended"),
             else `promos["<mc>-latest"]` (how "latest" and a note).  Only when the promotions file itself answers
             but names neither key does Forge's own maven-metadata.xml list get read (the same vendor's full
             version list, the fallback the probe planned); a failed promotions request never triggers it.
  * NeoForge maven.neoforged.net .../net/neoforged/neoforge: `{isSnapshot, versions: [...]}`.  Minecraft 1.A.B maps
             to the prefix `A.B.` (B is 0 when missing: 1.21.11 -> 21.11., 1.21 -> 21.0.); the newer yy.drop[.patch]
             numbering maps to `yy.drop.<patch or 0>.` (26.3 -> 26.3.0., 26.1.2 -> 26.1.2.).  The newest STABLE build
             wins (how "stable"); a version with any suffix (-beta ...) is not stable.  A Minecraft version that has
             only betas uses the newest beta (how "latest" and a note), it never fails.  The legacy artifact
             net.neoforged:forge (1.20.1 only) is NOT read, so NeoForge for 1.20.1 is `no_loader_version`.

No answer's list ORDER is trusted: the newest is chosen by version number (the NeoForge list is not sorted, and the
Quilt answer is not newest-first either: its first entries are 0.20.0 betas).  Loaders are strict: NeoForge and
Forge are different loaders and never answer for each other; nothing falls back to another vendor's site.

Every request goes through net.fetch(vendor="loader_meta", ...) (fixed allow-list in hosts.py, per-request
timeout of 90 s because the Fabric answer takes about 20 s), at most one request per loader per call (two for the
Forge maven fallback), never a retry loop.  A successful answer is remembered for 10 minutes in cache.py (the
derived result only: small, public, never anything that could carry a key).  Failures are never cached.

Failure reasons: `offline` (no answer, a 5xx, a rate limit, a refused address), `vendor_changed` (an answer that no
longer parses, or an unexpected HTTP status), `no_loader_version` (the site answered and has no build for that
Minecraft version), `unsupported` (not a loader / Minecraft version this resolver takes).  Nothing here raises.
"""
import re

from . import NO_LOADER_VERSION, OFFLINE, UNSUPPORTED, VENDOR_CHANGED, cache, fail, net, resolve

# ---------------------------------------------------------------------------- fixed addresses (never config / env)
FABRIC_URL = "https://meta.fabricmc.net/v2/versions/loader/%s"
QUILT_URL = "https://meta.quiltmc.org/v3/versions/loader/%s"
FORGE_PROMOS_URL = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN_URL = "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
NEOFORGE_URL = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"

CACHE_TTL = 600                      # seconds (10 minutes)
CACHE_PREFIX = "loader_meta:v1:"
MAX_ITEMS = 200000                   # a defensive parsing limit: no answer seen was over ~5,100 entries
HOWS = ("recommended", "stable", "latest")

_NAMES = {"fabric": "Fabric", "quilt": "Quilt", "forge": "Forge", "neoforge": "NeoForge"}
_MRPACK_KEYS = {"fabric": "fabric-loader", "quilt": "quilt-loader", "forge": "forge", "neoforge": "neoforge"}

# letters, digits, dots, dashes; starts and ends with a letter or digit; 40 characters at most; no whitespace
_VALID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,38}[A-Za-z0-9])?")
_NUMERIC = re.compile(r"([0-9]{1,9}(?:\.[0-9]{1,9})*)(.*)", re.S)
_PRE_TAGS = ("alpha", "beta", "rc", "pre", "snapshot", "nightly", "dev")
_MAVEN_VERSION = re.compile(r"<version>\s*([^<>\s]{1,80})\s*</version>")


# ---------------------------------------------------------------------------- the small public helpers

def valid_loader_version(text):
    """True for a plausible loader version: letters, digits, dots and dashes, 40 characters at most, no whitespace.
    It must start and end with a letter or digit and hold no `..` (it ends up in file names and pack manifests)."""
    return bool(isinstance(text, str) and _VALID.fullmatch(text) and ".." not in text)


def _loader(loader):
    if not isinstance(loader, str):
        return None
    return resolve.normalise_loader(loader)


def mrpack_dependency_key(loader):
    """The `dependencies` key a Modrinth .mrpack uses for this loader, or None for an unknown loader."""
    return _MRPACK_KEYS.get(_loader(loader))


def curseforge_loader_id(loader, loader_version):
    """The id a CurseForge manifest's `modLoaders` entry uses (`fabric-0.16.9`), or None for an unknown loader or
    a loader version that does not pass valid_loader_version()."""
    name = _loader(loader)
    if name is None or not valid_loader_version(loader_version):
        return None
    return "%s-%s" % (name, loader_version)


# ---------------------------------------------------------------------------- version arithmetic

def _split(version):
    """(tuple of ints, suffix) for a version that starts with digits; None otherwise (or when it is not valid)."""
    if not valid_loader_version(version):
        return None
    match = _NUMERIC.match(version)
    if not match:
        return None
    return tuple(int(p) for p in match.group(1).split(".")), match.group(2)


def _sort_key(version):
    """Orders versions by number; at the same number a plain version beats one with a suffix."""
    parsed = _split(version)
    if parsed is None:
        return ((), 0, "")
    numbers, suffix = parsed
    return (numbers, 0 if suffix else 1, suffix)


def _newest(versions):
    """The newest of `versions` by number (never by list order), or None."""
    best, best_key = None, None
    for version in versions:
        key = _sort_key(version)
        if best_key is None or key > best_key:
            best, best_key = version, key
    return best


def _has_pre_tag(version):
    """Quilt has no stable flag: a token starting with alpha / beta / rc / pre / snapshot / nightly / dev marks a
    pre-release (0.20.0-beta.9, 0.27.0-rc.1, 0.25.0-pre.2)."""
    for token in re.split(r"[.\-+_]", version.lower()):
        if token.startswith(_PRE_TAGS):
            return True
    return False


# ---------------------------------------------------------------------------- answers

def _safe_text(text, limit=40):
    cleaned = "".join(ch if (32 <= ord(ch) < 127) else "?" for ch in str(text))
    return cleaned[:limit]


def _no_build(name, mc):
    return fail(NO_LOADER_VERSION, "%s has no loader build for Minecraft %s. Name a loader version yourself, or "
                                   "pick another Minecraft version." % (name, mc))


def _changed(name):
    return fail(VENDOR_CHANGED, "The %s version list is not in the form Jarvis expects (the site may have "
                                "changed), so no loader version was chosen." % name)


def _ok(loader_version, how, note=None):
    out = {"ok": True, "loader_version": loader_version, "how": how}
    if note:
        out["note"] = note
    return out


class _Unknown:
    """Marker: the site answered 'I know no such Minecraft version' (not a failure of the site)."""


UNKNOWN = _Unknown()


def _request(name, url, want, unknown=None):
    """One net.fetch.  Returns (result, None) on success, (None, UNKNOWN) when `unknown(status, body)` says the
    site does not know the Minecraft version, or (None, failure dict).  Failures are folded into the three
    reasons of the contract: a site that did not answer (or answered 5xx / rate limited / was refused) is `offline`;
    one that answered something unexpected is `vendor_changed`."""
    res = net.fetch("loader_meta", url, want=want)
    if res.get("ok"):
        return res, None
    status, body, reason = res.get("status"), res.get("body"), res.get("reason")
    if unknown is not None:
        try:
            if unknown(status, body):
                return None, UNKNOWN
        except Exception:  # noqa: BLE001
            pass
    if reason in ("offline", "rate_limited", "unsupported"):
        return None, fail(OFFLINE, str(res.get("error") or "%s did not answer." % name))
    return None, _changed(name)


def _fabric_unknown(status, body):
    return status == 400 and isinstance(body, list) and not body


def _quilt_unknown(status, body):
    return status == 404 and isinstance(body, dict) and body.get("code") == "not_found"


def _entries_version(entry):
    loader = entry.get("loader") if isinstance(entry, dict) else None
    version = loader.get("version") if isinstance(loader, dict) else None
    return (loader, version) if valid_loader_version(version) else (None, None)


def _from_entries(name, mc, body, is_stable):
    """Fabric / Quilt: the newest version `is_stable(loader_dict, version)` accepts."""
    if not isinstance(body, list):
        return _changed(name)
    if not body:
        return _no_build(name, mc)
    parsed, stable = 0, []
    for entry in body[:MAX_ITEMS]:
        loader, version = _entries_version(entry)
        if loader is None:
            continue
        parsed += 1
        if is_stable(loader, version):
            stable.append(version)
    if not parsed:
        return _changed(name)
    newest = _newest(stable)
    if newest is None:
        return _no_build(name, mc)       # only betas and snapshots: Fabric / Quilt never use those
    return _ok(newest, "stable")


def _fabric(mc):
    res, err = _request("Fabric", FABRIC_URL % mc, "json", _fabric_unknown)
    if err is UNKNOWN:
        return _no_build("Fabric", mc)
    if err:
        return err
    return _from_entries("Fabric", mc, res.get("body"), lambda loader, version: loader.get("stable") is True)


def _quilt(mc):
    res, err = _request("Quilt", QUILT_URL % mc, "json", _quilt_unknown)
    if err is UNKNOWN:
        return _no_build("Quilt", mc)
    if err:
        return err
    return _from_entries("Quilt", mc, res.get("body"), lambda loader, version: not _has_pre_tag(version))


def _forge_from_maven(mc):
    """Forge's own full list, read only when the promotions file answers but names neither key for `mc`."""
    res, err = _request("Forge", FORGE_MAVEN_URL, "text")
    if err:
        return err
    text = res.get("body")
    if not isinstance(text, str):
        return _changed("Forge")
    found = _MAVEN_VERSION.findall(text[:8 * 1024 * 1024])
    if not found:
        return _changed("Forge")
    prefix = mc + "-"
    builds = []
    for full in found[:MAX_ITEMS]:
        if full.startswith(prefix):
            rest = full[len(prefix):]
            if valid_loader_version(rest) and _split(rest) is not None:
                builds.append(rest)
    newest = _newest(builds)
    if newest is None:
        return _no_build("Forge", mc)
    return _ok(newest, "latest", "Forge's promotions list has no entry for Minecraft %s, so the newest build in "
                                 "Forge's own version list (%s) is used." % (mc, newest))


def _forge(mc):
    res, err = _request("Forge", FORGE_PROMOS_URL, "json")
    if err:
        return err
    body = res.get("body")
    promos = body.get("promos") if isinstance(body, dict) else None
    if not isinstance(promos, dict) or not any(
            isinstance(k, str) and k.endswith(("-recommended", "-latest")) for k in list(promos)[:MAX_ITEMS]):
        return _changed("Forge")
    recommended, latest = promos.get(mc + "-recommended"), promos.get(mc + "-latest")
    if recommended is not None:
        return _ok(recommended, "recommended") if valid_loader_version(recommended) else _changed("Forge")
    if latest is not None:
        if not valid_loader_version(latest):
            return _changed("Forge")
        return _ok(latest, "latest", "Forge has not marked a recommended build for Minecraft %s, so its newest "
                                     "build (%s) is used." % (mc, latest))
    return _forge_from_maven(mc)


def neoforge_prefix(mc):
    """The start of every NeoForge version for Minecraft `mc` (see the module docstring); None for a shape
    that cannot map.  1.21.11 -> '21.11.', 1.21 -> '21.0.', 26.3 -> '26.3.0.', 26.1.2 -> '26.1.2.'."""
    parts = str(mc).split(".")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    if len(numbers) < 2 or len(numbers) > 3:
        return None
    if numbers[0] == 1:                                   # old numbering: 1.A.B -> A.B.<build>
        return "%d.%d." % (numbers[1], numbers[2] if len(numbers) == 3 else 0)
    return "%d.%d.%d." % (numbers[0], numbers[1], numbers[2] if len(numbers) == 3 else 0)


def _neoforge(mc):
    prefix = neoforge_prefix(mc)
    if prefix is None:
        return fail(UNSUPPORTED, "NeoForge versions cannot be looked up for Minecraft %s." % _safe_text(mc))
    res, err = _request("NeoForge", NEOFORGE_URL, "json")
    if err:
        return err
    body = res.get("body")
    versions = body.get("versions") if isinstance(body, dict) else None
    if not isinstance(versions, list) or not versions:
        return _changed("NeoForge")
    seen, stable, any_build = 0, [], []
    for version in versions[:MAX_ITEMS]:
        parsed = _split(version) if isinstance(version, str) else None
        if parsed is None:
            continue
        seen += 1
        if not version.startswith(prefix):
            continue
        any_build.append(version)
        if not parsed[1]:                                  # a plain number: no -beta or other suffix
            stable.append(version)
    if not seen:
        return _changed("NeoForge")
    newest = _newest(stable)
    if newest is not None:
        return _ok(newest, "stable")
    newest = _newest(any_build)
    if newest is None:
        return _no_build("NeoForge", mc)
    return _ok(newest, "latest", "NeoForge has only beta builds for Minecraft %s so far, so its newest beta (%s) "
                                 "is used." % (mc, newest))


_RESOLVERS = {"fabric": _fabric, "quilt": _quilt, "forge": _forge, "neoforge": _neoforge}


# ---------------------------------------------------------------------------- cache

def _cached(key):
    """A previously derived answer, re-checked as if it were hostile (the file is hand-editable)."""
    value = cache.get(key)
    if (isinstance(value, dict) and valid_loader_version(value.get("loader_version"))
            and value.get("how") in HOWS and isinstance(value.get("note", ""), str)
            and len(value.get("note", "")) <= 400):
        return _ok(value["loader_version"], value["how"], value.get("note") or None)
    return None


def latest_loader_version(loader, mc_version):
    """The loader version to use when the owner named none.  See the module docstring.  Never raises."""
    try:
        name = _loader(loader)
        if name is None:
            return fail(UNSUPPORTED, "%s is not a loader Jarvis can look up a version for (fabric, forge, neoforge "
                                     "or quilt)." % (_safe_text(loader) or "That"))
        mc = resolve.normalise_mc_version(mc_version)
        if mc is None:
            return fail(UNSUPPORTED, "%s is not a Minecraft release version (like 1.21.11 or 26.3)."
                        % (_safe_text(mc_version) or "That"))
        key = "%s%s:%s" % (CACHE_PREFIX, name, mc)
        hit = _cached(key)
        if hit is not None:
            return hit
        result = _RESOLVERS[name](mc)
        if result.get("ok"):
            cache.put(key, {k: result[k] for k in ("loader_version", "how", "note") if k in result}, CACHE_TTL)
        return result
    except Exception:  # noqa: BLE001 - a bug here must never reach a tool as an exception
        return fail(VENDOR_CHANGED, "The loader version could not be worked out.")
