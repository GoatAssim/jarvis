"""OptiFine (agent P6): the parser, the build choice, the hop-by-hop link, the jar check and the two tools.

    python3 tests/test_optifine.py

Standalone, plain asserts, no network: every request goes to tests/_modhosts_fake_optifine.py (an in-process
optifine.net behind the interface contract's `fetch` signature).  HOME is redirected before any jarvis import.

What is pinned down:
  * the parser against a hand-made page in the real markup (tests/data/modhosts/hand_p6_downloads.html) AND against the
    probe's saved page of the live site (optifine_downloads.html + optifine_rows.json: those tests print SKIP, and do
    not count as passed, when the probe files are not in tests/data/modhosts/);
  * strict file names, https upgrade, the ad wrapper (adfoc.us) is only ever read as text, never contacted;
  * build choice: recommended by default, `version` forms, previews only when asked, never a guess (ambiguous);
  * the chain is followed by hand (`max_redirects=0`), refuses a redirect or link off optifine.net, and every failure
    shape is `vendor_changed` with the official page link;
  * politeness: at least a second between two requests of one call (fake clock);
  * the shape check, the safe write (`.part`, atomic rename, nothing left behind), the destination guards, `exists`;
  * `dry_run` fetches only the version list and writes nothing; a guest is refused; nothing is ever "installed".
"""
import io
import itertools
import json
import os
import sys
import tempfile
import types
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-optifine-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import checklist_schema, tool_safety, tool_settings, tools, ui_bridge  # noqa: E402
from jarvis.actions import minecraft_optifine as mo  # noqa: E402
from jarvis.modhosts import optifine as of  # noqa: E402
import _modhosts_fake_optifine as fake  # noqa: E402

DATA = ROOT / "tests" / "data" / "modhosts"
HAND = fake.PAGE_FILE.read_bytes()
I6 = "OptiFine_1.20.1_HD_U_I6.jar"
I5 = "OptiFine_1.20.1_HD_U_I5.jar"
ASKED = []
ANSWER = [True]
_n = itertools.count(1)


class Skip(Exception):
    pass


def _confirm(question, body="", **kw):
    ASKED.append((question, body))
    return ANSWER[0]


ui_bridge.confirm = _confirm            # nothing in a test ever opens a real question


def fresh():
    """A clean fake site, a fake clock plugged into the pacer, no cache, no settings, the owner asking."""
    fake.reset()
    clock = fake.Clock()
    fake.STATE.clock = clock
    of._clock, of._sleep = clock.time, clock.sleep
    mo.FETCH, mo.CACHE = fake.fetch, False
    del ASKED[:]
    ANSWER[0] = True
    os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    try:
        tool_settings.settings_path().unlink()
    except OSError:
        pass
    return clock


def newdir():
    return str(Path(HOME) / ("dl%d" % next(_n)))


def set_setting(key, value):
    path = tool_settings.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.setdefault("minecraft_lookup", {})[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")


def rows_of(page=HAND):
    r = of.parse_downloads(page)
    assert r["ok"], r
    return r["rows"]


def assert_vendor_changed(r):
    assert r["ok"] is False and r["reason"] == "vendor_changed", r
    assert of.DOWNLOADS_URL in r["error"], r["error"]
    assert "github" not in r["error"].lower() and "gitlab" not in r["error"].lower(), r["error"]
    assert r["error"].count("http") == 1, "exactly one link is offered: the official page, never another site"


HAND_FILES = [
    "preview_OptiFine_26.2_HD_U_K2_pre5.jar", "preview_OptiFine_26.2_HD_U_K2_pre4.jar",
    "preview_OptiFine_26.1.2_HD_U_K1_pre2.jar", "preview_OptiFine_26.1.2_HD_U_K1_pre1.jar",
    "OptiFine_1.21.11_HD_U_J9.jar", "OptiFine_1.21.11_HD_U_J8.jar",
    "OptiFine_1.20.4_HD_U_I7.jar",
    "OptiFine_1.20.1_HD_U_I6.jar", "OptiFine_1.20.1_HD_U_I5.jar",
    "preview_OptiFine_1.20_HD_U_I5_pre5.jar", "preview_OptiFine_1.20_HD_U_I5_pre4.jar",
    "OptiFine_1.19.4_HD_U_I4.jar",
    "OptiFine_1.19.1_HD_U_H7.jar", "OptiFine_1.19.1_HD_U_H6.jar",
    "OptiFine_1.7.10_HD_U_D3.jar", "OptiFine_1.7.10_HD_G_D3.jar",
]


# ---------------------------------------------------------------- small pure helpers

def test_normalise_mc_version():
    good = {"1.20.1": "1.20.1", "26.3": "26.3", "26.1.2": "26.1.2", "1.7.2": "1.7.2", "1.21": "1.21",
            " 1.20.1 ": "1.20.1", "Minecraft 1.20.1": "1.20.1", "mc 26.3": "26.3", "v1.7.10": "1.7.10"}
    for raw, want in good.items():
        assert of.normalise_mc_version(raw) == want, (raw, of.normalise_mc_version(raw))
    for bad in ("", None, "1", "abc", "1.20.1.2", "../1.20", "1.20/1", "1.x", "1..2", "9999.1", "1.20.1; rm"):
        assert of.normalise_mc_version(bad) is None, bad


def test_valid_file_name():
    for ok in ("OptiFine_1.20.1_HD_U_I6.jar", "preview_OptiFine_26.2_HD_U_K2_pre5.jar", "OptiFine_1.7.10_HD_U_D3.jar"):
        assert of.valid_file_name(ok), ok
    for bad in ("", None, "..", "../OptiFine_1.20.1_HD_U_I6.jar", "OptiFine_1.20.1_HD_U_I6.exe", "OptiFine_1.20.1_HD_U_I6.jar?x=1",
                "OptiFine_1.20.1_HD_U_I6.jar/..", "optifine_1.20.1_HD_U_I6.jar", "OptiFine__HD.jar", "Opti Fine_1.20.1_HD.jar",
                "OptiFine_1.20.1_HD_U_I6.jar\n", "x" * 100 + ".jar"):
        assert not of.valid_file_name(bad), bad


def test_clean_text():
    out = of.clean_text("<b>Hi</b>\x07 there\x00\n\tfriend " + "x" * 200, 20)
    assert "<" not in out and "\x07" not in out and "\x00" not in out and "\n" not in out, repr(out)
    assert out.startswith("Hi there friend") and len(out) <= 21, repr(out)
    assert of.clean_text(None) == "" and of.clean_text("&lt;b&gt;x") == "x"


def test_pacer_waits_a_second_but_not_first():
    clock = fresh()
    p = of.Pacer()
    p.wait()
    assert clock.slept == [], clock.slept
    p.mark()
    p.wait()
    assert clock.slept == [of.MIN_GAP_SECONDS] and of.MIN_GAP_SECONDS >= 1.0, clock.slept
    clock.now += 5
    p.mark()
    clock.now += 0.4
    p.wait()
    assert abs(clock.slept[-1] - (of.MIN_GAP_SECONDS - 0.4)) < 1e-6, clock.slept


# ---------------------------------------------------------------- the parser

def test_parse_hand_page():
    r = of.parse_downloads(HAND)
    assert r["ok"] and r["skipped"] == 2, r.get("skipped")
    rows = r["rows"]
    assert [x["file_name"] for x in rows] == HAND_FILES, [x["file_name"] for x in rows]
    assert [x["file_name"] for x in rows if x["recommended"]] == [
        "OptiFine_1.21.11_HD_U_J9.jar", "OptiFine_1.20.4_HD_U_I7.jar", "OptiFine_1.20.1_HD_U_I6.jar",
        "OptiFine_1.19.4_HD_U_I4.jar", "OptiFine_1.19.1_HD_U_H7.jar", "OptiFine_1.7.10_HD_U_D3.jar"]
    assert sum(1 for x in rows if x["preview"]) == 6
    by = {x["file_name"]: x for x in rows}
    i6 = by[I6]
    assert (i6["mc_version"], i6["version_name"], i6["kind"], i6["date"], i6["forge"]) == \
        ("1.20.1", "HD U I6", "main", "2023-12-21", "Forge 47.2.18"), i6
    assert by[I5]["kind"] == "more" and by[I5]["recommended"] is False and by[I5]["preview"] is False
    p = by["preview_OptiFine_26.2_HD_U_K2_pre5.jar"]
    assert p["version_name"] == "HD U K2 pre5" and p["kind"] == "preview" and p["preview"] and not p["recommended"], p
    assert "0.0" not in {x["mc_version"] for x in rows}, "a <h2> inside a <script> string must not count"
    for x in rows:
        assert x["mirror"] == "https://optifine.net/adloadx?f=" + x["file_name"], x
    dump = json.dumps(rows)
    assert "adfoc" not in dump and "evil.jar" not in dump and "http://" not in dump, "no wrapper, no path trick, no plain http"


def test_parse_hand_page_untrusted_text_and_odd_rows():
    by = {x["file_name"]: x for x in rows_of()}
    h7 = by["OptiFine_1.19.1_HD_U_H7.jar"]
    assert "<" not in h7["forge"] and "\x07" not in h7["forge"] and len(h7["forge"]) <= 41, repr(h7["forge"])
    assert h7["date"] is None, "an impossible date is None, not a guess"
    h6 = by["OptiFine_1.19.1_HD_U_H6.jar"]            # only the ad-wrapper link: the name is read as text
    assert h6["mirror"] == "https://optifine.net/adloadx?f=OptiFine_1.19.1_HD_U_H6.jar" and h6["kind"] == "more", h6
    assert "OptiFine_1.18.2_HD_U_H4.jar" not in by, "a row whose Minecraft version disagrees with its file is skipped"
    assert "OptiFine_1.19.1_HD_U_H9.jar" not in by, "a path-trick link is skipped"


def test_parse_accepts_text_and_bytes_and_latin1():
    a = of.parse_downloads(HAND)
    b = of.parse_downloads(HAND.decode("latin-1"))
    assert a["ok"] and b["ok"] and a["rows"] == b["rows"]
    latin = HAND.replace(b"cafe", "caf\u00e9".encode("latin-1"))      # the real page is Latin-1: a byte that is not UTF-8
    assert b"caf\xe9" in latin and latin != HAND
    c = of.parse_downloads(latin)
    assert c["ok"] and c["rows"] == a["rows"], "a Latin-1 byte never trips the parser"
    crlf = of.parse_downloads(HAND.replace(b"\n", b"\r\n"))          # the real page has CRLF line ends
    assert crlf["ok"] and crlf["rows"] == a["rows"]


def test_parse_junk_is_vendor_changed():
    for junk in (b"", b"<html>nothing here</html>", b"mentions downloadLine but has no rows", None, 5,
                 b"<tr class='downloadLine downloadLineMain'><td class='colFile'>x</td></tr>"):
        assert_vendor_changed(of.parse_downloads(junk))


def test_parse_real_page():
    page, expected = DATA / "optifine_downloads.html", DATA / "optifine_rows.json"
    if not (page.exists() and expected.exists()):
        raise Skip("the probe's optifine_downloads.html / optifine_rows.json are not in tests/data/modhosts/")
    r = of.parse_downloads(page.read_bytes())
    assert r["ok"] and r["skipped"] == 0, r.get("skipped")
    exp = json.loads(expected.read_text(encoding="utf-8"))["rows"]
    got = r["rows"]
    assert [x["file_name"] for x in got] == [e["file"] for e in exp], "same rows, same order as the probe's parse"
    for g, e in zip(got, exp):
        assert g["kind"] == e["kind"] and g["mc_version"] == e["mc"] and g["preview"] == e["preview"], (g, e)
        assert "OptiFine " + g["version_name"] == e["label"], (g, e)
        assert g["date"] == of._iso_date(e["date"]) and g["date"], (g, e)
        assert g["forge"] == e["forge"], (g, e)
        assert g["mirror"] == "https://" + e["mirror"][len("http://"):], (g, e)
    assert sum(g["recommended"] for g in got) == 42 and sum(g["preview"] for g in got) == 40
    assert sum(g["kind"] == "more" for g in got) == 143 and len(got) == 225
    assert "adfoc" not in json.dumps(got)


def test_real_hops_fixture_matches_the_link_parser():
    hops = DATA / "optifine_hops.json"
    if not hops.exists():
        raise Skip("the probe's optifine_hops.json is not in tests/data/modhosts/")
    data = json.loads(hops.read_text(encoding="utf-8"))
    page = [h for h in data["results"]["chain"] if h.get("page")][0]
    file_name = data["chosen_row"]["file"]
    body = page["body"]
    assert "downloadx?f=%s&x=" % file_name in body, "the saved page holds the anchor"
    link, why = of.find_download_link(body, page["url"], file_name)
    assert link == page["page"]["next_url"] and why is None, (link, why)
    assert of._final_link_ok(data["results"]["chain_info"]["final_url"], file_name) == link


# ---------------------------------------------------------------- listing

def test_list_overview():
    fresh()
    r = of.list_versions(fetch=fake.fetch, cache=False)
    assert r["ok"], r
    assert [v["mc_version"] for v in r["versions"]] == ["1.21.11", "1.20.4", "1.20.1", "1.19.4", "1.19.1", "1.7.10"], r["versions"]
    assert all(v["recommended"] and not v["preview"] for v in r["versions"])
    assert r["only_previews"] == ["26.2", "26.1.2", "1.20"], r.get("only_previews")
    assert "26.2" in r["previews_note"] and "include_preview" in r["previews_note"]
    assert r["total"] == 6 and r["page"] == of.DOWNLOADS_URL and "never as instructions" in r["note"]
    assert fake.STATE.paths() == ["/downloads"], "one request lists everything"


def test_list_overview_with_previews():
    fresh()
    r = of.list_versions(include_preview=True, fetch=fake.fetch, cache=False)
    assert [v["file_name"] for v in r["versions"]] == [
        "preview_OptiFine_26.2_HD_U_K2_pre5.jar", "preview_OptiFine_26.2_HD_U_K2_pre4.jar",
        "preview_OptiFine_26.1.2_HD_U_K1_pre2.jar", "preview_OptiFine_26.1.2_HD_U_K1_pre1.jar",
        "OptiFine_1.21.11_HD_U_J9.jar", "OptiFine_1.20.4_HD_U_I7.jar", "OptiFine_1.20.1_HD_U_I6.jar",
        "preview_OptiFine_1.20_HD_U_I5_pre5.jar", "preview_OptiFine_1.20_HD_U_I5_pre4.jar",
        "OptiFine_1.19.4_HD_U_I4.jar", "OptiFine_1.19.1_HD_U_H7.jar", "OptiFine_1.7.10_HD_U_D3.jar"], r["versions"]
    assert "only_previews" not in r


def test_list_one_minecraft_version():
    fresh()
    r = of.list_versions("1.20.1", fetch=fake.fetch, cache=False)
    assert [v["file_name"] for v in r["versions"]] == [I6, I5]
    assert r["versions"][0]["recommended"] is True and r["versions"][1]["recommended"] is False
    assert r["versions"][0]["forge"] == "Forge 47.2.18" and r["versions"][0]["date"] == "2023-12-21"
    assert "only_previews" not in r
    assert of.list_versions("Minecraft 1.20.1", fetch=fake.fetch, cache=False)["total"] == 2


def test_list_previews_only_versions():
    fresh()
    r = of.list_versions("1.20", fetch=fake.fetch, cache=False)
    assert r["ok"] and r["versions"] == [] and r["only_previews"] == ["1.20"], r
    r = of.list_versions("1.20", include_preview=True, fetch=fake.fetch, cache=False)
    assert [v["version_name"] for v in r["versions"]] == ["HD U I5 pre5", "HD U I5 pre4"]
    assert all(v["preview"] and not v["recommended"] for v in r["versions"])
    r = of.list_versions("26.2", fetch=fake.fetch, cache=False)
    assert r["versions"] == [] and r["only_previews"] == ["26.2"]


def test_list_unknown_and_bad_versions():
    fresh()
    r = of.list_versions("99.9", fetch=fake.fetch, cache=False)
    assert r["ok"] is False and r["reason"] == "not_found" and "1.20.1" in r["error"] and "1.20.1" in r["mc_versions"], r
    r = of.list_versions("banana", fetch=fake.fetch, cache=False)
    assert r["ok"] is False and r["reason"] == "not_found"
    assert fake.STATE.paths() == ["/downloads"], "a malformed version never reaches the network"


def test_list_limit():
    fresh()
    r = of.list_versions("1.20.1", limit=1, fetch=fake.fetch, cache=False)
    assert len(r["versions"]) == 1 and r["truncated"] == 1 and r["total"] == 2
    assert len(of.list_versions("1.20.1", limit=0, fetch=fake.fetch, cache=False)["versions"]) == 2
    assert len(of.list_versions("1.20.1", limit="x", fetch=fake.fetch, cache=False)["versions"]) == 2
    assert len(of.list_versions("1.20.1", limit=None, fetch=fake.fetch, cache=False)["versions"]) == 2
    assert len(of.list_versions(limit=2, fetch=fake.fetch, cache=False)["versions"]) == 2


# ---------------------------------------------------------------- choosing a build

def test_choose_default_is_recommended():
    rows = rows_of()
    for mc, want in (("1.20.1", I6), ("1.21.11", "OptiFine_1.21.11_HD_U_J9.jar"), ("1.20.4", "OptiFine_1.20.4_HD_U_I7.jar"),
                     ("Minecraft 1.20.1", I6)):
        r = of.choose_build(rows, mc)
        assert r["ok"] and r["row"]["file_name"] == want, (mc, r)
    assert of.choose_build(rows, "1.20.1", "")["row"]["file_name"] == I6


def test_choose_version_forms():
    rows = rows_of()
    for form in ("HD_U_I6", "HD U I6", "i6", "hd-u-i6", "OptiFine_1.20.1_HD_U_I6.jar", "OptiFine HD U I6", "U_I6",
                 "1.20.1_HD_U_I6", " I6 "):
        r = of.choose_build(rows, "1.20.1", form)
        assert r["ok"] and r["row"]["file_name"] == I6, (form, r)
    assert of.choose_build(rows, "1.20.1", "I5")["row"]["file_name"] == I5


def test_choose_never_guesses():
    rows = rows_of()
    r = of.choose_build(rows, "1.20.4", "I6")
    assert r["ok"] is False and r["reason"] == "not_found" and "HD U I7" in r["error"], r
    r = of.choose_build(rows, "1.7.10", "D3")
    assert r["ok"] is False and r["reason"] == "ambiguous" and len(r["candidates"]) == 2, r
    assert {c["version_name"] for c in r["candidates"]} == {"HD U D3", "HD G D3"}
    assert of.choose_build(rows, "1.7.10", "HD_G_D3")["row"]["file_name"] == "OptiFine_1.7.10_HD_G_D3.jar"
    assert of.choose_build(rows, "1.7.10", "HD_U_D3")["row"]["file_name"] == "OptiFine_1.7.10_HD_U_D3.jar"
    for bad in ("Z9", "!!!", "I66"):
        r = of.choose_build(rows, "1.20.1", bad)
        assert r["ok"] is False and r["reason"] == "not_found", (bad, r)
    for mc in ("9.9", "abc", "", None):
        r = of.choose_build(rows, mc)
        assert r["ok"] is False and r["reason"] == "not_found", (mc, r)
    assert "1.20.1" in of.choose_build(rows, "9.9")["mc_versions"]


def test_choose_previews_only_when_asked():
    rows = rows_of()
    r = of.choose_build(rows, "1.20", preview=True)
    assert r["ok"] and r["row"]["file_name"] == "preview_OptiFine_1.20_HD_U_I5_pre5.jar", r
    r = of.choose_build(rows, "1.20")
    assert r["ok"] is False and r["reason"] == "no_compatible_file" and "preview" in r["error"], r
    r = of.choose_build(rows, "1.20.1", preview=True)
    assert r["ok"] is False and r["reason"] == "no_compatible_file", r
    for form in ("I5_pre4", "pre4", "preview_OptiFine_1.20_HD_U_I5_pre4.jar"):
        r = of.choose_build(rows, "1.20", form, preview=True)
        assert r["ok"] and r["row"]["file_name"] == "preview_OptiFine_1.20_HD_U_I5_pre4.jar", (form, r)
    r = of.choose_build(rows, "26.2", "HD_U_K2_pre5")      # a preview asked for as if it were a regular build
    assert r["ok"] is False and r["reason"] == "not_found" and "preview=true" in r["error"], r
    r = of.choose_build(rows, "1.20.1", "I6", preview=True)  # a regular build asked for as a preview
    assert r["ok"] is False and r["reason"] == "not_found" and "without preview" in r["error"], r


# ---------------------------------------------------------------- the cache seam

class FakeCache:
    def __init__(self, fail_get=False, fail_put=False):
        self.store, self.puts, self.fail_get, self.fail_put = {}, [], fail_get, fail_put

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("cache is broken")
        return self.store.get(key)

    def put(self, key, value, ttl_seconds=600):
        if self.fail_put:
            raise RuntimeError("cache is full")
        self.puts.append((key, ttl_seconds))
        self.store[key] = json.loads(json.dumps(value))


def test_cache_used_and_ttl():
    fresh()
    c = FakeCache()
    r1 = of.load_rows(fake.fetch, c)
    assert r1["ok"] and r1["cached"] is False and c.puts == [(of.CACHE_KEY, 600)], (r1, c.puts)
    r2 = of.load_rows(fake.fetch, c)
    assert r2["ok"] and r2["cached"] is True and r2["rows"] == r1["rows"]
    assert fake.STATE.count("/downloads") == 1, "the second look never touched the site"


def test_cache_garbage_and_failures_are_ignored():
    fresh()
    c = FakeCache()
    c.store[of.CACHE_KEY] = "junk"
    assert of.load_rows(fake.fetch, c)["cached"] is False
    c.store[of.CACHE_KEY] = [{"file_name": "../x.jar"}]
    assert of.load_rows(fake.fetch, c)["cached"] is False
    assert of.load_rows(fake.fetch, FakeCache(fail_get=True))["ok"]
    assert of.load_rows(fake.fetch, FakeCache(fail_put=True))["ok"]


def test_failed_parse_is_not_cached():
    fresh()
    fake.STATE.page = b"<html>redesigned</html>"
    c = FakeCache()
    assert_vendor_changed(of.load_rows(fake.fetch, c))
    assert c.puts == []


# ---------------------------------------------------------------- failures of the net seam

def test_net_failure_shapes():
    fresh()
    fake.STATE.fail["/downloads"] = {"ok": False, "error": "no route to host", "reason": "offline"}
    r = of.load_rows(fake.fetch, False)
    assert r["ok"] is False and r["reason"] == "offline", r
    fake.STATE.fail["/downloads"] = {"ok": False, "error": "slow down", "reason": "rate_limited"}
    assert of.load_rows(fake.fetch, False)["reason"] == "rate_limited"
    fake.STATE.fail["/downloads"] = {"ok": False, "error": "gone", "reason": "not_found"}
    assert_vendor_changed(of.load_rows(fake.fetch, False))
    fake.STATE.fail["/downloads"] = {"ok": False, "error": "strange", "reason": "something_new"}
    assert of.load_rows(fake.fetch, False)["reason"] == "offline"
    fake.STATE.fail.clear()
    fake.STATE.raise_on.add("/downloads")
    r = of.load_rows(fake.fetch, False)
    assert r["ok"] is False and r["reason"] == "offline" and "reach optifine.net" in r["error"], r
    assert_vendor_changed(of.load_rows(lambda *a, **k: None, False))


def test_http_error_statuses_either_way_net_reports_them():
    for as_ok in (False, True):
        fresh()
        fake.STATE.errors_as_ok = as_ok
        for status, reason in ((429, "rate_limited"), (503, "offline"), (500, "offline")):
            fake.STATE.status["/downloads"] = status
            r = of.load_rows(fake.fetch, False)
            assert r["ok"] is False and r["reason"] == reason, (as_ok, status, r)
        fake.STATE.status["/downloads"] = 403
        r = of.load_rows(fake.fetch, False)
        assert r["ok"] is False and r["reason"] in ("vendor_changed", "offline"), (as_ok, r)
        if as_ok:
            assert_vendor_changed(r)
        fake.STATE.status["/downloads"] = 404
        assert_vendor_changed(of.load_rows(fake.fetch, False))


def test_oversized_page_is_vendor_changed():
    fresh()
    fake.STATE.page = b"x" * of.PAGE_CAP
    assert_vendor_changed(of.load_rows(fake.fetch, False))


def test_net_module_is_imported_lazily_at_call_time():
    fresh()
    calls = []
    stub = types.ModuleType("jarvis.modhosts.net")
    stub.fetch = lambda *a, **k: (calls.append(a[1]), fake.fetch(*a, **k))[1]
    missing = object()
    saved = sys.modules.get("jarvis.modhosts.net", missing)
    try:
        sys.modules["jarvis.modhosts.net"] = stub
        r = of.list_versions("1.20.1", cache=False)           # fetch=None: the contract's net.fetch, found NOW
        assert r["ok"] and calls == [of.DOWNLOADS_URL], (r, calls)
        sys.modules["jarvis.modhosts.net"] = None             # "not installed"
        r = of.list_versions("1.20.1", cache=False)
        assert r["ok"] is False and r["reason"] == "unsupported" and of.DOWNLOADS_URL in r["error"], r
        assert of.resolve_final_link(I6)["reason"] == "unsupported"
        assert of.fetch_jar("https://optifine.net/downloadx?f=%s&x=%s" % (I6, fake.TOKEN), I6)["reason"] == "unsupported"
    finally:
        if saved is missing:
            sys.modules.pop("jarvis.modhosts.net", None)
        else:
            sys.modules["jarvis.modhosts.net"] = saved


# ---------------------------------------------------------------- the hops

def test_link_resolution_plain():
    fresh()
    r = of.resolve_final_link(I6, fetch=fake.fetch, pacer=of.Pacer())
    assert r["ok"] and r["url"] == "https://optifine.net/downloadx?f=%s&x=%s" % (I6, fake.TOKEN), r
    assert len(fake.STATE.log) == 1
    e = fake.STATE.log[0]
    assert e["url"] == "https://optifine.net/adloadx?f=" + I6 and e["max_redirects"] == 0 and e["want"] == of.WANT_PREFIX, e
    assert fake.STATE.bad_hosts == [] and fake.STATE.insecure == []


def test_link_resolution_follows_redirects_by_hand_and_upgrades_http():
    fresh()
    fake.STATE.modes = {"hop302"}
    r = of.resolve_final_link(I6, fetch=fake.fetch, pacer=of.Pacer())
    assert r["ok"] and r["url"].endswith("&x=" + fake.TOKEN), r
    assert [e["url"] for e in fake.STATE.log] == ["https://optifine.net/adloadx?f=" + I6,
                                                   "https://optifine.net/adloadx?f=%s&hop=1" % I6]
    assert all(e["max_redirects"] == 0 for e in fake.STATE.log), "every hop is seen, none followed by the client"
    fresh()
    fake.STATE.modes = {"hop302_http"}
    r = of.resolve_final_link(I6, fetch=fake.fetch, pacer=of.Pacer())
    assert r["ok"], r
    assert fake.STATE.insecure == [], "an http Location is upgraded before it is requested"
    assert fake.STATE.log[1]["url"].startswith("https://optifine.net/adloadx?"), fake.STATE.log[1]


def test_link_resolution_refuses_off_site_and_odd_pages():
    for mode in ("offsite_redirect", "loop", "no_link", "two_links", "bad_token", "other_file_link", "offsite_link"):
        fresh()
        fake.STATE.modes = {mode}
        r = of.resolve_final_link(I6, fetch=fake.fetch, pacer=of.Pacer())
        assert_vendor_changed(r)
        assert fake.STATE.bad_hosts == [], (mode, "the other host was contacted: %r" % fake.STATE.bad_hosts)
        if mode == "offsite_redirect":
            assert len(fake.STATE.log) == 1, "the redirect was seen and not followed"
        if mode == "loop":
            assert len(fake.STATE.log) == of.MAX_HOPS, len(fake.STATE.log)


def test_link_resolution_odd_inputs():
    fresh()
    r = of.resolve_final_link("../x.jar", fetch=fake.fetch)
    assert r["ok"] is False and r["reason"] == "not_found" and fake.STATE.log == []
    r = of.resolve_final_link("OptiFine_1.20.1_HD_U_Z9.jar", fetch=fake.fetch, pacer=of.Pacer())   # the site has no such page
    assert_vendor_changed(r)
    fresh()
    fake.STATE.modes = {"adloadx_is_jar"}
    r = of.resolve_final_link(I6, fetch=fake.fetch, pacer=of.Pacer())
    assert r["ok"] and r.get("direct") is True and r["url"] == "https://optifine.net/adloadx?f=" + I6, r


def test_find_download_link_text_fallback_and_strictness():
    base = "https://optifine.net/adloadx?f=" + I6
    tok = fake.TOKEN
    link, why = of.find_download_link("<script>go('downloadx?f=%s&x=%s')</script>" % (I6, tok), base, I6)
    assert link == "https://optifine.net/downloadx?f=%s&x=%s" % (I6, tok), (link, why)
    link, why = of.find_download_link("<a href=\"downloadx?f=%s&amp;x=%s\">x</a>" % (I6, tok), base, I6)
    assert link and why is None, (link, why)
    for bad in ("<a href='downloadx?f=%s&x=%s&y=1'>x</a>" % (I6, tok),          # an extra parameter
                "<a href='downloadx?f=%s&x=%s'>x</a>" % (I6, tok[:-1]),         # a short token
                "<a href='downloadx?f=%s'>x</a>" % I6,                          # no token
                "<a href='http://adfoc.us/downloadx?f=%s&x=%s'>x</a>" % (I6, tok),
                "<a href='//adfoc.us/downloadx?f=%s&x=%s'>x</a>" % (I6, tok),
                "<a href='https://optifine.net.evil.example/downloadx?f=%s&x=%s'>x</a>" % (I6, tok),
                "<a href='https://user@optifine.net/downloadx?f=%s&x=%s'>x</a>" % (I6, tok),
                "<a href='https://optifine.net/other?f=%s&x=%s'>x</a>" % (I6, tok),
                "", None):
        link, why = of.find_download_link(bad, base, I6)
        assert link is None and why, (bad, link)


# ---------------------------------------------------------------- the jar

def test_check_jar_shape():
    good = fake.make_jar()
    assert len(good) >= of.MIN_JAR_BYTES
    assert of.check_jar_shape(good, I6) == (True, "")
    assert of.check_jar_shape(good, I6, served_name=I6) == (True, "")
    ok, why = of.check_jar_shape(good, I6, served_name="OptiFine_9.9.9_HD_U_X1.jar")
    assert ok is False and "named" in why, why
    ok, why = of.check_jar_shape(b"PK\x03\x04" + b"0" * 10)
    assert ok is False and "small" in why, why
    ok, why = of.check_jar_shape(b"<html>" * 20000)
    assert ok is False and "ZIP signature" in why, why
    ok, why = of.check_jar_shape(b"PK\x03\x04" + b"\x00" * 120000)
    assert ok is False and "unreadable" in why, why
    ok, why = of.check_jar_shape(good[:-100])
    assert ok is False and "unreadable" in why, why
    ok, why = of.check_jar_shape(good, max_bytes=1000)
    assert ok is False and "larger" in why, why
    ok, why = of.check_jar_shape(good, min_bytes=10 ** 9)
    assert ok is False and "small" in why, why
    assert of.check_jar_shape(bytearray(good))[0] is True


def test_check_jar_shape_on_disk():
    tmp = Path(tempfile.mkdtemp(prefix="shape-", dir=HOME))
    (tmp / "good.jar").write_bytes(fake.make_jar())
    (tmp / "page.jar").write_bytes(b"<html>" * 20000)
    assert of.check_jar_shape(tmp / "good.jar", I6) == (True, "")
    assert of.check_jar_shape(str(tmp / "good.jar"), I6) == (True, "")
    assert of.check_jar_shape(tmp / "page.jar")[0] is False
    ok, why = of.check_jar_shape(tmp / "missing.jar")
    assert ok is False and why, why


def test_disposition_name():
    f = of._disposition_name
    assert f({"content-disposition": 'attachment; filename="a/b/OptiFine_x.jar"'}) == "OptiFine_x.jar"
    assert f({"content-disposition": "attachment; filename*=UTF-8''X.jar"}) == "X.jar"
    assert f({"content-disposition": "attachment; filename=Y.jar"}) == "Y.jar"
    assert f({}) is None and f({"content-disposition": "inline"}) is None


def test_fetch_jar():
    fresh()
    url = "https://optifine.net/downloadx?f=%s&x=%s" % (I6, fake.TOKEN)
    r = of.fetch_jar(url, I6, fetch=fake.fetch, pacer=of.Pacer())
    assert r["ok"] and r["data"] == fake.STATE.jar and r["served_name"] == I6, r.get("error")
    e = fake.STATE.log[-1]
    assert e["want"] == of.WANT_FULL and e["max_redirects"] == 0 and e["max_bytes"] == of.MAX_JAR_BYTES, e
    r = of.fetch_jar(url, I6, fetch=fake.fetch, pacer=of.Pacer(), max_bytes=1000)
    assert r["ok"] is False and r["reason"] == "too_big", r
    assert fake.STATE.log[-1]["max_bytes"] == 1000


def test_fetch_jar_refusals():
    fresh()
    r = of.fetch_jar("https://adfoc.us/serve/download", I6, fetch=fake.fetch, pacer=of.Pacer())
    assert r["ok"] is False and r["reason"] == "vendor_changed" and fake.STATE.log == []
    url = "https://optifine.net/downloadx?f=%s&x=%s" % (I6, fake.TOKEN)
    for mode in ("html_jar", "jar_redirect_offsite", "text_jar"):
        fresh()
        fake.STATE.modes = {mode}
        r = of.fetch_jar(url, I6, fetch=fake.fetch, pacer=of.Pacer())
        assert_vendor_changed(r)
        assert fake.STATE.bad_hosts == [], mode


# ---------------------------------------------------------------- the tools

def test_guest_is_refused_before_anything_happens():
    for sender in (json.dumps({"platform": "discord", "user_id": "42", "is_owner": False}), "{", "[]", '"x"',
                   json.dumps({"platform": "discord", "user_id": "42"})):
        fresh()
        os.environ["JARVIS_CHANNEL_SENDER"] = sender
        dest = newdir()
        for fn, args in ((mo.tool_list_optifine_versions, {"mc_version": "1.20.1"}),
                         (mo.tool_download_optifine, {"mc_version": "1.20.1", "dest": dest})):
            r = fn(args)
            assert r["ok"] is False and "owner's" in r["error"], (sender, r)
        assert fake.STATE.log == [] and ASKED == [] and not Path(dest).exists(), sender
    fresh()
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})
    assert mo.tool_list_optifine_versions({"mc_version": "1.20.1"})["ok"] is True
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "", "user_id": ""})
    assert mo.tool_list_optifine_versions({"mc_version": "1.20.1"})["ok"] is True      # no chat context = the PC
    os.environ.pop("JARVIS_CHANNEL_SENDER")


def test_tool_list():
    fresh()
    r = mo.tool_list_optifine_versions({"mc_version": "1.20.1"})
    assert r["ok"] and [v["file_name"] for v in r["versions"]] == [I6, I5], r
    assert json.dumps(r)
    r = mo.tool_list_optifine_versions({})
    assert r["ok"] and r["versions"][0]["mc_version"] == "1.21.11"
    r = mo.tool_list_optifine_versions({"include_preview": "true", "limit": 3})
    assert r["ok"] and len(r["versions"]) == 3 and r["versions"][0]["preview"] is True, r
    r = mo.tool_list_optifine_versions({"include_preview": "false"})
    assert all(not v["preview"] for v in r["versions"])
    fresh()
    fake.STATE.page = b"<html>redesign</html>"
    assert_vendor_changed(mo.tool_list_optifine_versions({}))
    mo.FETCH = lambda *a, **k: (_ for _ in ()).throw(ValueError("odd"))
    r = mo.tool_list_optifine_versions(None)
    assert r["ok"] is False and "reason" in r, "an exception inside never escapes a tool"


def test_download_happy_path():
    clock = fresh()
    dest = newdir()
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest})
    assert r["ok"] is True and r["downloaded"] is True and r["installed"] is False, r
    target = Path(dest) / I6
    assert r["path"] == str(target) and target.read_bytes() == fake.STATE.jar
    assert os.listdir(dest) == [I6], os.listdir(dest)
    assert (r["mc_version"], r["version_name"], r["file_name"]) == ("1.20.1", "HD U I6", I6)
    assert r["recommended"] is True and r["preview"] is False and r["size"] == len(fake.STATE.jar)
    assert "Downloaded" in r["message"] and "nothing was installed" in r["message"] and "never runs the jar" in r["message"]
    assert fake.STATE.paths() == ["/downloads", "/adloadx", "/downloadx"], fake.STATE.paths()
    assert fake.STATE.bad_hosts == [] and fake.STATE.insecure == []
    assert [e["max_redirects"] for e in fake.STATE.log] == [3, 0, 0], "the list page uses the default; both hops and the jar are by hand"
    assert len(ASKED) == 1 and ASKED[0][0] == "Save outside the downloads library?" and dest in ASKED[0][1], ASKED
    ts = [e["t"] for e in fake.STATE.log]
    assert all(b - a >= of.MIN_GAP_SECONDS - 1e-6 for a, b in zip(ts, ts[1:])), ts
    assert len(clock.slept) == 2, clock.slept
    assert json.dumps(r)


def test_download_choices():
    fresh()
    d1 = newdir()
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "version": "I5", "dest": d1})
    assert r["ok"] and r["file_name"] == I5 and r["recommended"] is False and (Path(d1) / I5).exists(), r
    d2 = newdir()
    r = mo.tool_download_optifine({"mc_version": "1.20", "preview": True, "dest": d2})
    assert r["ok"] and r["file_name"] == "preview_OptiFine_1.20_HD_U_I5_pre5.jar" and r["preview"] is True, r
    assert "preview build" in r["message"]
    d3 = newdir()
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": d3, "filename": "my-optifine.jar"})
    assert r["ok"] and r["saved_as"] == "my-optifine.jar" and os.listdir(d3) == ["my-optifine.jar"], r
    d4 = newdir()
    fresh()
    r = mo.tool_download_optifine({"mc_version": "26.1.2", "dest": d4})
    assert r["ok"] is False and r["reason"] == "no_compatible_file", r
    assert fake.STATE.paths() == ["/downloads"] and not Path(d4).exists() and ASKED == []


def test_download_default_dest_and_setting():
    fresh()
    r = mo.tool_download_optifine({"mc_version": "1.20.1"})
    assert r["ok"] and r["path"] == str(Path(HOME) / "Downloads" / I6), r
    assert (Path(HOME) / "Downloads" / I6).exists() and "Downloads" in ASKED[0][1]
    fresh()
    custom = newdir()
    set_setting("default_dest", custom)
    r = mo.tool_download_optifine({"mc_version": "1.20.4", "dest": ""})
    assert r["ok"] and r["path"] == str(Path(custom) / "OptiFine_1.20.4_HD_U_I7.jar"), r


def test_dry_run_fetches_only_the_list_and_writes_nothing():
    fresh()
    dest = newdir()
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest, "dry_run": True})
    assert r["ok"] and r["dry_run"] is True and r["would_download"]["file_name"] == I6, r
    assert r["path"] == str(Path(dest) / I6) and "nothing was written" in r["message"]
    assert fake.STATE.paths() == ["/downloads"], fake.STATE.paths()
    assert ASKED == [] and not Path(dest).exists()
    assert "downloaded" not in r, "a dry run never claims a download"
    os.makedirs(dest)
    (Path(dest) / I6).write_bytes(b"mine")
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest, "dry_run": "true"})
    assert r["ok"] is False and r["reason"] == "exists", "a dry run reports what the real run would refuse"


def test_exists_is_never_overwritten():
    fresh()
    dest = newdir()
    assert mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest})["ok"]
    before = (Path(dest) / I6).read_bytes()
    n_asked = len(ASKED)
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest})
    assert r["ok"] is False and r["reason"] == "exists" and r["path"] == str(Path(dest) / I6), r
    assert fake.STATE.paths() == ["/downloads", "/adloadx", "/downloadx", "/downloads"], fake.STATE.paths()
    assert len(ASKED) == n_asked and (Path(dest) / I6).read_bytes() == before
    dest2 = newdir()
    os.makedirs(dest2)
    (Path(dest2) / (I6 + ".part")).write_bytes(b"someone else's partial")
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest2})
    assert r["ok"] is False and r["reason"] == "exists", r
    assert (Path(dest2) / (I6 + ".part")).read_bytes() == b"someone else's partial" and os.listdir(dest2) == [I6 + ".part"]


def test_bad_destinations_cost_nothing():
    cases = ["relative/folder", "folder", str(Path(HOME) / ".jarvis"), str(Path(HOME) / ".jarvis" / "sub"), "/", HOME,
             HOME + "/a/../b", str(Path(HOME) / "x.jar"), "bad\x00name"]
    afile = Path(HOME) / "afile.txt"
    afile.write_text("x")
    cases.append(str(afile))
    if sys.platform != "win32":
        cases.append("/etc/optifine-test")
        real = Path(HOME) / "realdir"
        real.mkdir(exist_ok=True)
        link = Path(HOME) / "linkdir"
        try:
            if not os.path.lexists(link):
                os.symlink(real, link)
            cases.append(str(link))
            jarvis_link = Path(HOME) / "to-jarvis"
            (Path(HOME) / ".jarvis").mkdir(exist_ok=True)
            if not os.path.lexists(jarvis_link):
                os.symlink(Path(HOME) / ".jarvis", jarvis_link)
            cases.append(str(jarvis_link))
        except (OSError, NotImplementedError):
            pass
    for dest in cases:
        fresh()
        r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest})
        assert r["ok"] is False and r["reason"] == "bad_dest", (dest, r)
        assert fake.STATE.log == [] and ASKED == [], (dest, "a bad folder must not reach the network or ask anything")
    assert not (Path(HOME) / ".jarvis" / "sub").exists()


def test_bad_filenames_are_refused_not_fixed():
    for name in ("../x.jar", "x.exe", "a/b.jar", "a\\b.jar", "CON.jar", ".jar", "x.jar.", "x" * 200 + ".jar", "x.jar\n"):
        fresh()
        dest = newdir()
        r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest, "filename": name})
        assert r["ok"] is False and r["reason"] == "bad_dest", (name, r)
        assert fake.STATE.log == [] and not Path(dest).exists(), name


def test_declined_confirmation_downloads_nothing():
    fresh()
    ANSWER[0] = False
    dest = newdir()
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest})
    assert r["ok"] is False and r.get("cancelled") is True and r["reason"] == "bad_dest", r
    assert fake.STATE.paths() == ["/downloads"] and not Path(dest).exists() and len(ASKED) == 1


def test_bad_jars_leave_nothing_behind():
    for mode, reason in (("bad_zip", "verify_failed"), ("tiny_jar", "verify_failed"), ("truncated", "verify_failed"),
                         ("wrong_name", "verify_failed"), ("html_jar", "vendor_changed"), ("text_jar", "vendor_changed"),
                         ("jar_redirect_offsite", "vendor_changed")):
        fresh()
        fake.STATE.modes = {mode}
        dest = newdir()
        r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest})
        assert r["ok"] is False and r["reason"] == reason, (mode, r)
        assert not Path(dest).exists(), (mode, "nothing was created: not the folder, not a .part")
        assert fake.STATE.bad_hosts == [], mode
        if reason == "vendor_changed":
            assert of.DOWNLOADS_URL in r["error"], r


def test_site_failures_are_plain_answers():
    fresh()
    fake.STATE.page = b"<html>new design</html>"
    dest = newdir()
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest})
    assert_vendor_changed(r)
    assert fake.STATE.paths() == ["/downloads"] and not Path(dest).exists() and ASKED == []
    fresh()
    fake.STATE.raise_on.add("/downloads")
    assert mo.tool_download_optifine({"mc_version": "1.20.1", "dest": newdir()})["reason"] == "offline"
    fresh()
    fake.STATE.fail["/adloadx"] = {"ok": False, "error": "timeout", "reason": "offline"}
    dest = newdir()
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest})
    assert r["reason"] == "offline" and not Path(dest).exists()
    fresh()
    fake.STATE.modes = {"offsite_redirect"}
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": newdir()})
    assert_vendor_changed(r)
    assert fake.STATE.bad_hosts == []


def test_unknown_and_missing_versions():
    fresh()
    dest = newdir()
    r = mo.tool_download_optifine({"mc_version": "9.9", "dest": dest})
    assert r["ok"] is False and r["reason"] == "not_found" and not Path(dest).exists()
    r = mo.tool_download_optifine({"mc_version": "1.7.10", "version": "D3", "dest": dest})
    assert r["ok"] is False and r["reason"] == "ambiguous" and len(r["candidates"]) == 2, r
    fresh()
    for args in ({}, {"mc_version": ""}, {"mc_version": "  "}, {"dest": newdir()}):
        r = mo.tool_download_optifine(args)
        assert r["ok"] is True and r["needs_clarification"] is True, (args, r)
    r = mo.tool_download_optifine({"mc_version": "banana"})
    assert r["ok"] is False and r["reason"] == "not_found"
    assert fake.STATE.log == [] and ASKED == []
    assert mo.tool_download_optifine(None)["needs_clarification"] is True


def test_download_size_setting():
    fresh()
    set_setting("download_max_mb", 1)
    fake.STATE.jar = fake.make_jar(size=1_300_000)
    dest = newdir()
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest})
    assert r["ok"] is False and r["reason"] == "too_big" and not Path(dest).exists(), r
    assert fake.STATE.log[-1]["max_bytes"] == 1024 * 1024, fake.STATE.log[-1]
    fresh()
    fake.STATE.jar = fake.make_jar(size=1_300_000)
    dest = newdir()
    r = mo.tool_download_optifine({"mc_version": "1.20.1", "dest": dest})
    assert r["ok"] is True and fake.STATE.log[-1]["max_bytes"] == of.MAX_JAR_BYTES, "0 = no cap of the owner's"


def test_write_jar_is_atomic_and_clean():
    fresh()
    good = fake.make_jar()
    folder = Path(newdir())
    path, err = mo._write_jar(folder, "x.jar", good, "x.jar")
    assert err is None and path == folder / "x.jar" and path.read_bytes() == good and os.listdir(folder) == ["x.jar"]
    folder = Path(newdir())
    path, err = mo._write_jar(folder, "x.jar", b"not a zip " * 10000, "x.jar")
    assert path is None and err["reason"] == "verify_failed" and os.listdir(folder) == [], (err, os.listdir(folder))
    folder = Path(newdir())
    os.makedirs(folder)
    (folder / "x.jar").write_bytes(b"mine")
    path, err = mo._write_jar(folder, "x.jar", good, "x.jar")
    assert path is None and err["reason"] == "exists" and (folder / "x.jar").read_bytes() == b"mine"
    assert os.listdir(folder) == ["x.jar"], "the .part was removed"
    folder = Path(newdir())
    os.makedirs(folder)
    (folder / "y.jar.part").write_bytes(b"theirs")
    path, err = mo._write_jar(folder, "y.jar", good, "y.jar")
    assert path is None and err["reason"] == "exists" and (folder / "y.jar.part").read_bytes() == b"theirs"


# ---------------------------------------------------------------- registration and guards

def test_registration_shape():
    assert set(mo.TOOLS) == {"list_optifine_versions", "download_optifine"}
    assert [s["name"] for s in mo.TOOL_SCHEMAS] == ["list_optifine_versions", "download_optifine"]
    for s in mo.TOOL_SCHEMAS:
        assert s["parameters"]["type"] == "object" and s["description"]
    assert mo.TOOL_SCHEMAS[1]["parameters"]["required"] == ["mc_version"]
    assert mo.TOOL_GROUP == "minecraft"
    assert mo.TOOL_CONFIRM_REQUIRED == {"download_optifine"}, "only the download is confirm-gated; the list is read-only"
    assert not hasattr(mo, "TOOL_PACK_INSTRUCTION"), "only minecraft_lookup.py declares the group's pack instruction"
    assert set(mo.TOOL_KEYWORDS) == set(mo.TOOLS)
    for tool, words in mo.TOOL_KEYWORDS.items():
        assert words and all(isinstance(w, int) and w >= 5 for w in words.values()), tool
    assert not (set(mo.TOOL_KEYWORDS["list_optifine_versions"]) & set(mo.TOOL_KEYWORDS["download_optifine"]))
    assert set(mo.TOOL_RESULT_SPECS) <= set(mo.TOOLS)
    for name in mo.TOOLS:
        low = name.lower()
        assert "install" not in low and "write" not in low, "nothing is installed: no tool name may say so"


def test_checklist_is_valid_and_has_its_own_section():
    entries, meta, problems = checklist_schema.extract_supplied(
        mo.TEST_CHECKLIST, mo.TEST_CHECKLIST_GROUP, set(mo.TOOLS), mo.TOOL_GROUP)
    assert problems == [], problems
    assert set(entries) == set(mo.TOOLS)
    assert all(e["group"] == "minecraft_optifine" for e in entries.values())
    assert meta["minecraft_optifine"]["label"] == "Minecraft: OptiFine"


def test_real_loader_registers_both_tools():
    assert "list_optifine_versions" in tools.TOOLS and "download_optifine" in tools.TOOLS, "the real loader found the file"
    assert "download_optifine" in tool_safety.DEFAULT_CONFIRM_REQUIRED
    assert "list_optifine_versions" not in tool_safety.DEFAULT_CONFIRM_REQUIRED
    assert tools.AUTO_TEST_CHECKLIST["download_optifine"]["group"] == "minecraft_optifine"
    assert set(mo.TOOL_KEYWORDS["download_optifine"]) <= set(tools.AUTO_TOOL_KEYWORDS.get("download_optifine", {}))
    from jarvis import tool_result_shaping
    assert "download_optifine" in tool_result_shaping.TOOL_RESULT_SPECS


def test_owner_only_registration_by_the_core_patch():
    names = {"list_optifine_versions", "download_optifine"}
    if not names <= set(tools.OWNER_ONLY_TOOLS):
        raise Skip("tools.OWNER_ONLY_TOOLS has no OptiFine names yet (the core patch, agent A1, registers all six)")
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
    try:
        assert names <= set(tools.hidden_from_sender())
    finally:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)


def test_result_shaping_trims_what_is_optional():
    from jarvis import tool_result_shaping as shape
    fresh()
    res = of.list_versions("1.20.1", fetch=fake.fetch, cache=False)
    low = shape.shape_result("list_optifine_versions", res, "low")
    assert "note" not in low and all("forge" not in v and "date" not in v for v in low["versions"]), low
    assert low["versions"][0]["file_name"] == I6
    assert shape.shape_result("list_optifine_versions", res, "full") == res


def test_path_tools_rule_this_tool_depends_on_exists():
    from jarvis.actions import path_tools
    assert callable(getattr(path_tools, "_protected_reason", None)), (
        "download_optifine refuses protected folders through path_tools._protected_reason: if it was renamed, update "
        "minecraft_optifine._protected (it refuses every write when the rule can't be loaded)")
    assert path_tools._protected_reason(Path(HOME) / ".jarvis", writing=True)


def test_on_site_rules():
    f = of._on_site
    assert f("http://optifine.net/adloadx?f=x") == "https://optifine.net/adloadx?f=x", "plain http is upgraded"
    assert f("https://optifine.net/downloadx?f=x&x=y") == "https://optifine.net/downloadx?f=x&x=y"
    for bad in ("http://adfoc.us/serve/sitelinks/?id=1", "https://adfoc.us/x", "https://optifine.net.evil.example/x",
                "https://evil.example/optifine.net", "https://optifine.net:8443/x", "https://user@optifine.net/x",
                "ftp://optifine.net/x", "//optifine.net/x", "javascript:alert(1)", "", None):
        assert f(bad) is None, bad
    assert of._upgrade_https("HTTP://optifine.net/a") == "https://optifine.net/a"
    assert of._upgrade_https("https://optifine.net/a") == "https://optifine.net/a"
    assert of.adloadx_url(I6) == "https://optifine.net/adloadx?f=" + I6


def test_route_b_only_and_nothing_is_run():
    src_actions = Path(mo.__file__).read_text(encoding="utf-8")
    src_of = Path(of.__file__).read_text(encoding="utf-8")
    assert "_queue(" not in src_actions and "download_hub" not in src_actions and "add_items" not in src_actions, (
        "route A (queue on the hub) is deliberately not built: the probe could not decide it")
    for text in (src_actions, src_of):
        low = text.lower()
        assert "github.com" not in low and "gitlab.com" not in low, "no other site is named anywhere"
        for run in ("subprocess", "os.startfile", "popen", "os.system", "webbrowser"):
            assert run not in low, "the jar is an installer and nothing here may start anything: " + run
    assert of.HOST == "optifine.net" and of.DOWNLOADS_URL == "https://optifine.net/downloads"


def _run():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = skipped = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print("  ok     %s" % name)
        except Skip as why:
            skipped += 1
            print("  SKIP   %s: %s" % (name, why))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("  FAILED %s: %s: %s" % (name, type(exc).__name__, exc))
    print("\n%d passed, %d failed, %d skipped (%d tests)" % (passed, failed, skipped, len(tests)))
    if skipped:
        print("SKIPPED tests did NOT run: copy the probe's optifine_*.html/json into tests/data/modhosts/, or merge the core patch.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
