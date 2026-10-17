"""Engine discovery.

Every module in this folder that doesn't start with "_" (and isn't `base`) is imported and
may export:

    ENGINE       an `engines.base.Engine` instance (registered under `ENGINE.name`)
    ROUTE_RULES  a list of `engines.base.RouteRule` the router consults

Adding yt-dlp (phase 3) or Stremio (phase 4) is therefore a new file here and nothing
else -- no list to edit, so two agents adding two engines never touch the same line.  A
module that fails to import is recorded in LOAD_ERRORS (hub_doctor prints it) and skipped:
one broken engine can't take the hub down.
"""

import importlib
import pkgutil

ENGINES = {}
LOAD_ERRORS = {}
_RULES = []
_loaded = False


def load(force=False):
    global _loaded
    if _loaded and not force:
        return
    ENGINES.clear()
    LOAD_ERRORS.clear()
    del _RULES[:]
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        name = info.name
        if name.startswith("_") or name == "base":
            continue
        try:
            module = importlib.import_module("%s.%s" % (__name__, name))
            engine = getattr(module, "ENGINE", None)
            if engine is None or not getattr(engine, "name", ""):
                LOAD_ERRORS[name] = "exports no ENGINE"
                continue
            if engine.name in ENGINES:
                LOAD_ERRORS[name] = "engine name %r is already taken" % engine.name
                continue
            ENGINES[engine.name] = engine
            _RULES.extend(getattr(module, "ROUTE_RULES", None) or [])
        except Exception as exc:  # noqa: BLE001 - a broken engine must not stop the others
            LOAD_ERRORS[name] = "%s: %s" % (type(exc).__name__, exc)
    _loaded = True


def get(name):
    load()
    return ENGINES.get(name)


def names():
    load()
    return sorted(ENGINES)


def route_rules():
    load()
    return list(_RULES)


def availability():
    """{engine: {"available": bool, "reason": str, "label": str}} for hub_doctor / dl_status."""
    load()
    out = {}
    for name, engine in sorted(ENGINES.items()):
        try:
            ok, reason = engine.available()
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, "check failed: %s" % exc
        out[name] = {"available": bool(ok), "reason": reason or "", "label": engine.label or name}
    return out
