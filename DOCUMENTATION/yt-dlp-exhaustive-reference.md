# yt-dlp — Exhaustive Reference & Python Integration Manual

**Scope of this document.** This is a from-scratch, maximally detailed reference to `yt-dlp` built specifically for wiring it into a Python-based AI agent. Your setup: precompiled `yt-dlp` binary already on `PATH`, `ffmpeg`/`ffprobe` already available, agent written in Python, and the tool should fire on a "yt" trigger. Every CLI flag is documented, every flag is mapped to its Python `ydl_opts` dictionary key where one exists, and the back half of the document is a working code cookbook plus a ready-to-adapt tool class and function-calling schema.

Reference version context: PyPI's current release at time of writing is `2026.8.19`. yt-dlp ships roughly monthly (`stable`), nightly (`nightly`), and per-commit (`master`) channels — flags, defaults, and especially `--extractor-args` sub-options drift between releases, so treat exact defaults below as "true as of recent releases" and re-verify anything load-bearing against `yt-dlp --help` on your actual pinned binary before hardcoding behavior your agent depends on.

---

## Table of Contents

0. Orientation — what yt-dlp is and how it's built internally
1. Installation & Dependencies (full dependency tree, what breaks without each)
2. Two Integration Strategies — subprocess vs. native Python import
3. Anatomy of the Info Dict — the metadata object everything else is built from
4. Complete CLI Flag Reference — every flag, every category
5. Python `ydl_opts` Deep-Dive — hooks, callables, and objects the CLI can't express
6. Output Templates — full syntax and full field list
7. Format Selection — selectors, filters, sorting, merging
8. Modifying Metadata (`--parse-metadata` / `--replace-in-metadata`)
9. Extractor Arguments — mechanism + commonly-seen keys
10. Plugins — directory layout and packaging convention
11. Configuration Files — locations, precedence, netrc, env vars
12. Exit Codes & Exceptions
13. Code Cookbook — 16 complete, original, runnable recipes
14. A Production-Ready Wrapper Class + Agent Tool Schema
15. Gotchas, Footguns, and Operational Notes
16. Condensed Cheat Sheet (appendix)

---

## PART 0 — Orientation

`yt-dlp` is a command-line media downloader that started as a community fork of `youtube-dl` (itself forked again from the short-lived `youtube-dlc`). It ships under the Unlicense (public-domain-equivalent) for the git repo, the PyPI sdist, and the PyPI wheel. The prebuilt standalone binaries are a different story: the PyInstaller-bundled executables pull in GPLv3+ code, so *those specific binary artifacts* are effectively GPLv3+ as a combined work, and the Unix zipimport binary plus the source tarball bundle small ISC/MIT-licensed pieces (`meriyah`, `astring`). Since you're already running a precompiled binary, this mostly matters only if you plan to redistribute that exact binary yourself.

Internally, a single yt-dlp invocation walks through five stages, and understanding this pipeline makes almost every configuration option below self-explanatory:

1. **Extraction** — a site-specific "extractor" class (there are on the order of 1,800+ of them, one per supported site or family of sites) parses the URL/page and produces an **info dict**: a nested Python dictionary describing the video/playlist and every format available for it. This stage is what `--dump-json`, `--print`, `-F`, and `extract_info(download=False)` expose.
2. **Filtering** — video-selection flags (`--match-filters`, `--playlist-items`, `--date`, `--min-filesize`, etc.) decide whether a given entry proceeds.
3. **Format selection** — the `-f`/`format` selector expression picks which of the extracted formats to actually fetch, informed by `-S`/format-sort ordering.
4. **Downloading** — the native downloader (or an external one via `--downloader`) fetches the chosen format(s) to disk (or a temp path), handling fragments, retries, throttling detection, and resuming.
5. **Post-processing** — a chain of postprocessor objects runs in a defined order (merging separate video/audio, remuxing/re-encoding, embedding subtitles/thumbnails/metadata/chapters, running SponsorBlock cuts, running your `--exec` command, etc.), each one potentially renaming or replacing the file on disk.

Every configuration surface in this document is really just "how do I influence one of these five stages," which is worth keeping in mind when something doesn't behave as expected — the question is usually "which stage is misbehaving."

---

## PART 1 — Installation & Dependencies

You said you already have a precompiled binary and ffmpeg, so the `pip install` step below is *only* needed if you also want `import yt_dlp` inside your Python process (strongly recommended for an agent — see Part 2).

```bash
pip install -U yt-dlp
# with extras for browser impersonation:
pip install -U "yt-dlp[default,curl-cffi]"
```

Python support: **3.10+** on CPython, **3.11+** on PyPy. Older interpreters are not supported and may fail in subtle ways rather than refusing to import.

### Required-in-practice
- **ffmpeg + ffprobe** — you have these. Without them, yt-dlp can still download single-file formats, but it cannot merge a separate video-only + audio-only stream into one file, cannot extract audio, cannot remux/re-encode, cannot embed subtitles/thumbnails/chapters/metadata, and cannot cut `--download-sections`/SponsorBlock segments. Critically: **if ffmpeg is missing, yt-dlp silently changes its default format selector** from `bestvideo*+bestaudio/best` down to `best/bestvideo+bestaudio` (i.e., it prefers a single progressive file over a merge it can't perform). Since you have ffmpeg, you get the full pipeline — just be aware this fallback exists if your agent is ever deployed somewhere ffmpeg isn't guaranteed.
- **yt-dlp-ejs + a JS runtime** (deno recommended, or node.js/bun/QuickJS) — increasingly required for full YouTube support specifically (signature descrambling / PO-token handling that YouTube has pushed extractors toward). "Only deno enabled by default" — if you don't have any JS runtime available, some YouTube formats or entire extractions can fail or come back throttled/limited. Worth confirming deno or node is installed alongside your binary if YouTube is a primary target.

### Networking extras (all optional, improve robustness/coverage)
- `certifi` — bundles Mozilla's CA root store.
- `brotli` or `brotlicffi` — lets yt-dlp decode Brotli-compressed HTTP responses.
- `websockets` — needed for sites that serve media or signaling over a websocket.
- `requests` — used for HTTPS proxy support and persistent/keep-alive connections.

### Impersonation
- `curl_cffi` (the recommended package for this) — lets yt-dlp mimic real browser TLS/HTTP fingerprints (Chrome, Edge, Safari targets) via `--impersonate`. Some sites fingerprint TLS handshakes to detect and block non-browser clients; without this, `--impersonate` silently has nothing to work with. Install via the `curl-cffi` extra. Note it's bundled in most prebuilt binary variants already *except* the Unix zipimport `yt-dlp` binary and the 32-bit Windows `yt-dlp_x86` binary — if your precompiled binary is one of those two, `--impersonate` may not function until you separately provide `curl_cffi`.

### Metadata / embedding extras
- `mutagen` — enables `--embed-thumbnail` for many audio container formats.
- `AtomicParsley` (external binary, not pip) — fallback for embedding thumbnails into `mp4`/`m4a` specifically, for cases mutagen/ffmpeg can't handle.
- `xattr` / `pyxattr` / `setfattr` — lets `--xattrs` write extended filesystem attributes (Dublin Core / XDG metadata) on macOS/BSD.

### Misc extras
- `pycryptodomex` — required to decrypt AES-128-encrypted HLS streams and some other encrypted payloads. Without it, encrypted HLS sources will fail outright.
- `phantomjs` — legacy JS execution for a handful of older extractors; no longer used for YouTube; being phased out.
- `secretstorage` — lets `--cookies-from-browser` unlock the GNOME keyring to decrypt Chromium-family cookies on Linux.
- Any external downloader binary you want to hand off to via `--downloader` (`aria2c`, `axel`, `curl`, `wget`, `httpie`).

### Deprecated
- `rtmpdump` — old RTMP stream support; ffmpeg via `--downloader ffmpeg` replaces it entirely now.

**Practical takeaway for your setup:** because you already have ffmpeg, you're missing almost nothing important. The two things worth double-checking are (a) a JS runtime for full YouTube reliability, and (b) `curl_cffi` if you expect to hit sites that block based on TLS fingerprinting and your binary variant doesn't already bundle it.

---

## PART 2 — Two Integration Strategies

### Strategy A: subprocess against your precompiled binary

```python
import subprocess
import json

def run_yt_dlp(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """Thin wrapper around the yt-dlp binary already on PATH."""
    return subprocess.run(
        ["yt-dlp", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )

# Metadata only, structured as JSON, no network side effects beyond extraction
proc = run_yt_dlp(["-J", "--no-warnings", "--skip-download", url])
if proc.returncode != 0:
    raise RuntimeError(proc.stderr.strip())
info = json.loads(proc.stdout)
```

Strengths: uses your exact pinned binary and version; keeps the yt-dlp dependency entirely out of your Python process's import graph and virtualenv; trivially sandboxable (separate process, separate resource limits, easy to `kill()` on timeout, easy to run under a restricted user/container).

Weaknesses: every call pays process-spawn overhead; you can't register Python callables as hooks (`progress_hooks`, `match_filter`, custom postprocessors, custom logger, custom format selector — none of that exists across a process boundary); real-time progress requires either polling `--newline` + `--progress-template` output line-by-line from `proc.stdout` as it streams, or accepting that you only see the final JSON after the process exits; cancellation means killing the OS process (`terminate()`/`kill()`), which is fine but coarser than a Python-level abort flag.

**Streaming progress from a subprocess**, if you go this route:

```python
import subprocess

def download_with_progress(url: str, out_dir: str, on_progress):
    cmd = [
        "yt-dlp", "--newline", "--no-color",
        "--progress-template", "download:%(progress._percent_str)s|%(progress._speed_str)s|%(progress.eta)s",
        "-P", out_dir, url,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        if line.startswith("download:"):
            pct, speed, eta = line[len("download:"):].strip().split("|")
            on_progress(pct.strip(), speed.strip(), eta.strip())
    proc.wait()
    return proc.returncode
```

### Strategy B: native `import yt_dlp` (recommended for an agent)

```python
import yt_dlp

ydl_opts = {
    "format": "bestvideo*+bestaudio/best",
    "outtmpl": "%(title)s [%(id)s].%(ext)s",
}

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    error_code = ydl.download(["https://example.com/watch?v=XXXX"])
```

`ydl.download(urls)` returns `0` on full success and a nonzero int if anything failed (behavior further modulated by `ignoreerrors`). This is generally the better fit for an agent because:

- You get a real Python dict back from `extract_info(download=False)` instead of text to parse.
- `progress_hooks`, `postprocessor_hooks`, `match_filter`, `logger`, and custom format-selector functions are first-class Python objects — no IPC, no serialization.
- You can register a custom `PostProcessor` subclass to intercept the pipeline at any stage.
- Exceptions are real Python exception classes (`yt_dlp.utils.DownloadError` and friends) you can catch precisely, instead of parsing stderr text.

The tradeoffs: yt-dlp becomes an import in your process (version now tracks whatever you `pip install`, separately from your standalone binary, unless you're careful to keep them in sync); a raw `YoutubeDL()` call is synchronous/blocking, so for a responsive agent you'll typically run it in a thread pool or subprocess-via-`multiprocessing` anyway if you don't want to block your event loop (see the async wrapper in the cookbook, Part 13, recipe 10); and there is no built-in cross-thread cancellation — cancelling a native call means either killing the thread's owning process or engineering your own cooperative cancellation (e.g., have a progress hook raise a custom exception to unwind the download when a cancel flag is set).

**Recommendation for your case:** keep the precompiled binary as a fallback/CLI escape hatch (e.g., for `--update`, one-off debugging, or environments where you can't `pip install`), but drive the agent's actual tool calls through `import yt_dlp` for the reasons above. `pip install yt-dlp` pulls in the same project as your binary — same extractors, same options — just wired for in-process use.

---

## PART 3 — Anatomy of the Info Dict

Everything downstream (output templates, `--print`, format selection, your agent's own logic) revolves around one nested dictionary. Exact fields vary per extractor/site — not every site exposes every field — but the shape below is stable enough to build against.

**Top-level, for a single video/track:**
```
id                  str   — site-specific unique identifier
title               str
description         str
uploader / uploader_id / uploader_url
channel / channel_id / channel_url
duration            float (seconds)
view_count / like_count / comment_count   int or None
upload_date         str  "YYYYMMDD"
timestamp           int  (unix epoch)
webpage_url         str  — canonical URL, re-feeding this to yt-dlp should reproduce the same result
extractor / extractor_key   str — which extractor handled it
thumbnail           str  — URL of the single "best" thumbnail
thumbnails          list[dict] — {"url","width","height","id",...} for every available thumbnail
formats             list[dict] — see below; every downloadable stream
subtitles           dict[lang -> list[dict]] — manually authored subtitle tracks
automatic_captions  dict[lang -> list[dict]] — ASR/auto-generated captions, same shape as subtitles
chapters            list[dict] — {"start_time","end_time","title"}
categories          list[str]
tags                list[str]
is_live / was_live / live_status   bool / bool / str
age_limit           int
availability        str  — "public"/"unlisted"/"private"/"needs_auth"/"subscriber_only"/"premium_only"
```

**Each entry in `formats`:**
```
format_id     str  — extractor-specific code, what -F lists and -f matches against
ext           str  — "mp4","webm","m4a",...
vcodec/acodec str  — "none" means that stream carries no video/audio at all
width/height  int or None
fps           float or None
tbr/vbr/abr   float — total/video/audio bitrate in kbps
filesize / filesize_approx   int (bytes) or None
protocol      str  — "https","m3u8_native","dash",...
url           str  — the actual media URL (often signed/expiring)
format_note   str  — human-readable label, e.g. "1080p60"
```

**For a playlist / channel / search result**, the top-level dict instead carries `_type: "playlist"` (or `"multi_video"`), plus `entries`, which is either a list or a lazy generator of per-video info dicts (shallow ones, unless `--no-flat-playlist`/`extract_flat=False` forces a full per-video extraction). Iterate it like:

```python
info = ydl.extract_info(playlist_url, download=False)
for entry in info.get("entries", []):
    if entry is None:
        continue  # a playlist item that failed to resolve
    print(entry["id"], entry.get("title"))
```

**After an actual download** (`ydl.extract_info(url, download=True)` or `ydl.download([...])`), the dict gains a `requested_downloads` key: a list of dicts (one per file actually written, more than one if you merged/extracted-audio-and-kept-video/etc.) each containing `filepath` (or the older `_filename`) — **this is the single most reliable way to learn the true final on-disk path**, because output templates only predict a filename before post-processing, and merges/remuxes/re-encodes can change the extension or name.

```python
info = ydl.extract_info(url, download=True)
info = ydl.sanitize_info(info)  # strips non-JSON-safe objects, safe to log/store/serialize
final_paths = [d["filepath"] for d in info.get("requested_downloads", []) if "filepath" in d]
```

`ydl.sanitize_info(info)` is worth calling any time you intend to serialize, log, or hand the info dict to another part of your agent (e.g., put it in a tool-result payload) — the raw dict can contain non-JSON-serializable objects in edge cases.

---

## PART 4 — Complete CLI Flag Reference

Every flag from `yt-dlp --help`, grouped exactly as the tool itself groups them, each with its Python `ydl_opts` key noted where a direct one exists. Where I'm not fully confident an internal key name is stable across versions, I've flagged it — for anything load-bearing, cross-check against `python -c "import yt_dlp, pydoc; pydoc.pager(yt_dlp.YoutubeDL.__doc__)"`, which prints the authoritative, version-matched parameter docstring straight out of your installed package.

### General Options

- **`-h, --help`** — print help and exit. No `ydl_opts` equivalent (n/a for library use).
- **`--version`** — print the installed version string and exit.
- **`-U, --update`** / **`--no-update`** — self-update the binary to the latest release on its current channel; `--no-update` (default) skips the update check entirely. Irrelevant to library use; your Python package version is controlled by `pip`.
- **`--update-to [CHANNEL]@[TAG]`** — switch channels (`stable`/`nightly`/`master`, or any `owner/repo`) and/or jump to a specific tagged release. E.g. `yt-dlp --update-to nightly`, `yt-dlp --update-to stable@2023.07.06`.
- **`-i, --ignore-errors`** — **python:** `ignoreerrors` (bool, or the string `"only_download"` to ignore only download-stage errors but still fail hard on extraction errors). Keeps a batch/playlist job moving past a single bad entry instead of aborting the whole run; the run is still reported as "successful" even if some post-processing failed.
- **`--no-abort-on-error`** (default) / **`--abort-on-error`** — the inverse framing of the same knob: whether one failed item stops the rest of the queue. `--abort-on-error` is aliased to `--no-ignore-errors`.
- **`--list-extractors`** — dump every extractor's internal name and exit. Handy for your agent to sanity-check "is this domain even supported" without attempting a real extraction.
- **`--extractor-descriptions`** — like the above but with a one-line description per extractor.
- **`--use-extractors NAMES`** (alias `--ies`) — restrict or exclude which extractors are even tried, supports regex, `all`, `default`, and `end` (meaning "try this at the very end, as a last resort" — useful for pinning the generic extractor last). Prefix a name with `-` to exclude it.
- **`--default-search PREFIX`** — **python:** `default_search`. What to do when you're handed a bare unqualified string instead of a URL — e.g. set this to `"ytsearch"` so passing a plain search phrase behaves like a YouTube search. `auto` guesses, `auto_warning` guesses but logs a warning, `error` refuses, and the default `fixup_error` tries to repair obviously-broken URLs and only errors if it can't.
- **`--ignore-config`** (alias `--no-config`) — skip all config files except ones explicitly passed via `--config-locations`.
- **`--no-config-locations`** (default) / **`--config-locations PATH`** — whether to read any custom config file paths at all, and where from if so.
- **`--plugin-dirs DIR`** / **`--no-plugin-dirs`** — add (repeatable) or fully clear the directories yt-dlp scans for plugins.
- **`--js-runtimes RUNTIME[:PATH]`** / **`--no-js-runtimes`** — enable a specific JavaScript runtime (deno/node/quickjs/bun, in that priority order, deno on by default) with an optional explicit binary path, for yt-dlp-ejs. If you need a non-default runtime to win, pass `--no-js-runtimes` first, then enable the one you want.
- **`--remote-components COMPONENT`** / **`--no-remote-components`** — whether yt-dlp is allowed to fetch remote JS components on demand (`ejs:npm`, `ejs:github`); disabled entirely by default. Not needed if you already have `yt-dlp-ejs` installed locally or you're using an official prebuilt binary.
- **`--flat-playlist`** / **`--no-flat-playlist`** (default) — **python:** `extract_flat` (`True`/`False`/`"in_playlist"`). Flat mode returns shallow entries for a playlist (URL + minimal metadata) without recursing into each video — dramatically faster for "just list what's in this playlist" but you won't get full per-video metadata or even confirm downloadability until you extract each entry individually afterward.
- **`--live-from-start`** / **`--no-live-from-start`** (default) — attempt to download a live stream from its actual start rather than from "now." Experimental; currently limited to a handful of sites (YouTube, Twitch, TVer, mellow-fan among them).
- **`--wait-for-video MIN[-MAX]`** / **`--no-wait-for-video`** (default) — for a stream scheduled but not yet live, poll and wait rather than failing immediately.
- **`--mark-watched`** / **`--no-mark-watched`** (default) — mark the video watched on sites that support that concept, even during a `--simulate` run.
- **`--color [STREAM:]POLICY`** — **python:** `color`. `always`/`auto`/`never`/`no_color`, optionally scoped to `stdout`/`stderr`.
- **`--compat-options OPTS`** — **python:** `compat_opts` (list of strings). Reverts specific yt-dlp behavior changes back to legacy `youtube-dl`/`youtube-dlc` compatible behavior — useful only if you're migrating an old script that depended on old defaults.
- **`--alias ALIASES OPTIONS`** — define your own shorthand flag that expands to a longer option string, with `{0}`/`{1}`/... argument substitution. Capped at 100 expansions deep as a loop guard.
- **`-t, --preset-alias PRESET`** — apply one of the five built-in bundles (`mp3`, `aac`, `mp4`, `mkv`, `sleep`) — full expansions in Part on Preset Aliases below.

### Network Options

- **`--proxy URL`** — **python:** `proxy`. Standard `http://`/`https://` or `socks5://user:pass@host:port` proxy URL. Pass an empty string to force a direct connection even if a proxy is configured elsewhere (env vars, config file).
- **`--socket-timeout SECONDS`** — **python:** `socket_timeout` (float).
- **`--source-address IP`** — **python:** `source_address`. Bind outbound connections to a specific local IP — useful on multi-homed hosts.
- **`--impersonate CLIENT[:OS]`** — **python:** `impersonate`. Mimic a real browser's TLS/HTTP fingerprint (e.g. `chrome`, `chrome-110`, `chrome:windows-10`) via `curl_cffi`; pass an empty string to impersonate *some* client without caring which. Can slow things down and shouldn't be applied blanket to every request unless a site specifically needs it.
- **`--list-impersonate-targets`** — enumerate what impersonation targets are actually available given your installed `curl_cffi` version.
- **`-4, --force-ipv4`** / **`-6, --force-ipv6`** — pin all connections to one IP family.
- **`--enable-file-urls`** — **python:** `enable_file_urls` (bool). Off by default for security reasons — turning it on lets yt-dlp treat `file://` URLs as fetchable input, which you almost certainly do **not** want exposed to anything an end user can influence in an agent context, since it would let a crafted input read local files.

### Geo-restriction

- **`--geo-verification-proxy URL`** — **python:** `geo_verification_proxy`. A separate proxy used only to *check* what a geo-restricted site sees as your location; actual media download still goes through `--proxy` (or direct, if none set).
- **`--xff VALUE`** — **python:** `geo_bypass` / `geo_bypass_country` / `geo_bypass_ip_block` (the CLI flag maps onto these three depending on the value shape). Fakes an `X-Forwarded-For` header to try to slip past geographic blocks: `default` only does this where it's known to help, `never` disables it, or supply a CIDR block or a two-letter ISO country code directly.

### Video Selection

- **`-I, --playlist-items ITEM_SPEC`** — **python:** `playlist_items` (str). Comma-separated 1-based indices and/or `[START]:[STOP][:STEP]` ranges; negative indices count from the end, negative step reverses order. `-I "1:3,7,-5::2"` on a 15-item playlist selects positions 1,2,3,7,11,13,15.
- **`--min-filesize SIZE`** / **`--max-filesize SIZE`** — **python:** `min_filesize` / `max_filesize`. Accepts human sizes like `"50k"`, `"44.6M"`.
- **`--date DATE`** / **`--datebefore DATE`** / **`--dateafter DATE`** — **python:** `date` / `datebefore` / `dateafter`. Absolute `YYYYMMDD` or relative expressions like `today-2weeks`, `now`, `yesterday`.
- **`--match-filters FILTER`** / **`--no-match-filters`** (default) — **python:** `match_filter` (a callable, or a string filter expression, or `yt_dlp.utils.match_filter_func("...")` to convert a string to a callable). Reject entries by comparing any output-template-style field against a value; use `!field` to require a field's *absence*, `&` to AND multiple conditions, escape literal `&`/quotes with `\`. Repeating the flag ORs the conditions together (matches if *any* one passes). Passing `-` makes it interactively prompt per video instead.
- **`--break-match-filters FILTER`** / **`--no-break-match-filters`** (default) — same filter syntax, but a rejection here halts the entire remaining queue instead of just skipping that one item.
- **`--no-playlist`** / **`--yes-playlist`** — **python:** `noplaylist` (bool). Disambiguates a URL that points to both a specific video *and* a containing playlist — download just the one video, or the whole playlist.
- **`--age-limit YEARS`** — **python:** `age_limit` (int). Skip content flagged above this age rating.
- **`--download-archive FILE`** / **`--no-download-archive`** (default) — **python:** `download_archive` (path string). Maintains a flat text file of already-downloaded IDs; anything already listed is skipped on future runs — the standard mechanism for idempotent re-runs / cron-driven mirroring.
- **`--max-downloads NUMBER`** — **python:** `max_downloads` (int). Stops after this many successful downloads in the run (raises `MaxDownloadsReached` internally).
- **`--break-on-existing`** / **`--no-break-on-existing`** (default) — stop the whole queue (rather than just skipping) the moment an archive-matched item is hit — useful for "stop as soon as we catch up to what we already have," e.g. syncing a channel newest-first.
- **`--break-per-input`** / **`--no-break-per-input`** (default) — whether `--max-downloads`, `--break-on-existing`, `--break-match-filters`, and autonumbering reset per *input URL* rather than applying globally across every URL you passed in one invocation.
- **`--skip-playlist-after-errors N`** — abandon the rest of a playlist after this many consecutive failures within it.

### Download Options

- **`-N, --concurrent-fragments N`** — **python:** `concurrent_fragment_downloads` (int, default 1). Parallel fragment fetches for DASH/HLS-native downloads — raising this can meaningfully speed up fragmented formats.
- **`-r, --limit-rate RATE`** — **python:** `ratelimit` (bytes/sec, e.g. `"50K"`/`"4.2M"` on CLI, raw int in Python).
- **`--throttled-rate RATE`** — **python:** `throttledratelimit`. Below this speed, yt-dlp assumes the server is throttling it and re-extracts the format (sometimes gets a fresh, faster URL).
- **`-R, --retries RETRIES`** / **`--file-access-retries RETRIES`** / **`--fragment-retries RETRIES`** / **`--extractor-retries RETRIES`** — **python:** `retries` / `file_access_retries` / `fragment_retries` / `extractor_retries`. All accept an int or the literal string `"infinite"`.
- **`--retry-sleep [TYPE:]EXPR`** — **python:** `retry_sleep_functions` (dict keyed by `http`/`fragment`/`file_access`/`extractor`, values are backoff functions or the raw spec string). `EXPR` is a flat number of seconds, `linear=START[:END[:STEP]]`, or `exp=START[:END[:BASE]]`.
- **`--skip-unavailable-fragments`** (default, alias `--no-abort-on-unavailable-fragments`) / **`--abort-on-unavailable-fragments`** (alias `--no-skip-unavailable-fragments`) — whether one missing DASH/HLS/ISM fragment kills the whole download or is tolerated.
- **`--keep-fragments`** / **`--no-keep-fragments`** (default) — **python:** `keep_fragments`. Leave individual fragment files on disk after assembly instead of cleaning them up (useful for debugging a bad merge).
- **`--buffer-size SIZE`** (default `1024`) / **`--resize-buffer`** (default) / **`--no-resize-buffer`** — **python:** `buffersize` / `resizebuffer`. Download buffer sizing; resizing auto-tunes upward from the initial value.
- **`--http-chunk-size SIZE`** — **python:** `http_chunk_size`. Splits plain HTTP downloads into fixed-size chunks — experimental, mainly useful to dodge per-request bandwidth caps some servers impose.
- **`--playlist-random`** — **python:** `playlist_random` (bool). Shuffle playlist download order.
- **`--lazy-playlist`** / **`--no-lazy-playlist`** (default) — **python:** `lazy_playlist`. Start processing playlist entries as they stream in from the site rather than waiting for the entire playlist listing to resolve first; incompatible with `n_entries`, random order, and reverse order since none of those are knowable until the full list is in.
- **`--hls-use-mpegts`** (default for live) / **`--no-hls-use-mpegts`** (default for VOD) — **python:** `hls_use_mpegts`. MPEG-TS container tolerates interruption/corruption better and allows some players to play a partially-downloaded HLS file while it's still being written.
- **`--download-sections REGEX`** — **python:** `download_ranges` (a callable returning time ranges, in the library — the CLI string form is convenience sugar around this). Restrict to only chapters matching a regex, or a `*START-STOP` time range (negative values count from the end; `inf` means "to the end"), or `*from-url` to use `start_time`/`end_time` already embedded in the URL. Repeatable to combine several sections. Requires ffmpeg — which you have.
- **`--downloader [PROTO:]NAME`** (alias `--external-downloader`) — **python:** `external_downloader` (str, or dict keyed by protocol). Hand off to `aria2c`/`axel`/`curl`/`ffmpeg`/`httpie`/`wget` instead of yt-dlp's built-in downloader, optionally scoped per protocol (`http`, `ftp`, `m3u8`, `dash`, `rtmp`).
- **`--downloader-args NAME:ARGS`** (alias `--external-downloader-args`) — **python:** `external_downloader_args`. Raw passthrough arguments to whichever external downloader you selected.

### Filesystem Options

- **`-a, --batch-file FILE`** / **`--no-batch-file`** (default) — read one URL per line from a file (`-` for stdin); lines starting `#`, `;`, or `]` are treated as comments.
- **`-P, --paths [TYPES:]PATH`** — **python:** `paths` (dict, e.g. `{"home": "...", "temp": "...", "subtitle": "..."}`). Same TYPES vocabulary as `-o`, plus the special `home` (final destination, default) and `temp` (staging location — files land here first, then move to `home` once complete). Ignored if your output template is already an absolute path.
- **`-o, --output [TYPES:]TEMPLATE`** — **python:** `outtmpl` (str, or dict for per-type templates). Full syntax in Part 6.
- **`--output-na-placeholder TEXT`** (default `"NA"`) — **python:** `outtmpl_na_placeholder`. What to substitute when a template field isn't available for a given item.
- **`--restrict-filenames`** / **`--no-restrict-filenames`** (default) — **python:** `restrictfilenames`. ASCII-only output filenames, no `&`, no spaces — useful when downstream systems (older filesystems, 8-bit-unsafe pipes, Windows transfer) can't handle Unicode/special characters.
- **`--windows-filenames`** / **`--no-windows-filenames`** (default) — **python:** `windowsfilenames`. Sanitize specifically for Windows-illegal characters even when running on a different OS (handy if your agent writes files that later get synced to a Windows machine).
- **`--trim-filenames LENGTH`** — **python:** `trim_file_name`. Caps filename length (excluding extension) — protects against filesystem path-length limits on very long titles.
- **`-w, --no-overwrites`** / **`--force-overwrites`** / **`--no-force-overwrites`** (default) — **python:** `overwrites` (bool/None). Whether re-running against an existing output path clobbers it; `--force-overwrites` also implies `--no-continue`.
- **`-c, --continue`** (default) / **`--no-continue`** — **python:** `continuedl`. Resume a partially-downloaded file/fragment set versus restarting from scratch.
- **`--part`** (default) / **`--no-part`** — **python:** `nopart` (inverted sense). Whether in-progress downloads are staged as `.part` files or written straight into the final filename.
- **`--mtime`** / **`--no-mtime`** (default) — **python:** `updatetime`. Copy the server's `Last-Modified` header onto the local file's mtime.
- **`--write-description`** / **`--no-write-description`** (default) — **python:** `writedescription`.
- **`--write-info-json`** / **`--no-write-info-json`** (default) — **python:** `writeinfojson`. **Caution:** this file can contain personal information (e.g. things pulled from cookies/authenticated responses) — don't blindly forward it to end users of your agent without a look.
- **`--write-playlist-metafiles`** (default) / **`--no-write-playlist-metafiles`** — whether playlist-level metadata files get written alongside the per-video ones when the write-* flags above are active.
- **`--clean-info-json`** (default) / **`--no-clean-info-json`** — **python:** `clean_infojson`. Strips internal-only fields (like local filenames) from the written `.info.json`; the un-clean version keeps everything.
- **`--write-comments`** / **`--no-write-comments`** (alias `--get-comments`/`--no-get-comments`) — **python:** `getcomments`. Comments are actually fetched by default whenever the extractor considers that cheap, independent of this flag; this flag controls whether they get *persisted* into the infojson.
- **`--load-info-json FILE`** — **python:** `load_info_filename`. Skip live extraction entirely and reconstruct from a previously-written `.info.json` — useful for replaying/reprocessing without hitting the network again.
- **`--cookies FILE`** / **`--no-cookies`** (default) — **python:** `cookiefile`. Netscape-format cookie jar, read from and also written back to (so it doubles as persistent session storage across runs).
- **`--cookies-from-browser BROWSER[+KEYRING][:PROFILE][::CONTAINER]`** / **`--no-cookies-from-browser`** (default) — **python:** `cookiesfrombrowser` (tuple: `(browser, profile, keyring, container)`). Pulls live cookies straight out of an installed browser's storage — `brave`, `chrome`, `chromium`, `edge`, `firefox`, `opera`, `safari`, `vivaldi`, `whale`. On Linux, Chromium-family cookie decryption may need a keyring backend: `basictext`, `gnomekeyring`, `kwallet`, `kwallet5`, `kwallet6`.
- **`--cache-dir DIR`** (default `${XDG_CACHE_HOME}/yt-dlp`) / **`--no-cache-dir`** / **`--rm-cache-dir`** — **python:** `cachedir`. Persists things like extracted client IDs/signature-decoding artifacts between runs so they don't need re-deriving every time.

### Thumbnail Options

- **`--write-thumbnail`** / **`--no-write-thumbnail`** (default) — **python:** `writethumbnail`.
- **`--write-all-thumbnails`** — **python:** `write_all_thumbnails`. Every available thumbnail size/variant, not just the best one.
- **`--list-thumbnails`** — enumerate available thumbnails; implies simulate unless `--no-simulate` is also given.

### Internet Shortcut Options

- **`--write-link`** — platform-appropriate shortcut file (`.url` on Windows, `.webloc` on macOS, `.desktop` on Linux). Note the OS itself may cache the target URL at the file-path level.
- **`--write-url-link`** / **`--write-webloc-link`** / **`--write-desktop-link`** — force one specific shortcut format regardless of host platform.

### Verbosity and Simulation Options

- **`-q, --quiet`** / **`--no-quiet`** (default) — **python:** `quiet`. Combined with `--verbose`, quiet mode redirects the (still-produced) debug log to stderr instead of suppressing it.
- **`--no-warnings`** — **python:** `no_warnings`.
- **`-s, --simulate`** / **`--no-simulate`** — **python:** `simulate`. Resolve everything but write nothing to disk — this is exactly `extract_info(download=False)` in library terms.
- **`--ignore-no-formats-error`** / **`--no-ignore-no-formats-error`** (default) — **python:** `ignore_no_formats_error`. Lets metadata extraction succeed even when zero downloadable formats exist (experimental) — useful for "just tell me about this video" tooling that shouldn't hard-fail on unavailable content.
- **`--skip-download`** (alias `--no-download`) — **python:** `skip_download`. Still writes subtitles/thumbnails/description/infojson per your other flags, just not the media itself.
- **`-O, --print [WHEN:]TEMPLATE`** — **python:** `forceprint` (dict keyed by stage name → list of templates). Print an arbitrary output-template expression at a chosen pipeline stage (stage names match `--use-postprocessor`'s WHEN values, default `video`). Implies `--quiet`, and implies `--simulate` unless you also pass `--no-simulate` or pick a stage late enough that a real download already had to happen.
- **`--print-to-file [WHEN:]TEMPLATE FILE`** — **python:** `print_to_file`. Same idea, appended to a file instead of stdout.
- **`-j, --dump-json`** / **`-J, --dump-single-json`** — **python:** `dumpjson` / `dump_single_json`. `-j` prints one JSON object per video (one line each); `-J` prints a single JSON object for the whole invocation, so a playlist URL yields one object with an `entries` array rather than N separate lines. In the Python API you don't need either — `extract_info(download=False)` already gives you the live dict directly.
- **`--force-write-archive`** (alias `--force-download-archive`) — **python:** `force_write_download_archive`. Write archive entries even during a simulate/no-op run.
- **`--newline`** — **python:** `progress_with_newline`. Emit each progress update as its own line instead of repainting a single line — the right choice when you're piping subprocess output into something line-oriented (like the streaming-progress recipe in Part 2).
- **`--no-progress`** / **`--progress`** — **python:** `noprogress`. Force the progress bar off, or force it on even under `--quiet`.
- **`--console-title`** — mirror progress into the terminal's title bar (irrelevant when run headless/in an agent).
- **`--progress-template [TYPES:]TEMPLATE`** — **python:** `progress_template` (dict). Custom format string for progress output, with `download:`, `download-title:`, `postprocess:`, `postprocess-title:` prefixes selecting which stream it applies to; video fields are under an `info` namespace and live progress numbers under a `progress` namespace inside the template (e.g. `%(progress.eta)s`).
- **`--progress-delta SECONDS`** (default `0`) — **python:** `progress_delta`. Minimum time between progress emissions — raise this to cut down on event-stream noise if your agent is forwarding progress somewhere.
- **`-v, --verbose`** — **python:** `verbose`. Full debug output — this is your first move when an extraction mysteriously fails and the plain error message isn't enough.
- **`--dump-pages`** — base64-dump every fetched page into the log, extremely verbose, debugging only.
- **`--write-pages`** — same idea but written to files in the current directory instead of the log.
- **`--print-traffic`** — **python:** `debug_printtraffic`. Dump raw HTTP request/response traffic.

### Workarounds

- **`--encoding ENCODING`** — **python:** `encoding`. Force a specific text encoding (experimental) for cases where locale auto-detection guesses wrong.
- **`--legacy-server-connect`** — **python:** `legacy_server_connect`. Allow HTTPS to a server that doesn't support RFC 5746 secure renegotiation.
- **`--no-check-certificates`** — **python:** `nocheckcertificate`. Disables TLS certificate validation — only ever use this against a host you trust for other reasons, since it removes MITM protection entirely.
- **`--prefer-insecure`** — **python:** `prefer_insecure`. Use plain HTTP for *metadata retrieval* on sites where that's an option (does not typically affect the media download itself).
- **`--add-headers FIELD:VALUE`** — **python:** `http_headers` (dict). Repeatable; sets custom request headers globally.
- **`--bidi-workaround`** — **python:** `bidi_workaround`. Works around terminals lacking bidirectional text rendering; needs `bidiv` or `fribidi` present. Irrelevant for headless/agent use.
- **`--sleep-requests SECONDS`** — **python:** `sleep_interval_requests`. Pause between metadata-extraction requests — a basic politeness/anti-throttle measure.
- **`--sleep-interval SECONDS`** (alias `--min-sleep-interval`) / **`--max-sleep-interval SECONDS`** — **python:** `sleep_interval` / `max_sleep_interval`. Randomized pause before each download, min/max bounds.
- **`--sleep-subtitles SECONDS`** — **python:** `sleep_interval_subtitles`.

### Video Format Options

- **`-f, --format FORMAT`** — **python:** `format` (str, or a callable — see Part 7 and the custom-selector recipe).
- **`-S, --format-sort SORTORDER`** — **python:** `format_sort` (list of field strings). Full field vocabulary in Part 7.
- **`--format-sort-reset`** — **python:** `format_sort_force`-adjacent; discards any built-in default ordering and starts your sort spec from a blank slate.
- **`--format-sort-force`** (alias `--S-force`) / **`--no-format-sort-force`** (default) — **python:** `format_sort_force`. Whether your custom `-S` order is allowed to override fields yt-dlp normally treats as taking precedence no matter what.
- **`--video-multistreams`** / **`--no-video-multistreams`** (default) — **python:** `videomultistreams`. Allow a `+`-merge to keep more than one video stream in the resulting file.
- **`--audio-multistreams`** / **`--no-audio-multistreams`** (default) — **python:** `audiomultistreams`. Same, for audio streams.
- **`--prefer-free-formats`** / **`--no-prefer-free-formats`** (default) — **python:** `prefer_free_formats`. Break ties toward unencumbered containers (e.g. webm over a same-quality proprietary alternative); combine with `-S ext` to prefer them irrespective of quality rather than only as a tiebreaker.
- **`--check-formats`** / **`--check-all-formats`** / **`--no-check-formats`** — **python:** `check_formats`. Probe candidate formats to confirm they're actually fetchable before committing to them (costs extra requests, saves you from picking a format that 404s).
- **`-F, --list-formats`** — **python:** `listformats`. Enumerate every format yt-dlp sees for a URL, with the codes you'd feed to `-f`.
- **`--merge-output-format FORMAT`** — **python:** `merge_output_format`. Container to use when a `+`-merge actually has to happen (`avi`/`flv`/`mkv`/`mov`/`mp4`/`webm`); ignored entirely if no merge is required.

### Subtitle Options

- **`--write-subs`** / **`--no-write-subs`** (default) — **python:** `writesubtitles`.
- **`--write-auto-subs`** (alias `--write-automatic-subs`) / **`--no-write-auto-subs`** (default) — **python:** `writeautomaticsub`. Auto-generated (ASR) captions specifically, separate from human-authored ones.
- **`--list-subs`** — enumerate available subtitle languages/tracks; implies simulate unless overridden.
- **`--sub-format FORMAT`** — **python:** `subtitlesformat`. Preference list separated by `/`, e.g. `"ass/srt/best"`.
- **`--sub-langs LANGS`** — **python:** `subtitleslangs` (list). Comma-separated language codes, regex-capable (`"en.*,ja"`), `all` for everything, prefix a code with `-` to exclude it (e.g. `all,-live_chat`).

### Authentication Options

- **`-u, --username USERNAME`** / **`-p, --password PASSWORD`** — **python:** `username` / `password`. Omitting the password on CLI triggers an interactive prompt; in library use you must supply it explicitly (no interactive fallback).
- **`-2, --twofactor TWOFACTOR`** — **python:** `twofactor`.
- **`-n, --netrc`** / **`--netrc-location PATH`** (default `~/.netrc`) / **`--netrc-cmd NETRC_CMD`** — **python:** `usenetrc` / `netrc_location` / `netrc_cmd`. `netrc_cmd` runs an arbitrary shell command that must print netrc-formatted credentials and exit `0`; `{}` in the command is substituted with the extractor name, so you can dispatch to a secrets manager per-site instead of a plaintext file.
- **`--video-password PASSWORD`** — **python:** `videopassword`. Per-video password some sites require independent of account login.
- **`--ap-mso MSO`** / **`--ap-username USERNAME`** / **`--ap-password PASSWORD`** / **`--ap-list-mso`** — Adobe Pass / TV-provider authentication for sites gated behind cable/satellite login.
- **`--client-certificate CERTFILE`** / **`--client-certificate-key KEYFILE`** / **`--client-certificate-password PASSWORD`** — **python:** `client_certificate` / `client_certificate_key` / `client_certificate_password`. Mutual-TLS client cert auth.

### Post-Processing Options

- **`-x, --extract-audio`** — **python:** shorthand for adding an `FFmpegExtractAudio` entry to `postprocessors`; requires ffmpeg+ffprobe (which you have).
- **`--audio-format FORMAT`** (default `best`) — options: `aac`, `alac`, `flac`, `m4a`, `mp3`, `opus`, `vorbis`, `wav`; supports the same multi-rule chaining syntax as `--remux-video`.
- **`--audio-quality QUALITY`** (default `5`) — `0` (best) through `10` (worst) for VBR, or a literal bitrate like `128K`.
- **`--remux-video FORMAT`** — change container without re-encoding streams (fails if the target container can't hold the existing codec); supports chained rules like `"aac>m4a/mov>mp4/mkv"` (per-source-format target, with a final catch-all).
- **`--recode-video FORMAT`** — same target list, but actually re-encodes (slower, always succeeds codec-wise, at a quality/CPU cost).
- **`--postprocessor-args NAME:ARGS`** (alias `--ppa`) — **python:** `postprocessor_args` (dict). Raw argument passthrough to a specific postprocessor and/or the underlying executable it shells out to (`AtomicParsley`, `FFmpeg`, `FFprobe`); `_i`/`_o` suffixes (optionally numbered) position the args before a specific input/output file in the ffmpeg command line, e.g. `"Merger+ffmpeg_i1:-v quiet"`.
- **`-k, --keep-video`** / **`--no-keep-video`** (default) — **python:** `keepvideo`. Keep the pre-post-processing intermediate file around too (e.g. keep the original video after audio-extraction, instead of deleting it).
- **`--post-overwrites`** (default) / **`--no-post-overwrites`** — whether re-running post-processing overwrites already-post-processed output.
- **`--embed-subs`** / **`--no-embed-subs`** (default) — **python:** `embedsubtitles`. Only functions for mp4/webm/mkv containers.
- **`--embed-thumbnail`** / **`--no-embed-thumbnail`** (default) — **python:** `embedthumbnail`.
- **`--embed-metadata`** (alias `--add-metadata`) / **`--no-embed-metadata`** (alias `--no-add-metadata`) — **python:** `addmetadata`. Also pulls in chapters/infojson by default unless separately disabled.
- **`--embed-chapters`** (alias `--add-chapters`) / **`--no-embed-chapters`** — **python:** `addchapters`.
- **`--embed-info-json`** / **`--no-embed-info-json`** — **python:** `embed_infojson`. Attaches the infojson as a file inside mkv/mka containers specifically.
- **`--parse-metadata [WHEN:]FROM:TO`** / **`--replace-in-metadata [WHEN:]FIELDS REGEX REPLACE`** — full syntax and worked examples in Part 8.
- **`--xattrs`** — **python:** `xattrs`. Write Dublin Core / XDG metadata into filesystem extended attributes.
- **`--concat-playlist POLICY`** (default `multi_video`) — **python:** `concat_playlist`. `never`/`always`/`multi_video` — whether same-show multi-part playlist videos get concatenated into one file; every part must share codecs/stream count to qualify.
- **`--fixup POLICY`** (default `detect_or_warn`) — **python:** `fixup`. `never`/`warn`/`detect_or_warn`/`force` — whether yt-dlp auto-repairs known-broken output files (stretched video, malformed m4a, bad m3u8 timestamps/duration).
- **`--ffmpeg-location PATH`** — **python:** `ffmpeg_location`. Point explicitly at your ffmpeg install (binary or its containing directory) if it isn't already resolvable via `PATH` — since you said ffmpeg is available, you may not need this at all, but it's the right knob if you ever run in an environment with a different or sandboxed `PATH`.
- **`--exec [WHEN:]CMD`** / **`--no-exec`** — **python:** `exec_cmd`. Run an arbitrary shell command at a chosen pipeline stage, with output-template substitution into the command string. For safety, only `i`/`d` (integer), `f` (float), and `q` (shell-quoted) conversions are permitted in substituted fields — this is a deliberate guardrail against argument-injection from untrusted metadata (a video title containing shell metacharacters can't break out of the command). If you don't reference any field yourself, yt-dlp appends the shell-quoted final filepath automatically.
- **`--convert-subs FORMAT`** (alias `--convert-subtitles`) — `ass`/`lrc`/`srt`/`vtt`, or `none` to disable.
- **`--convert-thumbnails FORMAT`** — `jpg`/`png`/`webp`, same multi-rule chaining as `--remux-video`, or `none`.
- **`--split-chapters`** / **`--no-split-chapters`** (default) — **python:** `split_chapters`. One output file per embedded chapter.
- **`--remove-chapters REGEX`** / **`--no-remove-chapters`** (default) — **python:** `remove_chapters`. Cut out chapters whose title matches, same range syntax as `--download-sections`.
- **`--force-keyframes-at-cuts`** / **`--no-force-keyframes-at-cuts`** (default) — **python:** `force_keyframes_at_cuts`. Forces a re-encode at cut points during section-download/splitting/chapter-removal so the cut doesn't land mid-GOP and produce artifacts — costs time, improves cut cleanliness.
- **`--use-postprocessor NAME[:ARGS]`** — **python:** `use_postprocessors`. Enable a plugin postprocessor by name with `;`-delimited `KEY=VALUE` args, and pick which pipeline stage it runs at (`pre_process`, `after_filter`, `video`, `before_dl`, `post_process` [default], `after_move`, `after_video`, `playlist`).

### SponsorBlock Options

Built against the community-maintained [SponsorBlock](https://sponsor.ajay.app) database of YouTube segment timestamps.

- **`--sponsorblock-mark CATS`** — **python:** `sponsorblock_mark`. Turn matched segments into chapter markers rather than removing them. Categories: `sponsor`, `intro`, `outro`, `selfpromo`, `preview`, `filler`, `interaction`, `music_offtopic`, `hook`, `poi_highlight`, `chapter`, `all`, `default` (= `all`). Prefix a category with `-` to exclude it from an otherwise-broad selection.
- **`--sponsorblock-remove CATS`** — **python:** `sponsorblock_remove`. Actually cut the matched segments out of the file. If a category appears in both mark and remove, removal wins. Same category vocabulary except `default` here means `all,-filler`, and `poi_highlight`/`chapter` aren't valid remove targets.
- **`--sponsorblock-chapter-title TEMPLATE`** (default `"[SponsorBlock]: %(category_names)l"`) — **python:** `sponsorblock_chapter_title`. Template for the chapter names created by `--sponsorblock-mark`; only `start_time`, `end_time`, `category`, `categories`, `name`, `category_names` are available fields here.
- **`--no-sponsorblock`** — disables both mark and remove in one shot.
- **`--sponsorblock-api URL`** (default `https://sponsor.ajay.app`) — **python:** `sponsorblock_api`. Point at a self-hosted/alternate instance if you run your own.

### Extractor Options

- **`--extractor-retries RETRIES`** (default `3`) — separate retry budget specifically for known-flaky extractor-level errors (as opposed to `--retries`, which governs the download stage).
- **`--allow-dynamic-mpd`** (default, alias `--no-ignore-dynamic-mpd`) / **`--ignore-dynamic-mpd`** (alias `--no-allow-dynamic-mpd`) — whether to process DASH manifests marked dynamic (typically live content).
- **`--hls-split-discontinuity`** / **`--no-hls-split-discontinuity`** (default) — split an HLS playlist into separate formats at discontinuities (e.g. ad breaks) instead of treating it as one continuous stream.
- **`--extractor-args IE_KEY:ARGS`** — **python:** `extractor_args` (nested dict). Full treatment in Part 9.

### Preset Aliases (`-t`/`--preset-alias`)

Anthropic-of-yt-dlp's own bundled shortcuts — the project commits to keeping these five names stable even as their exact expansions may be tuned over time:

| Preset | Expands to |
|---|---|
| `mp3` | `-f 'ba[acodec^=mp3]/ba/b' -x --audio-format mp3` |
| `aac` | `-f 'ba[acodec^=aac]/ba[acodec^=mp4a.40.]/ba/b' -x --audio-format aac` |
| `mp4` | `--merge-output-format mp4 --remux-video mp4 -S vcodec:h264,lang,quality,res,fps,hdr:12,acodec:aac` |
| `mkv` | `--merge-output-format mkv --remux-video mkv` |
| `sleep` | `--sleep-subtitles 5 --sleep-requests 0.75 --sleep-interval 10 --max-sleep-interval 20` |

---

## PART 5 — Python `ydl_opts` Deep-Dive: Hooks & Callables

These have no CLI equivalent at all — they only exist because you're in a real Python process.

### `progress_hooks` (list of callables)

Each hook receives one dict per event, called repeatedly during download:

```python
def on_progress(d: dict):
    status = d["status"]  # "downloading" | "finished" | "error"
    if status == "downloading":
        # d also has: downloaded_bytes, total_bytes (or total_bytes_estimate),
        # speed (bytes/sec, float or None), eta (seconds, int or None),
        # _percent_str, _speed_str, _eta_str (pre-formatted human strings), filename
        pass
    elif status == "finished":
        # download stage is done; post-processing (if any) happens next
        # d["filename"] is the pre-post-processing path
        pass
    elif status == "error":
        pass

ydl_opts = {"progress_hooks": [on_progress]}
```

### `postprocessor_hooks` (list of callables)

Same shape, fired around each post-processing stage instead of the raw download:

```python
def on_pp(d: dict):
    # d["status"] in {"started", "processing", "finished", "error"}
    # d["postprocessor"] names which PP is running (e.g. "MoveFiles", "FFmpegMerger")
    pass

ydl_opts = {"postprocessor_hooks": [on_pp]}
```

### `logger` (object with four methods)

```python
class AgentLogger:
    def debug(self, msg):
        # yt-dlp funnels both true debug lines and normal info lines through here;
        # true debug lines are prefixed "[debug] " — filter on that if you only want info-level noise
        if not msg.startswith("[debug] "):
            self.info(msg)
    def info(self, msg):
        ...
    def warning(self, msg):
        ...
    def error(self, msg):
        ...

ydl_opts = {"logger": AgentLogger(), "quiet": True, "no_warnings": True}
```

### `match_filter` (callable)

```python
def filter_fn(info: dict, *, incomplete: bool):
    # return None to accept, or a string reason to reject
    if info.get("duration") and info["duration"] < 60:
        return "too short"
    return None

ydl_opts = {"match_filter": filter_fn}
```

You can also convert a `--match-filters`-style string expression into a callable without hand-writing one: `yt_dlp.utils.match_filter_func("duration > 60")`.

### `format` as a callable — custom format selection logic

```python
def format_selector(ctx: dict):
    """ctx['formats'] is sorted worst-to-best; yield one or more dicts describing
    what to actually fetch. Each yielded dict needs at minimum format_id, ext,
    and (if merging) requested_formats + a '+'-joined protocol string."""
    formats = ctx["formats"][::-1]
    best_video = next(f for f in formats if f["vcodec"] != "none" and f["acodec"] == "none")
    compatible_ext = {"mp4": "m4a", "webm": "webm"}.get(best_video["ext"])
    best_audio = next(
        f for f in formats
        if f["acodec"] != "none" and f["vcodec"] == "none" and f["ext"] == compatible_ext
    )
    yield {
        "format_id": f'{best_video["format_id"]}+{best_audio["format_id"]}',
        "ext": best_video["ext"],
        "requested_formats": [best_video, best_audio],
        "protocol": f'{best_video["protocol"]}+{best_audio["protocol"]}',
    }

ydl_opts = {"format": format_selector}
```

### Custom `PostProcessor` subclasses

```python
from yt_dlp.postprocessor.common import PostProcessor

class TitleLoggerPP(PostProcessor):
    def run(self, info):
        self.to_screen(f"post-processing finished for: {info.get('title')}")
        return [], info  # (files_to_delete, possibly-modified info dict)

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ydl.add_post_processor(TitleLoggerPP())
    ydl.download([url])
```

`run()` returns a 2-tuple: a list of filepaths yt-dlp should delete after this stage, and the (optionally mutated) info dict to hand to the next stage.

---

## PART 6 — Output Templates, in Full

Governed by `-o`/`outtmpl`. Base syntax is Python printf-style: `%(field_name)conversion`, e.g. `%(title)s`, `%(view_count)05d`.

**Field-name modifiers** (the part inside the parentheses supports far more than a bare field name):

1. **Dict/list traversal** with `.` — `%(tags.0)s` (first tag), `%(subtitles.en.-1.ext)s` (last English subtitle track's extension), Python-style slicing with `:` — `%(id.3:7)s`, `%(id.6:2:-1)s`, `%(formats.:.format_id)s` (every format's id). Curly braces build a sub-dict of just the named keys: `%(formats.:.{format_id,height})#j`. An empty field name means "the whole info dict": `%(.{id,title})s`.
2. **Arithmetic** on numeric fields with `+`, `-`, `*` — `%(playlist_index+10)03d`.
3. **strftime formatting**, separated by `>` — `%(upload_date>%Y-%m-%d)s`, `%(epoch-3600>%H-%M-%S)s`.
4. **Alternatives**, separated by `,` — tries each in order, first non-empty wins: `%(release_date>%Y,upload_date>%Y|Unknown)s`.
5. **Replacement**, separated by `&` — if the field is non-empty, substitute this instead of the field's actual value (evaluated after alternatives, so it fires if *any* alternative was non-empty): `%(chapters&has chapters|no chapters)s`, `%(title&TITLE={:>20}|NO TITLE)s`.
6. **Default**, separated by `|` — literal fallback for an empty field, overriding the global `--output-na-placeholder`: `%(uploader|Unknown)s`.
7. **Extra conversion types** beyond the standard printf set (`diouxXeEfFgGcrs`): `B` = bytes, `j` = JSON (`#` flag pretty-prints, `+` flag forces Unicode), `h` = HTML-escaped, `l` = comma-joined list (`#` flag newline-joins instead), `q` = shell-quoted (`#` flag splits a list into separate shell-quoted arguments), `D` = human decimal suffix like `10M` (`#` flag uses 1024 instead of 1000 as the base), `S` = sanitized as a filename (`#` flag applies the *restricted* sanitization rule set).
8. **Unicode normalization** with type `U` — NFC by default; `#` flag switches to NFD; `+` flag switches to the compatibility forms NFKC/NFKD. `%(title)+.100U` gives NFKC-normalized, truncated to 100 chars.

Full field grammar: `%(name[.keys][addition][>strf][,alternate][&replacement][|default])[flags][width][.precision][length]type`

**Per-file-type templates** — prefix a template with a type name and colon to route just that file type differently: `subtitle`, `thumbnail`, `description`, `annotation` (deprecated), `infojson`, `link`, `pl_thumbnail`, `pl_description`, `pl_infojson`, `chapter`, `pl_video`. E.g. `-o "%(title)s.%(ext)s" -o "thumbnail:%(title)s/%(title)s.%(ext)s"` drops thumbnails into a same-named subfolder. An empty template for a type suppresses writing that type entirely.

Default template: `%(title)s [%(id)s].%(ext)s`. Always keep `%(id)s` somewhere in your path — titles collide across uploads, IDs structurally cannot.

### Full field reference

**Core:** `id`, `title`, `fulltitle` (title with live-timestamp/generic-title noise stripped), `ext`, `alt_title`, `description`, `display_id`, `uploader`, `uploader_id`, `uploader_url`, `license`, `creators` (list) / `creator` (comma-joined), `timestamp`, `upload_date` (YYYYMMDD), `release_timestamp`, `release_date`, `release_year`, `modified_timestamp`, `modified_date`.

**Channel:** `channel`, `channel_id`, `channel_url`, `channel_follower_count`, `channel_is_verified` (bool).

**Location/duration:** `location`, `duration`, `duration_string` (HH:mm:ss).

**Engagement:** `view_count`, `concurrent_view_count` (live viewers right now), `like_count`, `dislike_count`, `repost_count`, `average_rating`, `comment_count` (unavailable for extractors that only fetch comments as a final slow step), `save_count`.

**Availability/status:** `age_limit`, `live_status` (`not_live`/`is_live`/`is_upcoming`/`was_live`/`post_live`), `is_live`, `was_live`, `playable_in_embed`, `availability` (`private`/`premium_only`/`subscriber_only`/`needs_auth`/`unlisted`/`public`), `media_type` (site-classified: `episode`/`clip`/`trailer`/etc.), `start_time`, `end_time` (URL-specified playback range).

**Extraction metadata:** `extractor`, `extractor_key`, `epoch` (unix time extraction finished), `autonumber` (global download counter, zero-padded to 5 digits, honors `--autonumber-start`), `video_autonumber`.

**Playlist context:** `n_entries`, `playlist_id`, `playlist_title`, `playlist` (`playlist_title` or falls back to `playlist_id`), `playlist_count`, `playlist_index`, `playlist_autonumber`, `playlist_uploader`, `playlist_uploader_id`, `playlist_channel`, `playlist_channel_id`, `playlist_webpage_url`.

**URLs:** `webpage_url`, `webpage_url_basename`, `webpage_url_domain`, `original_url` (what the user actually passed in, vs. `webpage_url`'s canonical form).

**Taxonomy:** `categories` (list), `tags` (list), `cast` (list).

**Chapter context** (when the item belongs to one): `chapter`, `chapter_number`, `chapter_id`.

**Series context**: `series`, `series_id`, `season`, `season_number`, `season_id`, `episode`, `episode_number`, `episode_id`.

**Music-track context**: `track`, `track_number`, `track_id`, `artists` (list) / `artist` (comma-joined), `genres` (list) / `genre` (comma-joined), `composers` (list) / `composer` (comma-joined), `album`, `album_type`, `album_artists` (list) / `album_artist` (comma-joined), `disc_number`.

**Only with `--download-sections` / inside `chapter:`-prefixed templates with `--split-chapters`**: `section_title`, `section_number`, `section_start`, `section_end`.

**Only usable inside `--print`** (not real output-filename fields): `urls` (every requested format's URL, one per line), `filename`, `formats_table`, `thumbnails_table`, `subtitles_table`, `automatic_captions_table` (these four are the same tables `-F`/`--list-thumbnails`/`--list-subs` render).

**Only available post-download** (`post_process`/`after_move` stages): `filepath` — the actual final path, which is the authoritative answer to "what did this end up being called," since merges/remuxes can diverge from the pre-download prediction.

**Only inside `--sponsorblock-chapter-title`**: `start_time`, `end_time`, `categories`, `category`, `category_names`, `name`, `type`.

**Filtering-format numeric/string fields** (documented fully in Part 7) are additionally usable here too.

### Worked examples

```bash
# Playlist videos into per-playlist subfolders, indexed by position
yt-dlp -o "%(playlist)s/%(playlist_index)s - %(title)s.%(ext)s" PLAYLIST_URL

# Bucket by upload year
yt-dlp -o "%(upload_date>%Y)s/%(title)s.%(ext)s" PLAYLIST_URL

# Conditionally prefix the playlist index only when one exists
yt-dlp -o "%(playlist_index&{} - |)s%(title)s.%(ext)s" URL

# Series organized by season/episode
yt-dlp -o "%(series)s/%(season_number)s - %(season)s/%(episode_number)s - %(episode)s.%(ext)s" URL

# Separate home/temp/subtitle paths
yt-dlp -P "C:/MyVideos" -P "temp:tmp" -P "subtitle:subs" -o "%(uploader)s/%(title)s.%(ext)s" URL --write-subs

# Stream straight to stdout instead of a file
yt-dlp -o - URL
```

---

## PART 7 — Format Selection, in Full

**No `-f` given** → default is `bestvideo*+bestaudio/best`; that shifts to `bestvideo+bestaudio/best` if `--audio-multistreams` is on, or degrades to `best/bestvideo+bestaudio` if ffmpeg is unavailable or you're streaming to stdout (`-o -`) — none of which applies to you since you have ffmpeg and presumably write to disk.

### Special selector keywords

| Selector | Meaning |
|---|---|
| `all` | every format, individually |
| `mergeall` | every format, merged into one file (needs `--video-multistreams`/`--audio-multistreams`) |
| `b*`, `best*` | best format containing video OR audio OR both |
| `b`, `best` | best format containing **both** video and audio in one stream — equivalent to `best*[vcodec!=none][acodec!=none]` |
| `bv`, `bestvideo` | best video-only stream — equivalent to `best*[acodec=none]` |
| `bv*`, `bestvideo*` | best format containing video (may also carry audio) — equivalent to `best*[vcodec!=none]` |
| `ba`, `bestaudio` | best audio-only stream — equivalent to `best*[vcodec=none]` |
| `ba*`, `bestaudio*` | best format containing audio (may also carry video) — generally **avoid**; you'll often get a video+audio combo when you actually wanted audio-only |
| `w*`/`worst*`, `w`/`worst`, `wv`/`worstvideo`, `wv*`/`worstvideo*`, `wa`/`worstaudio`, `wa*`/`worstaudio*` | mirror images of the above for worst quality |

`best<type>.<n>` picks the nth-ranked format of that type — `best.2` is the second-best combined format, `bv*.3` the third-best video-containing one.

A plain **file extension** also works directly for single-file formats: `-f webm`, `-f mp3` (from `3gp`, `aac`, `flv`, `m4a`, `mp3`, `mp4`, `ogg`, `wav`, `webm`).

A raw **numeric/string format code** (extractor-specific, enumerate with `-F`) selects exactly that stream: `-f 22`.

**Fallback chains** with `/` — leftmost preferred: `-f 22/17/18` tries 22, then 17, then 18, then gives up.

**Multiple simultaneous formats** with `,`: `-f 22,17,18` downloads all three if available — combine with fallback: `-f 136/137/mp4/bestvideo,140/m4a/bestaudio`.

**Merging** with `+` (needs ffmpeg — you have it): `-f bestvideo+bestaudio` downloads the best video-only and best audio-only stream separately and muxes them together. Multistream semantics get subtle here: unless `--video-multistreams`/`--audio-multistreams` are on, only the *first* format in your `+`-expression carrying a video (respectively audio) stream is kept and every later one with the same stream type is discarded — so `-f bestvideo+best+bestaudio --no-audio-multistreams` keeps `bestvideo` and `bestaudio` but drops `best`'s video stream since `bestvideo` already claimed that slot; order matters.

### Filtering with bracket conditions

`-f "best[height=720]"`, or a bare bracket with no selector defaults to filtering `best`: `-f "[filesize>10M]"`.

Numeric fields, comparable with `<` `<=` `>` `>=` `=` `!=`: `filesize`, `filesize_approx`, `width`, `height`, `aspect_ratio`, `tbr` (combined bitrate, kbps), `abr` (audio bitrate), `vbr` (video bitrate), `asr` (audio sample rate), `fps`.

String fields, comparable with `=` (equals), `^=` (starts with), `$=` (ends with), `*=` (contains), `~=` (regex match): `ext`, `acodec`, `vcodec`, `container`, `protocol`, `format_id`, `language`.

You can compound conditions and use `?` after an operator to treat a missing field as passing rather than failing the comparison.

### Sorting with `-S`/`format-sort`

Where filtering *excludes*, sorting *ranks* — generally the more robust tool, since a strict filter can leave you with nothing while a sort just re-orders what's available. Field keys you'll actually reach for: `hasvid`, `hasaud`, `ie_pref` (extractor's own opinion of format quality), `lang`, `quality`, `res` (resolution), `fps`, `hdr` (optionally ranked, e.g. `hdr:12`), `channels`, `codec` (or split as `vcodec`/`acodec` individually), `size` (an alias folding in `filesize`/`filesize_approx`), `br` (an alias for bitrate), `asr`, `proto`, `ext` (optionally with a preferred target like `ext:mp4`, still falling back gracefully rather than hard-filtering), `source`, `id`. Prefix any field with `+` to reverse it to ascending (smallest/lowest-quality first) — e.g. `-S "+size"` prefers the smallest file instead of yt-dlp's normal "biggest is best" assumption, which in practice is usually a better instinct than reaching for `-f worst`.

`--format-sort-force` lets your `-S` string override fields that would otherwise always take precedence regardless of user preference; `--format-sort-reset` throws out the built-in default ordering entirely and builds purely from what you specify.

### Worked format-selection examples

```bash
yt-dlp -f "bestvideo[height<=1080]+bestaudio/best[height<=1080]"   # cap at 1080p, merge if needed
yt-dlp -f "bv*[vcodec^=avc1]+ba[acodec^=mp4a]"                     # force specific codec families
yt-dlp -S "+size,+br"                                              # prefer smallest file / lowest bitrate
yt-dlp -S "res:720,codec:vp9"                                      # prefer 720p, prefer VP9 when tied
yt-dlp -f "ba[ext=m4a]/ba"                                         # audio-only, prefer m4a container
```

---

## PART 8 — Modifying Metadata

`--parse-metadata [WHEN:]FROM:TO` extracts data out of one field (or a literal output-template expression) using a Python regex with **named groups**, and writes those groups into other metadata fields — the canonical use is pulling structured artist/title info out of a messy, freeform description or title field before it gets embedded or used in your output template.

```bash
# A description formatted like "Artist Name - Song Title" — split it into real fields
yt-dlp --parse-metadata "description:(?P<artist>.+) - (?P<title>.+)" URL
```

`WHEN` accepts the same pipeline-stage vocabulary as `--use-postprocessor` (default `pre_process`, i.e. right after extraction, before anything else runs) — pick a later stage if the field you're parsing from isn't populated yet at that point.

`--replace-in-metadata [WHEN:]FIELDS REGEX REPLACE` does an in-place regex substitution on one or more existing fields (comma-separated `FIELDS`), repeatable to chain several replacements.

```bash
# Strip a channel's boilerplate suffix out of every title
yt-dlp --replace-in-metadata "title" " \| MyChannel$" "" URL
```

In the Python API these are typically supplied as `postprocessors` entries with `key: "MetadataParser"` and an `actions` list, but the CLI flags above are the far more ergonomic way to reach for this — you can pass them straight through as raw `ydl_opts` extra args if your wrapper just forwards arbitrary CLI-shaped strings, or construct the `MetadataParser` postprocessor action tuples directly if you want it fully native.

---

## PART 9 — Extractor Arguments

Mechanism: `--extractor-args IE_KEY:ARGS` on the CLI, or a nested dict in Python:

```python
ydl_opts = {
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
            "skip": ["dash"],
        }
    }
}
```

`IE_KEY` is the extractor's internal name (lowercase site name in most cases — `youtube`, `twitter`, `tiktok`, etc; get the exact key from `--list-extractors`). Repeatable on the CLI to configure multiple extractors in one invocation.

**Important caveat:** the actual set of supported sub-keys per extractor is not part of any stable public contract — sites change their delivery mechanics, and yt-dlp's maintainers adjust the corresponding extractor's accepted args accordingly, sometimes release to release. Commonly-seen YouTube sub-args in recent releases include `player_client` (which internal client persona to request formats as — `android`, `web`, `ios`, `tv`, etc., useful for working around client-specific throttling or format gaps), `player_skip` / `skip` (skip certain extraction sub-steps, e.g. `dash` or `hls` manifest fetching, to speed up metadata-only calls), and `formats` (e.g. `duplicate`, to include formats that would otherwise be de-duplicated away). Rather than hardcoding a list into your agent as gospel, treat `--extractor-args` as an escape hatch you reach for reactively when a specific site misbehaves, and check the extractor's current accepted keys against the yt-dlp source or wiki page for that site at the time you need it.

---

## PART 10 — Plugins

Since yt-dlp 2023.01.06, plugins are ordinary installed Python packages using a defined namespace convention — no separate plugin-loader package required.

**Directory/package layout for a local (non-pip) plugin:**
```
yt-dlp-plugins/
└── yt_dlp_plugins/
    ├── extractor/
    │   └── mysite.py       # class MySiteIE(InfoExtractor): ...
    └── postprocessor/
        └── mypp.py         # class MyPP(PostProcessor): ...
```

Drop that `yt_dlp_plugins` folder into one of the scanned plugin directories (defaults, plus anything you add with `--plugin-dirs`), or ship it as an installable package that declares the same namespace, and yt-dlp will discover and load it automatically at startup — both CLI and library use pick it up identically, since it's the same discovery code path either way. `--no-plugin-dirs` disables discovery entirely (useful in a locked-down agent deployment where you don't want an incidental file on disk silently changing extraction behavior); `--use-extractors`/`--ies` can allow/deny specific extractor names including plugin-provided ones.

---

## PART 11 — Configuration Files

Auto-loaded, in this precedence order, first match per level wins:

1. **Main config** — whatever path `--config-locations` points at.
2. **Portable config** — `yt-dlp.conf` next to the binary (or, running from source, in the parent directory of the `yt_dlp` package).
3. **Home config** — `yt-dlp.conf` in the path given to `-P` (or the current directory if `-P` wasn't given).
4. **User config** — `${XDG_CONFIG_HOME}/yt-dlp.conf`, `${XDG_CONFIG_HOME}/yt-dlp/config` (recommended location on Linux/macOS), `${XDG_CONFIG_HOME}/yt-dlp/config.txt`, and Windows equivalents under `${APPDATA}`, plus several legacy `~/.yt-dlp/...` and `~/yt-dlp.conf...` fallback paths.
5. **System config** — `/etc/yt-dlp.conf`, `/etc/yt-dlp/config`, `/etc/yt-dlp/config.txt`.

A config file is just one CLI flag per meaningful line, `#`-comments allowed, no space permitted between a dash and the flag name (`-o`, not `- o`), shell-style quoting where needed.

**`--ignore-config`** skips everything except an explicit `--config-locations`; if that flag is found *inside* a config file, no further config files load at all — placing it in the portable config, for instance, blocks home/user/system config from ever being read. As a backward-compatibility special case, finding it in the *system* config specifically only blocks the user config, not everything.

**File encoding**: decoded per a UTF BOM if present, otherwise per system locale; override with a `# coding: ENCODING` directive as the literal first line (no leading whitespace or BOM before it).

**`.netrc` auth**: create `~/.netrc` (`chmod a-rwx,u+rw` it), add one line per extractor: `machine <extractor> login <username> password <password>`, then pass `--netrc` (or `usenetrc=True`) to activate. `--netrc-cmd` is the more agent-friendly alternative — point it at a script that pulls credentials from wherever you actually store secrets, with `{}` substituted for the extractor name.

**Environment variables**: written as `${VAR}` throughout yt-dlp's own docs regardless of OS, though Windows natively uses `%VAR%` (yt-dlp accepts the Unix-style form for path-like options like `--output`/`--config-locations` even on Windows). `${XDG_CONFIG_HOME}` defaults to `~/.config`, `${XDG_CACHE_HOME}` to `~/.cache` if unset. On Windows, `~` resolves to `${HOME}` if set, else `${USERPROFILE}`, else `${HOMEDRIVE}${HOMEPATH}`.

**Practical note for an agent deployment:** you almost certainly want `ignoreconfig=True` (or `--ignore-config`) baked into your wrapper's defaults. Without it, a stray `yt-dlp.conf`/`~/.config/yt-dlp/config` left on the host machine by a human operator (or a previous unrelated tool) silently changes your agent's downloader behavior out from under you — including things like a leftover proxy, format preference, or output path nobody currently remembers setting.

---

## PART 12 — Exit Codes & Exceptions

**CLI process exit code**: `0` on full success; nonzero if anything failed (specifics live in stderr / `--verbose` output rather than in a rich set of distinct numeric codes you should branch on). With `--ignore-errors` active, individual item failures inside a batch are swallowed and the overall run can still exit `0` even though some items didn't complete — if you need per-item success/failure detail from a subprocess call, prefer `-j`/`--dump-single-json` and inspect the resulting structure/log rather than relying solely on the process exit code.

**Library return value**: `YoutubeDL.download(urls)` returns `0` on success, nonzero otherwise — same "individual failures can be swallowed by `ignoreerrors`" caveat applies.

**Python exception hierarchy** (`yt_dlp.utils`), useful to catch distinctly rather than one blanket `except Exception`:

- **`DownloadError`** — the umbrella exception for most failures that escape all the way out to your calling code; frequently wraps a more specific underlying cause.
- **`ExtractorError`** — raised inside an extractor when it can't make sense of a page/response; by the time it reaches your `try/except` it's often already been wrapped into a `DownloadError`, but its message text is usually still the most specific description of what actually went wrong.
- **`UnsupportedError`** — the URL didn't match any known extractor at all (including the generic fallback, if that was excluded).
- **`GeoRestrictedError`** — a subtype specifically indicating the content is blocked in your apparent region; worth a distinct user-facing message ("try a proxy" / "unavailable in your region") rather than a generic failure.
- **`PostProcessingError`** — the download itself succeeded but a postprocessing step (merge, extract-audio, embed, etc.) failed — often an ffmpeg problem, worth logging the full stderr from ffmpeg if you catch this.
- **`MaxDownloadsReached`** — internal signal used to implement `--max-downloads`; you generally won't need to catch this yourself unless you're doing something unusual with the download loop.
- **`ExistingVideoReached`** / **`RejectedVideoReached`** — internal signals backing `--break-on-existing` / `--break-match-filters` respectively.

```python
import yt_dlp

try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
except yt_dlp.utils.GeoRestrictedError:
    ...  # region-specific message
except yt_dlp.utils.DownloadError as e:
    ...  # generic failure, str(e) is usually agent-presentable as-is
```

---

## PART 13 — Code Cookbook

Sixteen complete, standalone recipes. Assume `import yt_dlp` at the top of each unless shown otherwise.

**1. Metadata only, no disk writes (the "dry run" your agent should default to before committing to a real download):**
```python
def peek(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreconfig": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)
```

**2. Enumerate formats/subs/thumbnails without downloading anything:**
```python
def list_options(url: str) -> dict:
    opts = {"quiet": True, "listsubtitles": True, "listformats": True, "list_thumbnails": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "formats": info.get("formats", []),
            "subtitles": list(info.get("subtitles", {}).keys()),
            "auto_captions": list(info.get("automatic_captions", {}).keys()),
            "thumbnails": info.get("thumbnails", []),
        }
```

**3. Best-quality merged download to a predictable mp4:**
```python
def download_best(url: str, out_dir: str) -> list[str]:
    opts = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": f"{out_dir}/%(title).150B [%(id)s].%(ext)s",
        "restrictfilenames": True,
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        info = ydl.sanitize_info(info)
        return [d["filepath"] for d in info.get("requested_downloads", []) if "filepath" in d]
```

**4. Audio-only extraction to mp3 with quality control:**
```python
def download_audio(url: str, out_dir: str, bitrate_kbps: str = "192") -> list[str]:
    opts = {
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": bitrate_kbps,
        }],
        "outtmpl": f"{out_dir}/%(title).150B [%(id)s].%(ext)s",
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        info = ydl.sanitize_info(info)
        return [d["filepath"] for d in info.get("requested_downloads", []) if "filepath" in d]
```

**5. Whole playlist, per-video subfolder, dedup across repeated runs:**
```python
def sync_playlist(playlist_url: str, out_dir: str, archive_path: str):
    opts = {
        "format": "bv*+ba/b",
        "outtmpl": f"{out_dir}/%(playlist)s/%(playlist_index)s - %(title)s [%(id)s].%(ext)s",
        "download_archive": archive_path,
        "ignoreerrors": True,   # one broken video shouldn't kill the whole sync
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([playlist_url])
```

**6. Only a specific time range (requires ffmpeg — you have it):**
```python
def download_clip(url: str, out_dir: str, start: str, end: str) -> list[str]:
    opts = {
        "format": "bv*+ba/b",
        "download_sections": [f"*{start}-{end}"],
        "force_keyframes_at_cuts": True,
        "outtmpl": f"{out_dir}/%(title)s_clip.%(ext)s",
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        info = ydl.sanitize_info(info)
        return [d["filepath"] for d in info.get("requested_downloads", []) if "filepath" in d]
```

**7. Subtitles: fetch, embed, and normalize to SRT:**
```python
def download_with_subs(url: str, out_dir: str, langs: list[str]) -> list[str]:
    opts = {
        "format": "bv*+ba/b",
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": langs,
        "subtitlesformat": "srt/best",
        "embedsubtitles": True,
        "postprocessors": [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}],
        "merge_output_format": "mp4",
        "outtmpl": f"{out_dir}/%(title)s.%(ext)s",
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        info = ydl.sanitize_info(info)
        return [d["filepath"] for d in info.get("requested_downloads", []) if "filepath" in d]
```

**8. Thumbnail fetch + embed as cover art:**
```python
def download_with_thumbnail(url: str, out_dir: str) -> list[str]:
    opts = {
        "format": "bestaudio/best",
        "writethumbnail": True,
        "embedthumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},
            {"key": "EmbedThumbnail"},
        ],
        "outtmpl": f"{out_dir}/%(title)s.%(ext)s",
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
```

**9. SponsorBlock: mark segments as chapters, hard-remove sponsor/self-promo:**
```python
def download_sponsorblocked(url: str, out_dir: str) -> list[str]:
    opts = {
        "format": "bv*+ba/b",
        "sponsorblock_mark": ["all"],
        "sponsorblock_remove": ["sponsor", "selfpromo"],
        "merge_output_format": "mp4",
        "outtmpl": f"{out_dir}/%(title)s.%(ext)s",
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
```

**10. Non-blocking wrapper for an async agent (yt-dlp itself is synchronous):**
```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=4)

async def download_best_async(url: str, out_dir: str, on_progress=None) -> list[str]:
    loop = asyncio.get_running_loop()

    def _sync_progress(d):
        if on_progress:
            loop.call_soon_threadsafe(on_progress, d)

    def _run():
        opts = {
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "outtmpl": f"{out_dir}/%(title)s [%(id)s].%(ext)s",
            "progress_hooks": [_sync_progress] if on_progress else [],
            "quiet": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            info = ydl.sanitize_info(info)
            return [d["filepath"] for d in info.get("requested_downloads", []) if "filepath" in d]

    return await loop.run_in_executor(_executor, _run)
```

**11. Precise, distinguishing error handling:**
```python
def safe_download(url: str, opts: dict) -> dict:
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return {"ok": True, "info": ydl.sanitize_info(info)}
    except yt_dlp.utils.GeoRestrictedError:
        return {"ok": False, "reason": "geo_restricted"}
    except yt_dlp.utils.UnsupportedError:
        return {"ok": False, "reason": "unsupported_url"}
    except yt_dlp.utils.PostProcessingError as e:
        return {"ok": False, "reason": "postprocessing_failed", "detail": str(e)}
    except yt_dlp.utils.DownloadError as e:
        return {"ok": False, "reason": "download_failed", "detail": str(e)}
```

**12. Custom format selector (video capped at 1080p, prefer smallest file at that cap):**
```python
def build_capped_selector(max_height: int = 1080):
    def selector(ctx):
        formats = [f for f in ctx["formats"] if not f.get("height") or f["height"] <= max_height]
        formats.sort(key=lambda f: f.get("filesize") or f.get("filesize_approx") or float("inf"))
        video_only = [f for f in formats if f["vcodec"] != "none" and f["acodec"] == "none"]
        combined = [f for f in formats if f["vcodec"] != "none" and f["acodec"] != "none"]
        audio_only = [f for f in formats if f["vcodec"] == "none" and f["acodec"] != "none"]
        if video_only and audio_only:
            v, a = video_only[0], audio_only[0]
            yield {
                "format_id": f'{v["format_id"]}+{a["format_id"]}',
                "ext": v["ext"],
                "requested_formats": [v, a],
                "protocol": f'{v["protocol"]}+{a["protocol"]}',
            }
        elif combined:
            yield combined[0]
    return selector

opts = {"format": build_capped_selector(1080)}
```

**13. Custom postprocessor to capture a structured event stream for your agent's UI:**
```python
from yt_dlp.postprocessor.common import PostProcessor

class AgentEventPP(PostProcessor):
    def __init__(self, emit):
        super().__init__(None)
        self._emit = emit
    def run(self, info):
        self._emit({"event": "postprocess_done", "title": info.get("title"), "filepath": info.get("filepath")})
        return [], info

def download_with_events(url: str, out_dir: str, emit) -> None:
    opts = {"format": "bv*+ba/b", "outtmpl": f"{out_dir}/%(title)s.%(ext)s", "quiet": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.add_post_processor(AgentEventPP(emit))
        ydl.download([url])
```

**14. Authenticated download using cookies pulled from an installed browser:**
```python
def download_authenticated(url: str, out_dir: str, browser: str = "chrome") -> list[str]:
    opts = {
        "format": "bv*+ba/b",
        "cookiesfrombrowser": (browser, None, None, None),
        "outtmpl": f"{out_dir}/%(title)s.%(ext)s",
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        info = ydl.sanitize_info(info)
        return [d["filepath"] for d in info.get("requested_downloads", []) if "filepath" in d]
```

**15. Proxy + browser impersonation for stubborn/blocking sites:**
```python
def download_through_proxy(url: str, out_dir: str, proxy_url: str) -> list[str]:
    opts = {
        "format": "bv*+ba/b",
        "proxy": proxy_url,
        "impersonate": "chrome",
        "outtmpl": f"{out_dir}/%(title)s.%(ext)s",
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        info = ydl.sanitize_info(info)
        return [d["filepath"] for d in info.get("requested_downloads", []) if "filepath" in d]
```

**16. Batch-download a list of URLs with per-item results instead of one all-or-nothing call:**
```python
def download_batch(urls: list[str], out_dir: str) -> list[dict]:
    results = []
    for url in urls:
        opts = {
            "format": "bv*+ba/b",
            "outtmpl": f"{out_dir}/%(title)s [%(id)s].%(ext)s",
            "quiet": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                info = ydl.sanitize_info(info)
                paths = [d["filepath"] for d in info.get("requested_downloads", []) if "filepath" in d]
                results.append({"url": url, "ok": True, "filepaths": paths})
        except yt_dlp.utils.DownloadError as e:
            results.append({"url": url, "ok": False, "error": str(e)})
    return results
```

---

## PART 14 — A Production-Ready Wrapper Class + Agent Tool Schema

A single class that ties the recipes above together into the shape an agent tool call actually wants: one entry point, structured input, structured output, no exceptions escaping uncaught.

```python
"""yt_tool.py — drop-in yt-dlp tool for a Python AI agent.
Route requests here whenever the user's intent involves fetching, inspecting,
or converting an online video/audio URL (your "yt" trigger).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Optional

import yt_dlp


class YtToolError(Exception):
    """Uniform error type surfaced to the agent, wrapping whatever yt-dlp raised."""


@dataclass
class YtResult:
    ok: bool
    filepaths: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    error: Optional[str] = None


class YtTool:
    def __init__(
        self,
        download_dir: str = "./downloads",
        ffmpeg_location: Optional[str] = None,
        cookiefile: Optional[str] = None,
        proxy: Optional[str] = None,
        download_archive: Optional[str] = None,
    ):
        self.download_dir = download_dir
        os.makedirs(download_dir, exist_ok=True)
        self.ffmpeg_location = ffmpeg_location or shutil.which("ffmpeg")
        self.cookiefile = cookiefile
        self.proxy = proxy
        self.download_archive = download_archive

    def _base_opts(self, **overrides) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreconfig": True,
            "noplaylist": True,
            "restrictfilenames": True,
            "outtmpl": os.path.join(self.download_dir, "%(title).150B [%(id)s].%(ext)s"),
        }
        if self.ffmpeg_location:
            opts["ffmpeg_location"] = self.ffmpeg_location
        if self.cookiefile:
            opts["cookiefile"] = self.cookiefile
        if self.proxy:
            opts["proxy"] = self.proxy
        if self.download_archive:
            opts["download_archive"] = self.download_archive
        opts.update(overrides)
        return opts

    def get_info(self, url: str) -> YtResult:
        opts = self._base_opts(skip_download=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return YtResult(ok=True, info=ydl.sanitize_info(info))
        except yt_dlp.utils.DownloadError as e:
            return YtResult(ok=False, error=str(e))

    def download_video(
        self,
        url: str,
        quality: str = "best",
        progress_cb: Optional[Callable[[dict], None]] = None,
    ) -> YtResult:
        quality_map = {
            "best": "bv*+ba/b",
            "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
            "720p": "bv*[height<=720]+ba/b[height<=720]",
            "480p": "bv*[height<=480]+ba/b[height<=480]",
        }
        opts = self._base_opts(
            format=quality_map.get(quality, quality),  # falls through to a raw selector string
            merge_output_format="mp4",
            progress_hooks=[progress_cb] if progress_cb else [],
        )
        return self._run(url, opts)

    def download_audio(
        self,
        url: str,
        codec: str = "mp3",
        quality: str = "192",
        progress_cb: Optional[Callable[[dict], None]] = None,
    ) -> YtResult:
        opts = self._base_opts(
            format="bestaudio/best",
            postprocessors=[{
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
                "preferredquality": quality,
            }],
            progress_hooks=[progress_cb] if progress_cb else [],
        )
        return self._run(url, opts)

    def list_formats(self, url: str) -> YtResult:
        r = self.get_info(url)
        if not r.ok:
            return r
        r.info = {"formats": r.info.get("formats", [])}
        return r

    def _run(self, url: str, opts: dict) -> YtResult:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                info = ydl.sanitize_info(info)
                paths = [d["filepath"] for d in info.get("requested_downloads", []) if "filepath" in d]
                return YtResult(ok=True, filepaths=paths, info=info)
        except yt_dlp.utils.GeoRestrictedError:
            return YtResult(ok=False, error="Content is geo-restricted in this environment.")
        except yt_dlp.utils.UnsupportedError:
            return YtResult(ok=False, error="URL is not a supported site/format.")
        except yt_dlp.utils.PostProcessingError as e:
            return YtResult(ok=False, error=f"Post-processing failed (check ffmpeg): {e}")
        except yt_dlp.utils.DownloadError as e:
            return YtResult(ok=False, error=str(e))


# --- One dispatch function, if your agent framework wants a single callable entry point ---
_tool = YtTool(download_dir="./downloads")

def yt_dispatch(url: str, mode: str = "video", quality: str = "best") -> dict:
    if mode == "info":
        result = _tool.get_info(url)
    elif mode == "audio":
        result = _tool.download_audio(url)
    else:
        result = _tool.download_video(url, quality=quality)
    return {
        "ok": result.ok,
        "filepaths": result.filepaths,
        "error": result.error,
        "title": result.info.get("title"),
        "duration": result.info.get("duration"),
    }
```

**Suggested function-calling tool schema** (adapt the outer shape to whatever your specific agent framework expects — this is written in Anthropic's tool-schema shape since that's the convention you'll most likely be plugging into, but the `input_schema` body is framework-agnostic JSON Schema):

```json
{
  "name": "yt_download",
  "description": "Fetch metadata for, download, or extract audio from a video/audio URL using yt-dlp. Supports YouTube and roughly 1,800 other sites. Use mode='info' first if you just need details (title, duration, available formats) without committing to a download.",
  "input_schema": {
    "type": "object",
    "properties": {
      "url": {
        "type": "string",
        "description": "The page or direct media URL to process."
      },
      "mode": {
        "type": "string",
        "enum": ["info", "video", "audio"],
        "description": "'info' = metadata only, no file written. 'video' = download merged video+audio. 'audio' = extract audio only (mp3)."
      },
      "quality": {
        "type": "string",
        "description": "Only used when mode='video'. One of 'best', '1080p', '720p', '480p', or a raw yt-dlp format-selector string for advanced cases.",
        "default": "best"
      }
    },
    "required": ["url", "mode"]
  }
}
```

A minimal dispatcher that ties your "yt" trigger phrase to the tool:

```python
import re

YT_TRIGGER = re.compile(r"\byt\b", re.IGNORECASE)

def route_message(user_message: str, extracted_url: str | None):
    if extracted_url and YT_TRIGGER.search(user_message):
        return yt_dispatch(extracted_url, mode="video")
    return None  # fall through to your other tools/handlers
```

---

## PART 15 — Gotchas, Footguns, and Operational Notes

- **Predicted filename ≠ final filename.** The `outtmpl` result is a prediction made *before* post-processing. Merges, remuxes, re-encodes, and fixups can all change the extension or name. Always read `info["requested_downloads"][*]["filepath"]` after a real download rather than reconstructing the path yourself from the template.
- **No ffmpeg → silently different defaults.** If ffmpeg ever isn't on `PATH` in some deployment environment, yt-dlp doesn't error — it quietly falls back to single-file "best" instead of merging separate video/audio streams. If your agent's output quality mysteriously drops in one environment, check ffmpeg availability there first.
- **YouTube specifically needs more than yt-dlp alone now.** Full support increasingly depends on `yt-dlp-ejs` plus a working JS runtime (deno by default). Missing this can manifest as throttled downloads, missing high-quality formats, or outright extraction failures that look like generic errors.
- **Age-gated, private, or login-required content needs cookies.** Either a `cookiefile` (Netscape format) or `cookiesfrombrowser` pointed at a logged-in browser profile. Without either, these will fail with an auth-related error rather than partially working.
- **`--write-info-json` / `writeinfojson` can leak personal information.** Anything pulled from an authenticated session can end up in that file. Don't pipe it straight through to an end user without a filter if your agent operates on behalf of a logged-in account.
- **A `YoutubeDL` instance is not obviously safe to share across concurrent threads.** For parallel downloads, prefer one instance per job (as in the batch/thread-pool recipes above) rather than reusing a single instance from multiple threads simultaneously.
- **There's no native "cancel this download" call.** Cancellation means either killing the owning OS process/thread, or having a `progress_hook`/`match_filter` you control raise an exception to unwind out of the download loop when your own cancel flag flips.
- **`--ignore-config` matters more than it looks like it should.** A stray config file left on a host by a previous run, a human operator, or an unrelated tool can silently change your agent's downloader behavior. Set `ignoreconfig=True` as a default in any wrapper you ship.
- **Batch jobs against one site benefit from deliberate pacing.** `sleep_interval_requests` / `sleep_interval` / `max_sleep_interval` exist specifically to avoid tripping a site's own rate-limiting or bot-detection when you're pulling many items in a row; the built-in `sleep` preset alias bundles reasonable defaults for this.
- **`--exec`'s substitution safety is deliberate, not a bug.** Only integer/float/shell-quoted conversions are allowed for substituted fields specifically so a maliciously-crafted video title can't break out of your command string — don't try to work around this by hand-building shell strings from raw info-dict fields elsewhere in your own code, since that reintroduces exactly the injection risk this guardrail exists to prevent.
- **Geo-bypass and impersonation are not guarantees.** `--xff`/`geo_bypass` and `--impersonate` are best-effort workarounds a site can defeat at any time by tightening its own detection; don't build agent logic that assumes these will always succeed.
- **Version drift.** The 90-day-old-version warning nag exists for a reason — extractors for actively-changing sites (YouTube foremost among them) break and get patched constantly. If your agent's yt-dlp integration is long-lived, plan for periodic `pip install -U yt-dlp` (and binary updates via `-U`/`--update-to nightly` on the standalone executable) rather than pinning forever.

---

## PART 16 — Condensed Cheat Sheet

The ~30 `ydl_opts` keys that cover the large majority of real agent use cases, for fast lookup without re-reading the full reference above.

| Key | Type | Purpose |
|---|---|---|
| `format` | str/callable | what to download |
| `outtmpl` | str/dict | filename template |
| `paths` | dict | per-type output directories |
| `restrictfilenames` | bool | ASCII-safe filenames |
| `ignoreerrors` | bool/str | tolerate per-item failures |
| `quiet`, `no_warnings` | bool | suppress CLI-style chatter |
| `noplaylist` | bool | single video even if URL is playlist+video |
| `playlist_items` | str | index/range selection |
| `download_archive` | path | dedup across runs |
| `match_filter` | callable/str | reject items by condition |
| `progress_hooks` | list[callable] | live download progress |
| `postprocessor_hooks` | list[callable] | live post-processing progress |
| `logger` | object | route log lines into your own logging |
| `postprocessors` | list[dict] | extract-audio, embed-*, remux, etc. |
| `merge_output_format` | str | container when merging streams |
| `writesubtitles` / `subtitleslangs` | bool / list | subtitle fetch |
| `writethumbnail` | bool | thumbnail fetch |
| `cookiefile` / `cookiesfrombrowser` | path / tuple | authenticated access |
| `proxy` | str | route traffic through a proxy |
| `impersonate` | str | browser TLS/HTTP fingerprint spoofing |
| `ratelimit` | int | cap download speed |
| `retries` / `fragment_retries` | int/"infinite" | resilience |
| `socket_timeout` | float | network timeout |
| `nocheckcertificate` | bool | skip TLS verification (use sparingly) |
| `extractor_args` | dict | site-specific tuning |
| `ffmpeg_location` | path | point at ffmpeg explicitly |
| `keepvideo` | bool | keep source file after post-processing |
| `simulate` / `skip_download` | bool | dry-run / metadata-only |
| `sleep_interval` / `max_sleep_interval` | float | politeness pacing for batch jobs |
| `ignoreconfig` | bool | don't let a host config file affect the agent |

---

*This document was assembled from yt-dlp's own CLI reference and Python-API conventions, reorganized and rewritten for the specific case of embedding it in a Python agent, plus original wrapper/cookbook code for that use case. For anything version-sensitive, `yt-dlp --version`, `yt-dlp --help`, and `python -c "import yt_dlp, pydoc; pydoc.pager(yt_dlp.YoutubeDL.__doc__)"` against your actual installed binary/package are the ground truth.*
