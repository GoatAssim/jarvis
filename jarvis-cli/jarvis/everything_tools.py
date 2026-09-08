"""File search via voidtools' Everything (Windows only), plus two small
Explorer helpers for acting on a result.

Everything.exe must already be running \u2014 this module is only the SDK
client, never the indexer. See DOCUMENTATION/everything_sdk_reference.md
and DOCUMENTATION/everything_sdk_python_reference.md for the full API this
wraps, and everything_config.py for the settings file (DLL path override,
result-count defaults).

Like every other tools module here, every public function takes an
`arguments` dict and always returns a JSON-serializable dict \u2014 real data,
or `{"error": "..."}` \u2014 never raising, so a missing DLL or a stopped
Everything.exe degrades to an honest message instead of crashing the ask.

ctypes gotchas this module works around (see the python reference's
\u00a710 "Gotchas"): argtypes/restype are declared explicitly for every export
we call (ctypes defaults to c_int, which silently truncates DWORDs,
64-bit sizes and pointers), full-path buffers are sized generously
(4096, not MAX_PATH) since Everything itself imposes no path-length cap,
and every Set* filter is re-applied on every search rather than relying on
leftover state from a previous call.
"""

import ctypes
import os
import platform
import subprocess
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from . import everything_config

EVERYTHING_REQUEST_FILE_NAME = 0x00000001
EVERYTHING_REQUEST_PATH = 0x00000002
EVERYTHING_REQUEST_FULL_PATH_AND_FILE_NAME = 0x00000004
EVERYTHING_REQUEST_SIZE = 0x00000010
EVERYTHING_REQUEST_DATE_MODIFIED = 0x00000040
EVERYTHING_REQUEST_ATTRIBUTES = 0x00000100

_DEFAULT_REQUEST_FLAGS = (
    EVERYTHING_REQUEST_FILE_NAME
    | EVERYTHING_REQUEST_PATH
    | EVERYTHING_REQUEST_FULL_PATH_AND_FILE_NAME
    | EVERYTHING_REQUEST_SIZE
    | EVERYTHING_REQUEST_DATE_MODIFIED
    | EVERYTHING_REQUEST_ATTRIBUTES
)

_ATTR_DIRECTORY = 0x10

_SORT_TYPES = {
    "name_asc": 1, "name_desc": 2,
    "path_asc": 3, "path_desc": 4,
    "size_asc": 5, "size_desc": 6,
    "extension_asc": 7, "extension_desc": 8,
    "date_created_asc": 11, "date_created_desc": 12,
    "date_modified_asc": 13, "date_modified_desc": 14,
}

_ERROR_CODES = {
    1: "EVERYTHING_ERROR_MEMORY",
    2: "EVERYTHING_ERROR_IPC — Everything.exe doesn't seem to be running",
    3: "EVERYTHING_ERROR_REGISTERCLASSEX",
    4: "EVERYTHING_ERROR_CREATEWINDOW",
    5: "EVERYTHING_ERROR_CREATETHREAD",
    6: "EVERYTHING_ERROR_INVALIDINDEX",
    7: "EVERYTHING_ERROR_INVALIDCALL",
    8: "EVERYTHING_ERROR_INVALIDREQUEST",
    9: "EVERYTHING_ERROR_INVALIDPARAMETER",
}

_PATH_BUF_LEN = 4096
_WIN_TICKS_PER_SEC = 10_000_000
_EPOCH_DIFF_SECONDS = 11644473600  # seconds between 1601-01-01 and 1970-01-01

# Common install locations for the SDK zip's DLL, checked in order. The SDK
# is a separate download from Everything itself, so there's no registry key
# to read \u2014 these are just where people tend to unzip it.
_CANDIDATE_DLL_DIRS = [
    r"C:\EverythingSDK\DLL",
    r"C:\Program Files\Everything-SDK\DLL",
    r"C:\Program Files (x86)\Everything-SDK\DLL",
    r"C:\Everything-SDK\DLL",
]

_dll_cache = {"path": None, "dll": None}


def _is_64bit_python():
    return ctypes.sizeof(ctypes.c_void_p) == 8


def _dll_filename():
    return "Everything64.dll" if _is_64bit_python() else "Everything32.dll"


def _resolve_dll_path():
    """Explicit config override first, then the usual install spots. Returns
    a path string, or None if nothing was found."""
    cfg = everything_config.load_config()
    override = (cfg.get("dll_path") or "").strip()
    if override and Path(override).is_file():
        return override

    env_override = (os.environ.get("EVERYTHING_SDK_DLL") or "").strip()
    if env_override and Path(env_override).is_file():
        return env_override

    name = _dll_filename()
    for d in _CANDIDATE_DLL_DIRS:
        candidate = Path(d) / name
        if candidate.is_file():
            return str(candidate)
    return None


def _setup_dll(dll):
    """Declare argtypes/restype for every export we call \u2014 left to ctypes'
    c_int default, DWORDs/pointers/64-bit sizes get silently mangled."""
    dll.Everything_SetSearchW.argtypes = [ctypes.c_wchar_p]
    dll.Everything_SetMatchCase.argtypes = [wintypes.BOOL]
    dll.Everything_SetMatchPath.argtypes = [wintypes.BOOL]
    dll.Everything_SetMatchWholeWord.argtypes = [wintypes.BOOL]
    dll.Everything_SetRegex.argtypes = [wintypes.BOOL]
    dll.Everything_SetMax.argtypes = [wintypes.DWORD]
    dll.Everything_SetSort.argtypes = [wintypes.DWORD]
    dll.Everything_SetRequestFlags.argtypes = [wintypes.DWORD]
    dll.Everything_QueryW.argtypes = [wintypes.BOOL]
    dll.Everything_QueryW.restype = wintypes.BOOL
    dll.Everything_GetLastError.restype = wintypes.DWORD
    dll.Everything_GetNumResults.restype = wintypes.DWORD
    dll.Everything_GetTotResults.restype = wintypes.DWORD
    dll.Everything_IsFolderResult.argtypes = [wintypes.DWORD]
    dll.Everything_IsFolderResult.restype = wintypes.BOOL
    dll.Everything_GetResultFullPathNameW.argtypes = [wintypes.DWORD, ctypes.c_wchar_p, wintypes.DWORD]
    dll.Everything_GetResultSize.argtypes = [wintypes.DWORD, ctypes.POINTER(ctypes.c_ulonglong)]
    dll.Everything_GetResultSize.restype = wintypes.BOOL
    dll.Everything_GetResultDateModified.argtypes = [wintypes.DWORD, ctypes.POINTER(ctypes.c_ulonglong)]
    dll.Everything_GetResultDateModified.restype = wintypes.BOOL
    dll.Everything_GetResultAttributes.argtypes = [wintypes.DWORD]
    dll.Everything_GetResultAttributes.restype = wintypes.DWORD
    return dll


def _get_dll():
    """Load (or reuse) the Everything SDK DLL. Returns (dll, error) where
    exactly one of the two is None."""
    if platform.system() != "Windows":
        return None, "the Everything SDK is Windows-only"
    if not everything_config.is_enabled():
        return None, "Everything search is disabled in ~/.jarvis/everything.json"

    path = _resolve_dll_path()
    if not path:
        return None, (
            f"couldn't find {_dll_filename()} \u2014 download the SDK from "
            "https://www.voidtools.com/Everything-SDK.zip, unzip it, and either "
            "drop it in one of the usual spots or set dll_path in "
            "~/.jarvis/everything.json (run 'jarvis everything-config' to see that file)"
        )

    if _dll_cache["dll"] is not None and _dll_cache["path"] == path:
        return _dll_cache["dll"], None

    try:
        dll = _setup_dll(ctypes.WinDLL(path))
    except OSError as e:
        return None, f"couldn't load {path}: {e}"

    _dll_cache["dll"] = dll
    _dll_cache["path"] = path
    return dll, None


def _filetime_to_iso(ticks):
    if not ticks:
        return None
    try:
        dt = datetime.fromtimestamp(ticks / _WIN_TICKS_PER_SEC - _EPOCH_DIFF_SECONDS)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.isoformat(timespec="seconds")


EVERYTHING_TOOL_SCHEMAS = [
    {
        "name": "search_files",
        "description": (
            "Search the whole PC for files/folders by name, instantly, using Everything "
            "(voidtools). Use for 'find the file named X', 'where is Y', 'list PDFs in "
            "Downloads', etc. Needs Everything.exe running in the background. Supports its "
            "search syntax: 'ext:py' filters by extension, 'path:C:\\Users' restricts to a "
            "folder, 'size:>10mb' filters by size, quotes for an exact phrase."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search text, in Everything's search syntax (space = AND, | = OR, ext:, path:, size:, etc.).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max rows to return (default and cap set in ~/.jarvis/everything.json).",
                },
                "match_case": {"type": "boolean", "description": "Case-sensitive match. Default false."},
                "match_whole_word": {"type": "boolean", "description": "Whole-word match only. Default false."},
                "match_path": {"type": "boolean", "description": "Match against the full path, not just the file name. Default false."},
                "regex": {"type": "boolean", "description": "Treat query as a regular expression. Default false."},
                "sort": {
                    "type": "string",
                    "enum": list(_SORT_TYPES.keys()),
                    "description": "Result order. Default name_asc.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "reveal_in_explorer",
        "description": (
            "Open Windows File Explorer with the given file or folder already selected/"
            "highlighted, so the user can see exactly where it lives. Use after search_files "
            "when the user wants to see a result, not just hear its path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Full path to the file or folder to reveal."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "open_file_location",
        "description": (
            "Open the folder that contains the given path in File Explorer (the folder "
            "window itself, nothing pre-selected). If path is already a folder, opens it "
            "directly. Use when the user wants to browse around a result, not just spot it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Full path to a file or folder."},
            },
            "required": ["path"],
        },
    },
]


def search_files(arguments):
    arguments = arguments or {}
    query = arguments.get("query")
    if not query or not isinstance(query, str):
        return {"error": "query is required"}

    sort_key = (arguments.get("sort") or "name_asc").strip().lower()
    if sort_key not in _SORT_TYPES:
        return {"error": f"unknown sort: {sort_key!r} (expected one of {list(_SORT_TYPES.keys())})"}

    dll, err = _get_dll()
    if err:
        return {"error": err}

    cfg = everything_config.load_config()
    cap = int(cfg.get("max_results_cap") or 200)
    max_results = arguments.get("max_results")
    try:
        max_results = int(max_results) if max_results is not None else int(cfg.get("default_max_results") or 30)
    except (TypeError, ValueError):
        return {"error": "max_results must be a number"}
    max_results = max(1, min(max_results, cap))

    try:
        dll.Everything_SetSearchW(query)
        dll.Everything_SetMatchCase(bool(arguments.get("match_case")))
        dll.Everything_SetMatchWholeWord(bool(arguments.get("match_whole_word")))
        dll.Everything_SetMatchPath(bool(arguments.get("match_path", cfg.get("match_path_default", False))))
        dll.Everything_SetRegex(bool(arguments.get("regex")))
        dll.Everything_SetSort(_SORT_TYPES[sort_key])
        dll.Everything_SetRequestFlags(_DEFAULT_REQUEST_FLAGS)
        dll.Everything_SetMax(max_results)

        if not dll.Everything_QueryW(True):
            code = dll.Everything_GetLastError()
            return {"error": f"Everything query failed: {_ERROR_CODES.get(code, f'error code {code}')}"}

        num_results = dll.Everything_GetNumResults()
        total = dll.Everything_GetTotResults()

        size = ctypes.c_ulonglong()
        modified = ctypes.c_ulonglong()
        path_buf = ctypes.create_unicode_buffer(_PATH_BUF_LEN)
        results = []
        for i in range(num_results):
            dll.Everything_GetResultFullPathNameW(i, path_buf, _PATH_BUF_LEN)
            is_folder = bool(dll.Everything_IsFolderResult(i))
            entry = {
                "path": path_buf.value,
                "name": Path(path_buf.value).name,
                "is_folder": is_folder,
            }
            if not is_folder and dll.Everything_GetResultSize(i, ctypes.byref(size)):
                entry["size_bytes"] = size.value
            if dll.Everything_GetResultDateModified(i, ctypes.byref(modified)):
                entry["date_modified"] = _filetime_to_iso(modified.value)
            results.append(entry)

        dll.Everything_CleanUp()
        return {
            "ok": True,
            "query": query,
            "count": len(results),
            "total_matches": total,
            "truncated": total > len(results),
            "results": results,
        }
    except Exception as e:
        return {"error": f"search_files failed: {e}"}


def _resolve_target_path(raw_path):
    if not raw_path or not isinstance(raw_path, str):
        return None, {"error": "path is required"}
    path = Path(raw_path).expanduser()
    if not path.exists():
        return None, {"error": f"path not found: {path}"}
    return path, None


def reveal_in_explorer(arguments):
    arguments = arguments or {}
    if platform.system() != "Windows":
        return {"error": "reveal_in_explorer is Windows-only"}
    path, err = _resolve_target_path(arguments.get("path"))
    if err:
        return err
    try:
        # explorer.exe routinely returns a non-zero exit code even on
        # success, so this is fire-and-forget rather than checked.
        subprocess.Popen(["explorer", f"/select,{path}"])
        return {"ok": True, "revealed": str(path)}
    except OSError as e:
        return {"error": f"couldn't open Explorer: {e}"}


def open_file_location(arguments):
    arguments = arguments or {}
    if platform.system() != "Windows":
        return {"error": "open_file_location is Windows-only"}
    path, err = _resolve_target_path(arguments.get("path"))
    if err:
        return err
    folder = path if path.is_dir() else path.parent
    try:
        os.startfile(str(folder))  # noqa: S606 — Windows-only, user-provided path they already searched for
        return {"ok": True, "opened": str(folder)}
    except OSError as e:
        return {"error": f"couldn't open {folder}: {e}"}


EVERYTHING_TOOLS = {
    "search_files": search_files,
    "reveal_in_explorer": reveal_in_explorer,
    "open_file_location": open_file_location,
}
