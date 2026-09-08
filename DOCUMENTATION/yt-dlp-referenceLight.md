# yt-dlp — Complete Reference for Python Integration

Context assumed for this doc: you have the `yt-dlp` binary precompiled and on `PATH`, `ffmpeg`/`ffprobe` available, and you're wiring this into a Python-based AI agent that triggers on some "yt" intent. Two integration paths are covered: **(A)** shelling out to the binary via `subprocess`, and **(B)** using it as a native Python library (`pip install yt-dlp`, `import yt_dlp`). B is almost always the better choice for an agent since you get structured dicts back instead of parsing stdout.

---

## 1. Installation & Dependencies

```bash
pip install -U yt-dlp
# or, since you already have a precompiled binary on PATH, no pip install is required
# to use it as a CLI subprocess — only needed if you want `import yt_dlp` in Python.
```

- **Python**: 3.10+ (CPython) or 3.11+ (PyPy) supported for the library.
- **ffmpeg / ffprobe**: required for merging separate video+audio streams, format conversion, audio extraction, embedding thumbnails/subs/metadata, chapter splitting, SponsorBlock cutting. You already have this — good, because a large fraction of useful functionality is gated on it.
- Optional extras: `curl_cffi` (browser impersonation, `pip install "yt-dlp[default,curl-cffi]"`), `mutagen`/`AtomicParsley` (thumbnail embedding in mp4/m4a), `pycryptodomex` (AES-128 HLS decryption), `websockets`, `brotli`, `certifi`, `requests`.
- **yt-dlp-ejs** + a JS runtime (deno recommended, or node/bun/QuickJS) is now required for **full YouTube support** (signature/PO-token handling). Without it, some YouTube formats/streams may be unavailable or throttled.

---

## 2. Two Ways to Call It From Python

### A. Subprocess (calling your precompiled binary)

```python
import subprocess, json

def yt_dlp_run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["yt-dlp", *args],
        capture_output=True, text=True, check=False
    )

# Get metadata as JSON without downloading
result = yt_dlp_run(["-J", "--no-warnings", "--skip-download", URL])
info = json.loads(result.stdout)

# Actually download
result = yt_dlp_run(["-f", "bv*+ba/b", "-o", "%(title)s [%(id)s].%(ext)s", URL])
```

Pros: uses your exact binary/version; easy sandboxing; no import surface.
Cons: you're parsing text/JSON output, spawning a process per call, and progress/streaming requires parsing stdout line-by-line (`--newline` + `--progress-template`) or JSON progress hooks aren't available this way.

### B. Native Python API (`import yt_dlp`)

```python
import yt_dlp

URLS = ["https://example.com/watch?v=XXXX"]

ydl_opts = {
    "format": "bestvideo*+bestaudio/best",
    "outtmpl": "%(title)s [%(id)s].%(ext)s",
    "ffmpeg_location": "/usr/bin/ffmpeg",  # optional if ffmpeg is already on PATH
}

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    error_code = ydl.download(URLS)   # returns 0 on success
```

Every CLI flag maps to a `ydl_opts` dict key (usually the long-flag name with dashes replaced by underscores, though some differ — see `yt_dlp/YoutubeDL.py`'s docstring or `python -c "import yt_dlp, pydoc; pydoc.pager(yt_dlp.YoutubeDL.__doc__)"` for the authoritative list). Below are the important ones.

#### Extract metadata only (no download)

```python
with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
    info = ydl.extract_info(URL, download=False)
    # info is a dict; if it's a playlist, info["entries"] is a list of dicts
    title = info.get("title")
    duration = info.get("duration")
    formats = info.get("formats", [])
```

`extract_info(url, download=False)` is the core call for "just get me the metadata/JSON" — equivalent to `-J`/`--dump-single-json`.

#### Filtering with `match_filter`

```python
def longer_than_a_minute(info, *, incomplete):
    duration = info.get("duration")
    if duration and duration < 60:
        return "The video is too short"   # returning a string = reject
    return None                            # None = accept

ydl_opts = {"match_filter": longer_than_a_minute}
```

#### Progress hooks (real-time status in your agent, no stdout parsing needed)

```python
def progress_hook(d):
    if d["status"] == "downloading":
        pct = d.get("_percent_str")
        speed = d.get("_speed_str")
        eta = d.get("eta")
        # push this to your agent's event stream / UI
    elif d["status"] == "finished":
        print("Done downloading, now post-processing:", d["filename"])

ydl_opts = {"progress_hooks": [progress_hook]}
```

There's also `postprocessor_hooks` (fires during post-processing steps like merging/extracting audio) with the same `status` shape.

#### Custom logger (route yt-dlp's log lines into your agent's logging, not stdout)

```python
class AgentLogger:
    def debug(self, msg):
        if msg.startswith("[debug] "):
            return
        self.info(msg)
    def info(self, msg):
        my_logger.info(msg)
    def warning(self, msg):
        my_logger.warning(msg)
    def error(self, msg):
        my_logger.error(msg)

ydl_opts = {"logger": AgentLogger(), "quiet": True, "no_warnings": True}
```

#### Custom postprocessors (hook into the pipeline, e.g. to capture final filepath)

```python
from yt_dlp.postprocessor import PostProcessor

class FilenameCollectorPP(PostProcessor):
    def __init__(self):
        super().__init__(None)
        self.filenames = []
    def run(self, information):
        self.filenames.append(information["filepath"])
        return [], information

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    fp = FilenameCollectorPP()
    ydl.add_post_processor(fp)
    ydl.download(URLS)
# fp.filenames now holds final on-disk paths — critical since the outtmpl-predicted
# filename and the *actual* post-processed filename can differ (merges, remuxes, etc.)
```

#### Error handling

```python
try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download(URLS)
except yt_dlp.utils.DownloadError as e:
    # covers unavailable video, network errors, extractor errors, etc.
    ...
```

Common exception classes in `yt_dlp.utils`: `DownloadError`, `ExtractorError`, `UnsupportedError`, `GeoRestrictedError`, `PostProcessingError`.

---

## 3. Core `ydl_opts` Keys You'll Actually Use

| Key | CLI equivalent | Notes |
|---|---|---|
| `format` | `-f/--format` | Format selector string, see §5 |
| `outtmpl` | `-o/--output` | String, or dict for per-type templates (`{"default": "...", "thumbnail": "..."}`) |
| `paths` | `-P/--paths` | Dict, e.g. `{"home": "/downloads", "temp": "/tmp"}` |
| `restrictfilenames` | `--restrict-filenames` | bool |
| `windowsfilenames` | `--windows-filenames` | bool |
| `ignoreerrors` | `-i/--ignore-errors` | bool or `"only_download"` |
| `quiet` / `no_warnings` | `-q` / `--no-warnings` | bool |
| `noplaylist` | `--no-playlist` | bool — if URL is video+playlist, get just the video |
| `playlist_items` | `-I/--playlist-items` | e.g. `"1:5"` |
| `playlistend` / `playliststart` | (legacy) | numeric bounds |
| `download_archive` | `--download-archive` | path to txt file tracking already-downloaded IDs |
| `writesubtitles` / `writeautomaticsub` | `--write-subs` / `--write-auto-subs` | bool |
| `subtitleslangs` | `--sub-langs` | list, e.g. `["en", "en.*"]` |
| `writethumbnail` | `--write-thumbnail` | bool |
| `writeinfojson` | `--write-info-json` | bool |
| `postprocessors` | `--extract-audio` etc. | list of dicts, see §4 |
| `merge_output_format` | `--merge-output-format` | `"mp4"`, `"mkv"`, etc. |
| `cookiefile` | `--cookies` | path to Netscape cookies.txt |
| `cookiesfrombrowser` | `--cookies-from-browser` | tuple `("chrome", None, None, None)` |
| `proxy` | `--proxy` | URL string |
| `ratelimit` | `-r/--limit-rate` | bytes/sec, int |
| `retries` | `-R/--retries` | int or `"infinite"` |
| `socket_timeout` | `--socket-timeout` | seconds |
| `nocheckcertificate` | `--no-check-certificates` | bool |
| `extractor_args` | `--extractor-args` | dict, e.g. `{"youtube": {"player_client": ["android"]}}` |
| `match_filter` | `--match-filters` | callable, see above, or a filter string |
| `progress_hooks` | (n/a, CLI shows a bar) | list of callables |
| `logger` | (n/a) | object with `debug/info/warning/error` |
| `ffmpeg_location` | `--ffmpeg-location` | path to binary or its directory |
| `keepvideo` | `-k/--keep-video` | bool, keep source after post-processing |
| `overwrites` | `--force-overwrites` / `-w` | bool |
| `sleep_interval` / `max_sleep_interval` | `--sleep-interval` | seconds |
| `geo_bypass` / `geo_bypass_country` | `--xff` | bool / ISO country code |
| `impersonate` | `--impersonate` | e.g. `"chrome-110"` |
| `age_limit` | `--age-limit` | int (years) |
| `noprogress` | `--no-progress` | bool |
| `simulate` | `-s/--simulate` | bool, no download/write |

---

## 4. Postprocessors (require ffmpeg/ffprobe — which you have)

Postprocessors go in the `postprocessors` list, each a dict with a `key` and args.

```python
"postprocessors": [
    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
    {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
    {"key": "FFmpegMetadata", "add_metadata": True},
    {"key": "EmbedThumbnail"},
    {"key": "FFmpegSubtitlesConvertor", "format": "srt"},
]
```

Available PP keys (from `--postprocessor-args` docs): `Merger`, `ModifyChapters`, `SplitChapters`, `ExtractAudio` (`FFmpegExtractAudio`), `VideoRemuxer` (`FFmpegVideoRemuxer`), `VideoConvertor` (`FFmpegVideoConvertor` — this is `--recode-video`), `Metadata` (`FFmpegMetadata`), `EmbedSubtitle`, `EmbedThumbnail`, `SubtitlesConvertor`, `ThumbnailsConvertor`, `FixupStretched`, `FixupM4a`, `FixupM3u8`, `FixupTimestamp`, `FixupDuration`.

Equivalent CLI-flag shortcuts if you prefer the flag form via subprocess:

- `-x, --extract-audio` — convert to audio-only. `--audio-format` (best/aac/alac/flac/m4a/mp3/opus/vorbis/wav), `--audio-quality` (0=best–10=worst, or bitrate like `128K`, default 5)
- `--remux-video FORMAT` — change container without re-encoding (avi/flv/gif/mkv/mov/mp4/webm/aac/aiff/alac/flac/m4a/mka/mp3/ogg/opus/vorbis/wav)
- `--recode-video FORMAT` — re-encode (slower, same format list)
- `--embed-subs` / `--embed-thumbnail` / `--embed-metadata` (alias `--add-metadata`) / `--embed-chapters` (alias `--add-chapters`) / `--embed-info-json`
- `--convert-subs FORMAT` (ass/lrc/srt/vtt) and `--convert-thumbnails FORMAT` (jpg/png/webp)
- `--split-chapters` — one file per internal chapter
- `--download-sections REGEX` — download only parts of a video by time range or chapter name; `*10:15-inf`, `*from-url`, or a chapter-name regex. Needs ffmpeg.
- `--remove-chapters REGEX` — cut chapters matching a regex (e.g. SponsorBlock-style pruning)
- `--sponsorblock-mark CATS` / `--sponsorblock-remove CATS` — categories: `sponsor, intro, outro, selfpromo, preview, filler, interaction, music_offtopic, hook, poi_highlight, chapter, all, default`
- `--exec [WHEN:]CMD` — run an arbitrary shell command after a stage, with output-template substitution for arguments (only `%()d/i`, `%()f`, `%()q` conversions allowed for security)

`--postprocessor-args NAME:ARGS` (alias `--ppa`) passes raw args straight to ffmpeg/ffprobe/AtomicParsley for a given PP, including `_i`/`_o` positional placement (before input/output file) — e.g. `--ppa "Merger+ffmpeg_i1:-v quiet"`.

---

## 5. Format Selection (`-f` / `format`)

Default with no `-f` given: `bestvideo*+bestaudio/best` (or `bestvideo+bestaudio/best` if `--audio-multistreams`, or `best/bestvideo+bestaudio` if ffmpeg unavailable or streaming to stdout).

Special selectors:

- `b`, `best` — best combined video+audio
- `b*`, `best*` — best containing video OR audio OR both
- `bv`, `bestvideo` — best video-only
- `bv*`, `bestvideo*` — best containing video (may include audio)
- `ba`, `bestaudio` — best audio-only
- `ba*`, `bestaudio*` — best containing audio (avoid — usually not what you want)
- `w`/`worst` and `wv`/`wa`/etc. mirror the above for worst quality
- `all` — every format separately; `mergeall` — merge every format (needs `--video-multistreams`/`--audio-multistreams`)
- `best.2`, `bv*.3` — nth-best of a type
- File extensions work directly: `-f webm`, `-f mp4` (best single-file format of that ext)
- `-f 22` — specific numeric/string format code (extractor-specific; list with `-F`/`--list-formats`)
- Fallback chains with `/`: `-f 22/17/18` — try in order
- Multiple simultaneous downloads with `,`: `-f 22,17,18`
- Merge separate streams with `+` (needs ffmpeg): `-f bestvideo+bestaudio`

**Filtering** — bracket conditions on numeric/string fields: `-f "best[height=720]"`, `-f "bv*[height<=1080][fps>30]"`. Comparators: `<`, `<=`, `>`, `>=`, `=`, `!=`. Numeric fields include `filesize`, `filesize_approx`, `width`, `height`, `aspect_ratio`, `tbr`, `abr`, `vbr`, `asr`, `fps`. String fields (use `=`, `^=` starts-with, `$=` ends-with, `*=` contains, `~=` regex match) include `ext`, `acodec`, `vcodec`, `container`, `protocol`, `format_id`, `language`.

**Sorting** with `-S`/`--format-sort` (`SORTORDER`), e.g. `-S "res:720,codec:h264,size"` — controls which "best" actually gets picked; often more reliable than filters. Preset field examples: `res`, `fps`, `codec` (`vcodec`/`acodec` preference), `size`, `br` (bitrate), `asr`, `ext`, `hdr`, `lang`, `proto`, `source`, `vcodec`, `acodec`, `channels`, `filesize`.

**Multistream flags**: `--video-multistreams` / `--audio-multistreams` control whether `+`-merges are allowed to include more than one video/audio stream in the output.

**Merge container**: `--merge-output-format FORMAT` (avi/flv/mkv/mov/mp4/webm) — controls the container used when `+` triggers an ffmpeg merge.

---

## 6. Output Templates (`-o` / `outtmpl`)

Syntax: `%(field_name)conversion`, Python printf-style, e.g. `%(title)s`, `%(view_count)05d`.

Key field-name modifiers:
- **Dotted traversal**: `%(tags.0)s`, `%(subtitles.en.-1.ext)s`, slicing `%(id.3:7)s`
- **Arithmetic**: `%(playlist_index+10)03d`
- **strftime formatting** via `>`: `%(upload_date>%Y-%m-%d)s`
- **Alternatives** via `,`: `%(release_date>%Y,upload_date>%Y|Unknown)s`
- **Replacement** via `&`: `%(chapters&has chapters|no chapters)s`
- **Default** via `|`: `%(uploader|Unknown)s` (overrides `--output-na-placeholder`)
- **Extra conversions**: `B` (bytes), `j` (json, `#` pretty-prints), `h` (HTML-escape), `l` (comma list, `#` newline list), `q` (shell-quoted), `D` (decimal-suffix like `10M`), `S` (sanitize as filename), `U` (Unicode NFC normalize)

Default template: `%(title)s [%(id)s].%(ext)s`. Always keep `%(id)s` in the path — titles collide, IDs don't.

Per-file-type templates: prefix with a type + colon — `subtitle`, `thumbnail`, `description`, `infojson`, `link`, `pl_thumbnail`, `pl_description`, `pl_infojson`, `chapter`, `pl_video`. E.g. `outtmpl={"default": "%(title)s.%(ext)s", "thumbnail": "%(title)s/%(title)s.%(ext)s"}`.

**Important gotcha**: the predicted filename from the template can differ from the real final filename after merging/post-processing. Use `--print after_move:filepath` (CLI) or read `info["filepath"]`/a `FilenameCollectorPP` (API, see §2) to get the true final path.

### Frequently useful metadata fields (all usable in `-o` and `--print`)
`id`, `title`, `fulltitle`, `ext`, `description`, `uploader`, `uploader_id`, `channel`, `channel_id`, `upload_date` (YYYYMMDD), `timestamp`, `duration`, `duration_string`, `view_count`, `like_count`, `comment_count`, `age_limit`, `live_status`, `is_live`, `availability`, `webpage_url`, `original_url`, `extractor`, `categories`, `tags`, `thumbnail`, `thumbnails` (list of dicts), `formats` (list of dicts), `subtitles`, `automatic_captions`, `chapters`, `playlist`, `playlist_id`, `playlist_index`, `playlist_count`, `series`, `season`, `episode`, `track`, `artist`, `album`. Full field is only reliably discoverable per-extractor via `-j`/`--dump-json`.

---

## 7. Video Selection / Filtering (CLI flags, or the `ydl_opts` equivalents)

- `-I/--playlist-items "1:3,7,-5::2"` — index ranges, negative = from end, negative step = reverse
- `--min-filesize` / `--max-filesize` — e.g. `"50k"`, `"44.6M"`
- `--date` / `--datebefore` / `--dateafter` — `YYYYMMDD` or relative `today-2weeks`
- `--match-filters FILTER` — expression over any output-template field, e.g. `"like_count>100 & description~='(?i)\bfoo\b'"`; `!field` checks absence
- `--break-match-filters` — like match-filters but stops the whole queue
- `--no-playlist` / `--yes-playlist` — disambiguate video-within-playlist URLs
- `--age-limit YEARS`
- `--download-archive FILE` + `--break-on-existing` — skip/stop on already-downloaded IDs
- `--max-downloads N`
- `--playlist-random` / `--lazy-playlist` (process-as-received; disables random/reverse/`n_entries`)

---

## 8. Network, Auth, Geo

- `--proxy URL` (supports `socks5://user:pass@host:port`; empty string forces direct connection)
- `--socket-timeout`, `--source-address`, `-4/-6` (force IPv4/IPv6)
- `--impersonate CLIENT[:OS]` — e.g. `chrome-110`, `chrome:windows-10`; needs `curl_cffi`. `--list-impersonate-targets` to see options.
- `--geo-verification-proxy`, `--xff VALUE` (fake X-Forwarded-For: `default`/`never`/CIDR/ISO country code)
- `-u/--username`, `-p/--password`, `-2/--twofactor`, `-n/--netrc` (reads `~/.netrc`), `--netrc-cmd` (shell command emitting netrc-format creds — good for keeping secrets out of your agent's config)
- `--video-password`
- `--cookies FILE` (Netscape format) or `--cookies-from-browser BROWSER[+KEYRING][:PROFILE][::CONTAINER]` — browsers: brave, chrome, chromium, edge, firefox, opera, safari, vivaldi, whale
- `--client-certificate` / `--client-certificate-key` / `--client-certificate-password`

Note for an agent: cookies are the standard way to handle login-gated or age-restricted content; hardcode a `cookiefile` path in your `ydl_opts` if your agent needs authenticated access to a specific account.

---

## 9. Verbosity / Simulation / Introspection (very useful for an agent doing "check before download")

- `-s/--simulate` — resolve everything, write nothing to disk
- `-j/--dump-json` / `-J/--dump-single-json` — machine-readable metadata (this is what `extract_info(download=False)` gives you natively in the API, no need to shell out for it)
- `-O/--print [WHEN:]TEMPLATE` — print any field/template at a given pipeline stage instead of full JSON
- `--print-to-file` — same but append to a file
- `-F/--list-formats`, `--list-subs`, `--list-thumbnails` — enumerate available options for a URL, useful for building a "pick a format" UI/flow in your agent
- `--list-extractors` / `--extractor-descriptions` — enumerate all ~1800 supported sites
- `-v/--verbose` — full debug output, good for troubleshooting extractor failures
- `--ignore-no-formats-error` — extract metadata even if nothing is downloadable

---

## 10. Filesystem Options

- `-a/--batch-file FILE` — URLs one per line (or `-` for stdin), `#`/`;`/`]` = comment lines
- `-P/--paths [TYPES:]PATH` — same TYPES as `-o`, plus `home` and `temp` (files land in `temp` first, then move to `home`)
- `--restrict-filenames` (ASCII-only, no `&`/spaces) vs `--windows-filenames` (Windows-safe chars) vs default (Unicode allowed)
- `--trim-filenames LENGTH`
- `-w/--no-overwrites`, `--force-overwrites`, `-c/--continue` (resume, default), `--no-part` (skip `.part` staging files)
- `--write-description`, `--write-info-json` (⚠ may contain personal info — be mindful if these ever get exposed to end users), `--load-info-json FILE` (reuse a prior extraction without re-hitting the network)
- `--cache-dir DIR` / `--no-cache-dir` / `--rm-cache-dir` — caches signature/client-id data, defaults to `${XDG_CACHE_HOME}/yt-dlp`

---

## 11. Download Mechanics

- `-N/--concurrent-fragments N` — parallel fragment downloads for DASH/HLS (default 1)
- `-r/--limit-rate RATE`, `--throttled-rate RATE` (triggers re-extraction if speed drops below this)
- `-R/--retries`, `--fragment-retries`, `--file-access-retries`, `--extractor-retries` (all accept `"infinite"`)
- `--retry-sleep [TYPE:]EXPR` — `TYPE` in `http`/`fragment`/`file_access`/`extractor`; `EXPR` = number, `linear=START:END:STEP`, or `exp=START:END:BASE`
- `--downloader [PROTO:]NAME` (alias `--external-downloader`) — delegate to `aria2c`, `axel`, `curl`, `ffmpeg`, `httpie`, `wget` per protocol
- `--downloader-args NAME:ARGS`
- `--hls-use-mpegts` — safer container for interrupted live downloads

---

## 12. Extractor Arguments (`--extractor-args` / `extractor_args`)

Site-specific tuning, format `IE_KEY:ARGS` on CLI or a dict in the API:

```python
"extractor_args": {
    "youtube": {
        "player_client": ["android", "web"],
        "skip": ["dash", "hls"],
    }
}
```

Common YouTube sub-args include `player_client`, `player_skip`, `skip`, `formats` (e.g. `duplicate`), `innertube_host`. Exact supported keys are extractor-specific and change over time — check `yt-dlp --extractor-args youtube:help`-style discovery (or the wiki page for the specific extractor) rather than hardcoding assumptions long-term.

---

## 13. Plugins

Since v2023.01.06, yt-dlp natively loads plugin packages. Plugin dirs default to platform-specific plugin directories; extend with `--plugin-dirs DIR` (repeatable) or `--no-plugin-dirs` to disable. From the API, plugins load automatically same as CLI — no special code needed unless you want to add extractors/postprocessors from your own package, which follows the same `yt_dlp_plugins.extractor.*` / `yt_dlp_plugins.postprocessor.*` naming convention.

---

## 14. Configuration Files

yt-dlp auto-loads config from (in order): `--config-locations` path → portable `yt-dlp.conf` next to the binary → home-dir `yt-dlp.conf` → user config (`${XDG_CONFIG_HOME}/yt-dlp/config` on Linux/macOS, `%APPDATA%\yt-dlp\config` on Windows) → system config (`/etc/yt-dlp.conf`). For an embedded agent, you'll usually want `"ignoreconfig": True` in `ydl_opts` (or `--ignore-config` on CLI) so a stray user/system config file on the host machine doesn't silently change your agent's behavior.

---

## 15. Preset Aliases (CLI only, `-t`/`--preset-alias`)

| Preset | Expands to |
|---|---|
| `mp3` | `-f 'ba[acodec^=mp3]/ba/b' -x --audio-format mp3` |
| `aac` | `-f 'ba[acodec^=aac]/ba[acodec^=mp4a.40.]/ba/b' -x --audio-format aac` |
| `mp4` | `--merge-output-format mp4 --remux-video mp4 -S vcodec:h264,lang,quality,res,fps,hdr:12,acodec:aac` |
| `mkv` | `--merge-output-format mkv --remux-video mkv` |
| `sleep` | `--sleep-subtitles 5 --sleep-requests 0.75 --sleep-interval 10 --max-sleep-interval 20` |

---

## 16. Minimal, Practical Recipes for an Agent

**Get metadata only (fast, no network cost beyond extraction):**
```python
with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
    info = ydl.extract_info(url, download=False)
```

**Download best MP4 (video+audio merged), safe filename, archive-tracked so re-runs skip dupes:**
```python
ydl_opts = {
    "format": "bv*+ba/b",
    "merge_output_format": "mp4",
    "outtmpl": "%(title).150s [%(id)s].%(ext)s",
    "restrictfilenames": True,
    "download_archive": "downloaded.txt",
    "ignoreerrors": True,
    "quiet": True,
    "ffmpeg_location": "/usr/bin/ffmpeg",   # or wherever yours lives; omit if it's on PATH
}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ydl.download([url])
```

**Extract audio only as mp3:**
```python
ydl_opts = {
    "format": "bestaudio/best",
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }],
    "outtmpl": "%(title)s.%(ext)s",
}
```

**Download a specific time range without pulling the whole file (needs ffmpeg):**
```python
ydl_opts = {
    "download_sections": ["*00:30-01:15"],
    "format": "bv*+ba/b",
}
```

**Get final on-disk path reliably** — combine `progress_hooks` (status `"finished"` gives you the pre-postprocess path) with a `FilenameCollectorPP` postprocessor (gives you the true final path after any merge/remux), as shown in §2.

---

## 17. Things Worth Building Into Your Agent Wrapper

- **Always run through `extract_info(download=False)` first** if your agent needs to show the user info/formats before committing to a download — one network round trip, no disk I/O.
- **Wrap every call in `try/except yt_dlp.utils.DownloadError`** and surface `str(e)` back to the agent's reasoning loop; error strings from yt-dlp are usually specific enough to explain to a user ("Video unavailable", "Sign in to confirm your age", geo-block messages, etc.).
- **Set `"quiet": True, "no_warnings": True, "logger": <your logger>`** so raw CLI-style text never leaks into your agent's stdout/output channel.
- **Pin `download_archive`** per user/session if your agent might be asked to fetch the same content repeatedly.
- Since you already have ffmpeg, you get the full post-processing pipeline (remux, extract-audio, embed-subs/thumbnails/metadata, split-chapters, SponsorBlock cutting) — worth exposing a `postprocessors`/flag passthrough in your tool's argument schema rather than hardcoding one pipeline.
