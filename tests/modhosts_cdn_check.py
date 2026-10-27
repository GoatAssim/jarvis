#!/usr/bin/env python3
"""Minecraft mod sources: does the REAL CurseForge download host and the fingerprint route work?  (owner-run probe, M3)

Two things the Minecraft code was built on are still marked NOT VERIFIED (shared/probe-results.md):

  1. Downloading from the CurseForge download host.  The first probe's two first-byte requests got HTTP 404, so
     nobody has seen a real file come down.
  2. CurseForge's fingerprint route (POST /v1/fingerprints/432).  The cross-platform matcher (crossmatch.py)
     and `curseforge.fingerprint_matches` were written from the documentation as remembered; the swagger the
     first probe saved lists no such route.  The murmur2 code behind it (`crossmatch.cf_fingerprint`) was never
     compared with a real CurseForge file either.

Run this ONCE on your own PC, with your own CurseForge key set the usual way (Settings > Integrations > Minecraft
mods).  A good report turns those facts into verified ones, or says exactly what was seen instead.

    python tests/modhosts_cdn_check.py --list        # print the planned requests, send nothing
    python tests/modhosts_cdn_check.py               # the run: about 12 to 25 requests, one small file
    python tests/modhosts_cdn_check.py --redact      # the same, but nothing that identifies you or the mod is written

It writes `modhosts-cdn-report.txt` in the folder you ran it from (ends with a one-page SUMMARY) and prints the
same lines.  Send the report back.  It needs no other file.

What it does, in order:
  * picks ONE small, public CurseForge mod file whose author allows third-party downloads (a few well-known small
    mods are tried; `--project-id` and `--file-id` name one yourself);
  * downloads it THROUGH `jarvis.modhosts.net.fetch` (never urllib: the same host allow-list, https only, redirect
    rules and User-Agent as the real tools) into a temporary folder that is deleted afterwards;
  * compares the size and the SHA-1 with what the API said, and computes the CurseForge fingerprint
    (`crossmatch.cf_fingerprint`) and compares it with the API's own `fileFingerprint`;
  * calls `curseforge.fingerprint_matches` with that fingerprint and records whether the route answered and the
    SHAPE of the answer (key names and kinds only);
  * records the download host that was used, and what a refusal from that host looks like: one sample request to
    the host's own root address (not a file) unless the real download itself was refused, and one look at a
    project whose author blocks downloads (the API's answer for it, for contrast).  `--no-cdn-sample` and
    `--no-blocked-sample` skip those two.

The key: this script never reads it.  `net.fetch` adds it, for api.curseforge.com only.  The report, the console and
the temporary folder are checked for anything shaped like a CurseForge key before they are written or deleted, and
the request log lists header NAMES only, never values.  `--redact` also hides the mod's name, slug and ids, the file
name, every URL path, the SHA-1 and fingerprint values and the local folders.  Hosts, HTTP statuses, sizes and yes /
no answers always stay: they are what this is for.

Exit code: 0 = everything checked out, 1 = something differed or failed (read the report), 2 = no key or nothing asked.
Not a test (no `test_` prefix, the runner skips it); it is exercised, with a fake transport, by
tests/test_minecraft_hardening.py.
"""
import argparse
import datetime
import hashlib
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.modhosts import config, crossmatch, curseforge, net  # noqa: E402

REPORT_NAME = "modhosts-cdn-report.txt"
CANDIDATE_QUERIES = ("appleskin", "mouse tweaks", "controlling", "jade")     # small, popular, long-lived mods
BLOCKED_SAMPLE_ID = 448233        # "Entity Culling", allowModDistribution false when the first probe ran (25 Oct 2026)
MAX_REQUESTS = 45                 # hard ceiling on requests made through net
MAX_PROJECTS = 6                  # candidate projects looked at before giving up
KEY_SHAPE = re.compile(r"\$2[abxy]\$\d{2}\$[A-Za-z0-9./]{20,}")      # what a CurseForge key looks like (bcrypt style)
KEY_SHAPE_BYTES = re.compile(rb"\$2[abxy]\$\d{2}\$[A-Za-z0-9./]{20,}")
PUBLIC_HEADERS = ("content-type", "content-length", "content-disposition", "accept-ranges", "location", "server",
                  "cache-control", "retry-after", "etag", "last-modified", "age", "via", "x-cache")
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
HIDDEN = "<hidden by --redact>"

PLAN = [
    "GET  api.curseforge.com/v1/mods/search            (one query per candidate mod, up to %d; sorted by downloads)" % MAX_PROJECTS,
    "GET  api.curseforge.com/v1/mods/<id>/files        (the chosen mod's files for one Minecraft version and loader)",
    "GET  <the file's own download link>               (edge.forgecdn.net; the key is NOT sent there; plain GET, no Range header)",
    "POST api.curseforge.com/v1/fingerprints/432       (twice: a fingerprint nobody has, then the downloaded file's)",
    "GET  <the download host's root address>           (ONE sample of what that host answers to a non-file request; --no-cdn-sample skips it)",
    "GET  api.curseforge.com/v1/mods/%d/files + .../download-url   (a project that blocks downloads, for contrast; --no-blocked-sample skips it)" % BLOCKED_SAMPLE_ID,
]


# ------------------------------------------------------------------------------------------------ the report
class Report:
    """Collects lines.  Everything that goes in passes through scrub() (key-shaped text out, one printable line)."""

    def __init__(self, redact, out):
        self.redact, self.out, self.lines, self.warnings = bool(redact), out, [], []
        self.facts = {}                       # name -> (True | False | None, text): the SUMMARY is built from these

    def scrub(self, text, limit=300):
        text = _CTRL.sub(" ", str(text))
        if KEY_SHAPE.search(text):
            self.warnings.append("a text shaped like a CurseForge key was removed from the report")
            text = KEY_SHAPE.sub("<key redacted>", text)
        return text if len(text) <= limit else text[:limit - 1] + "\u2026"

    def private(self, text):
        """A value that identifies the owner or the mod: hidden under --redact."""
        return HIDDEN if self.redact else self.scrub(text)

    def say(self, text="", private=False):
        line = HIDDEN if private and self.redact else self.scrub(text, 400)
        self.lines.append(line)
        try:
            self.out(line)
        except Exception:  # noqa: BLE001 - a console that cannot print must not stop the probe
            pass

    def fact(self, name, ok, text):
        self.facts[name] = (ok, self.scrub(text, 300))
        self.say("  %s %s: %s" % ({True: "[ok]  ", False: "[DIFF]", None: "[info]"}[ok], name, self.facts[name][1]))

    def text(self):
        return "\n".join(self.lines) + "\n"


class Recorder:
    """Wraps net's transport: counts and logs every request (method, host, path, status, header NAMES only)."""

    def __init__(self):
        self.rows, self.inner, self.saved = [], None, None

    def __enter__(self):
        self.saved = net.TRANSPORT
        self.inner = net.TRANSPORT or net._real_transport
        net.TRANSPORT = self
        return self

    def __exit__(self, *exc):
        net.TRANSPORT = self.saved

    def __call__(self, method, url, headers, json_body, timeout):
        parts = urlsplit(url)
        row = {"method": method, "host": parts.hostname or "", "path": parts.path or "/",
               "names": sorted(str(k) for k in headers),
               "key_header": any(str(k).lower() == net.KEY_HEADER for k in headers)}
        if len(self.rows) >= MAX_REQUESTS:
            raise RuntimeError("the request ceiling of this probe was reached")
        self.rows.append(row)
        try:
            reply = self.inner(method, url, headers, json_body, timeout)
        except Exception as exc:  # noqa: BLE001
            row["error"] = type(exc).__name__
            raise
        row["status"] = reply.status_code
        return reply


# ------------------------------------------------------------------------------------------------ small helpers
def describe(value, depth=0):
    """The SHAPE of a JSON value: key names and kinds, list lengths, never a value.  (A key echoed back by a
    vendor could not end up in the report this way.)"""
    if isinstance(value, dict):
        if depth >= 3:
            return "object"
        return "{" + ", ".join("%s: %s" % (re.sub(r"[^A-Za-z0-9_]", "?", str(k))[:40], describe(v, depth + 1))
                               for k, v in list(value.items())[:25]) + "}"
    if isinstance(value, list):
        first = describe(value[0], depth + 1) if value else "-"
        return "list[%d]%s" % (len(value), " of " + first if value else "")
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    return "text"


def host_of(url):
    return (urlsplit(str(url or "")).hostname or "").lower()


def headers_text(headers):
    got = {k: v for k, v in (headers or {}).items() if k in PUBLIC_HEADERS}
    return ", ".join("%s=%s" % (k, str(v)[:80]) for k, v in sorted(got.items())) or "(none of the usual ones)"


def failure_text(res, report):
    """What a net.fetch failure looks like, without the body's words (kind only)."""
    bits = ["reason=%s" % res.get("reason"), "status=%s" % res.get("status", "-")]
    if "body" in res:
        bits.append("body=%s" % describe(res["body"]) if not isinstance(res["body"], str)
                    else "body=text(%d characters)" % len(res["body"]))
    bits.append("sentence: %s" % report.scrub(res.get("error", ""), 200))
    return "; ".join(bits)


# ------------------------------------------------------------------------------------------------ the steps
def choose_file(args, report):
    """-> (project, file) or (None, None).  Only a file that can be downloaded here and is small."""
    cap = int(args.max_kb) * 1024
    if args.project_id and args.file_id:
        got = curseforge.get_file(args.project_id, args.file_id)
        proj = curseforge.get_project(args.project_id)
        if got.get("ok") and proj.get("ok"):
            return proj["project"], got["file"]
        report.say("The file you named could not be read: %s" % report.scrub((got if not got.get("ok") else proj).get("error")))
        return None, None
    seen = 0
    for query in CANDIDATE_QUERIES:
        res = curseforge.search(query, kind="mod", mc_version=args.mc_version, loader=args.loader, sort="downloads", limit=10)
        if not res.get("ok"):
            report.say("Search for the candidate '%s' failed: reason %s: %s" % (query, res.get("reason"), report.scrub(res.get("error"))))
            if res.get("reason") in ("no_key", "bad_key", "rate_limited", "offline"):
                return None, None
            continue
        for hit in res.get("hits") or []:
            if hit.get("distributable") is not True:
                continue
            seen += 1
            if seen > MAX_PROJECTS:
                return None, None
            files = curseforge.list_files(hit["id"], args.mc_version, args.loader)
            for f in (files.get("files") or []) if files.get("ok") else []:
                size = f.get("size")
                if (f.get("downloadable") and f.get("channel") == "release" and isinstance(size, int) and 0 < size <= cap
                        and (f.get("hashes") or {}).get("sha1") and (f.get("hashes") or {}).get("fingerprint")
                        and host_of(f.get("download_url")) not in ("", net.KEY_HOST)):
                    return hit, f
    return None, None


def step_download(args, report, project, file, tmp):
    """Download through net.fetch; returns (bytes or None, dict of what was seen)."""
    url, size = file["download_url"], file["size"]
    seen = {"download_host": host_of(url)}
    started = time.monotonic()
    res = net.fetch("curseforge", url, want="bytes", max_bytes=max(size * 2, 1024 * 1024), timeout=60)
    seen["seconds"] = round(time.monotonic() - started, 2)
    report.say("-- download through net.fetch --")
    report.say("  download link host (from the API): %s" % seen["download_host"])
    if not res.get("ok"):
        seen["failure"] = res
        report.say("  the download FAILED: " + failure_text(res, report))
        report.fact("CDN download", False, "refused or failed (see above)")
        if res.get("status") == 403:
            report.fact("a CDN 403 looks like", None, "from the real download: " + failure_text(res, report))
        return None, seen
    body = res.get("body")
    seen["final_host"] = host_of(res.get("final_url"))
    report.say("  host that sent the file: %s%s" % (seen["final_host"], "" if seen["final_host"] == seen["download_host"] else "  (a redirect happened)"))
    report.say("  HTTP %s in %s s; headers: %s" % (res.get("status"), seen["seconds"], headers_text(res.get("headers"))))
    if not isinstance(body, (bytes, bytearray)) or not body:
        report.fact("CDN download", False, "the host answered but sent no file")
        return None, seen
    path = Path(tmp) / "probe.jar"                      # our own name, never the vendor's
    path.write_bytes(bytes(body))
    data = path.read_bytes()                            # what really landed on disk
    sha1 = hashlib.sha1(data).hexdigest()
    seen.update(size=len(data), sha1=sha1)
    report.fact("CDN download", True, "a plain GET through net.fetch returned the file (host %s, %d bytes)" % (seen["final_host"], len(data)))
    report.fact("size", len(data) == size, "API said %d, the file has %d" % (size, len(data)))
    want_sha1 = (file.get("hashes") or {}).get("sha1")
    same = sha1 == want_sha1
    report.fact("sha1", same, "the file's SHA-1 %s the API's" % ("equals" if same else "DIFFERS from")
                + (" (values hidden)" if report.redact else " (%s / %s)" % (sha1, want_sha1)))
    report.fact("zip shape", data[:4] == b"PK\x03\x04", "the first bytes %s a zip's" % ("are" if data[:4] == b"PK\x03\x04" else "are NOT"))
    seen["data"] = data
    return data, seen


def step_fingerprint(args, report, project, file, data):
    fingerprint = crossmatch.cf_fingerprint(data)
    want = (file.get("hashes") or {}).get("fingerprint")
    report.say("-- fingerprint --")
    report.fact("cf_fingerprint equals the API's fileFingerprint", str(fingerprint) == str(want),
                "computed %s, API %s" % (report.private(fingerprint), report.private(want)) if not report.redact
                else "%s (values hidden)" % ("equal" if str(fingerprint) == str(want) else "DIFFERENT"))
    probe = curseforge.fingerprint_matches([curseforge.FINGERPRINT_PROBE])
    report.fact("fingerprint route answers (a fingerprint nobody has)", bool(probe.get("ok")),
                "ok, no match, as expected" if probe.get("ok") and not probe.get("matches")
                else ("ok, but it returned matches: %d" % len(probe.get("matches") or {})) if probe.get("ok")
                else "reason %s: %s" % (probe.get("reason"), report.scrub(probe.get("error"), 200)))
    got = curseforge.fingerprint_matches([fingerprint])
    if not got.get("ok"):
        report.fact("fingerprint_matches finds the downloaded file", False,
                    "reason %s: %s" % (got.get("reason"), report.scrub(got.get("error"), 200)))
    else:
        hits = (got.get("matches") or {}).get(fingerprint) or []
        same = [h for h in hits if str(h.get("file_id")) == str(file.get("file_id")) and str(h.get("project_id")) == str(file.get("project_id"))]
        report.fact("fingerprint_matches finds the downloaded file", bool(same), "%d match(es) for the file's fingerprint; %s" % (
            len(hits), "one of them is this very file" if same else "none is this file"))
        if same:
            report.fact("...and the match carries the same sha1", (same[0].get("hashes") or {}).get("sha1") == (file.get("hashes") or {}).get("sha1"),
                        "the match's sha1 %s the file's" % ("equals" if (same[0].get("hashes") or {}).get("sha1") == (file.get("hashes") or {}).get("sha1") else "DIFFERS from"))
    # the raw answer's shape, through the same door (net.fetch adds the key for the API host)
    raw = net.fetch("curseforge", curseforge.API_BASE + curseforge.FINGERPRINT_PATH % curseforge.GAME_ID, method="POST",
                    json_body={"fingerprints": [fingerprint]})
    if raw.get("ok"):
        report.fact("route answer SHAPE", None, "HTTP %s, %s" % (raw.get("status"), describe(raw.get("body"))))
    else:
        report.fact("route answer SHAPE", None, "no answer: " + failure_text(raw, report))


def step_cdn_sample(args, report, seen):
    """What a refusal from the download host looks like.  Only when the real download was not itself refused."""
    report.say("-- what a refusal from the download host looks like --")
    if args.no_cdn_sample:
        report.fact("a CDN 403 looks like", None, "skipped (--no-cdn-sample)")
        return
    host = seen.get("final_host") or seen.get("download_host")
    if not host or host not in net.allowed_hosts("curseforge") or host == net.KEY_HOST:
        report.fact("a CDN 403 looks like", None, "no download host to ask")
        return
    res = net.fetch("curseforge", "https://%s/" % host, want="bytes_prefix", max_bytes=512, timeout=30)
    if res.get("ok"):
        report.fact("a CDN 403 looks like", None, "NOT SEEN: the host's root address answered HTTP %s (headers: %s)" % (
            res.get("status"), headers_text(res.get("headers"))))
    elif res.get("status") == 403:
        report.fact("a CDN 403 looks like", res.get("reason") == net.CDN_REFUSED,
                    "SEEN on the host's root address: " + failure_text(res, report) + ("" if res.get("reason") == net.CDN_REFUSED
                                                                                      else "  (net.py did NOT call it cdn_refused)"))
    else:
        report.fact("a CDN 403 looks like", None, "NOT SEEN: the host's root address answered otherwise: " + failure_text(res, report))


def step_blocked_sample(args, report):
    report.say("-- an author-blocked project, for contrast (the API's answer, not the download host's) --")
    if args.no_blocked_sample:
        report.fact("API 403 for a blocked file", None, "skipped (--no-blocked-sample)")
        return
    listing = curseforge.list_files(BLOCKED_SAMPLE_ID)
    files = (listing.get("files") or []) if listing.get("ok") else []
    if not files:
        report.fact("API 403 for a blocked file", None, "the sample project could not be listed (it may have changed): %s" % (
            "ok but empty" if listing.get("ok") else report.scrub(listing.get("error"), 160)))
        return
    fid = files[0]["file_id"]
    raw = net.fetch("curseforge", "%s/mods/%d/files/%s/download-url" % (curseforge.API_BASE, BLOCKED_SAMPLE_ID, fid))
    mapped = curseforge.download_url(BLOCKED_SAMPLE_ID, fid)
    report.fact("API 403 for a blocked file", None, "net.fetch: " + failure_text(raw, report) if not raw.get("ok")
                else "net.fetch: ok (%s): this file is now downloadable" % describe(raw.get("body")))
    report.fact("download_url folds it into not_distributable", mapped.get("reason") == "not_distributable" if not mapped.get("ok") else None,
                "curseforge.download_url answered %s" % ("ok" if mapped.get("ok") else mapped.get("reason")))


# ------------------------------------------------------------------------------------------------ the run
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true", help="print the planned requests and send nothing")
    ap.add_argument("--redact", action="store_true", help="hide names, ids, URL paths, hash values and local folders")
    ap.add_argument("--mc-version", default="1.20.1")
    ap.add_argument("--loader", default="fabric", choices=("fabric", "forge", "neoforge", "quilt"))
    ap.add_argument("--max-kb", default=600, type=int, help="largest file to pick, in KB (default 600)")
    ap.add_argument("--project-id", default=None, help="use this CurseForge project (needs --file-id)")
    ap.add_argument("--file-id", default=None, help="use this CurseForge file (needs --project-id)")
    ap.add_argument("--no-cdn-sample", action="store_true")
    ap.add_argument("--no-blocked-sample", action="store_true")
    ap.add_argument("--out", default=None, help="where to write the report (default: %s in the current folder)" % REPORT_NAME)
    return ap


def run(argv=None, out=print):
    """-> (exit code, report text).  Writes no report file (main() does) and nothing outside its own temp folder."""
    args = build_parser().parse_args(argv)
    report = Report(args.redact, out)
    report.say("Minecraft CurseForge download-host and fingerprint check   %s   redact=%s" % (
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "yes" if args.redact else "no"))
    if args.list:
        report.say("Planned requests (nothing is sent with --list):")
        for line in PLAN:
            report.say("  " + line)
        return 0, report.text()
    if bool(args.project_id) != bool(args.file_id):
        report.say("--project-id and --file-id go together.")
        return 2, report.text()
    view = config.public_view()
    if not view.get("api_key_set"):
        report.say("No usable CurseForge key is set (%s). Nothing was asked. Set it in Settings > Integrations > Minecraft mods and run again." % (
            report.scrub(view.get("problem") or "none found")))
        return 2, report.text()
    report.say("Key: set (where from: %s). It is never shown and never read by this script." % view.get("source"))

    with Recorder() as recorder:
        project, file = choose_file(args, report)
        seen = {}
        if file is None:
            report.say("No small, distributable file could be chosen. Try --mc-version / --loader, or name one with --project-id and --file-id.")
            report.fact("a file was chosen", False, "none")
        else:
            report.say("-- chosen file --")
            report.say("  project: %s (id %s)" % (report.private(project.get("name")), report.private(project.get("id"))))
            report.say("  file: %s (id %s), %s bytes, channel %s" % (report.private(file.get("file_name")), report.private(file.get("file_id")),
                                                                    file.get("size"), file.get("channel")))
            report.say("  link: https://%s/%s" % (host_of(file.get("download_url")), "<path hidden>" if report.redact else urlsplit(file["download_url"]).path.lstrip("/")))
            with tempfile.TemporaryDirectory(prefix="jarvis-cdn-check-") as tmp:
                data, seen = step_download(args, report, project, file, tmp)
                if data is not None:
                    step_fingerprint(args, report, project, file, data)
                leaked = [p.name for p in Path(tmp).iterdir() if p.is_file() and KEY_SHAPE_BYTES.search(p.read_bytes())]
                report.fact("temporary folder holds no key-shaped text", not leaked, "scanned %d file(s), deleted afterwards" % len(list(Path(tmp).iterdir())))
        if (seen.get("failure") or {}).get("status") != 403:       # a real 403 above is the sample already
            step_cdn_sample(args, report, seen)
        step_blocked_sample(args, report)
        rows = list(recorder.rows)

    report.say("-- request log (names of headers only, never values) --")
    for row in rows:
        report.say("  %s %s%s -> %s   headers: %s%s" % (
            row["method"], row["host"], "" if report.redact else row["path"], row.get("status", row.get("error", "no answer")),
            ", ".join(row["names"]), "   [KEY HEADER]" if row["key_header"] else ""))
    wrong = sorted({r["host"] for r in rows if r["key_header"] and r["host"] != net.KEY_HOST})
    report.fact("the key header went to api.curseforge.com only", not wrong, "hosts that got it besides: %s" % (", ".join(wrong) or "none"))

    report.say()
    report.say("SUMMARY")
    report.say("  host used for the download: %s" % (seen.get("final_host") or seen.get("download_host") or "none"))
    bad = [n for n, (ok, _t) in report.facts.items() if ok is False]
    for name, (ok, text) in report.facts.items():
        report.say("  %-60s %s" % (name, {True: "ok", False: "DIFFERENT", None: "recorded"}[ok]))
    report.say("  If every line above says ok or recorded: the real CDN download and the fingerprint route are VERIFIED for this key,")
    report.say("  and crossmatch.cf_fingerprint equals CurseForge's own fingerprint on a real file.")
    report.say("  Lines that say DIFFERENT are the facts to send back; nothing else needs sending.")
    for warning in report.warnings:
        report.say("  note: " + warning)
    return (1 if bad else 0), report.text()


def main(argv=None):
    code, text = run(argv)
    args = build_parser().parse_args(argv)
    if not args.list and code != 2:
        path = Path(args.out) if args.out else Path.cwd() / REPORT_NAME
        path.write_text(text, encoding="utf-8")
        print("\nReport written to %s" % path)
    return code


if __name__ == "__main__":
    sys.exit(main())
