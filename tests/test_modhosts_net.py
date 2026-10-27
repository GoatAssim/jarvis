"""jarvis/modhosts/net.py, hosts.py and cache.py: the guarded client every Minecraft mod source goes through.

Covers:
  * the four allow-list blocks (exact hosts, 12+ lines apart), no host read from config or env, https only,
    redirects to other hosts refused, the netguard check on every hop;
  * the CurseForge key: added ONLY for vendor curseforge on api.curseforge.com, dropped on a redirect to the CDN,
    never taken from the caller, `no_key` when config.py or the key is missing, redacted from every error and body;
  * size caps, head / bytes_prefix, max_redirects=0 hop-by-hop, 429 handling (one short retry, never a loop),
    the status table, per-vendor timeouts, the User-Agent;
  * cache.py: time to live, size caps, atomic write, corrupt file, never holds the key.

No network and no real `requests`: a scripted transport stands in (the same seam style as netguard's resolver).
Run: python tests/test_modhosts_net.py
"""
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-modhosts-net-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_CHANNEL_SENDER", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.modhosts import cache, hosts, net  # noqa: E402

PASS, FAIL = [], []
FAKE_KEY = "FAKE-KEY-0123456789abcdef"          # obviously fake: no real key is ever used in a test
CONFIG_NAME = "jarvis.modhosts.config"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


# ------------------------------------------------------------------ helpers

class Script:
    """A transport that answers from a list (the last answer repeats) and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, json_body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "json": json_body,
                           "timeout": timeout})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def ok_json(obj, status=200, headers=None):
    h = {"content-type": "application/json"}
    h.update(headers or {})
    return net.Reply(status, h, json.dumps(obj))


class Env:
    """Installs a transport / resolver / sleep recorder / contact, restores everything afterwards."""

    def __init__(self, script, contact="github.com/GoatAssim", resolver=None):
        self.script, self.contact, self.sleeps = script, contact, []
        self.resolver = resolver or (lambda host, port: ["93.184.216.34"])

    def __enter__(self):
        self.saved = (net.TRANSPORT, net.RESOLVER, net.SLEEP, net.CONTACT_PROVIDER)
        net.TRANSPORT = self.script
        net.RESOLVER = self.resolver
        net.SLEEP = self.sleeps.append
        net.CONTACT_PROVIDER = (lambda: self.contact)
        return self

    def __exit__(self, *exc):
        net.TRANSPORT, net.RESOLVER, net.SLEEP, net.CONTACT_PROVIDER = self.saved


class Config:
    """Stands in for jarvis.modhosts.config: a key (or None), an exception, or a missing module (mode='missing')."""

    def __init__(self, key=FAKE_KEY, mode="key"):
        self.key, self.mode = key, mode

    def __enter__(self):
        self.had = CONFIG_NAME in sys.modules
        self.old = sys.modules.get(CONFIG_NAME)
        if self.mode == "missing":
            sys.modules[CONFIG_NAME] = None                      # an import of it now fails
        else:
            module = types.ModuleType(CONFIG_NAME)
            if self.mode == "raises":
                def boom():
                    raise RuntimeError("cannot read %s" % FAKE_KEY)
                module.get_key = boom
            else:
                module.get_key = lambda: self.key
            sys.modules[CONFIG_NAME] = module
        return self

    def __exit__(self, *exc):
        if self.had:
            sys.modules[CONFIG_NAME] = self.old
        else:
            sys.modules.pop(CONFIG_NAME, None)


MR = "https://api.modrinth.com/v2/x"
CF = "https://api.curseforge.com/v1/mods/1"


# ------------------------------------------------------------------ hosts.py

def test_the_four_allow_list_blocks():
    check("modrinth hosts", hosts.MODRINTH_HOSTS == ("api.modrinth.com", "cdn.modrinth.com"), hosts.MODRINTH_HOSTS)
    check("curseforge hosts: only the two seen by the probe",
          hosts.CURSEFORGE_HOSTS == ("api.curseforge.com", "edge.forgecdn.net"), hosts.CURSEFORGE_HOSTS)
    check("optifine hosts: optifine.net only", hosts.OPTIFINE_HOSTS == ("optifine.net",), hosts.OPTIFINE_HOSTS)
    check("loader meta hosts", hosts.LOADER_META_HOSTS == (
        "meta.fabricmc.net", "meta.quiltmc.org", "files.minecraftforge.net", "maven.minecraftforge.net",
        "maven.neoforged.net"), hosts.LOADER_META_HOSTS)
    every = set(hosts.MODRINTH_HOSTS + hosts.CURSEFORGE_HOSTS + hosts.OPTIFINE_HOSTS + hosts.LOADER_META_HOSTS)
    check("no GitHub / GitLab host, no ad wrapper, no guessed CDN",
          not any(("github" in h or "gitlab" in h or "adfoc" in h or "mediafilecdn" in h) for h in every), every)
    check("net.allowed_hosts reads the blocks", net.allowed_hosts("optifine") == frozenset({"optifine.net"}))
    check("an unknown vendor has no hosts", net.allowed_hosts("nope") == frozenset())


def test_blocks_are_twelve_lines_apart():
    lines = Path(hosts.__file__).read_text(encoding="utf-8").split("\n")
    closes = [i for i, line in enumerate(lines) if line.startswith(")")]
    starts = [i for i, line in enumerate(lines) if line.startswith("# ----")]
    check("four blocks", len(closes) == 4 and len(starts) == 4, (closes, starts))
    gaps = [starts[k + 1] - closes[k] - 1 for k in range(3)]
    check("12 or more lines between one block's end and the next block's start", all(g >= 12 for g in gaps), gaps)


# ------------------------------------------------------------------ allow-list, https, netguard

def test_nothing_reads_hosts_from_config_or_env():
    text = Path(net.__file__).read_text(encoding="utf-8")
    check("net.py never touches os.environ / getenv", "environ" not in text and "getenv" not in text)
    os.environ["MODRINTH_API_BASE"] = "https://evil.example"
    os.environ["CURSEFORGE_BASE_URL"] = "https://evil.example"
    try:
        script = Script(ok_json({}))
        with Env(script):
            res = net.fetch("modrinth", "https://evil.example/v2/x")
        check("a host named in the environment is still refused", res["ok"] is False and res["reason"] == "unsupported", res)
        check("...and nothing was sent", script.calls == [])
    finally:
        os.environ.pop("MODRINTH_API_BASE", None)
        os.environ.pop("CURSEFORGE_BASE_URL", None)


def test_refusals_send_nothing():
    cases = [
        ("unknown vendor", dict(vendor="somewhere", url=MR)),
        ("a host that is not on the list", dict(vendor="modrinth", url="https://evil.example/x")),
        ("another vendor's host", dict(vendor="modrinth", url="https://api.curseforge.com/v1/x")),
        ("the OptiFine ad wrapper", dict(vendor="optifine", url="https://adfoc.us/serve/sitelinks/?url=https://optifine.net")),
        ("plain http", dict(vendor="modrinth", url="http://api.modrinth.com/v2/x")),
        ("a custom port", dict(vendor="modrinth", url="https://api.modrinth.com:8443/v2/x")),
        ("credentials in the URL", dict(vendor="modrinth", url="https://user:pw@api.modrinth.com/v2/x")),
        ("a method other than GET / POST", dict(vendor="modrinth", url=MR, method="DELETE")),
        ("an unknown answer type", dict(vendor="modrinth", url=MR, want="stream")),
    ]
    for label, kw in cases:
        script = Script(ok_json({}))
        with Env(script):
            res = net.fetch(kw.pop("vendor"), kw.pop("url"), **kw)
        check("refused: " + label, res["ok"] is False and res["reason"] == "unsupported" and not script.calls, res)


def test_netguard_runs_on_the_first_request_and_every_hop():
    script = Script(ok_json({}))
    with Env(script, resolver=lambda host, port: ["10.0.0.5"]):
        res = net.fetch("modrinth", MR)
    check("an allowed host that resolves to a private address is refused", res["ok"] is False and not script.calls, res)
    seen = []

    def resolver(host, port):
        seen.append(host)
        return ["10.0.0.5"] if host == "cdn.modrinth.com" else ["93.184.216.34"]

    script = Script(net.Reply(302, {"Location": "https://cdn.modrinth.com/a"}), ok_json({}))
    with Env(script, resolver=resolver):
        res = net.fetch("modrinth", MR)
    check("the redirect target is checked too", res["ok"] is False and len(script.calls) == 1 and "cdn.modrinth.com" in seen, res)


# ------------------------------------------------------------------ redirects

def test_redirects_are_followed_by_hand_to_allowed_hosts_only():
    script = Script(net.Reply(302, {"Location": "https://cdn.modrinth.com/a"}), ok_json({"a": 1}))
    with Env(script):
        res = net.fetch("modrinth", MR)
    check("a redirect to an allowed host is followed", res["ok"] and res["body"] == {"a": 1}
          and res["final_url"] == "https://cdn.modrinth.com/a" and len(script.calls) == 2, res)

    script = Script(net.Reply(302, {"Location": "https://evil.example/a"}), ok_json({}))
    with Env(script):
        res = net.fetch("modrinth", MR)
    check("a redirect to another host is refused", res["ok"] is False and res["reason"] == "unsupported"
          and len(script.calls) == 1, res)

    script = Script(net.Reply(301, {"Location": "http://cdn.modrinth.com/a"}), ok_json({}))
    with Env(script):
        res = net.fetch("modrinth", MR)
    check("a redirect to plain http on an allowed host is upgraded to https",
          res["ok"] and script.calls[1]["url"].startswith("https://cdn.modrinth.com/"), script.calls)

    script = Script(net.Reply(302, {"Location": "/v2/y"}), ok_json({}))
    with Env(script):
        res = net.fetch("modrinth", MR)
    check("a relative Location stays on the same host", res["ok"] and script.calls[1]["url"] == "https://api.modrinth.com/v2/y",
          script.calls)

    script = Script(net.Reply(302, {"Location": "https://api.modrinth.com/v2/x"}))
    with Env(script):
        res = net.fetch("modrinth", MR, max_redirects=3)
    check("a redirect loop stops after max_redirects", res["ok"] is False and res["reason"] == "vendor_changed"
          and len(script.calls) == 4, (res, len(script.calls)))

    script = Script(net.Reply(302, {"Location": "https://optifine.net/downloadx?f=a"}))
    with Env(script):
        res = net.fetch("optifine", "https://optifine.net/adloadx?f=a", max_redirects=0)
    check("max_redirects=0 hands back the 3xx with its Location", res["ok"] and res["status"] == 302
          and res["headers"]["location"] == "https://optifine.net/downloadx?f=a" and len(script.calls) == 1, res)

    script = Script(net.Reply(307, {"Location": "https://api.modrinth.com/v2/y"}))
    with Env(script):
        res = net.fetch("modrinth", "https://api.modrinth.com/v2/version_files", method="POST", json_body={"hashes": []})
    check("a POST is never replayed through a redirect", res["ok"] is False and len(script.calls) == 1, res)


# ------------------------------------------------------------------ the CurseForge key

def test_the_key_goes_only_to_api_curseforge_com():
    with Config():
        script = Script(ok_json({"data": 1}))
        with Env(script):
            res = net.fetch("curseforge", CF)
        check("sent to api.curseforge.com", script.calls[0]["headers"].get("x-api-key") == FAKE_KEY, script.calls)
        check("...and never returned", FAKE_KEY not in json.dumps(res, default=str), res)

        script = Script(net.Reply(200, {"content-type": "application/java-archive"}, b"PK\x03\x04"))
        with Env(script):
            res = net.fetch("curseforge", "https://edge.forgecdn.net/files/1/2/a.jar", want="bytes")
        check("not sent to the CDN host", res["ok"] and "x-api-key" not in script.calls[0]["headers"], script.calls)

        script = Script(ok_json({}))
        with Env(script):
            net.fetch("modrinth", MR)
        check("not sent to Modrinth, even with a key configured", "x-api-key" not in script.calls[0]["headers"], script.calls)

        script = Script(ok_json({}))
        with Env(script):
            net.fetch("curseforge", CF, headers={"X-Api-Key": "caller-key", "Authorization": "Bearer x", "Cookie": "a=b"})
        sent = {k.lower(): v for k, v in script.calls[0]["headers"].items()}
        check("a caller cannot set the key, authorization or cookie", sent.get("x-api-key") == FAKE_KEY
              and "authorization" not in sent and "cookie" not in sent, sent)

        script = Script(net.Reply(302, {"Location": "https://edge.forgecdn.net/files/9/9/a.jar"}), net.Reply(200, {}, b"PK"))
        with Env(script):
            res = net.fetch("curseforge", CF, want="bytes")
        check("a redirect from the API to the CDN drops the key", res["ok"] and "x-api-key" in script.calls[0]["headers"]
              and "x-api-key" not in script.calls[1]["headers"], script.calls)


def test_no_key_means_no_key_and_nothing_is_sent():
    for label, cfg in (("config.py missing", Config(mode="missing")), ("no key set", Config(key=None)),
                       ("an empty key", Config(key="   ")), ("config raising", Config(mode="raises"))):
        script = Script(ok_json({}))
        with cfg, Env(script):
            res = net.fetch("curseforge", CF)
        check("no_key when " + label, res["ok"] is False and res["reason"] == "no_key" and not script.calls, res)
        check("...with the how-to sentence and no key in it", "personal" in res["error"].lower() and FAKE_KEY not in res["error"], res)
    script = Script(ok_json({"ok": 1}))
    with Config(mode="missing"), Env(script):
        res = net.fetch("modrinth", MR)
    check("Modrinth does not need config.py", res["ok"] is True, res)


def test_the_key_is_redacted_from_everything_returned():
    with Config():
        cases = {
            "an exception carrying the key and the URL": RuntimeError("boom %s %s" % (FAKE_KEY, CF)),
            "an HTTP 500 whose body echoes the key": net.Reply(500, {}, json.dumps({"you_sent": FAKE_KEY})),
            "an HTTP 403 whose body echoes the key": net.Reply(403, {}, "denied for " + FAKE_KEY),
            "an HTTP 418": net.Reply(418, {}, "I am " + FAKE_KEY),
        }
        for label, reply in cases.items():
            with Env(Script(reply)):
                res = net.fetch("curseforge", CF)
            text = json.dumps(res, default=str)
            check("no key in the answer to " + label, res["ok"] is False and FAKE_KEY not in text, text)
            check("no URL in it either", "api.curseforge.com/v1" not in text, text)


# ------------------------------------------------------------------ sizes, head, prefix

def test_size_caps_and_the_other_answer_types():
    with Env(Script(net.Reply(200, {}, b"x" * 3000))):
        res = net.fetch("modrinth", MR, want="bytes", max_bytes=1000)
    check("a body over max_bytes is too_big", res["ok"] is False and res["reason"] == "too_big", res)

    endless = net.Reply(200, {}, chunks=iter(lambda: b"x" * 1000, None))
    with Env(Script(endless)):
        res = net.fetch("modrinth", MR, want="bytes", max_bytes=5000)
    check("an endless body stops at the cap instead of hanging", res["ok"] is False and res["reason"] == "too_big", res)

    with Env(Script(net.Reply(200, {"content-type": "application/java-archive"}, b"PK\x03\x04" + b"z" * 5000))):
        res = net.fetch("optifine", "https://optifine.net/downloadx?f=a", want="bytes_prefix", max_bytes=4)
    check("bytes_prefix returns only the first bytes", res["ok"] and res["body"] == b"PK\x03\x04", res)

    with Env(Script(net.Reply(200, {"content-length": "123"}, b"body that must not be read"))):
        res = net.fetch("modrinth", MR, want="head")
    check("head returns the headers only", res["ok"] and res["body"] is None and res["headers"]["content-length"] == "123", res)

    check("default caps are generous defensive limits, not the owner's",
          net.DEFAULT_CAPS["json"] >= 8 * 1024 * 1024 and net.DEFAULT_CAPS["text"] >= 4 * 1024 * 1024)

    html = net.Reply(200, {"content-type": "text/html; charset=ISO-8859-1"}, "caf\u00e9".encode("latin-1"))
    with Env(Script(html)):
        res = net.fetch("optifine", "https://optifine.net/downloads", want="text")
    check("text honours the charset (OptiFine's page is Latin-1)", res["ok"] and res["body"] == "caf\u00e9", res)

    with Env(Script(net.Reply(200, {}, b"<html>not json</html>"))):
        res = net.fetch("modrinth", MR)
    check("a 200 that is not JSON is vendor_changed", res["ok"] is False and res["reason"] == "vendor_changed", res)
    with Env(Script(net.Reply(200, {}, b""))):
        res = net.fetch("modrinth", MR)
    check("an empty 200 is ok with no body", res["ok"] and res["body"] is None, res)


# ------------------------------------------------------------------ rate limits and statuses

def test_rate_limit_handling():
    script = Script(net.Reply(429, {"retry-after": "2"}), ok_json({"a": 1}))
    with Env(script) as env:
        res = net.fetch("modrinth", MR)
    check("one short Retry-After is honoured once", res["ok"] and env.sleeps == [2.0] and len(script.calls) == 2, (res, env.sleeps))

    script = Script(net.Reply(429, {"retry-after": "2"}))
    with Env(script) as env:
        res = net.fetch("modrinth", MR)
    check("a second 429 is rate_limited, not another retry", res["ok"] is False and res["reason"] == "rate_limited"
          and len(script.calls) == 2 and "2 seconds" in res["error"], (res, len(script.calls)))

    script = Script(net.Reply(429, {"retry-after": "120"}))
    with Env(script) as env:
        res = net.fetch("modrinth", MR)
    check("a long wait is reported with the wait stated, not slept", res["reason"] == "rate_limited" and env.sleeps == []
          and len(script.calls) == 1 and "120" in res["error"], res)


def test_status_table():
    table = [
        ("modrinth", 404, "not_found"), ("modrinth", 401, "bad_key"), ("curseforge", 401, "bad_key"),
        ("curseforge", 403, "bad_key"), ("modrinth", 403, "vendor_changed"), ("modrinth", 503, "offline"),
        ("modrinth", 418, "vendor_changed"), ("loader_meta", 400, "vendor_changed"),
    ]
    urls = {"modrinth": MR, "curseforge": CF, "loader_meta": "https://meta.fabricmc.net/v2/versions/loader/99.99"}
    for vendor, status, reason in table:
        with Config(), Env(Script(net.Reply(status, {}, b""))):
            res = net.fetch(vendor, urls[vendor])
        check("%s HTTP %d -> %s" % (vendor, status, reason), res["ok"] is False and res["reason"] == reason
              and res["status"] == status, res)
    with Env(Script(net.Reply(400, {"content-type": "application/json"}, b"[]"))):
        res = net.fetch("loader_meta", urls["loader_meta"])
    check("an error body is passed on for the loader code to read (Fabric: 400 and [])", res.get("body") == [] and res["status"] == 400, res)
    with Env(Script(net.Reply(404, {"content-type": "application/json"}, json.dumps({"error": "unknown"})))):
        res = net.fetch("loader_meta", "https://meta.quiltmc.org/v3/versions/loader/99.99")
    check("Quilt: 404 and a JSON body", res["reason"] == "not_found" and res.get("body") == {"error": "unknown"}, res)


def test_network_failures_say_the_kind_and_never_the_url():
    class ReadTimeout(Exception):
        pass

    with Env(Script(OSError("connection refused for https://api.modrinth.com/v2/x?secret=1"))):
        res = net.fetch("modrinth", MR)
    check("an unreachable site is offline", res["ok"] is False and res["reason"] == "offline", res)
    check("the URL and query never appear", "api.modrinth.com/v2" not in res["error"] and "secret" not in res["error"], res)
    with Env(Script(ReadTimeout("read timed out"))):
        res = net.fetch("modrinth", MR)
    check("a timeout is offline and says it took too long", res["reason"] == "offline" and "too long" in res["error"], res)
    with Env(Script(object())):
        res = net.fetch("modrinth", MR)
    check("a broken transport answer never raises", res["ok"] is False and "error" in res and "reason" in res, res)


# ------------------------------------------------------------------ request shape

def test_user_agent_timeouts_params_and_post():
    script = Script(ok_json({}))
    with Env(script):
        net.fetch("modrinth", MR)
    sent = script.calls[0]
    check("User-Agent is Jarvis/<build> (<contact>)", re.match(r"^Jarvis/[0-9A-Za-z._-]+ \(github\.com/GoatAssim\)$",
                                                               sent["headers"]["User-Agent"]), sent["headers"])
    check("Accept asks for JSON", sent["headers"]["Accept"] == "application/json")
    with Env(Script(ok_json({})), contact="me@example.com") as env:
        net.fetch("modrinth", MR)
        check("the contact setting is used", "(me@example.com)" in env.script.calls[0]["headers"]["User-Agent"])
    with Env(Script(ok_json({})), contact="bad\r\nX-Evil: 1") as env:
        net.fetch("modrinth", MR)
        check("an unsafe contact falls back to the default",
              env.script.calls[0]["headers"]["User-Agent"].endswith("(github.com/GoatAssim)"), env.script.calls[0]["headers"])

    expect = {"modrinth": 30, "curseforge": 30, "optifine": 60, "loader_meta": 90}
    urls = {"modrinth": MR, "curseforge": CF, "optifine": "https://optifine.net/downloads",
            "loader_meta": "https://meta.fabricmc.net/v2/versions/loader/1.21"}
    for vendor, seconds in expect.items():
        script = Script(ok_json({}))
        with Config(), Env(script):
            net.fetch(vendor, urls[vendor])
        check("default timeout of %s is %d s" % (vendor, seconds), script.calls[0]["timeout"] == seconds, script.calls[0]["timeout"])
    script = Script(ok_json({}))
    with Env(script):
        net.fetch("modrinth", MR, timeout=5)
    check("a timeout can be given per call", script.calls[0]["timeout"] == 5)

    script = Script(ok_json({}))
    with Env(script):
        net.fetch("modrinth", "https://api.modrinth.com/v2/search",
                  params={"query": "a b", "facets": '[["x"]]', "limit": 5, "skip": None})
    url = script.calls[0]["url"]
    check("params are percent-encoded, None ones left out", "query=a%20b" in url and "limit=5" in url
          and "%5B%5B%22x%22%5D%5D" in url and "skip" not in url, url)

    script = Script(ok_json({}))
    with Env(script):
        net.fetch("modrinth", "https://api.modrinth.com/v2/version_files", method="POST",
                  json_body={"hashes": ["a"], "algorithm": "sha1"})
    sent = script.calls[0]
    check("POST carries its JSON body and content type", sent["method"] == "POST" and sent["json"] == {"hashes": ["a"], "algorithm": "sha1"}
          and sent["headers"]["Content-Type"] == "application/json", sent)


# ------------------------------------------------------------------ cache.py

def _cache_file():
    return Path(HOME) / ".jarvis" / "modhosts" / "cache.json"


def test_cache_roundtrip_ttl_and_location():
    cache.clear()
    check("a missing key is None", cache.get("nope") is None)
    check("put stores", cache.put("k1", {"a": [1, 2]}, 600) is True)
    check("get returns it", cache.get("k1") == {"a": [1, 2]})
    check("it lives under ~/.jarvis/modhosts/cache.json", _cache_file().exists(), _cache_file())
    check("no temp file is left behind", not list(_cache_file().parent.glob("*.tmp")))
    real = cache._now
    try:
        cache._now = lambda: real() + 601
        check("an entry past its time to live is gone", cache.get("k1") is None)
    finally:
        cache._now = real
    check("a zero ttl is not stored", cache.put("k2", 1, 0) is False and cache.get("k2") is None)
    check("None is not stored", cache.put("k3", None, 60) is False)
    check("a huge key is refused", cache.put("k" * 1000, 1, 60) is False)


def test_cache_size_caps_and_corruption():
    cache.clear()
    check("one oversized value is refused", cache.put("big", "x" * (cache.MAX_ENTRY_BYTES + 10), 60) is False)
    old = cache.MAX_ENTRIES
    try:
        cache.MAX_ENTRIES = 5
        for i in range(9):
            cache.put("e%d" % i, i, 600 + i)
        entries = json.loads(_cache_file().read_text(encoding="utf-8"))["entries"]
        check("the entry count is capped", len(entries) <= 5, len(entries))
        check("the newest entry survives", cache.get("e8") == 8)
    finally:
        cache.MAX_ENTRIES = old
    old_total = cache.MAX_FILE_BYTES
    try:
        cache.clear()
        cache.MAX_FILE_BYTES = 3000
        for i in range(8):
            cache.put("s%d" % i, "y" * 800, 600 + i)
        check("the file size is capped", _cache_file().stat().st_size <= 3000 + 900, _cache_file().stat().st_size)
        check("the newest survives the size cap", cache.get("s7") == "y" * 800)
    finally:
        cache.MAX_FILE_BYTES = old_total
    _cache_file().write_text("{ not json", encoding="utf-8")
    check("a corrupt file reads as a miss", cache.get("s7") is None)
    check("...and put repairs it", cache.put("again", 1, 60) is True and cache.get("again") == 1)


def test_cache_never_holds_the_key():
    cache.clear()
    with Config():
        check("a value containing the key is refused", cache.put("leak", {"x": "a " + FAKE_KEY}, 60) is False)
        check("an ordinary value is fine with a key configured", cache.put("fine", {"x": 1}, 60) is True)
    check("the key is not in the file", FAKE_KEY not in _cache_file().read_text(encoding="utf-8"))


# ------------------------------------------------------------------ a 403 says where it came from (M3)

CDN = "https://edge.forgecdn.net/files/1/2/a.jar"


def test_a_403_from_the_download_host_is_not_a_key_problem():
    check("cdn_refused is its own code, none of the existing ones",
          net.CDN_REFUSED == "cdn_refused" and net.CDN_REFUSED not in ("bad_key", "not_distributable", "vendor_changed", "not_found"))
    with Config():
        script = Script(net.Reply(403, {"content-type": "text/html"}, b"<html>Forbidden</html>"))
        with Env(script):
            res = net.fetch("curseforge", CDN, want="bytes")
        check("a 403 from edge.forgecdn.net is cdn_refused, with its status",
              res["ok"] is False and res["reason"] == "cdn_refused" and res["status"] == 403, res)
        check("...and its sentence does not blame the key",
              "may be wrong" not in res["error"] and "not a key problem" in res["error"], res["error"])
        check("...no key was sent to that host, and none is in the answer",
              "x-api-key" not in script.calls[0]["headers"] and FAKE_KEY not in json.dumps(res, default=str), script.calls)

        script = Script(net.Reply(403, {}, b""))
        with Env(script):
            res = net.fetch("curseforge", CF)
        check("a 403 from api.curseforge.com stays bad_key",
              res["ok"] is False and res["reason"] == "bad_key" and res["status"] == 403 and "key may be wrong" in res["error"], res)

        script = Script(net.Reply(302, {"Location": CDN}), net.Reply(403, {}, b""))
        with Env(script):
            res = net.fetch("curseforge", CF, want="bytes")
        check("a 403 after a redirect from the API to the CDN is cdn_refused (the host that answered decides)",
              res["ok"] is False and res["reason"] == "cdn_refused" and len(script.calls) == 2, res)
        check("...the key went to the API request only",
              "x-api-key" in script.calls[0]["headers"] and "x-api-key" not in script.calls[1]["headers"], script.calls)

        for status, reason in ((404, "not_found"), (500, "offline"), (418, "vendor_changed")):
            with Env(Script(net.Reply(status, {}, b""))):
                res = net.fetch("curseforge", CDN, want="bytes")
            check("the download host's HTTP %d is still %s" % (status, reason), res["reason"] == reason and res["status"] == status, res)

    with Env(Script(net.Reply(403, {}, b""))):
        res = net.fetch("modrinth", "https://cdn.modrinth.com/data/x/a.jar", want="bytes")
    check("another vendor's 403 is unchanged (vendor_changed)", res["reason"] == "vendor_changed" and res["status"] == 403, res)


# --- runner (keep BELOW every test: it reads globals() when it executes) ----

if __name__ == "__main__":
    for name in sorted(n for n in list(globals()) if n.startswith("test_")):
        print(f"\n== {name}")
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append((name, f"raised {exc!r}"))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for item in FAIL:
        print("  FAILED:", item)
    sys.exit(1 if FAIL else 0)
