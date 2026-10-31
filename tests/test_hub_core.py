"""Download Hub, phase 1 core: the pieces with no network and no daemon.

Covers: states (the transition table, number / text / link / thumbnail cleaning,
sanitize_report's percent / speed / ETA rules, smoothing, backoff); netguard (the SSRF guard:
schemes, credentials, loopback / private / link-local / shared / IPv4-mapped addresses, the
allow-list, name resolution with every answer checked, mask_url); naming (hostile file
names, Content-Disposition, sub-folders that must stay inside the library root); settings
(validation, unknown keys are errors); the store (schema, WAL, dedupe, compare-and-set
transitions, claim_next's limits, restart recovery, events, counts, search, providers and
what a provider may write, staleness, heartbeat); and the router.

Run: python tests/test_hub_core.py
"""
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-core-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
# These tests assume no Stremio server is running (the owner's own may be): point the engine at a closed port.
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub import engines, naming, netguard, router, settings as hub_settings, states  # noqa: E402
from jarvis.hub.engines.base import RouteRule  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

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
    """A clock the tests move by hand."""

    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def fresh_store(clock=None):
    _n[0] += 1
    return Store(Path(HOME) / f"hub-{_n[0]}.db", clock=clock or time.time)


def add(store, url="https://example.com/a.bin", engine="http", **kw):
    return store.add_item(engine=engine, source_url=url, title=kw.pop("title", "a.bin"), **kw)[0]


# ---------------------------------------------------------------- states

def test_states():
    check("every state is known", set(states.STATES) >= {"queued", "downloading", "done", "unknown"})
    check("queued -> downloading is allowed for a hub row", states.can_transition("queued", "downloading"))
    check("done -> queued is not (a finished row stays finished)", not states.can_transition("done", "queued"))
    check("paused -> downloading is not (resume goes through queued)", not states.can_transition("paused", "downloading"))
    check("failed -> queued is allowed (retry)", states.can_transition("failed", "queued"))
    check("cancelled -> queued is allowed (retry)", states.can_transition("cancelled", "queued"))
    check("an unknown state is never allowed", not states.can_transition("queued", "exploded"))
    check("the same state is always fine", states.can_transition("done", "done"))
    check("a provider row may go anywhere (it is a mirror)", states.can_transition("done", "downloading", owner="provider"))

    check("clean_number rejects NaN", states.clean_number(float("nan")) is None)
    check("clean_number rejects infinity", states.clean_number(math.inf) is None)
    check("clean_number rejects negatives", states.clean_number(-1) is None)
    check("clean_number rejects booleans", states.clean_number(True) is None)
    check("clean_number rejects text", states.clean_number("fast") is None)
    check("clean_number accepts numeric text", states.clean_number("12") == 12.0)
    check("clean_percent clamps above 100", states.clean_percent(150) == 100.0)
    check("clean_percent clamps below 0", states.clean_percent(-20) == 0.0)
    check("clean_percent of junk is None", states.clean_percent("x") is None)
    check("derive_percent needs a total", states.derive_percent(5, 0) is None and states.derive_percent(5, None) is None)
    check("derive_percent is clamped", states.derive_percent(500, 100) == 100.0)

    check("smooth_speed starts at the first sample", states.smooth_speed(None, 100, 1) == 100)
    mid = states.smooth_speed(100, 200, 10)
    check("smooth_speed moves toward the new value without jumping to it", 100 < mid < 200)
    check("smooth_speed is time-based: a long gap moves further than a short one",
          states.smooth_speed(100, 200, 30) > states.smooth_speed(100, 200, 1))
    tracker = states.RateTracker(clock=lambda: 0.0)
    check("RateTracker's first sample has no speed yet", tracker.update(0, now=0.0) is None)
    check("RateTracker measures bytes per second", abs(tracker.update(1000, now=1.0) - 1000) < 1e-6)
    check("RateTracker ignores a sample that comes too soon", tracker.update(5000, now=1.1) == tracker.speed)
    check("eta from speed", states.RateTracker.eta(2000, 1000, 500) == 2.0)
    check("eta is None without a speed", states.RateTracker.eta(2000, 1000, 0) is None)
    check("backoff doubles and is capped",
          [states.backoff_seconds(n) for n in (1, 2, 3)] == [5, 10, 20] and states.backoff_seconds(30) == 300)

    check("clean_text strips control characters", states.clean_text("a\x00b\x07c\nd") == "abc d")
    check("clean_text caps its length", len(states.clean_text("x" * 1000, 50)) == 50)
    check("clean_text keeps None (a clear)", states.clean_text(None) is None)
    links = states.clean_links([
        {"label": "Store", "kind": "url", "target": "https://example.com/x"},
        {"label": "Steam", "kind": "url", "target": "steam://nav/games"},
        {"label": "Bad", "kind": "url", "target": "javascript:alert(1)"},
        {"label": "File", "kind": "url", "target": "file:///C:/Windows"},
        {"label": "Tool", "kind": "tool", "target": "my_tool"},
        {"label": "Tool2", "kind": "tool", "target": "Not A Tool"},
    ])
    check("links: https and steam pass, javascript: and file: are dropped",
          [l["label"] for l in links] == ["Store", "Steam", "Tool"])
    check("links: steam is dropped when the owner doesn't allow it",
          [l["label"] for l in states.clean_links([{"label": "S", "kind": "url", "target": "steam://x"}], ("https",))] == [])
    check("only a small image data-URI is a thumbnail",
          states.clean_thumb("data:image/png;base64,AAAA") is not None
          and states.clean_thumb("file:///etc/passwd") is None
          and states.clean_thumb("data:text/html;base64,AAAA") is None
          and states.clean_thumb("data:image/png;base64," + "A" * 300000) is None)
    check("capabilities keep only known verbs", states.clean_capabilities(["pause", "fly", "pause", "cancel"]) == ["pause", "cancel"])
    check("controls keep only valid tool names for known verbs",
          states.clean_controls({"pause": "my_pause", "cancel": "No Spaces", "fly": "x"}) == {"pause": "my_pause"})
    check("detail is capped", len(states.clean_detail([{"label": "L%d" % i, "value": "v"} for i in range(50)])) == states.MAX_DETAIL_ROWS)
    check("children are capped", len(states.clean_children([{"name": "f%d" % i} for i in range(500)])) == states.MAX_CHILDREN)

    # --- sanitize_report
    clean, notes, derived = states.sanitize_report({"percent": 150})
    check("a percent above 100 is clamped", clean["percent"] == 100.0)
    clean, notes, derived = states.sanitize_report({"percent": None})
    check("an explicit percent None means indeterminate and is kept", clean["percent"] is None and "percent" in clean)
    clean, notes, derived = states.sanitize_report({"bytes_done": 250, "bytes_total": 1000})
    check("an omitted percent is derived from the bytes and flagged", clean["percent"] == 25.0 and "percent" in derived)
    clean, notes, derived = states.sanitize_report({"percent": 80, "bytes_done": 100, "bytes_total": 1000})
    check("a disagreeing percent is kept (the provider's word) but noted",
          clean["percent"] == 80 and any("disagrees" in n for n in notes) and "percent" not in derived)
    clean, notes, derived = states.sanitize_report({"bytes_done": -5, "speed_bps": float("nan"), "percent": "abc"})
    check("garbage numbers are ignored, not stored", "bytes_done" not in clean and "speed_bps" not in clean and "percent" not in clean)
    check("and each ignored field is noted", len(notes) >= 3)
    clean, notes, derived = states.sanitize_report({"state": "done", "percent": 40})
    check("done forces 100 percent", clean["percent"] == 100.0 and clean["eta_s"] == 0.0 and clean["speed_bps"] is None)
    clean, notes, derived = states.sanitize_report({"state": "exploded"})
    check("an unknown state is ignored and noted", "state" not in clean and notes)
    clean, notes, derived = states.sanitize_report({"kind": "weird"})
    check("an unknown kind is ignored", "kind" not in clean)
    prev = {"bytes_done": 0, "bytes_total": 1000, "last_report_at": 100.0, "speed_bps": None,
            "state": "downloading", "percent": None}
    clean, notes, derived = states.sanitize_report({"bytes_done": 500}, prev=prev, now=102.0)
    check("speed is derived from successive byte counts", abs(clean["speed_bps"] - 250) < 1e-6 and "speed" in derived)
    check("eta is derived from the derived speed", abs(clean["eta_s"] - 2.0) < 1e-6 and "eta" in derived)
    clean, notes, derived = states.sanitize_report({"bytes_done": 500, "speed_bps": 999, "eta_s": 7}, prev=prev, now=102.0)
    check("a reported speed and eta are never replaced by guesses", clean["speed_bps"] == 999 and clean["eta_s"] == 7 and not derived - {"percent"})
    prev2 = dict(prev, percent=60.0, bytes_done=600)
    clean, notes, derived = states.sanitize_report({"percent": 30}, prev=prev2, now=102.0)
    check("a percent going backwards is accepted but noted", clean["percent"] == 30 and any("backwards" in n for n in notes))
    clean, notes, derived = states.sanitize_report({"state": "paused"}, prev=prev, now=102.0)
    check("pausing clears speed and eta", clean["speed_bps"] is None and clean["eta_s"] is None)
    clean, notes, derived = states.sanitize_report({"bytes_done": 5000, "bytes_total": 1000})
    check("bytes_done above the total is clamped", clean["bytes_done"] == 1000)
    clean, notes, derived = states.sanitize_report({"title": "x" * 1000, "message": "ok\x00"})
    check("text fields are capped and cleaned",
          len(clean["title"]) == states.MAX_TITLE and clean["message"] == "ok")


# ---------------------------------------------------------------- netguard

def _resolver(mapping):
    def resolve(host, port):
        if host not in mapping:
            raise OSError("no such host")
        return mapping[host]
    return resolve


def refused(url, allow=(), resolver=None):
    try:
        netguard.check_url(url, allow, resolver)
        return False
    except netguard.GuardError:
        return True


def test_netguard():
    check("a public literal address passes", not refused("http://93.184.216.34/x"))
    check("loopback is refused", refused("http://127.0.0.1/x"))
    check("localhost by name is refused (it resolves to loopback)", refused("http://localhost/x", resolver=_resolver({"localhost": ["127.0.0.1"]})))
    check("10.x is refused", refused("http://10.0.0.5/"))
    check("192.168.x is refused", refused("http://192.168.1.1/admin"))
    check("172.16.x is refused", refused("http://172.16.0.1/"))
    check("link-local (cloud metadata) is refused", refused("http://169.254.169.254/latest/meta-data"))
    check("shared address space 100.64.x is refused", refused("http://100.64.0.1/"))
    check("0.0.0.0 is refused", refused("http://0.0.0.0/"))
    check("IPv6 loopback is refused", refused("http://[::1]/"))
    check("an IPv4-mapped loopback is refused", refused("http://[::ffff:127.0.0.1]/"))
    check("multicast is refused", refused("http://224.0.0.1/"))
    check("file: is refused", refused("file:///etc/passwd"))
    check("ftp: is refused", refused("ftp://example.com/x"))
    check("javascript: is refused", refused("javascript:alert(1)"))
    check("a URL with credentials is refused", refused("http://user:pw@93.184.216.34/x"))
    check("a URL without a host is refused", refused("http:///path"))
    check("an empty URL is refused", refused(""))
    check("an invalid port is refused", refused("http://93.184.216.34:99999/x"))
    check("a name that resolves only to public addresses passes",
          not refused("http://files.example/x", resolver=_resolver({"files.example": ["93.184.216.34"]})))
    check("a name with ANY private answer is refused (every answer is checked)",
          refused("http://sneaky.example/x", resolver=_resolver({"sneaky.example": ["93.184.216.34", "10.0.0.1"]})))
    check("a name that does not resolve is refused", refused("http://nope.example/x", resolver=_resolver({})))
    check("allow-list: host:port lets that exact port through", not refused("http://127.0.0.1:11470/x", allow=["127.0.0.1:11470"]))
    check("allow-list: another port is still refused", refused("http://127.0.0.1:8080/x", allow=["127.0.0.1:11470"]))
    check("allow-list: a bare host allows any port", not refused("http://127.0.0.1:8080/x", allow=["127.0.0.1"]))
    check("allow-list: a name allows what it resolves to", not refused("http://nas.lan/x", allow=["nas.lan"], resolver=_resolver({"nas.lan": ["192.168.1.9"]})))
    check("allow-list entries that can't be read allow nothing", netguard.parse_allow(["", "host:notaport", "[::1"]) == set())
    check("parse_allow understands IPv6 with a port", ("::1", 8080) in netguard.parse_allow(["[::1]:8080"]))
    check("mask_url hides credentials and the query",
          netguard.mask_url("https://u:p@host.example:8443/a/b?token=SECRET") == "https://host.example:8443/a/b?…")
    check("mask_url survives a bad port", netguard.mask_url("http://host.example:99999/x").startswith("http://host.example"))


# ---------------------------------------------------------------- naming

def test_naming():
    s = naming.sanitize_filename
    check("path separators collapse to the last part", s("a/b/c.txt") == "c.txt" and s("..\\..\\evil.exe") == "evil.exe")
    check("reserved Windows characters are replaced", "<" not in s("a<b>:c|d?.txt") and "?" not in s("a<b>:c|d?.txt"))
    check("a Windows device name is defused", s("CON") != "CON" and s("nul.txt").lower() != "nul.txt" and s("COM1.log") != "COM1.log")
    check("trailing dots and spaces are removed", not s("name. . ").endswith((".", " ")))
    check("'..' never survives", ".." not in s("a..b..c.txt"))
    check("an empty name falls back to the default", s("") == "download" and s("...") == "download" and s(None) == "download")
    check("control characters are replaced", "\x00" not in s("a\x00b.txt") and "\n" not in s("a\nb.txt"))
    long_name = s("x" * 500 + ".mkv")
    check("a long name is cut but keeps its extension", len(long_name) <= naming.MAX_NAME and long_name.endswith(".mkv"))
    check("a leading-dot name stays usable", s(".gitignore") != "" and "/" not in s(".gitignore"))
    check("a sane name is unchanged", s("My Movie (2020).mkv") == "My Movie (2020).mkv")

    cd = naming.filename_from_content_disposition
    check("Content-Disposition: quoted filename", cd('attachment; filename="report 2024.pdf"') == "report 2024.pdf")
    check("Content-Disposition: unquoted filename", cd("attachment; filename=report.pdf") == "report.pdf")
    check("Content-Disposition: filename* wins and is decoded",
          cd("attachment; filename=\"fallback.txt\"; filename*=UTF-8''%E2%82%AC%20rates.txt") == "€ rates.txt")
    check("Content-Disposition: an escaped quote", cd(r'attachment; filename="a\"b.txt"') == 'a"b.txt')
    check("Content-Disposition: absent", cd(None) is None and cd("inline") is None)
    check("a hostile Content-Disposition still sanitises to a plain name",
          s(cd('attachment; filename="..\\..\\Windows\\evil.exe"')) == "evil.exe")
    check("filename_from_url decodes and takes the last segment", naming.filename_from_url("https://h/x/My%20File.zip?x=1") == "My File.zip")
    check("filename_from_url of a bare host is None", naming.filename_from_url("https://h/") is None)
    check("kind_for: video / audio / file",
          naming.kind_for("a.mkv") == "video" and naming.kind_for("a.flac") == "audio" and naming.kind_for("a.zip") == "file")
    check("kind_for uses the content type when the name has no clue", naming.kind_for("download", "video/mp4") == "video")
    check("an extension can come from the content type", naming.ext_from_content_type("application/pdf; charset=x") == ".pdf")

    root = Path(HOME) / "lib"
    root.mkdir(exist_ok=True)
    check("safe_subdir: a plain sub-folder", naming.safe_subdir(root, "Manuals") == root / "Manuals")
    check("safe_subdir: nested", naming.safe_subdir(root, "A/B") == root / "A" / "B")
    check("safe_subdir: empty means the root itself", naming.safe_subdir(root, "") == root)
    check("safe_subdir: '..' is refused", naming.safe_subdir(root, "../x") is None and naming.safe_subdir(root, "a/../../x") is None)
    check("safe_subdir: absolute paths are refused", naming.safe_subdir(root, "/etc") is None and naming.safe_subdir(root, "C:\\Windows") is None)
    check("safe_subdir: too deep is refused", naming.safe_subdir(root, "a/b/c/d/e/f") is None)
    check("safe_subdir: a bad part is sanitised, not trusted", naming.safe_subdir(root, "A<B") is not None and naming.under(root, naming.safe_subdir(root, "A<B")))
    check("under: inside and outside", naming.under(root, root / "x" / "y") and not naming.under(root, root.parent / "other"))
    check("under: a '..' that escapes is outside", not naming.under(root, str(root / ".." / "escape")))
    taken = root / "f.bin"
    taken.write_bytes(b"x")
    check("unique_path adds a counter", naming.unique_path(taken).name == "f (1).bin")
    (root / "g.bin.part").write_bytes(b"x")
    check("a foreign .part counts as taken", naming.unique_path(root / "g.bin").name == "g (1).bin")
    check("but your own .part doesn't, when finishing", naming.unique_path(root / "g.bin", ignore_part=True).name == "g.bin")


# ---------------------------------------------------------------- settings

def test_settings():
    check("defaults carry the plan's concurrency", hub_settings.DEFAULTS["concurrency"]["total"] == 4)
    merged = hub_settings.merged({"concurrency": {"http": 1}, "rate_limit_kbps": 500})
    check("stored overrides sit on the defaults one level deep",
          merged["concurrency"]["http"] == 1 and merged["concurrency"]["total"] == 4 and merged["rate_limit_kbps"] == 500)
    check("merged does not mutate DEFAULTS", hub_settings.DEFAULTS["concurrency"]["http"] == 3)
    clean, errors = hub_settings.validate_update({"concurrency.http": 2, "rate_limit_kbps": "100"})
    check("valid updates are cleaned", clean == {"concurrency.http": 2, "rate_limit_kbps": 100} and not errors)
    clean, errors = hub_settings.validate_update({"nonsense": 1})
    check("an unknown key is an error, never silently ignored", errors and not clean)
    clean, errors = hub_settings.validate_update({"provider_limits.stale_after_s": 5})
    check("a setting no phase has opened yet is an error too", errors and not clean)
    check("concurrency out of range is refused", hub_settings.validate_update({"concurrency.total": 99})[1])
    check("a boolean is not a whole number", hub_settings.validate_update({"concurrency.total": True})[1])
    check("a relative library_root is refused", hub_settings.validate_update({"library_root": "relative/dir"})[1])
    check("a drive root is refused as library_root", hub_settings.validate_update({"library_root": "/"})[1])
    clean, errors = hub_settings.validate_update({"library_root": str(Path(HOME) / "Lib")})
    check("an absolute library_root is accepted", not errors and clean["library_root"].endswith("Lib"))
    clean, errors = hub_settings.validate_update({"library_root": ""})
    check("an empty library_root resets to the default", not errors and clean["library_root"] == "")
    check("allow_private_hosts must be a list of plain hosts", hub_settings.validate_update({"allow_private_hosts": ["a b"]})[1]
          and hub_settings.validate_update({"allow_private_hosts": ["http://x/y"]})[1])
    clean, errors = hub_settings.validate_update({"allow_private_hosts": ["127.0.0.1:11470", "nas.lan"]})
    check("a good allow-list is accepted", not errors and clean["allow_private_hosts"] == ["127.0.0.1:11470", "nas.lan"])
    check("nothing to change is an error", hub_settings.validate_update({})[1])
    stored = {"concurrency": {"total": 4}}
    changed = hub_settings.apply_dotted(stored, {"concurrency.http": 2, "rate_limit_kbps": 5})
    check("apply_dotted folds nested keys", stored["concurrency"] == {"total": 4, "http": 2} and stored["rate_limit_kbps"] == 5
          and changed == ["concurrency", "rate_limit_kbps"])
    check("library_root defaults under the user's Downloads", str(hub_settings.library_root({"library_root": ""})).endswith(os.path.join("Downloads", "Jarvis")))


# ---------------------------------------------------------------- store

def test_store_basics():
    store = fresh_store()
    check("a new store is at the current schema", store.schema_version() == 2)
    check("and runs in WAL mode", str(store.journal_mode()).lower() == "wal")
    check("and passes its integrity check", store.quick_check() == "ok")
    item, created = store.add_item(engine="http", source_url="https://e.com/a.bin", title="a.bin", dedupe_key="k1")
    check("add_item creates a queued hub row", created and item["state"] == "queued" and item["owner"] == "hub" and item["id"].startswith("h_"))
    again, created2 = store.add_item(engine="http", source_url="https://e.com/a.bin", title="a.bin", dedupe_key="k1")
    check("the same dedupe key returns the live item, not a second row", not created2 and again["id"] == item["id"])
    store.transition(item["id"], "cancelled")
    third, created3 = store.add_item(engine="http", source_url="https://e.com/a.bin", title="a.bin", dedupe_key="k1")
    check("a finished (cancelled) item doesn't block adding it again", created3 and third["id"] != item["id"])
    paused, _ = store.add_item(engine="http", source_url="https://e.com/p", title="p", state="paused")
    check("start_paused queues it paused", paused["state"] == "paused")
    check("a bad state for a new item falls back to queued", store.add_item(engine="http", source_url="https://e.com/q", title="q", state="done")[0]["state"] == "queued")

    row = add(store, "https://e.com/t.bin")
    check("a legal transition works and is logged", store.transition(row["id"], "probing", expect="queued")["state"] == "probing"
          and any("queued -> probing" in e["message"] for e in store.list_events(row["id"])))
    check("an illegal transition is refused and logged", store.transition(paused["id"], "downloading") is None
          and store.get_item(paused["id"])["state"] == "paused"
          and any("refused" in e["message"] for e in store.list_events(paused["id"])))
    done = store.transition(row["id"], "done")
    check("probing -> done is allowed and stamps finished_at", done["state"] == "done" and done["finished_at"] and done["percent"] == 100.0)
    check("done -> queued is refused and the row is untouched", store.transition(row["id"], "queued") is None and store.get_item(row["id"])["state"] == "done")
    check("compare-and-set: the wrong expected state does nothing", store.transition(paused["id"], "queued", expect="downloading") is None)
    check("a missing row returns None", store.transition("h_nope", "queued") is None and store.get_item("h_nope") is None)

    live = add(store, "https://e.com/prog.bin")
    store.transition(live["id"], "downloading", expect="queued")
    updated = store.report_progress(live["id"], bytes_done=500, bytes_total=1000)
    check("progress derives the percent and flags it", updated["percent"] == 50.0 and "percent" in updated["derived"])
    updated = store.report_progress(live["id"], percent=250)
    check("progress clamps a wild percent", updated["percent"] == 100.0)
    updated = store.report_progress(live["id"], bytes_done=-1)
    check("progress ignores garbage and keeps what it knew", updated["bytes_done"] == 500)
    check("progress never changes the state by itself", store.get_item(live["id"])["state"] == "downloading")

    store.set_control(live["id"], "pause")
    check("a control request survives progress writes and active->active moves",
          store.report_progress(live["id"], bytes_done=600)["control"] == "pause"
          and store.transition(live["id"], "postprocessing", expect="downloading")["control"] == "pause")
    paused_now = store.transition(live["id"], "paused", expect="postprocessing")
    check("but is answered (cleared) when the row stops being active", paused_now["control"] == "")
    check("set_control refuses anything but pause / cancel / clear", store.set_control(live["id"], "explode") is None)
    check("pending_controls lists requests", store.set_control(row["id"], "cancel") is not None and [c["id"] for c in store.pending_controls()] == [row["id"]])

    retry = add(store, "https://e.com/r.bin")
    store.transition(retry["id"], "failed", error="boom")
    back = store.transition(retry["id"], "queued", error=None, attempts=0)
    check("failed -> queued clears the error and finished_at", back["error"] is None and back["finished_at"] is None)

    for i in range(130):
        store.add_event(retry["id"], "info", "e%d" % i)
    check("events are trimmed per item", len(store.list_events(retry["id"], limit=500)) <= 130)
    check("events come back oldest first", store.list_events(retry["id"], limit=3)[-1]["message"] == "e129")

    rid = add(store, "https://e.com/groups.bin", group_id=store.ensure_group("My batch"))["id"]
    check("groups are listed with their title", any(g["title"] == "My batch" for g in store.list_groups().values()))
    listed = store.list_items(group_id=store.get_item(rid)["group_id"])
    check("list_items carries the group title", listed and listed[0]["group_title"] == "My batch")
    check("search matches a title and treats % literally",
          [i["id"] for i in store.list_items(search="groups")] == [rid] and store.list_items(search="%") == [])
    check("list_items filters by state", all(i["state"] == "paused" for i in store.list_items(states_in=["paused"])))
    check("counts add up", store.counts()["total"] == len(store.list_items(limit=2000)))
    check("delete_items removes rows and their events", store.delete_items([rid]) == 1 and store.get_item(rid) is None)


def test_claim_and_recovery():
    clock = Clock()
    store = fresh_store(clock)
    a = add(store, "https://e.com/1", title="1")
    clock.advance(1)
    b = add(store, "https://e.com/2", title="2")
    clock.advance(1)
    c = add(store, "https://e.com/3", title="3", priority=5)
    first = store.claim_next({"default": 2}, total_cap=4)
    check("claim_next picks the highest priority first", first["id"] == c["id"] and first["state"] == "probing" and first["attempts"] == 1)
    second = store.claim_next({"default": 2}, total_cap=4)
    check("then oldest first", second["id"] == a["id"])
    check("the per-engine cap holds", store.claim_next({"default": 2}, total_cap=4) is None)
    check("a different engine has its own cap", store.add_item(engine="ytdlp", source_url="https://e.com/y", title="y")[0] and
          store.claim_next({"default": 2}, total_cap=4)["engine"] == "ytdlp")
    check("the total cap holds", store.claim_next({"default": 9}, total_cap=3) is None)
    store.transition(a["id"], "failed", error="x")
    later = add(store, "https://e.com/later", title="later")
    store.update_item(later["id"], {"next_attempt_at": clock() + 60})
    check("an item waiting for its retry time is skipped", store.claim_next({"default": 9}, total_cap=9)["id"] == b["id"])
    check("...and picked once its time has come", (clock.advance(61) or True) and store.claim_next({"default": 9}, total_cap=9)["id"] == later["id"])
    ctl = add(store, "https://e.com/ctl", title="ctl")
    store.set_control(ctl["id"], "pause")
    check("an item with a pending control request isn't claimed", store.claim_next({"default": 9}, total_cap=9) is None)
    store.set_control(ctl["id"], "")
    check("...and is once the request is cleared", store.claim_next({"default": 9}, total_cap=9)["id"] == ctl["id"])
    stalled = add(store, "https://e.com/st", title="st")
    store.transition(stalled["id"], "probing", expect="queued")
    store.transition(stalled["id"], "stalled", expect="probing", next_attempt_at=clock() + 10, message="retry")
    check("a stalled item waits for its retry time", store.claim_next({"default": 9}, total_cap=9) is None)
    clock.advance(11)
    reclaimed = store.claim_next({"default": 9}, total_cap=9)
    check("and is claimed again when it comes", reclaimed and reclaimed["id"] == stalled["id"] and reclaimed["state"] == "probing")

    s2 = fresh_store()
    x = add(s2, "https://e.com/x", title="x")
    s2.transition(x["id"], "probing", expect="queued")
    s2.transition(x["id"], "downloading", expect="probing")
    y = add(s2, "https://e.com/y", title="y")
    s2.transition(y["id"], "paused")
    prov, _ = s2.upsert_provider_row("demo", "r1", {"state": "downloading"})
    moved = s2.recover_after_restart()
    check("recovery puts running hub rows back in the queue", moved == 1 and s2.get_item(x["id"])["state"] == "queued")
    check("recovery leaves paused rows alone", s2.get_item(y["id"])["state"] == "paused")
    check("recovery leaves provider rows alone", s2.get_item("demo:r1")["state"] == "downloading")


def test_providers():
    clock = Clock()
    store = fresh_store(clock)
    p, err = store.register_provider("model_fetch", label="Model fetch", capabilities=["pause", "cancel", "fly"],
                                     controls={"pause": "mf_pause", "resume": "mf_resume"}, open_roots=[str(Path(HOME) / "Models")])
    check("a provider registers", p and not err and p["capabilities"] == ["pause", "cancel"])
    check("its controls must match declared capabilities", p["controls"] == {"pause": "mf_pause"})
    check("a reserved id is refused", store.register_provider("http")[1] and store.register_provider("hub")[1])
    check("a bad id is refused", store.register_provider("Bad Id")[1] and store.register_provider("a:b")[1])
    check("a bad mode is refused", store.register_provider("ok_one", mode="weird")[1])
    store.set_provider_enabled("model_fetch", False)
    p2, _ = store.register_provider("model_fetch", label="Model fetcher", capabilities=["pause"],
                                    open_roots=[str(Path(HOME) / "Models")])
    check("re-registering refreshes what the tool supplies", p2["label"] == "Model fetcher" and p2["capabilities"] == ["pause"])
    check("but never overwrites what the owner set (enabled)", p2["enabled"] is False)
    store.set_provider_enabled("model_fetch", True)

    item, problems = store.upsert_provider_row("model_fetch", "llama", {"title": "llama.gguf", "state": "downloading", "bytes_done": 10, "bytes_total": 100})
    check("a provider row is created under its own namespaced id", item["id"] == "model_fetch:llama" and item["owner"] == "provider" and item["percent"] == 10.0)
    hub_item = add(store, "https://e.com/own", title="own")
    check("a provider can't reach a hub row by guessing its id", store.upsert_provider_row("model_fetch", hub_item["id"].split("_", 1)[1], {"title": "x"}, create=False)[0] is None)
    store.upsert_provider_row("other_tool", "llama", {"title": "other"})
    check("two providers with the same local id get different rows", store.get_item("other_tool:llama")["title"] == "other"
          and store.get_item("model_fetch:llama")["title"] == "llama.gguf")
    check("a reserved provider can't write", store.upsert_provider_row("http", "x", {"title": "t"})[0] is None
          and store.upsert_provider_row("Bad Id", "x", {"title": "t"})[0] is None)
    check("an empty local id is refused", store.upsert_provider_row("model_fetch", "", {"title": "t"})[0] is None)
    check("create=False on a missing row does nothing", store.upsert_provider_row("model_fetch", "nope", {"title": "t"}, create=False)[0] is None)

    item, problems = store.upsert_provider_row("model_fetch", "llama", {"capabilities": ["pause", "remove", "cancel"]})
    check("a row's capabilities can only narrow the provider's declared ones", item["capabilities"] == ["pause"])
    item, problems = store.upsert_provider_row("model_fetch", "llama", {"dest": str(Path(HOME) / "Elsewhere" / "x.gguf")})
    check("a destination outside open_roots is dropped and noted", item["dest_file"] is None and problems)
    inside = str(Path(HOME) / "Models" / "x.gguf")
    item, problems = store.upsert_provider_row("model_fetch", "llama", {"dest": inside})
    check("a destination inside open_roots is kept", item["dest_file"] == inside)
    item, _ = store.upsert_provider_row("model_fetch", "llama", {"links": [{"label": "x", "kind": "url", "target": "javascript:1"}], "detail": [{"label": "a", "value": "b"}],
                                                            "thumb": "file:///etc/passwd"})
    check("links are filtered, detail kept, a bad thumbnail dropped", item["links"] == [] and item["detail"] == [{"label": "a", "value": "b"}] and item["thumb_ref"] is None)
    item, _ = store.upsert_provider_row("model_fetch", "llama", {"group": "season", "group_title": "Season 1"})
    check("a provider group is namespaced and titled", item["group_id"] == "model_fetch:season" and store.list_groups()["model_fetch:season"]["title"] == "Season 1")
    check("an unknown key in a report is ignored, not stored", store.upsert_provider_row("model_fetch", "llama", {"owner": "hub", "engine": "http", "control": "cancel"})[0]["owner"] == "provider"
          and store.get_item("model_fetch:llama")["control"] == "")
    check("an oversized report is dropped", store.upsert_provider_row("model_fetch", "big", {"title": "x" * 70000})[0] is None
          and "report" in (store.get_provider("model_fetch")["last_error"] or ""))

    store.set_provider_enabled("model_fetch", False)
    check("reports from a switched-off provider are ignored", store.upsert_provider_row("model_fetch", "llama", {"title": "again"})[0] is None)
    check("and its rows are hidden from the list", all(i["provider"] != "model_fetch" for i in store.list_items(limit=2000)))
    check("and from the counts", "model_fetch" not in store.counts()["by_provider"])
    check("unless asked for", any(i["provider"] == "model_fetch" for i in store.list_items(include_disabled=True, limit=2000)))
    store.set_provider_enabled("model_fetch", True)

    small = fresh_store()
    small.set_settings({"concurrency.total": 1})
    small.set_setting_raw("provider_limits", {"max_rows": 2, "max_report_kb": 64, "max_reports_per_s": 20, "stale_after_s": 30})
    small.upsert_provider_row("flood", "1", {"state": "downloading"})
    small.upsert_provider_row("flood", "2", {"state": "downloading"})
    check("past max_rows new rows are dropped and the provider's health says so",
          small.upsert_provider_row("flood", "3", {"state": "downloading"})[0] is None and "rows" in (small.get_provider("flood")["last_error"] or ""))
    check("existing rows still update", small.upsert_provider_row("flood", "1", {"percent": 5})[0]["percent"] == 5)
    check("a finished row frees a slot", small.upsert_provider_row("flood", "1", {"state": "done"})[0] and small.upsert_provider_row("flood", "3", {"state": "downloading"})[0] is not None)


def test_staleness_and_heartbeat():
    clock = Clock()
    store = fresh_store(clock)
    store.upsert_provider_row("quiet", "a", {"state": "downloading", "percent": 10}, now=clock())
    store.upsert_provider_row("quiet", "done1", {"state": "done"}, now=clock())
    hub_row = add(store, "https://e.com/h", title="h")
    store.transition(hub_row["id"], "probing", expect="queued")
    check("a fresh row isn't stale", store.mark_stale(now=clock())["stale"] == 0)
    clock.advance(31)
    res = store.mark_stale(now=clock())
    check("a row that stopped reporting is flagged stale, not failed", res["stale"] == 1 and store.get_item("quiet:a")["stale"] and store.get_item("quiet:a")["state"] == "downloading")
    check("a finished row is never flagged", not store.get_item("quiet:done1")["stale"])
    check("a hub row is never flagged (a dead daemon is shown another way)", not store.get_item(hub_row["id"])["stale"])
    check("flagging is done once", store.mark_stale(now=clock())["stale"] == 0)
    store.upsert_provider_row("quiet", "a", {"percent": 20}, now=clock())
    check("a new report clears the flag", not store.get_item("quiet:a")["stale"])
    clock.advance(86400 + 60)
    res = store.mark_stale(now=clock())
    check("after a day it becomes unknown and leaves the active counters", res["unknown"] == 1 and store.get_item("quiet:a")["state"] == "unknown")
    store.register_provider("slowpoke", label="Slow", interval_s=60)
    store.upsert_provider_row("slowpoke", "s", {"state": "downloading"}, now=clock())
    clock.advance(100)
    check("a provider that polls slowly gets a longer grace (5 intervals)", store.mark_stale(now=clock())["stale"] == 0)
    clock.advance(250)
    check("...but not forever", store.mark_stale(now=clock())["stale"] == 1)

    check("no heartbeat means no daemon", not store.daemon_info()["alive"])
    store.daemon_beat(4242, clock())
    check("a fresh heartbeat is alive", store.daemon_info()["alive"] and store.daemon_info()["pid"] == 4242)
    clock.advance(20)
    check("an old heartbeat is not", not store.daemon_info()["alive"])
    store.daemon_beat(4242, clock())
    check("the heartbeat is not a setting", "daemon" not in store.raw_settings())


def test_settings_roundtrip():
    store = fresh_store()
    check("defaults read back", store.get_settings()["concurrency"]["total"] == 4)
    store.set_settings({"concurrency.http": 1, "allow_private_hosts": ["127.0.0.1"], "rate_limit_kbps": 300})
    got = store.get_settings()
    check("a change is stored and merged over the defaults", got["concurrency"]["http"] == 1 and got["concurrency"]["total"] == 4
          and got["allow_private_hosts"] == ["127.0.0.1"] and got["rate_limit_kbps"] == 300)
    store.set_settings({"concurrency.total": 2})
    check("a second nested change keeps the first", store.get_settings()["concurrency"] == {"total": 2, "http": 1, "ytdlp": 2, "stremio": 3, "qbittorrent": 2, "hosters": 3})


# ---------------------------------------------------------------- router and engines

def test_router():
    r = router.route("https://example.com/file.zip")
    check("a direct link goes to the HTTP engine", r["ok"] and r["engine"] == "http" and "direct link" in r["reason"])
    check("surrounding whitespace is tolerated", router.route("  https://example.com/a  ")["ok"])
    m = router.route("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567")
    check("a magnet link goes to the stremio engine and, with its server down, says how to start it",
          not m["ok"] and m["engine"] == "stremio" and "isn't ready" in m["problem"] and "stack_start" in m["problem"])
    check("a bare infohash is recognised as a torrent", not router.route("0123456789abcdef0123456789abcdef01234567")["ok"])
    y = router.route("https://www.youtube.com/watch?v=abc")
    check("a YouTube page is never fed to the HTTP engine (it goes to yt-dlp, or says yt-dlp isn't ready)",
          y["engine"] == "ytdlp" and (y["ok"] or "ytdlp" in (y["problem"] or "")))
    check("garbage gets a plain problem", not router.route("hello world")["ok"] and router.route("hello world")["problem"])
    check("empty input is a problem", not router.route("")["ok"] and not router.route(None)["ok"])
    check("an absurdly long input is refused", not router.route("https://e.com/" + "a" * 5000)["ok"])
    check("forcing a missing engine names the installed ones", "http" in router.route("https://e.com/x", engine="nope")["problem"])
    check("forcing http on a magnet link is refused", not router.route("magnet:?xt=urn:btih:abc", engine="http")["ok"])
    forced = router.route("https://e.com/x", engine="http")
    check("forcing http on a link works and says so", forced["ok"] and "you chose" in forced["reason"])

    def boom(text, settings):
        raise RuntimeError("bad rule")

    engines.load()
    engines._RULES.append(RouteRule("http", 99, boom))
    try:
        check("a rule that raises doesn't break routing", router.route("https://example.com/x")["ok"])
    finally:
        engines.load(force=True)

    check("canonical_url lower-cases host and drops default port and fragment",
          router.canonical_url("HTTPS://Example.COM:443/A?x=1#frag") == "https://example.com/A?x=1")
    check("canonical_url keeps a non-default port", router.canonical_url("http://example.com:8080/a") == "http://example.com:8080/a")
    check("dedupe_key depends on the options that matter",
          router.dedupe_key("http", "u", {"subdir": "A"}) != router.dedupe_key("http", "u", {"subdir": "B"})
          and router.dedupe_key("http", "u", {}) == router.dedupe_key("http", "u", None))


def test_engines_registry():
    engines.load(force=True)
    check("the HTTP engine is discovered", "http" in engines.names())
    check("nothing failed to load", engines.LOAD_ERRORS == {})
    check("the HTTP engine is available (requests is a dependency)", engines.availability()["http"]["available"])
    check("loading twice is harmless", (engines.load() or True) and engines.names().count("http") == 1)
    check("the HTTP engine contributes a low-priority route rule", any(r.engine == "http" and r.priority <= 10 for r in engines.route_rules()))


try:
    test_states()
    test_netguard()
    test_naming()
    test_settings()
    test_store_basics()
    test_claim_and_recovery()
    test_providers()
    test_staleness_and_heartbeat()
    test_settings_roundtrip()
    test_router()
    test_engines_registry()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
