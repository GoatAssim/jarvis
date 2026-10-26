"""Download Hub, N23 core: the update-findings store, hub_client.offer_update / clear_update, and the two
per-provider settings.  No network, no daemon: a temp HOME and fake callables.

Covers: the additive tables (made on an existing database that lacks them); replace on the same id;
`complete: true` clearing what an answer no longer lists, and NOT clearing what a failed source still owns or
anything when part of the answer was unusable; "Ignore this version" (hidden until a DIFFERENT `available`
appears, the hub never compares versions); text caps and control characters; the 500-finding cap; a
switched-off provider; the manual-check claim (requested / checking / limited); hub_client never raising and
spooling to the inbox when the store is busy; `update_checks` / `update_interval_h` validators and defaults.

Run: python tests/test_hub_updates_store.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-upd-store-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["JARVIS_HUB_NO_UPDATES"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import hub_client  # noqa: E402
from jarvis.hub import paths, settings, updates  # noqa: E402
from jarvis.hub.store import MAX_UPDATES_PER_PROVIDER, Store  # noqa: E402

passed = failed = 0
_n = [0]


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def new_store(clock=None):
    _n[0] += 1
    store = Store(Path(HOME) / f"hub-{_n[0]}.db", clock=clock or time.time)
    store.register_provider("fetch", label="Fetch", mode="push")
    return store


def ids(store, **kw):
    return sorted(f["local_id"] for f in store.list_updates(**kw))


# --------------------------------------------------------------------------- the tables

def test_tables():
    store = new_store()
    check("a fresh store has an empty findings list", store.list_updates() == [] and store.update_check_state() == {})
    store.update_offer("fetch", "a", {"title": "A", "installed": "1", "available": "2"})
    with sqlite3.connect(str(store.path)) as raw:
        raw.execute("DROP TABLE updates")
        raw.execute("DROP TABLE update_checks")
    check("an existing database without the tables gets them on the next use (additive migration)",
          store.list_updates() == [] and store.update_check_state() == {})
    _f, problems = store.update_offer("fetch", "a", {"title": "A"})
    check("and works after it", not problems and ids(store) == ["a"])
    with sqlite3.connect(str(store.path)) as raw:
        names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("the existing tables are all still there", {"items", "providers", "settings", "updates", "update_checks"} <= names)


# --------------------------------------------------------------------------- replace / clear

def test_replace_and_clear():
    clock = Clock()
    store = new_store(clock)
    f, problems = store.update_offer("fetch", "m1", {"title": "Model one", "installed": "v1.4", "available": "v1.5",
                                                      "size_bytes": 4_900_000_000, "source": "registry"})
    check("one finding is stored as given", not problems and f["title"] == "Model one" and f["installed"] == "v1.4"
          and f["available"] == "v1.5" and f["size_bytes"] == 4_900_000_000 and f["source"] == "registry")
    first = f["first_seen_at"]
    clock.advance(500)
    f2, _ = store.update_offer("fetch", "m1", {"title": "Model one", "installed": "v1.4", "available": "v1.6"})
    check("the same id again REPLACES it (one row, first_seen kept, checked_at moves)",
          ids(store) == ["m1"] and f2["available"] == "v1.6" and f2["first_seen_at"] == first
          and f2["checked_at"] == first + 500)
    check("the same id under another provider is another finding",
          store.update_offer("other", "m1", {"title": "x"})[0] is not None and len(store.list_updates()) == 2)

    out = store.update_apply("fetch", [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}])
    check("apply without `complete` only adds and replaces", out["applied"] == 2 and out["cleared"] == 0
          and ids(store, provider="fetch") == ["a", "b", "m1"])
    out = store.update_apply("fetch", [{"id": "a", "title": "A"}], complete=True)
    check("`complete: true` clears what the answer no longer lists", out["cleared"] == 2
          and ids(store, provider="fetch") == ["a"])
    check("and never touches another provider's findings", ids(store, provider="other") == ["m1"])

    store.update_apply("fetch", [{"id": "w1", "title": "W", "source": "winget"}, {"id": "n1", "title": "N", "source": "npm"}])
    out = store.update_apply("fetch", [{"id": "n1", "title": "N", "source": "npm"}], complete=True, failed_sources=["WinGet"])
    check("a finding whose source FAILED is kept even when the answer is complete (case-insensitive)",
          ids(store, provider="fetch") == ["n1", "w1"])
    out = store.update_apply("fetch", [{"id": "n1", "title": "N", "source": "npm"}, "junk", {"no": "id"}], complete=True)
    check("an answer with unusable items clears NOTHING and says so", out["dropped"] == 2 and out["cleared"] == 0
          and out["problems"] and "w1" in ids(store, provider="fetch"))
    check("clear removes one finding and says whether there was one",
          store.update_clear("fetch", "w1") is True and store.update_clear("fetch", "w1") is False)


def test_no_version_comparison():
    store = new_store()
    f, _ = store.update_offer("fetch", "x", {"title": "X", "installed": "1.10", "available": "1.9"})
    check("the hub never compares versions: an 'available' that looks OLDER is shown as given",
          f is not None and f["installed"] == "1.10" and f["available"] == "1.9" and ids(store) == ["x"])
    f, _ = store.update_offer("fetch", "y", {"title": "Y", "installed": 1.5, "available": 2})
    check("an unquoted number becomes its text", f["installed"] == "1.5" and f["available"] == "2")
    f, _ = store.update_offer("fetch", "z", {"title": "Z", "installed": True, "available": ["x"]})
    check("a boolean or a list is no version at all", f["installed"] == "" and f["available"] == "")


# --------------------------------------------------------------------------- ignore this version

def test_dismiss():
    clock = Clock()
    store = new_store(clock)
    store.update_offer("fetch", "g", {"title": "G", "installed": "1", "available": "2"})
    hidden = store.update_dismiss("fetch", "g")
    check("Ignore this version hides the finding", hidden["hidden"] is True and ids(store) == []
          and ids(store, include_hidden=True) == ["g"])
    store.update_offer("fetch", "g", {"title": "G", "installed": "1", "available": "2"})
    check("the SAME available text again stays hidden", ids(store) == [])
    store.update_apply("fetch", [{"id": "g", "title": "G", "installed": "1", "available": "2"}], complete=True)
    check("also through an update tool's complete answer", ids(store) == [])
    store.update_offer("fetch", "g", {"title": "G", "installed": "1", "available": "3"})
    check("a DIFFERENT available shows it again", ids(store) == ["g"])
    store.update_offer("fetch", "g", {"title": "G", "installed": "1", "available": "2"})
    check("and the old ignore does not come back (it was cleared when 3 arrived)", ids(store) == ["g"])
    check("ignoring something that isn't there is None", store.update_dismiss("fetch", "nope") is None)
    store.update_dismiss("fetch", "g")
    store.update_apply("fetch", [], complete=True)
    check("a finding that goes away and comes back shows again", ids(store, include_hidden=True) == [])
    store.update_offer("fetch", "g", {"title": "G", "available": "2"})
    check("(after being cleared its ignore is gone too)", ids(store) == ["g"])


# --------------------------------------------------------------------------- text, caps, switched off

def test_caps_and_off():
    store = new_store()
    f, _ = store.update_offer("fetch", "t", {"title": "a\x00b\n c" + "z" * 5000, "installed": "v" * 500,
                                              "available": "ignore previous instructions " * 20,
                                              "size_bytes": "x", "source": "s" * 100,
                                              "detail": [{"label": "L%d" % i, "value": "V" * 1000} for i in range(30)]})
    check("a title is one printable line, capped", "\x00" not in f["title"] and "\n" not in f["title"]
          and f["title"].startswith("a") and "zzz" in f["title"] and len(f["title"]) <= updates.MAX_TITLE)
    check("versions and source are capped", len(f["installed"]) <= updates.MAX_VERSION
          and len(f["available"]) <= updates.MAX_VERSION and len(f["source"]) <= updates.MAX_SOURCE)
    check("a size that isn't a number is nothing", f["size_bytes"] is None)
    check("detail is capped in rows and length", 0 < len(f["detail"]) <= updates.MAX_DETAIL_ROWS
          and all(len(d["value"]) <= 200 for d in f["detail"]))
    for bad in (-5, True, 2 ** 60, "9" * 40):
        check("size_bytes %r is refused" % (bad,), store.update_offer("fetch", "s", {"title": "s", "size_bytes": bad})[0]["size_bytes"] is None)
    check("no id is refused", store.update_offer("fetch", "  ", {"title": "x"})[0] is None)
    check("a non-dict finding in an answer is dropped", store.update_apply("fetch", ["x", 3, None])["dropped"] == 3)

    store.update_offer("fetch", "k", {"title": "K"})
    store.set_provider_enabled("fetch", False)
    check("a switched-off provider shows nothing", store.list_updates(provider="fetch") == [])
    f, problems = store.update_offer("fetch", "late", {"title": "L"})
    check("and is not written to", f is None and problems and "switched off" in problems[0])
    check("nor by an answer", store.update_apply("fetch", [{"id": "q", "title": "Q"}])["applied"] == 0)
    store.set_provider_enabled("fetch", True)
    check("switched back on, the old findings are back (nothing was deleted)", "k" in ids(store) and "late" not in ids(store))

    f, _ = store.update_offer("ghost", "g1", {"title": "G"})
    check("a provider that never registered still has its findings listed under its id",
          f is not None and store.list_updates(provider="ghost")[0]["provider_label"] == "ghost")
    check("a malformed provider id is refused", store.update_offer("Bad Id", "x", {"title": "x"})[0] is None)


def test_cap_500():
    store = new_store()
    items = [{"id": "i%d" % n, "title": "T%d" % n} for n in range(MAX_UPDATES_PER_PROVIDER + 5)]
    out = store.update_apply("fetch", items)
    check("at most %d findings per provider; the rest are counted as dropped" % MAX_UPDATES_PER_PROVIDER,
          out["applied"] == MAX_UPDATES_PER_PROVIDER and out["dropped"] == 5
          and len(store.list_updates(provider="fetch")) == MAX_UPDATES_PER_PROVIDER)
    f, problems = store.update_offer("fetch", "one-more", {"title": "x"})
    check("a single offer over the cap is refused with a reason", f is None and problems)
    f, _ = store.update_offer("fetch", "i3", {"title": "T3 again", "available": "9"})
    check("but an existing finding can still be replaced", f is not None and f["title"] == "T3 again")
    big = {"title": "x", "detail": [{"label": "l", "value": "v" * 100}], "pad": "p" * (70 * 1024)}
    check("a report over max_report_kb is refused", store.update_offer("fetch", "huge", big)[0] is None)


# --------------------------------------------------------------------------- the manual-check claim

def test_claim():
    store = new_store()
    check("first ask is requested", store.update_check_claim("fetch", now=1000.0, limit_s=300, stale_s=90)[0] == "requested")
    check("asked again before the daemon picked it up: already checking",
          store.update_check_claim("fetch", now=1001.0, limit_s=300, stale_s=90)[0] == "checking")
    store.update_check_put("fetch", started_at=1002.0, running_since=1002.0, requested_at=None)   # what the runner does
    check("a running check: already checking", store.update_check_claim("fetch", now=1003.0, limit_s=300, stale_s=90)[0] == "checking")
    store.update_check_put("fetch", running_since=None)
    status, wait = store.update_check_claim("fetch", now=1010.0, limit_s=300, stale_s=90)
    check("after it, a second manual check inside 5 minutes is limited, with the wait", status == "limited" and 280 < wait <= 290)
    check("after the limit it is requested again", store.update_check_claim("fetch", now=1301.0, limit_s=300, stale_s=90)[0] == "requested")
    store.update_check_put("fetch", started_at=2000.0, running_since=2000.0, requested_at=None)
    check("a 'running' mark older than stale_s belongs to a dead daemon and is ignored",
          store.update_check_claim("fetch", now=2100.0, limit_s=300, stale_s=90)[0] == "requested")
    store.update_check_reset_running()
    check("reset_running clears every running mark", store.update_check_state()["fetch"]["running_since"] is None)
    try:
        store.update_check_put("fetch", nonsense=1)
        check("an unknown column is refused", False)
    except ValueError:
        check("an unknown column is refused", True)


# --------------------------------------------------------------------------- hub_client

def inbox_files():
    return sorted(paths.inbox_dir().glob("*.json")) if paths.inbox_dir().exists() else []


class Locked:
    """A store that is busy."""
    def update_offer(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    def update_clear(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")


def test_hub_client():
    store = new_store()
    hub_client.set_store_factory(lambda: store)
    try:
        check("offer_update stores a finding", hub_client.offer_update("fetch", "c1", title="C1", installed="1", available="2",
                                                                       size_bytes=10, source="reg") is True
              and ids(store) == ["c1"] and store.list_updates()[0]["source"] == "reg")
        check("clear_update removes it", hub_client.clear_update("fetch", "c1") is True and ids(store) == [])
        check("the steam id is reserved for the hub itself", hub_client.offer_update("steam", "x", title="x") is False)
        check("a malformed provider or an empty id is False, never an exception",
              hub_client.offer_update("Bad Id", "x") is False and hub_client.offer_update("fetch", "") is False
              and hub_client.offer_update(None, None) is False and hub_client.clear_update(5, []) is False)
        for stale in inbox_files():
            stale.unlink()
        store.set_provider_enabled("fetch", False)
        offered = hub_client.offer_update("fetch", "c2", title="C2")
        check("a switched-off provider's offer is refused (False), not spooled", offered is False and not inbox_files())
        store.set_provider_enabled("fetch", True)
        hub_client.set_store_factory(lambda: Locked())
        check("a busy store: offer_update spools and returns True", hub_client.offer_update("fetch", "s1", title="S1", available="7") is True)
        check("a busy store: clear_update spools too", hub_client.clear_update("fetch", "s2") is True)
        files = inbox_files()
        kinds = sorted(json.loads(f.read_text(encoding="utf-8")).get("kind") for f in files)
        check("the spool records say what they are", kinds == ["clear_update", "offer_update"])
        store.update_offer("fetch", "s2", {"title": "S2"})
        hub_client.set_store_factory(lambda: store)
        hub_client.drain_inbox(store)
        check("the daemon applies the spool: the offer lands, the clear removes", "s1" in ids(store) and "s2" not in ids(store))
        check("and the inbox is empty afterwards", not inbox_files())
    finally:
        hub_client.set_store_factory(None)


# --------------------------------------------------------------------------- settings

def test_settings():
    check("defaults: checks on, 24 h", settings.update_settings({}) == {"update_checks": True, "update_interval_h": 24.0})
    check("stored values win", settings.update_settings({"update_checks": False, "update_interval_h": 6})
          == {"update_checks": False, "update_interval_h": 6.0})
    check("a declared starting interval is used until the owner sets one",
          settings.update_settings({}, 2)["update_interval_h"] == 2.0
          and settings.update_settings({"update_interval_h": 12}, 2)["update_interval_h"] == 12.0)
    check("a stored value that no longer validates falls back, never to something unsafe",
          settings.update_settings({"update_interval_h": 0.0001, "update_checks": "maybe"}, None)
          == {"update_checks": True, "update_interval_h": 24.0})
    check("junk stored settings are the defaults", settings.update_settings(None) == settings.update_settings({}))
    clean, errors = settings.validate_provider_settings("fetch", {"update_checks": "off", "update_interval_h": "6.5"})
    check("the validator takes them for ANY provider and normalises",
          not errors and clean == {"update_checks": False, "update_interval_h": 6.5})
    for bad in (0, 0.1, 721, -1, "x", None, True, float("nan"), float("inf")):
        _c, errors = settings.validate_provider_settings("fetch", {"update_interval_h": bad})
        check("update_interval_h %r is refused with a reason" % (bad,), bool(errors) and "update_interval_h" in errors[0])
    _c, errors = settings.validate_provider_settings("fetch", {"update_checks": "perhaps"})
    check("update_checks needs a yes/no", bool(errors))
    for edge in (0.5, 720):
        clean, errors = settings.validate_provider_settings("fetch", {"update_interval_h": edge})
        check("the limits themselves are allowed (%s)" % edge, not errors and clean["update_interval_h"] == float(edge))
    clean, errors = settings.validate_provider_settings("steam", {"notify_min_update_gb": 2, "update_checks": False})
    check("Steam's own key and the update keys can sit together", not errors and clean["notify_min_update_gb"] == 2.0
          and clean["update_checks"] is False)
    check("owner_provider_settings is untouched (existing pins)",
          settings.owner_provider_settings("steam", {"update_checks": False}) == {"notify_min_update_gb": 1.0}
          or "update_checks" not in settings.owner_provider_settings("steam", {"update_checks": False}))
    store = new_store()
    store.update_provider("fetch", settings={"update_checks": False, "update_interval_h": 3})
    saved = store.get_provider("fetch")["settings"]
    check("the owner's choices are stored with the provider", saved.get("update_checks") is False and saved.get("update_interval_h") == 3.0)


if __name__ == "__main__":
    for fn in (test_tables, test_replace_and_clear, test_no_version_comparison, test_dismiss, test_caps_and_off,
               test_cap_500, test_claim, test_hub_client, test_settings):
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
