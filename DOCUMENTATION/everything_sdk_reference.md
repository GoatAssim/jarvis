# Everything SDK — Reference

Source: https://www.voidtools.com/support/everything/sdk/

## What it is
A DLL/Lib wrapper around Everything's IPC interface, used to search voidtools' "Everything" file search index from your own code. Requires **Everything** to already be running in the background — the SDK just talks to it. ANSI + Unicode builds, x86 + x64, thread-safe, supports blocking and non-blocking queries.

Download: https://www.voidtools.com/Everything-SDK.zip (ships `Everything32.dll` / `Everything64.dll`, headers, and a `.lib`)

---

## Core workflow

1. Set search state (`Everything_SetSearch`, `SetMatchCase`, `SetMatchPath`, `SetMatchWholeWord`, `SetRegex`, `SetMax`, `SetOffset`, `SetSort`, `SetRequestFlags`).
2. Run `Everything_Query(bWait)`.
3. Read result count (`GetNumResults` / `GetTotResults` etc.).
4. Read each result's fields (`GetResultFileName`, `GetResultPath`, `GetResultSize`, ...).

```c
Everything_SetSearch("abc 123");     // search text = "abc" AND "123"
Everything_SetMatchCase(TRUE);       // case-sensitive
Everything_Query(TRUE);              // TRUE = block until results arrive
```

State set via the `Set*` functions is **not** cleared by `Everything_Query` — it persists until you change it or call `Everything_Reset`.

---

## Function reference (by category)

### Manipulating search state
| Function | Purpose |
|---|---|
| `Everything_SetSearch(text)` | search string |
| `Everything_SetMatchPath(bool)` | match full path, not just filename |
| `Everything_SetMatchCase(bool)` | case-sensitive |
| `Everything_SetMatchWholeWord(bool)` | whole-word match |
| `Everything_SetRegex(bool)` | treat search text as regex |
| `Everything_SetMax(n)` | max results to return |
| `Everything_SetOffset(n)` | skip first n results (paging) |
| `Everything_SetReplyWindow(hwnd)` | window to receive async reply (needed for non-blocking query) |
| `Everything_SetReplyID(id)` | user-defined id echoed back with async reply |
| `Everything_SetSort(type)` | result ordering, see Sort table below |
| `Everything_SetRequestFlags(flags)` | which result fields to fetch, see table below |

### Reading search state
Mirrors of the above: `GetSearch`, `GetMatchPath`, `GetMatchCase`, `GetMatchWholeWord`, `GetRegex`, `GetMax`, `GetOffset`, `GetReplyWindow`, `GetReplyID`, `GetSort`, `GetRequestFlags`, plus `Everything_GetLastError()`.

### Executing / checking query
- `Everything_Query(BOOL bWait)` — runs the query.
  - `bWait = TRUE`: blocks until results are ready, returns `TRUE`/`FALSE`.
  - `bWait = FALSE`: posts the query and returns immediately; you **must** call `SetReplyWindow` first, then poll with `Everything_IsQueryReply`.
- `Everything_IsQueryReply(msg, wParam, lParam, id)` — checks a received `WM_COPYDATA` message against your reply id; used in the non-blocking flow.

**Errors** (`Everything_GetLastError()`):
`EVERYTHING_ERROR_CREATETHREAD`, `EVERYTHING_ERROR_REGISTERCLASSEX`, `EVERYTHING_ERROR_CREATEWINDOW`, `EVERYTHING_ERROR_IPC` (Everything not running), `EVERYTHING_ERROR_MEMORY`, `EVERYTHING_ERROR_INVALIDCALL` (forgot `SetReplyWindow` before non-blocking query).

### Manipulating / reading results
- `Everything_SortResultsByPath()` — re-sort the already-fetched result set client-side.
- `Everything_Reset()` — clears search state back to defaults and frees the result list.
- Counts: `GetNumFileResults`, `GetNumFolderResults`, `GetNumResults` (results actually returned, capped by `SetMax`), and `GetTotFileResults`, `GetTotFolderResults`, `GetTotResults` (total matches available, ignoring the max cap).
- Type checks per index: `IsVolumeResult`, `IsFolderResult`, `IsFileResult`.
- Per-result getters (all take a 0-based result index):
  - `GetResultFileName`, `GetResultPath`, `GetResultFullPathName`
  - `GetResultExtension`, `GetResultSize`, `GetResultAttributes`
  - `GetResultDateCreated`, `GetResultDateModified`, `GetResultDateAccessed`, `GetResultDateRun`, `GetResultDateRecentlyChanged`
  - `GetResultFileListFileName`, `GetResultRunCount`
  - Highlighted variants (search-term highlighting markup): `GetResultHighlightedFileName`, `GetResultHighlightedPath`, `GetResultHighlightedFullPathAndFileName`
  - `GetResultListSort` / `GetResultListRequestFlags` — tells you what sort/data the server actually applied (it may fall back if unsupported)

### General / housekeeping
`Everything_CleanUp()`, `Everything_Exit()` (tells the Everything process to quit), `Everything_GetMajorVersion`, `GetMinorVersion`, `GetRevision`, `GetBuildNumber`, `GetTargetMachine`, `IsDBLoaded`, `IsAdmin`, `IsAppData`, `IsFastSort`, `IsFileInfoIndexed`, `RebuildDB`, `UpdateAllFolderIndexes`, `SaveDB`, `SaveRunHistory`, `DeleteRunHistory`.

### Run history
`GetRunCountFromFileName`, `SetRunCountFromFileName`, `IncRunCountFromFileName` — track/adjust how many times a file has been "run" (affects Everything's run-count sort).

---

## `Everything_SetRequestFlags` — result data flags
Controls which fields get fetched per result (bitwise OR). Must be called **before** `Everything_Query`.

```
EVERYTHING_REQUEST_FILE_NAME                            0x00000001
EVERYTHING_REQUEST_PATH                                 0x00000002
EVERYTHING_REQUEST_FULL_PATH_AND_FILE_NAME               0x00000004
EVERYTHING_REQUEST_EXTENSION                             0x00000008
EVERYTHING_REQUEST_SIZE                                  0x00000010
EVERYTHING_REQUEST_DATE_CREATED                          0x00000020
EVERYTHING_REQUEST_DATE_MODIFIED                         0x00000040
EVERYTHING_REQUEST_DATE_ACCESSED                         0x00000080
EVERYTHING_REQUEST_ATTRIBUTES                            0x00000100
EVERYTHING_REQUEST_FILE_LIST_FILE_NAME                   0x00000200
EVERYTHING_REQUEST_RUN_COUNT                             0x00000400
EVERYTHING_REQUEST_DATE_RUN                              0x00000800
EVERYTHING_REQUEST_DATE_RECENTLY_CHANGED                 0x00001000
EVERYTHING_REQUEST_HIGHLIGHTED_FILE_NAME                 0x00002000
EVERYTHING_REQUEST_HIGHLIGHTED_PATH                      0x00004000
EVERYTHING_REQUEST_HIGHLIGHTED_FULL_PATH_AND_FILE_NAME    0x00008000
```
- Default = `FILE_NAME | PATH` (0x3) → uses the old v1 query protocol.
- Any other flag combo → tries the newer v2 query, falls back to v1 if unsupported.
- Requested data may not be honored — check `Everything_GetResultListRequestFlags()` after the query to see what was actually returned.
- Requires Everything ≥ 1.4.1.

## `Everything_SetSort` — sort types
```
EVERYTHING_SORT_NAME_ASCENDING                    1   EVERYTHING_SORT_NAME_DESCENDING                   2
EVERYTHING_SORT_PATH_ASCENDING                    3   EVERYTHING_SORT_PATH_DESCENDING                   4
EVERYTHING_SORT_SIZE_ASCENDING                    5   EVERYTHING_SORT_SIZE_DESCENDING                   6
EVERYTHING_SORT_EXTENSION_ASCENDING               7   EVERYTHING_SORT_EXTENSION_DESCENDING              8
EVERYTHING_SORT_TYPE_NAME_ASCENDING               9   EVERYTHING_SORT_TYPE_NAME_DESCENDING             10
EVERYTHING_SORT_DATE_CREATED_ASCENDING           11   EVERYTHING_SORT_DATE_CREATED_DESCENDING          12
EVERYTHING_SORT_DATE_MODIFIED_ASCENDING          13   EVERYTHING_SORT_DATE_MODIFIED_DESCENDING         14
EVERYTHING_SORT_ATTRIBUTES_ASCENDING             15   EVERYTHING_SORT_ATTRIBUTES_DESCENDING            16
EVERYTHING_SORT_FILE_LIST_FILENAME_ASCENDING     17   EVERYTHING_SORT_FILE_LIST_FILENAME_DESCENDING    18
EVERYTHING_SORT_RUN_COUNT_ASCENDING              19   EVERYTHING_SORT_RUN_COUNT_DESCENDING             20
EVERYTHING_SORT_DATE_RECENTLY_CHANGED_ASCENDING  21   EVERYTHING_SORT_DATE_RECENTLY_CHANGED_DESCENDING 22
EVERYTHING_SORT_DATE_ACCESSED_ASCENDING          23   EVERYTHING_SORT_DATE_ACCESSED_DESCENDING         24
EVERYTHING_SORT_DATE_RUN_ASCENDING               25   EVERYTHING_SORT_DATE_RUN_DESCENDING              26
```
- Default = `NAME_ASCENDING` (free, no index needed).
- "Fast sorts" are instant but must be enabled per-type in Everything: **Tools → Options → Indexes → check "fast sort"** for the type you want.
- If the requested sort isn't supported, check `Everything_GetResultListSort()` to see what was actually used.
- Must be set before `Everything_Query`. Requires Everything ≥ 1.4.1.

---

## Blocking vs non-blocking queries
- **Blocking** (`bWait = TRUE`): simplest, just call and results are ready when it returns.
- **Non-blocking** (`bWait = FALSE`): call `Everything_SetReplyWindow(hwnd)` first, then `Everything_Query(FALSE)` returns immediately; your window proc receives a `WM_COPYDATA` message, pass it to `Everything_IsQueryReply` to confirm it's the reply you're waiting for (matched against `SetReplyID`), then read results normally.

---

## Python example (ctypes)
Requires Everything running + the SDK DLL (`Everything32.dll`/`Everything64.dll` matching your Python's bitness) available locally.

```python
import ctypes, datetime, struct

EVERYTHING_REQUEST_FILE_NAME = 0x00000001
EVERYTHING_REQUEST_PATH      = 0x00000002
EVERYTHING_REQUEST_SIZE      = 0x00000010
EVERYTHING_REQUEST_DATE_MODIFIED = 0x00000040

dll = ctypes.WinDLL("C:\\EverythingSDK\\DLL\\Everything32.dll")
dll.Everything_GetResultDateModified.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_ulonglong)]
dll.Everything_GetResultSize.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_ulonglong)]
dll.Everything_GetResultFileNameW.argtypes = [ctypes.c_int]
dll.Everything_GetResultFileNameW.restype = ctypes.c_wchar_p

dll.Everything_SetSearchW("test.py")
dll.Everything_SetRequestFlags(
    EVERYTHING_REQUEST_FILE_NAME | EVERYTHING_REQUEST_PATH |
    EVERYTHING_REQUEST_SIZE | EVERYTHING_REQUEST_DATE_MODIFIED
)
dll.Everything_QueryW(1)  # 1 = blocking

num_results = dll.Everything_GetNumResults()
print(f"Result Count: {num_results}")

filename = ctypes.create_unicode_buffer(260)
date_modified = ctypes.c_ulonglong(1)
size = ctypes.c_ulonglong(1)

for i in range(num_results):
    dll.Everything_GetResultFullPathNameW(i, filename, 260)
    dll.Everything_GetResultDateModified(i, date_modified)
    dll.Everything_GetResultSize(i, size)
    print(ctypes.wstring_at(filename), date_modified.value, size.value)
```

Notes:
- Function names get an `A` (ANSI) or `W` (Unicode/wide) suffix, e.g. `Everything_SetSearchA` / `Everything_SetSearchW`, `Everything_QueryA` / `Everything_QueryW`. Pick one and stay consistent — `GetResultFileName` must match the A/W flavor used for `Query`.
- Dates come back as raw Windows `FILETIME` (100ns ticks since 1601-01-01) — convert with the epoch-diff math shown (or `datetime` conversion helpers) to get a normal `datetime`.
- Use `ctypes.create_unicode_buffer(260)` (or `create_string_buffer` for ANSI) sized to `MAX_PATH`, or call `GetResultFullPathName` with a larger buffer for long paths.

---

## Other official examples
Full source for these lives under the SDK zip / respective doc pages, not reproduced here:
- **C / C++** — https://www.voidtools.com/support/everything/sdk/c
- **C#** — https://www.voidtools.com/support/everything/sdk/csharp (community CLI variant: https://github.com/dipique/everythingio)
- **Clarion** — https://www.voidtools.com/support/everything/sdk/clarion
- **Visual Basic** — https://www.voidtools.com/support/everything/sdk/visual_basic
- **Raw IPC** (no SDK wrapper, talk to Everything's window directly) — https://www.voidtools.com/support/everything/sdk/ipc and https://www.voidtools.com/support/everything/sdk/ipc_c_example

---

## Practical notes
- Everything (the app/service) must be running — the SDK is only a client to it, not a standalone indexer.
- Search syntax inside `SetSearch` is the same query language as the main app (operators, modifiers, wildcards) — see https://www.voidtools.com/support/everything/search_syntax
- Thread-safe: multiple threads can share the DLL, but each has its own logical "search state" only insofar as you manage it — the SDK's state (search text, flags, etc.) is effectively global/per-process, so concurrent queries from multiple threads at once will race unless you serialize them.
- x86 vs x64: use the DLL matching your process bitness (`Everything32.dll` / `Everything64.dll`).
