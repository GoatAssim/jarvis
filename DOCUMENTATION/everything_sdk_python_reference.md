# Everything SDK — Python Reference

Windows-only. Lets you query voidtools' **Everything** search engine from code. Requires `Everything.exe` already running in the background — the SDK does not launch it.

> **Accuracy note:** `Everything_SetSearch` and the `EVERYTHING_REQUEST_*` flags below are pulled directly from voidtools' own SDK page and Python sample. The rest of the ~70 functions follow the SDK's long-stable, consistent naming/signature convention documented individually on voidtools.com — links to every single one are in §11. If a specific call misbehaves, that's the first place to check.

## Contents
1. Requirements & Getting the SDK
2. Loading the DLL in Python
3. Minimal Example
4. Typical Call Sequence
5. Constants
6. Function Reference (all ~70 functions)
7. Search Syntax Quick Reference
8. FILETIME → datetime
9. Practical Wrapper (copy-paste ready)
10. Gotchas
11. Further Reading (every official page)

---

## 1. Requirements & Getting the SDK

- **Everything.exe** installed and running (any recent version). No server/ETP setup needed — the DLL talks to it via local IPC automatically.
- **The SDK itself** — a separate download from voidtools (`Everything-SDK.zip`), not bundled with the normal installer. Get it from the [SDK page](https://www.voidtools.com/support/everything/sdk). It contains `Everything32.dll`, `Everything64.dll`, `Everything.h`, `Everything.lib`, and samples in C, C#, Clarion, Python and VB.
- **DLL bitness must match your Python interpreter**, not your OS — 64-bit Python → `Everything64.dll`, 32-bit Python → `Everything32.dll`. Mismatch is the classic `[WinError 193] %1 is not a valid Win32 application`.
- No official PyPI package — everyone talks to the DLL via `ctypes`, exactly like voidtools' own [Python sample](https://www.voidtools.com/support/everything/sdk/python).

## 2. Loading the DLL in Python

```python
import ctypes
dll = ctypes.WinDLL(r"C:\EverythingSDK\DLL\Everything64.dll")
```

Nearly every string-based export exists in two flavors:
- `...A` — ANSI (`char*`)
- `...W` — Unicode (`wchar_t*`)

**Use the `W` versions.** Python 3 `str` objects pass straight through as `ctypes.c_wchar_p`.

## 3. Minimal Example

```python
import ctypes

dll = ctypes.WinDLL(r"C:\EverythingSDK\DLL\Everything64.dll")

dll.Everything_SetSearchW("*.py")
dll.Everything_QueryW(True)          # True = block until results are ready

buf = ctypes.create_unicode_buffer(4096)
for i in range(dll.Everything_GetNumResults()):
    dll.Everything_GetResultFullPathNameW(i, buf, 4096)
    print(buf.value)

dll.Everything_CleanUp()
```

## 4. Typical Call Sequence

1. `SetSearch(query)` — required
2. Optional filters: `SetMatchCase`, `SetMatchPath`, `SetMatchWholeWord`, `SetRegex`, `SetSort`, `SetMax`, `SetOffset`
3. `SetRequestFlags(...)` — which fields you want back per result
4. `Query(True)` — execute (blocking)
5. `GetNumResults()` — how many rows came back
6. Loop `0..num-1` calling the `GetResult*` getters
7. `CleanUp()` when done with the result set

---

## 5. Constants

### 5.1 Request flags — `SetRequestFlags()` (bitwise OR)
| Constant | Value |
|---|---|
| `EVERYTHING_REQUEST_FILE_NAME` | `0x00000001` |
| `EVERYTHING_REQUEST_PATH` | `0x00000002` |
| `EVERYTHING_REQUEST_FULL_PATH_AND_FILE_NAME` | `0x00000004` |
| `EVERYTHING_REQUEST_EXTENSION` | `0x00000008` |
| `EVERYTHING_REQUEST_SIZE` | `0x00000010` |
| `EVERYTHING_REQUEST_DATE_CREATED` | `0x00000020` |
| `EVERYTHING_REQUEST_DATE_MODIFIED` | `0x00000040` |
| `EVERYTHING_REQUEST_DATE_ACCESSED` | `0x00000080` |
| `EVERYTHING_REQUEST_ATTRIBUTES` | `0x00000100` |
| `EVERYTHING_REQUEST_FILE_LIST_FILE_NAME` | `0x00000200` |
| `EVERYTHING_REQUEST_RUN_COUNT` | `0x00000400` |
| `EVERYTHING_REQUEST_DATE_RUN` | `0x00000800` |
| `EVERYTHING_REQUEST_DATE_RECENTLY_CHANGED` | `0x00001000` |
| `EVERYTHING_REQUEST_HIGHLIGHTED_FILE_NAME` | `0x00002000` |
| `EVERYTHING_REQUEST_HIGHLIGHTED_PATH` | `0x00004000` |
| `EVERYTHING_REQUEST_HIGHLIGHTED_FULL_PATH_AND_FILE_NAME` | `0x00008000` |

### 5.2 Sort types — `SetSort()`
| Value | Constant | Value | Constant |
|---|---|---|---|
| 1 | `NAME_ASCENDING` | 2 | `NAME_DESCENDING` |
| 3 | `PATH_ASCENDING` | 4 | `PATH_DESCENDING` |
| 5 | `SIZE_ASCENDING` | 6 | `SIZE_DESCENDING` |
| 7 | `EXTENSION_ASCENDING` | 8 | `EXTENSION_DESCENDING` |
| 9 | `TYPE_NAME_ASCENDING` | 10 | `TYPE_NAME_DESCENDING` |
| 11 | `DATE_CREATED_ASCENDING` | 12 | `DATE_CREATED_DESCENDING` |
| 13 | `DATE_MODIFIED_ASCENDING` | 14 | `DATE_MODIFIED_DESCENDING` |
| 15 | `ATTRIBUTES_ASCENDING` | 16 | `ATTRIBUTES_DESCENDING` |
| 17 | `FILE_LIST_FILENAME_ASCENDING` | 18 | `FILE_LIST_FILENAME_DESCENDING` |
| 19 | `RUN_COUNT_ASCENDING` | 20 | `RUN_COUNT_DESCENDING` |
| 21 | `DATE_RECENTLY_CHANGED_ASCENDING` | 22 | `DATE_RECENTLY_CHANGED_DESCENDING` |
| 23 | `DATE_ACCESSED_ASCENDING` | 24 | `DATE_ACCESSED_DESCENDING` |
| 25 | `DATE_RUN_ASCENDING` | 26 | `DATE_RUN_DESCENDING` |

All prefixed `EVERYTHING_SORT_`.

### 5.3 Error codes — `GetLastError()`
| Value | Constant |
|---|---|
| 0 | `EVERYTHING_OK` |
| 1 | `EVERYTHING_ERROR_MEMORY` |
| 2 | `EVERYTHING_ERROR_IPC` (Everything not running / not reachable) |
| 3 | `EVERYTHING_ERROR_REGISTERCLASSEX` |
| 4 | `EVERYTHING_ERROR_CREATEWINDOW` |
| 5 | `EVERYTHING_ERROR_CREATETHREAD` |
| 6 | `EVERYTHING_ERROR_INVALIDINDEX` |
| 7 | `EVERYTHING_ERROR_INVALIDCALL` |
| 8 | `EVERYTHING_ERROR_INVALIDREQUEST` |
| 9 | `EVERYTHING_ERROR_INVALIDPARAMETER` |

### 5.4 Target machine — `GetTargetMachine()`
`1` = x86, `2` = x64, `3` = ARM (architecture of the running `Everything.exe`)

### 5.5 Common `GetResultAttributes()` bits (standard Win32, not Everything-specific)
`READONLY=0x1`, `HIDDEN=0x2`, `SYSTEM=0x4`, `DIRECTORY=0x10`, `ARCHIVE=0x20`

---

## 6. Function Reference

Type legend: `DWORD`=32-bit unsigned int · `BOOL`=int, 0=false · index params are 0-based `DWORD`. Every `Get*Name`/`Get*Path` style function below auto-copies its string into a real Python `str` **if you set `restype = ctypes.c_wchar_p`** — no manual buffer needed. Only `GetResultFullPathName`(+ its highlighted variant) fills a buffer you provide, since Everything must synthesize that combined string on demand.

### 6.1 Search parameters (Set/Get pairs)
| Function | Params | Returns | Notes |
|---|---|---|---|
| `Everything_SetSearchW` | `query: str` | — | The search text. [Full official docs](https://www.voidtools.com/support/everything/sdk/everything_setsearch) — space = AND, confirmed by voidtools' own example (`"abc 123"` = abc AND 123). |
| `Everything_GetSearchW` | — | `str` | Current search text. |
| `Everything_SetMatchCase` | `enable: bool` | — | Case-sensitive matching. |
| `Everything_GetMatchCase` | — | `bool` | |
| `Everything_SetMatchPath` | `enable: bool` | — | Match against full path, not just filename. |
| `Everything_GetMatchPath` | — | `bool` | |
| `Everything_SetMatchWholeWord` | `enable: bool` | — | |
| `Everything_GetMatchWholeWord` | — | `bool` | |
| `Everything_SetRegex` | `enable: bool` | — | Treat the search string as regex. |
| `Everything_GetRegex` | — | `bool` | |
| `Everything_SetMax` | `n: int` | — | Cap on results returned by `Query()`. Leave unset for no cap. |
| `Everything_GetMax` | — | `int` | |
| `Everything_SetOffset` | `n: int` | — | Skip the first *n* matches — pagination. |
| `Everything_GetOffset` | — | `int` | |
| `Everything_SetSort` | `sort: int` | — | One of §5.2. |
| `Everything_GetSort` | — | `int` | |
| `Everything_SetRequestFlags` | `flags: int` | — | OR of §5.1 — which fields to fetch per result. |
| `Everything_GetRequestFlags` | — | `int` | |
| `Everything_SetReplyWindow` | `hwnd: int` | — | Only for the low-level async window-message IPC path — irrelevant if you use blocking `Query(True)` as almost all Python/ctypes code does. |
| `Everything_GetReplyWindow` | — | `int` | |
| `Everything_SetReplyID` | `id: int` | — | Same async-only caveat. |
| `Everything_GetReplyID` | — | `int` | |

### 6.2 Query execution
| Function | Params | Returns | Notes |
|---|---|---|---|
| `Everything_QueryW` | `wait: bool` | `bool` | Runs the query. `wait=True` blocks until ready (use this). `False` return → check `GetLastError()`. |
| `Everything_IsQueryReply` | `message, wParam, lParam, dwId` | `bool` | Only used with the raw async window-message IPC — not needed for typical ctypes scripts. |

### 6.3 Result counts
| Function | Returns | Notes |
|---|---|---|
| `Everything_GetNumResults` | `int` | Rows actually available this call (bounded by Max/Offset). |
| `Everything_GetNumFileResults` | `int` | Same, files only. |
| `Everything_GetNumFolderResults` | `int` | Same, folders only. |
| `Everything_GetTotResults` | `int` | Total matches **ignoring** Max/Offset — use for "N results found" / pagination UI. |
| `Everything_GetTotFileResults` | `int` | Same, files only. |
| `Everything_GetTotFolderResults` | `int` | Same, folders only. |

### 6.4 Per-result getters (`i: int` = result index)
| Function | Params | Returns | Requires flag |
|---|---|---|---|
| `Everything_GetResultFileNameW` | `i` | `str` | `FILE_NAME` |
| `Everything_GetResultPathW` | `i` | `str` | `PATH` |
| `Everything_GetResultFullPathNameW` | `i, buf, buf_len` | fills `buf` | `PATH`+`FILE_NAME` |
| `Everything_GetResultExtensionW` | `i` | `str` | `EXTENSION` |
| `Everything_GetResultSize` | `i, POINTER(c_ulonglong)` | `bool`, fills pointer with bytes | `SIZE` |
| `Everything_GetResultDateCreated` | `i, POINTER(c_ulonglong)` | `bool`, fills FILETIME | `DATE_CREATED` |
| `Everything_GetResultDateModified` | `i, POINTER(c_ulonglong)` | `bool`, fills FILETIME | `DATE_MODIFIED` |
| `Everything_GetResultDateAccessed` | `i, POINTER(c_ulonglong)` | `bool`, fills FILETIME | `DATE_ACCESSED` |
| `Everything_GetResultDateRun` | `i, POINTER(c_ulonglong)` | `bool`, fills FILETIME | `DATE_RUN` |
| `Everything_GetResultDateRecentlyChanged` | `i, POINTER(c_ulonglong)` | `bool`, fills FILETIME | `DATE_RECENTLY_CHANGED` |
| `Everything_GetResultAttributes` | `i` | `int` (Win32 bitmask, §5.5) | `ATTRIBUTES` |
| `Everything_GetResultFileListFileNameW` | `i` | `str` | `FILE_LIST_FILE_NAME` — only meaningful when searching a loaded file list |
| `Everything_GetResultRunCount` | `i` | `int` | `RUN_COUNT` |
| `Everything_GetResultHighlightedFileNameW` | `i` | `str`, `*`-wrapped matches | `HIGHLIGHTED_FILE_NAME` |
| `Everything_GetResultHighlightedPathW` | `i` | `str` | `HIGHLIGHTED_PATH` |
| `Everything_GetResultHighlightedFullPathAndFileNameW` | `i, buf, buf_len` | fills `buf` | `HIGHLIGHTED_FULL_PATH_AND_FILE_NAME` |
| `Everything_GetResultListSort` | — | `int` | Sort actually used (may differ from requested if not fast-sortable) |
| `Everything_GetResultListRequestFlags` | — | `int` | Flags actually honored for this result list |

### 6.5 Result type checks
| Function | Params | Returns |
|---|---|---|
| `Everything_IsFileResult` | `i` | `bool` |
| `Everything_IsFolderResult` | `i` | `bool` |
| `Everything_IsVolumeResult` | `i` | `bool` (e.g. `C:`) |

### 6.6 Run history (Everything's own usage-ranking data)
| Function | Params | Returns |
|---|---|---|
| `Everything_GetRunCountFromFileNameW` | `path: str` | `int` |
| `Everything_SetRunCountFromFileNameW` | `path: str, count: int` | `bool` |
| `Everything_IncRunCountFromFileNameW` | `path: str` | updated count |
| `Everything_SaveRunHistory` | — | `bool` |
| `Everything_DeleteRunHistory` | — | `bool` |

### 6.7 State, lifecycle & diagnostics
| Function | Params | Returns | Notes |
|---|---|---|---|
| `Everything_Reset` | — | — | Clears search text/filters/result list back to defaults. |
| `Everything_CleanUp` | — | — | Frees memory the DLL allocated for the current result set. Call after reading results, and before your program exits. |
| `Everything_Exit` | — | `bool` | Asks the running `Everything.exe` to close. |
| `Everything_IsDBLoaded` | — | `bool` | `False` while Everything is still building its index. |
| `Everything_IsAdmin` | — | `bool` | |
| `Everything_IsAppData` | — | `bool` | Whether the index lives under `%APPDATA%`. |
| `Everything_RebuildDB` | — | `bool` | |
| `Everything_SaveDB` | — | `bool` | |
| `Everything_UpdateAllFolderIndexes` | — | `bool` | |
| `Everything_IsFastSort` | `sort: int` | `bool` | Whether that §5.2 sort is served straight from the index. |
| `Everything_IsFileInfoIndexed` | `flag: int` | `bool` | Whether a category of info is actually available on this system (e.g. `DATE_ACCESSED` may be disabled for performance) — verify exact parameter on the linked page. |
| `Everything_GetLastError` | — | `int` | One of §5.3 — check right after a failed `Query()`. |
| `Everything_GetBuildNumber` | — | `int` | |
| `Everything_GetMajorVersion` | — | `int` | |
| `Everything_GetMinorVersion` | — | `int` | |
| `Everything_GetRevision` | — | `int` | |
| `Everything_GetTargetMachine` | — | `int` | §5.4 |
| `Everything_SortResultsByPath` | — | — | Re-sorts the already-fetched result list by path client-side, no new query. |

---

## 7. Search Syntax Quick Reference

What you pass into `SetSearch()`:

| Syntax | Meaning |
|---|---|
| `abc 123` | AND (space-separated) — confirmed by voidtools' own SDK example |
| `abc\|def` | OR |
| `!abc` | NOT / exclude |
| `"exact phrase"` | Literal phrase incl. spaces |
| `*` / `?` | Wildcard: any run of chars / exactly one char |
| `()` | Group terms |
| `file:` / `folder:` (or `dir:`) | Restrict to files / folders |
| `ext:py;txt` | Filter by extension(s) |
| `size:>10mb`, `size:100kb..500kb` | Size filter (`<`,`>`,`<=`,`>=`,`=`,`..` range) |
| `dm:today`, `dm:thisweek`, `dm:2024` | Date-modified filter (`dc:`=created, `da:`=accessed) |
| `path:C:\Users` | Restrict to under a path |
| `regex:` prefix (or `SetRegex(True)`) | Whole query as regex |

This is a practical subset — the full grammar (500+ modifiers) is documented across [Search Syntax](https://www.voidtools.com/support/everything/search_syntax), [Search Modifiers](https://www.voidtools.com/support/everything/search_modifiers) and [Search Functions](https://www.voidtools.com/support/everything/search_functions).

---

## 8. FILETIME → datetime

FILETIME = 100ns ticks since 1601-01-01. The result getters fill a `c_ulonglong` with this raw value:

```python
import datetime

WIN_TICKS_PER_SEC = 10_000_000
EPOCH_DIFF_SECONDS = 11644473600  # seconds between 1601-01-01 and 1970-01-01

def filetime_to_datetime(ticks: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ticks / WIN_TICKS_PER_SEC - EPOCH_DIFF_SECONDS)
```

(Uses local time zone, matching voidtools' own sample; swap in `datetime.utcfromtimestamp` if you want UTC.)

---

## 9. Practical Wrapper (copy-paste ready)

```python
import ctypes
import datetime
from ctypes import wintypes

dll = ctypes.WinDLL(r"C:\EverythingSDK\DLL\Everything64.dll")

EVERYTHING_REQUEST_FILE_NAME     = 0x00000001
EVERYTHING_REQUEST_PATH          = 0x00000002
EVERYTHING_REQUEST_SIZE          = 0x00000010
EVERYTHING_REQUEST_DATE_MODIFIED = 0x00000040

# Declare argtypes/restype yourself — ctypes defaults to c_int, which
# silently truncates 64-bit sizes/DWORDs and mishandles pointers if skipped.
dll.Everything_SetSearchW.argtypes = [ctypes.c_wchar_p]
dll.Everything_QueryW.argtypes = [wintypes.BOOL]
dll.Everything_QueryW.restype = wintypes.BOOL
dll.Everything_GetNumResults.restype = wintypes.DWORD
dll.Everything_GetResultFullPathNameW.argtypes = [wintypes.DWORD, ctypes.c_wchar_p, wintypes.DWORD]
dll.Everything_GetResultSize.argtypes = [wintypes.DWORD, ctypes.POINTER(ctypes.c_ulonglong)]
dll.Everything_GetResultDateModified.argtypes = [wintypes.DWORD, ctypes.POINTER(ctypes.c_ulonglong)]

WIN_TICKS_PER_SEC = 10_000_000
EPOCH_DIFF_SECONDS = 11644473600

def _filetime_to_dt(ticks: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ticks / WIN_TICKS_PER_SEC - EPOCH_DIFF_SECONDS)

def everything_search(query: str, max_results: int = 100) -> list[dict]:
    dll.Everything_SetSearchW(query)
    dll.Everything_SetRequestFlags(
        EVERYTHING_REQUEST_FILE_NAME | EVERYTHING_REQUEST_PATH |
        EVERYTHING_REQUEST_SIZE | EVERYTHING_REQUEST_DATE_MODIFIED
    )
    dll.Everything_SetMax(max_results)

    if not dll.Everything_QueryW(True):
        raise RuntimeError(f"Everything query failed, error code {dll.Everything_GetLastError()}")

    size = ctypes.c_ulonglong()
    modified = ctypes.c_ulonglong()
    path_buf = ctypes.create_unicode_buffer(4096)
    results = []

    for i in range(dll.Everything_GetNumResults()):
        dll.Everything_GetResultFullPathNameW(i, path_buf, 4096)
        dll.Everything_GetResultSize(i, ctypes.byref(size))
        dll.Everything_GetResultDateModified(i, ctypes.byref(modified))
        results.append({
            "path": path_buf.value,
            "size": size.value,
            "modified": _filetime_to_dt(modified.value),
        })

    dll.Everything_CleanUp()
    return results

if __name__ == "__main__":
    for r in everything_search("ext:py ~/projects"):
        print(r)
```

Add more fields by OR-ing in another `EVERYTHING_REQUEST_*` flag (§5.1) and calling its matching getter (§6.4) inside the loop.

---

## 10. Gotchas

- **DLL bitness must match your Python interpreter**, not Windows itself.
- **Everything.exe must already be running** — the script won't launch it; `Query()` fails with `EVERYTHING_ERROR_IPC` if it isn't.
- **Declare `argtypes`/`restype` explicitly.** ctypes defaults to `c_int` for everything, which silently mangles 64-bit sizes, DWORDs and pointers.
- **Prefer the `W` functions.** Python 3 `str` → `c_wchar_p` needs no manual encoding.
- **Buffer size for full-path getters:** `MAX_PATH` (260) can truncate long paths — use something generous like 4096.
- **Not documented as thread-safe** — serialize calls yourself if using multiple threads.
- **`Query()` returns `False` on failure** — always check `GetLastError()` (§5.3) rather than assuming success.
- **Filter toggles persist** (`SetMatchCase`, `SetRegex`, etc. stay set) until you change them — explicitly set every option each search if you need deterministic behavior in a long-running program.
- **Call `CleanUp()`** after you're done reading a result set to free the DLL's internal memory.

---

## 11. Further Reading — every official page

Overview: [SDK index](https://www.voidtools.com/support/everything/sdk) · [C](https://www.voidtools.com/support/everything/sdk/c) · [C#](https://www.voidtools.com/support/everything/sdk/csharp) · [Python](https://www.voidtools.com/support/everything/sdk/python) · [IPC protocol](https://www.voidtools.com/support/everything/sdk/ipc)

Search params: [SetSearch](https://www.voidtools.com/support/everything/sdk/everything_setsearch) · [GetSearch](https://www.voidtools.com/support/everything/sdk/everything_getsearch) · [SetMatchCase](https://www.voidtools.com/support/everything/sdk/everything_setmatchcase) · [GetMatchCase](https://www.voidtools.com/support/everything/sdk/everything_getmatchcase) · [SetMatchPath](https://www.voidtools.com/support/everything/sdk/everything_setmatchpath) · [GetMatchPath](https://www.voidtools.com/support/everything/sdk/everything_getmatchpath) · [SetMatchWholeWord](https://www.voidtools.com/support/everything/sdk/everything_setmatchwholeword) · [GetMatchWholeWord](https://www.voidtools.com/support/everything/sdk/everything_getmatchwholeword) · [SetRegex](https://www.voidtools.com/support/everything/sdk/everything_setregex) · [GetRegex](https://www.voidtools.com/support/everything/sdk/everything_getregex) · [SetMax](https://www.voidtools.com/support/everything/sdk/everything_setmax) · [GetMax](https://www.voidtools.com/support/everything/sdk/everything_getmax) · [SetOffset](https://www.voidtools.com/support/everything/sdk/everything_setoffset) · [GetOffset](https://www.voidtools.com/support/everything/sdk/everything_getoffset) · [SetSort](https://www.voidtools.com/support/everything/sdk/everything_setsort) · [GetSort](https://www.voidtools.com/support/everything/sdk/everything_getsort) · [SetRequestFlags](https://www.voidtools.com/support/everything/sdk/everything_setrequestflags) · [GetRequestFlags](https://www.voidtools.com/support/everything/sdk/everything_getrequestflags) · [SetReplyWindow](https://www.voidtools.com/support/everything/sdk/everything_setreplywindow) · [GetReplyWindow](https://www.voidtools.com/support/everything/sdk/everything_getreplywindow) · [SetReplyID](https://www.voidtools.com/support/everything/sdk/everything_setreplyid) · [GetReplyID](https://www.voidtools.com/support/everything/sdk/everything_getreplyid)

Query & counts: [Query](https://www.voidtools.com/support/everything/sdk/everything_query) · [IsQueryReply](https://www.voidtools.com/support/everything/sdk/everything_isqueryreply) · [GetNumResults](https://www.voidtools.com/support/everything/sdk/everything_getnumresults) · [GetNumFileResults](https://www.voidtools.com/support/everything/sdk/everything_getnumfileresults) · [GetNumFolderResults](https://www.voidtools.com/support/everything/sdk/everything_getnumfolderresults) · [GetTotResults](https://www.voidtools.com/support/everything/sdk/everything_gettotresults) · [GetTotFileResults](https://www.voidtools.com/support/everything/sdk/everything_gettotfileresults) · [GetTotFolderResults](https://www.voidtools.com/support/everything/sdk/everything_gettotfolderresults)

Per-result getters: [GetResultFileName](https://www.voidtools.com/support/everything/sdk/everything_getresultfilename) · [GetResultPath](https://www.voidtools.com/support/everything/sdk/everything_getresultpath) · [GetResultFullPathName](https://www.voidtools.com/support/everything/sdk/everything_getresultfullpathname) · [GetResultExtension](https://www.voidtools.com/support/everything/sdk/everything_getresultextension) · [GetResultSize](https://www.voidtools.com/support/everything/sdk/everything_getresultsize) · [GetResultDateCreated](https://www.voidtools.com/support/everything/sdk/everything_getresultdatecreated) · [GetResultDateModified](https://www.voidtools.com/support/everything/sdk/everything_getresultdatemodified) · [GetResultDateAccessed](https://www.voidtools.com/support/everything/sdk/everything_getresultdateaccessed) · [GetResultDateRun](https://www.voidtools.com/support/everything/sdk/everything_getresultdaterun) · [GetResultDateRecentlyChanged](https://www.voidtools.com/support/everything/sdk/everything_getresultdaterecentlychanged) · [GetResultAttributes](https://www.voidtools.com/support/everything/sdk/everything_getresultattributes) · [GetResultFileListFileName](https://www.voidtools.com/support/everything/sdk/everything_getresultfilelistfilename) · [GetResultRunCount](https://www.voidtools.com/support/everything/sdk/everything_getresultruncount) · [GetResultHighlightedFileName](https://www.voidtools.com/support/everything/sdk/everything_getresulthighlightedfilename) · [GetResultHighlightedPath](https://www.voidtools.com/support/everything/sdk/everything_getresulthighlightedpath) · [GetResultHighlightedFullPathAndFileName](https://www.voidtools.com/support/everything/sdk/everything_getresulthighlightedfullpathandfilename) · [GetResultListSort](https://www.voidtools.com/support/everything/sdk/everything_getresultlistsort) · [GetResultListRequestFlags](https://www.voidtools.com/support/everything/sdk/everything_getresultlistrequestflags)

Type checks: [IsFileResult](https://www.voidtools.com/support/everything/sdk/everything_isfileresult) · [IsFolderResult](https://www.voidtools.com/support/everything/sdk/everything_isfolderresult) · [IsVolumeResult](https://www.voidtools.com/support/everything/sdk/everything_isvolumeresult)

Run history: [GetRunCountFromFileName](https://www.voidtools.com/support/everything/sdk/everything_getruncountfromfilename) · [SetRunCountFromFileName](https://www.voidtools.com/support/everything/sdk/everything_setruncountfromfilename) · [IncRunCountFromFileName](https://www.voidtools.com/support/everything/sdk/everything_incruncountfromfilename) · [SaveRunHistory](https://www.voidtools.com/support/everything/sdk/everything_saverunhistory) · [DeleteRunHistory](https://www.voidtools.com/support/everything/sdk/everything_deleterunhistory)

State/diagnostics: [Reset](https://www.voidtools.com/support/everything/sdk/everything_reset) · [CleanUp](https://www.voidtools.com/support/everything/sdk/everything_cleanup) · [Exit](https://www.voidtools.com/support/everything/sdk/everything_exit) · [IsDBLoaded](https://www.voidtools.com/support/everything/sdk/everything_isdbloaded) · [IsAdmin](https://www.voidtools.com/support/everything/sdk/everything_isadmin) · [IsAppData](https://www.voidtools.com/support/everything/sdk/everything_isappdata) · [RebuildDB](https://www.voidtools.com/support/everything/sdk/everything_rebuilddb) · [SaveDB](https://www.voidtools.com/support/everything/sdk/everything_savedb) · [UpdateAllFolderIndexes](https://www.voidtools.com/support/everything/sdk/everything_updateallfolderindexes) · [IsFastSort](https://www.voidtools.com/support/everything/sdk/everything_isfastsort) · [IsFileInfoIndexed](https://www.voidtools.com/support/everything/sdk/everything_isfileinfoindexed) · [GetLastError](https://www.voidtools.com/support/everything/sdk/everything_getlasterror) · [GetBuildNumber](https://www.voidtools.com/support/everything/sdk/everything_getbuildnumber) · [GetMajorVersion](https://www.voidtools.com/support/everything/sdk/everything_getmajorversion) · [GetMinorVersion](https://www.voidtools.com/support/everything/sdk/everything_getminorversion) · [GetRevision](https://www.voidtools.com/support/everything/sdk/everything_getrevision) · [GetTargetMachine](https://www.voidtools.com/support/everything/sdk/everything_gettargetmachine) · [SortResultsByPath](https://www.voidtools.com/support/everything/sdk/everything_sortresultsbypath)

Syntax: [Search Syntax](https://www.voidtools.com/support/everything/search_syntax) · [Search Modifiers](https://www.voidtools.com/support/everything/search_modifiers) · [Search Functions](https://www.voidtools.com/support/everything/search_functions)
