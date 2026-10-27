# Minecraft guide

For two readers. **You, the owner:** what can I ask Jarvis for, how do I set up CurseForge, what will it never do, how sure are we that it works. **A developer:** section 9 says where the code is.

**Checked against:** the code of `jarvis-main_182_` (read, not run) and the reports of the agents who built it, written 2026-10-27. Every sentence about behaviour was checked in a file named in section 9, or says "not checked". Things other agents are changing right now (the update check, the panel, the key layer) are described **as they are in 182**; if you apply their patches, those parts may change.

**Paths.** A path such as `modhosts/net.py` or `actions/minecraft_lookup.py` is inside `jarvis-cli/jarvis/`. Paths that start with `tests/`, `web/` or `DOCUMENTATION/` are at the repo root. `~/.jarvis/` is Jarvis's folder in your home folder.

**Three words that mean exactly this, everywhere in this guide** (they are the legend of section 8):

| Word | Meaning |
|---|---|
| **Tested against a fake** | A test script exists that runs the code against a stand-in (a fake server, recorded or hand-written answers, a fake page). It says nothing about the real thing. **None of the reports records a test run**: the authors were told not to run them, so "tested" means "a test is written". |
| **Probe on your PC** | You ran `tests/modhosts_checks.py` on 25 Oct 2026 and the answers of the real services were saved. It shows what the service said that day, to the probe. It does not show that Jarvis's own tool works. |
| **Never tried** | Nobody has seen it work against the real service or in a real browser. |

---

## 1. What it is

Seven tools, all **yours alone** (a chat guest is not offered them and each one refuses a guest itself), that look things up on Modrinth, CurseForge and OptiFine's official site, download single files, write modpack files, and tell you which of your mods have newer files.

**Nothing is ever installed.** No tool unzips a pack, edits a launcher, runs an installer or starts the game. Every answer says "queued", "written", "downloaded, not installed" or "a newer file is available", never "installed" or "updated".

| Tool | What it does | You can say | Asks first? |
|---|---|---|---|
| `search_mc_mods` | Searches Modrinth (and CurseForge when you have a key) for mods, modpacks, shaders, resource packs or data packs. Up to 10 hits (default 5). With `source: both` you get two separate lists, never one merged ranking. | `Search Modrinth for sodium for 1.20.1 on Fabric` | No (read-only) |
| `get_mc_project` | One project with its newest **compatible** files (default 5; 0 = all), their channel, size, whether they can be downloaded, and the required dependencies by name. A name that fits several projects returns the candidates; Jarvis must ask you. Quilt with no Quilt file shows the Fabric file, marked `via: fabric`. | `Show me the sodium mod page for 1.20.1 on Fabric` | No |
| `download_mc_file` | Queues **one** file in the Download Hub (Menu > Downloads), on the HTTP engine, with the vendor's checksum so the hub checks the finished file (Modrinth: sha512, else sha1; CurseForge: sha1). Required dependencies are only listed unless you say `with_dependencies`. `dry_run` shows the exact file and queues nothing. | `Download Sodium for 1.20.1 Fabric into D:/Minecraft/mods` | Yes: the tool, then "Save outside the downloads library?" for a full path |
| `make_modpack_file` | Writes **one** `.mrpack` (Modrinth) or CurseForge `.zip` from a list of projects, one platform per pack. Required dependencies are added. Entries that cannot be placed are reported and skipped, never swapped for something similar. `loader_version` is optional (the latest is used and the report says which). `src` packs a folder of yours into `overrides/`. `dry_run` writes nothing. Optional cross-platform matching: section 5. | `Make a Modrinth pack of sodium, lithium and iris for 1.20.1 Fabric into D:/Packs` | Yes: the tool (dry runs too), then "Save outside the downloads library?" |
| `list_optifine_versions` | Lists OptiFine builds from optifine.net, newest first, the recommended build marked. Previews (test builds) only with `include_preview`. Default 30 rows (0 = all). | `Which OptiFine versions exist for 1.20.1?` | No |
| `download_optifine` | Downloads **one** OptiFine jar from the official site into a folder, under its official name. The jar is OptiFine's installer: it is **never run**. It does not use the Download Hub, so no row appears in Downloads. Never overwrites a file. | `Get OptiFine 1.20.1 HD U I6 into D:/Minecraft/mods` | Yes: the tool, then "Save outside the downloads library?" |
| `check_mc_updates` | Looks at **one folder** of yours and says which `.jar`, `.zip` and `.mrpack` files have a newer compatible file on Modrinth. Read-only. Section 6. | `Are my mods in D:/Minecraft/mods up to date? I play 1.20.1 on Fabric` | No |

For a pack, the tool's own instructions tell Jarvis to do the dry run first and show you the report before the real write.

---

## 2. The CurseForge key

Modrinth and OptiFine need nothing. **CurseForge only answers a request with a personal API key**, issued per developer and **not transferable**: Jarvis never ships one and never shares one. Without a key every CurseForge path answers with one sentence saying how to get a key, and the rest keeps working.

1. Get a free key at `console.curseforge.com` (your own account).
2. Paste it in **Settings > Integrations > Minecraft mods**. The page only ever shows "set" or "not set", never the key, and clears the box as soon as you save. Or put it in `~/.jarvis/curseforge.json` as `{"api_key": "<your key>"}`. The environment variable `CURSEFORGE_API_KEY` also works, but the file wins when both exist, and a shell command Jarvis runs can read an environment variable, so the file is the safer home.
3. Check it: `jarvis doctor minecraft` says whether a key is set and whether the file is readable (it makes no request). `jarvis doctor minecraft --deep` makes one small CurseForge request to see that the key is accepted.

The key is sent only to `api.curseforge.com`, in one header, by `modhosts/net.py`. It is never written in a result, an error, a log, a Downloads row or the cache, and never shown to the model. A pasted key with quotes or a trailing line break is cleaned; a key that cannot be an HTTP header value is reported as damaged instead of being sent.

---

## 3. Settings

All nine are tool settings of `actions/minecraft_lookup.py`. **Contact** and **Default folder** also appear in Settings > Integrations > Minecraft mods; all nine are in Settings > Tool settings. **`0` means no limit** on every number below.

| Setting | Default | What it does |
|---|---|---|
| `contact` | `github.com/GoatAssim` | Sent to Modrinth and CurseForge in the User-Agent (`Jarvis/<build> (<contact>)`) so they can tell who is asking. Letters, digits and `. _ : / @ + -` only. |
| `default_dest` | `~/Downloads` | The folder used whenever a download or pack tool is given no `dest`, and the folder `check_mc_updates` looks at when you name none and it has no last folder. `~` is expanded when the tool runs. A folder outside the Download Hub library still asks "Save outside the downloads library?". |
| `max_pack_files` | `0` | Most files in one pack. |
| `overrides_max_files` | `0` | Most files copied into a pack's `overrides/`. |
| `overrides_max_total_mb` | `0` | Largest total size of a pack's `overrides/`, in MB. |
| `overrides_max_file_mb` | `0` | Largest single file in `overrides/`, in MB. |
| `download_max_mb` | `0` | Largest download, in MB. Also the limit for the files read to match packs (section 5). |
| `deadline_seconds` | `0` | Overall time allowed for one tool call. Each network request still has its own timeout, so a dead connection never hangs. |
| `quilt_fabric_fallback` | on | When a project has no Quilt file, offer its Fabric file (marked `via: fabric`). Also used by the update check and by pack matching. |

What stays even at `0`: the vendors' own limits (Modrinth allows 300 requests a minute), batch sizes, and generous defensive limits that protect memory (one file read for matching is never held beyond 256 MB). The safety rules are not limits and cannot be switched off: protected folders are refused, no links followed, nothing from `~/.jarvis` goes into a pack, hidden and secret files are skipped, host lists are fixed in code, hashes are checked.

---

## 4. What it never does

- **Install.** It does not unzip a pack, create or edit a launcher instance or profile, run the OptiFine installer, run any jar, or start Minecraft. There is no tool with "install" in its name; a test pins that.
- **Work around a block.** If an author blocks third-party downloads, CurseForge gives no download link (or answers 403 to the link request). Jarvis says `not_distributable`, gives the project page, and tries nothing else: not another site, not a guessed CDN address.
- **Use another site for OptiFine.** Only the official `optifine.net` flow, one file per call, at least a second between requests. The ad-wrapper site OptiFine's Download button goes through (`adfoc.us`) is never contacted and is on no host list. If the site changes and Jarvis can't follow it, the answer says so and gives the official page. No third-party mirrors.
- **Talk to any other site.** The host lists are constants in `modhosts/hosts.py` (`api.modrinth.com`, `cdn.modrinth.com`; `api.curseforge.com`, `edge.forgecdn.net`; `optifine.net`; and five loader-version sites). GitHub and GitLab are not on them. A config file or environment variable can never add one.
- **Guess.** A name that fits several projects returns the candidates. A file with no identical match on the other platform is skipped. A file that works on several versions and was given no answer is "not checked". A file Modrinth doesn't know is "not checked".
- **Overwrite or write where it should not.** No file is replaced unless you say `overwrite` (packs) and then only a plain file; OptiFine never overwrites. Your home folder itself, `~/.jarvis`, OS folders, drive roots, relative paths, `..` and links are refused with `bad_dest` before any question or network call.
- **Update, rename or delete your mods.** `check_mc_updates` only reads.
- **Share a key** or ship one.
- **Do other things you may expect:** server-pack generation, importing or inspecting an existing pack, updating a pack, shared or embedded keys. Not built.

---

## 5. Packs and matching across platforms

A pack lists files from **one** platform: a `.mrpack` can only point at `cdn.modrinth.com`, a CurseForge pack only holds CurseForge ids. Normally Jarvis looks every entry up on the pack's own platform.

`make_modpack_file` has two optional arguments for building the *other* platform's pack from the same list: `entries_from` (the platform your entries are named on) and `download_to_match`. Each entry is looked up where it is named and matched to the **identical file** (same sha1) on the pack's platform.

- **CurseForge list to a Modrinth pack:** nothing is downloaded; Modrinth is asked which file has that sha1.
- **Modrinth list to a CurseForge pack:** CurseForge identifies a file by its own fingerprint, which needs the file's bytes. So Jarvis first **asks** (the answer gives the count and size), then, only with `download_to_match` **and** your yes in a dialog (default no), fetches each mod file from `cdn.modrinth.com`, checks it against Modrinth's size, sha1 and sha512, works out the fingerprint in memory, and throws the file away. Nothing is saved. Fingerprints are remembered (30 days, in `~/.jarvis/modhosts/cache.json`), so a second run downloads nothing. The route is tested with a harmless request **before** anything is downloaded.
- An entry with no identical file is reported and skipped, never replaced by a similar project. A CurseForge file whose author blocks downloads is not matched.
- **Never tried:** CurseForge's fingerprint route and the fingerprint formula against a real CurseForge file (section 8). If the formula were wrong you would get "not found" for every Modrinth entry, never a wrong match, because the sha1 must also agree. To test it with one jar you downloaded from CurseForge: `python -m jarvis.modhosts.crossmatch <file>` prints its fingerprint and sha1; compare the fingerprint with the file's `fileFingerprint` in the CurseForge API answer.

---

## 6. The update check

`check_mc_updates` answers: "which files in this folder have a newer compatible file on Modrinth?"

- **It reads, it never changes.** Nothing is downloaded, replaced, renamed or installed, and the folder is not written to. To get a newer file you ask for it with `download_mc_file`; each update in the answer carries `get_with` (the tool, project and file id to use). It says "a newer file is available", never "updated".
- **Which folder:** the one you name, else the last one it checked, else the `default_dest` setting. The answer says which (`folder_from`). It looks one level deep only: sub-folders, links, hidden files and other extensions are counted under `ignored` and never opened.
- **Only hashes leave your PC**, to `api.modrinth.com`. No file name, no path. A renamed jar still matches; a look-alike name never does.
- **"Newer"** means Modrinth published a file after the one you have, for the same Minecraft version and loader. Dates are compared, never version numbers. A release file is not offered a beta unless you ask for `channel: beta` or `alpha`.
- **Never a guess:** a file that works on several Minecraft versions or loaders, with no answer from you, is listed "not checked" (`needs_mc_version` / `needs_loader`) and Jarvis should ask you. A file built for another version or loader is `other_version`, not "up to date". Quilt falls back to Fabric files as elsewhere.
- **CurseForge files are not checked.** A file Modrinth doesn't know is "not checked" (`not_on_modrinth`) with a note that CurseForge identifies files by a fingerprint this check does not compute. No other site is contacted.
- **State file:** `~/.jarvis/modhosts/update_state.json` holds the last folder and a hash cache (path, size, modified time, sha1), so an unchanged jar isn't read twice. No key, no vendor answer. Safe to delete (one re-hash). It is not shown in Settings.
- **Heads-up:** with no folder named and no last folder, it reads `~/Downloads` and sends the sha1 of every `.jar`, `.zip` and `.mrpack` in it. Hashes only, but name a folder if you'd rather it did not.
- A file you downloaded with `download_mc_file` is not recorded anywhere; the folder is the record, so if you moved it, name the folder it is in.

---

## 7. The panel and the three commands

**Menu > Minecraft** (or `/minecraft` in the Ask box) opens the panel. Four tabs:

- **Search:** query, source, kind, Minecraft version, loader, sort. A hit's **Files** button fills the next tab.
- **Project files:** compatible files with the recommended one marked; pick one, choose a folder, **Check only (dry run)** or **Queue download**.
- **OptiFine:** list versions, check only, download.
- **Make a pack:** dry run first, then make the pack.

The panel is a thin face on **six** of the tools (not `check_mc_updates`, in 182). It never talks to a vendor and never sees your key: every action goes through the same tool checks as the Debug panel. Before anything is written it does a dry run and shows one dialog with the exact folder. Vendor text is shown as plain text, and a link is made only for `https` addresses on `modrinth.com`, `curseforge.com` or `optifine.net`. With no CurseForge key it shows a note with an "Open Minecraft settings" button.

**Known limit in 182:** the panel sends the "I just showed you the folder" flag, but `web/server.js` does not act on it. A folder outside the Download Hub library (the default `~/Downloads` counts as outside) therefore ends in "Not confirmed", and the panel offers a button to use the "Minecraft" folder inside the library instead. Asking Jarvis in chat goes through the tool's own question (not checked in a browser).

**Palette commands** (type `/` in the Ask box). All three are read-only lookups and run without a confirmation:

| Command | Example | Does |
|---|---|---|
| `/mcsearch <query>` | `/mcsearch sodium` | `search_mc_mods` with that query |
| `/mcfind <mc_version> <loader> <query>` | `/mcfind 1.20.1 fabric sodium` | the same, for one Minecraft version and loader |
| `/mcproject <project> [mc_version] [loader]` | `/mcproject sodium 1.20.1 fabric` | `get_mc_project` |

Downloads and packs are deliberately **not** commands: they need the folder question and the confirmation.

---

## 8. What is verified

Read this before you trust anything.

**Probe on your PC (25 Oct 2026; reports `probe/modhosts-checks-report.txt` and `-cf.txt`).** What the real services answered:

| Service | What the probe saw | Limits |
|---|---|---|
| **Modrinth** | Search with facets, project versions filtered by loader and Minecraft version, bulk lookups, `POST /version_files` and `/version_files/update` by hash; files carry sha1 and sha512 on `cdn.modrinth.com`; rate limit 300 per minute. Data packs come back as `mod` with category `datapack`. Tested with `26.3` and Fabric. | Not exercised: the `offset` search parameter, slugs in the bulk project lookup, the project members call (used only for the author's name). |
| **Loader sites** (Fabric, Quilt, Forge, NeoForge) | All five hosts answered. Fabric's answer is about 584 KB and took about 21 s twice. Quilt's list is not newest-first; NeoForge's is unsorted; some Minecraft versions have no Forge "recommended" build. | Jarvis picks by number, never list order. The Quilt choice needs a live check. |
| **OptiFine** (`optifine.net/downloads`) | 225 rows in the page itself; the "(Mirror)" link leads through `adloadx` to `downloadx`, which returns the jar; every hop stayed on `optifine.net`; no waiting or Referer was needed. | Whether the link expires could not be decided, so the tool downloads inside the call. |
| **CurseForge API** (run 2, 36 requests, 0 failures) | Search, mod files, bulk calls, class ids, loader numbers, `fileFingerprint` on files, sha1 and md5 only as hashes. A project that blocks distribution (id 448233) had null download links and answered 403 to the link route. Run 1's CurseForge part failed because the key was read from a damaged environment variable. | |
| **Minecraft versions** | Not all `1.x` any more: Modrinth's newest are `26.3`, `26.2`, `26.1.2`, `26.1.1`, `26.1`, then `1.21.11`. Both shapes are accepted. | |

**Tested against a fake** (tests are written; run them yourself, section 10):

| Part | Against what |
|---|---|
| Lookup tools, file choice, Quilt fallback, never-guess rule, dependencies | Fake Modrinth and fake CurseForge servers in `tests/` with hand-written fixtures |
| `net.py`: host lists, redirects, key only to `api.curseforge.com`, key scrubbed from errors | A fake transport |
| Download tool and the Download Hub `verify` option (hash on the finished file) | Fake adapters plus the real hub store; a local test server |
| Packs: `.mrpack` and CurseForge `.zip` | Golden files worked out from the format specs; both output formats re-read after writing |
| Loader-version lookup | A fake of the five loader sites |
| OptiFine tools | A fake `optifine.net` and the probe's saved page |
| Cross-platform matching and the fingerprint | An independent textbook implementation of the hash, hand-written catalogs |
| `check_mc_updates` | An in-process fake Modrinth |
| Panel | Pure helpers only, in Node without a browser |
| Guards (owner-only, confirm-gated, no "install", one network door, key stays in its file) | `tests/test_minecraft_guards.py`, `test_minecraft_integration.py` |

**Never tried:**

- A real **CurseForge CDN download.** The probe's two first-byte requests to `edge.forgecdn.net` got HTTP 404 (the cause is unknown: the request style, or the files). Whether the hub's plain download works is unknown; when you try one, watch its Downloads row. A 404 there is the honest result, and nothing else is tried.
- CurseForge's **fingerprint route** (the probe never called it) and the **murmur2 variant** against a real CurseForge file (section 5).
- The **panel in a browser.** No one has seen it rendered; how OptiFine lists and pack results look is unknown. The hand-written hostile-text fixture is only used on the helper functions.
- A real **download and pack end to end**: a pack opened in the Modrinth app, Prism or the CurseForge app; a real OptiFine jar through `download_optifine`; a real `check_mc_updates` run on your mods.
- A CurseForge **manifest that lists a blocked file** (such files are skipped and reported).
- Windows-specific behaviour (OneDrive placeholders are skipped as links; hidden-file attribute): written, not run.
- The Quilt choice against the live site, and `version_files/update` through the tool (its shape is in the probe's fixture).

**Known gaps in the code, so you don't file them as new bugs** (from the audit reports; not rechecked line by line):

- A file with no published hash is queued **without a checksum**; the result says so. The hub's duplicate check ignores `verify`, so a link already queued without one keeps no check; the tool says "can't promise its checksum".
- Several "Save outside the downloads library?" questions come with `with_dependencies` (one per file); declining one stops the rest.
- A 403 from the CDN host can be reported as "API key may be wrong" (`bad_key`); the download tool reports the null-link and 403 cases as `not_distributable`.
- CurseForge answers are never cached, so big CurseForge packs make many requests.

---

## 9. For developers

| What | Where |
|---|---|
| The seven tools | `actions/minecraft_lookup.py` (`search_mc_mods`, `get_mc_project`, the group's one `TOOL_PACK_INSTRUCTION`, all nine `TOOL_SETTINGS`, the three `TOOL_COMMANDS`), `minecraft_download.py`, `minecraft_packs.py`, `minecraft_optifine.py`, `minecraft_updates.py` |
| The engine | `modhosts/`: `net.py` (the one door), `hosts.py`, `config.py` (the key), `cache.py`, `untrusted.py`, `sources.py`, `modrinth.py`, `curseforge.py`, `resolve.py`, `loaders.py`, `packs.py`, `outdir.py`, `optifine.py`, `crossmatch.py`, `updates.py` |
| The panel | `web/public/minecraft-panel.js` and `.css`; the `minecraft` verb in `slash-commands-data.js` |
| State files | `~/.jarvis/curseforge.json`, `~/.jarvis/modhosts/cache.json`, `~/.jarvis/modhosts/update_state.json` |
| The rules and why | `AGENTS.md`, "Minecraft mod sources"; `REPO_MAP.md` |
| Reason codes | `modhosts/__init__.py`: `no_key`, `bad_key`, `rate_limited`, `not_found`, `ambiguous`, `not_distributable`, `no_compatible_file`, `no_loader_version`, `bad_dest`, `exists`, `too_big`, `verify_failed`, `vendor_changed`, `offline`, `unsupported` |

**When something goes wrong**

| You see | Cause | Try |
|---|---|---|
| "CurseForge needs your own personal API key..." (`no_key`) | No key, or a damaged one | Section 2; `jarvis doctor minecraft` |
| `not_distributable` with a project page | The author blocks third-party downloads | Download it from the page yourself. Jarvis will not try another way. |
| `no_compatible_file` with alternatives | Nothing for that Minecraft version and loader | Look at the alternatives; check the version text (`1.20.1`, `26.3`) |
| `ambiguous` with candidates | The name fits several projects | Give the slug or id |
| `bad_dest` | A protected, relative or missing folder | Give a full path like `D:/Minecraft/mods` |
| `exists` | The file is already there | Another `filename`, or `overwrite` for packs |
| `vendor_changed` (OptiFine) with a page link | optifine.net changed | Use the official page; report it |
| `no_loader_version` | No loader build for that Minecraft version, or the loader site is down | Give `loader_version` yourself |
| "Not confirmed: nothing was written" | You answered no, or nobody could answer (the panel, 182) | Section 7 |
| "`<tool>` is the owner's: someone in a chat can't use it" | A guest, or an unreadable sender | Use it yourself |

---

## 10. Tests

From the repo root. Fixtures live in `tests/data/modhosts/`, which is git-ignored: they must be added with `git add -f`, and a zip made from a clean checkout does not contain them (the tests that read them fail or print SKIP).

```
python3 tests/test_minecraft_guards.py
python3 tests/test_minecraft_integration.py
python3 tests/test_modhosts_net.py
python3 tests/test_modhosts_modrinth.py
python3 tests/test_modhosts_curseforge.py
python3 tests/test_modhosts_resolve.py
python3 tests/test_modhosts_loaders.py
python3 tests/test_modhosts_packs.py
python3 tests/test_modhosts_crossmatch.py
python3 tests/test_minecraft_lookup_tools.py
python3 tests/test_minecraft_download.py
python3 tests/test_minecraft_packs_tool.py
python3 tests/test_optifine.py
python3 tests/test_minecraft_updates.py
python3 tests/test_minecraft_panel.py
node tests/verify_minecraft_panel.js
```

The hand-test list is `DOCUMENTATION/MINECRAFT_TEST_CHECKLIST.md`.
