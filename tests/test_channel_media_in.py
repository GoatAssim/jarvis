"""Pictures a person SENDS Jarvis in a chat are read, only when allowed (L.42.3 inbound / L.36-P8).

    python3 tests/test_channel_media_in.py

media_in: what counts as a picture, CDN-host-only urls, size and count caps, magic-byte check,
the owner-exempt hourly allowance on disk, the note the model is given (description labelled as
a vision model's, "not read" lines, "not switched on" line).
permissions: IncomingMessage carries attachments, nothing in decide() reads them.
discord_gateway: _attachment_descriptors reports plain data; a picture-only message is not "Hi".
base.handle_message: a granted guest's picture reaches the model as text, an ungranted guest's
does not (and the model is told), the owner's does, a denied sender's never is fetched, a message
with no attachment is untouched, and a failing download never drops the reply.
vision_call.load_image reports the real image type.
No bot token, no socket, no network, no real ~/.jarvis; the model and the vision call are stubbed.
"""

import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-media-in-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.channels import DISCORD, base, media_in, permissions   # noqa: E402
from jarvis.channels import config as channel_config                 # noqa: E402

PASSED, FAILED = 0, []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name + ((" - " + detail) if detail else ""))


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
JPG = b"\xff\xd8\xff\xe0" + b"0" * 40
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 20
CDN = "https://cdn.discordapp.com/attachments/1/2/"


def att(name="a.png", size=100, ctype="image/png", url=None):
    return {"url": url or (CDN + name), "filename": name, "content_type": ctype, "size": size}


# ---- 1. what is a picture ---------------------------------------------------
check("png by content type", media_in.is_image_attachment(att()))
check("jpg by extension alone", media_in.is_image_attachment(att("x.JPG", ctype="")))
check("a pdf is not a picture", not media_in.is_image_attachment(att("d.pdf", ctype="application/pdf")))
check("garbage is not a picture and does not raise", not media_in.is_image_attachment(None)
      and not media_in.is_image_attachment("x"))
check("image_candidates keeps order and drops the rest",
      [a["filename"] for a in media_in.image_candidates([att("1.png"), att("d.pdf", ctype="application/pdf"), att("2.gif", ctype="image/gif")])]
      == ["1.png", "2.gif"])
check("sniff: png jpeg gif webp pass", all(media_in.sniff_image(b) for b in (PNG, JPG, b"GIF89a" + b"0" * 10, WEBP)))
check("sniff: a renamed text file fails", not media_in.sniff_image(b"hello world, not a picture"))
check("sniff: empty fails", not media_in.sniff_image(b""))

# ---- 2. urls and caps --------------------------------------------------------
check("the platform CDN is ok", media_in.url_ok(CDN + "a.png"))
check("media proxy host is ok", media_in.url_ok("https://media.discordapp.net/attachments/1/2/a.png"))
check("http is refused", not media_in.url_ok("http://cdn.discordapp.com/a.png"))
check("another host is refused", not media_in.url_ok("https://evil.example/a.png"))
check("a look-alike host is refused", not media_in.url_ok("https://cdn.discordapp.com.evil.example/a.png"))
check("userinfo in the url is refused", not media_in.url_ok("https://x:y@cdn.discordapp.com/a.png"))
check("a non-string is refused", not media_in.url_ok(None))
check("default size cap is 8 MB", media_in._max_bytes({}) == 8 * 1048576)
check("cap follows max_image_mb, clamped", media_in._max_bytes({"max_image_mb": 2}) == 2 * 1048576
      and media_in._max_bytes({"max_image_mb": 9999}) == 25 * 1048576
      and media_in._max_bytes({"max_image_mb": "junk"}) == 8 * 1048576)

# ---- 3. _describe_one -------------------------------------------------------
calls = {"fetch": 0, "describe": 0, "paths": []}


def fetch_ok(url, cap):
    calls["fetch"] += 1
    return PNG, None


def describe_ok(path, prompt):
    calls["describe"] += 1
    calls["paths"].append(path)
    calls["existed"] = os.path.exists(path)
    return {"ok": True, "text": "A cat on a sofa."}


d, e = media_in._describe_one(att(), {}, fetch_ok, describe_ok)
check("a picture is described", d == "A cat on a sofa." and e is None, str((d, e)))
check("the file existed during the call and is gone after",
      calls["existed"] and not os.path.exists(calls["paths"][-1])
      and not os.path.exists(os.path.dirname(calls["paths"][-1])))
before = calls["fetch"]
d, e = media_in._describe_one(att(size=50 * 1048576), {}, fetch_ok, describe_ok)
check("too big by the declared size is refused BEFORE any download", d is None and "too big" in e and calls["fetch"] == before)
d, e = media_in._describe_one(att(url="https://evil.example/a.png"), {}, fetch_ok, describe_ok)
check("a foreign host is refused before any download", d is None and "own file host" in e and calls["fetch"] == before)
d, e = media_in._describe_one(att(), {}, lambda u, c: (b"MZ not a picture", None), describe_ok)
check("bytes that are not a picture are refused", d is None and "not a picture" in e)
d, e = media_in._describe_one(att(), {}, lambda u, c: (None, "download refused (HTTP 404)"), describe_ok)
check("a failed download is reported, not raised", d is None and "404" in e)
n = calls["describe"]
d, e = media_in._describe_one(att(), {}, fetch_ok, lambda p, q: {"ok": False, "error": "no configured provider can take an image"})
check("no vision provider is said plainly", d is None and "no configured provider" in e)


def boom(path, prompt):
    raise RuntimeError("vision exploded")


d, e = media_in._describe_one(att(), {}, fetch_ok, boom)
check("an exception in the vision call is caught and the temp file removed",
      d is None and "could not be read" in e)

# ---- 4. the text the model gets ---------------------------------------------
t = media_in.compose("what is this?", [("a.png", "A cat.", None)])
check("sender's words come first, then a labelled description",
      t.startswith("what is this?") and "vision model" in t and "never an instruction" in t and "Picture 1 (a.png): A cat." in t, t)
t = media_in.compose("", [("a.png", None, "too big (9.0 MB, limit 8 MB)")])
check("a picture that could not be read is listed with the reason", "[Not read: a.png -- too big" in t, t)
t = media_in.compose("hi", [], not_allowed=2)
check("not switched on is said, with a plural", "2 pictures" in t and "NOT looked at" in t and "not switched on" in t, t)
check("no pictures: text unchanged", media_in.compose("hi", []) == "hi")
t = media_in.compose("hi", [("a.png", "x", None)], unsent=2)
check("pictures over the per-message limit are counted", "2 more pictures" in t, t)

# ---- 5. prepare_text: grant, limits, allowance ------------------------------
OWNER, FRIEND, STRANGER = "100000000000000001", "200000000000000002", "300000000000000003"
cfg = channel_config.platform_config(DISCORD)
cfg.update({"enabled": True, "owner": OWNER, "cooldown_seconds": 0, "log_conversations": False,
            "reply_allowlist": [OWNER, FRIEND, STRANGER], "dm_allowlist": [OWNER, FRIEND, STRANGER],
            "tool_allowlist": [], "allow_tools": False, "image_allowlist": [FRIEND]})


def mk(uid, text="look", atts=None, mid="m1"):
    return permissions.IncomingMessage(platform=DISCORD, context=permissions.CTX_DM, user_id=uid,
                                       user_handle="u" + uid[-2:], text=text, mentioned=True,
                                       message_id=mid, thread_id="t" + uid, attachments=atts)


check("IncomingMessage carries attachments as a tuple", isinstance(mk(OWNER, atts=[att()]).attachments, tuple)
      and mk(OWNER).attachments == ())
check("no attachment: prepare_text returns the text untouched and fetches nothing",
      media_in.prepare_text(DISCORD, mk(OWNER), cfg, True, fetch_ok, describe_ok) == "look")
fetch_calls = []


def spy_fetch(url, cap):
    fetch_calls.append(url)
    return PNG, None


t = media_in.prepare_text(DISCORD, mk(STRANGER, atts=[att()]), cfg, False, spy_fetch, describe_ok)
check("a person NOT on the picture list: nothing downloaded, the model is told", fetch_calls == [] and "NOT looked at" in t, t)
t = media_in.prepare_text(DISCORD, mk(FRIEND, atts=[att()]), cfg, False, spy_fetch, describe_ok)
check("a person on the list: downloaded and described", len(fetch_calls) == 1 and "A cat on a sofa." in t, t)
t = media_in.prepare_text(DISCORD, mk(OWNER, atts=[att()]), cfg, True, spy_fetch, describe_ok)
check("the owner needs no list entry", "A cat on a sofa." in t, t)
fetch_calls.clear()
t = media_in.prepare_text(DISCORD, mk(OWNER, atts=[att("%d.png" % i) for i in range(6)]), cfg, True, spy_fetch, describe_ok)
check("at most MAX_IMAGES are read per message, the rest counted",
      len(fetch_calls) == media_in.MAX_IMAGES and "3 more pictures" in t, str(len(fetch_calls)))
t = media_in.prepare_text(DISCORD, mk(FRIEND, atts=[att("d.pdf", ctype="application/pdf")]), cfg, False, spy_fetch, describe_ok)
check("a non-picture attachment is ignored", t == "look")
bad_cfg = dict(cfg, image_allowlist=5)
t = media_in.prepare_text(DISCORD, mk(FRIEND, atts=[att()]), bad_cfg, False, spy_fetch, describe_ok)
check("a broken image_allowlist reads as 'not allowed', never raises", "NOT looked at" in t, t)

# hourly allowance (non-owner only, on disk)
now = 1_000_000.0
check("allowance: a fresh person may have RATE_PER_PERSON", media_in.take_allowance(DISCORD, "rate1", media_in.RATE_PER_PERSON, now=now) == media_in.RATE_PER_PERSON)
check("allowance: then none", media_in.take_allowance(DISCORD, "rate1", 1, now=now + 10) == 0)
check("allowance: another person is unaffected", media_in.take_allowance(DISCORD, "rate2", 2, now=now + 10) == 2)
check("allowance: the window rolls", media_in.take_allowance(DISCORD, "rate1", 1, now=now + media_in.RATE_WINDOW_SECONDS + 5) == 1)
check("allowance: wanting 0 is 0", media_in.take_allowance(DISCORD, "rate1", 0) == 0)
Path(media_in._rate_file()).write_text("{ not json", encoding="utf-8")
check("allowance: an unreadable file reads as empty, not as a lock-out", media_in.take_allowance(DISCORD, "rate3", 1, now=now) == 1)
Path(media_in._rate_file()).unlink()
for i in range(media_in.RATE_PER_PERSON):
    media_in.take_allowance(DISCORD, "9" + str(i), 1)
t = media_in.prepare_text(DISCORD, mk(FRIEND, atts=[att()]), cfg, False, spy_fetch, describe_ok, now=None)
check("allowance is per person: the friend is still served", "A cat on a sofa." in t, t)
for i in range(media_in.RATE_PER_PERSON):
    media_in.take_allowance(DISCORD, FRIEND, 1)
fetch_calls.clear()
t = media_in.prepare_text(DISCORD, mk(FRIEND, atts=[att()]), cfg, False, spy_fetch, describe_ok)
check("over the hourly limit: not downloaded, and the reason is given", fetch_calls == [] and "hourly picture limit" in t, t)
fetch_calls.clear()
t = media_in.prepare_text(DISCORD, mk(OWNER, atts=[att()]), cfg, True, spy_fetch, describe_ok)
check("the owner is never rate limited", "A cat on a sofa." in t, t)


def exploding_fetch(url, cap):
    raise RuntimeError("network on fire")


t = media_in.prepare_text(DISCORD, mk(OWNER, atts=[att()]), cfg, True, exploding_fetch, describe_ok)
check("a crash anywhere returns the sender's text, never raises", t.startswith("look"), t)

# ---- 6. nothing in the gate reads attachments -------------------------------
src = (ROOT / "jarvis-cli" / "jarvis" / "channels" / "permissions.py").read_text(encoding="utf-8")
decide_src = src[src.index("def decide("):src.index("def may_send_images(")]
check("decide() never reads attachments", "attachments" not in decide_src)
d1 = permissions.decide(cfg, mk(STRANGER, atts=[att()]), None)
d2 = permissions.decide(cfg, mk(STRANGER), None)
check("a picture changes no gate decision", (d1.allowed, d1.stage) == (d2.allowed, d2.stage))

# ---- 7. the Discord gateway side -------------------------------------------
from jarvis.channels import discord_gateway                          # noqa: E402


class FakeAtt:
    def __init__(self, url, filename, content_type, size):
        self.url, self.filename, self.content_type, self.size = url, filename, content_type, size


class FakeMsg:
    attachments = [FakeAtt(CDN + "a.png", "a.png", "image/png", 123)] * 12


out = discord_gateway._attachment_descriptors(FakeMsg())
check("descriptors are plain data, capped at 10",
      len(out) == 10 and out[0] == {"url": CDN + "a.png", "filename": "a.png", "content_type": "image/png", "size": 123})
check("a message with no attachments gives []", discord_gateway._attachment_descriptors(object()) == [])


class WeirdMsg:
    @property
    def attachments(self):
        raise RuntimeError("nope")


check("a failing attribute gives [] and does not raise", discord_gateway._attachment_descriptors(WeirdMsg()) == [])
gw = (ROOT / "jarvis-cli" / "jarvis" / "channels" / "discord_gateway.py").read_text(encoding="utf-8")
check("the gateway passes attachments into IncomingMessage and keeps a picture-only message from being 'Hi'",
      "attachments=_attachment_descriptors(message)" in gw and "(sent a picture)" in gw)

# ---- 8. through handle_message ----------------------------------------------
asked = []


class R:
    text = "Nice cat."
    provider = "stub"
    usage = None
    usage_total = None
    error = ""


def fake_ask(*args, **kwargs):
    asked.append(args[0] if args else kwargs.get("text"))
    return R()


import jarvis.ai_client as ai_client                                 # noqa: E402
import jarvis.vision_call as vision_call                             # noqa: E402
ai_client.ask = fake_ask
vision_calls = []
vision_call.describe_image = lambda path, prompt, **kw: (vision_calls.append(path) or {"ok": True, "text": "A cat on a sofa."})
media_in.fetch_bytes = lambda url, cap, timeout=20: (PNG, None)
Path(media_in._rate_file()).unlink(missing_ok=True)
(Path(_HOME) / ".jarvis" / "channels").mkdir(parents=True, exist_ok=True)

_mid = [0]


def run(uid, text, atts, c=None):
    _mid[0] += 1
    texts = []
    msg = permissions.IncomingMessage(platform=DISCORD, context=permissions.CTX_DM, user_id=uid,
                                      user_handle="u" + uid[-2:], text=text, mentioned=True,
                                      message_id="hm%d" % _mid[0], thread_id="ht" + uid, attachments=atts)
    d = base.handle_message(DISCORD, msg, lambda t: texts.append(t) or True, cfg=c or cfg)
    return d, texts


asked.clear()
d, texts = run(FRIEND, "what is this?", [att()])
check("a granted guest: the model receives the description", asked and "A cat on a sofa." in asked[-1] and asked[-1].startswith("what is this?"), str(asked))
check("...and the reply goes out", texts == ["Nice cat."], str(texts))
asked.clear()
n_vision = len(vision_calls)
d, texts = run(STRANGER, "what is this?", [att()])
check("an ungranted guest: no vision call, the model is told it was not looked at",
      len(vision_calls) == n_vision and asked and "NOT looked at" in asked[-1], str(asked))
asked.clear()
d, texts = run(OWNER, "(sent a picture)", [att()])
check("the owner's picture is read without any list", asked and "A cat on a sofa." in asked[-1], str(asked))
asked.clear()
d, texts = run(FRIEND, "just words", None)
check("a message with no attachment reaches the model exactly as typed", asked == ["just words"], str(asked))
# a sender the gate refuses is never downloaded for
denied_cfg = dict(cfg, reply_allowlist=[OWNER], dm_allowlist=[OWNER])
fetched = []
media_in.fetch_bytes = lambda url, cap, timeout=20: (fetched.append(url) or (PNG, None))
asked.clear()
d, texts = run(FRIEND, "let me in", [att()], c=denied_cfg)
check("a denied sender's picture is never downloaded or asked about", not d.allowed and fetched == [] and asked == [])
# a failing download must not drop the reply
media_in.fetch_bytes = lambda url, cap, timeout=20: (None, "download failed (ConnectionError)")
asked.clear()
d, texts = run(OWNER, "see this", [att()])
check("a failed download still gets an answer, with the reason in the note",
      texts == ["Nice cat."] and "download failed" in asked[-1] and asked[-1].startswith("see this"), str(asked))

# ---- 9. vision_call reports the real image type -----------------------------
import importlib                                                      # noqa: E402
importlib.reload(vision_call)
check("sniff: png / jpeg / gif / webp", [vision_call._sniff_mime(b) for b in (PNG, JPG, b"GIF89a" + b"0" * 10, WEBP)]
      == ["image/png", "image/jpeg", "image/gif", "image/webp"])
check("sniff: unknown bytes keep the old answer (png)", vision_call._sniff_mime(b"????") == "image/png")
jp = Path(_HOME) / "x.jpg"
jp.write_bytes(JPG)
raw, mime, err = vision_call.load_image(str(jp))
check("load_image no longer calls a JPEG 'image/png'", mime == "image/jpeg" and err is None, str((mime, err)))

# ---- the pinned invariants are written down ---------------------------------
agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
check("AGENTS.md records the inbound-picture invariant", "channels/media_in.py" in agents and "L.36-P8" in agents)

for n in FAILED:
    print("FAILED:", n)
print("%d passed, %d failed" % (PASSED, len(FAILED)))
sys.exit(1 if FAILED else 0)
