"""custom_tools_agent.py -- "Ask Jarvis" in the Tool Manager editor (master plan
L.43). No key, no network: the provider adapter is replaced with a fake.

What this pins:
  - the prompt carries the CURRENT editor text and the request, and stays text-only
  - split_reply: note + one fenced file; no fence = a question (no code);
    an unclosed fence is reported incomplete
  - run() always ends with exactly ONE JARVIS_AGENT_DONE line and never raises
  - tokens streamed by the adapter reach stdout as JARVIS_STREAM lines
  - a half-written reply is never applied; a failing key resets and falls through
  - nothing is written to disk and no tools are offered to the model

Run with `python3 tests/test_custom_tools_agent.py`.
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "jarvis-cli"))

from jarvis import ai_providers  # noqa: E402
from jarvis import custom_tools_agent as cta  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok  " + name)
    else:
        failed += 1
        print("FAIL " + name + (": " + detail if detail else ""))


CODE = 'TOOL_GROUP = "custom"\nTOOLS = {}\nTOOL_SCHEMAS = []\n'
REPLY = "Adding the file.\n```python\n" + CODE + "```\nRead it first."


class Fake:
    def __init__(self, answers, stream=None):
        self.answers, self.calls, self.stream = list(answers), [], stream

    def __call__(self, provider, messages, timeout, tools=None, tool_executor=None):
        self.calls.append({"provider": dict(provider), "messages": messages, "tools": tools,
                           "stream_on": ai_providers.stream_enabled()})
        if self.stream:
            for piece in self.stream:
                ai_providers._emit_stream(ai_providers.EVENT_TEXT, delta=piece)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def ok(text):
    return ai_providers.AIResult(True, text=text)


def prov(name, **kw):
    d = {"name": name, "type": "fake", "enabled": True, "api_keys": ["k1"]}
    d.update(kw)
    return d


def run(payload, adapter, providers):
    ai_providers.ADAPTERS["fake"] = adapter
    buf = io.StringIO()
    real = sys.stdout
    sys.stdout = buf
    try:
        cta.run(payload, cfg={"providers": providers})
    finally:
        sys.stdout = real
    lines = buf.getvalue().splitlines()
    done = [json.loads(l[len(cta.DONE_MARKER):]) for l in lines if l.startswith(cta.DONE_MARKER)]
    stream = [json.loads(l[len("JARVIS_STREAM "):]) for l in lines if l.startswith("JARVIS_STREAM ")]
    return done, stream


# --- split_reply ---------------------------------------------------------
n, c, done = cta.split_reply(REPLY)
check("split: note before the fence", n.startswith("Adding the file.") and "Read it first." in n)
check("split: code is the fenced body", c == CODE and done is True)
n, c, done = cta.split_reply("Which folder do you mean?")
check("split: no fence = question, no code", c == "" and n == "Which folder do you mean?")
n, c, done = cta.split_reply("On it.\n```python\nx = 1\ny = 2")
check("split: open fence is incomplete", done is False and c.startswith("x = 1"))
check("split: trailing fence without newline closes", cta.split_reply("n\n```python\nx = 1```")[2] is True)

# --- prompt --------------------------------------------------------------
msgs = cta.build_messages("add a ping tool", "x = 1\n", "demo", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])
check("prompt: system first", msgs[0]["role"] == "system")
check("prompt: history kept", [m["role"] for m in msgs[1:3]] == ["user", "assistant"])
check("prompt: current text + request in last turn", "x = 1" in msgs[-1]["content"] and "add a ping tool" in msgs[-1]["content"])
check("prompt: empty editor says so", "empty" in cta.build_messages("go", "", "demo")[-1]["content"])
big = cta.build_messages("go", "", "d", [{"role": "user", "content": "z" * 5000}])
check("prompt: history turn is clipped", len(big[1]["content"]) < 700)

# --- run() ---------------------------------------------------------------
before = set(os.listdir(tempfile.gettempdir()))
fake = Fake([ok(REPLY)], stream=["Adding the file.\n```py", "thon\n", CODE])
done, stream = run({"instruction": "make it", "source": "", "name": "demo"}, fake, [prov("p")])
check("run: exactly one DONE line", len(done) == 1 and done[0]["ok"] is True)
check("run: code returned", done[0]["code"] == CODE and "Adding the file." in done[0]["note"])
check("run: tokens streamed as markers", "".join(e.get("d", "") for e in stream if e["k"] == "text").startswith("Adding the file."))
check("run: model gets NO tools", fake.calls[0]["tools"] is None)
check("run: streaming switched on for the attempt", fake.calls[0]["stream_on"] is True)
check("run: stream state cleared afterwards", ai_providers.stream_enabled() is False)

done, _ = run({"instruction": "which?", "source": CODE}, Fake([ok("Which folder?")]), [prov("p")])
check("run: question -> ok, no code", done[0]["ok"] and done[0]["code"] == "" and "folder" in done[0]["note"])

done, _ = run({"instruction": "x", "source": ""}, Fake([ok("Sure.\n```python\nx = 1\ny = 2")]), [prov("p")])
check("run: cut-off reply is never applied", done[0]["ok"] and done[0]["code"] == "" and "cut off" in done[0]["note"])

done, _ = run({"instruction": "", "source": ""}, Fake([]), [prov("p")])
check("run: empty request refused", len(done) == 1 and not done[0]["ok"])
done, _ = run({"instruction": "x"}, Fake([]), [])
check("run: no provider reported", not done[0]["ok"] and "provider" in done[0]["error"])
done, _ = run({"instruction": "x", "source": "a" * (cta.MAX_SOURCE_CHARS + 1)}, Fake([]), [prov("p")])
check("run: oversized file refused", not done[0]["ok"])

fake = Fake([RuntimeError("boom"), ok(REPLY)])
done, stream = run({"instruction": "x", "source": ""}, fake, [prov("p", api_keys=["k1", "k2"])])
check("run: second key used after the first fails", done[0]["ok"] and len(fake.calls) == 2)
check("run: reset sent between keys", any(e["k"] == "reset" for e in stream))
check("run: each key used on its own attempt", fake.calls[0]["provider"]["api_key"] == "k1" and fake.calls[1]["provider"]["api_key"] == "k2")

done, _ = run({"instruction": "x", "source": ""}, Fake([RuntimeError("down")]), [prov("p")])
check("run: failure reported, never raised", len(done) == 1 and not done[0]["ok"] and "down" in done[0]["error"])

# capable provider preferred over the cheap completion helper
fake = Fake([ok(REPLY)])
run({"instruction": "x", "source": ""}, fake, [prov("cheap", completion=True), prov("big")])
check("provider: skips the completion helper", fake.calls[0]["provider"]["name"] == "big")
fake = Fake([ok(REPLY)])
run({"instruction": "x", "source": ""}, fake, [prov("cheap", completion=True)])
check("provider: falls back to the helper if it is all there is", fake.calls[0]["provider"]["name"] == "cheap")
check("run: leaves no files behind", set(os.listdir(tempfile.gettempdir())) == before)

# --- output-limit cut-offs are continued, not thrown away ----------------
def cutres(text, cut=None):
    return ai_providers.AIResult(True, text=text, cut=cut)


L = ai_providers.CUT_LENGTH
fake = Fake([cutres("Sure.\n```python\nx = 1\n", L), cutres("y = 2\n```", None)])
done, _ = run({"instruction": "x", "source": ""}, fake, [prov("p")])
check("cont: a length cut is continued into one complete file",
      done[0]["ok"] and done[0]["code"] == "x = 1\ny = 2\n" and len(fake.calls) == 2, str(done))
check("cont: the continuation call carries the partial reply, then the ask",
      [m["role"] for m in fake.calls[1]["messages"][-2:]] == ["assistant", "user"]
      and "x = 1" in fake.calls[1]["messages"][-2]["content"])

fake = Fake([cutres("Sure.\n```python\nx = 1\nyy = 22\n", L),
             cutres("```python\nyy = 22\nz = 3\n```", None)])
done, _ = run({"instruction": "x", "source": ""}, fake, [prov("p")])
check("cont: a re-opened fence and repeated line are dropped at the seam",
      done[0]["code"] == "x = 1\nyy = 22\nz = 3\n", repr(done[0].get("code")))

fake = Fake([cutres("Sure.\n```python\na\n", L)] + [cutres("b\n", L)] * cta.MAX_CONTINUATIONS)
done, _ = run({"instruction": "x", "source": ""}, fake, [prov("big")])
check("cont: bounded -- gives up after MAX_CONTINUATIONS",
      len(fake.calls) == 1 + cta.MAX_CONTINUATIONS and done[0]["code"] == "")
check("cont: the give-up note names the output limit and the provider",
      "output limit" in done[0]["note"] and "big" in done[0]["note"] and "cut off" in done[0]["note"])

fake = Fake([cutres("Sure.\n```python\nx = 1\n", L), RuntimeError("down")])
done, _ = run({"instruction": "x", "source": ""}, fake, [prov("p")])
check("cont: a failing continuation still ends in exactly one DONE, nothing applied",
      len(done) == 1 and done[0]["ok"] and done[0]["code"] == "")

# --- the provider's own max_tokens is a floor-raiser, not overwritten ----
for cfg_max, want in ((None, cta.AGENT_MAX_TOKENS), (700, cta.AGENT_MAX_TOKENS), (32000, 32000)):
    fake = Fake([ok(REPLY)])
    kw = {} if cfg_max is None else {"max_tokens": cfg_max}
    run({"instruction": "x", "source": ""}, fake, [prov("p", **kw)])
    check("cap: provider max_tokens=%s -> %d" % (cfg_max, want), fake.calls[0]["provider"]["max_tokens"] == want)

# reserved so a saved command can't shadow it
from jarvis import reserved_names  # noqa: E402
names = getattr(reserved_names, "RESERVED_COMMAND_NAMES", None) or getattr(reserved_names, "RESERVED", None)
if names is None:
    names = next((v for v in vars(reserved_names).values() if isinstance(v, (set, frozenset)) and "ctools-suggest" in v), set())
check("reserved: ctools-agent can't be shadowed", "ctools-agent" in names)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
