# Download Hub guide

For two readers. **You, the owner:** what can I do, where do I click, what do I say to Jarvis, what is the hub doing. **A developer:** how it is built, where to add a source, how it is tested. Sections 1 to 7 are for you, section 8 is for the developer, 9 and 10 are for both.

**Checked against:** sections 2 to 9 were first written against the code of `jarvis-main_163_` (build 105) and the docs repo of 2026-10-23. **On 2026-10-27 agent 9B (round 9) re-read the following against `jarvis-main_182_` (its `build_info.py` says build 115; I did not recompute its hash):** the tool tables and the owner-only paragraph (section 3), the tabs, History and Settings (section 4), the retention row (section 5), HTTP, yt-dlp and Steam (section 6), the layout and test tables (section 8) and "Not built yet" (section 9). Anything not named here still follows `_163_`. "Present in 182" means the code exists and I read it, not that it works. Nothing in this guide was tried for real. The Updates tab is not described here: its own text is written by the agent that builds it.

**Paths.** A path such as `hub/daemon.py` or `actions/download_hub.py` is inside `jarvis-cli/jarvis/`. Paths that start with `tests/`, `web/` or `DOCUMENTATION/` are at the repo root. `~/.jarvis/` is Jarvis's folder in your home folder.

**Four words that mean exactly this, everywhere in this guide** (they are the legend of section 2):

| Word | Meaning |
|---|---|
| **Tried for real** | Someone ran it on a real PC with the real program or service, and a report or result file says so. |
| **Tested against a fake** | The tests pass against a stand-in (a fake server, a fake addon, a fixture of recorded files, a fake player, a headless browser with a fake hub). It says nothing about the real thing. |
| **Written, not run** | The code exists. Nothing, not even a fake, has exercised that part. |
| **Not built** | There is no code for it. |

---

## 1. What the hub is

The Download Hub is one list of everything Jarvis downloads, kept in one place and shown in one panel (Menu > Downloads). Some downloads the hub **runs itself** (direct links, video sites, torrents). Others are run by another program, such as Steam, and the hub only **watches and shows** them.

```
 you / Jarvis ──► tool call or panel button ──► the hub's list  (~/.jarvis/hub/hub.db)
   (dl_add ...)    only queues; never downloads        │
                                                       ▼
                     the hub daemon ("Download hub" in Menu > Daemons)
                     picks waiting rows, runs the engine, handles retry / resume
                                                       │
        finished file ──► the download folder (library_root)     progress ──► the panel
                                                       │
                              "done" / "failed" ──► Jarvis's notifier (inbox, toast, phone ...)
```

- **Who starts a download:** you (the panel's add box, a palette command, or by asking Jarvis), or another program that reports its own downloads (Steam, Playnite, a tool of yours).
- **Who runs it:** the hub daemon. A tool call only puts a row in the list, because every `jarvis` command is a short-lived process. If the daemon is not running, queued rows wait (the panel's Overview says so).
- **Where it is stored:** finished files go under the download folder, `library_root` (default: `Downloads/Jarvis` in your home folder), in sub-folders by kind (`Video`, `Audio`, `Files`; the Stremio engine uses `Stremio`, the qBittorrent engine `Torrents`). The list itself is the sqlite file `~/.jarvis/hub/hub.db`.
- **Who tells you it is done:** the panel updates live; Jarvis's normal notifier announces finished and failed downloads (settings in section 5); a chat card (`present_download_status`) can show a live bar inside the conversation.

*Sources: `hub/__init__.py` (module map), `hub/daemon.py`, `hub/paths.py`, `actions/download_hub.py` (`DAEMONS`).*

---

## 2. What works today, and how sure we are

Every cell comes from a report or result file. **Nothing in this table is "works" unless it says Tried for real.** Almost nothing is.

| Capability | How sure | What the evidence is, and its limits |
|---|---|---|
| **HTTP file** (direct link) | Tested against a fake | Local test servers on loopback with real sockets, including a real daemon process killed mid-file that then resumes (`tests/test_hub_http_engine.py`, `test_hub_daemon_process.py`). No report records a download from a real website. Parallel parts (`http.segments`) are tested the same way and are off by default. |
| **File-host links** (MediaFire, Google Drive files, Buzzheavier) | Tested against a fake | A local fake server that behaves the way the link-resolver plan *describes* each site, with HTML fixtures written from that description, **not recorded from the real sites** (`tests/test_hub_hosters_*.py`, `tests/fixtures/hosters/README.md`). It shows the code does what the plan says; it cannot show the sites still behave that way. Never run against a real site. `hoster_doctor` is the check on a PC with network and a harmless test link. |
| **yt-dlp** (video and audio sites) | Tested against a fake | A fake worker process, and a real yt-dlp + ffmpeg layer against a local server that skips itself when either is missing (`tests/test_hub_ytdlp_level_b.py`). Not tried on YouTube, HLS or DASH. The reports disagree about the real yt-dlp layer, see below. |
| yt-dlp **playlists as a group** | Tested against a fake | `dl_add_ytdlp` has `playlist` (`one` or `all`) and `generic` in 182 (read); a playlist link asks, `all` queues one group of at most 50. Fake yt-dlp helper only (`tests/test_hub_ytdlp_playlists.py`, `test_hub_ytdlp_hook.py`). Never tried on YouTube. |
| yt-dlp **generic extractor** | Written, not run | Switched off in code (`GENERIC_ENABLED = False` in `hub/engines/ytdlp.py`). |
| **Torrent via qBittorrent** | Tested against a fake | A fake of qBittorrent's Web API (`tests/test_hub_qbittorrent_engine.py`). Never run against a real qBittorrent. |
| **Stremio engine** (magnet, infohash, stream, `.torrent` link or file) | Tested against a fake | A fake Stremio server built from the owner's real 2026-10-18 measurements (`tests/_hub_fake_stremio.py`). The engine has never talked to a real server. |
| Stremio **server behaviour itself** | Tried for real | Run by the owner on Windows, server version 4.21.2: ranges, the redirect, resuming a cut download, the server forgetting registrations after a restart. HTTPS and the cloud API were not run. (Findings file `00-stremio-server.md`.) |
| Stremio **start / stop / detection** and the Stremio card in Settings | Tested against a fake | Fake probes on Linux. The card was seen in headless Chromium with stand-in helpers, not in the real Settings dialog. Windows probes (registry, Start Menu, shortcuts) were not run. |
| **Discovery** (`media_find`, `media_episodes`, `stream_search`, `stremio_addons`, `stremio_library`, `stremio_mark_watched`) | Tested against a fake | Fake addons, plus **one real recording** of Cinemeta's answer for one title (it holds no streams). The words that tell dub from sub and 1080p from 720p are guesses until a real addon with streams is probed. |
| **Bulk season** (`bulk_plan`, `bulk_run`) | Tested against a fake | A fake addon pair. A real season has never been queued. |
| **Playback** (`play_episode`, `play_local`) | Tested against a fake | Fake players and a fake browser opener. No real mpv, VLC or stremio-web. The player link into stremio-web is unconfirmed. |
| **mpv control** (`play_control`) | Tested against a fake | A fake mpv on a Unix socket (Linux). The Windows named-pipe branch is written, not run. |
| **Steam** (installs and updates, read-only) | Tested against a fake | Fixtures of Steam's files. The parser was checked statically on a real PC, and one real 145-second Windows run is recorded and used as a fixture. No Steam download has been watched to the end. |
| **Playnite** (installs Jarvis starts) | Tested against a fake | A fake bridge. No report in the docs records a real Playnite run (see "Open points" in the report that came with this guide). |
| **Panel** (Downloads tab, Pause / Resume, Dismiss) | Tested against a fake | Headless Chromium on Linux against a fake hub. **Also:** the owner's own try-by-hand file (`try-by-hand-results.txt`, 2026-10-22, Windows) marks two steps as passed: the Pause button is visible without hovering, and Pause then Resume gives feedback. It does not say which download was paused, and no report describes it. |
| **Providers tab, History tab, Overview** | Tested against a fake | Plain test scripts and a fake page. No report shows the History or Providers tab in a browser. |
| **Settings > Downloads** | Tested against a fake | A fake Settings helper in Node; the Stremio card and the settle-hours row in headless Chromium with stand-ins. The writes go through `dl_prefs` and the hub's validators, which are tested (`tests/test_hub_prefs.py`). |
| **Show in folder** | Tested against a fake | The Windows command was only checked with the launching functions replaced by fakes. Never run on Windows. |
| **Folder / file picker** | Tested against a fake | Headless Chromium against the real `hub_admin` back end. Not opened from the real panel. |
| **In-chat status card** | Tested against a fake | `tests/test_download_status_card.py`. Not seen in a browser. |
| **Notifications** (done, failed, Steam update size) | Tested against a fake | Fixtures and a real local store; no real Steam update. |
| **Guest gates** (a chat guest can only look at the queue) | Tested against a fake | The add tools, `torrent_files`, the season and library tools, the play and stack tools, the Steam tools and (with the 9B patch of round 9) every other hub tool refuse or are hidden from a guest; a guest keeps `dl_list`, `dl_status` and the status card. `tests/test_hub_guest_gates.py`, `tests/test_hub_control_gates.py` (simulated guest, direct call). No real guest message in a real chat. |
| **Sources tab**, **queue arrows and drag**, **equal-size Pause and Cancel buttons**, **a way from the Sources tab into Stremio's settings** | Present in 182 (brief 24), not verified | The Sources tab is in `tool.html`, `sources` is a `hub_admin.py` op, `applyOrder` is in `tool.js`. Seen in headless Chromium on Linux against a fake hub, per the report; never in your browser. You reported that the arrows did nothing before; whether they work for you is not known. |
| **History and Settings rework** | Tested against a fake | Present in 182 (brief 29b): the History tab (day groups, search, filters, Load more, Download again, Remove from history, an export) and Settings > Downloads (seven sections plus Other, a default and **Reset** per row). Headless Chromium on Linux; never in your browser. |
| **Choosing files of a torrent** (N24) | Tested against a fake | `torrent_files`, `file_indexes` on `dl_add_stremio` and the panel's **Choose files** (`hub/stremio/torrent_files.py`, `tests/test_hub_torrent_files.py`, `test_hub_torrent_group.py`). Whether Stremio's server fetches only the chosen files was never measured. |
| **Play in one call** (round 8) | Tested against a fake | `play_episode` resolves, searches, ranks and tries by itself and takes `pick`, `quality`, `audio`, `language`, `max_gb`, `min_seeders` (`hub/stremio/play_flow.py`, `tests/test_hub_play_flow.py`). Fake addons; never a real addon, server or player. Whether it is faster was never measured. |
| **Update findings** (N23 backend) | Tested against a fake | The store tables, `UpdateRunner` in the hub daemon, a provider's `update_tool`, `dl_updates`, `dl_updates_check`. A fake update tool and clock; no real program. Steam and package-manager sources are not present. |
| **History retention row** | Tested against a fake | `retention.keep_history_days` in Settings, asks first; rows only, never files. A fake page. |
| **Covers** (thumbnails on downloads), **Epic / GOG watchers**, **Steam cancel recognition**, **Steam pause / cancel** | Not built | See section 9. |

**Where the reports disagree (the newest word wins; the code wins over a report):**

1. The old status table (written as of `_156_`) says the hub core and the panel are "written, untested". The current status page and the test files show them tested against fakes.
2. (Old.) The status page as of `_162_` listed Dismiss, the Stremio card, mpv control, local `.torrent` paths, the guest gates and yt-dlp playlists as not merged. The code of `_163_` and of 182 contains all of them; in 182 `dl_add_ytdlp` also calls the playlist helper. This guide follows the code.
3. The status page says a real yt-dlp + ffmpeg run against a local server happened. The older detail page says no real yt-dlp run yet, and the newest yt-dlp report could not repeat it (no yt-dlp installed). Not settled.
4. Older pages say the qBittorrent engine was "written, not run". Its test file against the fake does run and pass; what has never happened is a run against a real qBittorrent.
5. (Old.) The yt-dlp playlist report said playlists "reach you once a small hook is added". The hook is in 182 (`playlist` and `generic` on `dl_add_ytdlp`).

*Sources: `STATUS.md`, `docs/STATUS_DETAIL.md`, `docs/ROADMAP.md`, `docs/findings/00-stremio-server.md`, and the reports `TRACK_A` to `TRACK_E`, `PANEL_FIX_AND_N1`, `PANEL_DISMISS_AND_STREMIO_CARD`, `YTDLP_PLAYLISTS_AND_ENGINE_TESTS`, `STREMIO_TORRENT_FILES_AND_MPV`, `GUARDS_AND_DOWNLOADS_PACK`, `PHASE6_FOLLOWUPS`, `N5_STEAM_FALSE_STALL`, `MERGE_AND_BASELINE` (all in the docs repo's `docs/reports/`), and `try-by-hand-results.txt` in the code repo.*

---

## 3. Using it by talking to Jarvis

The tool names below are the real ones. The sentences are examples of what you could say; how far each source can be trusted is in section 2. Tools marked **asks first** show you a confirmation. Tools marked **owner only** refuse a chat guest.

**Add**

| Tool | What it does | You could say |
|---|---|---|
| `dl_add` (owner only) | Queues any link or magnet; the router picks the engine and the answer names it. Arguments: `url`, `urls`, `engine`, `dest`, `group`, `start_paused`, `start_at`, `dry_run`. More than 5 links in one call asks first; so does a `dest` outside the library. Also takes the path of a `.torrent` file on this PC. | `Download https://example.com/manual.pdf` |
| `dl_add_ytdlp` (owner only) | A video or audio page with yt-dlp's own options: `mode`, `quality`, `container`, `video_codec`, `audio_format`, `audio_quality`, `embed_thumbnail`, `embed_metadata`, `embed_chapters`, `write_subs`, `embed_subs`, `sub_langs`, `sponsorblock_remove`, `skip_if_downloaded`, plus the shared `url`, `urls`, `dest`, `group`, `start_paused`, `start_at`, `dry_run`. One video per link. | `Queue this video as 1080p mp4 in the background` |
| `dl_add_stremio` (owner only) | A torrent with the Stremio engine: `magnet` (also an infohash or a `.torrent` link) or `stream`, optionally `file_index` (one file) or `file_indexes` (several files of one torrent, at most 100, one group, one download per file; not together with `file_index`), plus `dest`, `group`, `start_paused`, `start_at`, `dry_run`. A torrent with many files and no choice asks which first. Needs Stremio's server. | `Download this torrent, only files 2 and 5` |
| `dl_add_http` (owner only) | Plain file links with the HTTP engine and its own options: `segments` (1 to 16 parallel parts), `filename` (one link only), `referer`; no `headers` argument, so no Authorization or Cookie is ever stored. | `Download this file in 4 parts` |
| `torrent_files` (owner only, read-only) | Lists the files inside a torrent (`source`: a magnet, an infohash, a `.torrent` link or the full path of a `.torrent` file) so you can choose; queues nothing. A magnet asks Stremio's server and may take up to half a minute. | `What's in this torrent?` |
| `ytdl_download` with `background: true` | The older video tool; with `background` it queues into the hub instead of waiting. Not allowed together with `playlist` or `output_dir`. | `Download this video, but in the background` |
| `bulk_plan`, then `bulk_run` (owner only; `bulk_run` asks first) | Plans a whole season through your Stremio addons and shows a table (queues nothing), then queues it as one group. `bulk_plan` takes `title` or `id`, `type`, `season`, `episodes`, `quality`, `audio`, `max_gb_per_episode`, `min_seeders`, `on_missing`; `bulk_run` takes `plan_id`, `override_caps`, `start_paused`. | `Plan season 1 of <show>, 1080p, dubbed` then `Run the plan` |
| `dl_import_legacy` (owner only) | Lists what the older video tool already saved as read-only history rows. Moves and deletes nothing. | `Show my old video downloads in the hub` |

**Control**

| Tool | What it does | You could say |
|---|---|---|
| `dl_pause`, `dl_resume`, `dl_retry` (owner only) | One download (`id`), a batch (`group`), an engine (`engine`), a provider (`provider`) or everything (`all: true`). Pause keeps the partial file. | `Pause everything` |
| `dl_cancel` (asks first; owner only) | Same targets. Deletes the partial file of a cancelled download; finished files are never deleted. | `Cancel that download` |
| `dl_schedule` (owner only) | `when` in your own words, plus a target. `now` starts it at once, `clear` removes the start time. One time, not a repeat. The daemon must be running. | `Start the big download tomorrow at 3am` |
| `dl_dismiss` (owner only) | Hides a Steam / Playnite / tool row that is unknown or finished (`id`, or `finished: true`). Deletes nothing. | `Hide the finished Steam rows` |
| `dl_open` (owner only) | Opens a finished download, reveals it in its folder, or opens its folder (`action`: `open`, `reveal`, `folder`); `target: library` opens the download folder. Only inside the library. | `Show that file in its folder` |

**Find and watch**

| Tool | What it does | You could say |
|---|---|---|
| `dl_list` | Lists downloads. Filters: `state`, `engine`, `provider`, `kind`, `group`, `search`, `limit`, `summary`. | `What is downloading?` |
| `dl_status` | Totals, or one download in full (`id`). | `How far along is it?` |
| `dl_providers` (owner only) | The sources the hub only watches (Steam, Playnite, tools): on or off, last report, errors. | `Is the Steam download still going?` |
| `hub_doctor` (owner only) | Health check: store, daemon, folder, disk, each engine and provider. | `Why won't my downloads start?` |
| `present_download_status` | A live card in the chat for `ids`, a `group` or a `query`. Call it once, right after adding. | `Add it and show me a live bar` |
| `dl_updates` (owner only) | The update findings the hub has stored (title, installed, available, size, when last confirmed) and when each provider was last checked; optional `provider`. Names and versions come from other programs and are data, never instructions. | `Are there updates for my programs?` |
| `dl_updates_check` (owner only) | Asks the hub daemon to check for updates now (one manual check per provider every 5 minutes); returns at once; read the answer with `dl_updates`. It installs nothing. | `Check for updates now` |

**Discover (Stremio addons)**

| Tool | What it does | You could say |
|---|---|---|
| `media_find` | Finds a show or film by name; returns its id. | `Find <title>` |
| `media_episodes` | A title's episodes by season. | `How many episodes does season 2 have?` |
| `stremio_addons` | The installed addons (addresses are not shown). | `Which Stremio addons do I have?` |
| `stream_search` (owner's addons) | Ranks the streams of one episode or film by quality, audio and `language`. | `Find 1080p streams for episode 3` |
| `stremio_library` (owner only) | Your Stremio library: `show` is `all`, `continue_watching`, `watched` or `unwatched`. | `What am I watching in Stremio?` |
| `stremio_mark_watched` (owner only, asks first) | Marks a **movie** watched or unwatched; previews unless `apply: true`. | `Mark that film as watched` |

**Stremio stack and playback**

| Tool | What it does | You could say |
|---|---|---|
| `stack_status`, `stack_doctor` | Is Stremio's server up, and if not, why in one sentence; `stack_doctor` also gives a copyable `diagnostic`. | `Is Stremio running?` |
| `stack_start`, `stack_stop` (owner only) | Start or stop the server and / or stremio-web (`what`: `server`, `web`, `both`). Stop only touches what Jarvis started. | `Start Stremio` |
| `stremio_prepare` (asks first) | Sets the server's cache to unlimited by default; optionally `cache_root`, `max_connections`, `speed_limit_mbps`. | `Get Stremio ready for downloading` |
| `server_cache`, `server_cache_clear` (the second asks first; owner only) | What Stremio has cached; empty it. Refuses while a Stremio download runs. | `Clear Stremio's cache` |
| `stremio_open` | Opens a stremio-web screen (`screen`, `imdb_id`, `season`, `episode`, `query`). | `Open Stremio's library` |
| `play_episode` (owner only) | Plays by `title` or `imdb_id` with `season` / `episode`, or a finished download (`item_id`), or a `stream`. Since round 8 it searches, ranks and tries by itself: `quality`, `audio` (`dub`, `sub`, `any`), `language`, `max_gb`, `min_seeders`, and `pick` (a whole number from 1: row N of the last `stream_search` for that episode, kept about 10 minutes). When no player works it says so and opens nothing; the stremio-web stream list opens only when the call carries `via: "stremio"`. | `Play episode 1 of <show> in 1080p` |
| `play_local` (owner only) | Plays a finished download in mpv or VLC (`id` or `query`). | `Play the file I just downloaded` |
| `play_control` (owner only) | Controls the mpv Jarvis started: `action` is `pause`, `resume`, `toggle`, `seek` (`seconds`), `stop`, `volume` (`volume`) or `status`. | `Pause the video` |

**Preferences**

| Tool | What it does | You could say |
|---|---|---|
| `dl_prefs` (owner only) | Reads or changes the everyday settings without a question (`set`), or one download's own speed limit / open-when-done / notifications (`id`); `test_notification` sends a sample. | `Limit downloads to 2 MB/s` |
| `dl_settings` (asks first; owner only) | Reads or changes any writable setting, including `library_root`, `allow_private_hosts` and `retention.keep_history_days`. | `Move my download folder to D:/Downloads/Jarvis` |
| `dl_provider_prefs` (asks first; owner only) | One provider's own choices: `enabled`, `interval_s`, `settings` (for example Steam's `notify_min_update_gb`). | `Turn the Playnite source off` |
| `hub_demo_provider` (asks first; owner only), `hub_demo_pause`, `hub_demo_resume`, `hub_demo_cancel` | Fake demo downloads that show every look of the panel. The three `hub_demo_*` tools have no guest check; they touch fake rows only. | `Start the demo downloads` |

**Typed shortcuts** (the Ask box's "/" palette): `/dl <link>`, `/dls [id]`, `/dlpause <id>`, `/dlpauseall`, `/dlresume <id>`, `/dlresumeall`, `/dlproviders`, `/stack`, `/play`. A bare `/dlpause` is refused on purpose, so it cannot pause everything by accident.

**Owner only.** A person talking to Jarvis in a chat who is not you keeps **three** tools: `dl_list`, `dl_status` and the status card `present_download_status`. Every other tool on this page is yours: they are not offered to a guest, and the handler refuses one that is called anyway (a sender that cannot be read counts as a guest). The refusal reads: "`<tool>` is the owner's: in a chat you can only look at the queue (dl_list, dl_status)" (the older tools say "someone in a chat can't use it"). **What is the case in plain 182 and what the 9B patch of round 9 changes:** in 182 the add tools, `dl_dismiss`, `torrent_files`, the season and library tools, `play_control`, `play_episode`, the Steam tools and `dl_updates_check` refuse a guest in the handler; `play_local`, `stack_start`, `stack_stop` and `server_cache_clear` are hidden from a guest but their handlers do not refuse; the control, settings, providers, doctor and demo tools were neither hidden nor refused. The patch gates all of them (and `dl_updates`); the three `hub_demo_*` buttons' tools stay as they are. The set is `OWNER_ONLY_TOOLS` in `tools.py`.

*Sources: `actions/download_hub.py`, `actions/bulk_download.py`, `actions/stremio_discovery.py`, `actions/stremio_stack.py`, `actions/play_media.py`, `actions/stremio_open.py`, `actions/download_status_card.py`, `actions/hub_demo_controls.py`, `actions/download_hub_commands.py`, `ytdl_tools.py`, `tools.py` (`OWNER_ONLY_TOOLS`); each name and argument was read from the loaded tool schemas.*

---

## 4. Using the panel

Open it from Menu > Downloads. The strip at the top shows one status line and the buttons that act on the whole list: **Pause all**, **Resume all**, **Clear finished**, a **Speed** limit menu, **Open folder**, **Settings** and **Refresh**. A small tag at the right of the tab bar says whether the panel is getting live updates or polling.

**Three tabs: Downloads, Sources, History.** (The Providers tab became **Sources**, which also lists the download engines. An Updates tab is not in 182.)

### Downloads tab (three columns)

**Left: the list.** Search box; status chips **All, Active, Waiting, Scheduled, Failed** (this one also holds cancelled rows), **Finished, Not reporting**; source chips; **Group by** Nothing, Source or Batch (a Batch header has its own Pause, Resume and Retry); **Show hidden**.

**Middle: add a download, or the selected download.**
- *Add form:* paste one or more links (several become one batch). Under **Options**: Save in folder (with **Browse...**), Start at, Speed limit for these downloads, When it finishes, Tell me when it finishes (follow my settings / always / never), Tell me through (channels), Tell me when it (events), Batch name, and **Add it paused, I will start it myself**; the button is **Add to downloads** (**+ New** brings the form back after you have looked at a download). The form asks the hub which engine would take each link, then uses that engine's own add tool.
- *Choosing files of a torrent:* with a magnet, infohash or `.torrent` link in the box, **Choose files** ("See the files inside this torrent and tick the ones you want") lists its files and queues the ticked ones as one group. A magnet link can take up to half a minute to answer.
- *Detail pane:* title, status, controls, an explanation for rows that cannot be controlled, the bar and the numbers. A finished row offers **Open**, **Show in folder** and **Open folder**.

**Right: Overview.** Counts (Active, Queued, Paused, Scheduled, Stalled, Failed, Finished, Not reporting); **Downloads at once** with **- Fewer** and **+ More**; the next scheduled start; **Speed** (hub speed, other tools' speed, the limit, **Change limit**); **Storage** (free space, folder, **Open folder**, **Change...**); **When it finishes** and whether notifications are on; **Hub** (daemon running or not; if not, it says to start "Download hub" in Menu > Daemons); **Needs attention**; **Running now**; and **Try the panel** with **Start demo** and **Clear demo**.

### What one card shows

A coloured dot, the title, a kind icon, and one **toggle button**; a bar; a line with the source tag, a **read-only** tag where it applies, the status words and the size.

- The toggle says **Pause** while a download runs, **Resume** while it is paused, **Start now** for a download waiting for its start time, and **Retry** for a failed or cancelled one. It appears only when the row's own capabilities allow it. Pressing it shows "Pausing..." (or Resuming, Starting, Retrying) at once. The Space key does the same on the selected card (the hint says so; ⚠️ not seen in a browser).
- Other buttons: **Retry** on a stalled row, **Cancel** (red; it asks first), and on a Steam or Playnite row **Dismiss** where allowed.
- A Steam row shows "Steam runs this download; pause it in Steam" (or resume), a Playnite row "Playnite gives no progress or controls", and a running Steam row a second line with what Steam last reported and how old that is.
- **Pause and Cancel:** brief 24 made the toggle and Cancel one size (present in 182, read in the report and the code, not seen in your browser).
- **Waiting rows of the hub's own** show **up and down arrows** and a **drag grip** while the live update stream is on. They are meant to change the order downloads start in (the first one listed starts first). You reported that neither worked; brief 24 changed `applyOrder` in `tool.js` (present in 182). Whether it works for you is not known.

### The states a row can be in

| State | What the card says | What it means |
|---|---|---|
| `queued` | Queued, or "Waiting to retry in N s" | Waiting for its turn, or for the retry timer after a failed attempt. |
| `probing` | Checking... | The engine is looking at the link. |
| `connecting` | Connecting... | Opening the connection. |
| `downloading` | percent, speed, time left | Bytes are moving. A leading "≈" means the hub estimated that number itself. |
| `postprocessing` | a phase name and step, such as merging | The file is being merged, converted or tagged. |
| `paused` | Paused at N %, or "Scheduled, starts <time>" | Stopped on purpose; the partial file is kept. |
| `stalled` | Stalled, with a retry time | No progress for the stall time; the hub will try again. |
| `failed` | Failed at N % | Gave up after its retries. Retry puts it back in the queue. |
| `cancelled` | Cancelled | You cancelled it. Retry puts it back in the queue. |
| `done` | Done | Finished. |
| `unknown` | Unknown | Another tool's row that its tool stopped listing (see section 7). |
| *(not a state)* | "No update 2 min ago", chip **Not reporting** | The tool that owns the row has stopped reporting. The row is flagged, not failed. |

### Pause and Resume, Dismiss, Show in folder

- **Pause / Resume.** Pause keeps what is downloaded; Resume carries on from there (a direct link asks the server to continue with a `Range` request, and a server that refuses gets the file restarted, never appended to). Rows owned by another tool can be paused only if that tool says so; Steam and Playnite rows cannot.
- **Dismiss** hides a row that **another tool reported** and that is finished, failed, cancelled or unknown. It deletes nothing and touches no file, and asks nothing. The row comes back by itself if its tool reports it running again. A download the hub made itself is never dismissed: cancel it, or remove it from History. **Clear finished** (with a count) hides every such row. **Show hidden** lists hidden rows dimmed; **Show again** only lists the row normally until you reload the panel, and the hub keeps it hidden. The Show hidden switch is not remembered.
- **Show in folder** (finished rows, detail pane) opens the file manager with the file selected. If it can only open the folder, a warning says so. Built to fix a case that opened Documents; the fix has never run on Windows.

### Sources tab (it replaced the Providers tab)

One card per download engine (HTTP, yt-dlp, Stremio, qBittorrent) under "Download engines", and one card per tool that reports its own downloads (Steam, Playnite, tools, the demo): name, a badge, "Not reporting" when it has gone quiet, the last error, a **Turn off / Turn on** switch and **Configure** (which opens Settings at that source's block). Playnite appears only after the first install Jarvis starts through it. Present in 182 (brief 24); seen in headless Chromium against a fake hub, never in your browser.

### History tab

Finished, failed and cancelled downloads under day headers, with a summary of the whole filtered list, a search, a state filter, a sort, a date range and a source filter, pages of 100 with **Load more**, a "file missing" tag, an export of what is listed, and bulk **Remove from history** and **Download again** (no file is touched). Each row can offer its open buttons, **Save a copy** (through the browser), **Download again** and **Remove from history**. Present in 182 (brief 29b); a row's Retry, the bulk Download again and the export were not clicked by the agent that built it. How long finished rows are kept is set by `retention.keep_history_days` (section 5).

### Settings > Downloads

The panel's **Settings** button opens the normal Settings dialog at its Downloads page. It has a search bar and seven sections (Files, Speed and schedule, Running at once, Sources, Notifications, When a download finishes, Safety) plus "Other" for a setting the page has no wording for; every row shows its default and has a **Reset** link; each engine and each reporting program has its own block under Sources, including Steam's update-size row and the **Stremio card**: the hub's one-sentence state of Stremio, buttons **Check again**, **Copy diagnostic**, **Locate program...**, **Locate server.js...** and **Start**, plus the server-address and stremio-web rows and a repair offer for `stremio.json`. **"Keep finished downloads in History for"** (`retention.keep_history_days`, 0 = keep forever, under "When a download finishes") asks first when the change would remove History rows. Every row is a setting from section 5, written through the hub's own checks; a refusal shows in the hub's own words. Present in 182 (brief 29b, 7C); headless Chromium and a fake page; never in your browser.

*Sources: `actions/hub_ui/tool.html`, `actions/hub_ui/tool.js`, `web/public/downloads-settings.js`, `hub/states.py`; the reports `PANEL_FIX_AND_N1`, `PANEL_DISMISS_AND_STREMIO_CARD`, `N2_SHOW_IN_FOLDER`, `TRACK_D`, and `docs/findings/03-first-manual-run-162.md` for what you reported. Everything above was read from the panel code; the browser runs behind it are the ones named in section 2.*

---

## 5. Settings

These are the keys the hub reports as writable (`hub_admin.py`, `settings` op: `writable` and `prefs_writable`). The table is generated from `hub/settings.py`; the "everyday" ones can be changed by Jarvis with `dl_prefs` without a question, the two marked "`dl_settings` only" always ask first. Anything the hub refuses comes back in words ("must be between 1 and 16").

| Key | Changed with | What it does | Default | Allowed |
|---|---|---|---|---|
| `library_root` | `dl_settings` only (asks first) | Where finished files go. Empty = your Downloads folder, sub-folder `Jarvis`. | empty (Downloads/Jarvis) | an absolute path that is not a drive or filesystem root; empty resets it |
| `folders.audio` | `dl_prefs` or `dl_settings` | Sub-folder of the library for audio files. | `Audio` | a plain sub-folder name like `Movies` or `Media/Video`; not a full path, a drive or `..` |
| `folders.file` | `dl_prefs` or `dl_settings` | Sub-folder of the library for everything else (plain files). | `Files` | a plain sub-folder name like `Movies` or `Media/Video`; not a full path, a drive or `..` |
| `folders.video` | `dl_prefs` or `dl_settings` | Sub-folder of the library for video files. | `Video` | a plain sub-folder name like `Movies` or `Media/Video`; not a full path, a drive or `..` |
| `ext_folders` | `dl_prefs` or `dl_settings` | A folder per file extension, which wins over `folders.*`: `{".iso": "ISOs"}`. | empty | an object of at most 50 extensions; a value is a sub-folder of the library root (a full path needs `dl_settings`); an empty value removes the extension |
| `rate_limit_kbps` | `dl_prefs` or `dl_settings` | Overall speed limit for the hub's own downloads, KB/s. 0 = unlimited. | `0` | whole number 0 to 10000000 |
| `schedule` | `dl_prefs` or `dl_settings` | Speed windows: inside a window its limit replaces `rate_limit_kbps` (0 there = unlimited); first match wins. | empty | a list of at most 10 windows, each `days` (all, weekdays, weekends or like mon,tue), `from`, `to` (hh:mm, not equal), `rate_limit_kbps` 0 to 10,000,000 |
| `concurrency.http` | `dl_prefs` or `dl_settings` | Most direct-link downloads at once. | `3` | whole number 1 to 16 |
| `concurrency.stremio` | `dl_prefs` or `dl_settings` | Most Stremio torrent downloads at once. | `3` | whole number 1 to 16 |
| `concurrency.total` | `dl_prefs` or `dl_settings` | Most downloads the hub runs at once, all engines together. | `4` | whole number 1 to 16 |
| `concurrency.ytdlp` | `dl_prefs` or `dl_settings` | Most yt-dlp downloads at once. | `2` | whole number 1 to 16 |
| `http.max_retries` | `dl_prefs` or `dl_settings` | Retries of a direct-link download before it fails. | `3` | whole number 0 to 10 |
| `http.segment_min_bytes` | `dl_prefs` or `dl_settings` | Only files at least this big are split into parts (and only if the server supports `Range`). | 32 MiB | whole number 1 MiB to 1 TiB |
| `http.segments` | `dl_prefs` or `dl_settings` | Parallel connections per file. 1 = off. | `1` | whole number 1 to 16 |
| `http.stall_seconds` | `dl_prefs` or `dl_settings` | Seconds without a byte before a direct-link download counts as stalled. | `60` | whole number 10 to 3,600 |
| `stremio.max_retries` | `dl_prefs` or `dl_settings` | Retries of a Stremio download before it fails (also the retries while the server is down). | `3` | whole number 0 to 10 |
| `stremio.metadata_timeout` | `dl_prefs` or `dl_settings` | Seconds to wait for a torrent's file list; no peers means no list. | `90` | whole number 10 to 600 |
| `stremio.stall_seconds` | `dl_prefs` or `dl_settings` | Seconds without a byte before a Stremio download counts as stalled. | `90` | whole number 10 to 3,600 |
| `stremio.start_wait_seconds` | `dl_prefs` or `dl_settings` | Seconds Start may wait for Stremio's server to answer. | `60` | whole number 10 to 300 |
| `after_download.open` | `dl_prefs` or `dl_settings` | What the hub does when a download finishes. | `off` | `off`, `reveal` (show the file in its folder), `folder` (open the folder) or `open` (open the file) |
| `notify.coalesce_s` | `dl_prefs` or `dl_settings` | Finishes within this many seconds fold into one message. | `8` | whole number 0 to 600 |
| `notify.enabled` | `dl_prefs` or `dl_settings` | Master switch for download notifications. | on | true or false |
| `notify.min_seconds` | `dl_prefs` or `dl_settings` | A finished download that took less than this is not announced. | `0` | whole number 0 to 86,400 |
| `notify.providers` | `dl_prefs` or `dl_settings` | Also announce other tools' downloads (Steam and so on). | on | true or false |
| `notify.done.enabled` | `dl_prefs` or `dl_settings` | Announce the `done` event. | on | true or false |
| `notify.done.level` | `dl_prefs` or `dl_settings` | Importance level of the `done` notification; empty follows the notifier's own default for downloads. | follows the notifier | a notifier level (number or name), or empty / `default` |
| `notify.failed.enabled` | `dl_prefs` or `dl_settings` | Announce the `failed` event. | on | true or false |
| `notify.failed.level` | `dl_prefs` or `dl_settings` | Importance level of the `failed` notification; empty follows the notifier's own default for downloads. | follows the notifier | a notifier level (number or name), or empty / `default` |
| `notify.group_done.enabled` | `dl_prefs` or `dl_settings` | Announce the `group_done` event. | on | true or false |
| `notify.group_done.level` | `dl_prefs` or `dl_settings` | Importance level of the `group_done` notification; empty follows the notifier's own default for downloads. | follows the notifier | a notifier level (number or name), or empty / `default` |
| `notify.queue_empty.enabled` | `dl_prefs` or `dl_settings` | Announce the `queue_empty` event. | on | true or false |
| `notify.queue_empty.level` | `dl_prefs` or `dl_settings` | Importance level of the `queue_empty` notification; empty follows the notifier's own default for downloads. | follows the notifier | a notifier level (number or name), or empty / `default` |
| `notify.stalled.enabled` | `dl_prefs` or `dl_settings` | Announce the `stalled` event. | off | true or false |
| `notify.stalled.level` | `dl_prefs` or `dl_settings` | Importance level of the `stalled` notification; empty follows the notifier's own default for downloads. | follows the notifier | a notifier level (number or name), or empty / `default` |
| `notify.started.enabled` | `dl_prefs` or `dl_settings` | Announce the `started` event. | off | true or false |
| `notify.started.level` | `dl_prefs` or `dl_settings` | Importance level of the `started` notification; empty follows the notifier's own default for downloads. | follows the notifier | a notifier level (number or name), or empty / `default` |
| `unknown_settle_hours` | `dl_prefs` or `dl_settings` | Hours before a provider row that vanished from its tool's list settles into `cancelled`. | `24` | whole number 1 to 720 |
| `allow_private_hosts` | `dl_settings` only (asks first) | Private-network hosts the hub may fetch from anyway (the safety guard refuses them otherwise). | empty | a list of `host` or `host:port`, at most 50 |

**A provider's own setting** (changed with `dl_provider_prefs`, which asks first): Steam's `notify_min_update_gb`. A Steam update is announced only when it is at least this many GB; default 1; 0 announces every update; allowed 0 to 10,000. Installs of new games follow `notify.done`.

**One download's own options** (item options, set with `dl_prefs` and an `id`, or in the add form): speed limit, open-when-done, notification choices, and `segments`. A folder you give with `dest` wins over `ext_folders`, which wins over `folders.*`.

**Asks first, whatever the tool:** choosing `open` for what happens after a download (it opens files unseen); queuing more than 5 links in one call; saving outside the library.

**Retention (7C, present in 182):** `retention.keep_history_days` (default 90, 0 = keep forever, at most 3650) is writable through `dl_settings` (it asks first, because it removes History rows) and from the Settings row; `dl_prefs` refuses it. Once an hour the daemon removes finished list **rows** older than this, never files. **`retention.auto_delete_files`** is still read by nothing and cannot be changed; the hub never deletes a file by itself. **Defaults that exist but nothing can change today:** `http.connect_timeout`, `stremio.mode` (only `save` exists), the per-call `limits`, `provider_limits`, `allowed_link_schemes`, and the `demo` block.

*Sources: `hub/settings.py` (`DEFAULTS`, `WRITABLE`, `PREFS_WRITABLE`, `PROVIDER_SETTINGS`, the validators), `hub_admin.py`, `actions/download_hub.py`. The qBittorrent engine reads its own file instead (section 6).*

---

## 6. Sources

### Direct links (HTTP)
- **Needs:** nothing installed.
- **Can:** download a `http` or `https` file in the background, resume it after a pause or a daemon restart, and name and place it safely (nothing is ever written outside the download folder or a folder you chose). Parallel parts are available but off (`http.segments`).
- **Options of one download (`dl_add_http`, 7C, present in 182):** `segments` (1 to 16 parallel parts), `filename` (exactly one link; a name, not a path), `referer` (no user name or password in it). There is no `headers` argument: no Authorization or Cookie is ever stored.
- **Cannot:** fetch from a private or loopback address unless you allow it (`allow_private_hosts`); that guard checks every redirect too.
- **Checksum (`verify`).** A caller may queue a link with the item option `verify = {"algo": "sha1" | "sha256" | "sha512", "value": "<hex>", "size": <bytes, optional>}` (the Minecraft tools use it). Only the `http` engine honours it: it hashes the finished `.part`, and a mismatch fails the row without retry and deletes only that row's own file; `size` is a ceiling (a response announcing more than it, give or take 4 KB, is refused, and so is a stream that runs past it). If another engine takes the link the option is not stored, the add still succeeds, and the add result's `note` says `verify` was ignored; a link already queued keeps the options it was queued with (the note says so). **A caller that needs integrity must read that `note` before it says "checked".**
- **Limits:** tested only against local servers.

### File hosts (MediaFire, Google Drive files, Buzzheavier)
- **Needs:** nothing installed. `dl_add` already sends these links here (the router rule has priority 60); `dl_add_hoster` forces it.
- **How it works:** the row keeps the host's *page* link. Every attempt (first run, retry, resume, restart) a resolver (`hub/hosters/<host>.py`) asks the host for the real file link, and the HTTP engine does the transfer (pause, resume, progress, the SSRF guard, safe names). The resolved link is signed and expires, which is why it is never stored. If the file server refuses a fresh link (HTTP 401, 403 or 410) the attempt is retried so the next one asks again (`hosters.max_retries`, default 4).
- **MediaFire:** reads the download button on the page and sends the page as Referer. **Google Drive:** `drive.usercontent.google.com/download`, answering the "virus scan warning" form when Google shows one; "too many users viewed or downloaded" is waited out (an hour), nothing evades it. **Buzzheavier (and bzzhr, fuckingfast):** `GET <page>/download`, the file link is in the `hx-redirect` header.
- **Cannot (yet):** folders (MediaFire folders and Drive folders are refused by name, no method is guessed), captchas (a host that needs one will say "Needs you" with the page to open), MEGA, Gofile, 1fichier and the rest of the plan's hosts.
- **Owner's controls:** `hosters.enabled.<id>` (switch one off; its links then go to the HTTP engine), `hosters.extra_hosts` (a host that moved to a new domain), `hosters.canary` (a harmless test link per host for `hoster_doctor`), `hosters.user_agent`, `concurrency.hosters`. The last two of the first group and `hosters.user_agent` go through `dl_settings` (asks first).
- **Tools:** `hoster_resolve` says what a link would do and, by asking the host, the file name, size and any wait (it never prints the file address, which carries a token); `hoster_doctor` resolves each canary link and records what it saw as that resolver's health.
- **Safety:** a Cookie a resolver learned is sent on that one attempt only and is stored nowhere (`trusted_headers`); a redirect to another host drops it. A resolved link to a private address is refused like any link. Page text is data, never run.
- **Limits:** tested only against a fake that follows the plan's description. The plan itself is a desk study; its own table says which parts are confirmed and which are guesses.

### Video sites (yt-dlp)
- **Needs:** the `yt_dlp` Python package and `ffmpeg` on PATH. Without them the hub says "yt-dlp isn't installed" or "ffmpeg isn't installed or not on PATH".
- **Can:** YouTube and sites with their own yt-dlp extractor, with quality, container, audio-only, subtitles, thumbnail, chapters and SponsorBlock options; live percent, speed and time left; merging shown as a labelled phase; pause that keeps the partial file; cancel that leaves no half-written final file. It runs the same code as `ytdl_download`.
- **Playlists (present in 182):** `dl_add_ytdlp` takes `playlist`: a playlist or channel link is never taken silently; `all` queues up to 50 of its videos as one group, `one` just the single video the link names, and without it nothing is queued and Jarvis asks. Tested against a fake yt-dlp helper only.
- **Cannot:** the generic fallback for unknown sites is off (`generic` exists as an argument but only after you said yes for that link); no cookies, proxy or account.
- **Limits:** not tried on YouTube, HLS or DASH.

### Torrents (Stremio)
- **Needs:** the Stremio desktop app's streaming server on this PC (loopback addresses only). Jarvis finds the program by itself; if it cannot, use **Locate...** in Settings > Downloads or set `server.runtime_exe` and `server.server_js` in `~/.jarvis/stremio.json`.
- **Can:** magnet links, 40-character infohashes, Stremio stream objects, `.torrent` links, and a `.torrent` file path on this PC (through `dl_add`, not `dl_add_stremio`). It saves a real, ordinary video file, picks the right episode in a pack, shows peers, resumes after a dropped connection, and switches to a fallback release when a torrent has no seeders. A magnet is **refused** while the server is down (it tells you how to start it); a row already queued retries. A torrent with no peers fails as "no seeders" after 90 s (`stremio.metadata_timeout`) and is not retried.
- **Cannot:** `warm` mode, pausing the torrents while something plays, cleaning one torrent's cache, or choosing a file inside a `.torrent` file.
- **Limits:** never run against a real server; the server's own behaviour was measured for real (section 2).

### Finding shows and whole seasons (Stremio addons)
- **Needs:** at least one installed Stremio addon that can look titles up (if none can, Cinemeta is used); `stremio_library` and `stremio_mark_watched` use your Stremio account.
- **Can:** find a title, list episodes, rank streams by quality and audio, plan a season and queue it as one group (safe to run twice; at most 60 episodes or 150 GB unless you override).
- **Cannot:** be sure about dub / sub or resolution: those words are guesses until a real addon with streams is probed. Episodes of a series cannot be marked watched from here.

### Torrents (qBittorrent, optional)
- **Needs:** qBittorrent with its Web UI switched on and kept on this PC. Set up the connection in **Settings > Downloads > Sources > qBittorrent** (the Configure button on the Sources tab lands there): the on/off switch, the Web UI address, and, unless localhost needs none, the user name and password. The form writes `~/.jarvis/qbittorrent.json`, the only place the password is kept; the page never shows it again. Off until an address is saved.
- **Options:** the rest (category, tag, where it saves, what happens when a torrent is done, magnet preference, cancel behaviour, timeouts, polling, how many run at once) are ordinary rows in the same block. If the file also holds one of those keys it wins over the row; the card says so and offers "Let the rows below decide", which removes those keys from the file.
- **Can:** with `engine="qbittorrent"` on `dl_add`, add a magnet and show live percent, speed, time left, peers and seeds. It never takes magnets by default (the Stremio engine does), unless `prefer_for_magnets` is on. A cancel removes only a torrent the hub added and deletes its partial data only inside the library.
- **Limits:** never run against a real qBittorrent. The connection (switch, address, user name, password) is not a hub setting, so no settings answer can show a secret; it is read and saved by `hub_admin` `qbittorrent_status` / `qbittorrent_save` (validated by `save_connection`, owner only).

### Steam (watched, read-only)
- **Needs:** Steam on this PC. Install `psutil` in the Python Jarvis uses; without it `hub_doctor` warns and stalls are judged from Steam's downloading folder alone.
- **Can:** show installs and updates Steam is doing, however they were started. Updates are announced only above `notify_min_update_gb`.
- **Cannot:** show a live percent, speed or time left (Steam does not write them while it downloads); a running row says what Steam last reported and how old that is. It is called stalled only when the manifest, the downloading folder and Steam's disk writes are all flat for 60 seconds. **Pause and cancel are not possible from Jarvis**, and **recognising a Steam cancel is not built**. When Steam is closed the rows go to Not reporting, never to failed.
- **Limits:** one 145-second real run, not to the end.
- **Installing and updating from chat (`steam_library`, `steam_install_game`, `steam_update_game`, owner only):** they hand a `steam://install/<digits>` link to Steam. **Where the code is:** the launcher (`install_url`, `OPEN_URL`, `launch_install`) lives in `actions/steam_tools.py`, not in `hub/steam.py`: `hub/steam.py` only watches (it has no `subprocess` import, per the rule that the hub core starts nothing). Present in 182 (6G); nothing was tried with a real Steam.

### Playnite (watched)
- **Needs:** Playnite with its bridge plugin (on `localhost:19821`) and the token in `~/.jarvis/playnite.json`.
- **Can:** after `playnite_install_game`, show an install row with an indeterminate bar that becomes done when Playnite says the game is installed.
- **Cannot:** show progress or give controls; look up names or covers.
- **Limits:** no real run is recorded in the docs. The provider appears only after the first install Jarvis starts.

### Tools that report their own downloads, the demo, and old downloads
Any tool file can declare a source (see section 8); it shows on the Providers tab and in the list. The built-in **demo** provider shows seven fake downloads covering every look of the panel (use it to check the panel). `dl_import_legacy` shows what the older video tool saved.

### Epic and GOG
Not built. Only a read-only probe script (`tests/store_checks.py`) exists, waiting for your report.

*Sources: `hub/engines/*.py`, `hub/stremio/*.py`, `hub/steam.py`, `hub/playnite_provider.py`, `hub/doctor.py`, `actions/stremio_discovery.py`, `actions/bulk_download.py`; the reports `TRACK_A`, `TRACK_B`, `TRACK_E`, `N5_STEAM_FALSE_STALL`, `PROVIDER_ROW_LIFECYCLE`, `STREMIO_TORRENT_FILES_AND_MPV`, `YTDLP_PLAYLISTS_AND_ENGINE_TESTS`, `PHASE6_DISCOVERY_BULK`.*

---

## 7. When something goes wrong

Start with `hub_doctor` (or ask "why won't my downloads start?"): it checks the store, the daemon, the folder, disk space, each engine and provider. Only causes that the code or a report supports are listed; the rest are "not known".

| What you see | Likely cause | What to try |
|---|---|---|
| Rows stay Queued; Overview says "daemon not running" | The hub daemon is not running. | Start "Download hub" in Menu > Daemons. |
| A magnet is refused | Stremio's server is not running. | `stack_status`, then `stack_start` (or open the Stremio app). |
| Stremio card says "Not found" or "Found, but not its server files" | Jarvis cannot find the program, or only part of it. | **Locate program...** and **Locate server.js...** in Settings > Downloads, or edit `~/.jarvis/stremio.json`. |
| "App open, server silent" | The Stremio app is open but its server does not answer. Jarvis starts nothing. | Close and reopen the Stremio app, or check with `stack_doctor`. |
| "Running on another port" / "Port in use" | The server answers elsewhere, or something else holds the port. | The card's sentence names the address; **Copy diagnostic** gives the details. |
| "Could not start" / "Started but not answering" | The program stopped or never answered. | **Copy diagnostic** (it includes the console's last line); `stack_doctor`. |
| A torrent fails with "no seeders" | No peers answered within `stremio.metadata_timeout` (90 s). | Retry later, pick another release, or raise the setting. Not retried by itself. |
| yt-dlp says it isn't installed / ffmpeg missing | The package or the program is missing. | `pip install yt-dlp` (or `pip install -e .[ytdl]` in `jarvis-cli/`), install ffmpeg. |
| A video page is queued as one video, not a playlist | A playlist link without `playlist` queues nothing and asks ("one" or "all"). | Answer the question, or pass `playlist: all` (up to 50 videos, one group). |
| A Steam row has no percent or speed | By design: Steam does not write them. | Read the row's note; open Steam for the real progress. |
| A Steam row says Stalled | Manifest, downloading folder and disk writes were all flat for 60 s. | Check Steam itself. |
| Steam rows show "Not reporting" | Steam is closed. | Open Steam. They are never marked failed for this. |
| A Steam or Playnite row says "Unknown" | Its tool stopped listing it (for example you cancelled it in Steam). | It settles to Cancelled after `unknown_settle_hours` (24 h), or **Dismiss** it. The hub cannot tell a Steam cancel from a vanished row. |
| Playnite is missing from the Providers tab | It registers itself on the first install Jarvis starts. | Start one with `playnite_install_game`. |
| A Playnite row has no percent | By design: Playnite gives none. | Wait for "installed". |
| No Pause button on a card | The row's tool gave no pause (Steam, Playnite, read-only tools); or the installed copy of Jarvis is older than the files. | Read the note on the card; reinstall with `script.bat` and check `jarvis version` against `jarvis-cli/jarvis/build_info.py`. |
| The queue arrows or drag do nothing | They only draw on waiting hub rows while the live stream is on; brief 24 changed the code (present in 182) but nothing was seen in your browser. | Tell us what you saw. |
| "Show in folder" opens the wrong place | The Windows command was fixed but never run on Windows. | Tell us what it opened. |
| A change to a setting is refused | The value is outside its range, or the key needs `dl_settings`. | The message names the allowed range or tool. |
| "`<tool>` is the owner's: ..." | A chat guest, or an unreadable sender, tried an owner tool (every hub tool except `dl_list`, `dl_status` and the status card, after the 9B patch). | Use it yourself in the Jarvis page. |
| `hub_doctor` warns about `psutil` | Steam is here and `psutil` is not installed. | `pip install psutil` in the Python Jarvis uses. |
| `tests/test_build_info.py` fails, or `jarvis version` does not match `build_info.py` | The stored build hash is older than the source. | Run `script.bat` (never edit `build_info.py` by hand). |

The two installs named above, each on its own line:

```
pip install yt-dlp
pip install psutil
```

*Sources: `hub/stremio/status.py`, `hub/engines/ytdlp.py` (`available`), `hub/steam.py`, `hub/doctor.py`, `hub/states.py`, `hub/service.py`; the reports `N5_STEAM_FALSE_STALL`, `PROVIDER_ROW_LIFECYCLE`, `N9_STREMIO_DETECTION`, `GUARDS_AND_DOWNLOADS_PACK`, and `OWNER_TODO.md` (the reinstall case).*

---

## 8. For developers

### The layout of `hub/`

| File | What it is for |
|---|---|
| `hub/states.py` | The state words, the allowed moves, and the cleaning of everything a provider or engine reports. Pure, no I/O. |
| `hub/store.py` | The sqlite store (items, groups, providers, events, settings). Every write goes through it. |
| `hub/settings.py` | Defaults, the one validator, the speed-schedule maths, and which keys each tool may write. |
| `hub/service.py` | What tools, the panel and the CLI call: add, control, list, dismiss, open. Dicts in, dicts out. |
| `hub/daemon.py` | The long-lived process: scheduler, one worker thread per running row, the retry policy (in `_finish`), housekeeping. |
| `hub/router.py` | "Which engine takes this input?" Engines declare their own rules. |
| `hub/engines/` | One module per engine, discovered by name: `http.py`, `ytdlp.py`, `stremio.py`, `qbittorrent.py`, `hosters.py` (`base.py` is the contract; names starting with `_` are helpers). |
| `hub/hosters/` | File-host resolvers, discovered like engines: `base.py` (the contract and its exceptions), `fetch.py` (the SSRF-guarded fetcher), one module per host (`mediafire.py`, `gdrive.py`, `buzzheavier.py`). A resolver turns a page link into a file link; it moves no bytes. |
| `hub/providers.py` | Registering and polling providers (`TOOL_DOWNLOADS`, Steam, Playnite). |
| `hub/steam.py`, `hub/playnite_provider.py`, `hub/demo.py`, `hub/legacy.py` | The built-in providers. `hub/steam.py` starts nothing; the Steam launcher is in `actions/steam_tools.py`. |
| `hub/updates.py`, `hub/updates_admin.py` | N23: the stored update findings, the runner thread inside the daemon (`UpdateRunner`), the admin ops `updates`, `updates_check`, `updates_dismiss`. |
| `hub/stremio/` | Config, detection, status words, links, server API, players, mpv control, ranking, addons, discovery, bulk, `.torrent` files and the torrent file list, and round 8's `play_flow.py`, `title_cache.py`, `pick_store.py`, `stream_cache.py`, `addon_health.py`. |
| `hub/notify.py` | When to speak (events, coalescing, once-only). Delivery is Jarvis's notifier. |
| `hub/doctor.py`, `hub/netguard.py`, `hub/naming.py`, `hub/paths.py`, `hub/reveal.py` | Health checks, the SSRF guard, safe names and folders, where things live, "show in folder". |
| `hub_admin.py`, `hub_browse.py`, `hub_client.py` | The panel's back end (`jarvis hub-admin`), the folder picker's listing, and the small client a tool uses to report progress. |
| `actions/download_hub.py` and the other tool files | The model-facing tools, schemas, keywords, confirm lists and checklist entries. |
| `actions/hub_ui/`, `web/public/downloads-settings.js`, `web/public/download-status-card.js`, `web/server.js` (`/api/hub/*`) | The panel, the Settings page, the chat card, the web routes. |

### The life of one download

1. A tool call (`dl_add`) or the panel's add box reaches `actions/download_hub.py`. A guest is refused; more than 5 links, or a folder outside the library, asks first.
2. `service.add_items` asks `router.route` which engine takes each link, then writes `queued` rows in `~/.jarvis/hub/hub.db` (one batch gets a group). Nothing is downloaded yet.
3. The hub daemon's `tick()` (about once a second) starts a worker thread for each waiting row, within `concurrency.total` and the engine's own limit.
4. The engine's `run(item, ctx)` moves the bytes, reporting progress with `ctx.update` / `ctx.progress`, and returns an `Outcome` (`done`, `paused`, `cancelled`, `failed`, `stalled`). It never sets a final state itself.
5. `daemon._finish` turns the outcome into a state, or into a retry with backoff. Pause and cancel from tools are written as requests and applied by the daemon's next tick.
6. On `done` the daemon announces it (`hub/notify.py`, through the notifier) and does the "when it finishes" action.
7. The panel reads the same rows through `hub_admin.py` (`list`, the live `watch` stream); History is the finished rows.

If the daemon dies, every row it left running goes back to `queued` on the next start and the engine resumes it.

### How rows move between states

A **hub-owned** row follows this table (`hub/states.py`). A **provider-owned** row mirrors what its tool says, so any state is accepted.

| From | May go to |
|---|---|
| `queued` | probing, connecting, downloading, paused, cancelled, failed |
| `probing`, `connecting`, `downloading` | any active state, queued, paused, stalled, failed, cancelled, done (`postprocessing` is reached from all three) |
| `postprocessing` | downloading, queued, paused, stalled, failed, cancelled, done |
| `paused` | queued, cancelled |
| `stalled` | queued, probing, connecting, downloading, paused, failed, cancelled |
| `failed`, `cancelled` | queued (Retry) |
| `done` | nothing |
| `unknown` | queued, cancelled, failed, done |

Dismissable rows are provider rows that are `done`, `failed`, `cancelled` or `unknown`.

### Adding a new source

**An engine (the hub does the transfer):**
1. Create `hub/engines/<name>.py` exporting `ENGINE` (a subclass of `engines.base.Engine` with `name`, `label`, `available()` returning `(ok, reason)`, and `run(item, ctx)`) and `ROUTE_RULES = [RouteRule("<name>", priority, match)]`. The loader imports every module there; one that fails to import is recorded in `engines.LOAD_ERRORS` and shown by `hub_doctor`, and never takes the hub down. Priorities in use: qBittorrent explicit 1, HTTP 10, yt-dlp sites 40, Stremio 50, hosters 60, yt-dlp YouTube 70, qBittorrent preferred 500.
2. **Never add an engine-specific argument to `dl_add`.** Put the source's options in its own `dl_add_<source>` tool (see `dl_add_ytdlp` and `dl_add_stremio` in `actions/download_hub.py`), or in a setting reached through `dl_prefs`.
3. Register the tool in its `actions/` file: `TOOL_SCHEMAS`, `TOOLS`, `TOOL_GROUP`, `TOOL_KEYWORDS`, `TOOL_CONFIRM_REQUIRED` for anything that deletes or runs something unseen, `TOOL_COMMANDS` if you want a palette command, and a `TEST_CHECKLIST` entry (every tool needs one; the app's Test Checklist lists them). A tool group has **one** pack instruction (`TOOL_PACK_INSTRUCTION`), so add your sentences to the existing text and keep its length under the limit its test pins. Add the tool name to `OWNER_ONLY_TOOLS` in `tools.py` and refuse guests in the handler.
4. A new setting goes in `hub/settings.py` as a default, a validator and a `PREFS_WRITABLE` / `WRITABLE` entry, never changing an existing default or range; it needs a test.
5. Write a plain-assert test under `tests/` with no network, against a fake of the real service (`tests/_hub_testserver.py`, `_hub_fake_stremio.py` and `_hub_fake_qbittorrent.py` are the patterns).

**A provider (another program does the transfer; the hub only shows it):** declare a `TOOL_DOWNLOADS` entry in an `actions/` tool file (`id`, `label`, `mode` of `push`, `poll` or `both`, and for polling a `poll_tool`; an optional `add` block makes the loader generate `dl_add_<id>`, see `tool_download_add.py`). The loader validates it. Ids `hub`, `http`, `ytdlp`, `stremio`, `engine` and `engines` are reserved. A tool reports with `hub_client.begin`, `update` and `finish`. Do not edit the built-in provider list in `hub/providers.py` for a new store watcher.

### Which tests cover what

Plain scripts, no network. Run one test, a group, or a Node script:

```
python tests/test_hub_core.py
python tests/run_tests.py hub
node tests/verify_hub_panel.js
```

| Area | Test files |
|---|---|
| Core, store, states, service, tools | `test_hub_core.py`, `test_hub_service_tools.py`, `test_hub_prefs.py`, `test_hub_dest_notify.py`, `test_hub_schedule_options.py`, `test_hub_universal_notifications.py` |
| Daemon | `test_hub_daemon.py`, `test_hub_daemon_process.py` (a real daemon process, killed and resumed) |
| Add tools and router | `test_hub_add_tools.py`, `test_router_*.py`, `test_hub_commands.py` |
| HTTP | `test_hub_http_engine.py`, `test_hub_http_segments.py` |
| File hosts | `test_hub_hosters_core.py` (registry, routing, the fetcher, `trusted_headers`, every outcome, groups, settings), `test_hub_hosters_mediafire.py`, `test_hub_hosters_gdrive.py`, `test_hub_hosters_buzzheavier.py`, `test_hub_hosters_tools.py` |
| yt-dlp | `test_hub_ytdlp.py`, `test_hub_ytdlp_level_b.py`, `test_hub_ytdlp_parity.py` (needs yt-dlp), `test_hub_ytdlp_playlists.py` |
| qBittorrent | `test_hub_qbittorrent_engine.py` |
| Stremio | `test_hub_stremio_engine.py`, `test_hub_stremio_links.py`, `test_hub_stremio_tools.py`, `test_hub_stremio_detect.py`, `test_hub_stremio_torrentfile.py`, `test_hub_stremio_mpv.py`, `test_hub_stremio_pack_instruction.py`, `test_hub_stremio_discovery.py`, `test_hub_stremio_ranking.py`, `test_hub_stremio_real_cinemeta.py`, `test_hub_bulk.py` |
| Providers, Steam, Playnite | `test_hub_providers.py`, `test_hub_provider_lifecycle.py`, `test_hub_steam_alive.py` (needs a fixture the shared copy leaves out), `test_hub_steam_threshold.py`, `test_steam_checks.py`, `test_hub_store_checks.py` |
| Panel back end and picker | `test_hub_admin.py`, `test_hub_browse.py`, `test_download_status_card.py`, `test_hub_reveal.py`, `test_hub_demo_controls.py` |
| Minecraft mod sources (queue with `verify`) | `test_minecraft_download.py`, `test_minecraft_integration.py` (the checksum on the stored row), `test_minecraft_guards.py` |
| Guest gates and pack text | `test_hub_guest_gates.py`, `test_hub_control_gates.py` (every hub tool's handler against a simulated guest), `test_hub_updates_gates.py`, `test_hub_play_gates.py`, `test_hub_play_stack_gates.py`, `test_channels_pack_instruction.py`, `test_l28b_followups.py` (the exact owner-only set) |
| Panel and Settings pages (Node) | `verify_hub_panel.js`, `verify_hub_settings.js`, `verify_hub_add_form.js`, `verify_path_picker.js`, `verify_hub_browse_routes.js` |

Test helpers (not tests): `_hub_testserver.py`, `_hub_fake_stremio.py`, `_hub_fake_addon.py`, `_hub_fake_qbittorrent.py`, `_hub_ytdlp_media.py`, `_hub_fake_hosters.py` and `_hub_hoster_env.py` (the file hosts; the HTML it serves is in `tests/fixtures/hosters/`, written from the plan, not recorded).

### The shared files, and why

Several pieces of work touch the same files, so each change to one must be small, additive and listed. A new file cannot conflict, so prefer one.

| File | Why it is shared | Rule |
|---|---|---|
| `actions/download_hub.py` | Schemas, keywords, confirm lists and checklist entries for the hub tools | Add entries only |
| `tool_registry.py`, `tools.py` | Tool registration; the owner-only set | Add your entry only |
| `hub/service.py` | The tool-facing layer | Change only the functions you must |
| `hub/store.py`, `hub/states.py` | The database and the state machine | Additive columns and fields, with a migration |
| `hub/settings.py` | Defaults, validators, writable lists | Add keys and validators; never change a default or range |
| `hub/providers.py`, `hub/daemon.py`, `hub/router.py`, `hub/notify.py` | Provider registration, the core loop, routing, notifications | Small and listed |
| `actions/hub_ui/tool.js` (with `tool.css` and `tool.html` beside it) | The whole panel is one file | One change at a time |
| `web/server.js`, `web/public/app.js`, `web/public/settings.js` | Routes, the chat page, the Settings dialog | Add your block next to the similar ones |
| `AGENTS.md`, `REPO_MAP.md` | The repo's rules and file map | Add your own lines only |
| `jarvis/build_info.py` | Generated | Regenerate with `script.bat`; never edit by hand |

*Sources: `hub/__init__.py`, `hub/engines/__init__.py`, `hub/engines/base.py`, `hub/daemon.py`, `hub/router.py`, `tool_download_add.py`, `hub_client.py`, `tests/run_tests.py`, and the docs repo's `SHARED_FILES.md` and `AGENT_START.md`.*

---

## 9. Not built yet

| What | Waits for |
|---|---|
| The Updates tab (N23): findings in the panel, "Check now", "Ignore this version" | Handed to another agent in round 9; not in 182. Its own text describes it. |
| Update sources for Steam and for package managers (winget, choco, scoop, npm) | The Steam source waits for the shared probe `tests/steam_update_checks.py`, the package managers for `tests/package_update_checks.py`; neither was run on a real PC. |
| Covers (thumbnails) on every download | Your four cover decisions (N3 a to d): the Steam cover source, the cache folder, `thumb_v` versus data URIs, and the tint rule. |
| Deleting files by retention (`retention.auto_delete_files`) | Nothing reads it and nothing can change it; not decided. (The `keep_history_days` row is built.) |
| Steam cancel recognition (a cancelled Steam download settles as Cancelled, not Unknown) | Your re-run of `steam-checks.py` to the end, with a cancel inside it. |
| Pausing or cancelling a Steam download from Jarvis | Decision S2 (recommended: keep Steam read-only). |
| Epic and GOG watchers | Your probe report from `tests/store_checks.py`. |
| The yt-dlp generic extractor switched on | A decision on the safety conditions (flag `GENERIC_ENABLED`). |
| Moving the per-download notification, after-download and speed arguments out of every add tool | Decision N4. |
| Stremio `warm` mode; pausing torrents while something plays; cleaning one torrent's cache | A real Stremio server answer to build them from (decision 6 says `save`, so `warm` is unused). |
| Dub / sub words, season-pack preference, anime pack numbering, episode-level watched state | Your addon probe (the command is under this table). |
| Scheduled "grab new episodes", watched-state sync, a rename | Listed as stretch work; not started. |
| A real run of the N22 pieces (browser downloads, Steam install / library / update, the `stremio.json` repair) | Built in the code, none tried on a real PC; your four decisions N22 a to d: not re-read. |
| Whether Stremio's server fetches only the files you chose from a torrent (N24) | The four experiments in `OWNER_TODO.md`; never measured. |
| `dl_add_http` with a `headers` argument | Decided against: a `referer` only, so no secret is stored. |
| The rest of the link-resolver plan: Gofile, Mixdrop/Streamtape/Doodstream, 1fichier, the XFileSharing hosts, MultiUp, MEGA, the browser hand-off for captchas, per-host concurrency (`concurrency.hoster_<id>`), `prefer_hosts`, MediaFire and Drive folders | Phases 2 to 7 of `Link_Resolver_for_the_Jarvis_Download_Hub.md`; its section 7 lists the decisions needed first (browser pop-up, MEGA backend, Gofile, free MEGA account). Phase 0 and 1 are built. |
| A real run of any file-host resolver | The fixtures are written from the plan, not recorded. Set `hosters.canary` to a harmless file link per host and run `hoster_doctor`. |
| Playnite name and cover lookup for Steam rows; a per-provider notification switch | Not scheduled. |
| A real run of the qBittorrent engine | The settings block and `concurrency.qbittorrent` exist (brief 31); the engine has never run against a real qBittorrent. Decision 11 (the engine's shape, including its cancel behaviour) is still waiting for your confirmation. |
| A way to un-hide a dismissed row for good, and remembering Show hidden | Not scheduled. |

The addon probe, with a Stremio addon installed that has streams, and a show you may download:

```
python tests/addon_checks.py "<a show you may download>" --season 1 --episode 1
```

Send back the `addon-answers.json` it writes.

*Sources: `docs/NOT_BUILT.md`, `OWNER_TODO.md`, `briefs/README.md`, `docs/DECISIONS.md`, and the reports named in section 2.*

---

## 10. Words used

| Word | Meaning here |
|---|---|
| **Row** | One line in the hub's list: one download, whoever runs it. |
| **Engine** | Code inside the hub that moves the bytes of a row (HTTP, yt-dlp, Stremio, qBittorrent). The hub owns those rows. |
| **Provider** | A program or tool that downloads on its own and only reports to the hub (Steam, Playnite, a tool of yours, the demo). The hub shows its rows and never runs them. |
| **Watcher** | A provider that learns about downloads by reading another program's files or asking its bridge (Steam, Playnite). |
| **Push / poll** | A provider either tells the hub (push) or the hub asks it every so often (poll). |
| **Router** | The rule that picks an engine for a link. |
| **Daemon** | The hub's long-running background process; the only thing that runs transfers. |
| **Batch / group** | Several rows added together (a season, a list of links) that can be paused or retried as one. |
| **Library root / download folder** | The folder finished files go in (`library_root`). |
| **Stale / Not reporting** | A provider row whose tool has stopped reporting. Flagged, not failed. |
| **Settle** | What the hub does to a row it can no longer see: after `unknown_settle_hours` it moves to `cancelled`. |
| **Dismiss** | Hide a provider row. Deletes nothing. |
| **Capability** | A verb a row allows (pause, resume, cancel, retry, open, reveal ...). The panel shows only the buttons a row's capabilities allow. |
| **Pack (tool pack)** | The one instruction text that goes with a group of tools and tells Jarvis how to use them. |
| **Pack (torrent)** | A torrent holding many files, such as a whole season. |
| **Infohash / magnet** | The 40-character name of a torrent, and the link that carries it. |
| **Addon / Cinemeta** | A Stremio add-on that lists titles or streams; Cinemeta is the default one that looks titles up. |
| **SSRF guard** | The check that stops the hub fetching from your private network or this PC by accident. |
| **Confirm-gated / asks first** | A tool that shows you a confirmation before it runs. |
| **Owner only** | A tool a chat guest cannot use. |
| **Dry run** | Check the arguments and say what would happen; queue nothing. |
| **Fake / fixture** | A stand-in server, addon or player, or a recording of real files, that tests use instead of the real thing. |
