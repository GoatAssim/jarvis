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
    thing it is printing) calls `trace()` / `reply()`;
  * with no host installed they are exactly `print(..., file=sys.stderr)` and a
    no-op -- byte-for-byte what the code did before this module existed, so the
    plain CLI never depends on a host, a server or a socket;
  * with a host installed, a trace line carries its category in-band and the
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

Categories are the ids of the Ask panel's Console filter
(web/public/app.js, ASK_CONSOLE_CATEGORIES): tool-call, tool-result, tokens,
discovery, provider, narration, status, error. A line printed without a
category is simply uncategorised; the browser keeps classifying those from
their text, exactly as before.
"""

import sys

CAT_OPEN = "\x1e"
CAT_CLOSE = "\x1f"

# The ids a caller may use. A typo'd id is dropped rather than shipped, so a
# front-end never has to cope with a category it has no filter for.
CATEGORIES = frozenset({
    "tool-call", "tool-result", "tokens", "discovery",
    "provider", "narration", "status", "error",
})

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
