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


# ------------------------------------------------------------------ streaming a download to a .part file (MC-B)
# `to_file=` / `discard=`: the answer is written to a file the caller made as it arrives, the size cap is checked while it
# streams, SHA-1 and SHA-512 are computed over what arrived, and any failure hands the file to the caller's `discard`
# (net.py itself never deletes anything).  Without `to_file` every answer is what it always was.

import hashlib  # noqa: E402

SDL = "https://cdn.modrinth.com/data/AANobbMI/versions/V1/sodium.jar"


class Discards:
    """The caller's `discard`: remembers every call (and how big the file was at that moment) and really removes the
    file, as modhosts/apply.py's own function does."""

    def __init__(self):
        self.calls, self.sizes = [], []

    def __call__(self, path):
        self.calls.append(path)
        try:
            self.sizes.append(os.path.getsize(path))
        except OSError:
            self.sizes.append(None)
        try:
            os.unlink(path)
        except OSError:
            pass


class Feed:
    """A body that arrives in chunks: counts the chunks taken, and may break like a cut connection after the last one."""

    def __init__(self, parts, then=None):
        self.parts, self.then, self.taken = list(parts), then, 0

    def __iter__(self):
        for part in self.parts:
            self.taken += 1
            yield part
        if self.then is not None:
            raise self.then


def new_part(name="download.part"):
    """A fresh empty file the way the caller makes it: exclusively created, then closed."""
    folder = tempfile.mkdtemp(prefix="stream-", dir=HOME)
    path = os.path.join(folder, name)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600))
    return path


def stream(reply, vendor="modrinth", url=SDL, **kw):
    """One streamed fetch against a script (one reply, or a list of them) -> (result, path, discards, script)."""
    path = new_part()
    disc = Discards()
    script = Script(*reply) if isinstance(reply, (list, tuple)) else Script(reply)
    with Env(script):
        res = net.fetch(vendor, url, want="bytes", to_file=path, discard=disc, **kw)
    return res, path, disc, script


def test_stream_writes_the_file_and_hashes_what_arrived():
    data = bytes(range(256)) * 400                                   # 102 400 bytes
    cuts = [0, 1, 700, 4096, 4097, 65536, 90000, len(data)]
    parts = [data[a:b] for a, b in zip(cuts, cuts[1:])]
    parts.insert(2, b"")                                             # an empty chunk is skipped, not a problem
    reply = net.Reply(200, {"Content-Type": "application/java-archive", "Content-Length": str(len(data)),
                            "Set-Cookie": "a=b"}, chunks=Feed(parts))
    res, path, disc, script = stream(reply, max_bytes=len(data))
    check("ok, with the path, the size and both hashes, and no body held in memory",
          res["ok"] is True and res["path"] == path and res["size"] == len(data) and res["body"] is None
          and res["sha1"] == hashlib.sha1(data).hexdigest() and res["sha512"] == hashlib.sha512(data).hexdigest(), res)
    check("status, headers and final_url come back as for any call, and the cookie is dropped",
          res["status"] == 200 and res["final_url"] == SDL and res["headers"]["content-type"] == "application/java-archive"
          and "set-cookie" not in res["headers"], res)
    with open(path, "rb") as handle:
        on_disk = handle.read()
    check("the file holds exactly the bytes that arrived, and nothing was discarded", on_disk == data and disc.calls == [],
          (len(on_disk), disc.calls))
    sent = script.calls[0]
    check("it is a plain GET with the honest agent and no key", sent["method"] == "GET" and sent["headers"]["User-Agent"].startswith("Jarvis/")
          and "x-api-key" not in sent["headers"], sent)


def test_stream_the_cap_is_checked_while_streaming():
    feed = Feed([b"x" * 1000 for _ in range(20)])
    res, path, disc, _ = stream(net.Reply(200, {}, chunks=feed), max_bytes=5000)
    check("too_big, and the stream stopped at the chunk that crossed the cap, not at the end",
          res["ok"] is False and res["reason"] == "too_big" and "limit" in res["error"] and feed.taken == 6, (res, feed.taken))
    check("the file was handed to discard once, after it was closed (5000 bytes were in it), and is gone",
          disc.calls == [path] and disc.sizes == [5000] and not os.path.exists(path), (disc.calls, disc.sizes))

    feed = Feed([b"x" * 1000 for _ in range(5)])
    res, path, disc, _ = stream(net.Reply(200, {}, chunks=feed), max_bytes=5000)
    check("a file of exactly the cap is fine (the same boundary as an in-memory read)",
          res["ok"] is True and res["size"] == 5000 and disc.calls == [] and os.path.getsize(path) == 5000, res)

    feed = Feed([b"x" * 10])
    res, path, disc, _ = stream(net.Reply(200, {"Content-Length": "999999"}, chunks=feed), max_bytes=5000)
    check("a Content-Length over the cap is refused before one byte is read or written",
          res["ok"] is False and res["reason"] == "too_big" and feed.taken == 0 and disc.sizes == [0] and not os.path.exists(path),
          (res, feed.taken, disc.sizes))


def test_stream_short_and_long_reads_are_failures():
    body = b"s" * 6000
    res, path, disc, _ = stream(net.Reply(200, {"Content-Length": "10000"}, chunks=Feed([body[:3000], body[3000:]])), max_bytes=20000)
    check("a body that ends short of its Content-Length is offline, with the counts",
          res["ok"] is False and res["reason"] == "offline" and "6000 of 10000" in res["error"], res)
    check("...and the file is discarded once and gone", disc.calls == [path] and not os.path.exists(path), disc.calls)

    res, path, disc, _ = stream(net.Reply(200, {"Content-Length": "100"}, chunks=Feed([b"l" * 150])), max_bytes=20000)
    check("a body that runs past its Content-Length is vendor_changed and the file is gone",
          res["ok"] is False and res["reason"] == "vendor_changed" and disc.calls == [path] and not os.path.exists(path), res)

    res, path, disc, _ = stream(net.Reply(200, {"Content-Length": "300", "Content-Encoding": "identity"}, chunks=Feed([b"i" * 200])), max_bytes=1000)
    check("an 'identity' encoding is still compared", res["ok"] is False and res["reason"] == "offline" and disc.calls == [path], res)

    res, path, disc, _ = stream(net.Reply(200, {"Content-Length": "50", "Content-Encoding": "gzip"}, chunks=Feed([b"g" * 200])), max_bytes=1000)
    check("a content-encoded answer is not compared with its (encoded) Content-Length",
          res["ok"] is True and res["size"] == 200 and disc.calls == [], res)
    res, path, disc, _ = stream(net.Reply(200, {"Content-Length": "9999999", "Content-Encoding": "gzip"}, chunks=Feed([b"g" * 200])), max_bytes=1000)
    check("...nor refused up front because of it (the cap is still checked chunk by chunk)", res["ok"] is True and res["size"] == 200, res)


def test_stream_the_hashes_are_of_the_bytes_that_arrived():
    published = b"the file the vendor published"
    served = b"some other file entirely, served instead"
    res, path, disc, _ = stream(net.Reply(200, {}, chunks=Feed([served[:10], served[10:]])), max_bytes=1000)
    check("net does not judge a file: it reports what arrived, so the caller sees a mismatch",
          res["ok"] is True and res["sha1"] == hashlib.sha1(served).hexdigest() and res["sha512"] == hashlib.sha512(served).hexdigest()
          and res["sha1"] != hashlib.sha1(published).hexdigest() and res["sha512"] != hashlib.sha512(published).hexdigest(), res)
    with open(path, "rb") as handle:
        check("...and the file on disk is the served bytes", hashlib.sha1(handle.read()).hexdigest() == res["sha1"])
    check("a mismatch is the caller's to act on: net did not discard a whole file", disc.calls == [])


def test_stream_a_dropped_connection_is_offline_and_the_file_goes():
    secret = "https://cdn.modrinth.com/data/secret-path?token=abc123"
    feed = Feed([b"a" * 1000, b"b" * 1000], then=ConnectionError("connection reset " + secret))
    res, path, disc, _ = stream(net.Reply(200, {"Content-Length": "5000"}, chunks=feed), max_bytes=10000)
    check("offline, and it says the kind of failure",
          res["ok"] is False and res["reason"] == "offline" and "ConnectionError" in res["error"] and "dropped" in res["error"], res)
    check("the address and the token from the library's message are not in the answer",
          "secret" not in json.dumps(res) and "token" not in json.dumps(res) and "https://" not in json.dumps(res), res)
    check("the half-written file (2000 bytes) went to discard once, after it was closed, and is gone",
          disc.calls == [path] and disc.sizes == [2000] and not os.path.exists(path), (disc.calls, disc.sizes))

    class ReadTimeout(Exception):
        pass

    res, path, disc, _ = stream(net.Reply(200, {}, chunks=Feed([b"a" * 10], then=ReadTimeout("read timed out"))), max_bytes=1000)
    check("a read timeout mid-stream is offline and says it took too long",
          res["ok"] is False and res["reason"] == "offline" and "took too long" in res["error"] and disc.calls == [path], res)

    res, path, disc, _ = stream(net.Reply(200, {}, chunks=Feed([b"a" * 10], then=TimeoutError("slow"))), max_bytes=1000)
    check("a server that stalls past the overall deadline is offline too, and the file goes",
          res["ok"] is False and res["reason"] == "offline" and "too long" in res["error"] and disc.calls == [path]
          and not os.path.exists(path), res)


def test_stream_every_failure_leaves_no_file():
    cases = [
        ("a 404", [net.Reply(404, {}, b"")], "not_found", {}),
        ("a 500", [net.Reply(500, {}, b"")], "offline", {}),
        ("an odd status", [net.Reply(418, {}, b"")], "vendor_changed", {}),
        ("a 204 is not a file", [net.Reply(204, {}, b"")], "vendor_changed", {}),
        ("a 206 is not the whole file", [net.Reply(206, {"Content-Range": "bytes 0-9/100"}, b"x" * 10)], "vendor_changed", {}),
        ("rate limited twice", [net.Reply(429, {"Retry-After": "1"}, b""), net.Reply(429, {"Retry-After": "1"}, b"")], "rate_limited", {}),
        ("a redirect off the allow-list", [net.Reply(302, {"Location": "https://evil.example.com/a.jar"})], "unsupported", {}),
        ("a redirect loop", [net.Reply(302, {"Location": SDL})], "vendor_changed", {}),
        ("a 3xx handed back (max_redirects=0)", [net.Reply(302, {"Location": SDL})], "vendor_changed", {"max_redirects": 0}),
    ]
    for label, replies, reason, kw in cases:
        res, path, disc, _ = stream(replies, **kw)
        check("%s: failed with %s, the file went to discard once and is gone" % (label, reason),
              res["ok"] is False and res["reason"] == reason and disc.calls == [path] and not os.path.exists(path), (res, disc.calls))

    res, path, disc, script = stream(net.Reply(200, {}, b"x"), url="https://evil.example.com/a.jar")
    check("a host off the allow-list: refused with no request, the file goes",
          res["reason"] == "unsupported" and script.calls == [] and disc.calls == [path], (res, script.calls))
    res, path, disc, script = stream(net.Reply(200, {}, b"x"), url="http://cdn.modrinth.com/a.jar")
    check("plain http: refused with no request, the file goes", res["reason"] == "unsupported" and script.calls == [] and disc.calls == [path], res)
    res, path, disc, script = stream(net.Reply(200, {}, b"x"), vendor="nowhere")
    check("an unknown vendor: refused with no request, the file goes", res["reason"] == "unsupported" and script.calls == [] and disc.calls == [path], res)

    path, disc, script = new_part(), Discards(), Script(net.Reply(200, {}, b"x"))
    with Env(script, resolver=lambda host, port: ["10.0.0.5"]):
        res = net.fetch("modrinth", SDL, want="bytes", to_file=path, discard=disc)
    check("an address the SSRF guard refuses: no request, the file goes", res["ok"] is False and script.calls == [] and disc.calls == [path], res)

    path, disc = new_part(), Discards()
    with Env(Script(ConnectionError("no route to https://cdn.modrinth.com/x"))):
        res = net.fetch("modrinth", SDL, want="bytes", to_file=path, discard=disc)
    check("a connection that cannot be opened: offline, no address in the answer, the file goes",
          res["ok"] is False and res["reason"] == "offline" and "https://" not in res["error"] and disc.calls == [path], res)

    res, path, disc, _ = stream([net.Reply(302, {"Location": "https://cdn.modrinth.com/data/final.jar"}), net.Reply(200, {}, b"PKdata")], max_bytes=1000)
    check("a redirect to an allowed host is followed and the final answer is what is written",
          res["ok"] is True and res["final_url"] == "https://cdn.modrinth.com/data/final.jar" and res["size"] == 6 and disc.calls == [], res)


def test_stream_a_disk_error_is_bad_dest_and_the_file_goes():
    real = os.fsync

    def boom(fd):
        raise OSError(28, "No space left on device")

    os.fsync = boom
    try:
        res, path, disc, _ = stream(net.Reply(200, {}, chunks=Feed([b"q" * 100])), max_bytes=1000)
    finally:
        os.fsync = real
    check("a write that cannot be made durable is bad_dest, not a success",
          res["ok"] is False and res["reason"] == "bad_dest" and "disk" in res["error"], res)
    check("...and the file is discarded once and gone", disc.calls == [path] and not os.path.exists(path), disc.calls)


def test_stream_refuses_a_file_that_is_not_a_fresh_part_and_touches_nothing():
    folder = tempfile.mkdtemp(prefix="stream-", dir=HOME)

    def make(name, content=b""):
        path = os.path.join(folder, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    keep, full, empty = make("keep.jar", b"keep me"), make("full.part", b"already has bytes"), make("empty.part")
    os.mkdir(os.path.join(folder, "dir.part"))
    cases = [("a file that is not a .part", keep), ("a .part that already holds bytes", full),
             ("a .part that does not exist", os.path.join(folder, "missing.part")), ("a relative path", "rel.part"),
             ("a folder named like a .part", os.path.join(folder, "dir.part")), ("an empty string", ""), ("a number", 5),
             ("bytes instead of a path", os.path.join(folder, "empty.part").encode())]
    link = os.path.join(folder, "link.part")
    try:
        os.symlink(empty, link)
        cases.append(("a link to an empty .part", link))
    except (OSError, NotImplementedError, AttributeError):
        pass                                                         # no symlinks here (Windows without the right)
    disc, script = Discards(), Script(net.Reply(200, {}, b"x"))
    with Env(script):
        for label, target in cases:
            res = net.fetch("modrinth", SDL, want="bytes", to_file=target, discard=disc)
            check("%s: refused as unsupported" % label, res["ok"] is False and res["reason"] == "unsupported", res)
    check("no request was sent and discard was never called (a file that is not ours is never removed)",
          script.calls == [] and disc.calls == [], (script.calls, disc.calls))
    with open(keep, "rb") as handle:
        check("the other file still holds its bytes", handle.read() == b"keep me")
    with open(full, "rb") as handle:
        check("the non-empty .part still holds its bytes", handle.read() == b"already has bytes")
    check("the folder is still there", os.path.isdir(os.path.join(folder, "dir.part")))

    path = new_part()
    with Env(script):
        for label, change in (("want=json", {"want": "json"}), ("want=head", {"want": "head"}), ("want=bytes_prefix", {"want": "bytes_prefix"}),
                              ("a POST", {"method": "POST"}), ("no discard function", {"discard": None})):
            args = {"want": "bytes", "discard": disc}
            args.update(change)
            res = net.fetch("modrinth", SDL, to_file=path, **args)
            check("%s with to_file: refused as unsupported" % label, res["ok"] is False and res["reason"] == "unsupported", res)
    check("those refusals sent nothing and left the fresh file where it was",
          script.calls == [] and disc.calls == [] and os.path.getsize(path) == 0, (script.calls, disc.calls))


def test_stream_key_hygiene():
    data = b"PK" + b"j" * 500
    with Config():
        res, path, disc, script = stream(net.Reply(200, {"Content-Length": str(len(data))}, chunks=Feed([data])), vendor="curseforge",
                                         url=CDN, max_bytes=1000)
        check("a CurseForge file from the download host streams fine, with no key sent there and none in the answer",
              res["ok"] is True and "x-api-key" not in script.calls[0]["headers"] and FAKE_KEY not in json.dumps(res, default=str), res)
        res, path, disc, script = stream(net.Reply(403, {}, ("denied " + FAKE_KEY).encode()), vendor="curseforge",
                                         url="https://api.curseforge.com/v1/mods/1/files/2/download")
        check("a 403 from the API host is bad_key with the key scrubbed, and the file goes",
              res["ok"] is False and res["reason"] == "bad_key" and FAKE_KEY not in json.dumps(res, default=str) and disc.calls == [path], res)
    with Config(key=None):
        res, path, disc, script = stream(net.Reply(200, {}, b"x"), vendor="curseforge",
                                         url="https://api.curseforge.com/v1/mods/1/files/2/download")
        check("no key: no_key, no request, and the file goes", res["ok"] is False and res["reason"] == "no_key" and script.calls == []
              and disc.calls == [path], res)


def test_without_to_file_nothing_changes():
    import inspect
    params = inspect.signature(net.fetch).parameters
    check("to_file and discard are optional keyword-only arguments, off by default",
          params["to_file"].default is None and params["discard"].default is None
          and params["to_file"].kind is inspect.Parameter.KEYWORD_ONLY and params["discard"].kind is inspect.Parameter.KEYWORD_ONLY)
    body = b"PK" + b"z" * 300
    headers = {"content-type": "application/octet-stream", "content-length": str(len(body))}
    expected = {"ok": True, "status": 200, "headers": headers, "body": body, "final_url": SDL}
    for label, kw in (("omitted", {}), ("to_file=None", {"to_file": None}), ("a discard alone is ignored", {"discard": Discards()}),
                      ("both None", {"to_file": None, "discard": None})):
        with Env(Script(net.Reply(200, headers, body))):
            res = net.fetch("modrinth", SDL, want="bytes", max_bytes=1000, **kw)
        check("%s: the very same dict as before, with none of the file keys" % label, res == expected, res)
    discarded = Discards()
    with Env(Script(net.Reply(200, {}, b"x" * 3000))):
        res = net.fetch("modrinth", SDL, want="bytes", max_bytes=1000, discard=discarded)
    check("too_big without to_file is the same sentence and keys as before",
          res == {"ok": False, "error": "Modrinth answered with more data than the 1 MB limit.", "reason": "too_big", "status": 200}
          and discarded.calls == [], res)
    with Env(Script(net.Reply(404, {}, b""))):
        res = net.fetch("modrinth", SDL, want="bytes")
    check("a 404 without to_file is the same as before",
          res == {"ok": False, "error": "Modrinth has no such page (HTTP 404).", "reason": "not_found", "status": 404}, res)
    with Env(Script(ConnectionError("x"))):
        res = net.fetch("modrinth", SDL, want="bytes")
    check("a refused connection without to_file is the same as before",
          res == {"ok": False, "error": "Modrinth could not be reached (ConnectionError).", "reason": "offline"}, res)
    with Env(Script(net.Reply(302, {"Location": SDL}))):
        res = net.fetch("modrinth", SDL, want="bytes", max_redirects=0)
    check("max_redirects=0 without to_file still hands the 3xx back", res["ok"] is True and res["status"] == 302 and res["body"] == b"", res)


def test_net_py_never_deletes_anything():
    import ast
    tree = ast.parse(Path(net.__file__).read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("os", "shutil") and node.level == 0:
            bad += [a.name for a in node.names if a.name in ("unlink", "remove", "rmtree", "rmdir", "removedirs", "rename", "replace", "move")]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            on_files = isinstance(base, ast.Name) and base.id in ("os", "shutil")
            if (on_files and node.func.attr in ("unlink", "remove", "rmtree", "rmdir", "removedirs", "rename", "replace", "move")) \
                    or node.func.attr in ("unlink", "rmdir"):
                bad.append(node.func.attr)
    check("net.py calls nothing that deletes, renames or moves a file: the caller's discard does that (G11 stays as it is)", not bad, bad)


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
