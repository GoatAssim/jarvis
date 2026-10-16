"""The one seam between jarvis and a front-end that is hosting it in-process.

jarvis is, first of all, a command-line program: every plain `jarvis ...`
invocation is its own OS process, prints to its own stdout/stderr and is done.
That is still exactly what happens, and nothing in here changes it.

The web UI used to start one of those processes for every single request (an
ask, a panel refresh, the scheduler's 30-second tick) and parse what came back
out of two text pipes. `bridge.py` (`jarvis _internal_bridge`) is the other
way to be run: one warm process that executes those same requests one after
another, in-process, and talks to its caller over a typed message channel.
When -- and only when -- that host is running, it registers itself here.

So this module is deliberately tiny and one-directional:

  * `cli.py` (and anything else that wants to tell a front-end *what kind* of
    thing it is printing) calls `trace()` / `reply()` / `media()`;
  * with no host installed they are exactly `print(..., file=sys.stderr)` and a
    no-op -- byte-for-byte what the code did before this module existed, so the
    plain CLI never depends on a host, a server or a socket;
  * with a host installed, a trace line carries its category in-band, a media
    card (screenshot, download, ui, ...) carries its own in-band tag, and the
    final reply is also announced as a typed event.

Nothing here imports `bridge`, `web/` or anything else. The dependency runs the
other way: the host knows about jarvis, jarvis does not know about the host.

THE IN-BAND CATEGORY TAG
------------------------
A trace line's category has to reach the front-end in the SAME order as every
other stderr line (a tool's own output, a provider's error, a subprocess), so
it travels inside the stderr byte stream rather than on a second channel:

    <RS>category<US>the visible text of the line

(`\\x1e` and `\\x1f`, the ASCII record/unit separators; no real trace line starts
with either.) `bridge._PipeReader` strips the tag back off and reports the
category as a field of the event. A reader that does not know about the tag
would show it as two invisible characters and a word, which is why `trace()`
only writes it when a host is installed.

MEDIA CARDS
-----------
`media(kind, *fields)` is the ONE way code announces a card for the front end
(a screenshot, a download, a present_file card, a notification, a tool's UI
event, a dev_agent step, ...). Not hosted, it prints exactly the line every
producer printed before this existed:

    JARVIS_MEDIA<TAB>kind<TAB>field<TAB>field...        (stderr)

Hosted, the same line goes out with a tag in front, in the same stderr byte
stream (so a card stays in order with the trace lines around it):

    <RS>media:<nonce><US>JARVIS_MEDIA<TAB>kind<TAB>field...

and the worker turns it into a typed `media` event. The difference from a trace
category is that the tag carries a NONCE the host made up for the running
request and keeps to itself (`host.media_tag()`). It is never put in the
environment, so a shell command or a subprocess a tool starts can print the bare
`JARVIS_MEDIA` line, or even a bare `<RS>media<US>` tag, and it is only ever an
ordinary stderr line -- it cannot draw a card. A reader that does not recognise
the tag shows it as plain text, which is why the tag is only written when a host
is installed AND has a tag to give (no request running means no tag).

STDOUT MARKERS
---------------
The same idea for the four machine lines that go on STDOUT: `stream(ev)`,
`usage(u)`, `confirm(payload)` and `ui_request(req)`. Not hosted each prints the
legacy line (`JARVIS_STREAM {json}`, `JARVIS_USAGE {json}`,
`JARVIS_CONFIRM_REQUEST {json}`, `JARVIS_UI_REQUEST {json}`), byte-for-byte what
the producer printed before. Hosted, the line goes out behind the in-band tag
`<RS>marker:<nonce><US>` and the worker turns it into the typed event it has
always emitted (with `raw` = the legacy line). The point is the reply: it is
printed on stdout too, and it is model output, so a reply that contains a line
starting `JARVIS_CONFIRM_REQUEST ` used to be taken for a real request. A hosted
worker now only believes a tagged line; an untagged one is an ordinary `out`
line, whatever it says. The tag is only written when the line really goes to the
process's own stdout (not to a stream a caller passed in).

Categories are the ids of the Ask panel's Console filter
(web/public/app.js, ASK_CONSOLE_CATEGORIES): tool-call, tool-result, tokens,
discovery, provider, narration, status, error. A line printed without a
category is simply uncategorised; the browser keeps classifying those from
their text, exactly as before.
"""

import json
import sys

CAT_OPEN = "\x1e"
CAT_CLOSE = "\x1f"

# The ids a caller may use. A typo'd id is dropped rather than shipped, so a
# front-end never has to cope with a category it has no filter for.
CATEGORIES = frozenset({
    "tool-call", "tool-result", "tokens", "discovery",
    "provider", "narration", "status", "error",
})

# What a media line is tagged with is NOT in CATEGORIES (those are Console-filter
# ids); the full tag is `media:<nonce>`, see the docstring.
MEDIA_TAG = "media"
MEDIA_PREFIX = "JARVIS_MEDIA"

# The four stdout markers: kind -> legacy line prefix. The in-band tag for all of them is
# `marker:<nonce>` (see the docstring).
MARKER_TAG = "marker"
MARKER_PREFIXES = {
    "stream": "JARVIS_STREAM ",
    "usage": "JARVIS_USAGE ",
    "confirm": "JARVIS_CONFIRM_REQUEST ",
    "ui": "JARVIS_UI_REQUEST ",
}

_host = None  # the object bridge.serve() installs while it is hosting requests


def install(host):
    """Register the in-process host. Only bridge.serve() calls this."""
    global _host
    _host = host


def uninstall():
    global _host
    _host = None


def active():
    """True while a host is running this process's requests."""
    return _host is not None


def trace(text, cat=None):
    """Print one stderr trace line, tagged with its category when hosted.

    Without a host this is `print(text, file=sys.stderr, flush=True)` and
    nothing else.
    """
    if _host is not None and cat in CATEGORIES:
        text = CAT_OPEN + cat + CAT_CLOSE + text
    print(text, file=sys.stderr, flush=True)


def reply(name, text, ok=True):
    """Announce the finished reply of an ask as a typed event (hosted only).

    The reply is ALSO printed to stdout by the caller, exactly as before; this
    is the same text without the "Name: " prefix, so a front-end that wants the
    bubble does not have to find it by pattern-matching stdout. Never raises.
    """
    host = _host
    if host is None:
        return
    try:
        host.reply(str(name or ""), "" if text is None else str(text), bool(ok))
    except Exception:  # noqa: BLE001 -- announcing a reply must never break one
        pass


def media(kind, *fields):
    """Announce a card for the front end (screenshot, download, ui, ...).

    Prints one stderr line: `JARVIS_MEDIA<TAB>kind<TAB>field<TAB>...`. Without a
    host that is all it does, byte-for-byte the line the producers used to print
    themselves. With one, the line carries the host's per-request tag in front
    (see the module docstring) so it stays in stderr order and cannot be forged
    by ordinary output.

    Fields must not contain a tab or a newline (the legacy rule: JSON payloads
    already escape both, and the other producers strip them). Never raises
    for a host that misbehaves; a failed print is the caller's to handle, as it
    was with the bare `print`.
    """
    line = "\t".join([MEDIA_PREFIX, str(kind), *(str(f) for f in fields)])
    host = _host
    if host is not None:
        tag = None
        try:
            tag = host.media_tag()
        except Exception:  # noqa: BLE001 -- a host with no tag to give: plain line
            tag = None
        if tag:
            line = CAT_OPEN + tag + CAT_CLOSE + line
    print(line, file=sys.stderr, flush=True)


def _marker(kind, obj, out=None, *, ensure_ascii=True, default=str):
    """Write one stdout marker line (`<prefix><json>\n`) and flush.

    `out` is where the legacy code wrote (None = this process's stdout). The in-band tag
    is added only when a host is installed, has a tag to give, AND the line goes to the
    process's own stdout -- a caller that passed its own stream (a test, a sink) gets the
    plain legacy line it always got. Raises what json.dumps / the stream raises, like the
    print this replaced; callers that guarded it still guard it.
    """
    body = MARKER_PREFIXES[kind] + json.dumps(obj, default=default, ensure_ascii=ensure_ascii)
    target = sys.stdout if out is None else out
    host = _host
    if host is not None and target is sys.stdout:
        tag = None
        try:
            tag = host.marker_tag()
        except Exception:  # noqa: BLE001 -- a host with no tag to give: plain line
            tag = None
        if tag:
            body = CAT_OPEN + tag + CAT_CLOSE + body
    target.write(body + "\n")
    target.flush()


def stream(ev, out=None):
    """A live model event for the front end (`JARVIS_STREAM {json}`)."""
    _marker("stream", ev, out, ensure_ascii=False, default=None)


def usage(u):
    """What the ask cost (`JARVIS_USAGE {json}`)."""
    _marker("usage", u, ensure_ascii=False)


def confirm(payload):
    """A tool needs a yes/no (`JARVIS_CONFIRM_REQUEST {json}`); the answer is one stdin line."""
    _marker("confirm", payload)


def ui_request(req):
    """A tool asks the front end a question (`JARVIS_UI_REQUEST {json}`); one stdin line back."""
    _marker("ui", req)
