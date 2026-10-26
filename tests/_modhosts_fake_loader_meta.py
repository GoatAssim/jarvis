"""Fake loader-metadata sites for the Minecraft loader-version tests (jarvis/modhosts/loaders.py).

Same pattern as tests/_modhosts_fake_servers.py: not a socket server (net.py is https-only with fixed hosts), but a
TRANSPORT (net.TRANSPORT) plus a RESOLVER, so every request still goes through the real net.py rules (host
allow-list, netguard, User-Agent, size caps, redirects); only the bytes are made up, from
tests/data/modhosts/hand_loader_*.json:

    hand_loader_fabric.json       {"by_mc": {<mc>: [entries]}}        meta.fabricmc.net/v2/versions/loader/<mc>
    hand_loader_quilt.json        {"by_mc": {<mc>: [entries]}}        meta.quiltmc.org/v3/versions/loader/<mc>
    hand_loader_forge.json        {"promos": {...}}                   files.minecraftforge.net .../promotions_slim.json
    hand_loader_forge_maven.json  {"versions": [...]}                 maven.minecraftforge.net .../maven-metadata.xml
    hand_loader_neoforge.json     {"isSnapshot", "versions": [...]}   maven.neoforged.net .../net/neoforged/neoforge

What it answers like the real sites (the owner's probe, 25 Oct 2026): Fabric answers an unknown Minecraft version
with HTTP 400 and the body `[]`; Quilt with HTTP 404 and a JSON body whose `code` is "not_found"; the NeoForge list
is served in the fixture's own (unsorted) order.  The legacy NeoForge artifact (net.neoforged:forge) is served too,
so a test can show it is never asked for.

`calls` records every request (method, host, path, query, headers, timeout).  `overrides[host]` replaces what one
host answers: a net.Reply, an Exception to raise, or a callable(method, url, headers) returning either; use it for
failures and hostile bodies.  `sequence[host]` is a list of such replacements used one per request.

Use:
    fake = FakeLoaderMeta(); fake.install()      # sets net.TRANSPORT / RESOLVER / CONTACT_PROVIDER / SLEEP
    ... call jarvis.modhosts.loaders ...
    fake.uninstall()
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

DATA = Path(__file__).resolve().parent / "data" / "modhosts"

FABRIC_HOST = "meta.fabricmc.net"
QUILT_HOST = "meta.quiltmc.org"
FORGE_HOST = "files.minecraftforge.net"
FORGE_MAVEN_HOST = "maven.minecraftforge.net"
NEOFORGE_HOST = "maven.neoforged.net"
ALL_HOSTS = (FABRIC_HOST, QUILT_HOST, FORGE_HOST, FORGE_MAVEN_HOST, NEOFORGE_HOST)

NEOFORGE_PATH = "/api/maven/versions/releases/net/neoforged/neoforge"
NEOFORGE_LEGACY_PATH = "/api/maven/versions/releases/net/neoforged/forge"


def load(name):
    return json.loads((DATA / ("hand_loader_%s.json" % name)).read_text(encoding="utf-8"))


def maven_xml(versions):
    rows = "\n".join("      <version>%s</version>" % v for v in versions)
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<metadata>\n  <groupId>net.minecraftforge</groupId>\n"
            "  <artifactId>forge</artifactId>\n  <versioning>\n    <latest>%s</latest>\n    <release>%s</release>\n"
            "    <versions>\n%s\n    </versions>\n  </versioning>\n</metadata>\n"
            % (versions[-1] if versions else "", versions[-1] if versions else "", rows))


class FakeLoaderMeta:
    def __init__(self):
        self.fabric = load("fabric")
        self.quilt = load("quilt")
        self.forge = load("forge")
        self.forge_maven = load("forge_maven")
        self.neoforge = load("neoforge")
        self.calls = []
        self.overrides = {}          # host -> Reply | Exception | callable(method, url, headers)
        self.sequence = {}           # host -> [Reply | Exception | callable], one used per request

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

    # ---------------------------------------------------------------- helpers for tests
    def hosts_called(self):
        return [c["host"] for c in self.calls]

    def paths_called(self):
        return [c["path"] for c in self.calls]

    def reset(self):
        self.calls.clear()
        self.overrides.clear()
        self.sequence.clear()

    # ---------------------------------------------------------------- the transport
    def transport(self, method, url, headers, json_body, timeout):
        from jarvis.modhosts import net
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        self.calls.append({"method": method, "host": parts.hostname, "path": parts.path, "query": query,
                           "headers": dict(headers or {}), "timeout": timeout, "url": url})
        host = parts.hostname
        queued = self.sequence.get(host)
        if queued:
            return self._deliver(queued.pop(0), method, url, headers)
        if host in self.overrides:
            return self._deliver(self.overrides[host], method, url, headers)
        return self._route(net, host, unquote(parts.path))

    @staticmethod
    def _deliver(reply, method, url, headers):
        if callable(reply):                       # a net.Reply or an exception instance is not callable
            reply = reply(method, url, headers)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    # ---------------------------------------------------------------- routes
    def _json(self, net, status, body, headers=None):
        h = {"content-type": "application/json"}
        h.update(headers or {})
        return net.Reply(status, h, json.dumps(body))

    def _route(self, net, host, path):
        if host == FABRIC_HOST:
            prefix = "/v2/versions/loader/"
            if path.startswith(prefix):
                mc = path[len(prefix):]
                if mc in self.fabric["by_mc"]:
                    return self._json(net, 200, self.fabric["by_mc"][mc])
                return self._json(net, 400, [])                       # what the real site says for a Minecraft
            return net.Reply(404, {}, b"")                            # version it does not know
        if host == QUILT_HOST:
            prefix = "/v3/versions/loader/"
            if path.startswith(prefix):
                mc = path[len(prefix):]
                if mc in self.quilt["by_mc"]:
                    return self._json(net, 200, self.quilt["by_mc"][mc])
                return self._json(net, 404, {"code": "not_found", "message": "File with such name does not exist.",
                                             "status": 404})
            return net.Reply(404, {}, b"")
        if host == FORGE_HOST:
            if path == "/net/minecraftforge/forge/promotions_slim.json":
                return self._json(net, 200, {"homepage": self.forge["homepage"], "promos": self.forge["promos"]})
            return net.Reply(404, {}, b"")
        if host == FORGE_MAVEN_HOST:
            if path == "/net/minecraftforge/forge/maven-metadata.xml":
                return net.Reply(200, {"content-type": "text/xml"}, maven_xml(self.forge_maven["versions"]))
            return net.Reply(404, {}, b"")
        if host == NEOFORGE_HOST:
            if path == NEOFORGE_PATH:
                return self._json(net, 200, {"isSnapshot": self.neoforge["isSnapshot"],
                                             "versions": self.neoforge["versions"]})
            if path == NEOFORGE_LEGACY_PATH:
                return self._json(net, 200, {"isSnapshot": False, "versions": ["1.20.1-47.1.105", "1.20.1-47.1.106",
                                                                               "47.1.82"]})
            return net.Reply(404, {}, b"")
        return net.Reply(404, {}, b"")
