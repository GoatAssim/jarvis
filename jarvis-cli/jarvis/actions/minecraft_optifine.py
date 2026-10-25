"""OptiFine from Jarvis (Minecraft tools, agent P6): list what optifine.net offers, download one jar.

What this is, and is not:
  * `list_optifine_versions` reads the ONE official page (https://optifine.net/downloads) and lists the builds, newest
    first, the recommended build of each Minecraft version marked.  Previews (test builds) only with `include_preview`.
    Read-only.
  * `download_optifine` downloads ONE jar per call from optifine.net (no mirror, no other site), INSIDE the call
    (ROUTE B of the plan: the probe could not tell whether the link's token expires, so nothing is queued for later).
    It follows the site's own interstitial hop by hop, politely paced (at least a second between requests), writes
    `<name>.part` in the chosen folder, checks the jar's SHAPE (ZIP signature, readable ZIP, plausible size, the name
    the server says) and renames it into place.  OptiFine publishes no checksum, so shape is all there is to check.
  * The jar is OptiFine's INSTALLER.  Jarvis never runs it and never installs anything: the answer says "downloaded".
  * Both tools are the owner's (`tools.OWNER_ONLY_TOOLS`; the handlers also refuse a chat guest themselves, and an
    unreadable sender counts as a guest).  `download_optifine` is confirmed with the owner before every call
    (TOOL_CONFIRM_REQUIRED), and a full-path folder asks the hub's own "Save outside the downloads library?" question.
  * `dest` omitted = the tool setting `default_dest` (default `~/Downloads`).  `dry_run` fetches only the version list
    and writes nothing.  A page that no longer parses is reason `vendor_changed` with the official page link: there is
    no fallback to any other site, ever.
  * The parser, the link hops and the jar check live in `modhosts/optifine.py`; this file is the tools, the destination
    guards and the safe write.  The network is `jarvis.modhosts.net.fetch`, imported lazily there.

SAFE IMPORTS: nothing from jarvis.tools, ai_client or the router is imported at module level (see _template.py).
"""
import json
import os
import re
from pathlib import Path

# Tests swap these (the steam_tools OPEN_URL precedent): None = the real net.fetch / the real cache module.
FETCH = None
CACHE = None

OFFICIAL_PAGE = "https://optifine.net/downloads"
_JAR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,99}\.jar\Z")


def _opt():
    from ..modhosts import optifine          # lazy: loading this file must not depend on the package being complete
    return optifine


def _is_chat_guest():
    """True when this turn answers a chat message from someone who is NOT the owner.  No chat context (the PC, a
    scheduled job) is the owner's own.  An unreadable value counts as a guest: a permission check fails closed."""
    raw = os.environ.get("JARVIS_CHANNEL_SENDER") or ""
    if not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001  (bad JSON, or nested too deep to read: either way, not the owner)
        return True
    if not isinstance(data, dict):
        return True
    if not data.get("platform") or not data.get("user_id"):
        return False
    return not data.get("is_owner")


def _guest_refusal(tool):
    return {"ok": False, "reason": "unsupported",
            "error": "%s is the owner's: someone in a chat can't use it" % tool}


def _fail(reason, error, **extra):
    out = {"ok": False, "error": str(error), "reason": reason}
    out.update(extra)
    return out


def _unexpected(label, exc):
    return _fail("unsupported", "%s failed unexpectedly: %s" % (label, str(exc)[:160]))


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# --------------------------------------------------------------------------- the destination

def _default_dest():
    from .. import tool_settings
    raw = tool_settings.get("minecraft_lookup", "default_dest", "~/Downloads")
    return raw.strip() if isinstance(raw, str) and raw.strip() else "~/Downloads"


def _download_cap():
    """The `download_max_mb` tool setting in bytes; 0 (the default) = no cap of the owner's, so None here
    (modhosts/optifine.py still keeps its own defensive memory ceiling)."""
    from .. import tool_settings
    try:
        mb = float(tool_settings.get("minecraft_lookup", "download_max_mb", 0) or 0)
    except (TypeError, ValueError):
        mb = 0
    return int(mb * 1024 * 1024) if mb > 0 else None


def _protected(path):
    """Why a folder may not be written to, or None.  The ONE shared rule (path_tools._protected_reason: drive roots,
    the home folder itself, OS folders, ~/.jarvis).  If it cannot be asked, nothing is written: fail closed."""
    try:
        from . import path_tools
        check = path_tools._protected_reason
    except Exception:  # noqa: BLE001
        return "the folder rules (path_tools) couldn't be loaded, so nothing is written"
    for candidate in (Path(path), Path(os.path.realpath(path))):      # the path as written AND where it really points
        why = check(candidate, writing=True)
        if why:
            return why
    return None


def _resolve_dest(raw):
    """The folder to save into -> (Path, None) | (None, failure).  Absolute only (no '..', no drive root), nothing
    protected, never a symlink, never an existing file.  Nothing is created here."""
    text = str(raw).strip() if raw not in (None, "") and str(raw).strip() else _default_dest()
    if "\x00" in text:
        return None, _fail("bad_dest", "That folder name has an invalid character.")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        return None, _fail("bad_dest", "Give a full folder path like D:/Minecraft/mods (or leave dest out for %s); "
                                       "'%s' isn't one." % (_default_dest(), text[:80]))
    if ".." in path.parts or len(path.parts) <= 1:
        return None, _fail("bad_dest", "'%s' isn't usable: no '..' and no drive root." % text[:80])
    if text.lower().endswith(".jar") and not path.is_dir():
        return None, _fail("bad_dest", "dest is a FOLDER ('%s' looks like a file). Use filename for the file's name." % text[:80])
    why = _protected(path)
    if why:
        return None, _fail("bad_dest", "Won't save there: %s." % why)
    if os.path.islink(path):
        return None, _fail("bad_dest", "Won't save into a symlink/shortcut folder (%s)." % str(path)[:120])
    if os.path.lexists(path) and not os.path.isdir(path):
        return None, _fail("bad_dest", "%s exists and is not a folder." % str(path)[:120])
    return path, None


def _target_name(official, requested):
    """The file name to write: the official one, or `filename` when given.  A name that would have to be changed
    to be safe is refused, not silently fixed.  -> (name, None) | (None, failure)."""
    if requested in (None, ""):
        return official, None
    name = str(requested)                                  # NOT stripped: a name that needs fixing is refused
    ok = bool(_JAR_NAME_RE.match(name))
    if ok:
        try:
            from ..hub import naming
            ok = naming.sanitize_filename(name, default="") == name
        except Exception:  # noqa: BLE001 - the strict pattern above already decided
            pass
    if not ok:
        return None, _fail("bad_dest", "'%s' isn't a safe file name for the jar (letters, digits, dots, dashes, spaces; "
                                       "it must end in .jar)." % str(requested)[:80])
    return name, None


def _confirm_outside(folder, name):
    """A full-path folder writes outside the downloads library, so it asks first, like the hub does (default=False:
    an unattended run saves nothing)."""
    from .. import ui_bridge as ui
    return bool(ui.confirm("Save outside the downloads library?",
                           body="%s will be saved in:\n%s\n\nThat folder is outside your downloads library."
                                % (str(name)[:80], str(folder)[:200]),
                           level="warn", confirm_label="Save there", cancel_label="Don't", default=False))


def _write_jar(folder, name, data, official_name):
    """data -> folder/name, atomically: `<name>.part` (created exclusively), shape-checked ON DISK, then renamed.  A
    failure leaves nothing behind.  -> (path, None) | (None, failure)."""
    opt = _opt()
    target, part = folder / name, folder / (name + ".part")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        folder.mkdir(parents=True, exist_ok=True)         # only now: a download that fails earlier creates nothing
        fd = os.open(str(part), flags, 0o644)
    except FileExistsError:
        return None, _fail("exists", "%s is already there (a download of it may be running, or an earlier one failed). "
                                     "Delete it and ask again; nothing was changed." % str(part)[:160])
    except OSError as exc:
        return None, _fail("bad_dest", "Couldn't write in %s (%s)." % (str(folder)[:120], str(exc)[:80]))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        if os.path.getsize(part) != len(data):
            raise OSError("the file on disk is not the size that was downloaded")
        good, why = opt.check_jar_shape(str(part), official_name)
        if not good:
            _drop(part)
            return None, _fail("verify_failed", "The download didn't check out: %s. Nothing was kept." % why)
        if os.path.lexists(target):                       # appeared while we downloaded: never overwrite
            _drop(part)
            return None, _fail("exists", "%s appeared in the meantime; not overwritten, the download was discarded."
                               % str(target)[:160])
        os.replace(str(part), str(target))
    except OSError as exc:
        _drop(part)
        return None, _fail("bad_dest", "Couldn't save the jar (%s). Nothing was kept." % str(exc)[:100])
    return target, None


def _drop(path):
    try:
        os.unlink(str(path))
    except OSError:
        pass


# --------------------------------------------------------------------------- the tools

def tool_list_optifine_versions(args):
    if _is_chat_guest():
        return _guest_refusal("list_optifine_versions")
    try:
        args = args or {}
        return _opt().list_versions(args.get("mc_version"), _truthy(args.get("include_preview")), args.get("limit", 30),
                                    fetch=FETCH, cache=CACHE)
    except Exception as exc:  # noqa: BLE001 - a tool never raises
        return _unexpected("list_optifine_versions", exc)


def _size_text(n):
    return "%.1f MB" % (n / 1048576.0) if n >= 1048576 else "%d KB" % max(1, round(n / 1024.0))


def tool_download_optifine(args):
    if _is_chat_guest():
        return _guest_refusal("download_optifine")
    try:
        return _download(args or {})
    except Exception as exc:  # noqa: BLE001
        return _unexpected("download_optifine", exc)


def _download(args):
    opt = _opt()
    if not str(args.get("mc_version") or "").strip():
        q = "Which Minecraft version should the OptiFine jar be for (like 1.20.1)?"
        return {"ok": True, "needs_clarification": True, "question": q, "message": q}
    mc = opt.normalise_mc_version(args.get("mc_version"))
    if mc is None:
        return _fail("not_found", "'%s' isn't a Minecraft version number (like 1.20.1 or 26.3)." % str(args.get("mc_version"))[:30])
    preview, dry = _truthy(args.get("preview")), _truthy(args.get("dry_run"))
    version = args.get("version")

    folder, err = _resolve_dest(args.get("dest"))          # before any network: a bad folder costs nothing
    if err:
        return err
    wanted_name = args.get("filename")
    if wanted_name not in (None, ""):
        _name, err = _target_name("x.jar", wanted_name)
        if err:
            return err

    pacer = opt.Pacer()
    loaded = opt.load_rows(FETCH, CACHE, pacer)
    if not loaded.get("ok"):
        return loaded
    chosen = opt.choose_build(loaded["rows"], mc, version, preview)
    if not chosen.get("ok"):
        return chosen
    row = chosen["row"]
    name, err = _target_name(row["file_name"], wanted_name)
    if err:
        return err
    target, part = folder / name, folder / (name + ".part")
    if os.path.lexists(target):
        return _fail("exists", "%s already exists in %s; nothing was downloaded. Delete or rename it, or give another "
                               "filename." % (name, folder), path=str(target))
    if os.path.lexists(part):
        return _fail("exists", "%s is already there (a download of it may be running, or an earlier one failed). Delete "
                               "it and ask again; nothing was downloaded." % part, path=str(part))

    label = "OptiFine %s for Minecraft %s" % (row["version_name"], row["mc_version"])
    shown = opt.public_row(row)
    if dry:
        return {"ok": True, "dry_run": True, "route": "direct", "source": "optifine.net", "would_download": shown,
                "path": str(target), "dest": str(folder), "official_page": OFFICIAL_PAGE,
                "message": ("Dry run: would download %s%s from optifine.net to %s. Only the version list was fetched; "
                            "nothing was written." % (label, " (a preview build)" if row["preview"] else "", target))}

    if not _confirm_outside(folder, name):
        return _fail("bad_dest", "Not confirmed: nothing was downloaded.", cancelled=True)
    link = opt.resolve_final_link(row["file_name"], fetch=FETCH, pacer=pacer)
    if not link.get("ok"):
        return link
    got = opt.fetch_jar(link["url"], row["file_name"], fetch=FETCH, pacer=pacer, max_bytes=_download_cap())
    if not got.get("ok"):
        return got
    good, why = opt.check_jar_shape(got["data"], row["file_name"], served_name=got.get("served_name"))
    if not good:
        return _fail("verify_failed", "The download didn't check out: %s. Nothing was written." % why)
    path, err = _write_jar(folder, name, got["data"], row["file_name"])
    if err:
        return err
    out = dict(shown)
    out.update({
        "ok": True, "downloaded": True, "installed": False, "route": "direct", "source": "optifine.net",
        "path": str(path), "dest": str(folder), "size": len(got["data"]), "official_page": OFFICIAL_PAGE,
        "checked": "ZIP signature, readable ZIP, plausible size and the expected name (OptiFine publishes no checksum)",
        "message": ("Downloaded %s%s to %s (%s). Downloaded only: Jarvis never runs the jar (it is OptiFine's installer) "
                    "and nothing was installed." % (label, " (a preview build)" if row["preview"] else "", path,
                                                    _size_text(len(got["data"])))),
    })
    if wanted_name not in (None, "") and name != row["file_name"]:
        out["saved_as"] = name
    return out


# --------------------------------------------------------------------------- registry

TOOL_SCHEMAS = [
    {"name": "list_optifine_versions",
     "description": ("List the OptiFine builds on the official site (optifine.net), newest first, the recommended build of "
                     "each Minecraft version marked. Read-only. Without mc_version: one recommended build per Minecraft "
                     "version. With it: every build of that version. Previews are test builds and are only listed with "
                     "include_preview. The owner's own tool."),
     "parameters": {"type": "object", "properties": {
         "mc_version": {"type": "string", "description": "A Minecraft version like 1.20.1 or 26.3 (omit for the overview)."},
         "include_preview": {"type": "boolean", "description": "Also list preview (test) builds. Default false."},
         "limit": {"type": "integer", "description": "How many to list (default 30; 0 = all)."}}}},
    {"name": "download_optifine",
     "description": ("Download ONE OptiFine jar from the official site (optifine.net) into a folder. Confirmed with the owner "
                     "first, every time. The version defaults to the recommended non-preview build for mc_version; give "
                     "version (like HD_U_I6) for another, preview=true for a preview (test) build. dest is a folder "
                     "(omitted = the owner's default download folder). Downloaded only: the jar is OptiFine's installer, "
                     "it is never run and nothing is installed, so say 'downloaded', never 'installed'. If the site "
                     "changed and the download can't be followed, the answer says so and gives the official page; never "
                     "suggest another site."),
     "parameters": {"type": "object", "properties": {
         "mc_version": {"type": "string", "description": "The Minecraft version, like 1.20.1 or 26.3."},
         "version": {"type": "string", "description": "An OptiFine build like HD_U_I6 (omit for the recommended one)."},
         "preview": {"type": "boolean", "description": "Take a preview (test) build instead. Default false."},
         "dest": {"type": "string", "description": "Folder to save into, a full path like D:/Minecraft/mods (default: the owner's default download folder)."},
         "filename": {"type": "string", "description": "Save under another name (must end in .jar). Default: the official name."},
         "dry_run": {"type": "boolean", "description": "Say what would be downloaded and where; write nothing."}},
         "required": ["mc_version"]}},
]

TOOLS = {"list_optifine_versions": tool_list_optifine_versions, "download_optifine": tool_download_optifine}
TOOL_GROUP = "minecraft"

TOOL_KEYWORDS = {
    "list_optifine_versions": {"optifine versions": 10, "list optifine": 10, "optifine builds": 9, "which optifine": 9,
                               "latest optifine": 8, "recommended optifine": 8, "optifine for what": 6,
                               "optifine preview": 6, "optifine releases": 8},
    "download_optifine": {"download optifine": 10, "get optifine": 10, "optifine jar": 9, "optifine installer": 8,
                          "optifine into": 8, "optifine hd u": 9, "install optifine": 7},
}

# It writes a file into a folder the owner names: asked first, every time (dry runs too: the framework asks per call).
TOOL_CONFIRM_REQUIRED = {"download_optifine"}
TOOL_AI_REVIEW = set()

TOOL_RESULT_SPECS = {
    "list_optifine_versions": {
        "drop_fields": {"medium": [], "low": ["note", "page", "previews_note"]},
        "list_item_drop": {"versions": {"medium": [], "low": ["forge", "date"]}},
    },
    "download_optifine": {
        "drop_fields": {"medium": ["checked", "official_page"], "low": ["checked", "official_page", "source", "route", "dest"]},
        "list_item_drop": {"candidates": {"medium": ["forge"], "low": ["forge", "date"]}},
    },
}

TEST_CHECKLIST_GROUP = {
    "minecraft_optifine": {
        "label": "Minecraft: OptiFine",
        "blurb": "List the OptiFine builds on optifine.net and download one jar: list_optifine_versions, download_optifine. Official site only, downloaded never installed.",
    },
}

TEST_CHECKLIST = {
    "list_optifine_versions": {
        "group": "minecraft_optifine",
        "does": "Lists OptiFine builds from the official site, newest first, the recommended build of each Minecraft version marked.",
        "steps": [
            {"run": {"mc_version": "1.20.1"}, "expect": "ok: true; HD U I6 first with recommended: true, then the older builds (I5 ...), preview: false on all. Compare with optifine.net/downloads."},
            {"run": {"mc_version": "1.20.1", "include_preview": True}, "expect": "the same, and any preview builds of 1.20.1 as well (preview: true, a name ending in preN)."},
            {"run": {}, "expect": "one recommended build per Minecraft version, newest Minecraft first; a Minecraft version that only has previews is named in previews_note, not listed."},
            {"run": {"mc_version": "99.9"}, "expect": "ok: false, reason not_found, with the Minecraft versions the site does list."},
            {"ask": "Which OptiFine versions exist for Minecraft 1.20.1?", "expect": "Calls list_optifine_versions and names the recommended build; previews only if you ask for them."},
        ],
        "needs": ["internet access to optifine.net"],
        "watch": ["Only optifine.net is contacted; nothing is downloaded.", "Version names, dates and Forge text come from the site and are shown as data.", "From a remote chat guest it is refused before anything is read: 'list_optifine_versions is the owner's: someone in a chat can't use it'; a guest is not even offered the tool."],
    },
    "download_optifine": {
        "group": "minecraft_optifine",
        "does": "Downloads ONE OptiFine jar from optifine.net into a folder you name (default: your Downloads folder). Downloaded, never installed or run.",
        "steps": [
            {"run": {"mc_version": "1.20.1", "version": "HD_U_I6", "dry_run": True}, "expect": "asks first (every call does); then ok: true, dry_run: true, would_download OptiFine_1.20.1_HD_U_I6.jar and the full path. Nothing is written."},
            {"ask": "Get OptiFine 1.20.1 HD U I6 into <an empty test folder>", "expect": "asks to confirm the tool, then 'Save outside the downloads library?'; after both: ok: true, downloaded: true, installed: false; OptiFine_1.20.1_HD_U_I6.jar is in that folder, no .part file is left, and the reply says downloaded, not installed. It takes a few seconds (the site is paced politely)."},
            {"run": {"mc_version": "1.20.1", "dest": "<the same folder>"}, "expect": "ok: false, reason exists: the jar is not overwritten and nothing is downloaded."},
            {"run": {"mc_version": "1.20.1", "version": "I6", "dest": "<an empty test folder>", "preview": True}, "expect": "ok: false, reason not_found (I6 is not a preview build); nothing is written."},
            {"run": {"mc_version": "99.9", "dest": "<an empty test folder>"}, "expect": "ok: false, reason not_found; nothing is written."},
            {"run": {"mc_version": "1.20.1", "dest": "~/.jarvis"}, "expect": "ok: false, reason bad_dest (Jarvis's own folder); nothing is written. The same for C:/Windows or a drive root."},
            {"run": {"mc_version": "1.20.1", "dest": "relative/folder"}, "expect": "ok: false, reason bad_dest: it must be a full path."},
        ],
        "needs": ["internet access to optifine.net", "a folder you can write to"],
        "care": "Writes a jar into the folder you name. The jar is OptiFine's installer: Jarvis never runs it, you do.",
        "watch": ["Only optifine.net is contacted (never adfoc.us or a mirror site), one file per call.", "If optifine.net changes its pages the answer is reason vendor_changed with the official page link, and nothing is written.", "The only check is the jar's shape (a readable ZIP of plausible size with the expected name): OptiFine publishes no checksum.", "From a remote chat guest it is refused before anything happens: 'download_optifine is the owner's: someone in a chat can't use it'; a guest is not even offered the tool."],
    },
}
