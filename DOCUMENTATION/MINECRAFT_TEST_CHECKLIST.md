# Minecraft hand-test checklist

One numbered list for you to click through. Built from the manual checks of every report (the lookup, hub-verify, CurseForge, OptiFine, wave-1 glue, download, pack, loader-version, update-check and panel reports) and the pack-matching steps in `actions/minecraft_packs.py`. Written 2026-10-27 against `jarvis-main_182_`. **Nothing on this list has been run by anyone**: the authors were told not to run tests, so every "expect" below is what the code and its tests say should happen, not something seen.

Format of every step: **Do** what to type or click. **Expect** what you should see. **If not** what it usually means (only causes the reports or the code support; otherwise "not known: write down what you saw").

Tick the box, and write the date and what you saw next to any step that does not match. The guide is `DOCUMENTATION/MINECRAFT_GUIDE.md`; its section 8 says what has never been tried.

**Words used:** *Ask* means type it to Jarvis in the Ask box. *Run* means the Debug panel (Menu > Debug), tool name plus the arguments shown. *A test folder* means an empty folder you made, such as `D:/McTest`. Replace `<...>`.

---

## 0. Before you start

- [ ] **0.1 Do:** from the repo root run the tests in section 10 of the guide (or the ones for the parts you are about to try). **Expect:** each prints its passes and exits 0. **If not:** note which check failed; tests that read `tests/data/modhosts/` fixtures fail or print SKIP when the fixtures are missing (`tests/data` is git-ignored: they must be added with `git add -f`; the 182 zip has only `README.md` there).
- [ ] **0.2 Do:** `python3 tests/interactive_inspector.py --examples`. **Expect:** no drift warning for the Minecraft keywords. **If not:** a routing phrase moved or is shared; send the warning text.
- [ ] **0.3 Do:** restart the web server and hard-refresh the page. **Expect:** Menu now lists **Minecraft**. **If not:** the page is cached or an old build is installed; run `script.bat` and compare `jarvis version` with `jarvis-cli/jarvis/build_info.py`.
- [ ] **0.4 Do:** make a test folder `D:/McTest` (empty) and a second folder with 3 or 4 jars you downloaded from Modrinth (one an older version) and one jar from CurseForge: `D:/McMods`. **Expect:** nothing yet; you will use them below.
- [ ] **0.5 Do:** open Menu > Test Checklist. **Expect:** groups named **Minecraft: lookup**, **Minecraft: downloads**, **Minecraft: packs**, **Minecraft: OptiFine** and **Minecraft: updates**. **If not:** a tool file did not load; `jarvis tools-list` should name the seven tools.

---

## 1. The CurseForge key and Settings

- [ ] **1.1 Do:** Settings > Integrations > Minecraft mods, with no key. **Expect:** it says no key is set, with a link to console.curseforge.com; Contact and Default folder rows are there. **If not:** "Open Tool settings" appears instead of the two rows when the lookup tool file did not load.
- [ ] **1.2 Do:** `jarvis doctor minecraft`. **Expect:** a "CurseForge API key" line that skips with "no key set" (not a failure). **If not:** not known.
- [ ] **1.3 Do:** paste your own key in that page and save. **Expect:** the box clears at once; the page says a key is set and never shows it. **If not:** the file may be unreadable: `doctor` says so and how to fix it.
- [ ] **1.4 Do:** `jarvis doctor minecraft --deep`. **Expect:** the key is accepted (one small request). **If not:** a damaged or wrong key gets a warning; paste just the key again.
- [ ] **1.5 Do:** Settings > Tool settings, find the nine Minecraft settings. **Expect:** `contact` github.com/GoatAssim, `default_dest` ~/Downloads, six numbers at 0 (0 = no limit), `quilt_fabric_fallback` on. **If not:** a changed default means someone edited the setting.
- [ ] **1.6 Do:** Settings > Advanced > Files, open **Mod lookup cache** after any lookup in section 2. **Expect:** it opens read-only, with no "not a settings file name" error. **If not:** `web/server.js` `SETTINGS_NAME_RE` does not accept `modhosts/`.
- [ ] **1.7 Do:** search the page source and the browser's Network tab while saving the key. **Expect:** the key text appears nowhere you can read it back. **If not:** stop and report: that breaks a core rule.

---

## 2. Lookup: `search_mc_mods` and `get_mc_project`

- [ ] **2.1 Do:** Ask: `Search Modrinth for sodium for 1.20.1 on Fabric`. **Expect:** at most 5 hits, each with a slug and a `modrinth.com` link; nothing in Downloads. **If not:** if Jarvis answers without calling the tool, check step 0.2.
- [ ] **2.2 Do:** Ask: `Find a Fabric performance modpack for 1.20.1 on Modrinth`. **Expect:** five modpack hits (kind modpack) with page links; nothing downloaded.
- [ ] **2.3 Do:** Run `get_mc_project` with `{"project":"sodium","mc_version":"1.20.1","loader":"fabric"}`. **Expect:** release files only, for 1.20.1 on fabric, each with file id, channel, size, `downloadable`; "Fabric API" under the required dependencies; no HTML. **If not:** compare with the project page on modrinth.com.
- [ ] **2.4 Do:** the same with `"loader":"quilt"` on a Fabric-only mod. **Expect:** files carry `via: fabric` and a note says so. **If not:** check that `quilt_fabric_fallback` is on.
- [ ] **2.5 Do:** `{"project":"sodium","mc_version":"1.2.3","loader":"forge"}`. **Expect:** `no_compatible_file` with the closest alternatives listed.
- [ ] **2.6 Do:** `{"project":"an obviously made-up mod name xyz"}`. **Expect:** `not_found` (or `ambiguous` with candidates); never a guess.
- [ ] **2.7 Do:** Run `search_mc_mods` with `{"query":"sodium","source":"both"}` with **no** key. **Expect:** Modrinth hits plus a CurseForge reason `no_key` with the one sentence about getting a personal key. **If not:** `unsupported` instead of `no_key` means the CurseForge module did not load.
- [ ] **2.8 Do:** the same after you set the key (1.3). **Expect:** two separate lists, one per site, never one merged ranking.
- [ ] **2.9 Do:** Run `search_mc_mods` with `{"query":"sodium","mc_version":"latest"}`. **Expect:** `unsupported`, one sentence that it isn't a Minecraft version like 1.21.1 or 26.3.
- [ ] **2.10 Do:** Run `search_mc_mods` with `{"query":"sodium","source":"modrinth","limit":5,"offset":5}`, then without `offset`. **Expect:** different hits. **If not:** the `offset` parameter was never confirmed live; tell us.
- [ ] **2.11 Do:** the same search twice in a row, then open `~/.jarvis/modhosts/cache.json`. **Expect:** the file exists and the repeat is near-instant. **If not:** not known.
- [ ] **2.12 Do:** a CurseForge project that blocks downloads: `get_mc_project` `{"project":"448233","source":"curseforge","mc_version":"26.3","loader":"fabric"}`. **Expect:** files not downloadable (`downloadable: false`), nothing worked around. **If not:** not known.

---

## 3. Who may use the tools

- [ ] **3.1 Do:** from a non-owner Discord account, ask for a mod, for OptiFine, for a pack and for an update check. **Expect:** each is refused ("... is the owner's: someone in a chat can't use it"), nothing queued, nothing written; the tools are not offered to that account. **If not:** report which tool answered.
- [ ] **3.2 Do:** the same as yourself on the Jarvis page. **Expect:** all seven are offered and work.

---

## 4. The Download Hub `verify` option

`dl_add_http` has no `verify` argument, so this uses Python directly.

- [ ] **4.1 Do:** `python -c "from jarvis.hub import service; print(service.add_items(['<url of a small file you own>'], item_options={'verify': {'algo':'sha256','value':'<its real sha256>'}}))"`, then watch Menu > Downloads. **Expect:** a brief "checking the checksum", then Done.
- [ ] **4.2 Do:** the same with a wrong hash. **Expect:** Failed, "checksum mismatch: expected sha256 ..., got ...; the downloaded file was deleted"; no retry; no leftover `.part`.
- [ ] **4.3 Do:** the same with `size` far too small. **Expect:** Failed ("the server announced ... but ... was expected; refused"); nothing created.
- [ ] **4.4 Do:** `service.add_items(['https://example.com/a.jar'], dest='~', allow_absolute_dest=True)`, then with `'~/.jarvis/x'` and `'C:\Windows\Temp'`. **Expect:** nothing queued; the error names the home folder, OS folders and `~/.jarvis`.

---

## 5. `download_mc_file`

- [ ] **5.1 Do:** Run `{"project":"sodium"}`. **Expect:** it asks for the Minecraft version and loader; nothing is queued.
- [ ] **5.2 Do:** Run `{"project":"sodium","mc_version":"1.20.1","loader":"fabric","dest":"D:/McTest","dry_run":true}`. **Expect:** the exact file, `verify.algo: sha512`, `hub_check.ok: true`; no new Downloads row; no question.
- [ ] **5.3 Do:** Ask: `Download Sodium for 1.20.1 Fabric into D:/McTest`. **Expect:** one tool confirmation, then "Save outside the downloads library?" naming the folder, a Downloads row, `queued: true`, `installed: false`, the words "queued in Downloads; not installed". When the row finishes, the jar is in the folder and its checksum passed; Fabric API is listed under `requires`, not queued. **If not:** the question missing means the folder is inside the library.
- [ ] **5.4 Do:** repeat 5.3 with `with_dependencies: true`. **Expect:** one extra "Save outside..." question per queued dependency; `dependencies_queued`; any that could not be placed in `dependencies_skipped` with a reason. Declining one stops the rest.
- [ ] **5.5 Do:** point a throwaway row at a file with a different hash (4.2). **Expect:** the row fails with a checksum mismatch and the file is removed.
- [ ] **5.6 Do:** with your key: `{"project":"jei","source":"curseforge","mc_version":"1.20.1","loader":"forge","dest":"D:/McTest"}`. **Expect:** `queued: true`, `verify.algo: sha1`, a `cdn_note`. **Watch the Downloads row.** **If not:** a failure with HTTP 404 is the honest result (the probe's own CDN requests got 404, so this is the first real try): nothing is installed and no other site is tried. Write down which happened, success or 404.
- [ ] **5.7 Do:** `{"project":"448233","source":"curseforge","mc_version":"26.3","loader":"fabric"}`. **Expect:** `not_distributable` with the project page and no row.
- [ ] **5.8 Do:** `dest` of `~/.jarvis`, your home folder, `C:/Windows`, a drive root, a relative path. **Expect:** `bad_dest` each time, with no question asked and no network traffic.

---

## 6. `make_modpack_file`

- [ ] **6.1 Do:** Run `{"format":"modrinth","name":"Test Pack","mc_version":"1.20.1","loader":"fabric","entries":[{"project":"sodium"},{"project":"lithium"},{"project":"iris"}],"dest":"D:/McTest","dry_run":true}`. **Expect:** a confirmation of the tool, then `dry_run: true`, `written: false`, `resolved` and `dependencies_added` filled, `loader_version` with `loader_version_source: latest`; the folder is not created (try a new subfolder name to see).
- [ ] **6.2 Do:** Ask: `Make a Modrinth pack of sodium, lithium and iris for 1.20.1 Fabric into D:/McTest`. **Expect:** Jarvis does the dry run and shows the report; after you say go: the tool confirmation, then "Save outside the downloads library?", then `written: true`, `installed: false`. `Test Pack-1.0.0.mrpack` is in the folder, no `.part` left. **Then:** open it in the Modrinth app or Prism. **If not:** note which step failed.
- [ ] **6.3 Do:** repeat the same name. **Expect:** `exists`; nothing replaced. With `overwrite: true` it is replaced.
- [ ] **6.4 Do:** the same pack as `"format":"curseforge"` with your key. **Expect:** one `.zip` with `manifest.json` (loader as `fabric-<version>`) and, when you gave `src`, an `overrides/` folder. **Then:** import it in the CurseForge app. Without a key: `no_key` and one sentence.
- [ ] **6.5 Do:** `dest` `~/.jarvis`, `C:/Windows`, a drive root; and `src` `~/.jarvis`. **Expect:** `bad_dest` each time; nothing written; Jarvis's own folder is never packed.
- [ ] **6.6 Do:** `"entries":["this mod does not exist xyz"]`. **Expect:** `no_compatible_file` (nothing could be placed) with `skipped` naming the entry `not_found` or `ambiguous`; nothing written.
- [ ] **6.7 Do:** a CurseForge pack that includes a project that blocks downloads (for example Entity Culling). **Expect:** it is skipped as `not_distributable` with its project page; the others are listed.
- [ ] **6.8 Do:** a `src` folder that holds a hidden file, a `.env` and a shortcut/link. **Expect:** they are skipped and listed in the report, not packed.

### Matching across platforms (`entries_from`)

- [ ] **6.9 Do:** Run `{"format":"modrinth","name":"Cross Pack","mc_version":"1.20.1","loader":"fabric","entries_from":"curseforge","entries":["Sodium","Lithium"],"dest":"D:/McTest","dry_run":true}` (needs your key). **Expect:** `ok: true`, `written: false`, a `crossmatch` block (from curseforge, to modrinth, method sha1, `downloaded_files: 0`); `cross_matched` lists each file with an identical Modrinth file; an entry with none is in `skipped` (`not_found`), never replaced by a similar project.
- [ ] **6.10 Do:** Run `{"format":"curseforge","name":"Cross Pack","mc_version":"1.20.1","loader":"fabric","entries_from":"modrinth","entries":["sodium","lithium"],"dest":"D:/McTest","dry_run":true}`. **Expect:** a question (`needs_clarification`, `needs_download: true`) giving how many mod files and about how many MB would be downloaded from Modrinth; nothing downloaded, no dialog yet.
- [ ] **6.11 Do:** Ask: `Yes, go ahead and download them to match`. **Expect:** a dialog "Download mod files to match them on CurseForge?" with count and size, default Don't. After you agree: `crossmatch.method` fingerprint, `downloaded_files: 2`, `cross_matched` naming the CurseForge file for each mod that exists there. Asking again: `downloaded_files: 0` and no dialog. **If not:** `vendor_changed` BEFORE anything downloads means CurseForge's fingerprint route did not answer (never tried against the real service: tell us what it said). `not_found` for every mod may mean the fingerprint formula is wrong.
- [ ] **6.12 Do:** take one jar you downloaded from CurseForge and run `python -m jarvis.modhosts.crossmatch <that jar>`. **Expect:** it prints a fingerprint and sha1. Compare the fingerprint with the file's `fileFingerprint` in CurseForge's own answer for that file. **If not:** the formula is wrong; send both numbers and the file name.

---

## 7. Loader versions (when `loader_version` is left out)

In a Python shell with `jarvis-cli` on the path: `from jarvis.modhosts import loaders`.

- [ ] **7.1 Do:** `loaders.latest_loader_version("fabric", "26.3")`. **Expect:** the newest stable Fabric loader (the probe saw `0.20.1`). The first call takes about 20 s; the second is instant and makes no request.
- [ ] **7.2 Do:** `("quilt", "26.3")`. **Expect:** the highest non-pre-release Quilt loader, possibly higher than the probe's `0.24.0` (the list is unsorted; Jarvis picks by number). **If not:** report what you got.
- [ ] **7.3 Do:** `("forge", "26.3")` then `("forge", "26.1.1")`. **Expect:** `66.1.0` as `recommended`; `63.0.2` as `latest` with a note.
- [ ] **7.4 Do:** `("neoforge", "26.3")`, `("neoforge", "26.1.1")`, `("neoforge", "1.20.1")`. **Expect:** `26.3.0.99` as `stable`; `26.1.1.15-beta` as `latest` with a note; `no_loader_version`. (These numbers are from the probe day and will have moved.)
- [ ] **7.5 Do:** `("fabric", "99.99")`. **Expect:** `no_loader_version`.
- [ ] **7.6 Do:** disconnect the network, delete `~/.jarvis/modhosts/cache.json`, repeat the Fabric call. **Expect:** `offline`, with a sentence that contains no URL.

---

## 8. OptiFine

- [ ] **8.1 Do:** Ask: `Which OptiFine versions exist for 1.20.1?` **Expect:** `HD U I6` marked recommended, then `HD U I5`, matching optifine.net/downloads, no previews. With `include_preview` the previews appear. With no version you get one recommended build per Minecraft version and a note that 26.2 has only previews.
- [ ] **8.2 Do:** Run `download_optifine` `{"mc_version":"1.20.1","dry_run":true}`. **Expect:** a confirmation, then `dry_run: true` with the file name and full path; nothing written and the folder not created.
- [ ] **8.3 Do:** Ask: `Get OptiFine 1.20.1 HD U I6 into D:/McTest`. **Expect:** two questions (the tool, then "Save outside the downloads library?"); about 3 s later `OptiFine_1.20.1_HD_U_I6.jar` is in the folder, no `.part` left, the reply says "downloaded", not installed. **Then:** open it in 7-Zip, but **do not run it**. **If not:** `vendor_changed` with the official page link means optifine.net changed; use the page.
- [ ] **8.4 Do:** repeat 8.3. **Expect:** `exists` and no new download.
- [ ] **8.5 Do:** `dest` `~/.jarvis`, `C:/Windows`, a drive root, a relative path. **Expect:** `bad_dest` each time, with no questions and no network traffic.
- [ ] **8.6 Do:** (optional) disconnect the network and list versions. **Expect:** `offline`.

---

## 9. `check_mc_updates`

- [ ] **9.1 Do:** Ask: `Are my mods in D:/McMods up to date? I play 1.20.1 on Fabric.` **Expect:** `ok: true`; the older Modrinth jar under `updates` with `get_with` (download_mc_file, project, file id); the CurseForge jar under `not_checked` with `why: not_on_modrinth`; one `curseforge_note`; the message says nothing was downloaded, replaced, updated or installed; **the folder is unchanged** (compare file names and dates).
- [ ] **9.2 Do:** ask again without the version and loader. **Expect:** a jar that lists several versions or loaders is under `not_checked` (`needs_mc_version` / `needs_loader`) with `needs_clarification: true` and an `ask` line.
- [ ] **9.3 Do:** Run with `loader: quilt` on a Fabric-only jar. **Expect:** `latest.via: fabric`; with `quilt_fabric_fallback` off, `other_version` instead.
- [ ] **9.4 Do:** try `~/.jarvis`, your home folder, `C:/Windows` and a folder that does not exist. **Expect:** `bad_dest` each time, no network traffic.
- [ ] **9.5 Do:** an empty folder. **Expect:** `ok: true`, `checked: 0` and a message saying there was nothing to check; no request.
- [ ] **9.6 Do:** open `~/.jarvis/modhosts/update_state.json`. **Expect:** only the last folder and hash entries; no key, no vendor answer.
- [ ] **9.7 Do:** run the same check twice. **Expect:** the second is quicker (hash cache) with identical results.
- [ ] **9.8 Do:** rename one jar in `D:/McMods`, check again. **Expect:** it still matches (matched by content).

---

## 10. The panel (Menu > Minecraft)

- [ ] **10.1 Do:** Menu > Minecraft; then type `/minecraft`. **Expect:** the same panel opens both ways, with tabs Search, Project files, OptiFine, Make a pack; Escape and the × close it and focus returns.
- [ ] **10.2 Do:** Search `sodium` with **no** key. **Expect:** Modrinth hits, and a note with an "Open Minecraft settings" button that opens Settings > Integrations.
- [ ] **10.3 Do:** press **Files** on a hit; choose 1.20.1 and fabric. **Expect:** the Project files tab fills and one file is marked recommended. **If not:** this is the first time the layout is seen in a browser; write down what looks wrong.
- [ ] **10.4 Do:** pick a file, choose a folder, press **Queue download**. **Expect:** one dialog with the dry-run text and the folder. In 182, a folder outside the downloads library ends in an amber "Not confirmed" notice with a button "Use the Minecraft folder in the library"; with the library folder the row appears in Downloads. **If not:** if a row appears for an outside folder, `web/server.js` was changed since 182.
- [ ] **10.5 Do:** OptiFine tab: **List versions**, then **Check only**. Make a pack tab: dry run first, then **Make pack** and confirm; the file appears. **Expect:** results render (a generic table plus a "Raw answer" fold). **If not:** how OptiFine and pack results render is unknown; send a screenshot.
- [ ] **10.6 Do:** View Source and DevTools > Network while using the panel. **Expect:** only `/api/tools/preview` and `/api/tools/run`; no response or page text contains your key.
- [ ] **10.7 Do:** type `/mcsearch sodium`, `/mcfind 1.20.1 fabric sodium`, `/mcproject sodium`. **Expect:** all three appear in the palette and run without a confirmation. **If not:** one missing means the palette's tool-command list did not load.

---

## 11. Wrap-up

- [ ] **11.1 Do:** `python -c "from jarvis import tools; t=tools.AUTO_TOOL_PACK_INSTRUCTIONS['minecraft']; print(t.count('give the official optifine.net page link'))"`. **Expect:** `1`.
- [ ] **11.2 Do:** check `D:/McTest` and Downloads. **Expect:** only what you asked for; nothing was installed, no launcher profile touched.
- [ ] **11.3 Do:** send the results: each step that did not match, with the step number and what you saw. Steps 5.6, 6.11 and 6.12 are the ones nobody has seen work.
