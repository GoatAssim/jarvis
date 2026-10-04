#!/usr/bin/env python3
"""Auto-discovering test runner for tests/.

Every test_*.py file in this directory is a standalone script (plain
`assert`, no pytest dependency) that exits 0 on success and non-zero on
failure. This runner discovers them all, runs each as its own subprocess
(so one test's crash/exit can't take down the rest), and reports results
in either a one-line-per-file summary or a fully detailed view.

Usage (from the repo root, next to jarvis-cli/ and tests/):

    python3 tests/run_tests.py                     # run everything, simple output
    python3 tests/run_tests.py --detailed           # run everything, full output
    python3 tests/run_tests.py clipboard             # run tests whose name contains "clipboard"
    python3 tests/run_tests.py clipboard build_info   # run tests matching either
    python3 tests/run_tests.py --list                # list discovered tests, run nothing
    python3 tests/run_tests.py --list clipboard       # list only the matches for a filter
    python3 tests/run_tests.py -d clipboard_watch     # detailed output for one test
    python3 tests/run_tests.py --fail-fast            # stop at the first failing test
    python3 tests/run_tests.py --jobs 8               # run in parallel (output still ordered)
    python3 tests/run_tests.py --timeout 30           # override the per-test timeout (default 180s)

A test "matches" a filter if the filter text appears anywhere in its file
name (case-insensitive), with or without the "test_" prefix or ".py"
suffix — so "clipboard_watch", "test_clipboard_watch", and
"test_clipboard_watch.py" all select the same file, and "clipboard" alone
selects every clipboard test.

Exit code is 0 only if every selected test passed; 1 if any failed; 2 if
a filter matched nothing.
"""

import argparse
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent


def discover(filters):
    """Return sorted test_*.py paths, optionally narrowed by name filters."""
    all_tests = sorted(TESTS_DIR.glob("test_*.py"))
    if not filters:
        return all_tests
    needles = [f.lower().removeprefix("test_").removesuffix(".py") for f in filters]
    matched = []
    for path in all_tests:
        stem = path.stem.lower().removeprefix("test_")
        if any(n in stem for n in needles):
            matched.append(path)
    return matched


def run_one(path, timeout):
    """Run a single test file as a subprocess. Never raises — timeouts and
    crashes are captured as a failing result, same as any other failure."""
    start = time.monotonic()

    def as_text(x):
        if x is None:
            return ""
        return x.decode("utf-8", "replace") if isinstance(x, bytes) else x

    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            # No test should ever need input; inheriting the runner's stdin
            # risks a silent hang (e.g. an accidental input()) that looks
            # exactly like a slow test until the timeout finally fires.
            stdin=subprocess.DEVNULL,
        )
        elapsed = time.monotonic() - start
        output = as_text(proc.stdout) + as_text(proc.stderr)
        return {
            "path": path,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "output": output,
            "elapsed": elapsed,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - start
        partial = as_text(e.stdout) + as_text(e.stderr)
        return {
            "path": path,
            "ok": False,
            "returncode": None,
            "output": partial + f"\n[TIMED OUT after {timeout}s]",
            "elapsed": elapsed,
            "timed_out": True,
        }


def last_line(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            return line
    return "(no output)"


_FAIL_LINE = re.compile(r"^\s*(FAILED|FAIL)\b|^\s*-\s+\S.*:\s")
_EXC_LINE = re.compile(r"^[\w.]*(Error|Exception|Exit)\b.*")
_COUNT_LINE = re.compile(r"\d+\s*(/\s*\d+)?\s*(passed|checks passed)", re.I)


def failure_summary(text, limit=6):
    """Why a test file failed, as a short list of lines.

    last_line() alone is misleading for a FAILING test: a test that prints
    "42 passed, 2 failed" to stdout and then has some library write a
    "[tools] Auto-discovered ..." notice to stderr ends with that notice as
    its last line, hiding the real failure (this is exactly what happened
    with test_key_health / test_path_tools on Windows). So prefer, in order:
    the FAILED/FAIL lines the tests print themselves, the final exception
    line of a traceback, and the "N passed, M failed" count line.
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    picked = [ln.strip() for ln in lines if _FAIL_LINE.search(ln)]
    exc = [ln.strip() for ln in lines if _EXC_LINE.match(ln)]
    if exc:
        picked.append(exc[-1])
    counts = [ln.strip() for ln in lines if _COUNT_LINE.search(ln)]
    if counts:
        picked.append(counts[-1])
    seen, out = set(), []
    for ln in picked:
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    return out[:limit] or [last_line(text)]


def print_detailed(result):
    path, ok = result["path"], result["ok"]
    status = "PASS" if ok else "FAIL"
    print(f"\n{'=' * 70}")
    print(f"[{status}] {path.name}  ({result['elapsed']:.2f}s)")
    print("=" * 70)
    print(result["output"].rstrip() or "(no output)")


def print_simple(result):
    path, ok = result["path"], result["ok"]
    mark = "ok  " if ok else "FAIL"
    if ok:
        # Prefer the test's own "N passed" line over whatever a library
        # happened to write to stderr last.
        counts = [ln.strip() for ln in result["output"].splitlines() if _COUNT_LINE.search(ln)]
        summary = counts[-1] if counts else last_line(result["output"])
    else:
        summary = failure_summary(result["output"])[0]
    print(f"{mark}  {path.name:<45} {result['elapsed']:6.2f}s  {summary}")


def main():
    parser = argparse.ArgumentParser(
        description="Auto-discover and run every test_*.py in tests/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("filters", nargs="*", help="only run tests whose name contains any of these")
    parser.add_argument("--list", action="store_true", help="list matching tests and exit, run nothing")
    parser.add_argument("-d", "--detailed", action="store_true", help="print full output for every test")
    parser.add_argument("--fail-fast", action="store_true", help="stop after the first failure (sequential only)")
    parser.add_argument("--timeout", type=float, default=180.0, help="per-test timeout in seconds (default 180 — "
                         "several tests do real, unmocked sleeps: test_streaming_other_adapters.py alone runs "
                         "~65s, and any test that exercises multiple rounds on one key through ai_providers.py's "
                         "adapters without mocking key_health now eats K.3.6's real pacing sleeps too)")
    parser.add_argument("--jobs", "-j", type=int, default=1, help="run this many tests in parallel (default 1)")
    args = parser.parse_args()

    tests = discover(args.filters)

    if args.filters and not tests:
        print(f"No tests matched: {', '.join(args.filters)}", file=sys.stderr)
        available = ", ".join(p.stem for p in discover([]))
        print(f"\nAvailable: {available}", file=sys.stderr)
        return 2

    if args.list:
        for p in tests:
            print(p.name)
        print(f"\n{len(tests)} test file(s)", file=sys.stderr)
        return 0

    print(f"Running {len(tests)} test file(s)"
          + (f" matching {args.filters}" if args.filters else "")
          + f"  (jobs={args.jobs}, timeout={args.timeout:.0f}s)\n", file=sys.stderr)

    results = []
    start_all = time.monotonic()

    if args.jobs <= 1:
        for path in tests:
            result = run_one(path, args.timeout)
            results.append(result)
            (print_detailed if args.detailed else print_simple)(result)
            if args.fail_fast and not result["ok"]:
                print("\n--fail-fast: stopping after first failure.", file=sys.stderr)
                break
    else:
        # Run in parallel, but print in discovery order so output stays
        # readable/diffable regardless of which test happens to finish first.
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for result in pool.map(lambda p: run_one(p, args.timeout), tests):
                results.append(result)
        for result in results:
            (print_detailed if args.detailed else print_simple)(result)

    total_elapsed = time.monotonic() - start_all
    passed = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    print(f"\n{'-' * 70}")
    print(f"{len(passed)}/{len(results)} test files passed in {total_elapsed:.1f}s"
          + (f"  ({len(tests) - len(results)} not run, --fail-fast)" if len(results) < len(tests) else ""))
    if failed:
        print("\nFailed:")
        for r in failed:
            tag = "TIMEOUT" if r["timed_out"] else f"exit {r['returncode']}"
            reasons = failure_summary(r["output"])
            print(f"  - {r['path'].name}  ({tag}): {reasons[0]}")
            for extra in reasons[1:]:
                print(f"        {extra}")

    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # e.g. `python3 tests/run_tests.py --list | head` — not an error.
        sys.exit(0)
