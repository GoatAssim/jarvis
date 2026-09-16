"""Traceback/stack-trace parsing + error classification for dev_agent's
self-fix loop (§4.4/§5 of the dev_agent implementation plan).

classify() turns a captured stderr tail into a small closed set of labels
the loop in actions/dev_agent.py branches on, plus (when it can find one)
the single file most likely responsible — so _fix_files can hand the
writer model just that file's current content instead of the whole
project. Never raises: worst case is ("unknown", None), which the loop
still gets one writer-model retry with the raw traceback before giving up
(see §5's "never a hardcoded wall on the first unknown error shape").
"""

import re

# Ordered: first match wins. Order matters because some patterns are
# substrings of what a more specific pattern would also match (e.g. every
# ImportError line also could loosely resemble a generic exception line).
_MODULE_NOT_FOUND = re.compile(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]")
_NODE_MODULE_NOT_FOUND = re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")
_ADDR_IN_USE = re.compile(r"address already in use|EADDRINUSE", re.IGNORECASE)
_SYNTAX_ERROR = re.compile(r"SyntaxError: (.+)")
_IMPORT_ERROR = re.compile(r"ImportError: (.+)")
_PY_TRACEBACK_FILE = re.compile(r'File "([^"]+)", line (\d+)')
_NODE_STACK_FILE = re.compile(r"\(([^():]+):(\d+):(\d+)\)|at .*\(?([^\s():]+\.js):(\d+):(\d+)\)?")


def classify(stderr_tail):
    """Return (classified, target_file). `classified` is one of:
    "missing_dependency", "syntax_error", "import_error", "port_in_use",
    "runtime_error", "unknown". `target_file` is the innermost
    file (absolute or relative, as it appeared in the trace) the loop
    should hand back to the writer model — may be None if none was
    found, e.g. for "missing_dependency" where the fix is
    add-to-requirements, not a file edit."""
    text = stderr_tail or ""
    try:
        m = _MODULE_NOT_FOUND.search(text)
        if m:
            return "missing_dependency", None
        m = _NODE_MODULE_NOT_FOUND.search(text)
        if m:
            return "missing_dependency", None
        if _ADDR_IN_USE.search(text):
            return "port_in_use", _last_file(text)
        if _SYNTAX_ERROR.search(text):
            return "syntax_error", _last_file(text)
        if _IMPORT_ERROR.search(text):
            return "import_error", _last_file(text)
        target = _last_file(text)
        if target:
            return "runtime_error", target
        return "unknown", None
    except Exception:
        return "unknown", None


def missing_dependency_name(stderr_tail):
    """Best-effort extraction of the missing module/package name from a
    "missing_dependency"-classified stderr tail, for adding it straight
    to the dependency list and reinstalling. Returns None if it can't
    find one (the fix loop still falls back to a writer-model retry)."""
    text = stderr_tail or ""
    m = _MODULE_NOT_FOUND.search(text)
    if m:
        return m.group(1).split(".")[0]
    m = _NODE_MODULE_NOT_FOUND.search(text)
    if m:
        return m.group(1)
    return None


def _last_file(text):
    """Innermost (last-occurring) file+line mentioned in a Python
    traceback or Node stack trace — "innermost" because both formats
    print frames outermost-first, so the last match is the deepest
    frame, which is usually where the actual bug lives rather than
    just where it surfaced."""
    last = None
    for m in _PY_TRACEBACK_FILE.finditer(text):
        last = m.group(1)
    if last:
        return last
    for m in _NODE_STACK_FILE.finditer(text):
        f = m.group(1) or m.group(4)
        if f:
            last = f
    return last
