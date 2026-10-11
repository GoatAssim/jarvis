"""CLI `speak` / `listen` skip fenced code (the CLI half of I-B11).

  1. voice/speech_text.py: the rules (fences, markers, prose kept, inline code left alone, text with
     no fence returned unchanged, None / "" / non-str never raise, idempotent, CRLF read as LF).
  2. Python and JavaScript agree: one corpus through jarvis.voice.speech_text and through
     web/public/rich-text.js's JarvisRich.speechText. Needs `node`; prints SKIP without it.
  3. `jarvis speak` really hands the SPEAKER the stripped text (run in a subprocess with the TTS
     stubbed, so no audio device, voice engine or key is needed) and prints the same JSON as before.
  4. Static: `listen` strips what it speaks but still PRINTS the whole reply; the module is pure.

Written, not run, by the author: the owner runs it.

Run: python3 tests/test_cli_speech_text.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-speech-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI_DIR))

from jarvis.voice import speech_text as ST  # noqa: E402

M = ST.SPOKEN_CODE_MARKER
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


# LF only: the one deliberate difference from the JavaScript is CRLF handling (tested below on its own).
CORPUS = [
    "Hello there.\n\nThis has `inline code` and **bold**.",
    "Here is the fix:\n\n```js\nconst a = {b: 1};\nconsole.log(a);\n```\n\nRun it and tell me.",
    "Try:\n~~~sh\nrm -rf build\n~~~\nDone.",
    "A\n```\none\n```\nB\n```py\ntwo\n```\nC",
    "```\none\n```\n```\ntwo\n```",
    "```python\nprint(1)\n```",
    "Start.\n```js\nlet x = 1;\nlet y = 2;",
    "Before\n````md\n```js\nx\n```\n````\nAfter",
    "Use ```like this``` inline.",
    "```like this``` inline.",
    "   ```\nindented fence (3 spaces) is a fence\n   ```\nafter",
    "    ```\nfour spaces is NOT a fence\n    ```",
    "~~~\ntilde\n```\nnot a close\n~~~\nend",
    "```\nunclosed with a ~~~ inside\n~~~",
    "text\n```js extra words here\ncode\n```\n",
    "\n\n```\ncode\n```\n\n\n",
    "only prose, many\n\nparagraphs\n\n  and indents  ",
    "```js\ncode\n``` trailing words keep it open\nstill code\n```\nreal end",
    "Code block omitted.\n```\nx\n```\nCode block omitted.",
    "",
]


def test_rules():
    s = ST.speech_text
    check("text with no fence is returned unchanged (the very same text)", s(CORPUS[0]) == CORPUS[0] and s("plain") == "plain")
    out = s(CORPUS[1])
    check("a fenced block becomes the marker, prose around it is kept",
          out == "Here is the fix:\n\n" + M + "\n\nRun it and tell me.", out)
    check("nothing of the code survives", not re.search(r"const|console|```", out))
    check("~~~ fences are dropped too", s(CORPUS[2]) == "Try:\n\n" + M + "\n\nDone.")
    check("two separate blocks give two markers", s(CORPUS[3]) == "A\n\n" + M + "\n\nB\n\n" + M + "\n\nC")
    check("back-to-back blocks collapse to one marker", s(CORPUS[4]) == M)
    check("a message that is only code still speaks something", s(CORPUS[5]) == M)
    check("an unclosed fence runs to the end and is dropped", s(CORPUS[6]) == "Start.\n\n" + M)
    check("a longer fence can contain a shorter one", s(CORPUS[7]) == "Before\n\n" + M + "\n\nAfter")
    check("inline triple-backtick code is not a fence", "like this" in s(CORPUS[8]))
    check("four spaces of indent is not a fence, so nothing is dropped", "four spaces is NOT a fence" in s(CORPUS[11]))
    check("a closing fence needs nothing after it (trailing words keep the block open)",
          s(CORPUS[17]) == M + "\n\nreal end" or s(CORPUS[17]) == M, s(CORPUS[17]))
    check("None, '' and non-text never raise", s(None) == "" and s("") == "" and s(5) == "5" and s(["x"]) == "['x']")
    for text in CORPUS:
        once = s(text)
        check("idempotent: %r" % text[:30], s(once) == once, (once, s(once)))
    crlf = "Fix:\r\n```js\r\nx = 1\r\n```\r\nDone."
    check("CRLF text still has its fence found", s(crlf) == "Fix:\n\n" + M + "\n\nDone.", repr(s(crlf)))
    check("CRLF text with no fence comes back untouched", s("a\r\nb") == "a\r\nb")
    check("a bare CR (old Mac) is a line end too", s("x\r```\rcode\r```\ry") == "x\n\n" + M + "\n\ny")
    check("never returns empty for text that has anything in it",
          all(s(t) for t in CORPUS if t.strip()))


NODE = r"""
const fs = require('fs'), vm = require('vm');
const ctx = vm.createContext({ window: {}, console });
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const R = vm.runInContext('globalThis.JarvisRich', ctx);
const corpus = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(corpus.map((t) => R.speechText(t))));
"""


def test_parity():
    node = shutil.which("node")
    if not node:
        print("SKIP    python/javascript parity (node not installed)")
        return
    proc = subprocess.run([node, "-e", NODE, str(ROOT / "web" / "public" / "rich-text.js")],
                          input=json.dumps(CORPUS), capture_output=True, text=True, timeout=60)
    check("node ran the shared corpus", proc.returncode == 0 and proc.stdout.strip() != "", proc.stderr[-300:])
    if proc.returncode != 0:
        return
    js = json.loads(proc.stdout)
    check("same number of answers", len(js) == len(CORPUS))
    bad = [(t, ST.speech_text(t), j) for t, j in zip(CORPUS, js) if ST.speech_text(t) != j]
    check("every corpus text is spoken identically by Python and JavaScript", not bad)
    for b in bad[:5]:
        print("   ", b)
    check("the marker text is the same in both",
          "const SPOKEN_CODE_MARKER = \"%s\";" % M in (ROOT / "web" / "public" / "rich-text.js").read_text(encoding="utf-8"))


WRAPPER = r"""
import json, sys
sys.path.insert(0, %(cli)r)
from jarvis.voice import tts, config
spoken = []
def fake_speak(text, config=None, play=True, out_path=None):
    spoken.append({"text": text, "play": play, "out": out_path})
    return {"provider": "fake", "mime": "audio/fake", "played": False, "path": None}
tts.speak = fake_speak
config.voice_enabled = lambda cfg=None: True
sys.argv = ["jarvis", "speak"] + json.loads(%(args)r)
from jarvis import cli
try:
    cli.main()
finally:
    print("SPOKEN=" + json.dumps(spoken))
"""


def run_speak(args):
    code = WRAPPER % {"cli": str(CLI_DIR), "args": json.dumps(args)}
    p = subprocess.run([sys.executable, "-c", code], cwd=str(CLI_DIR), env=dict(os.environ),
                       capture_output=True, text=True, timeout=120)
    spoken = []
    for line in p.stdout.splitlines():
        if line.startswith("SPOKEN="):
            spoken = json.loads(line[len("SPOKEN="):])
    printed = "\n".join(l for l in p.stdout.splitlines() if not l.startswith("SPOKEN="))
    return p.returncode, spoken, printed, p.stderr


def test_speak_command():
    text = "Here:\n```sh\nrm -rf /tmp/x\n```\nSafe to run."
    code, spoken, printed, err = run_speak([text, "--no-play"])
    check("speak: exit 0", code == 0, (code, err[-300:]))
    check("speak: the speaker was handed the text with the code replaced",
          len(spoken) == 1 and spoken[0]["text"] == "Here:\n\n" + M + "\n\nSafe to run.", spoken)
    check("speak: --no-play still reaches the speaker call as play=False", spoken and spoken[0]["play"] is False)
    check("speak: stdout is still the same small JSON (ok / provider / mime / played / path)",
          '"ok": true' in printed and '"provider": "fake"' in printed and "rm -rf" not in printed, printed)
    code, spoken, printed, err = run_speak(["Just a plain sentence."])
    check("speak: a plain sentence goes through untouched", spoken and spoken[0]["text"] == "Just a plain sentence.", spoken)
    code, spoken, printed, err = run_speak(["```py\nprint(1)\n```"])
    check("speak: a message that is all code speaks the marker, not nothing", spoken and spoken[0]["text"] == M, spoken)
    already = "Fix:\n\n" + M + "\n\nDone."
    code, spoken, printed, err = run_speak([already, "--no-play"])
    check("speak: text the browser already stripped (the web path) passes through unchanged",
          spoken and spoken[0]["text"] == already, spoken)
    code, spoken, printed, err = run_speak(["   ", "--no-play"])
    check("speak: blank text is still the usage error and nothing is spoken", code == 1 and spoken == [] and "usage" in printed, (code, printed))


def test_static():
    cli_src = (CLI_DIR / "jarvis" / "cli.py").read_text(encoding="utf-8")
    listen = cli_src[cli_src.index('if argv[0] == "listen":'):cli_src.index('if argv[0] == "everything-config":')]
    speak = cli_src[cli_src.index('if argv[0] == "speak":'):cli_src.index('if argv[0] == "transcribe":')]
    check("listen imports and applies speech_text before speaking",
          "from .voice.speech_text import speech_text" in listen and "voice_tts.speak(speech_text(result.text)" in listen)
    check("listen still PRINTS the whole reply (only the spoken text is cut)",
          'print(f"{prefix}{result.text}")' in listen and listen.index('print(f"{prefix}{result.text}")') < listen.index("voice_tts.speak("))
    check("speak imports and applies speech_text", "from .voice.speech_text import speech_text" in speak and "speech_text(text)" in speak)
    check("speak still rejects blank text on the ORIGINAL text", re.search(r"text = .*strip\(\)\s*\n\s*if not text:", speak) is not None)
    mod = (CLI_DIR / "jarvis" / "voice" / "speech_text.py").read_text(encoding="utf-8")
    check("the module is pure: it imports nothing but re", re.findall(r"^(?:import|from)\s+(\S+)", mod, re.M) == ["re"])
    check("the notifier's voice channel is not changed by this (it still calls tts.speak itself)",
          "speech_text" not in (CLI_DIR / "jarvis" / "notifier.py").read_text(encoding="utf-8"))


test_rules()
test_parity()
test_speak_command()
test_static()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
