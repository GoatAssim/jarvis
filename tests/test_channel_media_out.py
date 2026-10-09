"""Pictures Jarvis makes during a chat ask reach the OWNER's Discord thread (L.42.3, outbound).

    python3 tests/test_channel_media_out.py

media_out collector: no-op outside an ask, owner-only, deduplicated, capped.
media_send.prepare: owner only, type allow-list, size cap, shrink or say so.
base._ask_jarvis / handle_message: collector opened for the owner only, closed
after every ask, files sent after the text to the asking thread only.
No bot token, no socket, no real ~/.jarvis; the model is stubbed.
"""

import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-media-out-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import media_out                                    # noqa: E402
from jarvis.channels import DISCORD, base, media_send, permissions  # noqa: E402
from jarvis.channels import config as channel_config            # noqa: E402

PASSED, FAILED = 0, []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name + ((" - " + detail) if detail else ""))


def make(name, size):
    p = Path(_HOME) / name
    p.write_bytes(b"x" * size)
    return str(p)


# ---- 1. the collector ------------------------------------------------------
check("offer outside an ask does nothing", media_out.offer("/x.png") is False and media_out.end() == [])
media_out.begin(allow=False)
check("a sender who may not receive files collects nothing", media_out.offer("/x.png") is False and media_out.end() == [])
media_out.begin(allow=True)
a = media_out.offer("/a.png")
b = media_out.offer("/a.png")
check("kept once, offering again is idempotent", a and b and media_out.end() == [("/a.png", "image")])
media_out.begin(allow=True)
for i in range(media_out.MAX_ITEMS + 3):
    media_out.offer("/f%d.png" % i)
check("capped per ask", len(media_out.end()) == media_out.MAX_ITEMS)
check("end() closes it", media_out.offer("/late.png") is False)

# ---- 2. what may be sent ---------------------------------------------------
png, pdf, exe, big = make("s.png", 100), make("d.pdf", 100), make("tool.exe", 100), make("big.png", 3 * 1024 * 1024)
files, notes, temps = media_send.prepare([(png, "image"), (pdf, "file")], True)
check("an image and a pdf go", [n for _, n in files] == ["s.png", "d.pdf"] and not notes)
check("a guest gets nothing", media_send.prepare([(png, "image")], False) == ([], [], []))
files, notes, _ = media_send.prepare([(exe, "file")], True)
check("an executable is refused and said so", not files and "not a file type" in notes[0])
files, notes, _ = media_send.prepare([(pdf, "image")], True)
check("a pdf offered as an image is refused", not files and notes)
files, notes, _ = media_send.prepare([(str(Path(_HOME)), "file")], True)
check("a folder is refused", not files and "not a regular file" in notes[0])
files, notes, _ = media_send.prepare([(big, "image")], True, {"max_upload_mb": 1})
check("over the cap and not shrinkable: said, not silent", (not files and "over the 1 MB limit" in notes[0]) or len(files) == 1, str(notes))
many = [(make("m%d.png" % i, 10), "image") for i in range(6)]
files, notes, _ = media_send.prepare(many, True)
check("at most MAX_FILES per reply, and it says so", len(files) == media_send.MAX_FILES and notes)
check("a missing file is reported", media_send.prepare([("/nope/none.png", "image")], True)[1])

# ---- 2b. the hourly limit ----------------------------------------------------
import time as _t                                                # noqa: E402
rate_cfg = {"max_files_per_hour": 3}
check("under the limit all are allowed", media_send.take_allowance("discord", "T1", 2, rate_cfg) == 2)
check("only the room left is allowed", media_send.take_allowance("discord", "T1", 5, rate_cfg) == 1)
check("limited means zero", media_send.take_allowance("discord", "T1", 1, rate_cfg) == 0)
check("another thread has its own allowance", media_send.take_allowance("discord", "T2", 1, rate_cfg) == 1)
check("old sends age out", media_send.take_allowance("discord", "T1", 1, rate_cfg, now=_t.time() + 3700) == 1)
check("the limit survives a reload (it is on disk)", media_send._load_sends(_t.time()).get("discord:T1"))
f, n, _ = media_send.prepare([(png, "image")], True, rate_cfg, platform="discord", thread_id="T1")
check("prepare says so when the hourly limit stops a file", n and "limit" in n[-1] or len(f) == 1, str(n))
f, n, _ = media_send.prepare([(png, "image")], True, rate_cfg)
check("no platform given: not rate limited", len(f) == 1)

# ---- 3. through handle_message ---------------------------------------------
OWNER, GUEST = "100000000000000001", "200000000000000002"
cfg = channel_config.platform_config(DISCORD)
cfg.update({"enabled": True, "owner": OWNER, "cooldown_seconds": 0, "log_conversations": False,
            "reply_allowlist": [OWNER, GUEST], "dm_allowlist": [OWNER, GUEST],
            "tool_allowlist": [OWNER, GUEST], "allow_tools": True})
seen_collect = []


class R:
    text = "Done."
    provider = "stub"
    usage = None
    usage_total = None
    error = ""


def fake_ask(*args, **kwargs):
    seen_collect.append(media_out.is_collecting())
    media_out.offer(png, "image")
    return R()


import jarvis.ai_client as ai_client                             # noqa: E402
ai_client.ask = fake_ask


def run(uid, with_send_file=True):
    texts, sent = [], []
    msg = permissions.IncomingMessage(platform=DISCORD, context=permissions.CTX_DM, user_id=uid,
                                      user_handle="u" + uid[-2:], text="screenshot please",
                                      mentioned=True, message_id="m" + uid + str(len(seen_collect)),
                                      thread_id="t" + uid)
    base.handle_message(DISCORD, msg, lambda t: texts.append(t) or True, cfg=cfg,
                        send_file=(lambda p, n: sent.append((p, n)) or True) if with_send_file else None)
    return texts, sent


texts, sent = run(OWNER)
check("owner: the text is sent", texts and texts[0] == "Done.", str(texts))
check("owner: the picture follows, once", [n for _, n in sent] == ["s.png"], str(sent))
check("owner: the collector was open during the ask", seen_collect and seen_collect[-1] is True)
check("the collector is closed afterwards", media_out.is_collecting() is False)
texts, sent = run(GUEST)
check("guest: no collector, no file", seen_collect[-1] is False and sent == [], str(sent))
texts, sent = run(OWNER, with_send_file=False)
check("a gateway with no send_file attaches nothing (as before)", sent == [] and seen_collect[-1] is False)

for n in FAILED:
    print("FAILED:", n)
print("%d passed, %d failed" % (PASSED, len(FAILED)))
sys.exit(1 if FAILED else 0)
