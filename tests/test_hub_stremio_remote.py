"""Stremio addresses other than this PC, Stremio's own website, and opening links in the Stremio app.

Covers `hub/stremio/config.py` (`allow_remote`, the address check with and without the box, `save_settings` with `network` and
`open_with`, `is_this_pc`, `open_with`), `hub/stremio/links.native_link`, `hub/stremio/player.open_link` / `open_in_app`,
`server.port_answers` (the https default port) and the `hub_admin` ops that carry them.  A temp HOME, a fake Stremio probe and
fake openers: nothing is contacted and no program is started.

Run: python tests/test_hub_stremio_remote.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-stremio-remote-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT", "JARVIS_STREMIO_SERVER_URL"):
    os.environ.pop(var, None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import hub_admin  # noqa: E402
from jarvis.hub.stremio import config, links, player, server  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


cfg_path = config.config_path()
cfg_path.parent.mkdir(parents=True, exist_ok=True)
TAILSCALE_WEB = "https://100.72.25.9:8080/"          # the address from the bug report (trailing slash and all)
ALIVE = lambda url: (True, "4.20.8")                 # noqa: E731 - a probe that says "a Stremio server answers"


def reset():
    # write_json keeps a .bak that read_json falls back to, so it goes too
    for leftover in (cfg_path, cfg_path.with_suffix(cfg_path.suffix + ".bak")):
        if leftover.exists():
            leftover.unlink()


def read():
    return json.loads(cfg_path.read_text(encoding="utf-8"))


# ===================================================================== the default stays as strict as it was
reset()
check("default: nothing is allowed beyond this PC", config.allow_remote() is False)
res = config.save_settings(web={"url": TAILSCALE_WEB})
check("default: the https Tailscale web address is refused, with the old words and a way out",
      not res["ok"] and "http on 127.0.0.1 or localhost" in res["error"] and "Allow addresses other than this PC" in res["error"]
      and not cfg_path.exists())
res = config.save_settings(server_url="https://100.72.25.9:11470", check=ALIVE)
check("default: the same goes for the server address", not res["ok"] and "server address must be http on 127.0.0.1 or localhost" in res["error"])
cfg_path.write_text(json.dumps({"web": {"url": TAILSCALE_WEB}, "server": {"url": "http://100.72.25.9:11470"}}), encoding="utf-8")
check("default: a hand-edited remote address is still ignored on read (the defaults are used) and problems() says so",
      config.web_url() == config.DEFAULT_WEB_URL and config.server_url() == config.DEFAULT_SERVER_URL
      and len(config.problems()) == 2 and "allow_remote" in config.problems()[0])
reset()
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://100.72.25.9:11470"
check("default: the environment override obeys the same rule", config.server_url() == config.DEFAULT_SERVER_URL)
os.environ.pop("JARVIS_STREMIO_SERVER_URL")

# ===================================================================== the box and the address in ONE save
res = config.save_settings(web={"url": TAILSCALE_WEB}, network={"allow_remote": True})
check("one save: the box and a remote https web address travel together and are saved",
      res["ok"] and res["saved"]["network"] == {"allow_remote": True} and res["saved"]["web"]["url"] == "https://100.72.25.9:8080")
check("one save: stremio.json has both, the trailing slash is gone", read()["network"] == {"allow_remote": True}
      and read()["web"]["url"] == "https://100.72.25.9:8080")
check("one save: web_url() now answers the Tailscale address (https kept)", config.web_url() == "https://100.72.25.9:8080")
check("one save: a deep link is built on it", links.deep_link_detail(config.web_url(), "tt0944947", "series", 1, 2)
      == "https://100.72.25.9:8080/#/detail/series/tt0944947/tt0944947:1:2")

res = config.save_settings(web={"url": config.STREMIO_SITE_URL})
check("Stremio's own website is accepted as the web address once the box is on",
      res["ok"] and config.web_url() == "https://web.stremio.com" and config.STREMIO_SITE_URL == "https://web.stremio.com")
check("its links open on the hosted site", links.screen_link(config.web_url(), "library") == "https://web.stremio.com/#/library")

for bad in ("https://user:pw@web.stremio.com", "https://web.stremio.com/path", "https://web.stremio.com/?q=1", "https://web.stremio.com/#x",
            "ftp://web.stremio.com", "https://bad host:8080", "https://x:99999", "web.stremio.com", "https://"):
    res = config.save_settings(web={"url": bad})
    check("remote on: %r is still refused" % bad, not res["ok"] and "http or https address" in res["error"])

res = config.save_settings(server_url="https://100.72.25.9:11470", check=lambda u: (False, "down"))
check("remote on: a server address that doesn't answer like Stremio is still not saved", not res["ok"] and "nothing answers like a Stremio server" in res["error"])
res = config.save_settings(server_url="https://100.72.25.9:11470", check=ALIVE)
check("remote on: a remote server address that answers is saved", res["ok"] and config.server_url() == "https://100.72.25.9:11470")
check("remote on: the engine's allow-list entry is that host and port (https default 443 is not guessed)",
      config.server_hostport() == "100.72.25.9:11470" and config.hostport("https://web.stremio.com") == "web.stremio.com:443")

# ===================================================================== turning the box off
res = config.save_settings(network={"allow_remote": False})
check("box off: refused while a saved address is not on this PC, and it says which",
      not res["ok"] and "isn't on this PC" in res["error"] and read()["network"] == {"allow_remote": True})
res = config.save_settings(network={"allow_remote": False}, web={"url": "http://127.0.0.1:8080"}, server_url="http://127.0.0.1:11470", check=ALIVE)
check("box off: allowed in the same save as local addresses", res["ok"] and not config.allow_remote() and config.web_url() == "http://127.0.0.1:8080"
      and config.server_url() == "http://127.0.0.1:11470")
for bad in ({"allow_remote": "yes"}, {"allow_remote": 1}, {"other": True}, "true", {}):
    res = config.save_settings(network=bad)
    check("network %r is refused and changes nothing" % (bad,), not res["ok"] and not config.allow_remote())

# ===================================================================== still the owner's button only
reset()
for var in ("JARVIS_CHANNEL", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CHANNEL_SENDER"):
    os.environ[var] = "1"
    res = config.save_settings(network={"allow_remote": True})
    os.environ.pop(var)
    check("unattended (%s): turning the box on is refused and writes nothing" % var, not res["ok"] and res.get("code") == "unattended" and not cfg_path.exists())
src = (ROOT / "jarvis-cli" / "jarvis" / "actions")
check("no model tool names the new setters", not any(name in py.read_text(encoding="utf-8", errors="replace") for py in src.glob("*.py")
                                                    for name in ("allow_remote", "save_settings", "check_network", "open_with=")))

# ===================================================================== a broken-shaped file is never overwritten
cfg_path.write_text(json.dumps({"network": "nope"}), encoding="utf-8")
res = config.save_settings(network={"allow_remote": True})
check("a stremio.json whose network entry isn't an object is left alone", not res["ok"] and "isn't shaped like Jarvis expects" in res["error"]
      and json.loads(cfg_path.read_text()) == {"network": "nope"} and config.allow_remote() is False)
reset()

# ===================================================================== this PC or not
check("is_this_pc: loopback names and addresses", all(config.is_this_pc(u) for u in ("http://127.0.0.1:8080", "http://localhost:8080", "http://[::1]:8080")))
check("is_this_pc: a public host and documentation addresses are not this PC",
      not any(config.is_this_pc(u) for u in ("https://web.stremio.com", "https://8.8.8.8", "http://192.0.2.77:8080", "", "nonsense")))

# ===================================================================== where links open
check("open_with: browser by default", config.open_with() == "browser")
res = config.save_settings(open_with="native")
check("open_with: native saves and reads back", res["ok"] and res["saved"]["open"] == "native" and config.open_with() == "native" and read()["open"] == {"with": "native"})
for bad in ("app", "", "Native2", None):
    res = config.save_settings(open_with=bad) if bad is not None else {"ok": False}
    check("open_with %r is refused" % (bad,), not res["ok"] and config.open_with() == "native")
cfg_path.write_text(json.dumps({"open": {"with": "weird"}}), encoding="utf-8")
check("open_with: an unreadable value reads as browser", config.open_with() == "browser")
reset()

# ===================================================================== the native link and the openers
WEB = "http://127.0.0.1:8080"
detail = links.deep_link_detail(WEB, "tt0944947", "series", 1, 1)
check("native_link: a detail link becomes a stremio:/// link", links.native_link(detail, WEB) == "stremio:///detail/series/tt0944947/tt0944947:1:1")
check("native_link: the board and a search", links.native_link(links.screen_link(WEB, "board"), WEB) == "stremio:///"
      and links.native_link(links.screen_link(WEB, "search", "sin tel"), WEB) == "stremio:///search?search=sin%20tel")
for bad in ("http://evil.example/#/detail/movie/tt1/tt1", "javascript:alert(1)", WEB + "/other", WEB):
    try:
        links.native_link(bad, WEB)
        check("native_link refuses %r" % bad, False)
    except ValueError:
        check("native_link refuses %r" % bad, True)

opened = {"browser": [], "app": []}
browser = lambda u: opened["browser"].append(u) or True      # noqa: E731
app = lambda u: opened["app"].append(u) or True              # noqa: E731
check("open_link in browser mode opens the web link in the browser and never the app",
      player.open_link(detail, opener=browser, app_opener=app) == (True, "") and opened == {"browser": [detail], "app": []})
config.save_settings(open_with="native")
opened = {"browser": [], "app": []}
check("open_link in native mode hands the stremio:/// link to the app and never the browser",
      player.open_link(detail, opener=browser, app_opener=app) == (True, "") and opened == {"browser": [], "app": ["stremio:///detail/series/tt0944947/tt0944947:1:1"]}
      and player.uses_app())
ok, why = player.open_link("http://evil.example/#/x", opener=browser, app_opener=app)
check("native mode still refuses a link that isn't on the stremio-web address", not ok and "isn't on stremio-web" in why and len(opened["app"]) == 1)
for bad in ("http://127.0.0.1:8080/#/x", "stremio:///x y", 'stremio:///x"', "stremio:///x\n"):
    ok, why = player.open_in_app(bad, opener=app)
    check("open_in_app refuses %r" % bad, not ok and len(opened["app"]) == 1)


def boom(url):
    raise OSError("no handler")


ok, why = player.open_in_app("stremio:///", opener=boom)
check("an app that isn't installed is reported in words, not raised", not ok and "couldn't open the Stremio app" in why)
config.save_settings(web={"url": "http://127.0.0.1:8080"})

# ===================================================================== a bare connect uses https' own port
seen = []
real_connect = server.socket.create_connection
server.socket.create_connection = lambda addr, timeout=None: seen.append(addr) or (_ for _ in ()).throw(OSError("closed"))
server.port_answers("https://web.stremio.com")
server.port_answers("http://127.0.0.1")
server.port_answers("https://100.72.25.9:8080")
server.socket.create_connection = real_connect
check("port_answers: https defaults to 443, http to 80, a given port wins", seen == [("web.stremio.com", 443), ("127.0.0.1", 80), ("100.72.25.9", 8080)])

# ===================================================================== hub_admin: the card's own buttons
reset()
out = hub_admin.admin("stremio_save", {"web": {"url": TAILSCALE_WEB}})
check("hub-admin: a remote web address without the box is refused, in words", not out.get("ok", True) and "Allow addresses other than this PC" in str(out.get("error")))
out = hub_admin.admin("stremio_save", {"web": {"url": TAILSCALE_WEB}, "network": {"allow_remote": True}, "open_with": "native"})
wc = out.get("web_config") or {}
check("hub-admin: the box, the address and 'open with' save in one go and the status shows them",
      out.get("ok") is not False and wc.get("url") == "https://100.72.25.9:8080" and wc.get("allow_remote") is True and wc.get("open_with") == "native"
      and wc.get("stremio_site_url") == "https://web.stremio.com" and wc.get("this_pc") is False)
dry = hub_admin.admin("stremio_save", {"web": {"url": "https://web.stremio.com"}, "network": {"allow_remote": True}, "dry_run": True})
check("hub-admin: a dry run judges the address by the box in the same request", dry.get("ok") is True and dry.get("dry_run") is True)
bad = hub_admin.admin("stremio_save", {"open_with": "carrier-pigeon"})
check("hub-admin: a bad 'open with' is refused", not bad.get("ok", True))
status = hub_admin.admin("stremio_status", {})
check("hub-admin: the status web block keeps its three keys", sorted((status.get("web") or {}).keys()) == ["answers", "configured", "url"])

# ===================================================================== the web route
server_js = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
check("server.js: the paths route passes network ({allow_remote} only) and open_with, clamped",
      "hubStremioNetwork(req.body?.network)" in server_js and 'typeof value.allow_remote === "boolean"' in server_js
      and 'open_with: typeof req.body?.open_with === "string"' in server_js)
check("hub_admin.py still has no address check of its own", "_loopback_url" not in (ROOT / "jarvis-cli" / "jarvis" / "hub_admin.py").read_text(encoding="utf-8"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
