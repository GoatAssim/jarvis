"""A fake Modrinth for the Minecraft mod source tests (the other vendors' fakes are other agents' files).

It is not a socket server: jarvis/modhosts/net.py is https-only with fixed hosts, so a local port could never be
reached.  Instead the fake is a TRANSPORT (net.TRANSPORT) plus a RESOLVER, the same seam style as
netguard.check_url(resolver=...).  The request still goes through every net.py rule (host allow-list, netguard,
User-Agent, size caps); only the bytes are made up, from tests/data/modhosts/hand_*.json:

    hand_1.json  a /search answer          hand_2.json  {project id: /project answer}
    hand_3.json  {project id: [versions]}  hand_4.json  {project id: /members answer}

It honours what the probe verified: /search facets (project_type, categories, versions), /project/{x}/version
`loaders` and `game_versions` filters, /projects?ids, /versions?ids, /version/{id}.  `calls` records every request
(method, path, query dict, headers) so a test can count network calls and look at the headers.

Use:
    fake = FakeModrinth(); fake.install()      # sets net.TRANSPORT / RESOLVER / CONTACT_PROVIDER
    ... call the adapter ...
    fake.uninstall()
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

DATA = Path(__file__).resolve().parent / "data" / "modhosts"


def _load(n):
    return json.loads((DATA / ("hand_%d.json" % n)).read_text(encoding="utf-8"))


class FakeModrinth:
    def __init__(self):
        self.search_doc = _load(1)
        self.projects = _load(2)
        self.versions = _load(3)
        self.members = _load(4)
        self.calls = []
        self.fail_paths = {}        # path substring -> (status, body) to answer instead
        self.extra_hosts = {}       # host -> handler(method, url, headers, body) for non-API hosts

    # ---------------------------------------------------------------- wiring
    def install(self):
        from jarvis.modhosts import net
        self._net = net
        self._saved = (net.TRANSPORT, net.RESOLVER, net.CONTACT_PROVIDER, net.SLEEP)
        net.TRANSPORT = self.transport
        net.RESOLVER = lambda host, port: ["93.184.216.34"]
        net.CONTACT_PROVIDER = lambda: "github.com/GoatAssim"
        net.SLEEP = lambda seconds: None
        return self

    def uninstall(self):
        net = self._net
        net.TRANSPORT, net.RESOLVER, net.CONTACT_PROVIDER, net.SLEEP = self._saved

    # ---------------------------------------------------------------- the transport
    def transport(self, method, url, headers, json_body, timeout):
        from jarvis.modhosts import net
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        self.calls.append({"method": method, "host": parts.hostname, "path": parts.path, "query": query,
                           "headers": dict(headers or {}), "timeout": timeout})
        for needle, (status, body) in self.fail_paths.items():
            if needle in parts.path:
                return net.Reply(status, {"content-type": "application/json"}, json.dumps(body))
        if parts.hostname != "api.modrinth.com":
            return net.Reply(404, {}, b"")
        path = parts.path
        if not path.startswith("/v2/"):
            return net.Reply(404, {}, b"")
        path = path[len("/v2"):]
        status, body = self._route(path, query)
        return net.Reply(status, {"content-type": "application/json", "x-ratelimit-limit": "300"},
                         json.dumps(body))

    # ---------------------------------------------------------------- routes
    def _route(self, path, query):
        if path == "/search":
            return 200, self._search(query)
        if path == "/projects":
            ids = json.loads(query.get("ids") or "[]")
            return 200, [p for p in (self._project(i) for i in ids) if p]
        if path == "/versions":
            ids = set(json.loads(query.get("ids") or "[]"))
            return 200, [v for vs in self.versions.values() for v in vs if v["id"] in ids]
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if parts[0] == "version" and len(parts) == 2:
            for vs in self.versions.values():
                for v in vs:
                    if v["id"] == parts[1]:
                        return 200, v
            return 404, {"error": "not_found"}
        if parts[0] == "project" and len(parts) >= 2:
            project = self._project(parts[1])
            if project is None:
                return 404, {"error": "not_found", "description": "the requested route does not exist"}
            if len(parts) == 2:
                return 200, project
            if parts[2] == "members":
                return 200, self.members.get(project["id"], [])
            if parts[2] == "version":
                loaders = json.loads(query["loaders"]) if "loaders" in query else None
                games = json.loads(query["game_versions"]) if "game_versions" in query else None
                out = []
                for v in self.versions.get(project["id"], []):
                    if loaders is not None and not set(loaders) & set(v["loaders"]):
                        continue
                    if games is not None and not set(games) & set(v["game_versions"]):
                        continue
                    out.append(v)
                out.sort(key=lambda v: v["date_published"], reverse=True)
                return 200, out
        return 404, {"error": "not_found"}

    def _project(self, ref):
        for p in self.projects.values():
            if ref == p["id"] or ref.lower() == p["slug"].lower():
                return p
        return None

    def _search(self, query):
        facets = json.loads(query.get("facets") or "[]")
        text = (query.get("query") or "").lower()
        hits = []
        for hit in self.search_doc["hits"]:
            if text and text not in (hit["title"] + " " + hit["slug"] + " " + hit["description"]).lower():
                continue
            if not all(any(self._facet(hit, f) for f in group) for group in facets):
                continue
            hits.append(hit)
        limit = int(query.get("limit") or 10)
        offset = int(query.get("offset") or 0)
        return {"hits": hits[offset:offset + limit], "offset": offset, "limit": limit, "total_hits": len(hits)}

    @staticmethod
    def _facet(hit, facet):
        key, _, value = facet.partition(":")
        if key == "project_type":
            return hit["project_type"] == value
        if key == "categories":
            return value in hit["categories"]
        if key == "versions":
            return value in hit["versions"]
        return False
