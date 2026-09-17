"""Export a conversation to Markdown / HTML / PDF / JSON.

THE MISSING THIRD LEG
---------------------
conv_search.py finds a conversation and the Logs viewer shows its raw
traffic, but there has never been a way to get one *out*. That matters more
than it sounds: a long debugging session with Jarvis is often the best
write-up of a problem that exists anywhere, and it is currently trapped in
~/.jarvis/conversations/<id>.json, in a shape (pending flags, extras arrays,
provider labels) nobody would want to read.

FORMATS, AND WHY THESE FOUR
---------------------------
    md     the default. Pasteable into an issue, a PR, a wiki, a notebook.
    html   self-contained, styled, opens in a browser, prints cleanly.
    json   the raw record, for anything programmatic.
    pdf    what people ask for when they mean "a file I can send someone".

PDF WITHOUT A PDF LIBRARY
-------------------------
reportlab and weasyprint are both large dependencies for something most
users will do once. So PDF is produced by rendering the HTML and asking a
browser to print it — headless Chrome/Edge if one is on the system, which on
Windows is essentially always. When none is available the export does NOT
fail: it writes the HTML and says plainly that the PDF step needs a browser
and where the HTML is. A partial success you can act on beats a clean
failure.

WHAT GETS INCLUDED
------------------
By default: the exchanges, in order, with timestamps. Optionally the tool
calls (`include_tools`), the thinking traces (`include_thinking`), and the
per-turn reasoning trace (`include_trace`). All three default OFF, because
the common case is "share what we worked out", not "share the machinery" —
and because a thinking trace can contain the model working through
half-formed ideas the user never saw and may not want to publish.
"""

import html
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import conversations

FORMATS = ("md", "html", "json", "pdf", "txt")
DEFAULT_FORMAT = "md"
MAX_EXCHANGES = 500

# Tried in order. The first that exists is used.
_BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "google-chrome", "chromium", "chromium-browser", "msedge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def _safe_slug(text, fallback="conversation"):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "")).strip("-").lower()[:50]
    return slug or fallback


def _ts(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value)[:16]


def _extras_of(exchange, kind):
    return [e.get("data") or {} for e in (exchange.get("extras") or [])
            if isinstance(e, dict) and e.get("type") == kind]


def render_markdown(record, include_tools=False, include_thinking=False,
                    include_trace=False, assistant_name="Jarvis"):
    title = record.get("title") or "Conversation"
    lines = [
        "# %s" % title,
        "",
    ]
    gist = (record.get("soft_context") or "").strip()
    if gist:
        lines += ["> %s" % gist, ""]
    lines += [
        "*%d exchanges · started %s · exported %s*" % (
            len(record.get("exchanges") or []),
            _ts(record.get("created_at")),
            datetime.now().strftime("%Y-%m-%d %H:%M")),
        "",
        "---",
        "",
    ]

    for exchange in (record.get("exchanges") or [])[:MAX_EXCHANGES]:
        stamp = _ts(exchange.get("ts"))
        lines.append("### You%s" % (" · %s" % stamp if stamp else ""))
        lines.append("")
        lines.append((exchange.get("user") or "").strip() or "*(empty)*")
        lines.append("")

        if exchange.get("pending"):
            lines += ["### %s" % assistant_name, "",
                      "*(no answer — this turn was interrupted)*", "", "---", ""]
            continue

        if include_thinking:
            for data in _extras_of(exchange, "thinking"):
                text = (data.get("text") or "").strip()
                if text:
                    lines += ["<details><summary>Thinking (%s)</summary>"
                              % (data.get("level") or "on"), "",
                              "```", text, "```", "", "</details>", ""]

        if include_tools:
            calls = _tool_calls_of(exchange)
            if calls:
                lines.append("<details><summary>%d tool call%s</summary>"
                             % (len(calls), "" if len(calls) == 1 else "s"))
                lines.append("")
                for call in calls:
                    lines.append("- `%s(%s)`" % (call["name"], call["args"]))
                lines += ["", "</details>", ""]

        provider = exchange.get("provider")
        header = "### %s" % assistant_name
        if provider:
            header += " · %s" % provider
        lines.append(header)
        lines.append("")
        lines.append((exchange.get("jarvis") or "").strip() or "*(no reply)*")
        lines.append("")

        if include_trace:
            for data in _extras_of(exchange, "trace"):
                for line in (data.get("lines") or []):
                    lines.append("> %s" % line)
                lines.append("")

        lines += ["---", ""]

    return "\n".join(lines).rstrip() + "\n"


def _tool_calls_of(exchange):
    """Tool calls recorded for a turn, from whichever extra carries them.

    The `trace` extra has a full step list; older turns only have the
    side-effect extras (`screenshot`, `download`, ...). Preferring trace and
    falling back keeps an export of an old conversation useful instead of
    empty.
    """
    calls = []
    for data in _extras_of(exchange, "trace"):
        for step in data.get("steps") or []:
            calls.append({"name": step.get("name") or "?",
                          "args": step.get("detail") or ""})
    if calls:
        return calls
    for extra in exchange.get("extras") or []:
        if isinstance(extra, dict) and extra.get("type") not in ("trace", "thinking", "console"):
            calls.append({"name": extra.get("type") or "?", "args": ""})
    return calls


_HTML_SHELL = """<!doctype html>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font: 15px/1.6 -apple-system, "Segoe UI", Roboto, sans-serif;
         max-width: 780px; margin: 40px auto; padding: 0 20px; color: #16202b; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
  .meta {{ color: #6b7a89; font-size: 0.85rem; margin-bottom: 28px; }}
  .gist {{ border-left: 3px solid #0a6ed1; padding-left: 12px; color: #44566b;
           margin: 12px 0 20px; }}
  .turn {{ margin-bottom: 26px; }}
  .role {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: .08em;
           color: #8a97a4; margin-bottom: 5px; }}
  .bubble {{ white-space: pre-wrap; word-wrap: break-word; }}
  .user .bubble {{ background: #f1f5f9; border-radius: 10px; padding: 11px 14px; }}
  details {{ margin: 8px 0; font-size: 0.85rem; color: #55636f; }}
  summary {{ cursor: pointer; }}
  pre {{ background: #f6f8fa; padding: 10px; border-radius: 8px;
         overflow-x: auto; font-size: 0.8rem; white-space: pre-wrap; }}
  .trace {{ border-left: 2px solid #d5dde5; padding-left: 10px; color: #6b7a89;
            font-size: 0.82rem; margin-top: 8px; }}
  hr {{ border: none; border-top: 1px solid #e6ebf0; margin: 24px 0; }}
  @media print {{ body {{ margin: 0; max-width: none; }} details {{ display: none; }} }}
</style>
<h1>{title}</h1>
{gist}
<div class="meta">{meta}</div>
{body}
"""


def render_html(record, include_tools=False, include_thinking=False,
                include_trace=False, assistant_name="Jarvis"):
    esc = html.escape
    chunks = []
    for exchange in (record.get("exchanges") or [])[:MAX_EXCHANGES]:
        stamp = _ts(exchange.get("ts"))
        chunks.append('<div class="turn user"><div class="role">You%s</div>'
                      '<div class="bubble">%s</div></div>'
                      % (" &middot; " + esc(stamp) if stamp else "",
                         esc((exchange.get("user") or "").strip())))

        if exchange.get("pending"):
            chunks.append('<div class="turn"><div class="role">%s</div>'
                          '<div class="bubble"><em>no answer — interrupted</em>'
                          '</div></div><hr>' % esc(assistant_name))
            continue

        if include_thinking:
            for data in _extras_of(exchange, "thinking"):
                text = (data.get("text") or "").strip()
                if text:
                    chunks.append("<details><summary>Thinking (%s)</summary>"
                                  "<pre>%s</pre></details>"
                                  % (esc(str(data.get("level") or "on")), esc(text)))

        if include_tools:
            calls = _tool_calls_of(exchange)
            if calls:
                items = "".join("<li><code>%s(%s)</code></li>"
                                % (esc(c["name"]), esc(str(c["args"]))) for c in calls)
                chunks.append("<details><summary>%d tool call%s</summary><ul>%s</ul></details>"
                              % (len(calls), "" if len(calls) == 1 else "s", items))

        role = esc(assistant_name)
        if exchange.get("provider"):
            role += " &middot; " + esc(str(exchange["provider"]))
        chunks.append('<div class="turn"><div class="role">%s</div>'
                      '<div class="bubble">%s</div>' % (role,
                      esc((exchange.get("jarvis") or "").strip())))

        if include_trace:
            for data in _extras_of(exchange, "trace"):
                lines = "".join("<div>%s</div>" % esc(line)
                                for line in (data.get("lines") or []))
                if lines:
                    chunks.append('<div class="trace">%s</div>' % lines)
        chunks.append("</div><hr>")

    gist = (record.get("soft_context") or "").strip()
    return _HTML_SHELL.format(
        title=esc(record.get("title") or "Conversation"),
        gist='<div class="gist">%s</div>' % esc(gist) if gist else "",
        meta="%d exchanges &middot; started %s &middot; exported %s" % (
            len(record.get("exchanges") or []), esc(_ts(record.get("created_at"))),
            datetime.now().strftime("%Y-%m-%d %H:%M")),
        body="\n".join(chunks),
    )


def _find_browser():
    for candidate in _BROWSERS:
        if "/" in candidate or "\\" in candidate:
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def _html_to_pdf(html_text, out_path):
    """Headless-browser print-to-PDF. (ok, detail)."""
    browser = _find_browser()
    if not browser:
        return False, ("no Chrome/Edge/Chromium found to print with")
    tmp = Path(tempfile.gettempdir()) / ("jarvis_export_%d.html" % abs(hash(html_text)) % 10**8)
    try:
        tmp.write_text(html_text, encoding="utf-8")
        result = subprocess.run(
            [browser, "--headless", "--disable-gpu", "--no-sandbox",
             "--print-to-pdf=%s" % out_path, "--no-pdf-header-footer",
             tmp.as_uri()],
            capture_output=True, timeout=90,
        )
        if out_path.exists() and out_path.stat().st_size > 0:
            return True, ""
        return False, (result.stderr or b"").decode("utf-8", "replace")[:200] or "no output"
    except subprocess.TimeoutExpired:
        return False, "the browser took too long"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def export(conv_id, fmt=DEFAULT_FORMAT, out_dir=None, include_tools=False,
           include_thinking=False, include_trace=False, assistant_name="Jarvis"):
    """Write one conversation to a file. Always returns a dict, never raises."""
    fmt = (fmt or DEFAULT_FORMAT).strip().lower().lstrip(".")
    if fmt not in FORMATS:
        return {"ok": False, "error": "format must be one of: %s" % ", ".join(FORMATS)}
    if not conversations.is_valid_id(conv_id):
        return {"ok": False, "error": "invalid conversation id"}
    record = conversations.get_conversation(conv_id)
    if not record:
        return {"ok": False, "error": "no such conversation"}

    out_dir = Path(out_dir).expanduser() if out_dir else (Path.home() / "Downloads")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        out_dir = Path(tempfile.gettempdir())

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    base = "jarvis-%s-%s" % (_safe_slug(record.get("title")), stamp)
    kwargs = dict(include_tools=include_tools, include_thinking=include_thinking,
                  include_trace=include_trace, assistant_name=assistant_name)

    try:
        if fmt == "json":
            path = out_dir / (base + ".json")
            path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        elif fmt == "html":
            path = out_dir / (base + ".html")
            path.write_text(render_html(record, **kwargs), encoding="utf-8")
        elif fmt == "txt":
            path = out_dir / (base + ".txt")
            plain = re.sub(r"[#*>`]", "", render_markdown(record, **kwargs))
            path.write_text(plain, encoding="utf-8")
        elif fmt == "pdf":
            html_text = render_html(record, **kwargs)
            path = out_dir / (base + ".pdf")
            ok, detail = _html_to_pdf(html_text, path)
            if not ok:
                # Degrade to HTML rather than failing outright — the user
                # still gets a shareable, printable file and a clear reason.
                fallback = out_dir / (base + ".html")
                fallback.write_text(html_text, encoding="utf-8")
                return {"ok": True, "format": "html", "path": str(fallback),
                        "title": record.get("title"),
                        "exchanges": len(record.get("exchanges") or []),
                        "warning": "Couldn't make a PDF (%s), so this is HTML — "
                                   "open it and print to PDF from the browser." % detail}
        else:
            path = out_dir / (base + ".md")
            path.write_text(render_markdown(record, **kwargs), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": "couldn't write the file: %s" % exc}

    return {"ok": True, "format": fmt, "path": str(path),
            "title": record.get("title"),
            "exchanges": len(record.get("exchanges") or []),
            "bytes": path.stat().st_size if path.exists() else 0}
