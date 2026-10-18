"""The process the yt-dlp engine starts for one download (plan 7.1, Level A).

    python -m jarvis.hub.engines._ytdlp_worker        (arguments as one JSON object on stdin)

It calls the existing `ytdl_tools.ytdl_download(arguments)` unchanged and prints exactly one
line on stdout:

    @@JARVIS_YTDL_RESULT@@ {json of ytdl_download's result}

Why a process and not a thread: `ytdl_download` is synchronous and returns only at the end, so
a thread can't be stopped.  A process can be killed, which is what makes pause, cancel and a
daemon shutdown work for yt-dlp; yt-dlp keeps its `.part` files, so the next run continues.

This module starts with an underscore on purpose: engines/__init__.py skips it, so it is never
mistaken for an engine.  Nothing else imports it.
"""

import json
import sys

MARKER = "@@JARVIS_YTDL_RESULT@@"


def main():
    try:
        arguments = json.loads(sys.stdin.read() or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": "bad worker input: %s" % exc}
    else:
        try:
            from jarvis import ytdl_tools
            result = ytdl_tools.ytdl_download(arguments)
        except Exception as exc:  # noqa: BLE001 - always answer with a result line
            result = {"ok": False, "error": "download failed: %s" % exc}
    try:
        line = json.dumps(result, default=str)
    except Exception:  # noqa: BLE001
        line = json.dumps({"ok": False, "error": "the result could not be encoded"})
    sys.stdout.write("\n%s %s\n" % (MARKER, line))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
