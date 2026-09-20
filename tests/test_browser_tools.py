"""Tests for jarvis/browser_tools.py (master plan Part C).

    python3 tests/test_browser_tools.py

HOME is redirected to a temp dir before any jarvis import (tool_safety.py
and ai_config.py both touch ~/.jarvis at call time).

Locator resolution (_find_locator) is tested against a fake Page/Locator
pair with a controllable clock, not a real browser — deterministic, no
subprocess, no timing flakiness. run_setup() is tested with subprocess.run
monkeypatched and `playwright` forced in/out of sys.modules, so it never
needs real network or a real pip install.

Set JARVIS_TEST_REAL_BROWSER=1 to additionally run a real headless-Chromium
check via page.set_content() (no network needed) if Playwright and its
Chromium binary happen to be installed — this validates the actual
selector/click/fill/wait_for/screenshot behavior end to end, but is opt-in
and skips itself cleanly rather than failing when the dependency (a
~300MB optional install) isn't present, per the master plan's own
"what can and can't be validated without a real environment" section.
"""

import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

_TMP = tempfile.mkdtemp(prefix="jarvis-browser-test-")
os.environ["HOME"] = _TMP
os.environ["USERPROFILE"] = _TMP
Path(_TMP, ".jarvis").mkdir(parents=True, exist_ok=True)

from jarvis import browser_tools  # noqa: E402
from jarvis import tool_safety  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402
from jarvis import tool_registry  # noqa: E402


# --- fakes for _find_locator -----------------------------------------------

class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def advance(self, ms):
        self.t += max(ms, 0) / 1000.0

    def monotonic(self):
        return self.t


class _FakeLocator:
    """Mimics just enough of playwright.sync_api.Locator: a match resolves
    almost instantly (like a real element already on the page), a miss
    blocks for the full requested timeout before raising — same shape a
    real Locator.wait_for has, so the budget-tracking math in
    _find_locator gets exercised honestly."""

    def __init__(self, matches, clock):
        self._matches = matches
        self._clock = clock

    @property
    def first(self):
        return self

    def wait_for(self, state="visible", timeout=0):
        if self._matches:
            self._clock.advance(min(5, timeout))
            return
        self._clock.advance(timeout)
        raise TimeoutError(f"not found within {timeout}ms")

    def click(self, timeout=0):
        if not self._matches:
            raise TimeoutError("not found")

    def fill(self, text, timeout=0):
        if not self._matches:
            raise TimeoutError("not found")
        self.filled = text

    def inner_text(self, timeout=0):
        if not self._matches:
            raise TimeoutError("not found")
        return "fake text"


class _FakePage:
    """match_strategy names which candidate succeeds: 'role_button',
    'role_link', 'label', 'text', 'raw', or None (nothing ever matches)."""

    def __init__(self, match_strategy=None, clock=None):
        self.match_strategy = match_strategy
        self.clock = clock or _FakeClock()
        self.url = "http://example.test/"

    def get_by_role(self, role, name=None, exact=False):
        return _FakeLocator(self.match_strategy == f"role_{role}", self.clock)

    def get_by_label(self, text, exact=False):
        return _FakeLocator(self.match_strategy == "label", self.clock)

    def get_by_text(self, text, exact=False):
        return _FakeLocator(self.match_strategy == "text", self.clock)

    def locator(self, selector):
        return _FakeLocator(self.match_strategy == "raw", self.clock)

    def title(self):
        return "Fake Title"


def _with_fake_clock(fn):
    """Run fn() with time.monotonic() (as browser_tools sees it) driven by
    a fresh _FakeClock, restoring the real one afterward no matter what."""
    import time as time_mod

    clock = _FakeClock()
    orig = time_mod.monotonic
    time_mod.monotonic = clock.monotonic
    try:
        return fn(clock)
    finally:
        time_mod.monotonic = orig


# --- _find_locator -----------------------------------------------------


def test_find_locator_prefers_first_matching_strategy():
    def run(clock):
        page = _FakePage(match_strategy="role_button", clock=clock)
        loc, tried = browser_tools._find_locator(page, "Submit", 4000)
        assert loc is not None
        assert tried == ["role=button"], tried
        assert clock.t * 1000 < 100, "a genuine match should resolve almost instantly"

    _with_fake_clock(run)


def test_find_locator_falls_through_in_order():
    def run(clock):
        page = _FakePage(match_strategy="text", clock=clock)
        loc, tried = browser_tools._find_locator(page, "some text", 4000)
        assert loc is not None
        assert tried == ["role=button", "role=link", "label", "text"], tried
        assert clock.t * 1000 <= 4000 + 50, f"overshot budget: {clock.t * 1000}ms"

    _with_fake_clock(run)


def test_find_locator_total_timeout_is_a_budget_not_per_candidate():
    """Regression: an earlier version gave every non-final candidate a
    flat probe regardless of the requested total, which could both
    overshoot a short timeout several times over AND starve a later,
    correct candidate of any time at all. Confirmed against a real
    headless Chromium before this fix landed."""
    def run(clock):
        page = _FakePage(match_strategy=None, clock=clock)
        loc, tried = browser_tools._find_locator(page, "nothing", 2000)
        assert loc is None
        assert tried == ["role=button", "role=link", "label", "text"], tried
        assert clock.t * 1000 <= 2000 + 50, f"overshot budget: {clock.t * 1000}ms"

    _with_fake_clock(run)


def test_find_locator_does_not_starve_the_last_candidate():
    def run(clock):
        page = _FakePage(match_strategy="text", clock=clock)
        loc, tried = browser_tools._find_locator(page, "late text", 3000)
        assert loc is not None, "the 'text' strategy should still get a fair share of the budget"
        assert tried[-1] == "text"

    _with_fake_clock(run)


def test_find_locator_raw_selector_skips_the_candidate_list():
    def run(clock):
        page = _FakePage(match_strategy="raw", clock=clock)
        for desc in ("#submit", ".btn", "//button", "xpath=//button", "css=.x", "text=Go"):
            loc, tried = browser_tools._find_locator(page, desc, 1000)
            assert loc is not None, desc
            assert tried == ["css/xpath"], (desc, tried)

    _with_fake_clock(run)


def test_looks_like_raw_selector():
    assert browser_tools._looks_like_raw_selector("#submit")
    assert browser_tools._looks_like_raw_selector(".btn.primary")
    assert browser_tools._looks_like_raw_selector("//button")
    assert browser_tools._looks_like_raw_selector("xpath=//button")
    assert not browser_tools._looks_like_raw_selector("the Submit button")
    assert not browser_tools._looks_like_raw_selector("email field")


# --- tool-level behavior via a fake page --------------------------------


def _with_fake_page(match_strategy, fn):
    fake_page = _FakePage(match_strategy=match_strategy)
    orig = browser_tools._ensure_page
    browser_tools._ensure_page = lambda: (fake_page, None)
    try:
        return fn(fake_page)
    finally:
        browser_tools._ensure_page = orig


def test_tool_browser_click_success():
    def run(page):
        result = browser_tools.tool_browser_click({"description": "Submit"})
        assert result == {"ok": True, "url": page.url}, result

    _with_fake_clock(lambda clock: _with_fake_page("role_button", run))


def test_tool_browser_click_not_found():
    def run(page):
        result = browser_tools.tool_browser_click({"description": "Nope"})
        assert result["ok"] is False
        assert "Nope" in result["error"]

    _with_fake_clock(lambda clock: _with_fake_page(None, run))


def test_tool_browser_click_requires_description():
    result = browser_tools.tool_browser_click({})
    assert result["ok"] is False and result.get("needs_clarification") is True


def test_tool_browser_fill_success_and_requires_text():
    def run(page):
        result = browser_tools.tool_browser_fill({"description": "Email", "text": "a@b.com"})
        assert result == {"ok": True}, result
        result2 = browser_tools.tool_browser_fill({"description": "Email"})
        assert result2["ok"] is False and result2.get("needs_clarification") is True

    _with_fake_clock(lambda clock: _with_fake_page("label", run))


def test_tool_browser_get_text_whole_page_and_element():
    class PageWithBody(_FakePage):
        def inner_text(self, selector, timeout=0):
            assert selector == "body"
            return "x" * 10

    def run(_page):
        page = PageWithBody(match_strategy="text")
        orig = browser_tools._ensure_page
        browser_tools._ensure_page = lambda: (page, None)
        try:
            whole = browser_tools.tool_browser_get_text({})
            assert whole == {"ok": True, "url": page.url, "text": "x" * 10, "truncated": False}, whole

            one = browser_tools.tool_browser_get_text({"description": "something"})
            assert one["ok"] is True and one["text"] == "fake text"
        finally:
            browser_tools._ensure_page = orig

    _with_fake_clock(lambda clock: run(None))


def test_tool_browser_get_text_truncates():
    class LongTextPage(_FakePage):
        def inner_text(self, selector, timeout=0):
            return "y" * (browser_tools.FETCH_MAX_CHARS + 500)

    page = LongTextPage()
    orig = browser_tools._ensure_page
    browser_tools._ensure_page = lambda: (page, None)
    try:
        result = browser_tools.tool_browser_get_text({})
        assert result["truncated"] is True
        assert len(result["text"]) == browser_tools.FETCH_MAX_CHARS
    finally:
        browser_tools._ensure_page = orig


def test_tool_browser_wait_for_requires_description():
    result = browser_tools.tool_browser_wait_for({})
    assert result["ok"] is False and result.get("needs_clarification") is True


def test_tool_browser_wait_for_timeout_seconds_is_clamped():
    calls = []

    def fake_find_locator(page, description, timeout_ms):
        calls.append(timeout_ms)
        return None, ["role=button", "role=link", "label", "text"]

    orig_find = browser_tools._find_locator
    orig_ensure = browser_tools._ensure_page
    browser_tools._find_locator = fake_find_locator
    browser_tools._ensure_page = lambda: (_FakePage(), None)
    try:
        browser_tools.tool_browser_wait_for({"description": "x", "timeout_seconds": 9999})
        assert calls[-1] == 120_000, calls  # clamped to the 120s max
        browser_tools.tool_browser_wait_for({"description": "x", "timeout_seconds": -5})
        assert calls[-1] == 1_000, calls  # clamped to the 1s floor
    finally:
        browser_tools._find_locator = orig_find
        browser_tools._ensure_page = orig_ensure


# --- browser_goto: URL handling and dangerous schemes -------------------


def test_tool_browser_goto_requires_url():
    result = browser_tools.tool_browser_goto({})
    assert result["ok"] is False and result.get("needs_clarification") is True


def test_tool_browser_goto_refuses_dangerous_schemes():
    for url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi"):
        result = browser_tools.tool_browser_goto({"url": url})
        assert result["ok"] is False, (url, result)
        assert "Refusing" in result["error"], (url, result)


def test_tool_browser_goto_adds_https_when_schemeless():
    seen = {}

    class RecordingPage(_FakePage):
        def goto(self, url, wait_until=None, timeout=None):
            seen["url"] = url

        def title(self):
            return "ok"

    page = RecordingPage()
    orig = browser_tools._ensure_page
    browser_tools._ensure_page = lambda: (page, None)
    try:
        result = browser_tools.tool_browser_goto({"url": "example.com/path"})
        assert result["ok"] is True, result
        assert seen["url"] == "https://example.com/path", seen
    finally:
        browser_tools._ensure_page = orig


# --- not-installed degradation -------------------------------------------


def test_ensure_page_reports_not_installed():
    orig_import = browser_tools._import_sync_playwright
    orig_session = dict(browser_tools._session)
    browser_tools._import_sync_playwright = lambda: None
    browser_tools._session["page"] = None
    browser_tools._session["context"] = None
    browser_tools._session["playwright"] = None
    try:
        page, err = browser_tools._ensure_page()
        assert page is None
        assert "jarvis browser-setup" in err, err

        result = browser_tools.tool_browser_goto({"url": "example.com"})
        assert result == {"ok": False, "error": browser_tools._NOT_INSTALLED}, result
    finally:
        browser_tools._import_sync_playwright = orig_import
        browser_tools._session.update(orig_session)


def test_close_browser_is_idempotent_when_nothing_open():
    browser_tools._session["page"] = None
    browser_tools._session["context"] = None
    browser_tools._session["playwright"] = None
    browser_tools.close_browser()  # must not raise
    result = browser_tools.tool_browser_close({})
    assert result == {"ok": True, "closed": False}, result


# --- run_setup() — subprocess mocked, no real installs or network -------


def _with_forced_playwright_import(present, fn):
    had = sys.modules.pop("playwright", None)
    sys.modules["playwright"] = types.ModuleType("playwright") if present else None
    try:
        return fn()
    finally:
        del sys.modules["playwright"]
        if had is not None:
            sys.modules["playwright"] = had


def test_run_setup_skips_pip_install_when_already_present():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def go():
        orig_run = browser_tools.subprocess.run
        browser_tools.subprocess.run = fake_run
        try:
            return browser_tools.run_setup()
        finally:
            browser_tools.subprocess.run = orig_run

    report = _with_forced_playwright_import(True, go)
    assert report["steps"][0] == {"step": "pip install playwright", "ok": True, "detail": "already installed"}
    assert len(calls) == 1, calls  # only the chromium install step should shell out
    assert report["ok"] is True


def test_run_setup_pip_install_failure_short_circuits():
    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="no network")

    def go():
        orig_run = browser_tools.subprocess.run
        browser_tools.subprocess.run = fake_run
        try:
            return browser_tools.run_setup()
        finally:
            browser_tools.subprocess.run = orig_run

    report = _with_forced_playwright_import(False, go)
    assert report["ok"] is False
    assert len(report["steps"]) == 1, report["steps"]  # never gets to the chromium step
    assert "no network" in report["steps"][0]["detail"]


def test_run_setup_all_steps_succeed():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def go():
        orig_run = browser_tools.subprocess.run
        browser_tools.subprocess.run = fake_run
        try:
            return browser_tools.run_setup()
        finally:
            browser_tools.subprocess.run = orig_run

    report = _with_forced_playwright_import(False, go)
    assert [s["step"] for s in report["steps"]] == ["pip install playwright", "playwright install chromium"]
    assert all(s["ok"] for s in report["steps"]), report
    assert report["ok"] is True
    assert len(calls) == 2


# --- registration: tool_safety.py, tools.py, tool_registry.py -----------


def test_browser_click_and_fill_require_confirmation_by_default():
    assert tool_safety.requires_confirmation("browser_click") is True
    assert tool_safety.requires_confirmation("browser_fill") is True


def test_other_browser_tools_do_not_require_confirmation_by_default():
    for name in ("browser_goto", "browser_get_text", "browser_screenshot",
                 "browser_wait_for", "browser_close"):
        assert tool_safety.requires_confirmation(name) is False, name


def test_browser_tools_registered_in_catalog():
    for name in browser_tools.BROWSER_TOOLS:
        assert name in system_tools.TOOLS, name
        assert name in system_tools.TOOL_INDEX if hasattr(system_tools, "TOOL_INDEX") else True

    schema_names = {s["name"] for s in system_tools.CORE_TOOL_SCHEMAS}
    for name in browser_tools.BROWSER_TOOLS:
        assert name in schema_names, name


def test_execute_tool_dispatches_browser_tools_with_arguments():
    """execute_tool's dict-args-vs-no-args branch has to know about
    BROWSER_TOOLS, or every browser_* call would be invoked as fn() with
    no arguments and immediately fail on a missing description/url."""
    orig = browser_tools._ensure_page
    browser_tools._ensure_page = lambda: (None, "stubbed")
    try:
        result = system_tools.execute_tool("browser_goto", {"url": "example.com"})
        assert result == {"ok": False, "error": "stubbed"}, result
    finally:
        browser_tools._ensure_page = orig


def test_browser_group_registered_in_tool_registry():
    assert tool_registry.group_of("browser_goto") == "browser"
    for name in browser_tools.BROWSER_TOOLS:
        assert tool_registry.group_of(name) == "browser", name
    ungrouped = tool_registry._ungrouped_tool_names()
    assert not any(n in ungrouped for n in browser_tools.BROWSER_TOOLS), ungrouped


# --- optional: real headless Chromium, no network required -------------


def test_real_browser_end_to_end_if_available():
    if os.environ.get("JARVIS_TEST_REAL_BROWSER") != "1":
        print("  skip   (set JARVIS_TEST_REAL_BROWSER=1 to run this one)")
        return
    try:
        page, err = browser_tools._ensure_page()
        if err:
            print(f"  skip   (browser not available here: {err})")
            return
    except Exception as e:  # noqa: BLE001 — genuinely optional, never fail the suite over it
        print(f"  skip   (browser not available here: {e})")
        return
    try:
        html = (
            "<html><head><title>T</title></head><body>"
            "<label for='e'>Email</label><input id='e'>"
            "<button id='b' onclick=\"document.getElementById('s').textContent='clicked'\">Go</button>"
            "<p id='s'>idle</p>"
            "</body></html>"
        )
        page.set_content(html)
        assert browser_tools.tool_browser_fill({"description": "Email", "text": "a@b.com"})["ok"] is True
        assert page.locator("#e").input_value() == "a@b.com"
        assert browser_tools.tool_browser_click({"description": "Go"})["ok"] is True
        result = browser_tools.tool_browser_get_text({"description": "#s"})
        assert result == {"ok": True, "url": "about:blank", "text": "clicked", "truncated": False}, result
        shot = browser_tools.tool_browser_screenshot({})
        assert shot["ok"] is True and Path(shot["path"]).exists()
    finally:
        browser_tools.close_browser()


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
