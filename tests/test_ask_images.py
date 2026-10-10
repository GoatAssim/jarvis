"""Pictures in the Ask Jarvis panel, and pictures kept with the conversation (Discord / Instagram / panel).

    python3 tests/test_ask_images.py

conv_images: the store (type from the bytes, strict ids, caps, delete).
media_in: Instagram CDN hosts, `prepare()` keeps a copy of what it fetched (and only that),
          `prepare_files()` (the panel) uses the same description path and wrapper wording.
conversations / raw_archive: `images` + `shown` ride on the exchange AND survive the raw-event replay
          the web UI loads from; redo finds the exchange by what was typed; delete / clear / redo
          remove the pictures (including ones only the raw log still names).
instagram_gateway: a picture-only DM is no longer dropped; only `image` attachments count.
base.handle_message: a picture reaches the ask with its references, a refused one does not.
cli: the panel's manifest (JARVIS_ASK_IMAGES_FILE) becomes description text + references.
web/ask-images.js limits agree with the Python side (static check).
No network, no real ~/.jarvis, no model: the vision call and the download are stubbed.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-ask-images-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ.pop("JARVIS_ASK_IMAGES_FILE", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import conv_images, conversations, raw_archive           # noqa: E402
from jarvis.channels import DISCORD, INSTAGRAM, base, media_in, permissions   # noqa: E402
from jarvis.channels import config as channel_config                   # noqa: E402
from jarvis.channels import instagram_gateway                          # noqa: E402

(Path(_HOME) / ".jarvis" / "channels").mkdir(parents=True, exist_ok=True)   # the per-person picture allowance lives here

PASSED, FAILED = 0, []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name + ((" - " + detail) if detail else ""))


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
JPG = b"\xff\xd8\xff\xe0" + b"0" * 40
GIF = b"GIF89a" + b"0" * 20
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 20

# ---- 1. the store ------------------------------------------------------------
check("kind comes from the bytes", [conv_images.kind_of(b) for b in (PNG, JPG, GIF, WEBP)] == ["png", "jpg", "gif", "webp"])
check("a text file has no kind", conv_images.kind_of(b"hello, not a picture") == "")
ref = conv_images.save(PNG, "../../evil.jpg\x00")
check("save returns id/name/size", ref and set(ref) == {"id", "name", "size"} and ref["size"] == len(PNG), str(ref))
check("the extension is the bytes', never the name's", ref["id"].endswith(".png"), ref["id"])
check("the id is strict", conv_images.is_valid_id(ref["id"]) and re.match(r"^ci_[0-9a-f]{24}\.png$", ref["id"]))
check("the file exists under conv_images", conv_images.path_for(ref["id"]).read_bytes() == PNG)
check("a control character in the name is gone", "\x00" not in ref["name"])
check("a non-picture is not kept", conv_images.save(b"MZ not a picture") is None)
check("an empty file is not kept", conv_images.save(b"") is None)
check("an oversized file is not kept", conv_images.save(PNG + b"0" * conv_images.MAX_BYTES) is None)
for bad in ("../x.png", "ci_abc.png", "ci_" + "g" * 24 + ".png", "ci_" + "a" * 24 + ".svg", "/etc/passwd", None, 5):
    check("path_for refuses %r" % (bad,), conv_images.path_for(bad) is None)
check("content types", conv_images.content_type(ref["id"]) == "image/png" and conv_images.content_type("x") == "")
dirty = conv_images.clean_refs([{"id": ref["id"], "name": "a", "size": "7", "path": "/etc/passwd", "url": "http://x"},
                                {"id": "../bad.png"}, "junk", None] + [dict(ref)] * 5)
check("clean_refs keeps three safe fields, valid ids only, at most three",
      len(dirty) == conv_images.MAX_PER_MESSAGE and all(set(r) == {"id", "name", "size"} for r in dirty)
      and dirty[0]["size"] == 7, str(dirty))
check("clean_refs of a non-list is empty", conv_images.clean_refs("x") == [] and conv_images.clean_refs(None) == [])
check("delete_many removes the file", conv_images.delete_many([ref]) == 1 and not conv_images.path_for(ref["id"]).exists())
check("delete_many ignores junk", conv_images.delete_many([{"id": "../x"}, None, "s"]) == 0)

# ---- 2. media_in: hosts ---------------------------------------------------------
IG = "https://scontent-fra5-1.cdninstagram.com/v/t51/abc.jpg?sig=1"
check("an Instagram CDN host is ok", media_in.url_ok(IG))
check("lookaside.fbsbx.com is ok", media_in.url_ok("https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1"))
check("an fbcdn host is ok", media_in.url_ok("https://scontent.xx.fbcdn.net/v/a.jpg"))
check("a look-alike suffix is refused", not media_in.url_ok("https://evilcdninstagram.com/a.jpg")
      and not media_in.url_ok("https://cdninstagram.com.evil.example/a.jpg"))
check("the bare suffix domain without a subdomain is refused", not media_in.url_ok("https://fbcdn.net/a.jpg"))
check("http / userinfo still refused", not media_in.url_ok(IG.replace("https", "http"))
      and not media_in.url_ok("https://u:p@scontent.cdninstagram.com/a.jpg"))
check("the Discord hosts still work", media_in.url_ok("https://cdn.discordapp.com/attachments/1/2/a.png"))

# ---- 3. media_in.prepare keeps what it fetched ---------------------------------------
OWNER, FRIEND, STRANGER = "100000000000000001", "200000000000000002", "300000000000000003"
cfg = channel_config.platform_config(DISCORD)
cfg.update({"enabled": True, "owner": OWNER, "cooldown_seconds": 0, "log_conversations": False,
            "reply_allowlist": [OWNER, FRIEND, STRANGER], "dm_allowlist": [OWNER, FRIEND, STRANGER],
            "tool_allowlist": [], "allow_tools": False, "image_allowlist": [FRIEND]})
CDN = "https://cdn.discordapp.com/attachments/1/2/"


def att(name="a.png", size=100, ctype="image/png", url=None):
    return {"url": url or (CDN + name), "filename": name, "content_type": ctype, "size": size}


def mk(uid, text="look", atts=None, platform=DISCORD, mid="m1"):
    return permissions.IncomingMessage(platform=platform, context=permissions.CTX_DM, user_id=uid,
                                       user_handle="u" + uid[-2:], text=text, mentioned=True,
                                       message_id=mid, thread_id="t" + uid, attachments=atts)


def describe_ok(path, prompt):
    return {"ok": True, "text": "A cat on a sofa."}


text, refs = media_in.prepare(DISCORD, mk(OWNER, atts=[att()]), cfg, True, lambda u, c: (PNG, None), describe_ok)
check("prepare: the text carries the description", "A cat on a sofa." in text and text.startswith("look"), text)
check("prepare: one reference kept, bytes on disk", len(refs) == 1 and conv_images.path_for(refs[0]["id"]).read_bytes() == PNG, str(refs))
check("prepare: the reference carries the picture's name", refs[0]["name"] == "a.png", str(refs))

text, refs = media_in.prepare(DISCORD, mk(OWNER, atts=[att()]), cfg, True, lambda u, c: (PNG, None),
                              lambda p, q: {"ok": False, "error": "no provider"})
check("prepare: a failed description still keeps the picture, and says so in the note",
      len(refs) == 1 and "could not be read" in text, text)

text, refs = media_in.prepare(DISCORD, mk(STRANGER, atts=[att()]), cfg, False, lambda u, c: (PNG, None), describe_ok)
check("prepare: a person without the grant: nothing fetched, nothing kept", refs == [] and "NOT looked at" in text, text)
text, refs = media_in.prepare(DISCORD, mk(OWNER, atts=[att(url="https://evil.example/a.png")]), cfg, True,
                              lambda u, c: (PNG, None), describe_ok)
check("prepare: an off-host picture is not kept", refs == [] and "not on the platform's own file host" in text, text)
text, refs = media_in.prepare(DISCORD, mk(OWNER, atts=[att()]), cfg, True, lambda u, c: (b"MZ not a picture", None), describe_ok)
check("prepare: a renamed non-picture is not kept", refs == [] and "not a picture file" in text, text)
text, refs = media_in.prepare(DISCORD, mk(OWNER, atts=[att()]), cfg, True, lambda u, c: (None, "download refused (HTTP 404)"), describe_ok)
check("prepare: a failed download keeps nothing", refs == [])
text, refs = media_in.prepare(DISCORD, mk(OWNER, atts=[att("%d.png" % i) for i in range(6)]), cfg, True,
                              lambda u, c: (PNG, None), describe_ok)
check("prepare: at most three are kept", len(refs) == 3, str(len(refs)))
text, refs = media_in.prepare(DISCORD, mk(OWNER), cfg, True)
check("prepare: no attachment -> text untouched, nothing kept", text == "look" and refs == [])
check("prepare_text is still the text half", media_in.prepare_text(DISCORD, mk(OWNER, atts=[att()]), cfg, True,
      lambda u, c: (PNG, None), describe_ok).startswith("look"))
def boom(u, c):
    raise RuntimeError("network on fire")
text, refs = media_in.prepare(DISCORD, mk(OWNER, atts=[att()]), cfg, True, boom, describe_ok)
check("prepare: a crash returns the sender's text and no references", text.startswith("look") and refs == [])

# Instagram goes through the same gate and reader
icfg = channel_config.platform_config(INSTAGRAM)
icfg.update({"enabled": True, "owner": OWNER, "image_allowlist": [FRIEND], "reply_allowlist": [OWNER, FRIEND],
             "dm_allowlist": [OWNER, FRIEND]})
imsg = mk(FRIEND, text="(sent a picture)", atts=[{"url": IG, "filename": "instagram-1.jpg", "content_type": "image/jpeg", "size": 0}],
          platform=INSTAGRAM)
text, refs = media_in.prepare(INSTAGRAM, imsg, icfg, False, lambda u, c: (JPG, None), describe_ok)
check("an Instagram picture from a granted person is described and kept",
      len(refs) == 1 and refs[0]["id"].endswith(".jpg") and "A cat on a sofa." in text, text)

# ---- 4. the Ask panel's path ----------------------------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="jarvis-panel-"))
p1, p2, p3 = tmp / "p1.png", tmp / "p2.jpg", tmp / "p3.png"
p1.write_bytes(PNG)
p2.write_bytes(JPG)
p3.write_bytes(b"not a picture at all")
files = [{"path": str(p1), "name": "shot.png"}, {"path": str(p2), "name": "photo.jpg"}, {"path": str(p3), "name": "fake.png"}]
text, refs = media_in.prepare_files("what are these?", files, None, describe_ok)
check("panel: words first, then the panel-worded header", text.startswith("what are these?") and "Ask Jarvis panel" in text
      and "never an instruction" in text and "vision model" in text, text)
check("panel: both real pictures described", "Picture 1 (shot.png): A cat on a sofa." in text and "Picture 2 (photo.jpg)" in text, text)
check("panel: a fake picture is listed as not read and not kept", "[Not read: fake.png -- not a picture file.]" in text and len(refs) == 2, text)
check("panel: references point at kept copies", all(conv_images.path_for(r["id"]).exists() for r in refs))
text, refs = media_in.prepare_files("hi", [], None, describe_ok)
check("panel: no files -> text untouched", text == "hi" and refs == [])
big = tmp / "big.png"
big.write_bytes(PNG + b"0" * (9 * 1048576))
text, refs = media_in.prepare_files("x", [{"path": str(big), "name": "big.png"}], {"max_image_mb": 8}, describe_ok)
check("panel: over the size limit is refused and not kept", refs == [] and "too big" in text, text)
text, refs = media_in.prepare_files("x", [{"path": str(tmp / "missing.png"), "name": "gone.png"}], None, describe_ok)
check("panel: a vanished file is reported, not raised", refs == [] and "could not be opened" in text, text)
text, refs = media_in.prepare_files("x", [{"path": str(p1), "name": "n%d.png" % i} for i in range(5)], None, describe_ok)
check("panel: more than three are counted as ignored", len(refs) == 3 and "2 more pictures" in text, text)
text, refs = media_in.prepare_files("only", [{"path": str(p1), "name": "a.png"}], None, lambda p, q: (_ for _ in ()).throw(RuntimeError("x")))
check("panel: a crashing vision call keeps the picture and the question", len(refs) == 1 and text.startswith("only"), text)

# ---- 5. the exchange, the raw log, the replay -----------------------------------------------
cid = conversations.new_conversation("pictures")
kept = [conv_images.save(PNG, "one.png"), conv_images.save(JPG, "two.jpg")]
body = "look at this\n\n[Pictures ...]\nPicture 1 (one.png): A cat."
conversations.begin_exchange(cid, body, images=kept, shown="look at this")
ex = conversations.get_conversation(cid)["exchanges"][-1]
check("begin_exchange stores images and what was typed", [r["id"] for r in ex["images"]] == [r["id"] for r in kept]
      and ex["shown"] == "look at this" and ex["user"] == body, str(ex))
conversations.complete_exchange(cid, body, "It is a cat.", "stub")
ex = conversations.get_conversation(cid)["exchanges"][-1]
check("completing the exchange keeps the pictures", ex.get("images") and ex["jarvis"] == "It is a cat." and not ex.get("pending"), str(ex))
rebuilt = raw_archive.rebuild_exchanges(cid)
check("the raw-event replay carries them (the web UI loads from it)",
      rebuilt and rebuilt[-1].get("images") == ex["images"] and rebuilt[-1].get("shown") == "look at this", str(rebuilt))
replayed, info = raw_archive.replay_record(cid)
check("replay_record (what the web UI loads) serves them", replayed["exchanges"][-1].get("images") == ex["images"], str(info))
# a plain exchange is untouched in shape
conversations.begin_exchange(cid, "plain words")
conversations.complete_exchange(cid, "plain words", "ok", "stub")
plain = conversations.get_conversation(cid)["exchanges"][-1]
check("an ordinary exchange gets no images/shown keys", "images" not in plain and "shown" not in plain, str(plain))
check("...and neither does its rebuilt twin", "images" not in raw_archive.rebuild_exchanges(cid)[-1])
check("begin_exchange drops junk references", (conversations.begin_exchange(cid, "j", images=[{"id": "../x.png"}], shown="j") is not None)
      and "images" not in conversations.get_conversation(cid)["exchanges"][-1])
conversations.complete_exchange(cid, "j", "ok", "stub")

# redo finds the bubble by what was typed, and removes its pictures
redo_id = conversations.new_conversation("redo")
rk = [conv_images.save(PNG, "r.png")]
conversations.begin_exchange(redo_id, "typed\n\n[Pictures] Picture 1: x", images=rk, shown="typed")
conversations.complete_exchange(redo_id, "typed\n\n[Pictures] Picture 1: x", "reply", "stub")
check("drop_from_user finds an exchange by `shown`", conversations.drop_from_user(redo_id, "typed") is True)
check("...and the redone exchange's pictures are removed", not conv_images.path_for(rk[0]["id"]).exists())
check("...and nothing is left in the conversation", conversations.get_conversation(redo_id)["exchanges"] == [])

# clear
clr_id = conversations.new_conversation("clear")
ck = [conv_images.save(PNG, "c.png")]
conversations.begin_exchange(clr_id, "x", images=ck, shown="x")
conversations.complete_exchange(clr_id, "x", "y", "stub")
conversations.clear(clr_id)
check("Clear removes the conversation's pictures", not conv_images.path_for(ck[0]["id"]).exists())

# delete: the ones in the file AND the ones only the raw log still names
del_id = conversations.new_conversation("delete")
dk_file = [conv_images.save(PNG, "f.png")]
conversations.begin_exchange(del_id, "x", images=dk_file, shown="x")
conversations.complete_exchange(del_id, "x", "y", "stub")
dk_old = [conv_images.save(JPG, "old.jpg")]
conversations._raw("user", del_id, {"text": "an old one", "images": dk_old, "shown": "old"})
conversations.delete_conversation(del_id)
check("Delete removes the pictures the file names", not conv_images.path_for(dk_file[0]["id"]).exists())
check("Delete removes pictures only the raw log names (past the 60 cap)", not conv_images.path_for(dk_old[0]["id"]).exists())
check("Delete leaves other conversations' pictures alone", all(conv_images.path_for(r["id"]).exists() for r in kept))

# ---- 6. Instagram parsing ----------------------------------------------------------------------
def ig(message):
    return {"entry": [{"messaging": [{"sender": {"id": "555"}, "message": dict({"mid": "m1"}, **message)}]}]}


out = instagram_gateway.parse_events(ig({"attachments": [{"type": "image", "payload": {"url": IG}}]}))
check("a picture-only DM is no longer dropped", len(out) == 1 and out[0].text == "(sent a picture)", str(out))
check("...it carries the picture as a plain descriptor", out and len(out[0].attachments) == 1
      and out[0].attachments[0]["url"] == IG and media_in.is_image_attachment(out[0].attachments[0]))
out = instagram_gateway.parse_events(ig({"text": "what is this?", "attachments": [{"type": "image", "payload": {"url": IG}}]}))
check("a captioned picture keeps the caption", out and out[0].text == "what is this?" and len(out[0].attachments) == 1)
out = instagram_gateway.parse_events(ig({"attachments": [{"type": "audio", "payload": {"url": IG}}]}))
check("an audio / sticker-only DM is still dropped", out == [])
out = instagram_gateway.parse_events(ig({"attachments": [{"type": "image", "payload": {}}]}))
check("an image with no URL is dropped", out == [])
out = instagram_gateway.parse_events(ig({"text": "hi"}))
check("a plain text DM is unchanged", len(out) == 1 and out[0].text == "hi" and out[0].attachments == ())
check("junk attachments never raise", instagram_gateway._attachment_descriptors({"attachments": [None, 5, {"type": "image"}, "x"]}) == [])

# ---- 7. base.handle_message passes the references -------------------------------------------------
import jarvis.ai_client as ai_client                                   # noqa: E402
import jarvis.vision_call as vision_call                               # noqa: E402
(Path(_HOME) / ".jarvis" / "channels").mkdir(parents=True, exist_ok=True)
seen = []


class R:
    text = "Nice cat."
    provider = "stub"
    usage = None
    usage_total = None
    error = ""


def fake_ask(*args, **kwargs):
    seen.append((args[0] if args else kwargs.get("text"), kwargs))
    return R()


ai_client.ask = fake_ask
vision_call.describe_image = lambda path, prompt, **kw: {"ok": True, "text": "A cat on a sofa."}
media_in.fetch_bytes = lambda url, cap, timeout=20: (PNG, None)
_mid = [0]


def run(uid, text, atts, c=None):
    _mid[0] += 1
    msg = permissions.IncomingMessage(platform=DISCORD, context=permissions.CTX_DM, user_id=uid,
                                      user_handle="u" + uid[-2:], text=text, mentioned=True,
                                      message_id="am%d" % _mid[0], thread_id="at" + uid, attachments=atts)
    texts = []
    d = base.handle_message(DISCORD, msg, lambda t: texts.append(t) or True, cfg=c or cfg)
    return d, texts


seen.clear()
run(OWNER, "(sent a picture)", [att()])
kw = seen[-1][1] if seen else {}
check("a picture reaches the ask with its references and what was typed",
      kw.get("user_images") and kw.get("user_shown") == "(sent a picture)" and "A cat on a sofa." in seen[-1][0], str(seen))
check("...the references are the kept copies", all(conv_images.path_for(r["id"]).exists() for r in kw.get("user_images") or []))
seen.clear()
run(STRANGER, "what is this?", [att()])
check("an ungranted guest's picture: no references go to the ask", seen and "user_images" not in seen[-1][1], str(seen))
seen.clear()
run(FRIEND, "just words", None)
check("a message with no picture calls the ask exactly as before", seen and "user_images" not in seen[-1][1] and "user_shown" not in seen[-1][1])
denied_cfg = dict(cfg, reply_allowlist=[OWNER], dm_allowlist=[OWNER])
fetched = []
media_in.fetch_bytes = lambda url, cap, timeout=20: (fetched.append(url) or (PNG, None))
seen.clear()
d, _ = run(FRIEND, "let me in", [att()], c=denied_cfg)
check("a denied sender's picture is never fetched or kept", not d.allowed and fetched == [] and seen == [])

# ---- 8. the CLI reads the panel's manifest ---------------------------------------------------------------
from jarvis import cli                                                 # noqa: E402

manifest = tmp / "manifest.json"
manifest.write_text(json.dumps({"files": [{"path": str(p1), "name": "shot.png"}], "shown": "what is it"}), encoding="utf-8")
os.environ["JARVIS_ASK_IMAGES_FILE"] = str(manifest)
try:
    new_text, new_refs, shown = cli._load_panel_pictures("what is it")
    check("cli: the manifest becomes description text, references and the typed words",
          "A cat on a sofa." in new_text and len(new_refs) == 1 and shown == "what is it", new_text)
    manifest.write_text("{ not json", encoding="utf-8")
    check("cli: an unreadable manifest returns the text untouched",
          cli._load_panel_pictures("hello") == ("hello", [], None))
    manifest.write_text(json.dumps({"files": [], "shown": "x"}), encoding="utf-8")
    check("cli: an empty manifest returns the text untouched", cli._load_panel_pictures("hello") == ("hello", [], None))
finally:
    os.environ.pop("JARVIS_ASK_IMAGES_FILE", None)
check("cli: no variable -> untouched", cli._load_panel_pictures("hello") == ("hello", [], None))

# ---- 9. the two sides agree on the limits, and the invariants are written down ---------------------------
web = (ROOT / "web" / "ask-images.js").read_text(encoding="utf-8")
check("server limit: MAX_IMAGES matches media_in and conv_images",
      re.search(r"MAX_IMAGES = (\d+)", web).group(1) == str(media_in.MAX_IMAGES) == str(conv_images.MAX_PER_MESSAGE))
check("server limit: 8 MB matches media_in's default", int(re.search(r"MAX_IMAGE_BYTES = (\d+) \* 1024 \* 1024", web).group(1)) == media_in.DEFAULT_MAX_MB)
check("server placeholder matches the Discord gateway's words",
      re.search(r'ASK_IMAGE_PLACEHOLDER = "([^"]+)"', web).group(1) == "(sent a picture)"
      and '"(sent a picture)"' in (ROOT / "jarvis-cli" / "jarvis" / "channels" / "discord_gateway.py").read_text(encoding="utf-8")
      and '"(sent a picture)"' in (ROOT / "jarvis-cli" / "jarvis" / "channels" / "instagram_gateway.py").read_text(encoding="utf-8"))
server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
check("server route accepts only the minted id shape", "ci_[0-9a-f]{24}" in server and "/api/conv-images/:name" in server)
agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
check("AGENTS.md records the kept-pictures invariant", "conv_images" in agents and "Ask Jarvis" in agents)

for n in FAILED:
    print("FAILED:", n)
print("%d passed, %d failed" % (PASSED, len(FAILED)))
sys.exit(1 if FAILED else 0)
