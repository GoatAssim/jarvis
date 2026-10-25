"""Minecraft mod sources: Modrinth, CurseForge, OptiFine and the loader sites, behind one small core.

MODULE MAP (who may open a connection, who may not)
  net.py        THE guarded client.  Nothing else in this package opens a connection.  Fixed host
                allow-lists (hosts.py, never config or env), netguard.check_url on every hop,
                redirects followed by hand to allow-listed hosts only, size caps, honest User-Agent,
                the CurseForge key added ONLY for api.curseforge.com and redacted from every error.
  hosts.py      The allow-lists, one block per owner (12+ lines apart so separate patches never touch
                the same lines).
  cache.py      Small on-disk cache of PUBLIC vendor answers (~/.jarvis/modhosts/cache.json).
  untrusted.py  Vendor text is written by strangers: clip it, strip markup, mark it untrusted.
  sources.py    Registry: get("modrinth" | "curseforge") -> the adapter module, or None.
  modrinth.py   Modrinth adapter (no key).  curseforge.py has the same functions (another agent's file).
  resolve.py    Choosing a file, the never-guess rule for a project, dependencies, loader / version text.
  (config.py, curseforge.py, loaders.py, optifine.py, packs.py, outdir.py are separate files.)

Every function here returns a plain dict and NEVER raises.  A failure is
    {"ok": False, "error": "<one plain sentence>", "reason": "<code>"}
with `reason` one of REASONS below (defined once, here).  Nothing in this package installs anything.
"""

# The machine reasons.  A caller branches on these; the sentence is for people.
NO_KEY = "no_key"                          # CurseForge needs a personal key and there is none
BAD_KEY = "bad_key"                        # the vendor refused the key
RATE_LIMITED = "rate_limited"              # HTTP 429 (the wait is stated in the sentence)
NOT_FOUND = "not_found"
AMBIGUOUS = "ambiguous"                    # several projects fit; never guess between them
NOT_DISTRIBUTABLE = "not_distributable"    # the author blocks third-party downloads
NO_COMPATIBLE_FILE = "no_compatible_file"
NO_LOADER_VERSION = "no_loader_version"
BAD_DEST = "bad_dest"
EXISTS = "exists"
TOO_BIG = "too_big"
VERIFY_FAILED = "verify_failed"
VENDOR_CHANGED = "vendor_changed"          # an answer no longer parses / an unexpected HTTP status
OFFLINE = "offline"                        # the network (or the vendor) did not answer
UNSUPPORTED = "unsupported"                # not built / not installed / not a value this tool takes

REASONS = (
    NO_KEY, BAD_KEY, RATE_LIMITED, NOT_FOUND, AMBIGUOUS, NOT_DISTRIBUTABLE, NO_COMPATIBLE_FILE,
    NO_LOADER_VERSION, BAD_DEST, EXISTS, TOO_BIG, VERIFY_FAILED, VENDOR_CHANGED, OFFLINE, UNSUPPORTED,
)


def fail(reason, error, **extra):
    """The one failure shape: {"ok": False, "error": <sentence>, "reason": <code>} plus any extra keys."""
    out = {"ok": False, "error": str(error), "reason": reason if reason in REASONS else VENDOR_CHANGED}
    out.update(extra)
    return out
