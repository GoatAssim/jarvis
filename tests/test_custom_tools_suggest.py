"""custom_tools_suggest.py — the model half of the Tool Manager editor's inline
suggestions (master plan L.33). No API key, no network: the provider adapter is
replaced with a fake.

What this pins:
  - the prompt carries the code around the caret and the <CURSOR> marker, and is
    bounded for a large file
  - replies are cleaned: fences stripped, prose rejected, length capped, an echo
    of the text that already follows the caret trimmed
  - suggest() never raises, and reports (rather than hides) why it had nothing
  - provider choice: "completion" flag, then "summarizer", then the first keyed
  - keys are tried in order and a failing one falls through
  - it does nothing but return text: no tool file is written

Run with `python3 tests/test_custom_tools_suggest.py`.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "jarvis-cli"))

from jarvis import ai_providers  # noqa: E402
from jarvis import custom_tools_suggest as cts  # noqa: E402

pass_count = 0
fail_count = 0


def check(name, cond, detail=""):
    global pass_count, fail_count
    if cond:
        pass_count += 1
        print("ok  " + name)
    else:
        fail_count += 1
        print("FAIL " + name + (f": {detail}" if detail else ""))


class FakeAdapter:
    """Records its calls and answers from a queue of AIResult / exceptions."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, provider, messages, timeout, tools=None, tool_executor=None):
        self.calls.append({"provider": dict(provider), "messages": messages, "timeout": timeout, "tools": tools})
        a = self.answers.pop(0) if self.answers else ai_providers.AIResult(False, error="no answer queued")
        if isinstance(a, Exception):
            raise a
        return a


def cfg_with(*providers):
    return {"providers": list(providers)}


def prov(name, **kw):
    d = {"name": name, "type": "fake", "enabled": True, "api_key": "k-" + name}
    d.update(kw)
    return d


def run_with(adapter, source, cursor, cfg):
    saved = ai_providers.ADAPTERS.get("fake")
    ai_providers.ADAPTERS["fake"] = adapter
    try:
        return cts.suggest(source, cursor, "draft", cfg=cfg)
    finally:
        if saved is None:
            ai_providers.ADAPTERS.pop("fake", None)
        else:
            ai_providers.ADAPTERS["fake"] = saved


SRC = "def tool_a(args):\n    value = args.get('v')\n    "


def ok_result(text):
    return ai_providers.AIResult(True, text=text)


# --- clean_completion ---------------------------------------------------------

def test_clean_completion():
    cc = cts.clean_completion
    check("plain code passes through", cc("return {'ok': True}") == "return {'ok': True}")
    check("a fenced reply is unwrapped", cc("```python\nreturn 1\n```") == "return 1")
    check("a fence without a language is unwrapped", cc("```\nreturn 1\n```") == "return 1")
    check("an echoed marker is removed", cc("return 1<CURSOR>") == "return 1")
    check("an empty or whitespace reply is nothing", cc("") == "" and cc("   \n  ") == "" and cc(None) == "")
    check("prose is rejected", cc("Here is the code you asked for:\nreturn 1") == "" and cc("Sure, this will work") == "")
    check("'I' is prose but 'if' is code", cc("I think this works") == "" and cc("if value:\n    pass") != "")
    check("trailing blank lines and spaces are dropped", cc("x = 1   \n\n\n") == "x = 1")
    check("leading indentation is kept", cc("    return 1") == "    return 1")
    check("a leading newline is kept (next-line suggestion)", cc("\n    return 1").startswith("\n"))
    long = "\n".join("line%d = %d" % (i, i) for i in range(40))
    check("capped at MAX_LINES", cc(long).count("\n") + 1 == cts.MAX_LINES)
    check("capped in characters", len(cc("x = '" + "a" * 5000 + "'")) <= cts.MAX_CHARS)
    check("CRLF is normalised", "\r" not in cc("a = 1\r\nb = 2"))
    check("an echo of the text after the caret is trimmed",
          cc("return value\n    TOOLS = {}", suffix="\nTOOLS = {}") == "return value")
    check("a reply equal to the line after the caret is nothing", cc("TOOLS = {}", suffix="\nTOOLS = {}\n") == "")
    check("echoing the caret's own line is nothing", cc("value = 1", prefix="x\nvalue = 1") == "")
    check("a reply ending with the next line's text drops it",
          cc("return value\nTOOLS = {}", suffix="\nTOOLS = {}\n") == "return value")


# --- prompt ---------------------------------------------------------------------

def test_prompt():
    msgs = cts.build_messages(SRC, len(SRC), "my_tool")
    check("system + user message", [m["role"] for m in msgs] == ["system", "user"])
    check("the marker sits at the caret", msgs[1]["content"].endswith("    <CURSOR>"))
    check("the file name is named", "my_tool.py" in msgs[1]["content"])
    mid = SRC.index("value")
    msgs = cts.build_messages(SRC, mid)
    check("text after the caret is included after the marker", "<CURSOR>value = args" in msgs[1]["content"])
    big = "x = 1\n" * 20000
    msgs = cts.build_messages(big, len(big) // 2)
    check("a large file is clipped around the caret", len(msgs[1]["content"]) < cts.PREFIX_CHARS + cts.SUFFIX_CHARS + 200)
    check("the system prompt forbids prose and fences", "No markdown fences" in msgs[0]["content"])


# --- suggest() -----------------------------------------------------------------

def test_suggest_success():
    a = FakeAdapter([ok_result("```python\nreturn {'ok': True, 'value': value}\n```")])
    r = run_with(a, SRC, len(SRC), cfg_with(prov("one")))
    check("a good reply comes back cleaned", r.get("ok") and r["text"] == "return {'ok': True, 'value': value}", str(r))
    check("the provider is named", r.get("provider") == "one")
    check("exactly one call, no tools", len(a.calls) == 1 and a.calls[0]["tools"] is None)
    check("the call is capped in tokens and time", a.calls[0]["provider"]["max_tokens"] == cts.SUGGEST_MAX_TOKENS and a.calls[0]["timeout"] <= cts.SUGGEST_TIMEOUT)


def test_suggest_empty_reply_is_ok_with_empty_text():
    a = FakeAdapter([ok_result("Sure, here you go")])
    r = run_with(a, SRC, len(SRC), cfg_with(prov("one")))
    check("a prose reply is ok but empty", r.get("ok") is True and r["text"] == "", str(r))


def test_suggest_failures_are_reported_not_raised():
    r = run_with(FakeAdapter([]), SRC, len(SRC), cfg_with())
    check("no providers -> a reason", r["ok"] is False and "provider" in r["error"])
    r = run_with(FakeAdapter([]), SRC, len(SRC), cfg_with(prov("nokey", api_key="")))
    check("a provider without a key is not eligible", r["ok"] is False)
    r = run_with(FakeAdapter([RuntimeError("boom")]), SRC, len(SRC), cfg_with(prov("one")))
    check("an adapter exception becomes an error result", r["ok"] is False and "boom" in r["error"], str(r))
    r = run_with(FakeAdapter([ai_providers.AIResult(False, error="HTTP 429 rate limited")]), SRC, len(SRC), cfg_with(prov("one")))
    check("a failed AIResult carries its reason", r["ok"] is False and "429" in r["error"], str(r))
    r = cts.suggest(123, 0, cfg=cfg_with(prov("one")))
    check("non-text source is refused", r["ok"] is False)
    r = cts.suggest("x" * (cts.MAX_SOURCE_CHARS + 1), 0, cfg=cfg_with(prov("one")))
    check("an oversized source is refused before any call", r["ok"] is False and "large" in r["error"])
    r = cts.suggest(SRC, "abc", cfg=cfg_with(prov("one")))
    check("a non-numeric cursor is refused", r["ok"] is False)
    r = run_with(FakeAdapter([ok_result("pass")]), SRC, 10 ** 9, cfg_with(prov("one")))
    check("an out-of-range cursor is clamped", r["ok"] is True)
    ai_providers.ADAPTERS.pop("nope", None)
    r = cts.suggest(SRC, 0, cfg=cfg_with({"name": "x", "type": "nope", "api_key": "k"}))
    check("an unknown provider type is reported", r["ok"] is False)


def test_provider_choice_and_key_failover():
    a = FakeAdapter([ok_result("pass")])
    run_with(a, SRC, len(SRC), cfg_with(prov("main"), prov("cheap", summarizer=True), prov("fast", completion=True)))
    check("the completion-flagged provider wins", a.calls[0]["provider"]["name"] == "fast")
    a = FakeAdapter([ok_result("pass")])
    run_with(a, SRC, len(SRC), cfg_with(prov("main"), prov("cheap", summarizer=True)))
    check("else the summarizer-flagged one", a.calls[0]["provider"]["name"] == "cheap")
    a = FakeAdapter([ok_result("pass")])
    run_with(a, SRC, len(SRC), cfg_with(prov("off", enabled=False), prov("main")))
    check("disabled providers are skipped", a.calls[0]["provider"]["name"] == "main")
    a = FakeAdapter([RuntimeError("first key bad"), ok_result("return 1")])
    r = run_with(a, SRC, len(SRC), cfg_with(prov("main", api_key="", api_keys=["k1", "k2"])))
    check("a bad key falls through to the next", r.get("ok") and r["text"] == "return 1" and [c["provider"]["api_key"] for c in a.calls] == ["k1", "k2"], str(r))


def test_never_writes_a_tool_file():
    import tempfile
    from jarvis import custom_tools_store as store
    before = None
    try:
        d = Path(store.TOOLS_DIR) if hasattr(store, "TOOLS_DIR") else None
        before = sorted(p.name for p in d.iterdir()) if d and d.exists() else None
    except Exception:  # noqa: BLE001
        before = None
    run_with(FakeAdapter([ok_result("return 1")]), SRC, len(SRC), cfg_with(prov("one")))
    after = None
    try:
        after = sorted(p.name for p in d.iterdir()) if d and d.exists() else None
    except Exception:  # noqa: BLE001
        after = None
    check("suggesting leaves the tools folder untouched", before == after)


if __name__ == "__main__":
    test_clean_completion()
    test_prompt()
    test_suggest_success()
    test_suggest_empty_reply_is_ok_with_empty_text()
    test_suggest_failures_are_reported_not_raised()
    test_provider_choice_and_key_failover()
    test_never_writes_a_tool_file()
    print(f"\n{pass_count} passed, {fail_count} failed")
    if fail_count:
        sys.exit(1)
