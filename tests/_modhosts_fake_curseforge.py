"""A fake CurseForge for the P3 tests (not a test itself). No network, no sockets.

``FakeCurseForge`` is a ``fetch`` with the signature of ``jarvis.modhosts.net.fetch``::

    fake(vendor, url, *, method="GET", params=None, json_body=None, headers=None,
         want="json", max_bytes=None, max_redirects=3)

so a test hands it to the adapter as ``fetch=fake``. It answers the routes the probe ran, from
tests/data/modhosts/hand_p3_mods.json and hand_p3_files.json (hand-written from the SHAPES the
probe recorded; not a recorded answer). It copies the behaviours the probe found, including the
ones that bite:

* the loader filter is loose: ``modLoaderType`` 4 or 5 returns files tagged Fabric OR Quilt, 1 or 6
  files tagged Forge OR NeoForge (the adapter must re-check each file's own gameVersions);
* the download-url route answers HTTP 403 with an empty body for the project that forbids it;
* a null ``downloadUrl`` on every file of that project;
* the bulk routes answer ``{"data": [...]}``.

Knobs a test can turn: ``style`` (how HTTP errors reach the adapter: "failure" = a failure dict with
``reason`` and ``status``; "status" = ``ok: True`` with the HTTP status and an empty body; "bare" = a
failure dict with only ``reason``/``error``), ``fail`` (a list of HTTP statuses to answer with, one per
call, before behaving), ``ignore_slug`` (the ``slug`` parameter finds nothing, as if the vendor did not honour it), ``leak`` (a text
every error carries, to prove the adapter scrubs a key a careless net might leave in), ``show_all_classes``
(search ignores ``classId``), and ``calls`` (every request, with the arguments it was given).

The fake never sees the CurseForge key: ``net.fetch`` adds the header, and the adapter passes none. A test
asserts ``headers`` is None on every recorded call and that the key text appears nowhere in ``calls``.
"""

import copy
import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "modhosts"
# modLoaderType -> the loader numbers the vendor lets through (the probe: a loose filter)
_LOOSE = {4: {4, 5}, 5: {4, 5}, 1: {1, 6}, 6: {1, 6}}
_LOOSE_NAMES = {4: {"Fabric", "Quilt"}, 5: {"Fabric", "Quilt"}, 1: {"Forge", "NeoForge"}, 6: {"Forge", "NeoForge"}}
_REASON = {401: "bad_key", 403: "bad_key", 404: "not_found", 429: "rate_limited"}


def load():
    mods = json.loads((DATA / "hand_p3_mods.json").read_text(encoding="utf-8"))["mods"]
    files = json.loads((DATA / "hand_p3_files.json").read_text(encoding="utf-8"))["files"]
    return mods, {int(k): v for k, v in files.items()}


class FakeCurseForge:
    def __init__(self, style="failure"):
        mods, files = load()
        self.mods = {m["id"]: m for m in mods}
        self.files = files
        self.style = style
        self.fail = []
        self.ignore_slug = False
        self.leak = None
        self.show_all_classes = False
        self.calls = []
        self.blocked = {m["id"] for m in mods if m.get("allowModDistribution") is False}

    # -- the fetch signature of net.fetch ------------------------------------------------------
    def __call__(self, vendor, url, *, method="GET", params=None, json_body=None, headers=None,
                 want="json", max_bytes=None, max_redirects=3):
        self.calls.append({"vendor": vendor, "url": url, "method": method, "params": dict(params or {}),
                           "json_body": copy.deepcopy(json_body), "headers": headers})
        if vendor != "curseforge" or not url.startswith("https://api.curseforge.com/v1/"):
            return self._http(url, 400)
        if self.fail:
            return self._http(url, self.fail.pop(0))
        path = url[len("https://api.curseforge.com/v1"):]
        p = params or {}
        if method == "GET" and path == "/mods/search":
            return self._ok(url, self._search(p))
        if method == "GET" and path == "/categories":
            return self._ok(url, {"data": [{"id": 6, "slug": "mc-mods", "isClass": True}]})
        if method == "POST" and path == "/mods":
            ids = [int(i) for i in (json_body or {}).get("modIds", [])]
            return self._ok(url, {"data": [copy.deepcopy(self.mods[i]) for i in ids if i in self.mods]})
        if method == "POST" and path == "/mods/files":
            want_ids = {int(i) for i in (json_body or {}).get("fileIds", [])}
            out = [copy.deepcopy(f) for fs in self.files.values() for f in fs if f["id"] in want_ids]
            return self._ok(url, {"data": out})
        m = re.match(r"^/mods/(\d+)$", path)
        if method == "GET" and m:
            mod = self.mods.get(int(m.group(1)))
            return self._ok(url, {"data": copy.deepcopy(mod)}) if mod else self._http(url, 404)
        m = re.match(r"^/mods/(\d+)/files$", path)
        if method == "GET" and m:
            return self._list_files(url, int(m.group(1)), p)
        m = re.match(r"^/mods/(\d+)/files/(\d+)$", path)
        if method == "GET" and m:
            for f in self.files.get(int(m.group(1)), []):
                if f["id"] == int(m.group(2)):
                    return self._ok(url, {"data": copy.deepcopy(f)})
            return self._http(url, 404)
        m = re.match(r"^/mods/(\d+)/files/(\d+)/download-url$", path)
        if method == "GET" and m:
            mod_id = int(m.group(1))
            if mod_id in self.blocked:
                return self._http(url, 403)
            for f in self.files.get(mod_id, []):
                if f["id"] == int(m.group(2)):
                    return self._ok(url, {"data": f["downloadUrl"]})
            return self._http(url, 404)
        return self._http(url, 404)

    # -- answers -------------------------------------------------------------------------------
    def _ok(self, url, body):
        return {"ok": True, "status": 200, "headers": {}, "body": body, "final_url": url}

    def _http(self, url, status):
        text = "HTTP %d from the fake CurseForge" % status
        if self.leak:
            text += " (" + self.leak + ")"
        if self.style == "status":
            return {"ok": True, "status": status, "headers": {}, "body": "", "final_url": url}
        out = {"ok": False, "error": text, "reason": _REASON.get(status, "offline" if status >= 500 else "vendor_changed")}
        if self.style == "failure":
            out["status"] = status
        if status == 429:
            out["retry_after"] = 7
        return out

    def _page(self, rows, p):
        size = int(p.get("pageSize") or 50)
        start = int(p.get("index") or 0)
        return rows[start:start + size], {"index": start, "pageSize": size, "resultCount": len(rows[start:start + size]), "totalCount": len(rows)}

    def _search(self, p):
        rows = list(self.mods.values())
        if p.get("classId") and not self.show_all_classes:
            rows = [m for m in rows if m["classId"] == int(p["classId"])]
        if p.get("slug"):
            rows = [] if self.ignore_slug else [m for m in rows if m["slug"] == p["slug"]]
        if p.get("searchFilter"):
            needle = str(p["searchFilter"]).lower()
            rows = [m for m in rows if needle in (m["name"] + " " + m["slug"]).lower()]
        if p.get("gameVersion"):
            rows = [m for m in rows if any(i.get("gameVersion") == p["gameVersion"] for i in m["latestFilesIndexes"])]
        if p.get("modLoaderType"):
            allowed = _LOOSE.get(int(p["modLoaderType"]), {int(p["modLoaderType"])})
            rows = [m for m in rows if any(i.get("modLoader") in allowed for i in m["latestFilesIndexes"])]
        rows.sort(key=lambda m: (-m["downloadCount"], m["id"]))
        page, pagination = self._page(rows, p)
        return {"data": copy.deepcopy(page), "pagination": pagination}

    def _list_files(self, url, mod_id, p):
        if mod_id not in self.mods:
            return self._http(url, 404)
        rows = list(self.files.get(mod_id, []))
        if p.get("gameVersion"):
            rows = [f for f in rows if p["gameVersion"] in f["gameVersions"]]
        if p.get("modLoaderType"):
            names = _LOOSE_NAMES.get(int(p["modLoaderType"]), set())
            rows = [f for f in rows if names & set(f["gameVersions"])]
        page, pagination = self._page(rows, p)
        return self._ok(url, {"data": copy.deepcopy(page), "pagination": pagination})
