"""Table-driven tests for jarvis/dev_agent_errors.py's classify() and
missing_dependency_name() (§5 / §10 of the §3.6 plan):

- a ModuleNotFoundError traceback classifies as "missing_dependency" with
  the module name extracted.
- a Node "Cannot find module" trace classifies the same way.
- a Python SyntaxError traceback classifies as "syntax_error" with the
  correct file.
- an OSError "Address already in use" classifies as "port_in_use".
- a nonsense string classifies as "unknown" without raising.

No test framework dependency — plain asserts, runnable directly:

    python3 tests/test_dev_agent_errors.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import dev_agent_errors  # noqa: E402


MODULE_NOT_FOUND_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/tmp/project/app.py", line 1, in <module>\n'
    "    import flask\n"
    "ModuleNotFoundError: No module named 'flask'\n"
)

NODE_MODULE_NOT_FOUND = (
    "Error: Cannot find module 'express'\n"
    "Require stack:\n"
    "- /tmp/project/app.js\n"
    "    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1075:15)\n"
)

SYNTAX_ERROR_TRACEBACK = (
    '  File "/tmp/project/app.py", line 10\n'
    "    def foo(\n"
    "           ^\n"
    "SyntaxError: unexpected EOF while parsing\n"
)

IMPORT_ERROR_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/tmp/project/app.py", line 2, in <module>\n'
    "    from foo import bar\n"
    "ImportError: cannot import name 'bar' from 'foo'\n"
)

PORT_IN_USE_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/tmp/project/app.py", line 5, in <module>\n'
    "    app.run(port=5000)\n"
    "OSError: [Errno 98] Address already in use\n"
)

NODE_PORT_IN_USE = "Error: listen EADDRINUSE: address already in use :::3000\n"

MULTI_FRAME_RUNTIME_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "/tmp/project/app.py", line 3, in <module>\n'
    "    run()\n"
    '  File "/tmp/project/helpers.py", line 12, in run\n'
    "    raise ValueError('bad')\n"
    "ValueError: bad\n"
)

NONSENSE = "asdkjhasd 12903 %^&*() this is not a real error at all"


def test_module_not_found_classifies_as_missing_dependency_with_name_extracted():
    classified, target = dev_agent_errors.classify(MODULE_NOT_FOUND_TRACEBACK)
    assert classified == "missing_dependency"
    assert target is None
    assert dev_agent_errors.missing_dependency_name(MODULE_NOT_FOUND_TRACEBACK) == "flask"


def test_node_module_not_found_classifies_as_missing_dependency_with_name_extracted():
    classified, target = dev_agent_errors.classify(NODE_MODULE_NOT_FOUND)
    assert classified == "missing_dependency"
    assert target is None
    assert dev_agent_errors.missing_dependency_name(NODE_MODULE_NOT_FOUND) == "express"


def test_dotted_module_name_reduces_to_top_level_package():
    text = (
        "Traceback (most recent call last):\n"
        '  File "/tmp/project/app.py", line 1, in <module>\n'
        "    import requests.adapters\n"
        "ModuleNotFoundError: No module named 'requests.adapters'\n"
    )
    assert dev_agent_errors.missing_dependency_name(text) == "requests"


def test_syntax_error_classifies_with_correct_file():
    classified, target = dev_agent_errors.classify(SYNTAX_ERROR_TRACEBACK)
    assert classified == "syntax_error"
    assert target == "/tmp/project/app.py"


def test_import_error_classifies_with_correct_file():
    classified, target = dev_agent_errors.classify(IMPORT_ERROR_TRACEBACK)
    assert classified == "import_error"
    assert target == "/tmp/project/app.py"


def test_address_in_use_classifies_as_port_in_use():
    classified, target = dev_agent_errors.classify(PORT_IN_USE_TRACEBACK)
    assert classified == "port_in_use"
    assert target == "/tmp/project/app.py"


def test_node_eaddrinuse_classifies_as_port_in_use():
    classified, target = dev_agent_errors.classify(NODE_PORT_IN_USE)
    assert classified == "port_in_use"
    assert target is None  # no recognizable file/line in this trace


def test_multi_frame_traceback_picks_the_innermost_file():
    classified, target = dev_agent_errors.classify(MULTI_FRAME_RUNTIME_ERROR)
    assert classified == "runtime_error"
    assert target == "/tmp/project/helpers.py", (
        "the innermost (last-occurring) frame should win, not the outermost one"
    )


def test_nonsense_string_classifies_as_unknown_without_raising():
    classified, target = dev_agent_errors.classify(NONSENSE)
    assert classified == "unknown"
    assert target is None


def test_empty_and_none_stderr_classify_as_unknown_without_raising():
    assert dev_agent_errors.classify("") == ("unknown", None)
    assert dev_agent_errors.classify(None) == ("unknown", None)


def test_missing_dependency_name_returns_none_when_not_a_dependency_error():
    assert dev_agent_errors.missing_dependency_name(SYNTAX_ERROR_TRACEBACK) is None
    assert dev_agent_errors.missing_dependency_name(NONSENSE) is None
    assert dev_agent_errors.missing_dependency_name("") is None
    assert dev_agent_errors.missing_dependency_name(None) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
