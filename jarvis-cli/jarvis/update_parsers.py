"""Pure parsers for the built-in update sources (H3): what the package managers print when they list what is
out of date, and the one rule that reads a Steam manifest.

NO I/O HERE.  No file, no child process, no network, no clock, no import of anything but `json` and `re`
(tests/test_update_sources_pkg.py checks that, and loads this file on its own).  Every function takes TEXT (or
a plain dict) and returns plain data, so the owner can paste real output into a test and see what is read.

!! WARNING: EVERY PARSER BELOW IS A GUESS UNTIL A REAL RUN HAS BEEN READ. !!
Nothing in this round ran a real winget, Chocolatey, Scoop, pip, pipx or npm, and no real Steam.  The formats
were written from memory of how those programs print, then checked against hand-written text only.  Each parser
carries a "WARNING: guessed format" line.  The raw output the owner's probe records
(tests/package_update_checks.py, tests/steam_update_checks.py) is what to compare them with; if a real
output does not parse, paste it into a test and fix only the one parser.

The shape every `parse_*` function answers is the same pair, `(items, issues)`:

  items   [{"id", "title", "installed", "available", "detail": [{"label", "value"}]}]
          `id` is the package id exactly as the manager printed it; `installed` and `available` are the
          manager's own words.  Versions are NEVER compared here (only "the two texts are identical" is used,
          to drop a row that is not an update).
  issues  [str]  empty = the text was read whole.  Anything in it means "do not claim the list is complete":
          the caller turns it into a problem and sets `complete: false`.  An empty answer is never silently
          "all up to date" unless the manager is one whose empty answer means exactly that (documented below).

Provider text is untrusted: every cell is cleaned (control characters out, one line) and capped here, and the
caller caps it a second time.
"""

import json
import re

MAX_ID = 120            # hub/updates.py clean_finding keeps 120 characters of an id: a longer one could not be applied
MAX_TITLE = 100
MAX_VERSION = 60
MAX_CELL = 160

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
_SPACES = re.compile(r"\s+")


def clean_text(value, limit=MAX_CELL):
    """Untrusted text -> one short printable line (control characters out, white space collapsed, capped)."""
    text = _SPACES.sub(" ", _CTRL.sub(" ", "" if value is None else str(value))).strip()
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def first_line(text, limit=120):
    """The first non-empty line of a program's output, cleaned (for 'it failed: <this>')."""
    for raw in str(text or "").replace("\r", "\n").split("\n"):
        line = clean_text(raw, limit)
        if line:
            return line
    return ""


def normalize_name(name):
    """PEP 503 style name: runs of - _ . become one -, lower case.  'Typing_Extensions' == 'typing-extensions'."""
    return re.sub(r"[-_.]+", "-", str(name or "").strip()).lower()


# --------------------------------------------------------------------------- package ids

# The hub's apply tool is given an id by the owner's click, by a stored finding or by a model in chat.  pkg_tools'
# own rule (letters, digits, . _ + - @ /) is the FIRST gate (the tool runs it too); these are stricter per manager,
# because `/` and `@` are harmless for winget or pip ids but mean something to npm (`owner/repo` installs from
# GitHub) and to pip (`name@url`).  An id that fails here is refused, whatever else is true.
_ID_RES = {
    "winget": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$"),
    "choco": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$"),
    "scoop": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$"),
    "pip": re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?$"),
    "pipx": re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?$"),
    "npm": re.compile(r"^(?:@[A-Za-z0-9][A-Za-z0-9._\-]*/)?[A-Za-z0-9][A-Za-z0-9._\-]*$"),
}


def valid_id(manager, package_id):
    """True when `package_id` is an id this manager's upgrade may be given (see the comment above)."""
    rule = _ID_RES.get(manager)
    text = package_id if isinstance(package_id, str) else ""
    return bool(rule and text and len(text) <= MAX_ID and rule.match(text))


# --------------------------------------------------------------------------- shared helpers

def _lines(text):
    """Text -> lines.  A progress spinner redraws with a bare carriage return: keep what was drawn LAST."""
    out = []
    for raw in str(text or "").replace("\r\n", "\n").split("\n"):
        pieces = [p for p in raw.split("\r") if p.strip()]
        out.append((pieces[-1] if pieces else "").rstrip())
    return out


def _item(package_id, title, installed, available, detail=None):
    return {
        "id": package_id,
        "title": clean_text(title or package_id, MAX_TITLE) or package_id,
        "installed": clean_text(installed, MAX_VERSION),
        "available": clean_text(available, MAX_VERSION),
        "detail": [{"label": clean_text(k, 60), "value": clean_text(v, MAX_CELL)} for k, v in (detail or []) if k],
    }


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    return out


# --------------------------------------------------------------------------- winget
# WARNING: guessed format.  `winget upgrade` prints an aligned table:
#
#   Name          Id            Version  Available  Source
#   -----------------------------------------------------
#   Git           Git.Git       2.43.0   2.44.0     winget
#   3 upgrades available.
#
# plus (maybe) a second table headed "The following packages have an upgrade available, but require explicit
# targeting for upgrade:", spinner characters drawn with carriage returns, and, when nothing is outdated, a
# sentence ("No installed package found matching input criteria.").  Columns are found from the header's own
# offsets.  A long cell is cut with an ellipsis by winget when it is narrow: an id cut that way is NOT usable, so
# that row is reported as an issue (never guessed), as is a row whose columns do not line up (wide characters).

_WINGET_NONE = re.compile(r"no (?:installed package|applicable upgrade|available upgrade|newer package)", re.I)
_WINGET_FOOTER = re.compile(r"^\s*(?:\d+\s+upgrades?\s+available|\d+\s+package\(s\)|the following packages)", re.I)
_DASHES = re.compile(r"^[-\s]{5,}$")


def parse_winget_upgrade(text):
    """WARNING: guessed format (see above).  -> (items, issues)."""
    lines = _lines(text)
    items, issues, tables, i = [], [], 0, 0
    while i < len(lines):
        head = lines[i]
        m_id, m_ver, m_av = (re.search(r"\b%s\b" % w, head) for w in ("Id", "Version", "Available"))
        if not (m_id and m_ver and m_av and m_id.start() < m_ver.start() < m_av.start()):
            i += 1
            continue
        tables += 1
        m_src = re.search(r"\bSource\b", head)
        id_col, ver_col, av_col = m_id.start(), m_ver.start(), m_av.start()
        src_col = m_src.start() if m_src and m_src.start() > av_col else None
        i += 1
        if i < len(lines) and _DASHES.match(lines[i]):
            i += 1
        while i < len(lines):
            row = lines[i]
            if not row.strip() or _WINGET_FOOTER.match(row):
                break
            i += 1
            name = row[:id_col].strip()
            pid = row[id_col:ver_col].strip()
            installed = row[ver_col:av_col].strip()
            available = (row[av_col:src_col] if src_col else row[av_col:]).strip()
            source = row[src_col:].strip() if src_col else ""
            if "…" in pid or pid.endswith("..."):
                issues.append("winget cut an id short in its table, so that package was left out")
                continue
            if not pid or " " in pid or not available:
                issues.append("a row of winget's table did not line up and was skipped (%s)" % (clean_text(name, 40) or "no name"))
                continue
            if len(pid) > MAX_ID:
                issues.append("an id longer than %d characters was left out" % MAX_ID)
                continue
            if installed == available:
                continue
            detail = [("Source", source)] if source else []
            if tables > 1:
                detail.append(("Note", "winget lists it apart, as needing explicit targeting"))
            items.append(_item(pid, name, installed, available, detail))
    if not tables:
        body = " ".join(l.strip() for l in lines if len(l.strip()) > 1)
        if _WINGET_NONE.search(body):
            return [], []
        return [], ["winget printed nothing Jarvis can read as a list" if not body else "winget's list was not a table Jarvis knows"]
    return _dedupe(items), _dedupe_issues(issues)


def _dedupe_issues(issues):
    out = []
    for text in issues:
        if text not in out:
            out.append(text)
    return out


# --------------------------------------------------------------------------- Chocolatey
# WARNING: guessed format.  `choco outdated -r` (limit output) prints one line per package:
#
#   git|2.43.0|2.44.0|false
#
# id | installed | available | pinned.  Without -r a header "Output is package name | current version | ..." and a
# summary sentence come too; both are skipped.  A PINNED package is left out on purpose: the owner pinned it, and
# `choco upgrade` would refuse it.  An empty answer means "nothing is outdated" (that is how -r prints it).

def parse_choco_outdated(text):
    """WARNING: guessed format (see above).  -> (items, issues)."""
    items, issues = [], []
    for line in _lines(text):
        s = line.strip()
        if "|" not in s or s.lower().startswith("output is"):
            continue
        parts = [p.strip() for p in s.split("|")]
        pid = parts[0]
        if len(parts) < 3 or not parts[1] or not parts[2]:
            issues.append("a line of choco's list was not 'id|installed|available' and was skipped")
            continue
        if not valid_id("choco", pid):
            issues.append("a package id Jarvis cannot use was skipped (%s)" % clean_text(pid, 40))
            continue
        if len(parts) > 3 and parts[3].lower() == "true":
            continue
        if parts[1] == parts[2]:
            continue
        items.append(_item(pid, pid, parts[1], parts[2]))
    return _dedupe(items), _dedupe_issues(issues)


# --------------------------------------------------------------------------- Scoop
# WARNING: guessed format.  Current `scoop status`:
#
#   Name   Installed Version  Latest Version  Missing Dependencies  Info
#   ----   -----------------  --------------  --------------------  ----
#   7zip   23.01              24.05
#   Everything is ok!                      <- when nothing is outdated
#
# Older Scoop printed "Updates are available for:" and lines like `    git: 2.43.0 -> 2.44.0`.  Both are read.  A row
# with no Latest Version (Info says "Install failed", "Removed by upstream") is not an update; a row whose Info
# says "Held package" is left out like a pinned choco package.

_SCOOP_HEAD = re.compile(r"^\s*Name\s+Installed Version\s+Latest Version")
_SCOOP_ARROW = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*):\s*(\S+)\s*->\s*(\S+)")


def parse_scoop_status(text):
    """WARNING: guessed format (see above).  -> (items, issues)."""
    lines = _lines(text)
    items, issues, recognised, i = [], [], False, 0
    while i < len(lines):
        head = lines[i]
        arrow = _SCOOP_ARROW.match(head)
        if arrow:
            recognised = True
            name, old, new = arrow.groups()
            if old != new:
                items.append(_item(name, name, old, new))
            i += 1
            continue
        if not _SCOOP_HEAD.match(head):
            if re.search(r"everything is ok|up to date", head, re.I) and "scoop is" not in head.lower():
                recognised = True
            i += 1
            continue
        recognised = True
        iv, lv = head.index("Installed Version"), head.index("Latest Version")
        md = head.find("Missing Dependencies")
        info = head.find("Info")
        stop = md if md > lv else (info if info > lv else None)
        i += 1
        if i < len(lines) and _DASHES.match(lines[i]):
            i += 1
        while i < len(lines) and lines[i].strip():
            row = lines[i]
            i += 1
            name = row[:iv].strip()
            old = row[iv:lv].strip()
            new = (row[lv:stop] if stop else row[lv:]).strip()
            note = row[info:].strip() if info > lv else ""
            if not name:
                continue
            if not new or "held" in note.lower():
                continue
            if not valid_id("scoop", name) or " " in new or " " in old:
                issues.append("a row of scoop's table did not line up and was skipped (%s)" % clean_text(name, 40))
                continue
            if old != new:
                items.append(_item(name, name, old, new))
    if not recognised and not items:
        return [], ["scoop printed nothing Jarvis can read as a status" if not str(text or "").strip()
                    else "scoop's status was not a table Jarvis knows"]
    return _dedupe(items), _dedupe_issues(issues)


# --------------------------------------------------------------------------- pip
# WARNING: guessed format.  `python -m pip list --outdated --format json` prints a JSON array:
#   [{"name": "requests", "version": "2.31.0", "latest_version": "2.32.0", "latest_filetype": "wheel"}]
# Notices ("[notice] A new release of pip ...") go to stderr, but the array is cut out of stdout anyway.  An empty
# stdout means nothing is outdated.

def parse_pip_outdated(text):
    """WARNING: guessed format (see above).  -> (items, issues)."""
    body = str(text or "").strip()
    if not body:
        return [], []
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end < start:
        return [], ["pip's answer was not a JSON list"]
    try:
        data = json.loads(body[start:end + 1])
    except ValueError:
        return [], ["pip's answer was not valid JSON"]
    if not isinstance(data, list):
        return [], ["pip's answer was not a JSON list"]
    items, issues = [], []
    for entry in data:
        if not isinstance(entry, dict):
            issues.append("an entry of pip's list was not an object and was skipped")
            continue
        name = str(entry.get("name") or "").strip()
        new = str(entry.get("latest_version") or "").strip()
        old = str(entry.get("version") or "").strip()
        if not valid_id("pip", name) or not new:
            issues.append("an entry of pip's list had no usable name or latest version and was skipped")
            continue
        if old == new:
            continue
        kind = str(entry.get("latest_filetype") or "").strip()
        items.append(_item(name, name, old, new, [("Latest file type", kind)] if kind else []))
    return _dedupe(items), _dedupe_issues(issues)


def pip_outcome(text, name):
    """After `pip install --upgrade <name>`: ("done", new version or "") | ("unchanged", "") | ("unknown", "").
    WARNING: guessed wording ("Successfully installed requests-2.32.0", "Requirement already satisfied")."""
    body = str(text or "")
    for m in re.finditer(r"Successfully installed ([^\n]+)", body):
        for part in m.group(1).split():
            hit = re.match(r"^(.+)-(\d[^\s]*)$", part)
            if hit and normalize_name(hit.group(1)) == normalize_name(name):
                return "done", hit.group(2)
        return "done", ""
    # "Requirement already satisfied" is printed for every dependency too, so it only means "nothing to do" when
    # pip installed nothing at all (no "Successfully installed" line, checked above)
    if re.search(r"Requirement already (?:satisfied|up-to-date)", body):
        return "unchanged", ""
    return "unknown", ""


# --------------------------------------------------------------------------- npm
# WARNING: guessed format.  `npm outdated -g --json` prints an object keyed by package name:
#   {"npm": {"current": "10.2.3", "wanted": "10.5.0", "latest": "10.5.0", "dependent": "global", "location": "..."}}
# (the value can be a list of such objects when a package is found in two places).  npm exits with code 1 when
# anything is outdated, so the caller accepts code 1 with a readable answer.  `latest` is what `npm install -g
# <name>@latest` fetches, so it is the "available" text.  Empty stdout means nothing is outdated.

def parse_npm_outdated(text):
    """WARNING: guessed format (see above).  -> (items, issues)."""
    body = str(text or "").strip()
    if not body:
        return [], []
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        return [], ["npm's answer was not a JSON object"]
    try:
        data = json.loads(body[start:end + 1])
    except ValueError:
        return [], ["npm's answer was not valid JSON"]
    if not isinstance(data, dict):
        return [], ["npm's answer was not a JSON object"]
    items, issues = [], []
    for name, meta in data.items():
        if isinstance(meta, list):
            meta = next((m for m in meta if isinstance(m, dict)), None)
        if not isinstance(meta, dict):
            issues.append("an entry of npm's list was not an object and was skipped")
            continue
        new = str(meta.get("latest") or meta.get("wanted") or "").strip()
        old = str(meta.get("current") or "").strip()
        if not valid_id("npm", str(name)) or not new:
            issues.append("an entry of npm's list had no usable name or latest version and was skipped")
            continue
        if old == new:
            continue
        wanted = str(meta.get("wanted") or "").strip()
        detail = [("Wanted", wanted)] if wanted and wanted != new else []
        items.append(_item(str(name), str(name), old, new, detail))
    return _dedupe(items), _dedupe_issues(issues)


# --------------------------------------------------------------------------- pipx
# WARNING: guessed format, and the weakest of the six.  `pipx list --outdated` is NOT in every pipx (the tool
# asks `pipx list --help` first).  What it prints was never seen: this parser takes `package NAME VERSION, ...`
# lines (installed only) and `NAME CURRENT -> LATEST` lines.  A line it cannot place is an issue.  Without that
# flag the tool lists the installed apps with `pipx list --json` (parsed below) and says it CANNOT TELL.

def parse_pipx_version(text):
    """`pipx --version` -> '1.4.3' (or '' when no version number is in the text)."""
    m = re.search(r"\d+(?:\.\d+)+", str(text or ""))
    return m.group(0) if m else ""


def pipx_help_has_outdated(text):
    """True when `pipx list --help` mentions --outdated."""
    return "--outdated" in str(text or "")


def parse_pipx_list_json(text):
    """`pipx list --json` -> (apps [{"name", "version"}], issues).  WARNING: guessed format
    ({"venvs": {"black": {"metadata": {"main_package": {"package": "black", "package_version": "24.1.0"}}}}})."""
    try:
        data = json.loads(str(text or "").strip() or "null")
    except ValueError:
        return [], ["pipx's list was not valid JSON"]
    venvs = data.get("venvs") if isinstance(data, dict) else None
    if not isinstance(venvs, dict):
        return [], ["pipx's list had no 'venvs' object"]
    apps = []
    for key, venv in venvs.items():
        main = ((venv or {}).get("metadata") or {}).get("main_package") if isinstance(venv, dict) else None
        main = main if isinstance(main, dict) else {}
        apps.append({"name": clean_text(main.get("package") or key, 80), "version": clean_text(main.get("package_version"), 40)})
    return apps, []


def parse_pipx_outdated(text):
    """WARNING: guessed format (see above).  -> (items, issues)."""
    items, issues = [], []
    for line in _lines(text):
        s = line.strip()
        if not s:
            continue
        arrow = re.match(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)\s+(\S+)\s*(?:->|→)\s*(\S+)$", s)
        listed = re.match(r"^package\s+([A-Za-z0-9][A-Za-z0-9._\-]*)\s+(\S+?),", s)
        if arrow:
            name, old, new = arrow.groups()
            if old != new:
                items.append(_item(name, name, old, new))
        elif listed:
            items.append(_item(listed.group(1), listed.group(1), listed.group(2), ""))
        elif not re.match(r"^(?:venvs are in|apps are exposed|\s*-|nothing|no )", s, re.I):
            issues.append("a line of pipx's outdated list was not understood and was skipped")
    return _dedupe(items), _dedupe_issues(issues)


# --------------------------------------------------------------------------- what an upgrade printed

_ADMIN = re.compile(
    r"administrator|elevated|elevation|run as admin|requires? admin|access (?:to the path .{0,200}?)?is denied"
    r"|permission denied|0x80070005|UAC", re.I)
_NOTHING = re.compile(
    r"no (?:available|applicable) upgrade|no newer package versions|no applicable update|is the latest version"
    r"|already up[- ]to[- ]date|no updates? (?:are )?available", re.I)
# ("already installed" is NOT here on purpose: winget prints "Found an existing package already installed.
# Trying to upgrade" in the middle of a REAL upgrade)


def looks_like_needs_admin(text):
    """True when an upgrade's output reads like 'this needs an elevated session'.  WARNING: guessed wording."""
    return bool(_ADMIN.search(str(text or "")))


def looks_like_nothing_to_do(text):
    """True when an upgrade's output reads like 'already the newest'.  WARNING: guessed wording."""
    return bool(_NOTHING.search(str(text or "")))


# --------------------------------------------------------------------------- what Jarvis itself requires

def parse_pyproject_requirement_names(text):
    """Package names out of the text of jarvis-cli/pyproject.toml: `dependencies` and every
    `[project.optional-dependencies]` list.  Names come back normalised.  The self-reference extras
    (`jarvis-cli[voice-audio,...]`) are not packages and are dropped."""
    names, section, in_list = set(), "", False
    for raw in str(text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("["):
            section = line.strip("[] ").lower()
            in_list = False
            continue
        if section == "project" and re.match(r"^dependencies\s*=\s*\[", line):
            in_list = True
        elif section == "project.optional-dependencies" and re.match(r"^[A-Za-z0-9_\-]+\s*=\s*\[", line):
            in_list = True
        elif not in_list:
            continue
        for quoted in re.findall(r'"([^"]+)"', line):
            m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)", quoted)
            if m and not normalize_name(m.group(1)).startswith("jarvis-cli"):
                names.add(normalize_name(m.group(1)))
        # a quoted requirement can hold "]" (`requests[socks]`): look for the list's own closing bracket
        if "]" in re.sub(r'"[^"]*"', "", line):
            in_list = False
    return names


def parse_requirement_name(requirement):
    """'requests>=2.25' / 'colorama; platform_system==...' -> ('requests', has_extra_marker)."""
    text = str(requirement or "")
    m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)", text)
    return (normalize_name(m.group(1)) if m else ""), bool(re.search(r"extra\s*==", text))


# --------------------------------------------------------------------------- Steam
# WARNING: the rule below is a guess.  `hub/steam.py` says plainly that no "update pending" signal has been
# measured on a real Steam: bit 2 (UpdateRequired) was seen on games that were idle, and `TargetBuildID` was never
# recorded.  So the rule is cautious and follows what that module already trusts:
#
#   not installed (no LastUpdated, no SizeOnDisk)            -> "not_installed"  (an install, not an update)
#   Steam is fetching or staging it, or has it paused WITH
#     bytes                                                  -> "fetching"       (Downloads shows it already)
#   TargetBuildID is set, not 0, and differs from buildid    -> "pending"        (the strongest sign; unmeasured)
#   UpdateRequired (2) set                                   -> "pending", UNLESS Steam also set 512 (paused)
#                                                               with every Bytes* field 0 or absent: that is a
#                                                               background update Steam queued and runs by itself
#                                                               (hub/steam.py saw two runtimes sit at 518 for
#                                                               minutes and finish with 0 bytes)
#   anything else                                            -> "current"
#
# Bits: 2 UpdateRequired, 4 FullyInstalled, 256 UpdateRunning, 512 UpdatePaused, 1024 UpdateStarted,
# 1048576 Downloading, 2097152 Staging, 4194304 Committing.  Copies of hub/steam.py's numbers on purpose (this
# module imports nothing); tests/test_update_sources_steam.py compares them with the real ones.

STEAM_UPDATE_REQUIRED = 2
STEAM_UPDATE_PAUSED = 512
STEAM_FETCHING_MASK = 256 | 1024 | 1048576 | 2097152 | 4194304


def _int(value):
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _build(value):
    """A build id as Steam wrote it: '' for missing or all zeros, else the text without leading zeros."""
    text = clean_text(value, 40)
    return "" if not text.strip("0") else text.lstrip("0") or text


def steam_update_state(fields):
    """One manifest's fields (a dict of what the .acf said, keys in any case) -> a dict:
    {"state": "pending" | "fetching" | "current" | "not_installed" | "unreadable", "reason": str, "appid": str,
     "flags": int, "installed": str, "available": str, "size_bytes": int | None}.
    WARNING: a guessed rule (see the block above)."""
    f = {str(k).lower(): v for k, v in (fields or {}).items() if not isinstance(v, (dict, list))}
    appid = str(f.get("appid", "")).strip()
    flags = _int(f.get("stateflags"))
    out = {"state": "unreadable", "reason": "", "appid": appid, "flags": flags, "installed": clean_text(f.get("buildid"), 40),
           "available": "", "size_bytes": None}
    if not appid.isdigit():
        out["reason"] = "the manifest has no app id"
        return out
    to_dl, got = _int(f.get("bytestodownload")), _int(f.get("bytesdownloaded"))
    to_stage, staged = _int(f.get("bytestostage")), _int(f.get("bytesstaged"))
    if _int(f.get("lastupdated")) <= 0 and _int(f.get("sizeondisk")) <= 0:
        out.update(state="not_installed", reason="not installed yet: a first install is not an update")
        return out
    quiet = to_dl <= 0 and got <= 0 and to_stage <= 0 and staged <= 0
    if flags & STEAM_FETCHING_MASK:
        out.update(state="fetching", reason="Steam is fetching it now")
        return out
    if flags & STEAM_UPDATE_PAUSED and not quiet:
        out.update(state="fetching", reason="Steam has it paused with a download in progress")
        return out
    installed, target = _build(f.get("buildid")), _build(f.get("targetbuildid"))
    if to_dl > 0:
        out["size_bytes"] = to_dl
    if target and installed and target != installed:
        out.update(state="pending", reason="Steam's TargetBuildID differs from the installed buildid", available=target)
        return out
    if flags & STEAM_UPDATE_REQUIRED:
        if flags & STEAM_UPDATE_PAUSED and quiet:
            out.update(state="current", reason="a background update Steam queued and runs by itself")
            return out
        out.update(state="pending", reason="Steam's UpdateRequired flag is set", available=target if target != installed else "")
        return out
    out.update(state="current", reason="no update flag and no newer build id")
    return out
