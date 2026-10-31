"""Shared set-up for the hoster tests (not a test itself): a store and library per call, a claimed row, an engine
run, and a resolver context that may talk to loopback.  Import it AFTER setting HOME and putting jarvis-cli and
tests on sys.path (every test file does both first, like the other hub tests)."""

import tempfile
from pathlib import Path

import _hub_fake_hosters as fake
from jarvis.hub import hosters
from jarvis.hub.engines import hosters as hosters_engine
from jarvis.hub.engines.base import EngineContext
from jarvis.hub.store import Store

HOME = Path(tempfile.mkdtemp(prefix="jarvis-hoster-tests-"))
SERVER, BASE, STATE = fake.start()
_n = [0]


def settings_for(resolver_id=None, extra=None):
    """Hub settings that let loopback through the guard, and (optionally) make 127.0.0.1 a page host of one resolver."""
    out = {"allow_private_hosts": ["127.0.0.1"], "http.stall_seconds": 5}
    if resolver_id:
        out["hosters.extra_hosts"] = {resolver_id: ["127.0.0.1"]}
    out.update(extra or {})
    return out


def env(resolver_id=None, extra=None):
    """A fresh store + library root, pointed at the fake server.  -> (store, library_path)"""
    _n[0] += 1
    lib = HOME / ("lib%d" % _n[0])
    store = Store(HOME / ("hub-%d.db" % _n[0]))
    store.set_settings(dict(settings_for(resolver_id, extra), library_root=str(lib)))
    STATE.reset()
    return store, lib


def claim(store, url, **options):
    # A high priority: claim_next takes the highest first, so the row added HERE is the one claimed even when a folder
    # run has left its children queued.
    item = store.add_item(engine="hosters", source_url=url, title="", options=options, priority=10_000)[0]
    claimed = store.claim_next({"default": 100}, 100)
    assert claimed and claimed["id"] == item["id"]
    return claimed


def run(store, item, stop=None):
    ctx = EngineContext(store, item, store.get_settings(), stop_reason=stop or (lambda: None),
                        rate_limit_bps=lambda: 0)
    return hosters_engine.ENGINE.run(item, ctx)


def rctx(stop=None, extra=None):
    """A resolver context over loopback, for calling `resolver.resolve(url, ctx)` directly."""
    settings = {"allow_private_hosts": ["127.0.0.1"]}
    settings.update(extra or {})
    return hosters.make_context(settings, stop=stop)


def raises(exc_type, fn, *args, **kwargs):
    """The exception `fn` raised if it is an `exc_type`, else None."""
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    except Exception:  # noqa: BLE001 - a different exception type is a failed expectation, not a crash
        return None
    return None
