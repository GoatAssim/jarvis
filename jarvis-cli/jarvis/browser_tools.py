"""Browser control via Playwright for Jarvis — a stateful, authenticated,
JS-executing session, as opposed to web_tools.py's stateless single-GET
page fetch. See jarvis-master-plan.md Part C for the full design writeup;
this docstring only covers what isn't obvious from the code.

SESSION MODEL (v1 — "process lifetime = one ask")
---------------------------------------------------
Two things have different lifetimes:

  * Login state (cookies, local storage) lives in an on-disk Playwright
    persistent profile directory (~/.jarvis/browser-profile) and survives
    indefinitely — across asks and across `jarvis` restarts. That's just a
    directory; Playwright owns it.

  * The running Chromium *process* is scoped to one ask.

The master plan's suggested hook for "closed when the ask ends" is
threading a handle through ai_client.ask()'s tool loop with a `finally`.
This module does something simpler that lands at the same behavior: since
`jarvis` is a brand-new OS process on every CLI invocation (see
history.py's module docstring) and web/server.js shells out to `jarvis`
the same way, one OS process already IS one ask everywhere in this
codebase — there's no separate "ask ended" event to hook that isn't just
"this process is exiting". So the browser context is a module-level
singleton, opened lazily on the first browser_* call and kept alive for
every later browser_* call in the same process (no repeated ~1-2s
Chromium startup within one multi-step flow), and `atexit` closes it
unconditionally when the process exits — success, error, or an uncaught
exception all funnel through the same interpreter shutdown atexit hooks
into. This is deliberately NOT wired into ai_client.py at all, so this
patch never touches a file the router/confirmation work already has open.

Known gap (documented, not silently accepted): a hard kill of the jarvis
process — SIGKILL, or server.js's Stop button on Windows, which the
comment above ai_client.ask()'s conversation-persistence call notes is an
unconditional `taskkill /F` — skips atexit the same way it skips every
other Python cleanup hook, so the Chromium child can outlive its parent
until the OS reaps it in that one case. Nothing here makes that worse than
any other subprocess-owning tool already in this codebase. This is still
true of the v1 path below even now that v2 exists (see next section):
v1's session is a bare module-level singleton with no supervisor watching
it, unlike the daemon.

SESSION MODEL (v2 — the warm daemon, browser_daemon.py, OFF BY DEFAULT)
-------------------------------------------------------------------------
`jarvis browser-daemon` (a `daemons.py` BUILTIN, same footing as
`clipboard-watch`) holds its own persistent-context session open across
every ask, not just the calls within one, and exposes it over a loopback
TCP socket (port written to ~/.jarvis/browser-daemon.port). A background
thread inside the daemon closes the browser (not the daemon process
itself) after `browser_daemon_idle_seconds` (config, default 600s) of no
requests — the "always-warm... with an idle-timeout auto-close" the
master plan asked for: warm while actually being used across a run of
asks, freed automatically once it stops being useful, and never a mystery
process someone finds days later. The daemon process itself keeps
listening either way; a request after an idle-close just re-pays the
~1-2s Chromium startup once, exactly like a fresh v1 ask does today.

Every `tool_browser_*` function below (see "public entry points" near the
bottom) tries the daemon first ONLY when the `browser_warm_daemon` config
flag is on (`ai_config.json`'s `defaults` dict, same pattern as
`browser_headless` above) and only uses the result if the daemon actually
answers — `_call_daemon()` never raises and a short connect timeout means
a stopped or stale daemon is indistinguishable from "not installed" to
every caller. So flipping `browser_warm_daemon` on can never make a
browser_* call behave worse than the v1 fallback already does; it is
purely additive. The flag defaults OFF: the daemon has to be started
explicitly (`daemon_start`/`daemon-start browser`, or the AI's own
`daemon_start` tool) and the config flag has to be turned on by hand —
this is deliberately not something the model can silently switch on
itself, same spirit as `browser_headless`.

DEPENDENCY STORY
------------------
Playwright is a ~300MB-with-browser-binary optional dependency, so it is
never imported at module top level — only lazily, inside the one helper
that needs it. This mirrors how audio_tools.py guards its Windows-only
code with a sys.platform check instead of assuming the environment: a
missing dependency degrades every tool below to a clean {"ok": False,
"error": ...} pointing at `jarvis browser-setup`, rather than crashing
tools.py's whole import chain for anyone who hasn't installed it.

SELECTOR STRATEGY
-------------------
Every tool that names an element takes a plain-English `description`
("the Submit button"), never a CSS selector — see _find_locator(), which
tries Playwright's accessibility-first locators in order (role, then
label, then text) before falling back to treating the string as a literal
selector, and only does that fallback when the string already looks like
one. A model reasoning from text, with no live view of the DOM, can
describe *what an element is* far more reliably than it can invent a
correct selector for markup it has never seen.
"""

import atexit
import json
import secrets
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

JARVIS_DIR = Path.home() / ".jarvis"
PROFILE_DIR = JARVIS_DIR / "browser-profile"
ENCODING = "utf-8"

# v2 warm daemon (see browser_daemon.py and master plan Part C "v2 warm
# daemon"). `jarvis browser-daemon` writes its loopback port here on
# startup and removes it on clean shutdown; a stale file (process killed
# without cleanup) just means _call_daemon()'s connect attempt fails and
# every tool below falls back to its own local, process-lifetime session
# exactly as it did before this feature existed.
DAEMON_PORT_FILE = JARVIS_DIR / "browser-daemon.port"
_DAEMON_CONNECT_TIMEOUT = 0.3
_DAEMON_CALL_TIMEOUT = 30.0

# Mirrors web_tools.FETCH_MAX_CHARS — kept as its own constant (not
# imported from there) so this module has no import-time dependency on
# web_tools.py at all, purely to keep the two modules as decoupled as the
# master plan's "why this is a separate module" section asks for.
FETCH_MAX_CHARS = 4000

_DEFAULT_ACTION_TIMEOUT_MS = 10_000
_PROBE_MS = 1500  # short per-candidate look before trying the next
                   # selector strategy, so 3 near-misses don't burn the
                   # whole timeout before the real match gets a turn

_NOT_INSTALLED = "Browser control isn't set up yet. Run: jarvis browser-setup"

# A page-supplied javascript:/data: href handed straight to goto(), or a
# file:// URL reaching local disk, is a real footgun — refuse outright
# rather than asking for confirmation, since there's no legitimate reason
# a browser_goto call needs any of these.
_DANGEROUS_SCHEMES = ("file", "javascript", "data")


# ── process-lifetime session -------------------------------------------

_session = {"playwright": None, "context": None, "page": None}


def _import_sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    return sync_playwright


def _headless_setting():
    """Config flag for the implementing agent's own debugging — not
    something the model can flip mid-conversation (no tool exposes it).
    Reuses ai_config.json's existing "defaults" dict rather than a new
    config file, same pattern "tools_enabled" already uses there; an
    installation that has never set browser_headless just gets the
    default (True) with nothing to migrate.
    """
    try:
        from . import ai_config
        cfg = ai_config.load_ai_config()
        return bool(cfg.get("defaults", {}).get("browser_headless", True))
    except Exception:
        return True


def _warm_daemon_setting():
    """Whether browser_* tool calls should try the warm daemon first —
    off by default, same "read from ai_config.json's defaults dict, no
    schema to update" pattern as _headless_setting() right above. Turning
    this on is what actually makes `jarvis browser-daemon` matter to the
    model; leaving it off (the default) keeps every tool exactly as it
    behaved before v2 existed, even if the daemon happens to be running."""
    try:
        from . import ai_config
        cfg = ai_config.load_ai_config()
        return bool(cfg.get("defaults", {}).get("browser_warm_daemon", False))
    except Exception:
        return False


def _read_daemon_port():
    """The port `jarvis browser-daemon` is currently listening on, or
    None if it isn't running (no file) or the file is stale/corrupt —
    both treated the same as "not running", never raised."""
    try:
        data = json.loads(DAEMON_PORT_FILE.read_text(encoding=ENCODING))
        port = int(data.get("port"))
        return port if port > 0 else None
    except Exception:
        return None


def _call_daemon(op, args):
    """Best-effort round trip to the warm browser daemon over its
    loopback socket. Returns the daemon's parsed JSON response dict on
    success, or None on ANY failure (not running, connect refused, bad
    JSON, timeout) — callers always treat None as "fall back to the local
    session", so a dead or slow daemon degrades to v1 behavior rather than
    failing the tool call outright. The short connect timeout matters
    most: a stale port file pointing at nothing must not stall every
    browser_* call waiting on a connection that will never complete."""
    port = _read_daemon_port()
    if port is None:
        return None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_DAEMON_CONNECT_TIMEOUT) as sock:
            sock.settimeout(_DAEMON_CALL_TIMEOUT)
            sock.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode(ENCODING))
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks).decode(ENCODING, errors="replace")
    except OSError:
        return None
    lines = raw.splitlines()
    if not lines:
        return None
    try:
        result = json.loads(lines[0])
    except ValueError:
        return None
    return result if isinstance(result, dict) else None


def _via_daemon_or_local(op, args, local_fn):
    """Route one browser_* call through the warm daemon when
    browser_warm_daemon is on and the daemon actually answers; otherwise
    run the same local, process-lifetime logic this module has always
    used. Turning the flag on can therefore never make a call fail any
    worse than a fresh checkout already does — the daemon is purely
    additive on top of v1."""
    if _warm_daemon_setting():
        result = _call_daemon(op, args)
        if result is not None:
            return result
    return local_fn(args)


def _ensure_page():
    """Lazily open the persistent context on first use this process, and
    hand back the same page for every later call — see the module
    docstring's "process lifetime = one ask" section. Returns
    (page, None) on success, (None, error_string) on failure; never
    raises.
    """
    if _session["page"] is not None:
        return _session["page"], None
    sync_playwright = _import_sync_playwright()
    if sync_playwright is None:
        return None, _NOT_INSTALLED
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        pw = sync_playwright().start()
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=_headless_setting(),
        )
        page = context.pages[0] if context.pages else context.new_page()
    except Exception as e:
        return None, f"Could not start the browser: {e}"
    _session["playwright"] = pw
    _session["context"] = context
    _session["page"] = page
    return page, None


def close_browser():
    """Tear down the session, if one is open. Both the explicit
    browser_close tool and the atexit hook below call this — one teardown
    path, always safe to call when nothing is open, never raises."""
    context = _session.get("context")
    pw = _session.get("playwright")
    _session["page"] = None
    _session["context"] = None
    _session["playwright"] = None
    try:
        if context is not None:
            context.close()
    except Exception:
        pass
    try:
        if pw is not None:
            pw.stop()
    except Exception:
        pass


atexit.register(close_browser)


# ── selector strategy -----------------------------------------------------

_RAW_SELECTOR_PREFIXES = ("#", ".", "[", "//", ">>", "xpath=", "css=", "text=")


def _looks_like_raw_selector(description):
    d = description.strip()
    return d.startswith(_RAW_SELECTOR_PREFIXES)


def _locator_candidates(page, description):
    """Ordered (label, factory) pairs — accessibility-first. See the
    module docstring's "Selector strategy" section for why this order."""
    return [
        ("role=button", lambda: page.get_by_role("button", name=description, exact=False)),
        ("role=link", lambda: page.get_by_role("link", name=description, exact=False)),
        ("label", lambda: page.get_by_label(description, exact=False)),
        ("text", lambda: page.get_by_text(description, exact=False)),
    ]


def _find_locator(page, description, timeout_ms):
    """Resolve `description` to a single Playwright locator.

    A description that already looks like a CSS/XPath selector (starts
    with '#', '.', '[', '//', 'xpath=', 'css=' or 'text=') skips straight
    to page.locator() — the one path given a raw selector directly is
    honored, per the master plan, rather than forced through the
    accessibility candidates. Otherwise tries, in order, an accessible
    role match, a label match, then a text match.

    `timeout_ms` is a budget for the WHOLE call, not per candidate, and is
    split evenly across whichever candidates haven't been tried yet
    (capped per-candidate at _PROBE_MS so a generous overall timeout isn't
    fully spent guessing wrong strategies before the right one gets a
    turn) — confirmed against a real headless Chromium that this needs
    real care: an earlier version gave every non-final candidate a flat
    _PROBE_MS regardless of the requested timeout, which could both
    overshoot a short timeout 2-3x AND, the opposite failure, starve a
    later-but-correct candidate of any time at all once two flat-cost
    misses had already eaten the whole budget. Dividing what's actually
    left by however many candidates remain avoids both.

    Returns (locator, tried) on success, (None, tried) if nothing
    resolved — `tried` is the list of strategy labels attempted, for a
    useful error message. Never raises.
    """
    description = (description or "").strip()
    if _looks_like_raw_selector(description):
        try:
            return page.locator(description).first, ["css/xpath"]
        except Exception:
            return None, ["css/xpath"]
    tried = []
    candidates = _locator_candidates(page, description)
    n = len(candidates)
    start = time.monotonic()
    for i, (label, make) in enumerate(candidates):
        remaining_ms = timeout_ms - int((time.monotonic() - start) * 1000)
        if remaining_ms <= 0:
            break
        tried.append(label)
        is_last = i == n - 1
        probe_ms = remaining_ms if is_last else max(1, min(_PROBE_MS, remaining_ms // (n - i)))
        try:
            loc = make().first
            loc.wait_for(state="visible", timeout=probe_ms)
            return loc, tried
        except Exception:
            continue
    return None, tried


# ── tools -------------------------------------------------------------


def _local_tool_browser_goto(args=None):
    args = args or {}
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "needs_clarification": True, "message": "Need a URL to go to."}
    scheme = urlsplit(url).scheme.lower()
    if not scheme:
        url = "https://" + url
        scheme = "https"
    if scheme in _DANGEROUS_SCHEMES:
        return {"ok": False, "error": f"Refusing to navigate to a {scheme}: URL — not allowed."}
    page, err = _ensure_page()
    if err:
        return {"ok": False, "error": err}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        return {"ok": False, "error": f"Navigation failed: {e}"}
    try:
        title = page.title()
    except Exception:
        title = ""
    return {"ok": True, "url": page.url, "title": title}


def _local_tool_browser_click(args=None):
    args = args or {}
    description = (args.get("description") or "").strip()
    if not description:
        return {
            "ok": False, "needs_clarification": True,
            "message": "Describe the element to click (e.g. 'the Submit button'), not a CSS selector.",
        }
    page, err = _ensure_page()
    if err:
        return {"ok": False, "error": err}
    try:
        loc, tried = _find_locator(page, description, _DEFAULT_ACTION_TIMEOUT_MS)
        if loc is None:
            return {
                "ok": False,
                "error": f'Couldn\'t find "{description}" on the page (tried: {", ".join(tried)}).',
            }
        loc.click(timeout=_DEFAULT_ACTION_TIMEOUT_MS)
    except Exception as e:
        return {"ok": False, "error": f"Click failed: {e}"}
    return {"ok": True, "url": page.url}


def _local_tool_browser_fill(args=None):
    args = args or {}
    description = (args.get("description") or "").strip()
    text = args.get("text")
    if not description:
        return {
            "ok": False, "needs_clarification": True,
            "message": "Describe the field to fill (e.g. 'the email field'), not a CSS selector.",
        }
    if text is None:
        return {"ok": False, "needs_clarification": True, "message": "Need the text to type into the field."}
    page, err = _ensure_page()
    if err:
        return {"ok": False, "error": err}
    try:
        loc, tried = _find_locator(page, description, _DEFAULT_ACTION_TIMEOUT_MS)
        if loc is None:
            return {
                "ok": False,
                "error": f'Couldn\'t find "{description}" on the page (tried: {", ".join(tried)}).',
            }
        loc.fill(str(text), timeout=_DEFAULT_ACTION_TIMEOUT_MS)
    except Exception as e:
        return {"ok": False, "error": f"Fill failed: {e}"}
    return {"ok": True}


def _local_tool_browser_get_text(args=None):
    args = args or {}
    description = (args.get("description") or "").strip()
    page, err = _ensure_page()
    if err:
        return {"ok": False, "error": err}
    try:
        if description:
            loc, tried = _find_locator(page, description, _DEFAULT_ACTION_TIMEOUT_MS)
            if loc is None:
                return {
                    "ok": False,
                    "error": f'Couldn\'t find "{description}" on the page (tried: {", ".join(tried)}).',
                }
            text = loc.inner_text(timeout=_DEFAULT_ACTION_TIMEOUT_MS)
        else:
            text = page.inner_text("body", timeout=_DEFAULT_ACTION_TIMEOUT_MS)
    except Exception as e:
        return {"ok": False, "error": f"Could not read text: {e}"}
    text = (text or "").strip()
    truncated = len(text) > FETCH_MAX_CHARS
    return {"ok": True, "url": page.url, "text": text[:FETCH_MAX_CHARS], "truncated": truncated}


def _prune_browser_screenshots():
    """Same cap screenshot_tools.py's own _prune() uses (MAX_KEEP), scoped
    to this tool's own "browser_*.png" filenames so it never touches or
    counts desktop screenshots (screenshot_tools._prune() only globs
    "ss_*.png", so the reverse is already true) — kept here rather than
    calling into screenshot_tools' private _prune() so this module doesn't
    depend on that function's internals staying the same shape."""
    from . import screenshot_tools

    files = sorted(screenshot_tools.SCREENSHOT_DIR.glob("browser_*.png"), key=lambda p: p.stat().st_mtime)
    while len(files) > screenshot_tools.MAX_KEEP:
        try:
            files.pop(0).unlink(missing_ok=True)
        except OSError:
            break


def _local_tool_browser_screenshot(args=None):
    page, err = _ensure_page()
    if err:
        return {"ok": False, "error": err}
    from . import screenshot_tools

    screenshot_tools.ensure_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fid = "browser_" + stamp + "_" + secrets.token_hex(3)
    path = screenshot_tools.SCREENSHOT_DIR / f"{fid}.png"
    try:
        page.screenshot(path=str(path), timeout=_DEFAULT_ACTION_TIMEOUT_MS)
    except Exception as e:
        return {"ok": False, "error": f"Screenshot failed: {e}"}
    _prune_browser_screenshots()
    # Same machine line screenshot_tools._emit_media prints — matched
    # literally (not imported) so the web UI's existing "screenshot" extras
    # case picks this up with zero web UI changes. See module docstring.
    print(f"JARVIS_MEDIA\tscreenshot\t{path.name}", file=sys.stderr, flush=True)
    size = path.stat().st_size if path.exists() else 0
    return {
        "ok": True,
        "file": path.name,
        "path": str(path),
        "bytes": size,
        "url": page.url,
        "note": (
            "Screenshot saved and delivered to the user in the UI. Do NOT "
            "describe the pixels or invent what is on screen — reply in one "
            "short sentence that it is ready."
        ),
    }


def _local_tool_browser_wait_for(args=None):
    args = args or {}
    description = (args.get("description") or "").strip()
    if not description:
        return {
            "ok": False, "needs_clarification": True,
            "message": "Describe what to wait for (e.g. 'the results list', 'the Submit button').",
        }
    timeout_seconds = args.get("timeout_seconds")
    try:
        timeout_seconds = float(timeout_seconds) if timeout_seconds is not None else 10.0
    except (TypeError, ValueError):
        timeout_seconds = 10.0
    timeout_seconds = max(1.0, min(timeout_seconds, 120.0))
    page, err = _ensure_page()
    if err:
        return {"ok": False, "error": err}
    try:
        loc, tried = _find_locator(page, description, int(timeout_seconds * 1000))
    except Exception as e:
        return {"ok": False, "error": f"Wait failed: {e}"}
    if loc is None:
        return {
            "ok": False, "timed_out": True,
            "error": f'"{description}" never appeared within {timeout_seconds:.0f}s (tried: {", ".join(tried)}).',
        }
    return {"ok": True, "url": page.url}


def _local_tool_browser_close(args=None):
    was_open = _session.get("page") is not None
    close_browser()
    return {"ok": True, "closed": was_open}


# ── `jarvis browser-setup` ---------------------------------------------


def run_setup():
    """`jarvis browser-setup` — installs the playwright package and its
    Chromium binary, reporting pass/fail per step in the same
    ok/fail-with-a-detail shape doctor.py's checks use (kept local here
    rather than importing doctor.py, which has no notion of an
    install-time setup step to hang this off of). Never raises."""
    steps = []

    try:
        import playwright  # noqa: F401
        steps.append({"step": "pip install playwright", "ok": True, "detail": "already installed"})
    except ImportError:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "playwright"],
                capture_output=True, text=True, timeout=300,
            )
            ok = result.returncode == 0
            detail = "installed" if ok else (result.stderr or result.stdout or "unknown error").strip()[-800:]
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, detail = False, str(e)
        steps.append({"step": "pip install playwright", "ok": ok, "detail": detail})
        if not ok:
            return {"ok": False, "steps": steps}

    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=600,
        )
        ok = result.returncode == 0
        detail = "installed" if ok else (result.stderr or result.stdout or "unknown error").strip()[-800:]
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, detail = False, str(e)
    steps.append({"step": "playwright install chromium", "ok": ok, "detail": detail})
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


# ── public entry points (v2: warm daemon when enabled, local session otherwise) ---

def tool_browser_goto(args=None):
    return _via_daemon_or_local("goto", args, _local_tool_browser_goto)


def tool_browser_click(args=None):
    return _via_daemon_or_local("click", args, _local_tool_browser_click)


def tool_browser_fill(args=None):
    return _via_daemon_or_local("fill", args, _local_tool_browser_fill)


def tool_browser_get_text(args=None):
    return _via_daemon_or_local("get_text", args, _local_tool_browser_get_text)


def tool_browser_screenshot(args=None):
    return _via_daemon_or_local("screenshot", args, _local_tool_browser_screenshot)


def tool_browser_wait_for(args=None):
    return _via_daemon_or_local("wait_for", args, _local_tool_browser_wait_for)


def tool_browser_close(args=None):
    # Explicit early close is honored against whichever session is
    # actually open — the daemon's warm session when the flag is on and
    # it answers, this process's own local session otherwise. Either way
    # the daemon process itself keeps running (its idle-timeout owns
    # closing the browser on its own schedule); this only closes the
    # browser, not the daemon.
    return _via_daemon_or_local("close", args, _local_tool_browser_close)


# ── schemas -------------------------------------------------------------

BROWSER_TOOL_SCHEMAS = [
    {
        "name": "browser_goto",
        "description": (
            "Open a URL in a real, persistent browser session that stays logged "
            "in across asks. Starts the browser if none is open yet this ask. "
            "Use this — not web_fetch — when you need to interact with the page "
            "afterwards (fill a form, click through a flow), not just read it "
            "once. Refuses file:// / javascript: / data: URLs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full or bare URL, e.g. 'github.com/login'."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_click",
        "description": (
            "Click an element on the currently open page. Describe WHAT the "
            "element is in plain English (e.g. 'the Submit button', 'the second "
            "search result') — do NOT supply a CSS selector or XPath; the tool "
            "resolves accessible role/label/text matches itself and is far more "
            "reliable that way. This can submit forms, buy things, or change "
            "account settings on a real logged-in session, so it asks for "
            "confirmation before running."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Plain-English description of the element, not a selector.",
                },
            },
            "required": ["description"],
        },
    },
    {
        "name": "browser_fill",
        "description": (
            "Type text into a field on the currently open page. Describe the "
            "field in plain English (e.g. 'the email field'), not a CSS "
            "selector. Asks for confirmation before running, since it writes "
            "into a real, possibly logged-in session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Plain-English description of the field, not a selector.",
                },
                "text": {"type": "string", "description": "Text to type into the field."},
            },
            "required": ["description", "text"],
        },
    },
    {
        "name": "browser_get_text",
        "description": (
            "Read text from the currently open page — the whole visible page if "
            "`description` is omitted, or just one element if given. Capped at "
            f"{FETCH_MAX_CHARS} characters, same as web_fetch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Optional: plain-English description of one element to read.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "browser_screenshot",
        "description": (
            "Screenshot the currently open page and show it to the user in the "
            "Jarvis UI — the image is NOT sent to you. Use this to 'see' page "
            "state text can't capture: a canvas-based UI, a CAPTCHA, a "
            "layout-dependent form."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_wait_for",
        "description": (
            "Wait until an element appears on the currently open page — for "
            "pages that load content asynchronously after browser_goto or "
            "browser_click returns. Describe the element in plain English."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Plain-English description of what to wait for."},
                "timeout_seconds": {"type": "integer", "description": "Max seconds to wait. Default 10, max 120."},
            },
            "required": ["description"],
        },
    },
    {
        "name": "browser_close",
        "description": (
            "Close the browser session early, before this ask ends. The "
            "session closes automatically once the ask finishes either way, so "
            "this is only needed when a multi-step task knows it's done with "
            "the browser partway through."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

BROWSER_TOOLS = {
    "browser_goto": tool_browser_goto,
    "browser_click": tool_browser_click,
    "browser_fill": tool_browser_fill,
    "browser_get_text": tool_browser_get_text,
    "browser_screenshot": tool_browser_screenshot,
    "browser_wait_for": tool_browser_wait_for,
    "browser_close": tool_browser_close,
}
