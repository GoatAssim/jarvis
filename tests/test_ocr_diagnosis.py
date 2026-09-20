"""tool_diagnosis.diagnose() for read_screen / click_on_text.

The bug (master plan §2, "Tesseract on PATH but OCR still fails"): the
generic matcher blamed Tesseract for EVERY OCR error. Reproduced against the
real ocr_tools code paths before the fix:

  - Tesseract found, language data broken  -> "Missing dependency: tesseract,
    install it" (it is installed; the real reason was in the raw error).
  - Tesseract genuinely not on PATH        -> also "missing pytesseract,
    Pillow: pip install ..." (the install note in the message names them),
    and "reopen your terminal", which doesn't help a long-running web server.
  - pip packages missing                   -> also listed the Tesseract
    program as missing ("pytesseract" contains "tesseract").

The error strings used below for the Tesseract/pytesseract-raised cases are
the ones captured from a real pytesseract 0.3.13 + Tesseract 5.3.4 run.

Run: python3 tests/test_ocr_diagnosis.py
"""

import inspect
import os
import sys
import tempfile
from pathlib import Path

# HOME first (AGENTS.md > Testing): doctor is imported lazily by
# tool_diagnosis and must not see a developer's real ~/.jarvis.
_HOME = tempfile.mkdtemp(prefix="jarvis-ocr-diag-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ocr_tools  # noqa: E402
from jarvis import tool_diagnosis as td  # noqa: E402

# Verbatim from pytesseract.TesseractNotFoundError.
PYTESSERACT_NOT_FOUND = ("tesseract is not installed or it's not in your "
                         "PATH. See README file for more information.")
# Verbatim from a real run with TESSDATA_PREFIX pointing at a missing folder.
TESSDATA_FAILURE = (
    "(1, 'Error opening data file /nonexistent/eng.traineddata Please make "
    "sure the TESSDATA_PREFIX environment variable is set to your "
    "\"tessdata\" directory. Failed loading language \\'eng\\' Tesseract "
    "couldn\\'t load any languages! Could not initialize tesseract.')")
# Verbatim from a real run with a tesseract on PATH that exits non-zero.
BROKEN_BINARY = "Command '['tesseract', '--version']' returned non-zero exit status 127."

BOTH = ("read_screen", "click_on_text")


class _OnPlatform:
    """Pretend to be a given OS for the install hint / fix wording."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self._real = td.platform.system
        td.platform.system = lambda: self.name

    def __exit__(self, *exc):
        td.platform.system = self._real


def _names(diag):
    return [m["name"] for m in diag.get("missing", [])]


# --- Tesseract not on PATH (the stale-PATH case) ---------------------------

def test_binary_not_found_blames_only_the_program():
    err = ocr_tools._no_tesseract_binary(PYTESSERACT_NOT_FOUND)
    for tool in BOTH:
        d = td.diagnose(tool, err)
        assert d is not None
        assert _names(d) == ["tesseract"], _names(d)
        # The install note names pip packages; they must NOT be blamed.
        assert "pip install" not in d["fix"], d["fix"]
        assert d["cause"] and "PATH" in d["cause"]


def test_binary_not_found_windows_fix_covers_the_stale_path_case():
    err = ocr_tools._no_tesseract_binary(PYTESSERACT_NOT_FOUND)
    with _OnPlatform("Windows"):
        d = td.diagnose("read_screen", err)
    fix = d["fix"]
    assert "where tesseract" in fix
    # The point of the fix: the web server keeps the PATH it started with.
    assert "restart the web server" in fix and "double-clicking" in fix
    assert "winget install UB-Mannheim.TesseractOCR" in fix
    assert "Tesseract-OCR" in fix
    assert "reopen your terminal" not in fix


def test_binary_not_found_other_os_fix():
    err = ocr_tools._no_tesseract_binary(PYTESSERACT_NOT_FOUND)
    with _OnPlatform("Linux"):
        d = td.diagnose("read_screen", err)
    assert "which tesseract" in d["fix"]
    assert "sudo apt install tesseract-ocr" in d["fix"]


# --- pip packages missing ---------------------------------------------------

def test_pip_missing_blames_only_the_packages():
    d = td.diagnose("click_on_text", ocr_tools._no_pytesseract())
    assert d is not None
    assert sorted(_names(d)) == ["Pillow", "pytesseract"], _names(d)
    assert not any(m["kind"] == "program" for m in d["missing"])
    assert "pip install pytesseract Pillow" in d["fix"]


# --- Tesseract found, then failed (the "on PATH but still fails" case) -------

def test_language_data_failure_is_not_called_a_missing_install():
    err = {"error": f"OCR failed: {TESSDATA_FAILURE}"}
    for tool in BOTH:
        d = td.diagnose(tool, err)
        assert d is not None
        assert "missing" not in d, "Tesseract WAS found — nothing is missing"
        assert "TESSDATA_PREFIX" in d["fix"]
        assert "install" not in d["cause"].lower().replace("installed", "")
        assert "language data" in d["cause"]


def test_other_ran_and_failed_errors_point_at_the_real_error():
    d = td.diagnose("read_screen", {"error": f"OCR failed: {BROKEN_BINARY}"})
    assert d is not None
    assert "missing" not in d
    assert "--list-langs" in d["fix"]


# --- unrecognised shapes are left alone --------------------------------------

def test_capture_failure_gets_no_tesseract_advice():
    # "not found" in a capture error used to trigger the tesseract install hint.
    err = {"error": "screen capture failed: powershell not found"}
    for tool in BOTH:
        assert td.diagnose(tool, err) is None


def test_argument_errors_still_get_nothing():
    assert td.diagnose("click_on_text", {"error": "text is required"}) is None


# --- other tools are unaffected ---------------------------------------------

def test_other_tools_keep_the_generic_behaviour():
    # The generic path must still turn a missing ffmpeg into an install hint.
    d = td.diagnose("ytdl_download", {"error": "[WinError 2] The system cannot find the file specified"})
    assert d is not None
    assert any(m["name"] == "ffmpeg" for m in d.get("missing", []))


def test_annotate_attaches_the_new_diagnosis():
    err = {"error": f"OCR failed: {TESSDATA_FAILURE}"}
    out = td.annotate("read_screen", err)
    assert out is not err and out["diagnosis"]["cause"]
    assert out["error"] == err["error"], "the raw error must still travel with it"


# --- drift guard: the prefixes must match what ocr_tools really emits --------

def test_prefixes_match_the_real_ocr_tools_messages():
    assert ocr_tools._no_pytesseract()["error"].lower().startswith(td._OCR_NO_PIP)
    assert ocr_tools._no_tesseract_binary("x")["error"].lower().startswith(td._OCR_NO_BINARY)
    # "OCR failed: {e}" is an inline f-string in both tools, not a helper.
    src = inspect.getsource(ocr_tools)
    assert src.count('f"OCR failed: {e}"') == 2, (
        "ocr_tools' 'OCR failed:' message changed; update "
        "tool_diagnosis._OCR_RAN_AND_FAILED to match")
    assert td._OCR_RAN_AND_FAILED == "ocr failed"


# --- runner: keep test functions ABOVE this block (AGENTS.md > Testing) ------
if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in fns:
        try:
            f()
            print(f"ok   {n}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {n}\n     {e}")
    print(f"\n{len(fns) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
