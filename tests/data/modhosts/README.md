# tests/data/modhosts/: what the probe recorded

Everything here is written by `python tests/modhosts_checks.py` (phase 0 of the Minecraft mod tools). Until the owner has
run it and merged the output, **this folder holds only this README**, and any fact about CurseForge, Modrinth, OptiFine
or the loader sites that the plan marks "Probe" is unverified.

## How to read the files

- Each `*.json` is the vendor's own answer, **trimmed**: lists cut to a handful of items, long strings cut to a few
  hundred characters. Key names and structure are never changed, so a parser can be tested against them as they are.
  `probe_meta.json` says, per file: the request (method, URL, body), the HTTP status, the response headers worth knowing
  (content type, rate limits, redirects), what was cut (path -> original length), and for the loader files the
  "latest stable" the probe itself derived and the rule it used.
- A few files are **composites** the probe assembled (they are not one raw answer): `curseforge_swagger_summary.json`,
  `curseforge_blocked_project.json`, `curseforge_modpack_entries.json`, `curseforge_cdn_hops.json`,
  `optifine_hops.json`, `modrinth_generic_ua.json`, and the `loader_meta_forge*.json` / `loader_meta_neoforge*.json`
  files (subsets of the big lists). Their shape is described below.
- The CurseForge key is never in any file: it is sent only to `api.curseforge.com`, redacted from everything written, and
  the probe searches every file for it before finishing (and deletes any file that contains it).
- Files named `hand_<n>.json` are reserved for fixtures an agent writes by hand; the probe never writes that name.
- The mod, project and file names in here belong to their authors; they are kept only as test data.

## Modrinth (`https://api.modrinth.com/v2`)

| File | What it is |
| --- | --- |
| `modrinth_tag_game_version.json` | `GET /tag/game_version`, release entries only, newest first. The newest one is the Minecraft version the probe used. |
| `modrinth_tag_loader.json` | `GET /tag/loader`: loader names and the project types each supports. |
| `modrinth_search.json` | `GET /search` with `query`, `facets` (`project_type:mod`, `categories:fabric`, `versions:<mc>`), `limit`, `index`. |
| `modrinth_search_quilt.json` | The same with `categories:quilt` (do Fabric mods show up under Quilt?). |
| `modrinth_search_shader.json`, `modrinth_search_resourcepack.json`, `modrinth_search_datapack.json` | One or two hits each: the real `project_type` and `categories` for shaders, resource packs and data packs (data packs may be `project_type: mod` with a `datapack` category). |
| `modrinth_project.json` | `GET /project/{slug}`. |
| `modrinth_versions.json` | `GET /project/{slug}/version?loaders=[..]&game_versions=[..]` (filters honoured: see the report). |
| `modrinth_bulk_versions.json`, `modrinth_version.json`, `modrinth_bulk_projects.json` | `GET /versions?ids=[..]`, `GET /version/{id}`, `GET /projects?ids=[..]`. |
| `modrinth_version_files.json` | `POST /version_files` with `{"hashes": [sha1], "algorithm": "sha1"}`: an object keyed by hash. |
| `modrinth_version_file.json` | `GET /version_file/{sha1}`. |
| `modrinth_version_files_update.json` | `POST /version_files/update` (hashes, algorithm, loaders, game_versions): what an update check (S2) will use. |
| `modrinth_generic_ua.json` | `{user_agent_sent, status, body_start}`: what a generic `okhttp/4.9.3` User-Agent gets back. |

## CurseForge (`https://api.curseforge.com/v1`; only present when the owner's key was found)

| File | What it is |
| --- | --- |
| `curseforge_swagger_summary.json` | Not raw. From `swagger.json`: `paths` (method, operationId, parameter names, `*` = required, body schema), `enums` (values, names and description if given), `schemas` (property names and types), `distribution_properties` (any property whose name contains "istribution"). |
| `curseforge_classes.json` | `GET /categories?gameId=432&classesOnly=true`: the class ids (mods, modpacks, resource packs, worlds, shaders, ...). |
| `curseforge_loaders.json`, `curseforge_loaders_all.json` | `GET /minecraft/modloader?version=<mc>` and without a version (the second is cut to 40 entries): loader names (`forge-47.2.0`, ...) with their `type` number. |
| `curseforge_search.json` | `GET /mods/search` (gameId, classId, searchFilter, sortField, sortOrder, pageSize), no loader filter. |
| `curseforge_search_filtered.json` | The same with `gameVersion` and `modLoaderType`. |
| `curseforge_mod.json` | `GET /mods/{id}`. |
| `curseforge_files.json`, `curseforge_files_filtered.json` | `GET /mods/{id}/files`, unfiltered and with `gameVersion` + `modLoaderType`: how `gameVersions` mixes Minecraft versions, loader names and other tags; the hash `algo` numbers. |
| `curseforge_file.json`, `curseforge_download_url.json` | `GET /mods/{id}/files/{fileId}` and `.../download-url`. |
| `curseforge_bulk.json`, `curseforge_bulk_files.json` | `POST /mods` (`{modIds, filterPcOnly}`) and `POST /mods/files` (`{fileIds}`). |
| `curseforge_blocked_project.json` | Composite: `found`, `scanned`, `flag_values_seen`, and when one turned up, `mod` (with the distribution flag), `files` (null `downloadUrl`) and `download_url_endpoint` (what the download-url route answers for it). |
| `curseforge_cdn_hops.json` | Composite: one real `downloadUrl`, followed hop by hop with a 1-byte range request and **no key**: hosts, statuses, redirects. The source of the CDN host allow-list. |
| `curseforge_modpack_entries.json` | Composite: one small modpack file's size and headers, its zip entry list, whether `manifest.json` is at the root, and a summary of the manifest (keys, `minecraft`, `manifestType`, first three `files`). The zip itself is not kept. |

## OptiFine (`https://optifine.net`, official site only)

| File | What it is |
| --- | --- |
| `optifine_downloads.html` | The `/downloads` page as received (capped at 1.5 MB). The parser tests run against this. |
| `optifine_downloads_more_<n>.html` | Only present if "Show all versions" / "+ More" / "+ Preview versions" turned out to be real links; the pages they led to. |
| `optifine_hops.json` | Composite: the chosen older version, every hop of its Download chain and of its (Mirror) chain (host, scheme, status, redirect target, content type, length, `Content-Disposition`, ZIP signature), the text of each intermediate page (cut at 120,000 characters), how the final link is formed (`final_link_shape`), whether it works a second time straight away, without a `Referer`, and after the wait (`after_wait`). The OptiFine route (A or B) comes from this. |

The intermediate pages carry ad scripts and a short-lived anonymous download token; both are harmless but are not test data
worth keeping beyond the parser. **No OptiFine jar is ever kept**: only headers and the first 16 bytes of the final file.

## Loader metadata (for the "latest loader version" resolver)

| File | What it is |
| --- | --- |
| `loader_meta_fabric.json` | `https://meta.fabricmc.net/v2/versions/loader/<mc>` (first 3 entries). `probe_meta.json` says which entry the probe took as "latest stable" and why. |
| `loader_meta_quilt.json` | `https://meta.quiltmc.org/v3/versions/loader/<mc>` (first 3 entries). |
| `loader_meta_forge.json` | `promotions_slim.json` cut to the entries for the tested versions plus the newest ten keys: `{homepage, promos}`. |
| `loader_meta_forge_maven.json` | Composite from `maven-metadata.xml`: version count, first/last versions, those for the tested Minecraft version, the start of the XML. |
| `loader_meta_neoforge.json` | `{isSnapshot, versions}` cut to the versions that map to the tested Minecraft version plus the newest four. |
| `loader_meta_neoforge_legacy.json` | The older `net.neoforged:forge` artifact (used for 1.20.1), if it answered. |

Each loader file names the Minecraft version it was fetched for in `probe_meta.json` (`derived.mc_version`); it can be an
older version than the newest if the newest had no build yet.

## Index

`probe_meta.json`: `{written, user_agent, minecraft_version_tested, fixtures: {<file>: {status, headers, request, lists_and_strings_cut, derived?}}}`.
