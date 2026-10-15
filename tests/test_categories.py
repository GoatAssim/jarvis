"""L.11 / L.14.3 - category-name rules, and that Python and JavaScript agree.

`jarvis/categories.py` and `web/public/category-input.js` implement the same
rules. This runs one corpus through both and fails on any difference, so a name
the editor accepts can never be re-spelled by the registry when it is saved.
The parity half needs `node` and prints SKIP without it.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import categories as C  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def test_name_rules():
    n = C.normalize_name
    check("trims and collapses whitespace", n("  Web   stuff \u00a0 ") == "Web stuff")
    check("a control character becomes a space, not glue", n("a\tb\nc\x00d") == "a b c d")
    check("empty / blank / non-text is no name", n("") == "" and n("   ") == "" and n(None) == "" and n(5) == "" and n(["x"]) == "")
    check("cut to the length limit, without a trailing space", n("x" * 30) == "x" * 24 and n("a" * 23 + " b") == "a" * 23)
    check("strict mode can measure the overflow", len(n("x" * 30, truncate=False)) == 30)
    check("key is case-insensitive", C.key_of("Utility") == C.key_of(" utility ") == "utility")
    check("counts code points, not UTF-16 units", n("\U0001F600" * 24) == "\U0001F600" * 24 and len(n("\U0001F600" * 30)) == 24)


def test_list_rules():
    nl = C.normalize_list
    check("first spelling wins on a case-insensitive duplicate", nl(["Utility", "utility", " UTILITY "])[0] == ["Utility"])
    check("blank entries are dropped, not errors", nl(["a", "", "  ", "b"], strict=True) == (["a", "b"], ""))
    check("None and a bare string are accepted", nl(None) == ([], "") and nl("solo") == (["solo"], ""))
    check("order is kept", nl(["b", "a", "c"])[0] == ["b", "a", "c"])
    nine = [str(i) for i in range(9)]
    check("lenient keeps the first 8", nl(nine)[0] == nine[:8] and nl(nine)[1] == "")
    lst, err = nl(nine, strict=True)
    check("strict refuses a ninth and says why", lst == [] and "at most 8" in err)
    lst, err = nl(["x" * 25], strict=True)
    check("strict refuses an over-long name rather than renaming it", lst == [] and "longer than 24" in err)
    check("lenient cuts an over-long name", nl(["x" * 25])[0] == ["x" * 24])
    check("strict refuses non-text, lenient skips it", nl([1], strict=True)[1] != "" and nl([1, "a"])[0] == ["a"])
    check("strict refuses a non-list, lenient shrugs", nl({"a": 1}, strict=True)[1] != "" and nl({"a": 1}) == ([], ""))
    check("two long names that collide after the cut are one", nl(["x" * 24 + "a", "x" * 24 + "b"])[0] == ["x" * 24])


CORPUS = [
    "Utility", " utility ", "UTILITY", "Web   stuff", "a\tb", "a\nb\x00c", "\u00a0nbsp\u00a0", "x" * 24, "x" * 25, "x" * 40,
    "a" * 23 + " b", "", "   ", "\U0001F600" * 24, "\U0001F600" * 30, "caf\u00e9", "CAF\u00c9", "\u0130stanbul", "stra\u00dfe",
    "\u03a3\u03a3", "tab\u2003em", "line\u2028sep", "\ufefftrim", "ok", "Ok", "oK", "a,b", "--flag", "-x", "emoji \u2764\ufe0f",
    "\x7f", "\u0085nel", "a\u200bb",
]
LISTS = [
    [], ["a"], ["a", "A", "b"], ["Utility", "utility"], ["", "  ", "x"], [str(i) for i in range(8)], [str(i) for i in range(9)],
    ["x" * 25], ["x" * 24 + "a", "x" * 24 + "b"], ["a", 1, "b"], "solo", None, {"a": 1}, ["\u0130", "i\u0307"],
] + [[w] for w in CORPUS]

# (list, old, new) for rename; (list, name) is the first two for remove.
EDITS = [
    (["a", "Utility", "b"], "utility", "Tools"), (["UTILITY"], "utility", "x"), (["a"], "zzz", "x"),
    (["Utility", "tools"], "Utility", "Tools"), (["utility"], "utility", "Utility"), (["a"], "a", "a"),
    (["a"], "", "x"), (["a"], "a", "  "), (None, "a", "b"), ("solo", "solo", "x"), (["\u0130stanbul"], "\u0130stanbul", "x"),
    (["caf\u00e9"], "CAF\u00c9", "Caf\u00e9s"), (["a", "b", "c"], "b", "c"), ([str(i) for i in range(8)], "3", "9"),
    (["x" * 24], "x" * 24, "y" * 30), (["a", "a "], "a", "b"), (["Ok", "oK"], "ok", "OK"),
]
NEWS = ["  Web   stuff ", "", "   ", 5, None, "x" * 24, "x" * 25, "\U0001F600" * 24, "\U0001F600" * 25, "a\tb", "--flag"]

NODE = r"""
const vm = require('vm'), fs = require('fs');
const ctx = vm.createContext({ window: {}, console });
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const C = ctx.window.JarvisCategories;
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {
  names: input.names.map((s) => [C.normalizeName(s), C.normalizeName(s, false), C.keyOf(s)]),
  lists: input.lists.map((l) => { const a = C.normalizeList(l, false), b = C.normalizeList(l, true); return [a.list, a.error, b.list, b.error]; }),
  limits: [C.MAX_NAME_LEN, C.MAX_PER_ITEM],
  rename: (input.edits || []).map(([l, o, n]) => { const r = C.renameInList(l, o, n); return [r.list, r.changed]; }),
  remove: (input.edits || []).map(([l, o]) => { const r = C.removeFromList(l, o); return [r.list, r.changed]; }),
  checkNew: (input.news || []).map((v) => { const r = C.checkNewName(v); return [r.name, r.error]; }),
};
process.stdout.write(JSON.stringify(out));
"""


def test_python_and_javascript_agree():
    node = shutil.which("node")
    if not node:
        print("SKIP    python/javascript parity (node not installed)")
        return
    proc = subprocess.run([node, "-e", NODE, str(ROOT / "web" / "public" / "category-input.js")],
                          input=json.dumps({"names": CORPUS, "lists": LISTS, "edits": EDITS, "news": NEWS}),
                          capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    # utf-8 both ways: the corpus holds non-ASCII names and node speaks UTF-8; text=True would use the
    # Windows ANSI code page, whose decode error kills the reader thread and leaves proc.stdout None.
    check("node ran the shared corpus", proc.returncode == 0 and (proc.stdout or "").strip() != "")
    if proc.returncode != 0:
        print(proc.stderr)
        return
    js = json.loads(proc.stdout)
    check("limits are the same number in both", js["limits"] == [C.MAX_NAME_LEN, C.MAX_PER_ITEM])
    bad = []
    for s, got in zip(CORPUS, js["names"]):
        want = [C.normalize_name(s), C.normalize_name(s, truncate=False), C.key_of(s)]
        if want != got:
            bad.append(("name", s, want, got))
    check("every name normalises identically", not bad)
    for b in bad[:5]:
        print("   ", b)
    badl = []
    for l, got in zip(LISTS, js["lists"]):
        a, ae = C.normalize_list(l)
        b, be = C.normalize_list(l, strict=True)
        # JS has no tuples, and a JSON string/None/dict input arrives unchanged.
        if [a, ae, b, be] != got:
            badl.append((l, [a, ae, b, be], got))
    check("every list normalises identically, lenient and strict, errors included", not badl)
    bade = []
    for (l, o, n), gr, gd in zip(EDITS, js["rename"], js["remove"]):
        r = C.rename_in_list(l, o, n)
        d = C.remove_from_list(l, o)
        if [list(r[0]), r[1]] != gr or [list(d[0]), d[1]] != gd:
            bade.append((l, o, n, r, d, gr, gd))
    check("rename and remove edit a list identically in both", not bade)
    for b in bade[:5]:
        print("   ", b)
    badn = [(v, C.check_new_name(v), g) for v, g in zip(NEWS, js["checkNew"]) if list(C.check_new_name(v)) != g]
    check("the strict check of a new name agrees in both (message included)", not badn)
    for b in badn[:5]:
        print("   ", b)
    for b in badl[:5]:
        print("   ", b)


test_name_rules()
test_list_rules()
test_python_and_javascript_agree()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
