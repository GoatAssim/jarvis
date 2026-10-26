# AGENTS.md

Instructions for AI agents working in this repository. Read this first —
it captures constraints that aren't obvious from the code alone, and
following them will save you from re-deriving things or re-breaking
things that were already fixed once.

## Where things actually live

- Package root: `jarvis-cli/jarvis/*.py`. The repo root sits one level
  above `jarvis-cli/`.
- Tests live in `tests/` at the **repo root**, next to `jarvis-cli/`, not
  inside it.
- **`REPO_MAP.md` at the repo root is the current map.** Read it before
  grepping around: file layout, where every piece of state lives, the
  subsystems worth understanding first, and the full command surface.
- **`DOCUMENTATION/outdated/` is history, not documentation.** Everything
  in there is a point-in-time snapshot and is not maintained. Several of
  those files describe designs that were never built or bugs fixed long
  ago — including the old `jarvis-codebase-orientation*.md`, which used to
  serve the role `REPO_MAP.md` now serves. If one of them disagrees with
  `REPO_MAP.md`, `REPO_MAP.md` is right. Don't "restore" something from
  there without checking the code first.
- `DOCUMENTATION/` itself now holds only things that are still true:
  `PERSONAS_GUIDE.md` and the external `everything_sdk_*` / `yt-dlp-*`
  references.
- Enhancements #8–#10 (historical tool-run reshaping, recap-level tool-call
  summarization, router activation logging) landed without accompanying
  tests in `tests/test_enhancements.py` — they were verified with one-off
  inline snippets only. If you're touching any of `ai_client._tool_runs_note()`,
  `conversations._extras_recap_fragment()`, or `tool_router.RouteResult.matches`
  /`ai_client.ask()`'s `on_route` callback, add real coverage for it while
  you're in there rather than assuming it's already tested.

## Invariants — do not break these

- **Per-turn content never goes in the cached static prompt prefix.**
  `_system_prompt_parts()` returns (static, tail). Anything derived from
  the router's decision, the user's message, the conversation or the
  sender belongs in the tail. This has been broken twice — once by
  `pack_instructions_ctx`, once by the tools blurb's playnite/spotify
  flags — and both times the symptom was silent: the prompt still worked,
  the cache just never hit. `tests/test_prompt_cache.py` guards it.
- **Download Hub (`jarvis/hub/`, `actions/download_hub.py`).** An engine only returns an
  `Outcome`; the retry / stall / restart policy lives in `daemon._finish` and nowhere else. Every row
  write goes through `Store._update` (so it is sanitised and state changes are checked); a provider
  writes only `<provider>:<id>` rows, through `Store.upsert_provider_row`. Nothing is written, opened
  or deleted outside the library root (`dl_add`'s `dest` is a sub-folder, `dl_open` takes an id never a
  path, cancel deletes only a recorded `.part`). The HTTP engine's SSRF guard (`netguard`) runs on the
  first request and every redirect; `allow_private_hosts` is editable only through the confirm-gated
  `dl_settings`. `hub_client` never raises. The panel renders download text with `textContent` only
  (`tests/verify_hub_panel.js` greps for markup assignment). The model cannot create a provider.
  `dl_add` is the router and keeps only the shared arguments: a source's own options go in that source's
  add tool (`dl_add_ytdlp`, `dl_add_stremio`), never on `dl_add`. A tool-supplied provider's `dl_add_<id>` is
  generated from the `add` block of its `TOOL_DOWNLOADS` entry (`tool_download_add.py`), never written by hand.
  Every add tool is the owner's: a chat guest is refused in the handler, whatever an allow-list says.
  A provider row is hidden, never deleted, by `dl_dismiss` (`Store.dismiss_items`: only a non-hub row that is
  `unknown` or final; `items.dismissed_at`; `list_items` and `counts` skip it; it reappears when its provider
  reports it live again). `unknown` is not permanent: `providers.settle_unknown` (daemon housekeeping) moves a
  non-stale one to `cancelled` after `unknown_settle_hours` (default 24); a stale row (provider down) never settles.
  Retention is real: the daemon calls `Store.prune_history` at start and hourly, deleting final history ROWS older
  than `retention.keep_history_days` (0 = keep forever). Nothing deletes a file; `retention.auto_delete_files` still does nothing.
- **In-chat download card and the web back end.** `present_download_status` (actions/download_status_card.py) is a read-only tool in the `downloads` group with no confirm gate: it takes an id, ids, a group or a title query, prints one `download_status` card line through `wire.media` (at most 8 cards) and returns a one-line text status. The card (`web/public/download-status-card.js`) follows `/api/hub/stream` and falls back to `/api/hub/card`; untrusted text goes in with `textContent` only. `jarvis hub-admin <op>` (hub_admin.py; JSON on stdin, JSON on stdout, `watch` = JSON lines) is the web back end behind `/api/hub/*`: reads `card`, `list`, `settings`, `history`, `file`, `thumb`; row-only writes `history_remove`, `history_clear`, `redownload`, `reorder`, refused for an unattended process (its other ops, `stremio_status`, `stremio_save`, `browse` and `mkdir`, are in the Stremio and picker rules). No model tool reaches it. Paths and thumbnails are resolved from the database by item id, never taken from the caller, and a file must sit inside the library root or the provider's `open_roots`. Routes: GET `/api/hub/card`, `list`, `settings`, `history`, `thumb/:id`, `file/:id`, `stream`; POST `/api/hub/history/remove`, `history/clear`, `redownload`, `reorder` (the Stremio and picker routes, `/api/hub/stremio`, `/stremio/paths`, `/browse`, `/browse/new-folder`, are in their own rules); reads need a loopback Host, writes also the `X-Jarvis-Hub` header.
- **Panel buttons come from the row.** A Downloads-panel button is drawn only when the row's own `capabilities` contain its verb; the toggle (Pause / Resume / Start now / Retry) is `primaryAction` in hub_ui/tool.js and calls the same tool the model uses. A row with no pause or resume says why in words (`controlNote`); never invent a verb a provider did not declare. A control tool's `note` is shown as a warning (`controlNotice`).
- **`hub/steam.py` stays read-only and inert (N5).** No subprocess, no PowerShell, no network, no steam:// command
  (`test_hub_steam_alive.py` checks the imports). A Steam row that is running never carries a live percent,
  done-bytes, speed or ETA (speed_bps / eta_s are sent as None on purpose); "stalled" needs the manifest and every
  readable second signal flat for `STALL_SECONDS`, and an unreadable signal is "unavailable", never "flat".
  `steam-checks.py` keeps copies of `STALL_SECONDS` and `WRITES_MIN_BYTES_PER_S`.
- **The Stremio half of the hub (`hub/engines/stremio.py`, `hub/stremio/`, `actions/stremio_stack.py`,
  `stremio_open.py`, `play_media.py`).** The engine is the HTTP engine's `stream_to_file` plus
  registration (`POST /<hash>/create`, then poll `stats.json`: a torrent with no peers never answers
  `create`) and a stats watchdog; storage mode is `save` only. A Stremio source is stored as ONE
  canonical magnet (`links.canonical_source`) and deduped on infohash + file index
  (`router.canonical_url`), never on trackers. The streaming server is **loopback only unless the owner
  switched on `network.allow_remote`** (Settings > Downloads > Stremio > "Allow addresses other than this PC"; saved only by
  that owner's button through `config.save_settings`, never by a tool, and refused for an unattended process):
  `hub/stremio/config.py` ignores a non-loopback `server.url` / `web.url` on read while that box is off, because the
  engine adds the server's host:port to the SSRF allow-list for its OWN calls (through `_EngineCtx`; the owner's
  `allow_private_hosts` is never edited and no other port opens). With the box on, http or https on any host is accepted
  (still no credentials, path or query), a new server address must still answer like a Stremio server before it is saved,
  and the box cannot be cleared while a saved address is not on this PC. Jarvis only STARTS stremio-web for an address that
  is on this PC (`config.is_this_pc`). Where links open is `open.with` (`browser` or `native`): native hands a
  `stremio:///...` link (`links.native_link`, only built from a link on the web address) to the OS via
  `player.open_in_app` and needs no stremio-web running. Tests: `test_hub_stremio_remote.py`. Detection of "is it up" is
  by ANSWERING on the port, never by daemon state: a server the Stremio app started reads "already
  up" and is never duplicated or killed (`stack_stop` / `server_cache_clear` only act on a server
  Jarvis started). `stremio_prepare` and `server_cache_clear` are confirm-gated by name;
  `server_cache_clear` deletes only inside `<cacheRoot>/stremio-cache`, never through a link, never
  while the server runs or a Stremio download is active. The players (`player.check_target`) play
  only video/audio files under the downloads folders and the server's own `/<hash>/<n>` link; no tool
  argument is a path or URL, and a command line is always an argv list. The account key in
  `~/.jarvis/stremio.json` is never read by this code or printed (`config.public_view` blanks it).
  **Finding Stremio (N9).** `hub/stremio/detect.py` finds the app with no setup (the configured port, a running
  Stremio, Windows' install list, the Start Menu shortcut, the usual folders; read-only, each probe takes its
  inputs as arguments); `hub/stremio/status.py` turns the facts into ONE state and plain sentence that
  `stack_status`, `stack_start`, `stack_doctor` and `hub-admin stremio_status` all share. Jarvis starts nothing
  while the Stremio app is open and its server merely silent. The paths are saved ONLY by the owner's
  Locate... button (`hub-admin stremio_save` -> `config.save_paths`: loopback, `X-Jarvis-Hub` header, refused for
  an unattended process, both files must exist); no tool names `save_paths` / `stremio_save`
  (`test_hub_stremio_detect.py` fails if one does). The start wait is the hub setting
  `stremio.start_wait_seconds` (default 60).
  Tests: `test_hub_stremio_links.py`, `_engine.py`, `_tools.py`, `_detect.py` (a fake server in
  `tests/_hub_fake_stremio.py`); the older hub tests set `JARVIS_STREMIO_SERVER_URL` to a closed port
  so a real Stremio on the dev machine can't change their answers.
- **Finding shows and downloading a season (phase 6: `actions/stremio_discovery.py`, `bulk_download.py`,
  `hub/stremio/{addons,discovery,bulk,ranking,markers}.py`).** Every addon call goes through ONE guarded client
  (`addons.Net`: `netguard.check_url` on every hop, the owner's `allow_private_hosts` is the only way past it);
  addon addresses and the account key are never shown, logged or put in an error. `markers.py` is the only place
  that knows addon wording, and it is still a guess (no real stream text has been seen). `bulk_plan` queues
  nothing and saves no plan when no installed addon offers streams; `bulk_run` is confirm-gated out of band and
  writes rows with `Store.add_item`, never through `dl_add`. A title is resolved by
  `stremio_discovery.resolve_title` (shared by `media_find`, `bulk_plan`, `play_episode`): one result whose
  normalised name equals the query and is the only such result wins; otherwise the top score must be 0.9 or more
  and lead the next by 0.15; else the tool asks and never picks. `play_episode` looks a title up only when there
  is no `imdb_id` and no `item_id`, and never before the season and episode are known. The `stremio` group has
  ONE pack instruction, in `stremio_stack.py` (`test_hub_stremio_pack_instruction.py` pins it). Tests:
  `test_hub_stremio_ranking.py`, `_discovery.py`, `test_hub_bulk.py`, `test_hub_stremio_real_cinemeta.py` (the
  owner's real Cinemeta answer in `tests/data/`, served by a fake).
- **The model may start, stop and inspect daemons; it may not create one.**
  Registering a daemon stores an argv Jarvis later runs unattended, and
  the model is the component most exposed to text written by other people
  (a chat guest, a fetched page, a file it was asked to read). There is
  deliberately no `daemon_add` tool. Same reasoning as `notify_owner`
  taking no recipient.
- **`send_dm` is the one tool that takes a recipient, so every limit on it is
  load-bearing.** It must stay owner-only (`JARVIS_CHANNEL_SENDER`), resolve
  only known contacts and never guess between two matches, stay in
  `TOOL_CONFIRM_REQUIRED`, keep its on-disk per-recipient rate limit, and
  refuse scheduled/unattended runs. The one exception (since 10-11l): a
  scheduled `tool` job whose approval includes the `message` kind may send
  (`actions/send_dm._job_authorizes_message`; an ask job or a shell the model
  started never qualifies, and it fails closed). `notify_owner` takes no recipient for the
  same reason; don't add one to it, and don't loosen `send_dm` to match.
  `tests/test_send_dm.py` pins each of these.
- **One notification system (Phase E / E5).** The Download Hub is part of it: its daemon and its panel (`host.notify`, kind `download`) both raise `notifier.notify()` records; the panel never calls `host.toast`. Everything shown to the person -- a toast,
  a browser or OS notification, a DM to the owner -- is meant to be a `notifier.notify()`
  record drawn by the one toast renderer; no module adds its own. The page calls the
  browser's `new Notification(...)` in ONE place (`JarvisNotifications.osNotice` /
  `osNotify` in `notifications.js`), never from `app.js` or a panel. A new kind of
  notification is one row in `notify_registry.KINDS`, not a new string typed at a call
  site. Every DM Jarvis sends the owner on its own initiative leaves a silent `owner_dm`
  inbox record: the `notify_owner` TOOL via `notifier.record_owner_dm`, every other caller
  (new-sender ping, rule notice, awaiting-reply DM, the Instagram group-transport notice,
  `jarvis channels-test`) via `outbound.notify_owner_recorded()` -- call that, never the
  bare `outbound.notify_owner()`. An URGENT awaiting reply is recorded at the standard
  level (toast + desktop notification), never a second DM. That kind has no Settings row
  on purpose, so a chat guest who can call the tool cannot make the screen pop. On the
  page there is ONE toast host: `JarvisUI.toastStack(position)` in `ui-kit.js`; the
  one-line `JarvisUI.toast` messages and the notifier's cards are both its children. Ask
  yes/no questions with `JarvisUI.confirm`, never the browser's native dialogs, and there
  is no separate run-confirm popup. `tests/test_one_notification_system.py` pins today's
  numbers (zero for all of the above): adding one fails it, and removing one fails it
  until you lower the number in the same patch.
  **The rule for the whole notification module (L.30): a new action (notification type), level,
  kind, position, look or tone is ONE row in `notify_registry`; no page, tool or panel keeps its
  own list.** Everything else reads it through `notifier.describe_options()` /
  `jarvis notify-config --catalog` / `/api/notifications/catalog` (the pickers in Schedules,
  Settings, the Download Hub and the notification test tool all do), and
  `tests/test_no_hardcoded_notify_lists.py` fails if one types out level names, three or more
  type ids or the old 1-5 range. How a notification is DRAWN is the record's `look`
  (`card` = a full card, `compact` = the one-line `JarvisUI.toast` called from
  `notifications.js`, `none`), set by `notifier.resolve_delivery()`: explicit channels beat
  `look=`, a level whose preset has no on-page action mutes it, and a failure or anything that
  needs acknowledging is always a card. There is no second toast path. A page click may only
  use the actions marked `page_ok` (inbox, card, small toast): never desktop, voice or a DM, and
  the test tool (`notification_test`, Menu > Notification test) never takes a recipient and
  leaves the phone channels out even at Broadcast.
- **A chat guest's details never go in `memory.py`.** That store rides
  along in the owner's own prompts. Guest facts belong in
  `channels/people.py`, which is capped, keyed by platform id, and only
  ever surfaced back into that same person's conversation.
- **Anything that can be redelivered must be deduplicated.** Both chat
  platforms redeliver (Meta retries a webhook whose 200 was lost, Discord
  replays on a resumed session). `channels/dedupe.py` is the guard and it
  is on disk, not in memory, because a gateway restart is exactly when a
  redelivery happens.
- **Per-person permissions can only take access away, and they fail closed
  (L.36).** `channels/user_perms.py` narrows what someone the gate already
  lets use tools may run (an allow-list, never a deny-list, so a tool added
  next month isn't silently open to a limited person) and whether Jarvis may
  DM them. It must never *grant* anything the gate or `send_dm`'s owner-only
  rules refuse. An unreadable `user_perms.json` means "limits unknown" — that
  message runs with tools off and `send_dm` refuses; it never reads as "no
  restrictions". The Channels panel and `jarvis channels-user` both go
  through `channels/user_admin.py` and edit the same allow-lists as
  `channels-allow`; don't add a second path that writes them, and don't let a
  bot token pass through the browser. `tests/test_user_admin.py` pins this.
  **Linking two accounts as one person (L.36b) is identity only** — it shares a
  name and notes with the model and nothing else. `permissions.py` must never
  read `linked`, and a link must never make an account the owner or inherit a
  switch. A hand-added person known only by handle is a `placeholder`; when it
  is adopted on first message its `user_perms.json` entry must move to the real
  id (a limit left behind widens access). `tests/test_channel_manual_people.py`
  pins both.
- **"Forget this person" only takes things away, in a fixed order, and refuses
  three cases (L.36-P11).** `user_admin.forget_person` removes access first
  (every dm/reply/tool list), then the person's `user_perms` limits, then -- only
  if asked -- the logs of threads that are only their DMs plus the conversations
  those logs point at, then their directory handles, and the `people.json`
  record LAST, so a failure part-way leaves them with less access and the record
  still there to retry. It refuses the owner, anyone a `"*"` entry covers in any
  list (deleting a custom tool limit for someone a wildcard still lets through
  would WIDEN their access), and anyone whose `user_perms.json` is unreadable
  (limits we can't see are never wiped). `forget_refusal` is the one place those
  live; the preview and the real run share it. It never touches the usage ledger
  (counts only), lines in group threads other people wrote in, or the linked
  account on the other platform. No model tool may reach it, the CLI only acts
  with `--yes`, and the route only with `confirm: true`. Notes are edited and
  deleted by `people.note_id` (a hash of the text), never by position: the list
  is a ring buffer a guest can push to.
- **The three per-person insight views are read-only, and the usage ledger holds
  counts, never text (L.36-P1/P2/P3).** `user_admin.conversation_view`,
  `usage_view` and `simulate` change nothing. `simulate` ("Test as this person")
  is a dry run of the REAL gate — `permissions.decide` plus
  `user_perms.resolve_tool_access`, which `base.handle_message` also calls, so the
  two cannot drift. It must never call a model, send, or write anything: no
  transcript line, no usage line, no `people.touch`, no cooldown mark, no
  conversation. `channels/usage.py` records tokens, requests and tool *names* only
  — never a message, reply, tool argument or result — because it is written even
  when `log_conversations` is off. A reply with no `to_user` in a group thread is
  attributed to nobody; don't "improve" `transcript.person_records` into guessing,
  that puts one person's conversation in another's view. If a stage is added to
  `permissions.decide`, add it to `user_admin.GATE_STAGES`
  (`tests/test_channel_insights.py` forces a denial at every stage and fails if
  the walkthrough is missing one). `tests/test_channel_insights.py` pins all of this.
- **Server / channel switches only take access away, and the "where" filters are
  normalized once (L.36-P7).** `channels/servers.py` writes ONLY into `scopes`
  (`guild:<id>` / `channel:<id>`): `enabled` / `tools` off -> `false`, on -> the key
  is removed (never written `true`, which would GRANT over a switched-off platform);
  `mention` can be turned on or inherited, never off. It must never write
  `allowed_guilds` / `allowed_channels` -- they are "empty = unrestricted", so removing
  the last entry would open every server. `allowed_guilds` alone has ONE separate,
  narrower writer, `channels/allowed_guilds.py` (L.36-P17; panel "Allowed servers" /
  `jarvis channels-guilds`): adding to an EMPTY list (which turns the filter on and
  silences every other server) or adding an id the bot has never seen only previews
  until `confirm` / `--yes`; removing the LAST entry is refused always, confirmed or not
  (clearing the filter is a deliberate `channels-set discord allowed_guilds []`); it
  writes only that one key. `allowed_channels` is still hand-edited. Those two lists, like the allow-lists, go
  through `config.normalize_entries` in `load_config()`, in `permissions.resolve_scope()`
  and in `permissions.decide()`: a hand-typed bare id (`"allowed_guilds": 123`) is a
  one-entry list, not a `TypeError`. Never iterate a config list directly -- use the
  normalizer. Names (`guild_name`, `channel_name`) are display only; no stage of
  `decide()` reads them. `tests/test_channel_servers.py` and
  `tests/test_allowed_guilds.py` pin all of this.
- **Shared rooms (server channels, threads, group chats) are a different place from a DM, and
  the difference is enforced in seven ways (channel spaces, L.57).** (1) Speaker labels
  (`[Name]: ` on saved turns, the group paragraph in the prompt tail) and every room field in
  the transcript exist ONLY for rooms; a DM's exchange, history and prompt stay byte-identical
  (`group_speaker_labels` off restores the old room behaviour too). (2) `channels/spaces.json`
  is a label cache that records only ADDRESSED messages; nothing reads it for a decision, and
  it is rebuildable from the transcripts. (3) The per-room `log` (saving) and `ambient`
  (reading) switches only take access away: off -> a literal `false` in `scopes`, on -> the
  key is removed, never written `true`, and only a literal `false` counts when read
  (`permissions.resolve_scope`, `permissions.ambient_room_on`), so a hand-typed `"false"` neither
  mutes nor un-mutes a room, and a scope can never turn saving back on over a platform that has
  it off. `servers._apply_switch` is the one writer (`spaces.set_switch` reaches it for any
  platform, by thread id). A room with saving off gets no conversation id at all: `handle_message`
  overrides the ONE key `log_conversations` in `cfg` so all of its writes agree. (4) Recalled
  per-person items reach the prompt in a room only under the S6 rules: the platform's
  `group_recall` (default OFF, literal `true` only) or the item's own `on`; `off` always wins; the
  name, the owner's style note and standing rules are never gated. The flags (`people.note_groups`,
  person_memory `groups`) are set by the owner only (`user_admin.set_note_groups` /
  `set_memory_groups`, the CLI and panel routes); NO model tool reads or writes them, and the
  tools that echo notes (`who_am_i_talking_to`, `remember_sender`) apply the same gate through
  `room_recall` in the sender env. (5) Messages that did NOT mention Jarvis may be read only by
  `channels/ambient.py`: in process memory, bounded per room, short-lived, admitted by
  `permissions.ambient_ok` (owner and reply list by default), framed as untrusted text in the
  per-turn tail, never in a DM, and never written anywhere: no transcript, usage, people, spaces,
  log or exchange. The hook (`discord_gateway._keep_room_context`) runs AFTER the gate verdict
  and changes nothing about it: no reply, reaction or typing indicator. (6) The Discord adapter's
  new display data (author / thread names, reply-to, readable mentions) is never read by
  `decide()`, and reply-to comes only from the message discord.py already resolved (no fetch).
  (7) The Instagram export importer (`channels/instagram_import.py`, `channels-import-instagram`)
  and the room viewer (`channels/space_view.py`, `channels-spaces`, `channels-space`, the two
  GET routes) grant and change nothing. The importer writes ONLY group-chat transcript lines
  (3+ people, `imported: true`, no gate decision on them) and their `spaces.json` entries; a DM is
  never imported, it is a preview unless `--yes`, and it creates no person, permission, scope,
  conversation or usage line. The viewer is read-only: it writes, rotates and registers nothing,
  and a model-facing caller must use `view(..., rooms_only=True)`, which refuses a DM. Both are
  offline: no model, no network, no send. The one model-facing caller is the owner-only
  `recent_spaces` tool (`actions/recent_spaces.py`): rooms only, never a DM; refused for a guest
  and for a scheduled run; listed in `tools.OWNER_ONLY_TOOLS`; no id or path argument; what it
  returns is other people's words (marked as such, one line each, turned-away lines left out)
  and is never an instruction. The panel (S11) only adds controls on routes that already existed
  and renders room text with `textContent` (never markup); anything that WIDENS what is said or
  read in a room (group recall on, one item set to "on", reading anyone) asks for a second click.
  (8) The UNOFFICIAL Instagram group transport (`channels/instagram_private.py`,
  `instagram-group-login`, `instagram-group-serve`, `instagram-group-threads`, plan S9c) is the ONLY
  file allowed to import a private-API client library (the optional, exactly-pinned `instagrapi`,
  extra `instagram-private`, imported inside `_default_client_factory()` and nowhere else), imports
  none at module level, and is imported by nothing except those three commands (`jarvis.channels`
  never loads it). `instagram-group-threads` is owner-run, prints thread ids / titles / handles and
  never message text, and refuses after a login check. The adapter maps every login-style error to a
  STOP and refuses to save a session that contains the password; it never relogs in by itself. It is OFF by default (`group_transport`, Instagram
  only, the literal `true`), re-reads that switch every cycle and drops a reply in flight when it is off.
  It polls ONLY `pt-<id>` entries of `allowed_channels` (an empty list polls nothing, unlike the gate),
  starts from "now", and feeds the same `base.gate` / `base.handle_message` as every message: there is
  no second permission path. Its ids (`pt-` threads, `pk-` people) are never merged with the webhook's,
  and it never writes the handle -> id directory. On any login check it stops, tells the owner once and
  writes a halt marker; it never retries by itself. The password is asked only at the terminal prompt,
  never stored, logged, put in `channels.json` or sent through the web UI; only the client's session is
  kept (`~/.jarvis/instagram_private_session.json`, on the sensitive-path list). The panel only flips the
  switch, behind a second click that states the risk.
  `tests/test_instagram_import.py`, `tests/test_space_view.py`, `tests/test_recent_spaces.py` and
  `tests/verify_channels_rooms.js` pin it.
  `tests/test_channel_spaces.py`, `test_channel_group_history.py`, `test_group_recall_toggles.py`,
  `test_channel_ambient.py`, `test_channel_save_switch.py` and `test_discord_enrichment.py` pin
  the rest of this.
- **A time limit on tool access is a ceiling, enforced at read time, and it only
  ever means less access (L.36-P6).** The grant is the ordinary `tool_allowlist`
  entry; `user_perms.tools_until` only says when it ends.
  `user_perms.resolve_tool_access` answers "no tools" once the deadline has passed
  and WRITES NOTHING (the Test tab calls it too); `user_admin.expire_due` is the only
  thing that removes the person from the list and it is never called from a dry run.
  Order of writes: deadline first, list entry second when granting; list entry first,
  deadline second when ending. An unreadable deadline counts as passed. Turning
  `tool` on or off, blocking, forgetting, or `channels-allow|deny ... tool` ends the
  countdown. `tests/test_channel_timed_and_instruction.py` pins this.
- **Pictures, the platform tools switch, panel DMs and the turned-away list
  (L.36-P8 / P9 / P13 / P14).** (1) `image_allowlist` is a fourth list but NOT a
  stage of the gate: `permissions.may_send_images()` only ever ANDs with an
  allowed decision, so a picture grant can never open a conversation;
  `channels/media_in.py` is its only reader (Discord and Instagram) and the panel says so. Every
  place that walks "all of someone's lists" uses `user_admin.LISTS`, so block,
  forget, handle edits and listing cannot forget it. It is not a bulk flag or
  part of any quick setup. (2) `channels/master_tools.py` is the only way the
  panel touches `allow_tools`: turning it ON previews first and needs
  `confirm`, is refused while the tool list holds `"*"`, and OFF applies at
  once. (3) `channels/panel_dm.py` delivers through `actions/send_dm.tool_send_dm`
  itself (limits, resolver, log and delivery are the tool's, not copies), is
  preview-then-confirm, and refuses the owner, a placeholder, anyone blocked or
  with DMs off, and any call made from inside a chat or an unattended run. (4)
  `channels/denied.py` is read-only, covers only the `dm_allowed` / `reply`
  stages, and NEVER returns what a stranger wrote. No model tool reaches any of
  these four (a tool that did would let a guest's message widen access or send
  as the owner). `tests/test_channel_pack_p8_p9_p13_p14.py` and
  `tests/verify_l36_pack_ui.py` pin this.
- **The owner's instruction for a person is style text, owner-typed only
  (L.36-P12).** `people.instruction` is set by `user_admin.set_instruction` (panel /
  `channels-instruction`) and by nothing a chat guest can reach (`remember_sender`
  never touches it); one line, capped, framed in `people.prompt_block` as the
  owner's note on style that changes nothing about what the person may do. Access is
  still decided in code. It is not copied across a link and never goes in `memory.py`.
- **The permission change log is written by the writers, never by callers
  (L.36-P15).** `channels/changelog.py` is hooked into `config.add_to_set /
  remove_from_set / set_value`, `user_perms.set_tools / set_can_dm` and
  `people.set_follow`; a new way to change access must go through one of them (or
  call `changelog.record` itself), not around them. A line is written only after the
  change was saved and only if something changed. It never raises, and it holds names
  and switches only -- never a message, a note, a token or any config value except the
  `owner` id and the `allow_tools` flag. `reason()` tags batch routes; the web panel
  marks itself with `JARVIS_CHANGE_SOURCE=panel` (server.js sets it for every
  `channels-*` command).
- **A handle edit moves access, it does not widen it (L.36-P10).**
  `user_admin.set_handle` works only for a hand-added person who has never written
  (`touch()` overwrites the handle on every real message) and never for the owner. The
  order is fixed: limits copied, new handle listed, record moved, old handle
  de-listed, old limits dropped -- so a failure part-way leaves the old entries, not
  an orphaned grant. Only the old HANDLE is removed from a list; a real id entry stays.
- **Quick setups and bulk edits are the single switches, written together
  (L.36-P4/P5).** `channels/preset_admin.py` changes nothing except by calling
  `user_admin.set_flag` and `user_admin.set_tools`; don't give it a second way
  to write an allow-list or `user_perms.json`. Presets live in `channels/presets.py`,
  fixed in code on purpose (a tool list is a permission; adding one is a reviewed
  change, not a file edit). A setup that turns tools on writes the tool LIST first
  and the `tool` switch second, and does not turn it on if the list failed -- never
  "every tool" by accident. It leaves the owner, `send_dm`, blocking and the
  platform's `allow_tools` alone, and is refused for the owner. A bulk edit may not
  set `owner`, and may turn `tool` OFF but not ON (a setup says which tools).
  Every refusal (blocked, covered by `"*"`, unreadable limits) is returned per
  person; never collapse it into one success. The panel previews a setup (a dry
  run that writes nothing) before Apply. `user_admin.list_flag_refusal` is the one
  place the blocked / wildcard rule lives, so a preview cannot drift from the switch.
- **MCP servers are added or changed only by a human.** The MCP panel's
  buttons, `jarvis mcp-edit` and a hand-edit of `mcp_config.json` are the only
  write paths (`mcp_client.save_server` / `set_server_flag` / `remove_server`);
  no model tool may reach them, MCP tools stay confirm-gated unless the owner
  marks a server Trusted, and secrets (env values, a URL's userinfo/query)
  never go back to the browser. `tests/test_mcp_edit.py` pins this.

- **Never touch `tool_safety.py`, confirmation prompts, `risk_review()`,
  or the AI-review gating inside `_make_tool_executor()`** while doing
  router/discovery/token-optimization work. It's a separate safety
  concern; changes there need their own explicit review, not a drive-by.
- **`MAX_TOOL_ROUNDS` in `ai_providers.py` stays at 5.** Never lower it to
  save tokens — optimize what's sent *per round* instead.
- `TOOLS`/`CORE_TOOL_SCHEMAS` stay fully loaded and locally executable at
  all times. Only what's **sent to the model** gets filtered by the
  router/discovery layer — never gate real tool execution behind it.
- Every provider's `tools_payload` is already rebuilt fresh each round.
  Don't reintroduce an ever-growing resent list.
- `jarvis` is a **brand-new OS process on every CLI invocation** (see
  `history.py`'s docstring). Nothing survives in memory between calls —
  persistence goes through `conversations.py` or `discovery_cache.py` on
  disk, never a module-level global.
- **The web UI may run a request in a warm `jarvis _internal_bridge` worker
  instead of a new process (`jarvis/bridge.py`, `web/bridge.js`); jarvis never
  depends on it.** Code to the rule above anyway -- a worker is a normal
  process that happens to live longer. Three things are yours to keep true:
  (1) anything a process used to do when it ended (close a browser, an MCP
  session, an app jarvis launched) goes in `bridge.Host._end_of_request`, or a
  warm worker will carry it into the next request; (2) anything read once at
  import (tool discovery, personas, MCP config) must be covered by
  `bridge.source_fingerprint`, which is how a worker notices it is stale and
  asks to be replaced -- do not make it ignore a file just because it changes
  often; (3) the CLI half must stay inert without a host: `wire.trace()` is a
  plain stderr print, `wire.media()` prints exactly the legacy `JARVIS_MEDIA` line, and
  `wire.reply()` a no-op unless `bridge.serve()` installed itself, and `cli.py` imports `bridge` only to serve `_internal_bridge`. The
  server side must keep the process path as the fallback (no idle worker, `refuse`,
  `retire`, a death before `started` all mean "start a process"); a request
  that has sent `started` is never re-run. `tests/test_bridge.py` and
  `tests/verify_bridge_client.js` pin this. `JARVIS_BRIDGE=0` turns it off.
- **Cards and markers for the front end go through `wire`, and nothing trusts a bare line.**
  The stdout markers (`JARVIS_STREAM`, `JARVIS_USAGE`, `JARVIS_CONFIRM_REQUEST`,
  `JARVIS_UI_REQUEST`) are printed with `wire.stream/usage/confirm/ui_request`, never by
  hand: a hosted worker only believes the tagged line, because the reply is printed on the
  same stdout and is model output -- and a process the server spawns without a worker is
  handed a one-time `JARVIS_WIRE_NONCE` that `wire.py` reads once and removes from the
  environment (never re-export it or log it), so the server believes only lines tagged with
  it. `relayBridgeEvent` must never pass a plain `out` event to `onStdoutLine`. Cards: screenshot, download, present_file, notification,
  ui, dev_agent and organize_json cards are announced with
  `wire.media(kind, *fields)` -- never a hand-written
  `print("JARVIS_MEDIA...", file=sys.stderr)` (a new producer, including a custom
  tool, uses the same call). Hosted, the line is tagged with a per-request nonce that
  only `bridge.Host` knows (`media_tag()`); keep it out of the environment, the
  event stream and every log, because anything a shell command or subprocess can
  learn it can use to forge a card. A line with no tag, a wrong tag or a malformed
  body must stay an ordinary stderr line. `server.js` sends the browser one
  `ask-media` message for both the bridge and the spawn fallback, behind the
  `shapeMediaEvent` whitelist (don't widen it to "whatever the child printed"). Fields
  carry no tab or newline. `tests/test_bridge.py` and `tests/verify_wire_media.js` pin
  this.
- **There is no debug/verbose flag anywhere in `cli.py`.** All stderr trace
  output (`on_attempt`, `on_tool_call`, `on_tool_result`, `on_route`) is
  always-on, unconditional `print(..., file=sys.stderr)`. Don't assume one
  exists and gate new trace output behind it — either add real output
  unconditionally, matching the existing convention, or introduce the flag
  itself explicitly (and update every existing trace call site to respect
  it, not just the new one) if you actually want gating.

- **A picture a tool makes reaches a chat only through `media_out` -> `channels/media_send.py`, and only to the owner who asked (L.42.3).**
  `jarvis/media_out.py` is a per-ask collector the gateway opens in `base._ask_jarvis`
  (`collect_media` = the sender is the owner AND the gateway can attach files);
  `take_screenshot`, `browser_screenshot` and `present_file` (non-folder) `offer()` their file and
  it is a no-op everywhere else (CLI, web UI). `media_send.prepare` is the one place that decides
  what may leave the PC: owner only, an extension allow-list (images; plus pdf/text via
  `present_file`), regular files only, a size cap (`max_upload_mb`, default 8; an oversized image is
  re-encoded when Pillow is present, otherwise reported), at most `MAX_FILES` per reply and an on-disk hourly limit per thread and overall (`media_sends.json`, like `send_dm`'s). The
  gateway's `send_file(path, name)` is bound to the thread the message came from; there is NO
  recipient parameter anywhere, so don't add one. Never open the collector for a guest, never
  accept a path from the sender or a fetched page, and don't widen the extension list without
  thinking about exfiltration. Instagram has no `send_file` yet, so nothing is attached there.
  `tests/test_channel_media_out.py` pins this.

- **A picture someone SENDS reaches the model only as a description, only after the gate said yes, and only if the sender is the owner or on `image_allowlist` (L.36-P8 / L.42.3 inbound).**
  The Discord gateway reports `message.attachments` as plain descriptors (`IncomingMessage.attachments`: url, name, type, size -- never bytes;
  `permissions.decide()` must never read them). `channels/media_in.prepare` (`prepare_text` is its text half) is the one reader and `base.handle_message` calls it
  AFTER the gate and the dedupe guard, so a picture can never open a conversation, be paid for twice, or be fetched for a sender who was refused.
  It asks `permissions.may_send_images` itself; a person without the grant is not read AND the model is told N pictures were not looked at.
  What it will fetch: https URLs on a platform's own CDN only (`ALLOWED_HOSTS` + `ALLOWED_HOST_SUFFIXES`: Discord's, and Meta's `*.cdninstagram.com` / `*.fbcdn.net` / `lookaside.fbsbx.com` for Instagram, matched on a dot boundary; redirects not followed; never an address from message text),
  image types whose first bytes really are an image, at most `MAX_IMAGES` per message, at most `max_image_mb` each (default 8, checked against the
  declared AND the received size), and for anyone but the owner `RATE_PER_PERSON` per hour on disk (`media_in.json`). The file lives in a private
  temp folder for one vision call and is deleted straight after (a COPY is kept with the conversation, see the next invariant). The model gets the vision model's DESCRIPTION wrapped in a note that says so and
  that text inside a picture is content, never an instruction -- keep that wording; the picture is an injection channel and its cost lands on the
  owner's vision provider. Don't widen the host list beyond those two platforms, don't fetch a URL a sender typed, don't put the picture in the conversation record as bytes,
  and don't let this open the OUTBOUND collector (`media_out`), which stays owner-only. An Instagram DM that is only a picture is no longer dropped
  (`instagram_gateway._attachment_descriptors`); a sticker / audio / reaction still is.
  `tests/test_channel_media_in.py` pins this.
- **A confirm-gated tool can run from chat only through `channels/approvals.py`, only for the OWNER, and every failure is a "no" (L.29 / L.42.1).**
  `base.handle_message` builds `on_confirm_request` only when the sender is the verified owner AND the gateway passed `ask_approval`; a guest's ask
  keeps `None`, so `run_shell` / `code_agent` / `edit_file` stay cancelled from a guest turn (the missing channel was the only guard on those, L.29
  note 1 -- never hand a guest one). Only the owner's id can answer (Discord: the INTERACTION's user; Instagram: the owner's DM, the whole message
  `y`/`yes`/`n`/`no`, only while a request is pending; any other owner message while one is pending DENIES it). 60 seconds, then denied; a gateway
  start drops that platform's pending rows (`approvals.cancel_pending`) and a press after that fails closed. The action shown is `ai_client`'s own
  `arguments` -- nothing is regenerated between prompt and run -- and an action that cannot be shown in full is denied, never asked. The pending store
  and the transcript hold the tool NAME and the outcome, never the arguments. Don't touch `tool_safety`, `risk_review` or the gate in
  `_make_tool_executor` to make this work; it only supplies the callback they already call. `send_dm`'s own limits are unchanged and still refuse
  unattended runs. `tests/test_chat_approvals.py` pins this.
- **The thinking TRACE in chat goes to the owner only (L.42.2).** `thinking.wants_trace` is true only for the owner and only while their thread is `on`
  (default off); a guest whose switch is `on` gets the higher LEVEL (L.44) but never the trace, because a trace can quote the memory block, other
  people's records and tool results (Q-L44h is still open). It is sent as separate message(s) after the reply, never written to the transcript as text,
  and nothing is sent when the level was `off`. `tests/test_chat_approvals.py` pins this.
- **A picture that came with a message is KEPT with the conversation so the owner can look at it -- and only the references travel (Ask panel pictures, Discord / Instagram pictures).**
  `jarvis/conv_images.py` is the only writer of `~/.jarvis/conv_images/` (`ci_<24 hex>.<png|jpg|gif|webp>`; the extension comes from the bytes' signature, never a
  sender's file name; `path_for` re-validates every id, so nothing outside that folder can be named). `media_in.prepare` / `prepare_files` keep a copy of every picture that was
  actually fetched and really is a picture (even if the description then fails) and return references `{id, name, size}`; `base.handle_message` / `cli._load_panel_pictures`
  pass them as `user_images` / `user_shown` to `ai_client.ask`, which hands them to `conversations.begin_exchange(images=, shown=)`. The exchange's `user` still holds the
  words PLUS the picture descriptions (that is what the model was asked and what later turns' history needs); `shown` is what was typed and is what a reload draws.
  The same two fields are written on the raw `user` event and carried by `raw_archive.rebuild_exchanges` -- the web UI loads from that replay, so leave it out and a reload
  loses the pictures. Redo finds the exchange by `user` OR `shown` and deletes its pictures; Delete and Clear delete them too (the raw log's ones included).
  Never put bytes, a path or a sender-given URL in a reference. The model never sees a picture, only the vision model's description wrapped as content.
  `web/server.js` serves them at `/api/conv-images/<id>` (strict id pattern, nosniff, sandbox).
- **Pictures attached in the Ask Jarvis panel use the same reader.** The browser sends base64 in the `ask` message (`images`); `web/ask-images.js` checks count (3), size (8 MB)
  and the real image signature and stages the files in a private temp folder with a manifest (`JARVIS_ASK_IMAGES_FILE`); `cli._load_panel_pictures` -> `media_in.prepare_files`
  describes and keeps them. The folder is removed when the child exits. A message that is only pictures is stored as `(sent a picture)`. The Tool Maker's embedded ask
  (`ask-embed.js`) does not send pictures. `tests/test_ask_images.py` + `tests/verify_ask_images.js` pin this.

- **Calendars: linking and managing a calendar is human-only; a model tool may only touch Jarvis's OWN events (L.54).**
  Every rule lives in `jarvis/actions/calendar_tools.py`; `admin()` (`jarvis calendar-admin`, called only by
  web/server.js `/api/calendar/*`) is what the Calendars panel uses. No model tool and no chat path may call
  `admin`, `link_feed`, `add_feed_unverified`, `set_settings`, `remove_feed`, `set_feed_enabled` or `check_feed`: a feed is a URL Jarvis fetches and reads
  on every calendar question, and the model is the part most exposed to text other people wrote
  (`tests/test_l54_calendar.py` greps for it). A linked (ICS) calendar is read-only: its events are never written,
  edited or deleted by anything. A feed's real address goes to the browser only as `mask_url()` and errors are run
  through `_scrub()`. **Every write loads `calendar.json` with `load_config_strict()`**, so a file that cannot be
  read is refused rather than overwritten with an empty calendar (`tests/test_l54_calendar_hardening.py`). Old
  Jarvis-calendar events (`keep_days`, default 60, set from the Calendars tab) are only removed when an event is
  added, and the add says how many went. **The 500-event limit is a refusal, never a drop**: `_save_new_event` is the
  one place a new event is stored (the panel and the chat tool `calendar_add_event` both use it) and at 500 live
  events it refuses, naming the number; `_prune_local` removes only events past their keep time and never drops
  one that is still within it (it used to cut to the newest 500 by list position, silently). **Every writer of
  `calendar.json` takes `_config_lock()`** (an `O_EXCL` lockfile, like `notifier.py`; the panel's link / enable /
  remove / event / settings writes, the chat tool and `calendar-add` via `add_feed_unverified`) around its whole
  load-change-save, so two processes cannot lose each other's change; a write that cannot get the lock refuses and
  changes nothing. Never nest a second lock on the same file; the cache has its own (`calendar_cache.json.lock`).
  A probe must leave nothing behind: `check_feed` fetches with `"_no_store": True` (a link that is only checked is
  never cached) and `link_feed` fills the cache only after the calendar is saved. `keep_days` and `week_start`
  (mon / sun / sat) are the only settings the panel writes (`set_settings`, `POST /api/calendar/settings`,
  `calendar-admin settings`); like the rest of `admin()` it is human-only and no model tool reaches it.

- **Settings > Advanced is owner-only and UI-only; every rule lives in `jarvis/settings_admin.py` (L.43).**
  The Settings modal (`web/public/settings.js`) reaches files and tunables only through
  `/api/settings/*` -> `jarvis settings-admin`. Keep it that way: no model tool, no slash passthrough and no
  chat path may call `settings_admin` or `tunables.set_override` / `reset_override` (`tests/test_l43_settings.py`
  greps for it), and `refuse_unattended()` must keep refusing writes when `JARVIS_CHANNEL`,
  `JARVIS_CHANNEL_SENDER`, `JARVIS_SCHEDULED`, `JARVIS_TASK_ID` or an unattended `JARVIS_CONTEXT` is set. Every
  write goes through the one gate (parse, keep value types, the file's own checks, `base_version` stale check,
  `confirm_guarded` for safety files, backup first, atomic write, change log with paths only); don't add a second
  write path around it, and don't make a state file or a file another panel owns (`scheduled.json`, `daemons.json`,
  `backlog.json`, `channels/*`) editable here -- that would bypass the owning panel's own rules (L.36's fail-closed
  permissions). **The one exception is the web console's own preference files in `web/data`** (addressed as
  `web-data/<name>.json`): `favorites.json`, `favorite-daemons.json` and `ask-console-filter.json` are editable
  because `settings_admin.py` repeats the web server's own rules for each (the daemon-id pattern, the 500 cap, the
  console kinds) and `tests/test_l43_web_data.py` pins those copies against `web/server.js`. `reboot.json` and any
  other file in that folder stay read-only; a new `web/data` file is read-only until it is added to
  `_WEB_DATA_FILES` together with its rules. The web server tells the CLI where the folder is on every settings
  call (`JARVIS_WEB_DATA`, absolute paths only). Secrets are masked on read and restored on write; a token reaches the browser only on an explicit
  reveal, and is forgotten when Settings closes. A knob becomes a tunable only by adding it to
  `tunables.REGISTRY` (type, range, default, `applies`) and reading it through `tunables.const()` / `get()`.
  **`MAX_TOOL_ROUNDS`, the confirmation prompts, `tool_safety` behaviour and the risk review are not tunables and must
  not become ones.** The `/api/settings/*` routes require a loopback Host, a same-origin `Origin` when present and the
  `X-Jarvis-Settings: 1` header; don't weaken that, and don't point the page at `/api/config/*` for new work.

- **Minecraft mod sources (the `minecraft` tool group: `search_mc_mods`, `get_mc_project`, `download_mc_file`,
  `make_modpack_file`, `list_optifine_versions`, `download_optifine`; code in `jarvis/modhosts/` and
  `actions/minecraft_*.py`).** Nothing here is ever "installed": no tool name says install, and a result says
  "queued", "written" or "downloaded, not installed". `tests/test_minecraft_guards.py` pins everything below;
  `tests/test_minecraft_integration.py` runs the real adapters and the real `net.py` over a fake transport.
  - *Owner only, asked first.* Every tool of the group is in `tools.OWNER_ONLY_TOOLS`, AND each handler refuses a chat
    guest itself before any network call (an unreadable sender counts as a guest). The three that write a file,
    `download_mc_file`, `make_modpack_file` and `download_optifine`, are confirm-gated by name
    (`TOOL_CONFIRM_REQUIRED`). A new tool of the group joins both lists; the guard test loops over the whole group and
    does not pin "exactly six". In jarvis-main_181_ `download_mc_file` and `make_modpack_file` are loaded tools that
    are not yet in `OWNER_ONLY_TOOLS` (only their handler check protects them), so the guard test's owner-only checks
    for those two names fail until they are added together with their entry in `tests/test_l28b_followups.py`; delete
    this sentence when that is done.
  - *One door.* Only `modhosts/net.py` opens a connection (it holds the one `requests` import). It allows https only,
    follows redirects by hand, checks every hop against the host lists in `modhosts/hosts.py` and against the SSRF guard
    (`netguard`), and caps size and time. The host lists are constants: never read from config or the environment, so a
    hand-edit cannot send the key elsewhere. A new vendor host is a deliberate edit of `hosts.py` (its blocks are kept
    12+ lines apart so separate patches do not collide).
  - *The CurseForge key.* It lives in `~/.jarvis/curseforge.json` as `{"api_key": ...}` (or the environment variable
    `CURSEFORGE_API_KEY`) and is read ONLY by `modhosts/config.py`. `config.get_key()` has exactly four callers:
    `net.py` (puts it in the `x-api-key` header for `api.curseforge.com` only, and drops a key header a caller supplies),
    `cache.py` (refuses to cache anything that holds it), `sources.py` (a yes/no "is one set") and `curseforge.py`
    (scrubs it from messages, says "no key" before any request). It is never in a result, a Downloads row, the cache or
    a log; the Settings gate masks it on read. The guard test keeps allow-lists of the files that may name
    `curseforge.json` or call `get_key()`; a new reader or namer is a decision to make on purpose (add it to the list
    and say why here), or use `config.py`. The environment variable is readable by a shell command the model runs, like
    other env keys, so the file is the safer home.
  - *Stranger text.* Project names, summaries and file names come from the mod sites. They are markup-stripped, clipped
    and the result is marked with `untrusted.mark()` (`untrusted_note`, plus `text_flagged` when a line reads like an
    instruction to an AI). OptiFine results carry no such note because only file names that match a strict pattern are
    accepted there: there is no free text.
  - *Downloads.* `download_mc_file` queues through `download_hub._queue()` with `item_options = {filename, verify}`:
    Modrinth verifies with sha512 (else sha1), CurseForge with sha1 (never its fingerprint, never md5). The hub honours
    `verify` only on the `http` engine and says so in the add result's `note`. **A caller that needs integrity must read
    that `note` before it says "checked": if the note says `verify` was ignored, the file was not checked** (the tool
    reports `verify.applied` and says "not verified" in words). A file whose author blocked distribution (no link) is
    refused and never worked around: the `download-url` route is not asked. `download_optifine` does NOT use the hub: it
    downloads inside the call (optifine.net publishes no checksum, so it checks the jar's shape), writes
    `<name>.part` exclusively and renames it, never overwrites, and asks its own "Save outside the downloads library?"
    question; no row appears in Downloads.
  - *Packs.* `make_modpack_file` writes ONE `.mrpack` or CurseForge `.zip` per call, from one platform's own lookups; an
    entry that is not found, ambiguous, blocked or without a compatible file is reported and skipped, never swapped.
    `loader_version` given is used exactly; omitted, `modhosts/loaders.py` finds the latest (the loader-meta hosts) and the
    answer says which; with neither the pack is refused. A folder packed into `overrides/` is read without following
    links and is refused if it is, or contains, `~/.jarvis`. Quilt may use a Fabric-tagged file only when it is labelled
    `via`. Files are written by `modhosts/outdir.py` (temp `.part`, then rename).
  - *Settings and routing.* `web/server.js` `SETTINGS_NAME_RE` accepts one folder level: `channels/`, `web-data/` or
    `modhosts/`; `settings_admin._split_name` is the real gate, and `modhosts/` reaches only the files `_FILES` names
    (today `modhosts/cache.json`, read-only). `curseforge.json` is a top-level Settings file with a validator and a
    masked key. The group has ONE `TOOL_PACK_INSTRUCTION` (in `minecraft_lookup.py`; the OptiFine sentence is its last
    sentence) and ONE `TOOL_SETTINGS` list (`minecraft_lookup.py`, nine settings); the other three action files declare
    neither. No routing phrase is shared between two tools of the group, and none weighs less than 5.

## Testing

No framework dependency — plain `assert` throughout (also valid as
pytest functions if pytest happens to be available). Each `test_*.py` is a
standalone script: it resolves its own imports off `Path(__file__)`, so it
runs correctly from any `cwd`, and it exits 0 on success / non-zero on
failure — no test runner was strictly required to run one file at a time.

**Running more than one file at a time used to mean copy-pasting a
hand-maintained list here — that list silently drifted (it named under a
quarter of the ~80 files actually in `tests/`) and nobody had to notice,
because nothing ever cross-checked it against the directory. Use
`tests/run_tests.py` instead — it discovers every `test_*.py` in `tests/`
by globbing the directory itself, so there is no list to fall out of
sync:**

    python3 tests/run_tests.py                     # run everything, one line per file
    python3 tests/run_tests.py --list               # see what it discovered, run nothing
    python3 tests/run_tests.py clipboard             # run only tests matching a name filter
    python3 tests/run_tests.py clipboard build_info   # multiple filters = OR
    python3 tests/run_tests.py -d clipboard_watch     # --detailed: full output, not just the summary line
    python3 tests/run_tests.py --fail-fast            # stop at the first failing file
    python3 tests/run_tests.py --jobs 8               # run in parallel (output stays in discovery order)
    python3 tests/run_tests.py --timeout 300          # override the per-test timeout (default 180s)

A filter matches by substring against the file's name with `test_`/`.py`
stripped, case-insensitive — `clipboard`, `clipboard_watch`, and
`test_clipboard_watch.py` all work. Exit code is 0 only if every selected
test passed, 1 if any failed, 2 if a filter matched nothing. Each test
runs as its own subprocess (`stdin` closed, so nothing can silently hang
waiting on input) with its own timeout, so one crashing or hanging file
can't take the rest of the run down with it. For a FAILING file the
summary shows the test's own `FAILED` lines, the final exception line and
its `N passed` count -- not the last line of output, which was often an
unrelated stderr notice (e.g. `[tools] Auto-discovered ...`) that hid the
real reason.

**Read the default timeout's own `--help` text before assuming a file is
hung** — several tests now involve real, unmocked sleeps.
`key_health.pace_key()` (K.3.6, `jarvis-cli/jarvis/key_health.py`) puts a
real `time.sleep()` floor (`DEFAULT_MIN_ROUND_INTERVAL = 3.0`s) between
two requests on the same key, called unconditionally from every one of
`ai_providers.py`'s five adapters via `_pace_round()`. `test_key_health.py`
mocks `time.sleep` for its own direct tests of `pace_key`/`_pace_round`,
but most other test files that exercise multi-round adapter scenarios
(`test_finish_signal.py`, `test_streaming_other_adapters.py`, etc.) don't
mock `key_health` at all — they now pay the real 3-second floor on every
same-key round, which is why `test_finish_signal.py` takes ~140s and
`test_streaming_other_adapters.py` takes ~65s where they used to be near-
instant. Those same unmocked tests also read/write the real
`~/.jarvis/key_health.json` on whatever machine runs them, rather than an
isolated temp copy — worth fixing (give the whole suite a shared fixture
that redirects `key_health.HEALTH_FILE`, the way `test_key_health.py`'s
own `_reset()` does just for itself) before this gets worse as more
adapter-loop tests are added. Set `min_round_interval_seconds` to `0` on a
provider block, or pass `--timeout` generously, in the meantime.

Front-end logic that's pure enough to run outside a browser gets a plain
Node script instead, same no-framework convention (the first two slice the real
function straight out of `web/public/app.js` by source range, so it can't
silently drift out of sync with a copy; `verify_daemons_panel.js` instead
loads all of `web/public/daemons.js` into a bare `window` with `vm` and calls
its exposed `JarvisDaemons._pure` helpers (`verify_daemons_console.js` does the same for the console
line classifier). `npm install marked` inside
`tests/` first for `verify_math_rendering.js`, the one that needs it to
actually render Markdown):

    node tests/verify_math_rendering.js
    node tests/verify_hub_panel.js                # Download Hub panel: pure helpers + "never builds markup from data" greps
    node tests/verify_hub_settings.js             # Download Hub Settings > Downloads: the section built against a fake ui; the Steam update-size row, its confirm-gated dl_provider_prefs write, decline and hub-error paths
    node tests/verify_path_picker.js              # Download Hub N14: the folder / file picker's pure helpers + "text only, no browser dialogs" greps
    node tests/verify_hub_browse_routes.js        # Download Hub N14: the two picker routes' guards (loopback Host, X-Jarvis-Hub, Origin) and payload clamps, cut from the real web/server.js
    node tests/verify_thread_extras_collapse.js   # BUG-5/6: answered confirmations are one line, one card per dev_agent job, cards before the reply on reload, saved thinking follows the toggle; no npm install
    node tests/verify_ask_trace_replay.js   # also: the Raw/Summary console views (buildAskTraceViews) and the saved-as-drawn card merge (mergeDisplayWithExtras)
    node tests/verify_saved_as_shown_routes.js   # POST /api/console/:id/trace and POST /api/conversations/:id/display (validation, stdin payload, size caps)
    node tests/verify_ask_console_filter.js   # E.5/K.2.5.3: Ask Jarvis Console filter classifier (tool calls / token cost / [tools] ...), markup and CSS wiring; no npm install (its Python twin test_ask_console_filter.py checks app.js/server.js/style.css category lists agree)
    node tests/verify_daemons_panel.js
    node tests/verify_daemons_console.js   # the console line classifier (H.1.7)
    node tests/verify_log_conversation.js  # L.2: Logs Conversation view grouping (turns, tool pairing, failover, side requests, filters) + replays tests/fixtures/*.jsonl; no npm install
    node tests/verify_l9_sequence_bar.js   # L.9: sequence bar reachable; needs `playwright` + Chromium, prints SKIP without them
    python3 tests/verify_l11_daemon_categories.py # L.11: daemon categories in a real browser; needs `playwright` (Python) + Chromium, prints SKIP without them
    python3 tests/verify_l13_favorite_daemons.py  # L.13: favorite daemons in a real browser; needs `playwright` (Python) + Chromium, prints SKIP without them
    node tests/verify_slash_palette.js      # no npm install; runs the real palette
    python3 tests/test_tool_commands.py     # I.2.11: TOOL_COMMANDS contract, loader, follow-the-tool rule, `jarvis tool-commands`
    node tests/verify_tool_commands_palette.js  # I.2.11: tool commands in the palette (list, built-in wins, fail-closed tier, prepare -> preview -> run); no npm install
    node tests/verify_tool_commands_route.js    # I.2.11: /api/tool-commands routes' guards against the real server.js source; no npm install
    python3 tests/test_category_manage.py   # L.12: rename / merge / delete a category as a whole (daemons: registry + CLI + routes pinned; commands share the JS rules)
    node tests/verify_category_manager.js   # L.12: category-manager.js pure helpers + the whole dialog chain against a fake JarvisUI/fetch; no npm install
    python3 tests/test_l27c_autostart_notice.py  # L.27 follow-up: a failed daemon autostart raises ONE `daemon` notification per pass (text, cap, never raises, real inbox record, Settings row)
    python3 tests/test_e2_toast_position.py  # E2: toast position (8 places, default bottom-right), native OS notification renamed `desktop` (`toast` still read as `desktop`)
    python3 tests/test_notify_registry.py   # E0: notify_registry tables (kinds, actions, levels, positions, looks, tones) agree with notifier.py
    python3 tests/test_no_hardcoded_notify_lists.py  # L.30: static -- no page / panel / server keeps its own list of levels, notification types or the 1-5 range (reads them from the registry)
    python3 tests/test_notification_test_tool.py  # L.30: the `notification_test` tool and its Menu panel (goes through notify() only, preview == record, no phone channel, lists read from the registry)
    python3 tests/test_e4_notify_owner_record.py  # E4: the `notify_owner` tool writes ONE silent `owner_dm` inbox record per call (failed only if nobody was reached); result dict unchanged; never re-DMs
    python3 tests/test_one_notification_system.py  # E5: static ratchet -- no second browser Notification / toast host / toast() helper / native confirm / OS toast / owner DM / unregistered kind
    python3 tests/test_hub_universal_notifications.py  # Download Hub on the one notification system: `download` kind, notify-send --kind/--source/--failed, POST /api/notifications/send, host.notify, hub panel has no toast of its own
    python3 tests/test_hub_steam_alive.py  # Download Hub N5: a running Steam download is never "stalled" from the manifest alone; second signals (downloading folder, Steam's disk writes via psutil), the 60 s rule, replay of the real 2026-10-21 run (tests/fixtures/steam_n5_real_run.jsonl; tests/fixtures is git-ignored, so a shared copy may not have it)
    python3 tests/test_l43_tree.py          # L.43: "changed from default" report + per-value reset (same gate as every Settings write)
    node tests/verify_settings_tree.js      # L.43: settings.js tree helpers, deep-link parser and wiring; no npm install
    python3 tests/verify_l36_insights_ui.py # L.36-P1/P2/P3: Conversation / Usage / Test tabs in a real browser; needs `playwright` (Python) + Chromium, prints SKIP without them
    python3 tests/test_code_agent_extras.py # L.47b: code_agent runs are saved as replay extras, slimmed (no file content, no listing)
    node tests/verify_agent_panel.js        # L.47: the Focus Agent panel's reducer + tree builder (agent-panel.js); no npm install
    python3 tests/test_channel_timed_and_instruction.py   # L.36-P6 / P12: time-limited tools and the per-person instruction
    python3 tests/test_channel_pack_p8_p9_p13_p14.py      # L.36-P8 / P9 / P13 / P14: picture grant, platform tools switch, panel DM, turned-away list
    python3 tests/test_ask_images.py         # pictures in the Ask panel + kept with the conversation (store, Instagram, replay, redo / delete, CLI manifest)
    python3 tests/test_raw_console_and_display.py   # the Ask console lines and each turn's cards are saved AS SHOWN: raw trace lines (text, colour, order, full length), `console-append-trace`, `save_display` / `conv-save-display`, replay carries them
    node tests/verify_ask_images.js          # the panel's picture helpers, the server's checking / staging, and the wiring; no npm install
    python3 tests/test_chat_approvals.py     # L.29 / L.42.1 + L.42.2: the chat confirmation channel (owner-only, 60 s, restart, Instagram Y / N, Discord buttons) and the owner-only thinking trace
    node tests/verify_thinking_row.js        # L.44 panel row: the Thinking control in Permissions > Capabilities; no npm install
    python3 tests/test_channel_media_in.py   # L.36-P8 / L.42.3 inbound: pictures a person sends on Discord are read only for the owner and the picture list (host / size / count / hourly caps, description wrapped as content, never raises)
    python3 tests/verify_l53_tool_maker_ui.py # L.53: the Tool Maker's Ask Jarvis side panel in a real browser (toggle, one conversation per tab, New chat, Undo, tag); needs `playwright` (Python) + Chromium, prints SKIP without them
    python3 tests/test_allowed_guilds.py    # L.36-P17: allowed_guilds editor (preview-first, last entry never removable, real gate, CLI, route pinned statically)
    python3 tests/verify_l36_guilds_ui.py   # L.36-P17: Home tab (platform cards, 40 servers), status dropdown, allowed-servers editor in a real browser; needs `playwright` (Python) + Chromium, prints SKIP without them
    python3 tests/verify_l36_pack_ui.py     # L.36-P8/P9/P13/P14/P16: those panels + the add-person panel in a real browser; needs `playwright` (Python) + Chromium, prints SKIP without them
    node tests/verify_channels_panel.js     # L.36: the Channels panel's pure helpers (people list, filters, tool-scope diffing); no npm install
    node tests/verify_mcp_servers.js        # L.31: the MCP panel's pure helpers (state labels, search, filters, what a blank secret means); no npm install
    python3 tests/verify_mcp_servers_ui.py  # L.31: the MCP panel in a real browser against a real stdio MCP server; needs `playwright` (Python) + Chromium, prints SKIP without them
    python3 tests/test_tool_maker.py        # L.53: the Tool Maker conversation: withheld tools (hidden AND refused, fail closed), context, history trimming, conv-new origin
    python3 tests/test_bridge.py            # the direct channel's CLI half against a real `jarvis _internal_bridge` on a throwaway HOME: protocol, process reuse, env overlay, stdin, confirm round trip, typed reply / trace categories, refuse / retire before started, staleness fingerprint, CLI independence; no network, no API key
    python3 tests/test_skillunload_unknown.py  # I-B3: `jarvis skillunload <name>` errors (exit 1, "unloaded": false) for a skill that was not loaded in that scope; a loaded one still unloads; --all and usage unchanged
    python3 tests/test_c29b_desktop_deps.py  # C29b: desktop tools load without pygetwindow/pyautogui; doctor lists pygetwindow (Windows-only note); every desktop tool has a diagnosis dependency entry
    node tests/verify_bridge_client.js      # web/bridge.js against a fake worker: no blocking or queueing, reuse, fallbacks, crash-not-rerun, kill, breaker, recycling; no npm install
    node tests/verify_ask_embed.js          # L.53: ask-embed.js's reply/stream/size helpers and the Tool Manager's chat-fold + per-tool conversation key; no npm install
    node tests/verify_code_editor.js        # L.33: the Tool Manager editor's tokenizer, edit helpers and completions; no npm install
    python3 tests/test_tool_folders.py      # L.46.3: a tool's screen files - editor tabs' back end, import with folders / zip, uninstall that removes the folder
    python3 tests/test_tool_import_multi.py # import: overwrite choice, several tools per bundle, screen text in drafts, screen scripts in the plan, CLI flags
    node tests/verify_tool_folders_choices.js # the Tool Manager's pure helpers for those + "Commands from this file" + the draft key; no npm install
    python3 tests/verify_tool_folders_tabs_ui.py # the screen-file tabs and their server-side backup in a real browser; needs `playwright` (Python) + Chromium, prints SKIP without them
    node tests/verify_tool_folders_ui.js    # L.46.3: the Tool Manager's pure helpers for those (tab labels, dirty tracking, import kind, Delete dialog) + the text editor's Enter; no npm install
    python3 tests/verify_l33_code_editor.py # L.33: the editor in a real browser (keys, undo, ghost suggestions, scroll sync); needs `playwright` (Python) + Chromium, prints SKIP without them
                                            # against a fake DOM + fake JarvisHost
    node tests/verify_panel_open_guard.js   # I-B18(c): openScriptPanel() says so when a panel's script didn't load; no npm install
    python3 tests/verify_panel_open_guard.py  # same, in a real browser with daemons.js blocked; needs `playwright` (Python) + Chromium, prints SKIP without them
    python3 tests/test_minecraft_glue.py     # Minecraft wave-1 glue: OptiFine tools owner-only, modhosts/ settings name, pack-instruction sentence, split_dest refusal wording
    python3 tests/test_minecraft_guards.py   # the Minecraft group's cross-file promises: owner-only, confirm-gated, no "install" in a name, no shared routing phrase, one network door, key allow-lists, key stays in its file, stranger text marked (the owner-only checks for download_mc_file / make_modpack_file fail until tools.py lists them)
    python3 tests/test_minecraft_integration.py  # real adapters + real net.py over a fake transport: CurseForge download, blocked file, Modrinth download then pack (same checksum), CurseForge pack, dry runs; inline data, no tests/data files
    python3 tests/test_modhosts_net.py       # modhosts/net.py, hosts.py, cache.py: allow-lists, redirects, netguard, key header, scrubbing
    python3 tests/test_modhosts_config.py    # the CurseForge key: config.py, Settings entries, policy, doctor, settings.js
    python3 tests/test_modhosts_modrinth.py  # Modrinth adapter + untrusted.py against the in-process fake (needs hand_*.json fixtures)
    python3 tests/test_modhosts_curseforge.py  # CurseForge adapter against a fake fetch (needs hand_p3_*.json fixtures)
    python3 tests/test_modhosts_resolve.py   # resolve.py: file choice, Quilt fallback, never-guess rule, dependencies
    python3 tests/test_modhosts_loaders.py   # loaders.py: latest loader version per loader (needs hand_loader_*.json fixtures)
    python3 tests/test_modhosts_packs.py     # packs.py / outdir.py: golden .mrpack index and CurseForge manifest (needs golden_*.json fixtures)
    python3 tests/test_optifine.py           # OptiFine parser, build choice, hop-by-hop link, jar check and both tools
    python3 tests/test_minecraft_lookup_tools.py  # search_mc_mods / get_mc_project end to end (needs hand_*.json fixtures)
    python3 tests/test_minecraft_download.py # download_mc_file against fake adapters and the real hub store (needs hand_p4_catalog.json)
    python3 tests/test_minecraft_packs_tool.py   # make_modpack_file end to end

Two things that will waste your time if nobody tells you:

- **Redirect `HOME` to a temp dir BEFORE importing any jarvis module** in a
  test that touches `~/.jarvis`. Most modules resolve `Path.home()` at
  import time, so setting it afterwards has no effect and your test will
  read and write the developer's real store.
  `tests/test_workspace.py` shows the pattern.
- **Define test functions above the runner block at the bottom of the
  file.** The runner reads `globals()` when it executes, so a test appended
  after it is silently never run — and "N passed" still prints, which is
  how you don't notice.

Any new test file's `sys.path` setup must point at `jarvis-cli/`
(`Path(__file__).resolve().parent.parent / "jarvis-cli"`), or imports
fail with `ModuleNotFoundError: No module named 'jarvis'`.

Order of operations for a change:
1. `python3 -m compileall jarvis/<changed_files>.py -q` — catches syntax
   errors immediately.
2. A short inline snippet against the **exact repro** that prompted the
   change (import the module, call the changed function directly) — no
   API keys or live model needed.
3. A quick regression check with 1–2 adjacent phrasings/cases, so the fix
   doesn't overcorrect onto something that should still work.
4. Once a change has real regression risk (not a one-off repro), add a
   permanent test to `tests/test_enhancements.py` (or a new
   `tests/test_<topic>.py`, same no-dependency pattern) instead of
   leaving it as a throwaway snippet.
5. If `tests/interactive_inspector.py` exists, use it for a human-
   readable "what would actually happen for this message" view instead
   of hand-reconstructing the router/schema logic:
       python3 tests/interactive_inspector.py              # REPL
       python3 tests/interactive_inspector.py "a message"  # one-shot
       python3 tests/interactive_inspector.py --examples    # batch

**If you change the router's scoring/trim algorithm, or `ai_client.ask()`'s
active-schema construction, update the mirrored copy of that logic inside
`tests/interactive_inspector.py` in the same change.** It cross-checks
its mirror against the real code and prints `MIRROR DRIFT` on disagreement
— that's a safety net for catching a missed update, not a substitute for
making one. Run the `--examples` batch afterward and confirm no drift
warnings. Note: `tool_router.RouteResult` now carries a real `.matches`
field (list of `(group, tool_name, matched_phrase)` triples, populated by
`route()` itself) — the inspector's `_explain_keyword_matches()` still
re-derives this by mirroring rather than reading `route.matches` directly,
so there are technically two sources of per-phrase match detail now. Worth
collapsing to one (have the inspector just read `route.matches`) next time
you're touching that file, but it hasn't been done yet — don't assume
they've been unified just because `route.matches` exists.

## The Tool Maker conversation (L.53) — it may only EDIT

The Tool Manager's **Ask Jarvis** side panel is a normal Jarvis conversation (same ask
path, providers, tools and saved conversation as the main panel, via `ask-embed.js` and
the same websocket `ask` message) with two additions, both in `jarvis/tool_maker.py`:

- **Context.** `server.js` writes `{name, source}` (the tool's name and the editor's
  whole text, unsaved edits included) to a private temp file `jarvis-tm-ctx-*.json` and
  points `JARVIS_TOOL_MAKER_CONTEXT_FILE` at it for that one `jarvis ask`; the file is
  deleted when the child exits. It is a file because a 120 000-char module does not fit
  an env value or argv on Windows. The block goes in the **tail** of the system prompt
  (`_system_prompt_parts(tool_maker_ctx=...)`), never the cached prefix, and long
  ` ```python ` blocks in past ASSISTANT turns are collapsed before they go back to the
  model (the conversation file and the raw log keep the full text).
- **It may only edit.** Validate executes a file's top level and Save loads it, so a
  model allowed to do either would run its own code unattended. While the variable is
  set, `tool_maker.WITHHELD_TOOLS` (write_file, edit_file, run_shell, run_command,
  run_custom_command, run_chain, create_command, update_command, git_run,
  git_commit_all, code_agent, dev_agent, spawn_subagent, run_subagents) are hidden from
  every schema list and search **and refused in `tools.execute_tool`, the owner's own
  run included**. It is enforced by name in code, not by prompt wording. The set is a
  deny-list on purpose (this conversation is otherwise a normal one): **when you add a
  tool that can write a file or run a program, add it to `WITHHELD_TOOLS`.**
  `tests/test_tool_maker.py` checks every listed name is a real tool.
- **Fail closed.** `active()` is "the variable is set", not "the file is readable": a
  missing, corrupt or oversize context file means *no context*, never *no restrictions*.
- Origin: the conversation is created with origin `tool-maker` and the tool's name as its
  detail (`POST /api/conversations` accepts only that origin from a browser; `conv-new
  --origin tool-maker`), is **not** made the "current" conversation, and the detail
  follows the tool's name on every turn (`stamp_conversation`). Logs shows a "Tool Maker" tag.
- One conversation per TOOL: the browser keeps `tool name -> conversation id`
  (`jarvis-tool-maker-chats` in localStorage); a new unsaved tab is keyed by its draft key
  and moves under the name on first save.
- `custom_tools_agent.py`, `jarvis ctools-agent` and `/api/ctools/:name/agent` (the L.43
  dock's back end) are no longer used by the UI. They are left in place until removed on
  purpose.

## Tool screens (TOOL_UI), drafts and the Tool Manager's other switches

A tool file can ship a screen of its own: a module-level `TOOL_UI` list (see
`jarvis/tool_ui.py`, `actions/_template.py` section 9) whose entries are a
**button** beside Menu (opens the tool's page in a sandboxed frame) or a **Menu
entry** (opens it in a shadow-root panel). The older `ui_bridge` way (a tool
raising a toast/card/question while it runs) is unchanged — do not fold one into
the other.

- **A page is its author's code, never the model's.** Nothing model-facing may
  create, edit or switch one — same rule as custom tools and daemons. The browser
  names an element **id**; the folder it maps to is decided by `jarvis tool-ui`,
  and every path (`path`, `html`, `js`, `css`) is resolved and must stay inside the
  declaring file's directory (`tool_ui._resolve_inside`). `server.js` must never
  build a path from request input.
- **`host.runTool` is restricted to the element's own file's tools** (checked in
  `jarvis tool-ui run`, not just in the browser) and keeps the Debug panel's
  confirmation gate (`/api/tools/preview` first).
- **The Tool Manager switches** live in `disabled.json` via `tool_disable.py`
  (`tools`, `commands`, `personas`, `ui`). Daemons are the exception: their switch
  IS `daemons.json`'s `enabled` flag (the Daemons panel edits the same one) — do not
  add a second copy. A new setter in `tool_disable.py` must stay out of anything
  the model can call; `tests/test_tool_disable.py` enforces that.
- **A file's tools all being off takes its daemons and screens with it** (Q-L46m).
  Screens are derived on every read (`tools.ui_is_off`, `ui_ids_off_with_tool`;
  nothing stored); daemons are applied on the CHANGE by `tool_daemons.sync` (marked
  `dormant` with `dormant_reason: "tool-off"`, remembered in `tool_off_seen`), so a
  choice the owner makes afterwards is not undone, and a running daemon is never
  stopped by it. Don't make `sync` a standing rule; `tests/test_q_l46m_tool_off_cascade.py`
  pins this. A whole MCP server is switched with the MCP panel's own route
  (`mcp-edit` -> `set_server_flag`), never a second write path to the MCP config.
- **A tool's screen files go through ONE rule: only what its own saved `TOOL_UI` resolves
  to** (`custom_tools_store.web_footprint`). The editor's extra tabs (`jarvis ctools-files
  list|read|write`), import with folders (`jarvis ctools-import`, several files / a folder /
  a .zip) and uninstall that removes the folder (`ctools-delete --folders`) all use it. The
  browser names a path; the CLI checks it is in that list and re-resolves it inside
  `~/.jarvis/tools` -- `server.js` never builds one. Import: a plan first (names and sizes,
  nothing run), then `--write` checks the .py exactly as a save does against a staging
  folder, keeps only files a declared screen uses and rolls back if any .py is refused;
  any `..` / absolute / drive path refuses the WHOLE bundle. A bundle may hold several
  top-level .py files (up to `IMPORT_MAX_TOOLS`): each is its own tool, all or none are
  written, and two declaring the same tool / screen id / palette command refuse the lot.
  Nothing is written over an existing tool or screen file unless `overwrite` is passed
  (`--overwrite`, `?overwrite=1`); then each replaced file keeps a `.bak` and a failed import
  restores it. A link or a folder at a screen file's path is never written through, overwrite
  or not. The plan carries the text of each `.js` / `.html` screen file so the review can
  make the person open it before Import turns on (a Menu panel's JavaScript runs in the page
  with Jarvis). Uninstall removes only served files (`.html .htm .js .css` and their `.bak`) and
  the folder if that empties it; a folder another tool names (quoted path in its source) or
  uses, a link, and a `_` folder are never touched. None of it has a model-facing entry
  point. `tests/test_tool_folders.py` pins this.
- **Drafts** (`custom_tools_store.py`, `~/.jarvis/tools/_drafts/`) are copies, never
  tools: not loaded, not listed as tool files. A refused save stashes its text there
  server-side so it is never only in a browser tab. A draft may also carry the tab's unsaved
  screen-file text (`web`, cleaned by `_clean_draft_web`): `save_draft(..., web=None)` keeps
  what it holds, `{}` clears it, and it is never written under the tools folder. Keep the
  draft key validated (`DRAFT_KEY_RE`) before it touches a path.
- A new template that ships a folder goes in `custom_tools_ui_templates.py`; the
  starter files are embedded as strings because the package has no `package-data`
  (html/js/css next to the .py would not reach an installed copy).

## Test Checklist — adding a tool means updating it

The web console has **Menu → Test Checklist**: every tool Jarvis can call, how
to test it (prompts for Ask, arguments for a Debug direct-run), what a pass
looks like, and a place to record what works and what doesn't. An entry has
**two possible homes**:

- **Shipped:** `web/public/test-checklist-data.js`, for every tool that ships
  with jarvis.
- **The tool's own module:** a module-level `TEST_CHECKLIST` dict (tool name ->
  entry, the same shape, `group` optional and defaulting to `TOOL_GROUP`) —
  one entry per tool, so a module with several tools gives each its own —
  plus `TEST_CHECKLIST_GROUP` if the module invents a brand-new `TOOL_GROUP`
  (or more than one — see below). This is the **only** option for a user's own
  tool in `~/.jarvis/tools/`, which can never be in a file that ships with the
  app. `actions/_template.py` section 8 documents it; every Tool Manager
  template carries an example. Discovery reads it, `jarvis tools-list` (GET
  `/api/tools`) carries it, and the panel merges it in. A tool that ships with
  jarvis may use this home too (K.2.9) — it is no longer custom-tool-only — but
  every shipped tool today keeps its entry in the data file. Use ONE home per
  tool — the coverage test and `tests/test_checklist_supplied.py` both fail if a
  tool has an entry in both.

What counts as a well-formed entry is defined once, in
`jarvis-cli/jarvis/checklist_schema.py`, and checked by the same code for both
homes. A malformed module-supplied entry is dropped and logged
(`[checklist] file.py: ...`) — the tool itself still loads — and the Custom
Tools editor's Check button reports it; the coverage test then fails for a
shipped module because the tool is left with no entry.

**Whenever you add, rename, remove or change the behaviour of a tool, you MUST
update that tool's entry — in `web/public/test-checklist-data.js`, or in its own
module's `TEST_CHECKLIST` if that is where its entry lives — in the same
change.** This is not optional and not a follow-up. A tool change without its
checklist entry is an incomplete change, the same as a tool with no schema.

For each tool the entry needs:

- `group` — one of the ids in the file's `"groups"` list (match the group in
  `tool_registry.TOOL_GROUPS`; add a group there too if you added one). In a
  module's own `TEST_CHECKLIST`, leave it out and it follows the module's
  `TOOL_GROUP` — or, if the module's tools split across more than one
  logical category, set it explicitly to one of its own declared group ids
  (see `TEST_CHECKLIST_GROUP`'s two shapes just below).
- `does` — one line on what it's for.
- `steps` — at least one, ideally two or three: an `{"ask": "...", "expect":
  "..."}` prompt to type into Ask, and/or a `{"run": {...args...}, "expect":
  "..."}` direct run from Debug (skips the model). Write `expect` as what a
  pass looks like, concretely. Use `<angle brackets>` for things the tester
  must fill in — the UI highlights them.
- `needs` / `os` / `care` / `watch` when they apply: prerequisites (accounts,
  installed programs, plugins), Windows-only, side effects worth warning about
  (writes files, sends messages, starts processes, changes system state), and
  known gotchas or invariants worth checking while testing.

`TEST_CHECKLIST_GROUP` has two shapes. The common one, `{"label": ...,
"blurb": ...}`, names one section — the panel's home for the module's whole
`TOOL_GROUP` — and is what most modules need (one module, one category). A
module whose tools genuinely split across more than one logical category can
instead give it `{group_id: {"label": ..., "blurb": ...}, ...}`, one entry
per section, and tag each `TEST_CHECKLIST` entry's own `group` with the id of
the section it belongs in (an id from that dict, or the module's `TOOL_GROUP`
itself, which is always a valid target whether or not it also has its own
meta there). The two shapes are told apart by their keys alone — a plain
`{"label", "blurb"}` dict is the single-section shape; anything else is read
as `{group_id: meta, ...}` — so a module never needs to say which one it's
using. `actions/_template.py` section 8 works through both shapes with a
worked two-tool, two-group example.

Renamed a tool? Rename its key. Removed one? Delete its entry. If you changed
what a tool does, fix the affected `does` / `steps` / `expect` text — editing a
step's text automatically un-ticks it in testers' browsers, which is what you
want.

Do NOT put test results (status, notes, ticks) in that file. Results live only
in the tester's browser (localStorage); nothing about the checklist is ever
written by the CLI or stored under `~/.jarvis`. The panel is purely front end.

What happens if an entry is missing from both homes: the tool still appears in
the menu (the panel reads the live catalogue from `/api/tools`), but only as a
bare name marked **NO CHECKLIST**, with no details, and the Overview's Coverage
section names it. `tests/test_checklist_coverage.py` fails on this too for any
tool that ships with jarvis, so you'll hear about it before the tester does.

The data file is strict JSON between its `JSON-BEGIN` / `JSON-END` markers
(double quotes, no trailing commas, no comments) because both the browser and
that test parse it. A module's `TEST_CHECKLIST` is ordinary Python, but its
values must be plain JSON types (it is sent to the browser as JSON).

**Features are not tools (master plan L.19).** The shipped data file also has a
top-level `"features"` object for things worth a manual pass that are not tools —
today the `/` command palette (group `command_palette`, whose entry in `"groups"`
has `"kind": "feature"`). Same entry shape and the same validator
(`checklist_schema.validate_entry(..., allow_do=True)`); the one difference is that
a step may be `{"do": "<what to do by hand in the page>", "expect": "..."}` besides
`ask` / `run`. `do` is accepted for features only — a tool's own `TEST_CHECKLIST`
never gets it. The panel lists them next to the tools, tagged **Feature**; the
tool-coverage tests ignore them, and `tests/test_checklist_features.py` checks them
on their own. Do not fake a tool to hold a feature entry. If you change the palette's
behaviour, update its entries there **and** `DOCUMENTATION/COMMAND_PALETTE_TESTING.md`
(the long manual pass the checklist entries point at rather than copy).

**A step can open a panel.** The shipped data file also has a top-level `"panels"`
object (panel id -> `{"label", "menu": <Menu item DOM id>}` or `{"label",
"settings": <section>, "tab": <Advanced tab>}`), and a step in a shipped entry may be
`{"open": "<panel id>", "expect": "..."}`: the checklist shows an **Open** button that
closes itself and opens that Menu panel or Settings section. Use it as the first step
of a feature entry that starts "Menu -> X". `open` is shipped-only (a tool module's own
`TEST_CHECKLIST` never gets it, and `checklist_schema.extract_supplied` drops it). If you
rename a Menu item's id in `index.html` or a Settings section / Advanced tab, update
`"panels"`; `tests/test_checklist_panels.py` fails until you do.

## The Debug panel's source filter (§3, L.18)

Menu → Debug lists every tool read-only. `tools.tools_list_payload()` (`jarvis
tools-list`, GET `/api/tools`) gives each item two source fields, kept apart on
purpose:

- `source` — the four coarse **origin buckets**: `builtin`, `auto` (shipped
  `jarvis/actions/*.py`), `user` (`~/.jarvis/tools`), `mcp`. The button row
  `#debug-source-filter`.
- `source_detail` — `{kind, module, label, path?, server?}`: the module / file /
  MCP server the tool is actually **loaded from** (`screenshot_tools.py`,
  `actions/notify_owner.py`, `~/.jarvis/tools/mine.py`, `MCP: github`). The second
  filter row `#debug-module-filter` (chips, or a select past 8 sources) is built
  from its `label` and AND-combines with the bucket and the search box. It is
  **derived** by `_tool_source_detail()` from where discovery found the tool —
  never declared by a tool, so there is nothing for a tool author to add.

A source is not a **group** (`tool_registry.TOOL_GROUPS`: how the router bundles
tools — two sources can share a group), not a name prefix, not a Test Checklist
section, not a safety flag. Do not derive one from another. An older CLI that does
not send `source_detail` just gets no second row. `tests/test_debug_source_detail.py`
pins the contract.

## The `/` command palette - adding a CLI subcommand or a Menu panel means updating its registry

The Ask box's `/` palette (`web/public/slash-commands-data.js` +
`slash-palette.js`) is a map of **every** name `jarvis-cli` reserves. When you add,
rename, or remove a subcommand:

1. Add/remove it in `jarvis-cli/jarvis/reserved_names.py` (already required).
2. Put the same name in `slash-commands-data.js` in exactly one place:
   - a verb's `covers` list (that verb is how a user reaches it), or
   - `passthrough` if it runs one-shot and headless - give it a `summary`, a
     `usage` string and a `riskTier`. This table is *also* the server allowlist
     for `POST /api/slash/run` (`server.js` reads this file), so adding a name
     here is what makes it runnable from a chat box, and nothing else is;
   - `notExposed`, with a one-line reason, **only for a technical reason**: it
     never exits (a supervisor), it needs a terminal or the microphone, it is an
     internal hook, or it is a syntax token. Destructive is not a technical reason:
     mark it `dangerous` in `passthrough` (owner decision D-I4 - flagged, not hidden).
   `tests/test_slash_coverage.py` fails if a reserved name is in none of the three,
   in two, or in the registry but not in `reserved_names.py`.
3. Tier honestly: `safe` (read-only or trivially reversible), `caution` (changes
   state but scoped and recoverable), `dangerous` (bulk-destructive, irreversible,
   or broad blast radius). `dangerous` passthrough commands are confirmed
   client-side before the request is made.
4. A new *verb* means a handler in `slash-palette.js`, a `riskTier` that matches
   what a typo costs, `confirm` if it is destructive or interrupts something
   running, `whileReplying: false` if it must not fire mid-reply, and a `preview`
   line. Keep "first Enter completes, second runs" for anything that is not a
   plain `safe`, non-Chat verb with nothing required to fill in
   (`runsOnFirstEnter()`).
5. Add the case to `tests/verify_slash_palette.js`, and the manual steps to
   `DOCUMENTATION/COMMAND_PALETTE_TESTING.md`.
6. Passthrough arguments are an argv array, never a shell string. Do not build a
   command line by string concatenation anywhere on this path.

Adding a **Menu panel** (a new `menu-item-*` in `index.html`) is the same rule:
give the verb that opens it a `menu` field naming that id, and an opener in
`JarvisHost.openPanel()` (bottom of `app.js`). `test_slash_coverage.py` fails until
every `menu-item-*` is claimed by exactly one verb.

The parser lives in one place. Do not add another `/`-prefix regex to the ask-form
submit handler in `app.js`: register a verb instead.

### Commands a tool contributes (`TOOL_COMMANDS`, I.2.11 / K.6.7)

A tool file can put commands in the palette itself (`jarvis/tool_commands.py`,
`actions/_template.py` section 12). They are NOT in the static registry above: they come
from `GET /api/tool-commands` and are kept by `slash-palette.js` (`TOOLCMDS`).

- **A command is a typed twin of one tool of its own file.** The tool is whatever the
  declaration names; typed words can only fill the declared `args`. `jarvis tool-commands
  prepare|run` builds the arguments (`tool_commands.build_arguments`) -- the browser never
  has its own copy of those rules and must not grow one. Words reach the CLI as ONE JSON
  array, never as separate argv items (a word starting `--` would read as a flag).
- **Fail closed.** No `riskTier`, or an unknown one, is `dangerous` -- in Python AND again
  in `cleanToolCmd`. The palette confirms a dangerous command, and any whose tool is
  `confirm_required` / `ai_review`, through `/api/tools/preview` on the real arguments; if
  that check itself fails, nothing runs.
- **The built-in always wins.** A name in `reserved_names.py` is dropped in Python; a name
  that is a verb, alias, passthrough or not-exposed word is dropped in `server.js` (which
  already reads the registry) and again in the palette. `server.js` lists nothing if it
  can't read the registry. Do not let a tool command shadow a built-in "because it was
  there first".
- **Two independent reasons it can be off** (`tools.command_off_reason`). (1) It follows its
  tool, derived live, stored nowhere: off when the tool is off, or when every tool of its
  file is (the Q-L46m rule). (2) The owner's own switch for the command, a Tool Manager row
  of kind "Tool command", stored in `~/.jarvis/disabled.json` under `"tool_commands"`
  (`tool_disable.set_tool_command_disabled`; `jarvis tool-disable-set <name> <true|false>
  --kind tool-command`). Neither overwrites the other: clearing the owner's switch on a
  command whose tool is off leaves it off, and switching the tool back on never clears the
  owner's. `tool_commands_payload` reports `switched_off` (the owner's) and
  `disabled_by_tool` (off only for the tool's sake; the Tool Manager locks that row's
  switch). Like every other switch in `disabled.json` it has no model-facing writer, and
  deleting the tool file drops its commands' entries (`custom_tools_store.delete_tool`).
  This is NOT the saved-command switch (`command-disable-set`, commands.json).
- **Nothing model-facing may create or edit one**, same as `TOOL_UI` and daemons: the author
  of the file is the person who installed it. `tests/test_tool_commands.py` fails if any
  other module names `TOOL_COMMANDS`.
- `tests/test_tool_commands.py`, `verify_tool_commands_palette.js` and
  `verify_tool_commands_route.js` pin all of this; `tests/test_tool_command_switch.py` and
  `verify_tool_manager_tcmd.js` pin the owner's switch and its Tool Manager row. `test_slash_coverage.py` still only
  covers the CLI's own names: `tool-commands` is claimed by the `ctools` verb's `covers`.

## Categories (L.11 daemons, L.14 commands)

A daemon's `categories` is a list of labels - not a kind, and not the `builtin` flag, which is also a
lock and stays separate. Empty means "Undefined" in the panel. The name rules live twice and must
agree: `jarvis-cli/jarvis/categories.py` and `web/public/category-input.js`;
`tests/test_categories.py` runs one corpus through both. Vocabularies are per surface (a daemon's
suggestions never come from commands). A name reaches the CLI as `--category NAME`, and the flag
parser reads any following `--...` token as a flag, so `server.js` refuses names starting with `--`
(`daemonCategoryArgs`). Writes are strict (refused with a reason, nothing saved); reads of a
hand-edited registry are lenient. L.14 should reuse both files rather than copy them.

**Renaming, merging and deleting a category as a whole** (L.12 follow-up) edits every item's list; a
category has no record of its own. The rule is `categories.rename_in_list` / `remove_from_list`, with the
JS twins `renameInList` / `removeFromList` in `category-input.js` (parity-tested in `test_categories.py`).
Renaming to a name already in use IS the merge (there is no separate merge operation). Daemons go through
`daemons.rename_category` / `delete_category` and the `daemon-category` verb; commands are edited in
`server.js` (`/api/command-categories/*`) with the shared JS rules. The two vocabularies stay separate, so
neither path may touch the other. The dialog is `public/category-manager.js` (one modal at a time, built
from `JarvisUI.choose/prompt/confirm`).

## The folder / file picker (N14) - rules for anything that calls it

- A page cannot learn a picked path, so `jarvis/hub_browse.py` lists folders (`hub-admin browse`) and `web/public/path-picker.js` draws them. Call `JarvisPathPicker.open({kind: "folder" | "file", start, title, extensions})`; it resolves the full path or `null`. Use `JarvisPathPicker.startFor(fieldText, libraryRoot)` for `start` and `JarvisPathPicker.insideRoot(libraryRoot, picked)` to turn a pick inside the library into a sub-folder.
- The picker only FILLS a text box. Every field keeps its box (people paste paths), and the hub still validates the path when the field is saved (library rules, the confirmation outside the library). Never skip that check because "the picker chose it".
- Listing is read-only and one folder deep: names and kinds, a size and date for files, never contents, no tree walking, no model tool. `browse` and `mkdir` go through `hub_admin` and both refuse an unattended process; the routes are POST with a loopback Host and the `X-Jarvis-Hub` header (`requireHubBrowse`). A new picker route copies that, it does not weaken it.
- File and folder names come off the disk: they reach the page through `textContent` only. `tests/verify_path_picker.js` greps for markup assignment and browser dialogs.

## Patch conventions

Changes ship as scoped `diff -u` patches, one concept per patch:

- Name each patch for what it does (`jarvis-<feature-or-fix>.patch`), not
  generically.
- Diff against the zip baseline you were handed — **unless** a file
  you're touching has already been patched in the working tree, in which
  case diff against the *current* tree state instead, or it won't apply
  (`patch`/`git apply` need matching context, not a description).
- Don't fold a new change into an existing delivered patch, and don't
  modify a delivered patch after the fact. If a later patch depends on an
  earlier one touching the same file, say so explicitly (which patch must
  be applied first).
- Before handing a patch over: verify it actually applies cleanly against
  its stated base, and that the full test suite still passes afterward.

## Style already in place

- Inline comments explain *why*, not just what — match that density.
- Router/discovery/schema-shaping changes must be backward compatible by
  default: a no-op for the common case, behavior change only for the
  specific edge case being targeted.
- Prefer additive, opt-in data-shape changes (e.g. a value that can be a
  plain type or a richer dict, unwrapped via `isinstance()`) over
  changing an existing field's meaning — so old entries never need mass
  editing.
