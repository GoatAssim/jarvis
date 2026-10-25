#!/usr/bin/env python3
"""Minecraft mod sources, phase 0: ask CurseForge, Modrinth, OptiFine and the loader sites what they REALLY answer.

The plan for the Minecraft tools (search_mc_mods, get_mc_project, download_mc_file, make_modpack_file,
list_optifine_versions, download_optifine) rests on facts that were written down but never checked.  This
script is the experiment that replaces them.  Same precedent as `steam-checks.py`, `tests/addon_checks.py`
and `tests/store_checks.py`: you run it once on your own PC, the answers become test fixtures.

Not a test (no `test_` prefix, the runner skips it).  Python standard library only: it imports nothing from
jarvis, so it runs on a tree where none of the Minecraft code exists yet.  It is READ-ONLY: it asks questions
and writes only its own report and fixture files.  It never keeps an OptiFine jar (headers and the first few
bytes only) and never keeps a modpack (it reads the file list of one small zip, in memory).

    python tests/modhosts_checks.py --list           # print the planned requests, send nothing
    python tests/modhosts_checks.py                  # the full run (about 5 minutes, ~85 requests at most)
    python tests/modhosts_checks.py --no-curseforge --no-optifine --no-loaders

Run it from the repo root.  It writes:

    modhosts-checks-report.txt        (in the folder you ran it from; ends with a one-page SUMMARY)
    tests/data/modhosts/*.json|html   (the fixtures; tests/data/modhosts/README.md says what each one is)

Send both back (the report and the whole tests/data/modhosts/ folder).

CurseForge: your own personal key, in `~/.jarvis/curseforge.json` as {"api_key": "..."} or in the environment
variable CURSEFORGE_API_KEY.  The key is sent ONLY to api.curseforge.com, never to a CDN, never into a URL,
and the script searches every file it wrote for the key before it finishes (it deletes the file and exits
loudly if the key is found).  No key: the CurseForge part is skipped and the report says so.

OptiFine: the owner chose to reverse-engineer the official site, so the script follows ONE older version's link
one hop at a time, with a pause of a couple of seconds between requests, and stops at any host other than
optifine.net.  The page's Download button is an ad wrapper on another host (adfoc.us) that carries the optifine.net
link in its `url` parameter; the script reads that parameter and starts from the direct optifine.net link, and never
contacts the wrapper.  It reads headers and the first bytes of the final file and closes the connection.  The whole
OptiFine part is about 20 requests.
"""
import argparse
import datetime
import gzip
import io
import json
import os
import platform
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, OrderedDict
from html import unescape as html_unescape
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "data" / "modhosts"
REPORT_NAME = "modhosts-checks-report.txt"

MODRINTH = "https://api.modrinth.com/v2"
CURSEFORGE = "https://api.curseforge.com/v1"
CF_SWAGGER = "https://api.curseforge.com/swagger/v1/swagger.json"
CF_GAME = 432                       # Minecraft (plan section 3)
OPT_URL = "https://optifine.net/downloads"
FABRIC_META = "https://meta.fabricmc.net/v2/versions/loader/%s"
QUILT_META = "https://meta.quiltmc.org/v3/versions/loader/%s"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
NEOFORGE_VERSIONS = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
NEOFORGE_LEGACY = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/forge"

MAX_REQUESTS = 100                  # hard ceiling; the planned total is lower (see PLAN)
TIMEOUT = 25                        # per request, so a dead connection never hangs the run
DEFAULT_PAUSE = 0.7                 # seconds between requests (Modrinth allows 300/min; this is far under)
OPT_PAUSE = 2.5                     # optifine.net is ad-funded: be slower there
JSON_CAP = 12 * 1024 * 1024         # parsing guard for API answers (the swagger is the biggest)
HTML_CAP = 1500000                  # saved OptiFine HTML is capped at this many bytes
FALLBACK_MC = "1.21.1"              # only used when the newest Minecraft version has no files yet / tags fail
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

# what the plan already expects; anything else the probe sees goes into "hosts to add to the allow-lists"
PLANNED_HOSTS = {"api.modrinth.com", "cdn.modrinth.com", "api.curseforge.com", "edge.forgecdn.net",
                 "mediafilecdn.forgecdn.net", "optifine.net"}
CF_CDN_ALLOW = [".forgecdn.net", "api.curseforge.com"]
OPT_ALLOW = ["optifine.net", "www.optifine.net"]
LOADER_WORDS = {"forge", "fabric", "quilt", "neoforge", "liteloader", "cauldron", "rift"}
PUBLIC_HEADERS = ("content-type", "content-length", "content-disposition", "content-range", "accept-ranges",
                  "location", "server", "cache-control", "retry-after", "x-ratelimit-limit",
                  "x-ratelimit-remaining", "x-ratelimit-reset", "etag", "last-modified", "age", "via")

# (section, max requests, what).  `--list` prints this; the run keeps to it (and to MAX_REQUESTS).
PLAN = [
    ("modrinth", 1, "GET  api.modrinth.com/v2/tag/game_version               (newest release = the Minecraft version used below)"),
    ("modrinth", 1, "GET  api.modrinth.com/v2/tag/loader"),
    ("modrinth", 2, "GET  api.modrinth.com/v2/search?query=sodium&facets=[[project_type:mod],[categories:fabric],[versions:<mc>]]   (once more with an older version if empty)"),
    ("modrinth", 1, "GET  api.modrinth.com/v2/search   (same with categories:quilt: do Fabric mods show up under Quilt?)"),
    ("modrinth", 3, "GET  api.modrinth.com/v2/search   (project_type:shader, project_type:resourcepack, categories:datapack: one hit each)"),
    ("modrinth", 1, "GET  api.modrinth.com/v2/project/<slug of the first hit>"),
    ("modrinth", 2, "GET  api.modrinth.com/v2/project/<slug>/version?loaders=[..]&game_versions=[..]   (filtered, then unfiltered to prove the filter works)"),
    ("modrinth", 3, "GET  api.modrinth.com/v2/versions?ids=[..] | /version/<id> | /projects?ids=[..]   (bulk fetches)"),
    ("modrinth", 3, "POST api.modrinth.com/v2/version_files | GET /version_file/<sha1> | POST /version_files/update   (by sha1)"),
    ("modrinth", 1, "GET  api.modrinth.com/v2/project/<slug> with a generic User-Agent (what does it get back?)"),
    ("loaders", 3, "GET  meta.fabricmc.net/v2/versions/loader/<mc>          (one more try with an older version if empty; and once for an unknown version, 99.99)"),
    ("loaders", 3, "GET  meta.quiltmc.org/v3/versions/loader/<mc>           (one more try with an older version if empty; and once for an unknown version, 99.99)"),
    ("loaders", 1, "GET  files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"),
    ("loaders", 1, "GET  maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml   (the fallback when promotions has no entry)"),
    ("loaders", 1, "GET  maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"),
    ("loaders", 1, "GET  maven.neoforged.net/api/maven/versions/releases/net/neoforged/forge     (the old 1.20.1 artifact)"),
    ("curseforge", 1, "GET  api.curseforge.com/swagger/v1/swagger.json   (summarised: paths, enums, schema properties)"),
    ("curseforge", 1, "GET  api.curseforge.com/v1/categories?gameId=432&classesOnly=true"),
    ("curseforge", 2, "GET  api.curseforge.com/v1/minecraft/modloader?version=<mc>  and without a version   (loader numbers cross-check)"),
    ("curseforge", 1, "GET  api.curseforge.com/v1/mods/search?gameId=432&classId=<mods>&searchFilter=sodium   (no loader filter; the numbers are read from the answer)"),
    ("curseforge", 2, "GET  api.curseforge.com/v1/mods/search   (same with gameVersion=<mc>&modLoaderType=<fabric>; once more with an older version if empty)"),
    ("curseforge", 1, "GET  api.curseforge.com/v1/mods/<id>"),
    ("curseforge", 2, "GET  api.curseforge.com/v1/mods/<id>/files   (unfiltered, then with gameVersion + modLoaderType)"),
    ("curseforge", 2, "GET  api.curseforge.com/v1/mods/<id>/files/<fileId> | .../download-url"),
    ("curseforge", 2, "POST api.curseforge.com/v1/mods | POST /v1/mods/files   (bulk)"),
    ("curseforge", 6, "GET  api.curseforge.com/v1/mods/search   (up to --cf-scan-pages pages of 50, looking for a project that forbids third-party distribution)"),
    ("curseforge", 2, "GET  api.curseforge.com/v1/mods/<blocked>/files | .../download-url   (the null downloadUrl)"),
    ("curseforge", 4, "GET  <one real downloadUrl on a *.forgecdn.net host>, one hop at a time, 1 byte (no key sent); one extra search only if none was seen yet"),
    ("curseforge", 1, "GET  api.curseforge.com/v1/mods/search?classId=<modpacks>   (pick the smallest modpack file)"),
    ("curseforge", 9, "GET  <that modpack file on the CDN>: its size (up to 3 hops), then its zip file list (whole file if small, else up to 6 byte ranges); no key sent; nothing kept"),
    ("optifine", 2, "GET  optifine.net/downloads   (a second try with a browser User-Agent only if the first is refused)"),
    ("optifine", 3, "GET  the targets of \"Show all versions\" / \"+ More\" / \"+ Preview versions\" (only if they are real links; on the layout seen so far they are javascript and cost nothing)"),
    ("optifine", 5, "GET  the (Mirror) link of ONE older version (direct on optifine.net), one hop at a time (host, status, redirect, content type; 16 bytes of the file).  The Download button is an ad wrapper on another host: NOT contacted"),
    ("optifine", 1, "GET  the first hop again over https (if the site's link was plain http)"),
    ("optifine", 1, "GET  the final link a second time straight away (single use?)"),
    ("optifine", 5, "GET  a fresh chain stopped before the file, then its final link WITHOUT a Referer header"),
    ("optifine", 4, "GET  another fresh chain stopped before the file (a token that is left unused)"),
    ("optifine", 2, "GET  after --optifine-wait seconds (default 120): the first final link again, and the unused fresh one"),
]


# --------------------------------------------------------------------------------------------------------------
# small pure helpers (no network, easy to check by hand)
# --------------------------------------------------------------------------------------------------------------

def vkey(v):
    """Sortable key for '1.21.1', '21.1.172', '26.1': the numbers in it."""
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def host_ok(host, allow):
    """allow holds exact hosts ('optifine.net') and suffixes ('.forgecdn.net')."""
    host = (host or "").lower()
    for a in allow:
        a = a.lower()
        if a.startswith("."):
            if host.endswith(a) or host == a[1:]:
                return True
        elif host == a:
            return True
    return False


def trim(obj, cuts, max_list=8, max_str=400, path="$"):
    """Copy of a parsed answer with long lists and strings cut, so a fixture stays a readable size.
    `cuts` collects {path: original length}; the structure and the key names are never changed."""
    if isinstance(obj, dict):
        return {k: trim(v, cuts, max_list, max_str, path + "." + str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > max_list:
            cuts[path + "[]"] = max(cuts.get(path + "[]", 0), len(obj))
            obj = obj[:max_list]
        return [trim(v, cuts, max_list, max_str, path + "[]") for v in obj]
    if isinstance(obj, str) and len(obj) > max_str:
        cuts[path + " (string)"] = max(cuts.get(path + " (string)", 0), len(obj))
        return obj[:max_str]
    return obj


def shape(o, depth=4):
    """The skeleton of an answer: key names and value types, lists as [first item, 'len=N']."""
    if isinstance(o, dict):
        if depth <= 0:
            return "{...%d keys}" % len(o)
        return {str(k): shape(v, depth - 1) for k, v in list(o.items())[:40]}
    if isinstance(o, list):
        if not o:
            return []
        return [shape(o[0], depth - 1), "len=%d" % len(o)]
    if o is None:
        return "null"
    return type(o).__name__


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + "...(+%d)" % (len(s) - n)


def classify_game_versions(gv):
    """CurseForge mixes Minecraft versions, loader names and other tags in one `gameVersions` list."""
    mcs, loaders, other = [], [], []
    for x in gv or []:
        if not isinstance(x, str):
            continue
        if re.fullmatch(r"\d+(\.\d+)+([-_ ][A-Za-z0-9]+)?", x):
            mcs.append(x)
        elif x.lower() in LOADER_WORDS:
            loaders.append(x)
        else:
            other.append(x)
    return mcs, loaders, other


def derive_loader_numbers(mods):
    """From search results alone: which `modLoader` number goes with which loader NAME.
    A file index says (fileId, modLoader); the file itself lists its loader names in `gameVersions`."""
    tally = {}
    for m in mods:
        if not isinstance(m, dict):
            continue
        files = {f.get("id"): f for f in (m.get("latestFiles") or []) if isinstance(f, dict)}
        for ix in (m.get("latestFilesIndexes") or []):
            if not isinstance(ix, dict):
                continue
            number, f = ix.get("modLoader"), files.get(ix.get("fileId"))
            if number is None or not f:
                continue
            _, loaders, _ = classify_game_versions(f.get("gameVersions"))
            for name in loaders:
                tally.setdefault(number, Counter())[name] += 1
    return tally


def neoforge_prefixes(mc):
    """NeoForge versions start with the Minecraft version.  Old scheme: 1.21.1 -> 21.1.<build>, 1.21 -> 21.0.<build>.
    New scheme (Minecraft 26.x, seen in the first probe run): 26.3 -> 26.3.0.<build>, so the patch number is explicit
    and the build is the fourth number.  The longest rule is tried first so that 26.1 does not swallow 26.1.2.
    The data decides which rule fits (the loader coverage table in the report shows the matches per version)."""
    parts = mc.split(".")
    if parts and parts[0] == "1" and len(parts) >= 2:
        two = (parts[1:] + ["0"])[:2]
        return [(".".join(two) + ".", "old scheme: Minecraft 1.A.B -> A.B.<build> (B is 0 when missing)")]
    three = (parts + ["0", "0"])[:3]
    out = [(".".join(three) + ".", "new scheme: year.drop.patch (patch 0 when missing), then the build")]
    if len(parts) >= 2:
        out.append((".".join(parts[:2]) + ".", "fallback: first two numbers only"))
    return out


def is_stable_version(v):
    return bool(re.fullmatch(r"[0-9][0-9.]*", v or ""))


def parse_content_range_total(cr):
    m = re.match(r"\s*bytes\s+\d+-\d+/(\d+)\s*$", cr or "")
    return int(m.group(1)) if m else None


def filename_from_disposition(cd):
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd or "", re.I)
    return m.group(1) if m else None


class PageScan(HTMLParser):
    """Collects anchors (href, text, raw start tag), script bodies and meta-refresh tags from one page."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.anchors, self.scripts, self.metas = [], [], []
        self._a = None
        self._in_script = False
        self._sbuf = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a":
            if self._a is not None:
                self.anchors.append(self._a)
            self._a = {"href": d.get("href"), "attrs": d, "raw": self.get_starttag_text(), "text": ""}
        elif tag == "script":
            self._in_script, self._sbuf = True, []
        elif tag == "meta" and (d.get("http-equiv") or "").lower() == "refresh":
            self.metas.append(d)

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            self.anchors.append(self._a)
            self._a = None
        elif tag == "script" and self._in_script:
            self.scripts.append("".join(self._sbuf))
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self._sbuf.append(data)
        elif self._a is not None:
            self._a["text"] += data

    def finish(self):
        try:
            self.close()
        except Exception:
            pass
        if self._a is not None:
            self.anchors.append(self._a)
            self._a = None
        return self


def scan_html(html):
    s = PageScan()
    try:
        s.feed(html)
    except Exception:
        pass
    return s.finish()


def optifine_target(href, base):
    """(optifine.net URL, wrapper host) for one link of the downloads page.  The Download button is an ad wrapper, e.g.
    http://adfoc.us/serve/sitelinks/?id=1&url=http://optifine.net/adloadx?f=X.jar&x=d52b, whose `url` parameter is the real
    optifine.net link.  The wrapper host is returned so the report can name it; it is never contacted."""
    if not href:
        return None, None
    u = urllib.parse.urljoin(base, html_unescape(href))
    p = urllib.parse.urlparse(u)
    if host_ok(p.hostname, OPT_ALLOW):
        return u, None
    inner = (urllib.parse.parse_qs(p.query).get("url") or [None])[0]
    if inner and host_ok(urllib.parse.urlparse(inner).hostname, OPT_ALLOW):
        return inner, p.hostname
    return None, p.hostname


_OPT_TR = re.compile(r"<tr\b[^>]*\bclass\s*=\s*(['\"])([^'\"]*\bdownloadLine\b[^'\"]*)\1[^>]*>(.*?)</tr>", re.I | re.S)
_OPT_H2 = re.compile(r"<h2[^>]*>\s*Minecraft\s+([^<]+?)\s*</h2>", re.I)


def _opt_cell(body, cls):
    m = re.search(r"<td\b[^>]*\bclass\s*=\s*['\"][^'\"]*\b%s\b[^'\"]*['\"][^>]*>(.*?)</td>" % cls, body, re.I | re.S)
    return m.group(1) if m else ""


def _opt_href(cell):
    m = re.search(r"""<a\b[^>]*\bhref\s*=\s*(['"])(.*?)\1""", cell, re.I | re.S)
    return m.group(2) if m else None


def _opt_text(cell):
    return " ".join(re.sub(r"<[^>]+>", " ", html_unescape(cell)).split())


def parse_optifine_rows(html, base):
    """The rows of the real page: <tr class='downloadLine downloadLineMain|More|Preview'> under <h2>Minecraft X</h2>.
    Main = the recommended build, More = older builds of the same version, Preview = preview builds."""
    heads = [(m.start(), m.group(1).strip()) for m in _OPT_H2.finditer(html)]
    rows = []
    for m in _OPT_TR.finditer(html):
        classes = m.group(2).split()
        body = m.group(3)
        kind = ("preview" if "downloadLinePreview" in classes else "more" if "downloadLineMore" in classes
                else "main" if "downloadLineMain" in classes else "other")
        mc = None
        for pos, name in heads:
            if pos < m.start():
                mc = name
            else:
                break
        dl_raw, mir_raw = _opt_href(_opt_cell(body, "colDownload")), _opt_href(_opt_cell(body, "colMirror"))
        dl, wrapper = optifine_target(dl_raw, base)
        mirror, _ = optifine_target(mir_raw, base)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(mirror or dl or "").query)
        f = (q.get("f") or [None])[0]
        if not mc and f:
            fm = re.match(r"(?:preview_)?OptiFine_([0-9][0-9.]*)_", f)
            mc = fm.group(1) if fm else None
        rows.append({"file": f, "mc": mc, "kind": kind, "preview": kind == "preview", "label": _opt_text(_opt_cell(body, "colFile")),
                     "download": dl, "download_href_raw": html_unescape(dl_raw) if dl_raw else None, "wrapper_host": wrapper,
                     "mirror": mirror, "date": _opt_text(_opt_cell(body, "colDate")), "forge": _opt_text(_opt_cell(body, "colForge")),
                     "raw_tag": clip(m.group(0), 420)})
    return rows


def _rows_from_anchors(scan, base):
    """Fallback for a page whose rows are not <tr class='downloadLine ...'> any more: pair the Download and (Mirror) anchors."""
    def fname(a):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(a.get("href") or "").query)
        return (q.get("f") or [None])[0]
    downloads, mirrors = [], {}
    for a in scan.anchors:
        text = " ".join((a["text"] or "").split()).lower()
        if not a.get("href"):
            continue
        if text == "download":
            downloads.append(a)
        elif "mirror" in text and fname(a):
            mirrors[fname(a)] = urllib.parse.urljoin(base, a["href"])
    rows = []
    for a in downloads:
        dl, wrapper = optifine_target(a["href"], base)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(dl or "").query)
        f = (q.get("f") or [None])[0] or fname(a)
        m = re.match(r"(?:preview_)?OptiFine_([0-9][0-9.]*)_", f or "")
        kind = "preview" if f and (f.startswith("preview_") or re.search(r"_pre\d*", f)) else "other"
        rows.append({"file": f, "mc": m.group(1) if m else None, "kind": kind, "preview": kind == "preview", "label": "",
                     "download": dl, "download_href_raw": a["href"], "wrapper_host": wrapper, "mirror": mirrors.get(f),
                     "date": "", "forge": "", "raw_tag": a["raw"]})
    return rows


def analyse_downloads(html, base):
    """What a parser finds on optifine.net/downloads, plus the markup of the 'more versions' controls."""
    scan = scan_html(html)
    rows = parse_optifine_rows(html, base)
    parsed_by = "table rows"
    if not any(r["download"] or r["mirror"] for r in rows):
        rows, parsed_by = _rows_from_anchors(scan, base), "anchors (the row markup was not recognised)"
    names = sorted(set(re.findall(r"(?:preview_)?OptiFine_[0-9][\w.\-]*?\.jar", html)))
    mcs = []
    for r in rows:
        if r["mc"] and r["mc"] not in mcs:
            mcs.append(r["mc"])
    if not mcs:
        for n in names:
            m = re.match(r"(?:preview_)?OptiFine_([0-9][0-9.]*)_", n)
            if m and m.group(1) not in mcs:
                mcs.append(m.group(1))
    markers = OrderedDict()
    for label, pat in (("show_all_versions", r"Show\s+all\s+versions"), ("more", r"\+\s*More"),
                       ("preview_versions", r"\+\s*Preview\s+versions")):
        wins = []
        for m in re.finditer(pat, html, re.I):
            wins.append(html[max(0, m.start() - 160): m.end() + 120])
            if len(wins) >= 2:
                break
        markers[label] = wins
    toggles = []
    for a in scan.anchors:
        text = " ".join((a["text"] or "").split())
        if re.search(r"show all versions|^\+\s*more|preview versions", text, re.I):
            toggles.append({"text": text, "href": a.get("href"), "raw_tag": a["raw"]})
    return {"bytes": len(html), "anchors": len(scan.anchors), "parsed_by": parsed_by,
            "download_links": sum(1 for r in rows if r["download"] or r["mirror"]),
            "mirror_links": sum(1 for r in rows if r["mirror"]),
            "wrapper_hosts": dict(Counter(r["wrapper_host"] for r in rows if r["wrapper_host"])),
            "kinds": dict(Counter(r["kind"] for r in rows)),
            "download_same_as_mirror": sum(1 for r in rows if r["download"] and r["download"] == r["mirror"]),
            "distinct_jar_names": len(names), "jar_name_sample": names[:6],
            "distinct_mc_versions": mcs, "downloadline_rows": len(_OPT_TR.findall(html)),
            "rows": rows, "markers": markers, "toggles": toggles[:10]}


def pick_row(rows, forced_mc=None):
    """The older version the hop tests follow: not the newest, not a preview, the recommended (main) build if there is one."""
    stable = [r for r in rows if r["mc"] and not r["preview"] and (r["mirror"] or r["download"])]
    if not stable:
        return None, "no stable row with an optifine.net link was found"
    mcs = []
    for r in stable:
        if r["mc"] not in mcs:
            mcs.append(r["mc"])
    newest = max(mcs, key=vkey)
    if forced_mc:
        if forced_mc not in mcs:
            return None, "--optifine-mc %s has no stable row on the page (versions: %s)" % (forced_mc, ", ".join(sorted(mcs, key=vkey, reverse=True)[:12]))
        cands = [forced_mc]
        why = "--optifine-mc %s" % forced_mc
    else:
        cands = [m for m in ("1.20.1", "1.19.2", "1.21.1", "1.18.2") if m in mcs and m != newest]
        why = "an older, widely used version"
    if not cands:
        older = sorted([m for m in mcs if m != newest], key=vkey, reverse=True)
        cands = older[:1] or [newest]
        why = "second-newest Minecraft version on the page" if older else "only version on the page"
    mine = [r for r in stable if r["mc"] == cands[0]]
    mine.sort(key=lambda r: 0 if r["kind"] == "main" else 1)
    if mine:
        return mine[0], "%s (Minecraft %s, %s build; newest on the page is %s)" % (why, cands[0], mine[0]["kind"], newest)
    return None, "no row found for Minecraft %s" % cands[0]


def analyse_hop_page(html, page_url):
    """An intermediate page (ads, countdown): where does its link to the file come from?"""
    scan = scan_html(html)
    cands = []
    for a in scan.anchors:
        href = a.get("href") or ""
        text = " ".join((a["text"] or "").split())
        if re.search(r"downloadx|[?&]x=", href, re.I):
            cands.append((1, "anchor", href, text, a["raw"]))
        elif text.lower() in ("download", "click here", "here") and href and not href.startswith(("#", "javascript")):
            cands.append((3, "anchor (by its text)", href, text, a["raw"]))
    for s in scan.scripts:
        for m in re.finditer(r"""["']([^"'\s]*(?:downloadx|[?&]x=)[^"'\s]*)["']""", s):
            cands.append((2, "script text", m.group(1), "", ""))
    for mt in scan.metas:
        m = re.search(r"url\s*=\s*(.+)$", mt.get("content") or "", re.I)
        if m:
            cands.append((2, "meta refresh", m.group(1).strip(" '\""), "", ""))
    cands.sort(key=lambda c: c[0])
    out = {"bytes": len(html), "anchors": len(scan.anchors), "scripts": len(scan.scripts),
           "next_url": None, "found_in": "nothing", "candidate_count": len(cands)}
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    out["title"] = " ".join(t.group(1).split())[:120] if t else None
    if cands:
        _, how, href, text, raw = cands[0]
        out["next_url"] = urllib.parse.urljoin(page_url, href)
        out["found_in"] = how
        out["link_text"] = text
        out["link_tag"] = raw
    hints = []
    for m in re.finditer(r"setTimeout|setInterval|countdown|count down|please wait|\d+\s*seconds?", html, re.I):
        hints.append(html[max(0, m.start() - 120): m.end() + 120])
        if len(hints) >= 4:
            break
    out["countdown_hints"] = hints
    secs = [int(x) for x in re.findall(r"(\d{1,3})\s*seconds?", html, re.I)]
    out["visible_seconds"] = secs[:6]
    wins = []
    for m in re.finditer(r"downloadx", html, re.I):
        wins.append(html[max(0, m.start() - 160): m.end() + 200])
        if len(wins) >= 3:
            break
    out["downloadx_snippets"] = wins
    out["script_excerpts"] = [clip(s.strip(), 1500) for s in scan.scripts
                              if s.strip() and re.search(r"download|timeout|[?&]x=", s, re.I)][:3]
    return out


def link_shape(url):
    u = urllib.parse.urlparse(url)
    params = {}
    for k, v in urllib.parse.parse_qs(u.query).items():
        val = v[0] if v else ""
        charset = ("hex" if re.fullmatch(r"[0-9a-fA-F]+", val or "x!") else
                   "alnum" if re.fullmatch(r"[0-9A-Za-z]+", val or "!") else "mixed")
        params[k] = {"length": len(val), "charset": charset, "value": val if len(val) <= 80 else val[:80] + "..."}
    return {"scheme": u.scheme, "host": u.hostname, "path": u.path, "params": params}


def summarise_swagger(sw):
    """The parts of CurseForge's swagger the tools depend on: routes with parameter names, every enum, every
    schema's property names, and any property that looks like the distribution flag."""
    out = {"openapi": sw.get("openapi") or sw.get("swagger"), "info": sw.get("info") or {},
           "paths": OrderedDict(), "enums": OrderedDict(), "schemas": OrderedDict(), "distribution_properties": {}}
    for path, item in (sw.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch") or not isinstance(op, dict):
                continue
            params = []
            for p in op.get("parameters") or []:
                if isinstance(p, dict) and p.get("name"):
                    params.append("%s%s:%s" % (p["name"], "*" if p.get("required") else "", p.get("in", "?")))
            body = None
            content = (op.get("requestBody") or {}).get("content") or {}
            for _, mt in content.items():
                body = ((mt or {}).get("schema") or {}).get("$ref", "").split("/")[-1] or None
                break
            out["paths"].setdefault(path, {})[method.upper()] = {"operationId": op.get("operationId"),
                                                                 "params": params, "body": body}
    comps = (sw.get("components") or {}).get("schemas") or sw.get("definitions") or {}
    for name, sch in comps.items():
        if not isinstance(sch, dict):
            continue
        if "enum" in sch:
            out["enums"][name] = {"values": sch.get("enum"), "type": sch.get("type"), "description": sch.get("description"),
                                  "names": sch.get("x-enumNames") or sch.get("x-enum-varnames") or sch.get("enumNames")}
        props = {}
        for pname, pv in (sch.get("properties") or {}).items():
            if not isinstance(pv, dict):
                continue
            t = pv.get("$ref", "").split("/")[-1] or pv.get("type") or "?"
            if pv.get("type") == "array":
                it = pv.get("items") or {}
                t = "[%s]" % (it.get("$ref", "").split("/")[-1] or it.get("type") or "?")
            props[pname] = t + ("?" if pv.get("nullable") else "")
            if re.search(r"istribution", pname, re.I):
                out["distribution_properties"].setdefault(name, []).append(pname)
        if props:
            out["schemas"][name] = props
    return out


def decide_optifine_route(o):
    """Route A or B from D6, from what the tests saw.  Returns (route, why)."""
    if not o.get("reached_file"):
        return ("none", "the Download link could not be followed to a file automatically (%s); the tool should say so "
                        "and give the official page link (plan section 3 / D6)" % o.get("stopped_because", "see the hops"))
    reuse, fresh = o.get("reuse_2s"), o.get("fresh_after_wait")
    parts = []
    if reuse is not None:
        parts.append("the same final link %s a second time 2 seconds later" % ("worked" if reuse else "did NOT work"))
    if o.get("old_after_wait") is not None:
        parts.append("the used link %s after %ss" % ("still worked" if o["old_after_wait"] else "did NOT work", o.get("wait")))
    if fresh is not None:
        parts.append("an unused fresh link %s after %ss" % ("worked" if fresh else "did NOT work", o.get("wait")))
    why = "; ".join(parts) or "no re-use test could run"
    if reuse is True and fresh is True:
        return "A", why + " -> the link outlives a delay and a retry, so it can be queued on the hub like any file"
    if reuse is False or fresh is False:
        return "B", why + " -> the link is single-use or short-lived, so the tool must download inside the call"
    return "undetermined", why + " -> not enough answers; Route B is the safe default until the test is repeated"


# --------------------------------------------------------------------------------------------------------------
# the one place that touches the network (a test harness may replace `transport`)
# --------------------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are NOT followed: the probe walks them one hop at a time and records each."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Resp(object):
    def __init__(self, method, url, status=None, headers=None, body=b"", error=None, ms=0, truncated=False, peeked=False):
        self.method, self.url, self.status = method, url, status
        self.headers, self.body, self.error = headers or {}, body or b"", error
        self.ms, self.truncated, self.peeked = ms, truncated, peeked

    def header(self, name):
        return self.headers.get(name.lower())

    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8", "replace"))
        except Exception:
            return None

    def public_headers(self):
        return {k: v for k, v in self.headers.items() if k in PUBLIC_HEADERS}

    def location(self):
        loc = self.header("location")
        return urllib.parse.urljoin(self.url, loc) if loc else None


def transport(method, url, headers, body, timeout, body_limit, peek):
    """GET/POST one URL.  Never raises.  `peek`: for a non-text answer read only that many bytes and close."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        try:
            r = _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            r = e
        status = r.getcode()
        hdrs = {}
        for k, v in r.headers.items():
            k = k.lower()
            if k in ("set-cookie", "cookie", "authorization", "proxy-authorization"):
                continue
            hdrs[k] = (hdrs[k] + ", " + v) if k in hdrs else v
        ctype = (hdrs.get("content-type") or "").lower()
        texty = (not ctype) or ctype.startswith("text/") or "json" in ctype or "xml" in ctype or "javascript" in ctype
        peeked = truncated = False
        try:
            if peek is not None and not texty and status < 300:
                data, peeked = r.read(peek), True
            else:
                data = r.read(body_limit + 1)
                if len(data) > body_limit:
                    data, truncated = data[:body_limit], True
        except Exception as e:           # a connection that drops mid-body: keep what we have
            data = b""
            hdrs["x-probe-read-error"] = "%s: %s" % (type(e).__name__, e)
        finally:
            try:
                r.close()
            except Exception:
                pass
        if hdrs.get("content-encoding", "").lower() == "gzip" and not peeked and not truncated:
            try:
                data = gzip.decompress(data)
            except Exception:
                pass
        return Resp(method, url, status, hdrs, data, None, int((time.monotonic() - t0) * 1000), truncated, peeked)
    except Exception as e:
        return Resp(method, url, None, {}, b"", "%s: %s" % (type(e).__name__, e), int((time.monotonic() - t0) * 1000))


# --------------------------------------------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------------------------------------------

def load_key():
    """(key or None, where it came from, why there is none).  The key is never printed or stored anywhere but in memory;
    the reason names files and field NAMES only."""
    p = Path.home() / ".jarvis" / "curseforge.json"
    shown = "~/.jarvis/curseforge.json"
    reasons = []
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                reasons.append("%s is JSON but not an object like {\"api_key\": \"...\"}" % shown)
            else:
                k = str(data.get("api_key") or "").strip()
                if k:
                    return k, shown, None
                reasons.append("%s exists but has no non-empty \"api_key\" (fields it has: %s)" % (shown, sorted(data.keys()) or "none"))
        else:
            hint = ""
            if p.with_name("curseforge.json.txt").exists():
                hint = " (but curseforge.json.txt exists: Windows hid the real extension; rename it)"
            reasons.append("%s does not exist%s" % (shown, hint))
    except ValueError as e:
        reasons.append("%s is not valid JSON (%s)" % (shown, e))
    except Exception as e:
        reasons.append("%s could not be read (%s: %s)" % (shown, type(e).__name__, e))
    k = os.environ.get("CURSEFORGE_API_KEY", "").strip()
    if k:
        return k, "environment variable CURSEFORGE_API_KEY", None
    reasons.append("environment variable CURSEFORGE_API_KEY is not set")
    return None, None, "; ".join(reasons)


def read_build():
    """The build number for the User-Agent, read from build_info.py as TEXT (nothing from jarvis is imported)."""
    try:
        text = (ROOT / "jarvis-cli" / "jarvis" / "build_info.py").read_text(encoding="utf-8")
        m = re.search(r"^BUILD_NUMBER\s*=\s*(\d+)", text, re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "probe"


class RangeFile(object):
    """A read-only file object over HTTP Range requests, so zipfile can list a zip without downloading it."""
    BLOCK = 131072

    def __init__(self, probe, url, size, label, max_requests=6):
        self.probe, self.url, self.size, self.label = probe, url, size, label
        self.pos, self.cache, self.requests, self.max_requests = 0, {}, 0, max_requests

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        self.pos = max(0, min(self.pos, self.size))
        return self.pos

    def _block(self, i):
        if i not in self.cache:
            if self.requests >= self.max_requests:
                raise IOError("too many range requests for one zip listing")
            a, b = i * self.BLOCK, min(self.size, (i + 1) * self.BLOCK) - 1
            r = self.probe.send("GET", self.url, headers={"Range": "bytes=%d-%d" % (a, b)}, pause=DEFAULT_PAUSE,
                                body_limit=self.BLOCK + 4096, label="%s bytes %d-%d" % (self.label, a, b))
            self.requests += 1
            if r.status != 206:
                raise IOError("range request answered %s" % r.status)
            self.cache[i] = r.body
        return self.cache[i]

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        end = min(self.size, self.pos + n)
        out = []
        while self.pos < end:
            i = self.pos // self.BLOCK
            blk = self._block(i)
            off = self.pos - i * self.BLOCK
            take = blk[off: off + (end - self.pos)]
            if not take:
                break
            out.append(take)
            self.pos += len(take)
        return b"".join(out)


class Probe(object):
    def __init__(self, args):
        self.args = args
        self.key, self.key_source, self.key_reason = (None, None, None) if args.no_curseforge else load_key()
        self.build = read_build()
        self.ua = "Jarvis/%s (github.com/GoatAssim)" % self.build
        self.n = 0
        self._last = 0.0
        self.lines = []                 # the report body
        self.reqlog = []                # one line per request, for the appendix
        self.failures = []
        self.written = []               # fixture paths actually written
        self.meta = OrderedDict()       # fixture name -> how it was obtained
        self.facts = {"cdn_hosts": set(), "hosts_seen": set(), "mc_releases": []}
        self.mc, self.mc_source, self.mc_mod = None, None, None
        self.cf_sample_url = None
        self.opt = {}
        self.started = datetime.datetime.now()

    # ---- output ----------------------------------------------------------------------------------------------
    def redact(self, s):
        s = "" if s is None else str(s)
        if self.key:
            s = s.replace(self.key, "<key redacted>").replace(urllib.parse.quote(self.key, safe=""), "<key redacted>")
        return s

    def say(self, text="", console=True):
        text = self.redact(text)
        self.lines.append(text)
        if console:
            try:
                print(text)
            except UnicodeEncodeError:
                print(text.encode("ascii", "replace").decode("ascii"))
            sys.stdout.flush()

    def section(self, title):
        self.say("")
        self.say("=" * 78)
        self.say(title)
        self.say("=" * 78)

    def fail(self, label, reason):
        reason = clip(self.redact(reason), 300)
        self.failures.append((label, reason))
        self.say("  !! FAILED: %s: %s" % (label, reason))

    # ---- the network -----------------------------------------------------------------------------------------
    def send(self, method, url, headers=None, body=None, cf=False, pause=None, body_limit=JSON_CAP, peek=None, label=""):
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        h = {"User-Agent": self.ua, "Accept-Encoding": "identity"}
        if headers:
            h.update(headers)
        if cf:
            if host != "api.curseforge.com" or not url.startswith("https://") or not self.key:
                raise RuntimeError("refusing to attach the CurseForge key to a request for %s" % host)
            h["x-api-key"] = self.key
        elif any(k.lower() == "x-api-key" for k in h):
            raise RuntimeError("x-api-key is only added through cf=True")
        if self.n >= MAX_REQUESTS:
            return Resp(method, url, error="request ceiling (%d) reached; this request was not sent" % MAX_REQUESTS)
        wait = (DEFAULT_PAUSE if pause is None else pause) - (time.monotonic() - self._last)
        if wait > 0 and self.n:
            time.sleep(wait)
        self.n += 1
        r = transport(method, url, h, body, TIMEOUT, body_limit, peek)
        self._last = time.monotonic()
        if r.error:
            r.error = self.redact(r.error)
        self.facts["hosts_seen"].add(host)
        if host == "api.modrinth.com" and r.header("x-ratelimit-limit"):
            snap = {k: r.header(k) for k in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")}
            self.facts.setdefault("ratelimit_first", snap)
            self.facts["ratelimit_last"] = snap
        short = urllib.parse.urlparse(url)
        line = "[%2d] %-4s %s%s -> %s (%s, %d ms)%s" % (
            self.n, method, host, clip(short.path + ("?" + short.query if short.query else ""), 80),
            r.status if r.status is not None else "no answer", human_bytes(len(r.body)), r.ms,
            ("  " + clip(r.error, 120)) if r.error else "")
        self.reqlog.append(self.redact(line) + ("   (%s)" % label if label else ""))
        try:
            print("  " + self.redact(line))
        except UnicodeEncodeError:
            pass
        return r

    def jget(self, url, label, method="GET", body=None, cf=False, params=None, expect_fail=False, pause=None):
        """One JSON call.  Returns (Resp, parsed answer or None).  A non-2xx answer is recorded as a failure
        unless the call is an experiment where that answer IS the data (expect_fail)."""
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers, data = {"Accept": "application/json"}, None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        r = self.send(method, url, headers=headers, body=data, cf=cf, pause=pause, label=label)
        if r.error or r.status is None:
            self.fail(label, r.error or "no answer")
            return r, None
        parsed = r.json()
        if not (200 <= r.status < 300) and not expect_fail:
            self.fail(label, "HTTP %s %s" % (r.status, clip(r.text().replace("\n", " "), 160)))
        elif parsed is None and r.body and not expect_fail:
            self.fail(label, "HTTP %s but the answer is not JSON: %s" % (r.status, clip(r.text(), 120)))
        return r, parsed

    def note_url(self, url):
        """Remember the host of a real download URL (the CDN allow-list comes from these)."""
        host = (urllib.parse.urlparse(url or "").hostname or "").lower()
        if host:
            self.facts["cdn_hosts"].add(host)
            if self.cf_sample_url is None and host.endswith(".forgecdn.net") and (url or "").startswith("https://"):
                self.cf_sample_url = url

    # ---- fixtures --------------------------------------------------------------------------------------------
    def fixture(self, name, data, resp=None, max_list=8, max_str=400, derived=None, request=None):
        cuts = {}
        out = trim(data, cuts, max_list, max_str)
        text = json.dumps(out, indent=1, ensure_ascii=False)
        self._write(name, text)
        info = {"status": resp.status if resp is not None else None,
                "headers": resp.public_headers() if resp is not None else {},
                "lists_and_strings_cut": cuts}
        if resp is not None:
            info["request"] = {"method": resp.method, "url": resp.url}
        if request:
            info["request"] = request
        if derived is not None:
            info["derived"] = derived
        self.meta[name] = info

    def fixture_text(self, name, text, resp=None, note=None):
        self._write(name, text)
        self.meta[name] = {"status": resp.status if resp is not None else None,
                           "request": {"method": "GET", "url": resp.url} if resp is not None else None,
                           "note": note}

    def _write(self, name, text):
        text = self.redact(text)
        try:
            FIXTURES.mkdir(parents=True, exist_ok=True)
            path = FIXTURES / name
            path.write_text(text, encoding="utf-8", newline="\n")
            self.written.append(path)
        except Exception as e:
            self.fail("write " + name, "%s: %s" % (type(e).__name__, e))

    # ---- following redirects and intermediate pages, one hop at a time -------------------------------------
    def follow_chain(self, start, label, allow, headers=None, referer=None, send_referer=True, max_hops=6,
                     pause=None, peek=16, countdown_wait=False, note=False):
        """GET `start` and follow 3xx, meta refreshes and the link of an intermediate page, one request per
        hop.  Stops at the first answer that is not a redirect or a page, or at a host outside `allow`.
        Returns (hops, info) with info = {final_url, final_referer, pending_url, pending_referer}."""
        hops, url, prev = [], start, referer
        info = {"final_url": None, "final_referer": None, "pending_url": None, "pending_referer": None}
        for i in range(1, max_hops + 1):
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if not host_ok(host, allow):
                hops.append({"n": i, "url": url, "host": host, "stopped": "host is outside the probe's allow-list for "
                             "this chain (%s); not contacted" % ", ".join(allow)})
                info["stopped_because"] = "the chain left the allowed hosts (next hop: %s)" % host
                break
            h = dict(headers or {})
            if send_referer and prev:
                h["Referer"] = prev
            r = self.send("GET", url, headers=h, pause=pause, body_limit=400000, peek=peek, label="%s hop %d" % (label, i))
            ctype = (r.header("content-type") or "").split(";")[0].strip().lower()
            hop = {"n": i, "method": "GET", "url": url, "scheme": urllib.parse.urlparse(url).scheme, "host": host,
                   "status": r.status, "error": r.error, "content_type": ctype or None,
                   "content_length": r.header("content-length"), "content_range": r.header("content-range"),
                   "content_disposition": r.header("content-disposition"), "location": r.location(),
                   "referer_sent": h.get("Referer"), "headers": r.public_headers()}
            hops.append(hop)
            if r.error or r.status is None:
                info["stopped_because"] = r.error or "no answer"
                break
            loc = r.location()
            if 300 <= r.status < 400 and loc:
                prev, url = url, loc
                continue
            if r.status < 300 and (ctype.startswith("text/html") or ctype == "application/xhtml+xml" or (not ctype and r.body[:1] == b"<")):
                text = r.text()
                page = analyse_hop_page(text, url)
                hop["page"] = page
                hop["body"] = text[:120000]
                nxt = page.get("next_url")
                if nxt:
                    if countdown_wait:
                        secs = [s for s in page.get("visible_seconds", []) if 0 < s <= 15]
                        if secs:
                            hop["waited_s"] = max(secs) + 1
                            self.say("      (page mentions a %ds countdown; waiting %ds like a browser would)" % (max(secs), max(secs) + 1))
                            time.sleep(max(secs) + 1)
                    prev, url = url, nxt
                    continue
                info["stopped_because"] = "an intermediate page with no link to the file that this probe can find (see its markup)"
                break
            # anything else is an answer we stop on: a file, or an error
            hop["first_bytes_hex"] = r.body[:4].hex()
            hop["zip_signature"] = (r.body[:4] == b"PK\x03\x04") if len(r.body) >= 4 else None   # None: too few bytes read to say
            hop["filename"] = filename_from_disposition(r.header("content-disposition"))
            if r.status < 300:
                info["final_url"], info["final_referer"] = url, (h.get("Referer") if send_referer else None)
            else:
                info["stopped_because"] = "HTTP %s" % r.status
            break
        else:
            # max_hops reached while still following: the URL we would have requested next is "pending"
            info["pending_url"], info["pending_referer"] = url, prev
            info["stopped_because"] = "hop limit reached"
        if not info["final_url"] and not info.get("pending_url") and "stopped_because" not in info:
            info["stopped_because"] = "chain ended without a file"
        if note:
            for hop in hops:
                self.note_url(hop.get("url"))
        return hops, info

    def describe_hops(self, hops):
        for hop in hops:
            if hop.get("stopped"):
                self.say("      hop %d: %s" % (hop["n"], hop["stopped"]))
                continue
            extra = ""
            if hop.get("location"):
                extra += "  -> %s" % clip(hop["location"], 110)
            if hop.get("filename"):
                extra += "  file=%s" % hop["filename"]
            if hop.get("zip_signature") is not None and (hop.get("status") or 500) < 300:
                extra += "  zip-signature=%s" % ("yes" if hop["zip_signature"] else "NO (first bytes %s)" % hop.get("first_bytes_hex"))
            self.say("      hop %d: %s %s  status=%s  type=%s  length=%s%s" % (
                hop["n"], hop["scheme"], hop["host"], hop["status"], hop["content_type"], hop["content_length"], extra))
            page = hop.get("page")
            if page:
                self.say("             page: title=%r, %d anchors, %d scripts; link to the next step found in: %s"
                         % (page.get("title"), page["anchors"], page["scripts"], page["found_in"]))
                if page.get("next_url"):
                    self.say("             next link: %s" % clip(page["next_url"], 150))
                for hint in page.get("countdown_hints", [])[:2]:
                    self.say("             countdown/script hint: %s" % clip(" ".join(hint.split()), 200))

    # =========================================================================================================
    # MODRINTH
    # =========================================================================================================
    def run_modrinth(self):
        self.section("MODRINTH  (https://api.modrinth.com/v2, no key; User-Agent: %s)" % self.ua)
        facts = self.facts
        r, tags = self.jget(MODRINTH + "/tag/game_version", "modrinth GET /tag/game_version")
        mc, source = self.args.mc_version, "--mc-version"
        if isinstance(tags, list):
            rel = [t for t in tags if isinstance(t, dict) and t.get("version_type") == "release" and t.get("version")]
            rel.sort(key=lambda t: t.get("date") or "", reverse=True)
            facts["mc_releases"] = [t["version"] for t in rel[:8]]
            self.fixture("modrinth_tag_game_version.json", rel[:12], resp=r, max_list=12)
            if not mc and rel:
                mc, source = rel[0]["version"], "newest release in Modrinth's game_version tag"
            self.say("game_version tag: %d entries; newest releases: %s" % (len(tags), ", ".join(facts["mc_releases"][:6])))
        if not mc:
            mc, source = FALLBACK_MC, "built-in fallback (the tag list was not available)"
        self.mc, self.mc_source = mc, source
        self.say("Minecraft version used for every test below: %s  (%s)" % (mc, source))

        r, lt = self.jget(MODRINTH + "/tag/loader", "modrinth GET /tag/loader")
        if isinstance(lt, list):
            self.fixture("modrinth_tag_loader.json", lt, resp=r, max_list=40)
            facts["ms_loaders"] = {x.get("name"): x.get("supported_project_types") for x in lt if isinstance(x, dict)}
            self.say("loader tag: %s" % ", ".join("%s%s" % (k, v) for k, v in facts["ms_loaders"].items()))

        # --- search ---
        def search(label, facets, query, limit):
            params = OrderedDict()
            if query:
                params["query"] = query
            params["facets"] = json.dumps(facets, separators=(",", ":"))
            params["limit"] = limit
            params["index"] = "relevance"
            resp, data = self.jget(MODRINTH + "/search", "modrinth GET /search (%s)" % label, params=params)
            hits = data.get("hits") if isinstance(data, dict) else None
            return resp, data, (hits if isinstance(hits, list) else [])

        facets = [["project_type:mod"], ["categories:fabric"], ["versions:%s" % mc]]
        r, d, hits = search("fabric, %s" % mc, facets, "sodium", 5)
        self.mc_mod = mc
        if not hits and mc != FALLBACK_MC:
            self.say("  no Fabric hits for %s (too new?); trying %s instead" % (mc, FALLBACK_MC))
            self.mc_mod = FALLBACK_MC
            facets = [["project_type:mod"], ["categories:fabric"], ["versions:%s" % FALLBACK_MC]]
            r, d, hits = search("fabric, %s" % FALLBACK_MC, facets, "sodium", 5)
        slug = project_id = None
        if hits:
            self.fixture("modrinth_search.json", d, resp=r, max_list=5, max_str=300)
            honoured_v = all(self.mc_mod in (h.get("versions") or []) for h in hits)
            honoured_l = all("fabric" in (h.get("categories") or []) for h in hits)
            facts["ms_facets_honoured"] = honoured_v and honoured_l
            slug, project_id = hits[0].get("slug"), hits[0].get("project_id")
            self.say("search: parameters query, facets (JSON array of arrays), limit, index were accepted; %d of %s hits; "
                     "response keys: %s" % (len(hits), d.get("total_hits"), ", ".join(sorted(d.keys()))))
            self.say("  hit keys: %s" % ", ".join(sorted(hits[0].keys())))
            self.say("  facets honoured (every hit lists %s in `versions` and fabric in `categories`): %s"
                     % (self.mc_mod, "YES" if facets and honoured_v and honoured_l else "NO"))
            facts["ms_hit_keys"] = sorted(hits[0].keys())
            self.say("  first hit: slug=%s project_id=%s project_type=%s" % (slug, project_id, hits[0].get("project_type")))
        else:
            self.fail("modrinth search", "no hits, so the project/version tests cannot run")

        qf = [["project_type:mod"], ["categories:quilt"], ["versions:%s" % self.mc_mod]]
        r, d, qhits = search("quilt, %s" % self.mc_mod, qf, "sodium", 5)
        if isinstance(d, dict):
            self.fixture("modrinth_search_quilt.json", d, resp=r, max_list=5, max_str=300)
            slugs = [h.get("slug") for h in qhits]
            facts["ms_quilt_has_fabric_pick"] = bool(slug and slug in slugs)
            self.say("quilt search: %d hits (%s); does the Fabric pick '%s' appear under Quilt? %s; do quilt hits list "
                     "'quilt' in categories? %s" % (len(qhits), ", ".join(map(str, slugs)), slug,
                                                    "YES" if facts["ms_quilt_has_fabric_pick"] else "no",
                                                    "yes" if qhits and all("quilt" in (h.get("categories") or []) for h in qhits) else "no"))

        for label, fac, fname in (("shader", [["project_type:shader"]], "modrinth_search_shader.json"),
                                  ("resourcepack", [["project_type:resourcepack"]], "modrinth_search_resourcepack.json"),
                                  ("datapack", [["project_type:mod"], ["categories:datapack"]], "modrinth_search_datapack.json")):
            r, d, hh = search(label, fac, "", 2)
            if isinstance(d, dict):
                self.fixture(fname, d, resp=r, max_list=2, max_str=200)
                self.say("%s search: %d hit(s); project_type=%s; categories of the first hit: %s" % (
                    label, len(hh), hh[0].get("project_type") if hh else "-", hh[0].get("categories") if hh else "-"))
                facts.setdefault("ms_kinds", {})[label] = (hh[0].get("project_type"), hh[0].get("categories")) if hh else None

        if not slug:
            return
        # --- project, versions ---
        r, proj = self.jget(MODRINTH + "/project/" + urllib.parse.quote(slug), "modrinth GET /project/{slug}")
        if isinstance(proj, dict):
            self.fixture("modrinth_project.json", proj, resp=r, max_str=300)
            facts["ms_project_keys"] = sorted(proj.keys())
            self.say("project %s: keys: %s" % (slug, ", ".join(sorted(proj.keys()))))
            self.say("  project_type=%s client_side=%s server_side=%s loaders=%s; %d game_versions"
                     % (proj.get("project_type"), proj.get("client_side"), proj.get("server_side"), proj.get("loaders"),
                        len(proj.get("game_versions") or [])))
        params = OrderedDict([("loaders", json.dumps(["fabric"])), ("game_versions", json.dumps([self.mc_mod]))])
        r, vers = self.jget(MODRINTH + "/project/%s/version" % urllib.parse.quote(slug), "modrinth GET /project/{slug}/version (filtered)", params=params)
        r2, allv = self.jget(MODRINTH + "/project/%s/version" % urllib.parse.quote(slug), "modrinth GET /project/{slug}/version (unfiltered)")
        if isinstance(vers, list) and vers:
            self.fixture("modrinth_versions.json", vers, resp=r, max_list=4, max_str=300)
            ok = all(self.mc_mod in (v.get("game_versions") or []) and "fabric" in (v.get("loaders") or []) for v in vers)
            unf = len(allv) if isinstance(allv, list) else None
            leaked = (sum(1 for v in allv if not (self.mc_mod in (v.get("game_versions") or []) and "fabric" in (v.get("loaders") or [])))
                      if isinstance(allv, list) else None)
            facts["ms_version_filters_honoured"] = ok and (leaked is None or leaked > 0 or unf == len(vers))
            self.say("version list: query parameters `loaders` and `game_versions` (each a JSON array in the URL). Filtered: %d "
                     "versions, unfiltered: %s (of which %s do not match the filter)." % (len(vers), unf, leaked))
            self.say("  filters honoured (every filtered version matches, others were left out): %s"
                     % ("YES" if facts["ms_version_filters_honoured"] else "NO / unclear"))
            v0 = vers[0]
            self.say("  version keys: %s" % ", ".join(sorted(v0.keys())))
            self.say("  file keys: %s" % ", ".join(sorted((v0.get("files") or [{}])[0].keys())))
            for v in vers:
                for f in v.get("files") or []:
                    self.note_url(f.get("url"))
            facts["ms_version_keys"] = sorted(v0.keys())
            ids = [v["id"] for v in vers[:2] if v.get("id")]
            files = v0.get("files") or []
            primary = next((f for f in files if f.get("primary")), files[0] if files else None)
            sha1 = ((primary or {}).get("hashes") or {}).get("sha1")
            self.say("  hashes on a file: %s; download host(s) seen: %s" % (
                sorted(((primary or {}).get("hashes") or {}).keys()),
                sorted({urllib.parse.urlparse(f.get("url") or "").hostname for v in vers for f in v.get("files") or []})))
            # --- bulk and by-hash ---
            r, bulk = self.jget(MODRINTH + "/versions", "modrinth GET /versions?ids=[..]", params={"ids": json.dumps(ids)})
            if isinstance(bulk, list):
                self.fixture("modrinth_bulk_versions.json", bulk, resp=r, max_list=3, max_str=200)
                self.say("bulk versions: `ids` (JSON array) -> %d versions" % len(bulk))
            r, one = self.jget(MODRINTH + "/version/" + urllib.parse.quote(ids[0]), "modrinth GET /version/{id}")
            if isinstance(one, dict):
                self.fixture("modrinth_version.json", one, resp=r, max_str=200)
            r, bp = self.jget(MODRINTH + "/projects", "modrinth GET /projects?ids=[..]", params={"ids": json.dumps([project_id])})
            if isinstance(bp, list):
                self.fixture("modrinth_bulk_projects.json", bp, resp=r, max_list=3, max_str=200)
                self.say("bulk projects: `ids` (JSON array) -> %d projects" % len(bp))
            if sha1:
                body = {"hashes": [sha1], "algorithm": "sha1"}
                r, byhash = self.jget(MODRINTH + "/version_files", "modrinth POST /version_files", method="POST", body=body)
                if isinstance(byhash, dict):
                    self.fixture("modrinth_version_files.json", byhash, resp=r, max_str=200,
                                 request={"method": "POST", "url": MODRINTH + "/version_files", "body": body})
                    self.say("POST /version_files {hashes:[sha1], algorithm:'sha1'} -> object keyed by hash: %s; hash found: %s"
                             % (list(byhash.keys())[:1], sha1 in byhash))
                r, one_hash = self.jget(MODRINTH + "/version_file/" + sha1, "modrinth GET /version_file/{sha1}")
                if isinstance(one_hash, dict):
                    self.fixture("modrinth_version_file.json", one_hash, resp=r, max_str=200)
                ubody = {"hashes": [sha1], "algorithm": "sha1", "loaders": ["fabric"], "game_versions": [self.mc_mod]}
                r, upd = self.jget(MODRINTH + "/version_files/update", "modrinth POST /version_files/update", method="POST", body=ubody)
                if isinstance(upd, dict):
                    self.fixture("modrinth_version_files_update.json", upd, resp=r, max_str=200,
                                 request={"method": "POST", "url": MODRINTH + "/version_files/update", "body": ubody})
                    self.say("POST /version_files/update {hashes, algorithm, loaders, game_versions} -> object keyed by hash; "
                             "answered for our hash: %s" % (sha1 in upd))
            else:
                self.fail("modrinth by-hash tests", "the first version had no file with a sha1")
        elif isinstance(vers, list):
            self.fail("modrinth versions", "the filtered list for %s / fabric is empty" % self.mc_mod)

        # --- a generic User-Agent ---
        r = self.send("GET", MODRINTH + "/project/" + urllib.parse.quote(slug), headers={"User-Agent": "okhttp/4.9.3", "Accept": "application/json"},
                      label="generic User-Agent okhttp/4.9.3")
        self.fixture("modrinth_generic_ua.json", {"user_agent_sent": "okhttp/4.9.3", "status": r.status, "error": r.error,
                                                  "body_start": clip(r.text(), 400)}, resp=r)
        facts["ms_generic_ua"] = (r.status, clip(r.text().replace("\n", " "), 200))
        self.say("generic User-Agent 'okhttp/4.9.3' -> HTTP %s %s" % (r.status, clip(r.text().replace("\n", " "), 200)))
        rl = facts.get("ratelimit_last") or {}
        self.say("rate-limit headers (last Modrinth answer): %s" % (rl if rl else "none seen"))

    # =========================================================================================================
    # LOADER META
    # =========================================================================================================
    def run_loaders(self):
        mc = self.mc or self.args.mc_version or FALLBACK_MC
        self.section("LOADER METADATA  (latest stable loader version; Minecraft %s)" % mc)
        lm = self.facts.setdefault("loader_meta", OrderedDict())

        def per_mc(name, template, fixture, stable_rule):
            used = mc
            r, d = self.jget(template % urllib.parse.quote(used), "%s loader list for %s" % (name, used))
            if (not isinstance(d, list) or not d) and used != FALLBACK_MC:
                self.say("  %s has no list for %s; trying %s" % (name, used, FALLBACK_MC))
                used = FALLBACK_MC
                r, d = self.jget(template % urllib.parse.quote(used), "%s loader list for %s" % (name, used))
            info = {"url": template % used, "mc_used": used}
            if isinstance(d, list) and d:
                latest, how = stable_rule(d)
                info.update({"shape": shape(d[0], 3), "entries": len(d), "latest_stable": latest, "rule": how})
                self.fixture(fixture, d, resp=r, max_list=3, max_str=200, derived={"mc_version": used, "latest_stable": latest, "rule": how})
                self.say("%s: %s -> %d entries; latest stable = %s  (%s)" % (name, info["url"], len(d), latest, how))
                self.say("  entry shape: %s" % clip(json.dumps(shape(d[0], 3)), 700))
            else:
                info["error"] = "no usable list"
            lm[name] = info

        def fabric_rule(d):
            for e in d:
                lo = (e or {}).get("loader") or {}
                if lo.get("stable") is True:
                    return lo.get("version"), "first entry whose loader.stable is true (list is newest first)"
            return ((d[0] or {}).get("loader") or {}).get("version"), "no entry has loader.stable == true; took the first"

        def quilt_rule(d):
            for e in d:
                v = ((e or {}).get("loader") or {}).get("version") or ""
                if v and not re.search(r"alpha|beta|rc|pre|snapshot", v, re.I):
                    return v, "first entry whose version has no alpha/beta/rc/pre tag (Quilt has no stable flag, if absent)"
            return ((d[0] or {}).get("loader") or {}).get("version"), "every entry looks pre-release; took the first"

        per_mc("fabric", FABRIC_META, "loader_meta_fabric.json", fabric_rule)
        per_mc("quilt", QUILT_META, "loader_meta_quilt.json", quilt_rule)

        # what do Fabric and Quilt answer for a Minecraft version that does not exist (for the `no_loader_version` refusal)?
        for name, template in (("fabric", FABRIC_META), ("quilt", QUILT_META)):
            r, d = self.jget(template % "99.99", "%s loader list for an unknown version" % name, expect_fail=True)
            self.fixture("loader_meta_%s_unknown.json" % name, {"minecraft_version_asked": "99.99", "status": r.status,
                                                                  "body": d if d is not None else clip(r.text(), 300)}, resp=r)
            lm.setdefault(name, {})["unknown_version_answer"] = {"status": r.status, "body": clip(json.dumps(d) if d is not None else r.text(), 160)}
            self.say("%s for an unknown Minecraft version (99.99) -> HTTP %s %s" % (name, r.status, clip(json.dumps(d) if d is not None else r.text(), 160)))

        # Forge: promotions_slim.json, then the maven listing
        promos, neo_versions = None, None
        r, d = self.jget(FORGE_PROMOS, "forge promotions_slim.json")
        info = {"url": FORGE_PROMOS}
        if isinstance(d, dict) and isinstance(d.get("promos"), dict):
            promos = d["promos"]
            keys = sorted(promos.keys(), key=lambda k: vkey(k.split("-")[0]), reverse=True)
            rec, lat = promos.get("%s-recommended" % mc), promos.get("%s-latest" % mc)
            chosen = rec or lat
            how = ("promos['%s-recommended']" % mc) if rec else (("promos['%s-latest'] (no recommended build)" % mc) if lat else "no promos entry for this Minecraft version")
            subset = {k: promos[k] for k in list(OrderedDict.fromkeys(
                [k for k in promos if k.startswith(mc + "-") or k.startswith(FALLBACK_MC + "-")] + keys[:10]))}
            self.fixture("loader_meta_forge.json", {"homepage": d.get("homepage"), "promos": subset}, resp=r, max_list=50,
                         derived={"mc_version": mc, "recommended": rec, "latest": lat, "rule": how})
            info.update({"shape": {"homepage": "str", "promos": "{'<mc>-latest' | '<mc>-recommended': '<forge version>'}"},
                         "entries": len(promos), "recommended": rec, "latest": lat, "chosen": chosen, "rule": how,
                         "newest_keys": keys[:6]})
            self.say("forge promotions: %d promo keys; for %s: recommended=%s latest=%s  -> %s" % (len(promos), mc, rec, lat, how))
            self.say("  newest promo keys: %s" % ", ".join(keys[:6]))
            self.say("  a full Forge version is '<mc>-<forge>', e.g. %s-%s" % (mc, chosen or "<forge>"))
        else:
            info["error"] = "promotions_slim.json not usable"
        lm["forge"] = info
        r = self.send("GET", FORGE_MAVEN, headers={"Accept": "application/xml"}, label="forge maven-metadata.xml")
        if r.status == 200:
            versions = re.findall(r"<version>([^<]+)</version>", r.text())
            mine = [v for v in versions if v.startswith(mc + "-")]
            self.fixture("loader_meta_forge_maven.json", {"url": FORGE_MAVEN, "version_count": len(versions),
                                                          "first": versions[:5], "last": versions[-5:],
                                                          "for_mc_version": mine[-10:], "xml_start": r.text()[:500]}, resp=r, max_list=12)
            lm["forge"]["maven_fallback"] = {"url": FORGE_MAVEN, "versions": len(versions), "for_mc": mine[-3:]}
            self.say("forge maven-metadata.xml: %d versions; for %s: %s" % (len(versions), mc, mine[-3:] or "none"))
        else:
            self.fail("forge maven-metadata.xml", r.error or "HTTP %s" % r.status)

        # NeoForge
        r, d = self.jget(NEOFORGE_VERSIONS, "neoforge versions list")
        info = {"url": NEOFORGE_VERSIONS}
        if isinstance(d, dict) and isinstance(d.get("versions"), list):
            vs = [v for v in d["versions"] if isinstance(v, str)]
            ordered = sorted(vs, key=vkey)
            chosen, rule_used = None, None
            for prefix, why in neoforge_prefixes(mc):
                mine = [v for v in ordered if v.startswith(prefix)]
                if mine:
                    stable = [v for v in mine if is_stable_version(v)]
                    chosen, rule_used = (stable or mine)[-1], "%s (prefix %s; %s; newest %s by version number)" % (
                        why, prefix, "%d stable of %d" % (len(stable), len(mine)), "stable" if stable else "any")
                    sample = mine[-8:]
                    break
            if not chosen:
                sample = ordered[-8:]
            self.fixture("loader_meta_neoforge.json", {"isSnapshot": d.get("isSnapshot"), "versions": list(OrderedDict.fromkeys(sample + ordered[-4:]))}, resp=r, max_list=30,
                         derived={"mc_version": mc, "latest_stable": chosen, "rule": rule_used})
            info.update({"shape": {"isSnapshot": "bool", "versions": "[str] (%d)" % len(vs)}, "entries": len(vs),
                         "latest_stable": chosen, "rule": rule_used or "no NeoForge version matched any prefix rule for %s" % mc,
                         "newest": ordered[-5:]})
            self.say("neoforge: %d versions; newest 5 by number: %s" % (len(vs), ordered[-5:]))
            self.say("  Minecraft %s maps to: %s" % (mc, chosen or "NOTHING FOUND (look at the newest versions above and map by hand)"))
            self.say("  rule: %s" % info["rule"])
            self.say("  list order in the answer (first 3 / last 3): %s / %s  (so sort by number, do not trust the order)" % (vs[:3], vs[-3:]))
            neo_versions = ordered
        else:
            info["error"] = "versions list not usable"
        lm["neoforge"] = info
        r, d = self.jget(NEOFORGE_LEGACY, "neoforge legacy artifact (net.neoforged:forge)", expect_fail=True)
        if isinstance(d, dict) and isinstance(d.get("versions"), list):
            vs = sorted([v for v in d["versions"] if isinstance(v, str)], key=vkey)
            self.fixture("loader_meta_neoforge_legacy.json", {"isSnapshot": d.get("isSnapshot"), "versions": vs[-8:]}, resp=r, max_list=10)
            lm["neoforge"]["legacy_artifact"] = {"url": NEOFORGE_LEGACY, "versions": len(vs), "newest": vs[-3:]}
            self.say("neoforge legacy artifact (net.neoforged:forge, used for 1.20.1): %d versions, newest: %s" % (len(vs), vs[-3:]))
        else:
            lm["neoforge"]["legacy_artifact"] = {"url": NEOFORGE_LEGACY, "status": r.status}
            self.say("neoforge legacy artifact: HTTP %s (nothing usable)" % r.status)
        self.loader_coverage(promos, neo_versions)

    def loader_coverage(self, promos, neo_versions):
        """Forge and NeoForge for the newest Minecraft releases, from the lists already fetched (no requests).  It shows
        whether the version rules hold for 2-number (26.3) and 3-number (26.1.2) Minecraft versions, and which versions have
        a recommended Forge build."""
        releases = (self.facts.get("mc_releases") or [self.mc])[:8]
        table = OrderedDict()
        self.say("")
        self.say("loader coverage for the newest Minecraft releases (Forge: promotions; NeoForge: prefix rule, newest stable / newest of any kind):")
        for mcv in releases:
            row = {}
            if promos is not None:
                row["forge"] = {"recommended": promos.get(mcv + "-recommended"), "latest": promos.get(mcv + "-latest")}
            if neo_versions is not None:
                ent = None
                for prefix, why in neoforge_prefixes(mcv):
                    mine = [v for v in neo_versions if v.startswith(prefix)]
                    if mine:
                        stable = [v for v in mine if is_stable_version(v)]
                        ent = {"prefix": prefix, "rule": why, "matches": len(mine), "stable_matches": len(stable),
                               "newest_stable": stable[-1] if stable else None, "newest_any": mine[-1]}
                        break
                row["neoforge"] = ent or {"prefix": neoforge_prefixes(mcv)[0][0], "matches": 0}
            table[mcv] = row
            f, n = row.get("forge") or {}, row.get("neoforge") or {}
            self.say("  %-8s forge recommended=%-9s latest=%-9s | neoforge prefix %-8s matches=%-3s newest stable=%-12s newest any=%s" % (
                mcv, f.get("recommended"), f.get("latest"), n.get("prefix"), n.get("matches"), n.get("newest_stable"), n.get("newest_any")))
        self.facts["loader_coverage"] = table
        self.fixture("loader_meta_coverage.json", {"minecraft_releases_checked": releases, "per_version": table}, max_list=20)

    # =========================================================================================================
    # CURSEFORGE
    # =========================================================================================================
    def run_curseforge(self):
        self.section("CURSEFORGE  (https://api.curseforge.com/v1; your key goes ONLY to this host)")
        facts = self.facts
        cf = facts.setdefault("cf", {})
        mc = self.mc or FALLBACK_MC

        def get(path, label, params=None, **kw):
            return self.jget(CURSEFORGE + path, "curseforge " + label, cf=True, params=params, **kw)

        # --- swagger ---
        r = self.send("GET", CF_SWAGGER, headers={"Accept": "application/json"}, cf=True, label="curseforge swagger")
        sw = r.json() if r.status == 200 else None
        if isinstance(sw, dict):
            summ = summarise_swagger(sw)
            self.fixture("curseforge_swagger_summary.json", summ, resp=r, max_list=100000, max_str=2000)
            cf["enums"] = summ["enums"]
            cf["distribution_properties"] = summ["distribution_properties"]
            self.say("swagger: %s %s; %d routes, %d enums, %d schemas" % (summ["openapi"], (summ["info"] or {}).get("title"),
                                                                          sum(len(v) for v in summ["paths"].values()), len(summ["enums"]), len(summ["schemas"])))
            for name, e in summ["enums"].items():
                if re.search(r"loader|hash|algo|release|relation|sortfield|status", name, re.I):
                    self.say("  enum %s = %s%s" % (name, e["values"], ("  names=%s" % e["names"]) if e.get("names") else
                                                  ("  (%s)" % clip(e.get("description"), 160) if e.get("description") else "")))
            self.say("  properties that look like the distribution flag: %s" % (summ["distribution_properties"] or "NONE in the swagger"))
            interesting = [p for p in summ["paths"] if re.search(r"search|/mods|/files|categories|modloader|fingerprint|download-url|game", p, re.I)]
            self.say("  routes of interest: %s" % ", ".join("%s %s" % ("/".join(summ["paths"][p].keys()), p) for p in interesting[:24]))
        else:
            self.fail("curseforge swagger", r.error or "HTTP %s (%s)" % (r.status, clip(r.text(), 100)))
            if r.status in (401, 403):
                self.say("  The key was refused. Check it in ~/.jarvis/curseforge.json (key name: api_key); the remaining CurseForge tests are skipped.")
                cf["key_refused"] = True
                return

        # --- classes ---
        r, d = get("/categories", "GET /categories?gameId=432&classesOnly=true", params=OrderedDict([("gameId", CF_GAME), ("classesOnly", "true")]))
        classes = (d or {}).get("data") if isinstance(d, dict) else None
        class_id = modpack_class = None
        if isinstance(classes, list):
            self.fixture("curseforge_classes.json", d, resp=r, max_list=60, max_str=200)
            cf["classes"] = OrderedDict((c.get("slug"), c.get("id")) for c in classes if isinstance(c, dict))
            self.say("classes (id name slug): " + "; ".join("%s %s %s" % (c.get("id"), c.get("name"), c.get("slug")) for c in classes if isinstance(c, dict)))

            def find(*pats):
                for c in classes:
                    blob = "%s %s" % (c.get("slug"), c.get("name"))
                    if any(re.search(p, blob, re.I) for p in pats):
                        return c.get("id")
            class_id = find(r"^mc-mods", r"^mods\b") or 6
            modpack_class = find(r"modpack")
            cf["class_mods"], cf["class_modpacks"] = class_id, modpack_class
            self.say("  -> mods class id = %s, modpacks class id = %s; the other classes above are the ids for resource packs, worlds, shaders, ..." % (class_id, modpack_class))
        else:
            class_id = 6
            self.say("  (classes unavailable; using 6 for mods only to keep going; this is the plan's unverified guess)")

        # --- modloader list ---
        r, d = get("/minecraft/modloader", "GET /minecraft/modloader?version=%s" % mc, params={"version": mc})
        ml = (d or {}).get("data") if isinstance(d, dict) else None
        if isinstance(ml, list):
            self.fixture("curseforge_loaders.json", d, resp=r, max_list=30, max_str=200)
            self.describe_modloaders(ml, "for %s" % mc)
        r, d = get("/minecraft/modloader", "GET /minecraft/modloader (no version)")
        ml_all = (d or {}).get("data") if isinstance(d, dict) else None
        if isinstance(ml_all, list):
            self.fixture("curseforge_loaders_all.json", {"data": ml_all[:40]}, resp=r, max_list=40, max_str=200)
            self.describe_modloaders(ml_all, "all versions (%d entries)" % len(ml_all))

        # --- search without a loader filter: read the loader numbers out of the answer ---
        qp = OrderedDict([("gameId", CF_GAME), ("classId", class_id), ("searchFilter", "sodium"), ("sortField", 2), ("sortOrder", "desc"), ("pageSize", 10)])
        r, d = get("/mods/search", "GET /mods/search (no loader filter)", params=qp)
        mods = (d or {}).get("data") if isinstance(d, dict) else None
        mod_id = file_id = None
        fabric_id = None
        if isinstance(mods, list) and mods:
            self.fixture("curseforge_search.json", d, resp=r, max_list=5, max_str=250)
            cf["search_keys"] = sorted(mods[0].keys())
            self.say("search: parameters gameId, classId, searchFilter, sortField, sortOrder, pageSize were accepted; %d mods; pagination=%s"
                     % (len(mods), (d or {}).get("pagination")))
            self.say("  classId honoured (every mod's classId == %s): %s" % (class_id, "YES" if all(m.get("classId") == class_id for m in mods) else "NO"))
            self.say("  mod keys: %s" % ", ".join(sorted(mods[0].keys())))
            for m in mods:
                for f in m.get("latestFiles") or []:
                    self.note_url(f.get("downloadUrl"))
            tally = derive_loader_numbers(mods)
            nums = OrderedDict()
            for number, c in sorted(tally.items(), key=lambda kv: kv[0]):
                nums[number] = c.most_common(1)[0][0]
                self.say("  modLoader %s appears with loader name(s) %s in the files' gameVersions" % (number, dict(c)))
            cf["loader_numbers_from_search"] = nums
            for number, name in nums.items():
                if name.lower() == "fabric":
                    fabric_id = number
            mod_id = mods[0].get("id")
            lf = (mods[0].get("latestFiles") or [{}])
            file_id = (lf[0] or {}).get("id")
            flag_names = sorted({p for ps in (cf.get("distribution_properties") or {}).values() for p in ps}) or ["allowModDistribution"]
            cf["flag_candidates"] = flag_names
            self.say("  distribution flag on these %d mods: %s" % (len(mods), {fn: dict(Counter(repr(m.get(fn, "<missing>")) for m in mods)) for fn in flag_names}))
        else:
            self.fail("curseforge search", "no mods came back; the mod/file tests are skipped")
        if fabric_id is None:
            fabric_id = next((v for k, v in (cf.get("loaders_from_endpoint") or {}).items() if k == "fabric"), None)
        if fabric_id is None:
            fabric_id = 4
            self.say("  (no source gave the Fabric number; trying 4 only to ask the question: UNVERIFIED guess, the answer below tests it)")
        cf["fabric_id_used"] = fabric_id

        if isinstance(mods, list) and mods:
            used_mc = mc
            qp2 = OrderedDict(qp)
            qp2.update([("gameVersion", mc), ("modLoaderType", fabric_id)])
            r, d2 = get("/mods/search", "GET /mods/search (gameVersion=%s, modLoaderType=%s)" % (mc, fabric_id), params=qp2)
            m2 = (d2 or {}).get("data") if isinstance(d2, dict) else None
            if not m2 and mc != FALLBACK_MC:
                self.say("  no mods for %s with that loader; trying %s" % (mc, FALLBACK_MC))
                used_mc = FALLBACK_MC
                qp2["gameVersion"] = FALLBACK_MC
                r, d2 = get("/mods/search", "GET /mods/search (gameVersion=%s, modLoaderType=%s)" % (used_mc, fabric_id), params=qp2)
                m2 = (d2 or {}).get("data") if isinstance(d2, dict) else None
            if isinstance(m2, list) and m2:
                self.fixture("curseforge_search_filtered.json", d2, resp=r, max_list=4, max_str=250)
                ok = [any(ix.get("gameVersion") == used_mc and ix.get("modLoader") == fabric_id for ix in (m.get("latestFilesIndexes") or [])) for m in m2]
                cf["filters_honoured"] = all(ok)
                self.say("  filtered search: %d mods; parameters gameVersion and modLoaderType honoured (each mod has a file index for %s + loader %s): %s (%d of %d)"
                         % (len(m2), used_mc, fabric_id, "YES" if all(ok) else "PARTLY/NO", sum(ok), len(ok)))
                mod_id = m2[0].get("id")
                lf = m2[0].get("latestFiles") or [{}]
                file_id = (lf[0] or {}).get("id") or file_id
                cf["mc_used"] = used_mc
            else:
                cf["mc_used"] = used_mc
                self.say("  filtered search returned nothing usable; later tests use the unfiltered pick")

        if mod_id:
            r, d = get("/mods/%s" % mod_id, "GET /mods/{id}")
            if isinstance(d, dict) and isinstance(d.get("data"), dict):
                self.fixture("curseforge_mod.json", d, resp=r, max_list=5, max_str=250)
                self.say("mod %s: %s; links keys: %s" % (mod_id, d["data"].get("name"), sorted((d["data"].get("links") or {}).keys())))
            r, d = get("/mods/%s/files" % mod_id, "GET /mods/{id}/files (unfiltered)", params={"pageSize": 50})
            files = (d or {}).get("data") if isinstance(d, dict) else None
            if isinstance(files, list) and files:
                self.fixture("curseforge_files.json", d, resp=r, max_list=6, max_str=250)
                self.describe_files(files, cf)
                file_id = file_id or files[0].get("id")
                for f in files:
                    self.note_url(f.get("downloadUrl"))
            mc_f = cf.get("mc_used") or mc
            r, d = get("/mods/%s/files" % mod_id, "GET /mods/{id}/files (gameVersion + modLoaderType)",
                       params=OrderedDict([("gameVersion", mc_f), ("modLoaderType", fabric_id), ("pageSize", 5)]))
            ff = (d or {}).get("data") if isinstance(d, dict) else None
            if isinstance(ff, list):
                self.fixture("curseforge_files_filtered.json", d, resp=r, max_list=4, max_str=250)
                good = [(mc_f in (f.get("gameVersions") or [])) and any(str(x).lower() == "fabric" for x in (f.get("gameVersions") or [])) for f in ff]
                cf["file_filters_honoured"] = bool(ff) and all(good)
                self.say("  files filtered by gameVersion=%s&modLoaderType=%s: %d files; every file lists that version and 'Fabric' in gameVersions: %s"
                         % (mc_f, fabric_id, len(ff), "YES" if ff and all(good) else ("no files" if not ff else "NO")))
                if ff:
                    file_id = ff[0].get("id")
                    for f in ff:
                        self.note_url(f.get("downloadUrl"))
            if file_id:
                r, d = get("/mods/%s/files/%s" % (mod_id, file_id), "GET /mods/{id}/files/{fileId}")
                if isinstance(d, dict):
                    self.fixture("curseforge_file.json", d, resp=r, max_list=5, max_str=250)
                r, d = get("/mods/%s/files/%s/download-url" % (mod_id, file_id), "GET /mods/{id}/files/{fileId}/download-url", expect_fail=True)
                if isinstance(d, dict):
                    self.fixture("curseforge_download_url.json", d, resp=r)
                    self.say("download-url endpoint -> HTTP %s %s" % (r.status, clip(json.dumps(d), 200)))
            # --- bulk ---
            ids = [m.get("id") for m in (mods or [])[:3] if m.get("id")]
            r, d = get("/mods", "POST /mods (bulk)", method="POST", body={"modIds": ids, "filterPcOnly": True})
            if isinstance(d, dict):
                self.fixture("curseforge_bulk.json", d, resp=r, max_list=3, max_str=200,
                             request={"method": "POST", "url": CURSEFORGE + "/mods", "body": {"modIds": ids, "filterPcOnly": True}})
                self.say("bulk mods: body {modIds:[..], filterPcOnly:true} -> %d mods" % len(d.get("data") or []))
            fids = [x for x in [file_id] + [(m.get("latestFiles") or [{}])[0].get("id") for m in (mods or [])[:2]] if x]
            r, d = get("/mods/files", "POST /mods/files (bulk)", method="POST", body={"fileIds": fids})
            if isinstance(d, dict):
                self.fixture("curseforge_bulk_files.json", d, resp=r, max_list=3, max_str=200,
                             request={"method": "POST", "url": CURSEFORGE + "/mods/files", "body": {"fileIds": fids}})
                self.say("bulk files: body {fileIds:[..]} -> %d files" % len(d.get("data") or []))
                for f in d.get("data") or []:
                    self.note_url(f.get("downloadUrl"))

        self.cf_blocked_scan(class_id, cf)
        self.cf_cdn_and_pack(class_id, modpack_class, cf)

    def describe_modloaders(self, ml, label):
        cf = self.facts.setdefault("cf", {})
        by_prefix = {}
        for e in ml:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                by_prefix.setdefault(e["name"].split("-")[0].lower(), Counter())[e.get("type")] += 1
        self.say("modloader endpoint %s: entry keys %s" % (label, sorted(ml[0].keys()) if ml and isinstance(ml[0], dict) else "-"))
        if ml and isinstance(ml[0], dict):
            self.say("  sample: %s" % clip(json.dumps(ml[0]), 300))
        self.say("  name prefix -> `type` values: %s" % {k: dict(v) for k, v in by_prefix.items()})
        if by_prefix:
            nums = cf.setdefault("loaders_from_endpoint", {})
            for k, v in by_prefix.items():
                top = v.most_common(1)[0][0]
                if isinstance(top, int):
                    nums[k] = top

    def describe_files(self, files, cf):
        f0 = files[0]
        self.say("files: %d returned; file keys: %s" % (len(files), ", ".join(sorted(f0.keys()))))
        mcs, loaders, other = Counter(), Counter(), Counter()
        for f in files:
            a, b, c = classify_game_versions(f.get("gameVersions"))
            mcs.update(a)
            loaders.update(b)
            other.update(c)
        self.say("  gameVersions mixes three kinds of entry. Minecraft versions seen: %s; loader names seen: %s; other tags seen: %s"
                 % (list(mcs)[:10], dict(loaders), dict(other)))
        self.say("  sample gameVersions: %s" % f0.get("gameVersions"))
        self.say("  sortableGameVersions entry: %s" % clip(json.dumps((f0.get("sortableGameVersions") or [None])[0]), 250))
        algos = Counter(h.get("algo") for f in files for h in (f.get("hashes") or []) if isinstance(h, dict))
        self.say("  hash algo numbers seen in file hashes: %s (compare with the HashAlgo enum above; usually 1=sha1, 2=md5)" % dict(algos))
        self.say("  releaseType values: %s; null downloadUrl in this list: %d of %d" % (
            dict(Counter(f.get("releaseType") for f in files)), sum(1 for f in files if not f.get("downloadUrl")), len(files)))
        cf["hash_algos_seen"] = dict(algos)
        cf["gameversions_kinds"] = {"minecraft": list(mcs)[:10], "loaders": dict(loaders), "other": dict(other)}

    def cf_blocked_scan(self, class_id, cf):
        flag_names = cf.get("flag_candidates") or ["allowModDistribution"]
        found, scanned, tally = None, 0, Counter()
        null_url_other = 0
        forced = self.args.cf_blocked_id
        self.say("")
        self.say("-- looking for a project that forbids third-party distribution --")
        if forced:
            r, d = self.jget(CURSEFORGE + "/mods/%s" % forced, "curseforge GET /mods/{id} (--cf-blocked-id)", cf=True)
            if isinstance(d, dict) and isinstance(d.get("data"), dict):
                found = d["data"]
        else:
            for page in range(max(0, self.args.cf_scan_pages)):
                qp = OrderedDict([("gameId", CF_GAME), ("classId", class_id), ("sortField", 2), ("sortOrder", "desc"),
                                  ("pageSize", 50), ("index", page * 50)])
                r, d = self.jget(CURSEFORGE + "/mods/search", "curseforge GET /mods/search (scan page %d)" % (page + 1), cf=True, params=qp)
                mods = (d or {}).get("data") if isinstance(d, dict) else None
                if not isinstance(mods, list) or not mods:
                    break
                for m in mods:
                    scanned += 1
                    for fn in flag_names:
                        tally[(fn, repr(m.get(fn, "<missing>")))] += 1
                    if any(not f.get("downloadUrl") for f in (m.get("latestFiles") or []) if isinstance(f, dict)):
                        null_url_other += 1
                    for f in m.get("latestFiles") or []:
                        self.note_url(f.get("downloadUrl"))
                    if found is None and any(m.get(fn) is False for fn in flag_names):
                        found = m
                if found is not None:
                    break
            self.say("scanned %d mods; flag values seen (flag, value): count -> %s" % (scanned, {"%s=%s" % k: v for k, v in tally.items()}))
            self.say("  mods with a null downloadUrl in `latestFiles`: %d" % null_url_other)
        out = {"found": bool(found), "scanned": scanned, "flag_names_checked": flag_names,
               "flag_values_seen": {"%s=%s" % k: v for k, v in tally.items()}, "mods_with_null_download_url": null_url_other}
        if found:
            flag_used = next((fn for fn in flag_names if found.get(fn) is False), None)
            cf["blocked_flag"], cf["blocked_example"] = flag_used, {"id": found.get("id"), "name": found.get("name")}
            self.say("FOUND: %s (id %s): flag `%s` is false" % (found.get("name"), found.get("id"), flag_used))
            out["mod"] = found
            r, d = self.jget(CURSEFORGE + "/mods/%s/files" % found.get("id"), "curseforge GET /mods/{blocked}/files", cf=True, params={"pageSize": 5})
            if isinstance(d, dict):
                out["files"] = d
                files = d.get("data") or []
                nulls = sum(1 for f in files if not f.get("downloadUrl"))
                cf["blocked_null_urls"] = (nulls, len(files))
                self.say("  its files: %d listed, %d with downloadUrl == null  (the null URL is what the tool must treat as 'not downloadable here')" % (len(files), nulls))
                if files:
                    r, d = self.jget(CURSEFORGE + "/mods/%s/files/%s/download-url" % (found.get("id"), files[0].get("id")),
                                     "curseforge GET /mods/{blocked}/files/{fileId}/download-url", cf=True, expect_fail=True)
                    out["download_url_endpoint"] = {"status": r.status, "body": d if d is not None else clip(r.text(), 300)}
                    self.say("  the download-url endpoint for it -> HTTP %s %s" % (r.status, clip(r.text().replace("\n", " "), 160)))
        else:
            cf["blocked_flag"] = None
            self.say("NOT FOUND within %d mods. Run again with --cf-scan-pages 20, or --cf-blocked-id <a project id you know forbids it>." % scanned)
        self.fixture("curseforge_blocked_project.json", out, max_list=5, max_str=250)

    def cf_cdn_and_pack(self, class_id, modpack_class, cf):
        self.say("")
        self.say("-- CDN hosts and one small modpack file (the key is NOT sent to these hosts) --")
        sample_url = self.cf_sample_url
        if not sample_url:
            r, d = self.jget(CURSEFORGE + "/mods/search", "curseforge GET /mods/search (a few mods for a CDN sample)", cf=True,
                             params=OrderedDict([("gameId", CF_GAME), ("classId", class_id), ("pageSize", 5), ("sortField", 2), ("sortOrder", "desc")]))
            for m in ((d or {}).get("data") or []) if isinstance(d, dict) else []:
                for f in m.get("latestFiles") or []:
                    if isinstance(f, dict) and f.get("downloadUrl"):
                        self.note_url(f["downloadUrl"])
            sample_url = self.cf_sample_url
        if sample_url:
            hops, info = self.follow_chain(sample_url, "cf cdn", CF_CDN_ALLOW, headers={"Range": "bytes=0-0"}, send_referer=False,
                                           peek=1, max_hops=3, note=True)
            self.describe_hops(hops)
            self.fixture("curseforge_cdn_hops.json", {"start": sample_url, "hops": hops, "info": info}, max_list=10, max_str=300)
            cf["cdn_range_ok"] = any(h.get("status") == 206 for h in hops)
        else:
            self.say("  no real download URL was available for a CDN sample")
        self.say("download URL hosts seen so far: %s" % sorted(h for h in self.facts["cdn_hosts"] if h not in ("cdn.modrinth.com",)))

        if not modpack_class:
            self.say("  no modpacks class id, so the modpack-file test is skipped")
            return
        qp = OrderedDict([("gameId", CF_GAME), ("classId", modpack_class), ("pageSize", 50), ("sortField", 2), ("sortOrder", "desc")])
        r, d = self.jget(CURSEFORGE + "/mods/search", "curseforge GET /mods/search (modpacks)", cf=True, params=qp)
        cap = int(self.args.cf_pack_mb * 1024 * 1024)
        best = None
        for m in ((d or {}).get("data") or []) if isinstance(d, dict) else []:
            for f in m.get("latestFiles") or []:
                if isinstance(f, dict) and f.get("downloadUrl") and not f.get("isServerPack") and (f.get("fileLength") or 0) > 1024:
                    if best is None or f["fileLength"] < best[1]["fileLength"]:
                        best = (m, f)
        if not best:
            self.say("  no downloadable modpack file in the first 50 modpacks; modpack test skipped")
            return
        m, f = best
        self.say("smallest modpack file in the first 50 popular modpacks: %s / %s (%s)" % (m.get("name"), f.get("fileName"), human_bytes(f.get("fileLength"))))
        self.note_url(f.get("downloadUrl"))
        hops, info = self.follow_chain(f["downloadUrl"], "modpack file", CF_CDN_ALLOW, headers={"Range": "bytes=0-0"}, send_referer=False,
                                       peek=1, max_hops=3, note=True)
        self.describe_hops(hops)
        last = hops[-1] if hops else {}
        size = parse_content_range_total(last.get("content_range")) or int(f.get("fileLength") or 0)
        final = info.get("final_url")
        entries, zf, how = None, None, None
        if final and 0 < size <= cap:
            r = self.send("GET", final, label="modpack file (whole, %s)" % human_bytes(size), body_limit=cap + 1024)
            if r.status == 200 and r.body[:2] == b"PK":
                try:
                    zf, how = zipfile.ZipFile(io.BytesIO(r.body)), "downloaded whole into memory (%s), nothing kept" % human_bytes(len(r.body))
                except Exception as e:
                    self.fail("modpack zip", "%s: %s" % (type(e).__name__, e))
            else:
                self.fail("modpack file", "HTTP %s, first bytes %s" % (r.status, r.body[:4].hex()))
        elif final and last.get("status") == 206 and size > 0:
            try:
                zf, how = zipfile.ZipFile(RangeFile(self, final, size, "modpack file")), "read with byte ranges (file is %s, above --cf-pack-mb); nothing kept" % human_bytes(size)
            except Exception as e:
                self.fail("modpack zip (ranges)", "%s: %s" % (type(e).__name__, e))
        else:
            self.say("  the CDN did not give a usable answer for the modpack file; zip listing skipped")
        if zf is not None:
            try:
                infos = zf.infolist()
                names = [i.filename for i in infos]
                top = sorted({n.split("/")[0] for n in names})
                manifest_summary = None
                if "manifest.json" in names:
                    mi = zf.getinfo("manifest.json")
                    if mi.file_size <= 2 * 1024 * 1024:
                        try:
                            mf = json.loads(zf.read("manifest.json").decode("utf-8-sig", "replace"))
                            manifest_summary = {"keys": sorted(mf.keys()), "manifestType": mf.get("manifestType"),
                                                "manifestVersion": mf.get("manifestVersion"), "name": mf.get("name"),
                                                "version": mf.get("version"), "author": mf.get("author"),
                                                "overrides": mf.get("overrides"), "minecraft": mf.get("minecraft"),
                                                "files_count": len(mf.get("files") or []),
                                                "first_files": (mf.get("files") or [])[:3]}
                        except Exception as e:
                            manifest_summary = {"error": "%s: %s" % (type(e).__name__, e)}
                self.say("  zip listing (%s): %d entries; top-level names: %s" % (how, len(names), top[:12]))
                self.say("  manifest.json at the root: %s" % ("YES" if "manifest.json" in names else "NO"))
                if manifest_summary:
                    self.say("  manifest summary: %s" % clip(json.dumps(manifest_summary), 600))
                cf["modpack_manifest_at_root"] = "manifest.json" in names
                self.fixture("curseforge_modpack_entries.json", {
                    "file": {"modId": m.get("id"), "modName": m.get("name"), "fileId": f.get("id"), "fileName": f.get("fileName"),
                             "fileLength": f.get("fileLength"), "download_host": urllib.parse.urlparse(f["downloadUrl"]).hostname,
                             "gameVersions": f.get("gameVersions")},
                    "headers_of_the_final_hop": last.get("headers"), "how_listed": how, "entry_count": len(names),
                    "top_level_names": top, "entries": [{"name": i.filename, "size": i.file_size} for i in infos[:300]],
                    "manifest_json_at_root": "manifest.json" in names, "manifest": manifest_summary}, max_list=300, max_str=300)
            except Exception as e:
                self.fail("modpack zip listing", "%s: %s" % (type(e).__name__, e))

    # =========================================================================================================
    # OPTIFINE
    # =========================================================================================================
    def run_optifine(self):
        self.section("OPTIFINE  (official site only: https://optifine.net/downloads; slow and polite on purpose)")
        o = self.opt = {"wait": self.args.optifine_wait}
        hdr = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.8"}
        r = self.send("GET", OPT_URL, headers=hdr, pause=OPT_PAUSE, body_limit=HTML_CAP, label="optifine downloads page")
        ua_used = self.ua
        scan = analyse_downloads(r.text(), OPT_URL) if r.status == 200 else None
        if r.status != 200 or not scan or not scan["download_links"]:
            self.say("  first try gave %s (%s rows with a link). Trying once with a browser-like User-Agent to see the difference."
                     % (r.status if r.status else r.error, scan["download_links"] if scan else 0))
            hdr2 = dict(hdr, **{"User-Agent": BROWSER_UA})
            r2 = self.send("GET", OPT_URL, headers=hdr2, pause=OPT_PAUSE, body_limit=HTML_CAP, label="optifine downloads page (browser UA)")
            s2 = analyse_downloads(r2.text(), OPT_URL) if r2.status == 200 else None
            o["jarvis_ua_status"] = r.status
            if s2 and s2["download_links"]:
                r, scan, ua_used = r2, s2, BROWSER_UA
                o["needed_browser_ua"] = True
        o["ua_used"] = "Jarvis UA" if ua_used == self.ua else "browser-like UA"
        if r.status != 200 or not scan:
            self.fail("optifine downloads page", r.error or "HTTP %s" % r.status)
            o["stopped_because"] = "the downloads page could not be fetched"
            return
        hdr["User-Agent"] = ua_used
        html = r.text()
        self.fixture_text("optifine_downloads.html", html, resp=r, note="truncated at %d bytes" % HTML_CAP if r.truncated else None)
        rows = scan["rows"]
        self.say("downloads page: HTTP %s, %s%s, user agent used: %s" % (r.status, human_bytes(len(r.body)), " (TRUNCATED at the cap)" if r.truncated else "", o["ua_used"]))
        self.say("  rows found by the %s parser: %d  (by kind: %s) in %d Minecraft sections" % (scan["parsed_by"], len(rows), scan["kinds"], len(scan["distinct_mc_versions"])))
        stable_mcs = sorted({x["mc"] for x in rows if x["mc"] and not x["preview"]}, key=vkey, reverse=True)
        self.say("  newest Minecraft versions with a non-preview build: %s" % stable_mcs[:6])
        self.say("  Download button: %d rows go through a wrapper host %s (NEVER contacted by this probe); for %d rows its `url` parameter is the "
                 "same optifine.net link as the (Mirror) link" % (sum(scan["wrapper_hosts"].values()), scan["wrapper_hosts"] or "-", scan["download_same_as_mirror"]))
        self.say("  (Mirror) links that point straight at optifine.net: %d" % scan["mirror_links"])
        for x in rows[:2]:
            self.say("  sample row: %s %s | %s | %s | %s | mirror=%s" % (x["mc"], x["kind"], x["label"], x["date"], x["forge"], x["mirror"]))
            self.say("      raw: %s" % clip(" ".join(x["raw_tag"].split()), 360))
        for label, wins in scan["markers"].items():
            self.say("  markup around %r: %s" % (label, clip(" ".join(wins[0].split()), 330) if wins else "NOT in the raw HTML (script-built, or another wording)"))
        self.say("  the 'show all / more / preview' controls are javascript: links that reveal hidden blocks of THIS page: %s"
                 % ("yes" if all(str(t.get("href") or "").startswith("javascript") for t in scan["toggles"]) and scan["toggles"] else "NO, see the toggle anchors in the report"))
        o["rows"], o["markers_found"] = len(rows), {k: bool(v) for k, v in scan["markers"].items()}
        o["wrapper_hosts"] = scan["wrapper_hosts"]
        self.fixture("optifine_rows.json", {"page_url": OPT_URL, "parsed_by": scan["parsed_by"], "kinds": scan["kinds"],
                                            "rows": [{k: v for k, v in x.items() if k not in ("raw_tag", "download_href_raw")} for x in rows]},
                     max_list=400, max_str=300)
        # the toggles are real links on some other layout: fetch what they point at (not needed for the layout seen in the first run)
        extra = []
        for t in scan["toggles"]:
            href = t.get("href") or ""
            if href and not href.startswith(("#", "javascript")):
                u = urllib.parse.urljoin(OPT_URL, href)
                if host_ok(urllib.parse.urlparse(u).hostname, OPT_ALLOW) and u not in extra and u != OPT_URL:
                    extra.append(u)
        for i, u in enumerate(extra[:3], 1):
            rx = self.send("GET", u, headers=hdr, pause=OPT_PAUSE, body_limit=HTML_CAP, label="optifine extra page %d" % i)
            if rx.status == 200:
                self.fixture_text("optifine_downloads_more_%d.html" % i, rx.text(), resp=rx)

        row, why = pick_row(rows, self.args.optifine_mc)
        if not row:
            self.say("  cannot pick a version to follow: %s" % why)
            o["stopped_because"] = why
            return
        link = row["mirror"] or row["download"]
        o["link_source"] = "(Mirror) link" if row["mirror"] else "Download link, unwrapped"
        self.say("")
        self.say("-- following ONE older version: %s  (%s) --" % (row["file"], why))
        self.say("  starting from its %s: %s" % (o["link_source"], link))
        if row["download"] and row["mirror"] and row["download"] != row["mirror"]:
            self.say("  NOTE: the Download button's inner link differs from the (Mirror) link: %s" % row["download"])
        o["file"] = row["file"]

        def tail(info, hops):
            last = hops[-1] if hops else {}
            return last, bool(info.get("final_url") and last.get("zip_signature"))

        hopsA, infoA = self.follow_chain(link, "optifine chain", OPT_ALLOW, headers=hdr, referer=OPT_URL, max_hops=5,
                                         pause=OPT_PAUSE, peek=16, countdown_wait=True)
        self.describe_hops(hopsA)
        results = {"link_followed": link, "link_source": o["link_source"], "chain": hopsA, "chain_info": dict(infoA),
                   "download_button": {"raw_href": row.get("download_href_raw"), "wrapper_host": row.get("wrapper_host"),
                                       "inner_link": row.get("download"), "same_as_mirror": row["download"] == row["mirror"]}}
        final = infoA.get("final_url")
        last, reached = tail(infoA, hopsA)
        o["reached_file"] = reached
        if not reached:
            o["stopped_because"] = infoA.get("stopped_because") or "the chain ended without a jar (see the hops)"
            self.say("  RESULT: the chain did NOT reach a jar: %s" % o["stopped_because"])
        else:
            self.say("  RESULT: the chain reached a file: %s, content-type %s, length %s, filename %s, ZIP signature %s"
                     % (clip(final, 120), last.get("content_type"), last.get("content_length"), last.get("filename"), "yes" if last.get("zip_signature") else "NO"))
            self.say("  how the final link is formed: %s" % clip(json.dumps(link_shape(final)), 400))
            results["final_link_shape"] = link_shape(final)
            o["expected_name_matches"] = (last.get("filename") or "") == row["file"]
        if hopsA and hopsA[0].get("scheme") == "http":
            u = "https://" + link.split("://", 1)[1]
            rs = self.send("GET", u, headers=hdr, pause=OPT_PAUSE, body_limit=400000, peek=16, label="optifine first hop over https")
            results["https_first_hop"] = {"url": u, "status": rs.status, "error": rs.error, "content_type": rs.header("content-type"), "location": rs.location()}
            self.say("  the site's links are plain http; the same first hop over https -> %s %s" % (rs.status or rs.error, rs.header("content-type") or ""))
            o["https_works"] = bool(rs.status and rs.status < 400)
        if reached:
            h_a = dict(hdr, **({"Referer": infoA["final_referer"]} if infoA.get("final_referer") else {}))
            rr = self.send("GET", final, headers=h_a, pause=OPT_PAUSE, body_limit=400000, peek=16, label="optifine final link again (straight away)")
            ok = bool(rr.status and rr.status < 300 and rr.body[:4] == b"PK\x03\x04")
            o["reuse_2s"] = ok
            results["reuse_straight_away"] = {"status": rr.status, "content_type": rr.header("content-type"), "zip_signature": rr.body[:4] == b"PK\x03\x04", "location": rr.location()}
            self.say("  the SAME final link a second time, straight away -> HTTP %s, type %s, ZIP signature %s  => %s" % (
                rr.status, rr.header("content-type"), "yes" if ok else "no", "re-usable" if ok else "SINGLE-USE (or bound to something else)"))

            n_pre = len(hopsA) - 1
            if n_pre >= 1:
                # does the final request need the Referer header?  A fresh, never-used link, fetched at once without it.
                self.say("  fresh chain for the Referer test (stopped one hop before the file) ...")
                hopsB, infoB = self.follow_chain(link, "optifine referer chain", OPT_ALLOW, headers=hdr, referer=OPT_URL, max_hops=n_pre,
                                                 pause=OPT_PAUSE, peek=16, countdown_wait=True)
                results["referer_test_chain"] = hopsB
                pb = infoB.get("pending_url")
                if pb:
                    rb = self.send("GET", pb, headers=hdr, pause=OPT_PAUSE, body_limit=400000, peek=16, label="optifine fresh final link WITHOUT Referer")
                    okb = bool(rb.status and rb.status < 300 and rb.body[:4] == b"PK\x03\x04")
                    o["final_without_referer"] = okb
                    results["fresh_without_referer"] = {"status": rb.status, "content_type": rb.header("content-type"), "zip_signature": rb.body[:4] == b"PK\x03\x04", "location": rb.location()}
                    self.say("  a fresh final link fetched WITHOUT a Referer header -> HTTP %s, ZIP %s  (%s)" % (
                        rb.status, "yes" if okb else "no", "a Referer is not needed" if okb else "a Referer (or the earlier cookies) seems to be needed"))
                # expiry: one more fresh link, left unused, and the already-used link, both after the wait
                self.say("  preparing a fresh, UNUSED final link for the expiry test ...")
                hopsC, infoC = self.follow_chain(link, "optifine expiry chain", OPT_ALLOW, headers=hdr, referer=OPT_URL, max_hops=n_pre,
                                                 pause=OPT_PAUSE, peek=16, countdown_wait=True)
                results["expiry_test_chain"] = hopsC
                pending = infoC.get("pending_url")
                wait = max(0, int(self.args.optifine_wait))
                if pending and pending != final:
                    self.say("  waiting %d seconds (Ctrl+C skips the rest of the OptiFine tests) ..." % wait)
                    t_end = time.monotonic() + wait
                    try:
                        while time.monotonic() < t_end:
                            time.sleep(min(15, max(0.0, t_end - time.monotonic())))
                            left = int(t_end - time.monotonic())
                            if left > 0:
                                print("      %d s left" % left)
                                sys.stdout.flush()
                    except KeyboardInterrupt:
                        self.say("  wait skipped by Ctrl+C")
                    ro = self.send("GET", final, headers=h_a, pause=OPT_PAUSE, body_limit=400000, peek=16, label="optifine used link after the wait")
                    h_new = dict(hdr, **({"Referer": infoC["pending_referer"]} if infoC.get("pending_referer") else {}))
                    rn = self.send("GET", pending, headers=h_new, pause=OPT_PAUSE, body_limit=400000, peek=16, label="optifine unused link after the wait")
                    old_ok = bool(ro.status and ro.status < 300 and ro.body[:4] == b"PK\x03\x04")
                    new_ok = bool(rn.status and rn.status < 300 and rn.body[:4] == b"PK\x03\x04")
                    o["old_after_wait"], o["fresh_after_wait"] = old_ok, new_ok
                    results["after_wait"] = {"seconds": wait, "used_link": {"status": ro.status, "zip": old_ok, "location": ro.location()},
                                             "unused_link": {"url": pending, "status": rn.status, "zip": new_ok, "location": rn.location(),
                                                             "content_type": rn.header("content-type")}}
                    self.say("  after %ds: the link already used once -> HTTP %s (%s); the link never used before -> HTTP %s (%s)" % (
                        wait, ro.status, "ZIP" if old_ok else "no file", rn.status, "ZIP" if new_ok else "no file"))
                else:
                    self.say("  could not prepare an unused link; the expiry test was skipped")
            else:
                self.say("  the chain is a single request, so there is no earlier hop to stop at; the Referer and expiry tests were skipped")
        self.fixture("optifine_hops.json", {"page_url": OPT_URL, "chosen_row": {k: v for k, v in row.items() if k not in ("raw_tag",)},
                                            "chosen_because": why, "user_agent": o["ua_used"], "results": results}, max_list=40, max_str=130000)
        route, rwhy = decide_optifine_route(o)
        o["route"], o["route_why"] = route, rwhy
        self.say("")
        self.say("OPTIFINE ROUTE (D6): %s -- %s" % (route, rwhy))

    # =========================================================================================================
    # the plan, the banner, the summary
    # =========================================================================================================
    def selected_sections(self):
        a = self.args
        out = ["modrinth"]
        if not a.no_loaders:
            out.append("loaders")
        if not a.no_curseforge:
            out.append("curseforge")
        if not a.no_optifine:
            out.append("optifine")
        return out

    def planned_total(self):
        secs = self.selected_sections()
        total = 0
        for sec, n, _ in PLAN:
            if sec not in secs:
                continue
            if sec == "curseforge" and not self.key:
                continue
            total += n
        return total

    def print_plan(self):
        print("PLAN: nothing is sent in --list mode.\n")
        secs = self.selected_sections()
        for sec, n, text in PLAN:
            note = ""
            if sec not in secs:
                note = "   [skipped: --no-%s]" % sec
            elif sec == "curseforge" and not self.key:
                note = "   [skipped: no CurseForge key found]"
            print("  %-10s up to %d  %s%s" % (sec, n, text, note))
        print("\nUp to %d requests in total (hard ceiling %d), about %.1f seconds apart (%.1f s on optifine.net)." % (
            self.planned_total(), MAX_REQUESTS, DEFAULT_PAUSE, OPT_PAUSE))
        print("CurseForge key: %s" % ("found in %s (it is sent only to api.curseforge.com)" % self.key_source if self.key else
                                      "none found (%s): the CurseForge part would be skipped" % self.key_reason))

    def banner(self):
        a = self.args
        self.say("Minecraft mod sources: phase 0 probe  (%s, Python %s, %s)" % (self.started.strftime("%Y-%m-%d %H:%M"), platform.python_version(), platform.system()))
        self.say("User-Agent: %s" % self.ua)
        self.say("")
        self.say("This script only READS.  It will contact:")
        self.say("  api.modrinth.com                      Modrinth API, no key")
        if not a.no_loaders:
            self.say("  meta.fabricmc.net, meta.quiltmc.org   Fabric and Quilt loader versions")
            self.say("  files.minecraftforge.net, maven.minecraftforge.net   Forge versions")
            self.say("  maven.neoforged.net                   NeoForge versions")
        if not a.no_curseforge:
            if self.key:
                self.say("  api.curseforge.com                    CurseForge API (key found in %s; sent ONLY here)" % self.key_source)
                self.say("  *.forgecdn.net                        one file's first byte, and one small modpack zip's file list (no key sent)")
            else:
                self.say("  (CurseForge: NO KEY -> that section is SKIPPED.  %s)" % self.key_reason)
        if not a.no_optifine:
            self.say("  optifine.net                          the official download pages; ONE older version, one file, ~%d s of waiting" % a.optifine_wait)
        self.say("")
        self.say("Up to %d requests (hard ceiling %d), pausing between them.  It writes %s and the files in tests/data/modhosts/."
                 % (self.planned_total(), MAX_REQUESTS, REPORT_NAME))
        self.say("It keeps no OptiFine jar and no modpack.")

    def write_summary(self):
        f, o, cf = self.facts, self.opt, self.facts.get("cf", {})
        self.say("")
        self.say("#" * 78)
        self.say("SUMMARY  (one page for the other agents; everything here is what the probe SAW, not what the plan assumed)")
        self.say("#" * 78)
        self.say("Run: %s, build %s, Minecraft version tested: %s (%s)" % (self.started.strftime("%Y-%m-%d %H:%M"), self.build, self.mc, self.mc_source))
        self.say("Requests sent: %d of a ceiling of %d.  Failures: %d (listed at the end)." % (self.n, MAX_REQUESTS, len(self.failures)))
        self.say("")
        self.say("1. OPTIFINE ROUTE (D6)")
        if "optifine" in self.selected_sections() and o:
            self.say("   Route: %s" % o.get("route", "none"))
            self.say("   Why: %s" % o.get("route_why", o.get("stopped_because", "the OptiFine section did not get far enough; see the report")))
            self.say("   User-Agent that worked: %s; plain-http links work over https: %s; final link needs a Referer: %s"
                     % (o.get("ua_used", "?"), o.get("https_works", "not tested"),
                        {True: "no", False: "maybe", None: "not tested"}[o.get("final_without_referer")]))
            self.say("   Rows on the page: %s; link followed: %s; the Download button's wrapper host(s) (not contacted): %s"
                     % (o.get("rows", "?"), o.get("link_source", "?"), o.get("wrapper_hosts") or "none"))
            self.say("   the 'show all / more / preview' markers in the raw HTML: %s" % o.get("markers_found", "?"))
        else:
            self.say("   NOT DETERMINED: the OptiFine section did not run (%s)." % ("--no-optifine" if self.args.no_optifine else "see failures"))
        self.say("")
        self.say("2. CURSEFORGE CLASS IDS (game 432)")
        if cf.get("classes"):
            self.say("   %s" % ", ".join("%s=%s" % (k, v) for k, v in cf["classes"].items()))
            self.say("   mods=%s, modpacks=%s" % (cf.get("class_mods"), cf.get("class_modpacks")))
        else:
            self.say("   NOT DETERMINED (%s)" % ("no key" if not self.key else "the classes call failed"))
        self.say("")
        self.say("3. CURSEFORGE MODLOADER NUMBERS")
        if self.key and cf:
            self.say("   from the live search answers (loader name seen in the file's gameVersions): %s" % (dict(cf.get("loader_numbers_from_search") or {}) or "none"))
            self.say("   from /minecraft/modloader (name prefix -> type): %s" % (cf.get("loaders_from_endpoint") or "none"))
            for name in ("ModLoaderType",):
                if name in (cf.get("enums") or {}):
                    self.say("   from the swagger enum %s: %s %s" % (name, cf["enums"][name]["values"], cf["enums"][name].get("names") or clip(cf["enums"][name].get("description"), 200) or ""))
            self.say("   number used for the Fabric tests: %s; modLoaderType + gameVersion filters honoured: search=%s, files=%s"
                     % (cf.get("fabric_id_used"), cf.get("filters_honoured", "?"), cf.get("file_filters_honoured", "?")))
            self.say("   file hash algo numbers seen: %s; swagger enums about hashes: %s" % (
                cf.get("hash_algos_seen", "?"), {k: v["values"] for k, v in (cf.get("enums") or {}).items() if re.search(r"hash|algo", k, re.I)} or "none"))
            self.say("   how `gameVersions` mixes things: %s" % cf.get("gameversions_kinds", "?"))
        else:
            self.say("   NOT DETERMINED (%s)" % ("no key" if not self.key else "CurseForge section failed"))
        self.say("")
        self.say("4. CURSEFORGE DISTRIBUTION-BLOCKED FLAG")
        if self.key and cf:
            self.say("   swagger properties that look like it: %s" % (cf.get("distribution_properties") or "none"))
            self.say("   flag seen false on a real project: %s; example: %s; its files with a null downloadUrl: %s"
                     % (cf.get("blocked_flag") or "NOT FOUND in the scan", cf.get("blocked_example", "-"), cf.get("blocked_null_urls", "-")))
        else:
            self.say("   NOT DETERMINED (%s)" % ("no key" if not self.key else "CurseForge section failed"))
        self.say("")
        self.say("5. DOWNLOAD HOSTS SEEN (CurseForge CDN and Modrinth files)")
        hosts = sorted(h for h in f["cdn_hosts"] if h)
        self.say("   %s" % (", ".join(hosts) or "none seen"))
        self.say("   CDN answers byte-range requests (modpack zip listing without a full download): %s" % cf.get("cdn_range_ok", "not tested"))
        self.say("   modpack zip has manifest.json at its root: %s" % cf.get("modpack_manifest_at_root", "not tested"))
        self.say("")
        self.say("6. MODRINTH QUERY PARAMETER NAMES")
        if "ms_facets_honoured" in f or "ms_hit_keys" in f:
            self.say("   GET /search            : query, facets (JSON array of arrays: project_type / categories / versions), limit, index -> filters honoured: %s" % f.get("ms_facets_honoured", "?"))
            self.say("   GET /project/{}/version: loaders (JSON array), game_versions (JSON array) -> filters honoured: %s" % f.get("ms_version_filters_honoured", "?"))
            self.say("   GET /versions, /projects: ids (JSON array)")
            self.say("   POST /version_files, /version_files/update: JSON body with hashes, algorithm (+ loaders, game_versions for update)")
            self.say("   not exercised: offset, include_changelog, featured")
            self.say("   project_type / loader values seen for the other kinds: %s" % f.get("ms_kinds", "?"))
            self.say("   do Fabric mods appear in a Quilt search: %s" % f.get("ms_quilt_has_fabric_pick", "?"))
            self.say("   rate-limit headers: %s" % (f.get("ratelimit_last") or "none seen"))
            self.say("   generic User-Agent gets: %s" % (str(f["ms_generic_ua"]) if f.get("ms_generic_ua") else "not tested"))
        else:
            self.say("   NOT DETERMINED (the Modrinth section failed)")
        self.say("")
        self.say("7. LOADER METADATA")
        lm = f.get("loader_meta")
        if lm:
            for name, info in lm.items():
                self.say("   %-9s %s" % (name, info.get("url")))
                self.say("             shape: %s" % clip(json.dumps(info.get("shape")), 220))
                self.say("             latest stable for %s: %s   rule: %s" % (info.get("mc_used", self.mc),
                         info.get("latest_stable") or info.get("chosen") or "NOT RESOLVED", info.get("rule", info.get("error", "-"))))
                if info.get("maven_fallback"):
                    self.say("             fallback: %s (%d versions)" % (info["maven_fallback"]["url"], info["maven_fallback"]["versions"]))
                if info.get("legacy_artifact"):
                    self.say("             legacy 1.20.1 artifact: %s" % info["legacy_artifact"])
                if info.get("unknown_version_answer"):
                    self.say("             an unknown Minecraft version answers: %s" % info["unknown_version_answer"])
            if cf.get("loaders_from_endpoint"):
                self.say("   cross-check, CurseForge /minecraft/modloader prefixes seen: %s" % sorted(cf["loaders_from_endpoint"]))
            if f.get("loader_coverage"):
                self.say("   Forge / NeoForge per recent Minecraft release (see the coverage table above for the rule used):")
                for mcv, row in f["loader_coverage"].items():
                    fg, nf = row.get("forge") or {}, row.get("neoforge") or {}
                    self.say("     %-8s forge rec=%s latest=%s | neoforge %s (%s matches)" % (mcv, fg.get("recommended"), fg.get("latest"), nf.get("newest_stable") or nf.get("newest_any"), nf.get("matches")))
        else:
            self.say("   NOT DETERMINED (%s)" % ("--no-loaders" if self.args.no_loaders else "the loader section failed"))
        self.say("")
        self.say("8. HOSTS TO ADD TO THE ALLOW-LISTS (seen in this run, not in the plan's list)")
        extra = sorted(h for h in (f["hosts_seen"] | f["cdn_hosts"]) if h and h not in PLANNED_HOSTS)
        self.say("   %s" % (", ".join(extra) or "none"))
        self.say("   (the loader sites are new: net.py needs them for the loader resolver; the OptiFine hops above show whether optifine.net alone is enough)")
        self.say("")
        self.say("9. WHAT DID NOT RUN OR FAILED")
        if not self.key and not self.args.no_curseforge:
            self.say("   - CurseForge was skipped: no key (%s)." % self.key_reason)
        for label, reason in self.failures:
            self.say("   - %s: %s" % (label, reason))
        if not self.failures and (self.key or self.args.no_curseforge):
            self.say("   nothing failed")

    def finish(self):
        self.say("")
        self.say("-" * 78)
        self.say("REQUEST LOG (%d requests)" % self.n)
        self.say("-" * 78)
        for line in self.reqlog:
            self.say(line, console=False)
        self.write_summary()
        # the index of what each fixture was
        self._write("probe_meta.json", json.dumps({"written": self.started.isoformat(timespec="seconds"), "user_agent": self.ua,
                                                   "minecraft_version_tested": self.mc, "fixtures": self.meta}, indent=1, ensure_ascii=False))
        report = Path.cwd() / self.args.report
        try:
            report.write_text(self.redact("\n".join(self.lines)) + "\n", encoding="utf-8", newline="\n")
            self.written.append(report)
        except Exception as e:
            print("could not write the report: %s" % e)
        # the key must not be in anything we wrote
        leaked = []
        if self.key:
            needles = [self.key.encode("utf-8"), urllib.parse.quote(self.key, safe="").encode("utf-8")]
            for p in self.written:
                try:
                    blob = Path(p).read_bytes()
                except Exception:
                    continue
                if any(n and n in blob for n in needles):
                    leaked.append(p)
        if leaked:
            for p in leaked:
                try:
                    Path(p).unlink()
                except Exception:
                    pass
            print("\n" + "!" * 78)
            print("!! THE CURSEFORGE KEY WAS FOUND IN: %s" % ", ".join(str(p) for p in leaked))
            print("!! Those files were DELETED.  Do not send anything from this run; tell the owner/agent that the probe leaks the key.")
            print("!" * 78)
            return 3
        print("")
        print("Wrote %s and %d files in %s" % (report, len(self.written) - 1, FIXTURES))
        print("Checked every file written: the CurseForge key is not in any of them." if self.key else "No CurseForge key was used.")
        print("")
        print("SEND BACK:  1) %s   2) the whole folder tests/data/modhosts/" % report.name)
        return 0


def build_parser():
    ap = argparse.ArgumentParser(
        description="Phase 0 probe for the Minecraft mod tools: asks Modrinth, CurseForge, OptiFine and the loader sites what they "
                    "really answer, and records the answers as test fixtures.  Read-only.",
        epilog="Run from the repo root.  --list prints the planned requests and sends nothing.")
    ap.add_argument("--list", action="store_true", help="print the planned requests and exit without sending anything")
    ap.add_argument("--no-curseforge", action="store_true", help="skip the CurseForge section (it is also skipped when no key is set)")
    ap.add_argument("--no-optifine", action="store_true", help="skip the OptiFine section")
    ap.add_argument("--no-loaders", action="store_true", help="skip the loader-metadata section")
    ap.add_argument("--mc-version", default=None, help="Minecraft version for the tests (default: the newest release Modrinth knows)")
    ap.add_argument("--optifine-mc", default=None, help="Minecraft version whose OptiFine links are followed (default: an older, popular one)")
    ap.add_argument("--optifine-wait", type=int, default=120, help="seconds to wait before re-using the OptiFine links (default 120)")
    ap.add_argument("--cf-scan-pages", type=int, default=6, help="pages of 50 CurseForge mods to scan for a distribution-blocked project (default 6)")
    ap.add_argument("--cf-blocked-id", type=int, default=None, help="skip the scan: a CurseForge project id you know forbids third-party distribution")
    ap.add_argument("--cf-pack-mb", type=float, default=4.0, help="a modpack file up to this size is read whole into memory, a bigger one with byte ranges (default 4)")
    ap.add_argument("--report", default=REPORT_NAME, help="report file name, written in the current folder (default %s)" % REPORT_NAME)
    ap.add_argument("--no-pause", action="store_true", help="no start delay and no final 'press Enter' (for scripts)")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    probe = Probe(args)
    if args.list:
        probe.print_plan()
        return 0
    probe.banner()
    if not args.no_pause:
        try:
            print("\nStarting in 5 seconds.  Ctrl+C now cancels.")
            time.sleep(5)
        except KeyboardInterrupt:
            print("cancelled")
            return 1
    steps = [("modrinth", probe.run_modrinth)]
    if not args.no_loaders:
        steps.append(("loaders", probe.run_loaders))
    if probe.key and not args.no_curseforge:
        steps.append(("curseforge", probe.run_curseforge))
    if not args.no_optifine:
        steps.append(("optifine", probe.run_optifine))
    try:
        for name, fn in steps:
            try:
                fn()
            except KeyboardInterrupt:
                raise
            except Exception as e:      # one broken section must never lose the others
                probe.fail("%s section crashed" % name, "%s: %s" % (type(e).__name__, e))
                probe.say(probe.redact(traceback.format_exc()), console=False)
    except KeyboardInterrupt:
        probe.say("\nInterrupted: the report below holds what was collected so far.")
    code = probe.finish()
    if not args.no_pause and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
