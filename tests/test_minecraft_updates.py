"""modhosts/updates.py and actions/minecraft_updates.py: `check_mc_updates` (brief S2), against an in-process fake Modrinth.

Covers:
  * registration: TOOL_GROUP "minecraft", keywords of weight 5 or more that collide with no other Minecraft tool, a name the
    risk scorer reads as a read (starts with check_, none of install/write/edit/create/run/launch/exec/delete/remove/reset),
    owner-only (the name AND download_mc_file and make_modpack_file are in tools.OWNER_ONLY_TOOLS), no pack instruction and no
    setting declared here, its own checklist section, a schema that matches the code, result specs that keep the message;
  * a guest (and an unreadable sender) is refused BEFORE any file is read, any request is made or any state is written;
  * the folder rules: ~/.jarvis, the home folder, a drive root, an OS folder, '..', a relative path, a missing folder, a file,
    a link to a folder, a link into ~/.jarvis: all `bad_dest` with no request;
  * the scan: ONE level only; links, hidden files, sub-folders, other extensions and unreadable files are counted and never
    sent; a file inside a sub-folder is never even opened;
  * matching by CONTENT: a renamed jar still matches, a look-alike name with other bytes does not, a lying answer (a version that
    does not list the hash) is not a match; only hashes, the algorithm and the filters ever leave the PC, only to Modrinth;
  * updates: newer files, up to date, the channel rules (a beta is not offered to a release file; a beta or alpha file also sees
    betas / alphas), the fallback listing, resource packs and shaders (their own loaders), the choice of Minecraft version;
  * never a guess: a file that works on several versions / loaders is `needs_mc_version` / `needs_loader` and the answer asks;
    a file built for another version or loader is `other_version`, never `up_to_date`;
  * Quilt falls back to Fabric (labelled `via`), and the setting switches it off;
  * a file Modrinth does not know (a CurseForge file) is "not checked" with the reason, never guessed, no other site contacted;
  * failures: rate limited / offline / changed answer for the whole check, a failing update or listing call for the files it
    covers, the names call failing; the `deadline_seconds` setting (partial, no request after it);
  * the state file: written under HOME, a hash cache that spares unchanged files, a changed file is re-hashed, a corrupt file is
    ignored, the last folder is reused (and re-validated), entries of deleted files go, a size cap;
  * the folder is never changed by a check; vendor text is clipped, stripped and marked untrusted (an instruction-like version
    name is flagged); a tool never raises; result shaping keeps the message, the note and what was not checked.

No network.  Run: python tests/test_minecraft_updates.py
"""
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOME = tempfile.mkdtemp(prefix="jarvis-minecraft-updates-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER",
             "JARVIS_TOOL_MAKER_CONTEXT_FILE", "CURSEFORGE_API_KEY"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import checklist_schema, policy, tool_result_shaping, tool_safety, tools  # noqa: E402
from jarvis.actions import minecraft_download as dl  # noqa: E402
from jarvis.actions import minecraft_lookup as lk  # noqa: E402
from jarvis.actions import minecraft_optifine as of  # noqa: E402
from jarvis.actions import minecraft_packs as pk  # noqa: E402
from jarvis.actions import minecraft_updates as mu  # noqa: E402
from jarvis.modhosts import REASONS, net, sources, untrusted, updates  # noqa: E402

PASS, FAIL = [], []
FAKE_KEY = "FAKE-KEY-0123456789abcdef"
GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})
CATALOG_TEXT = (ROOT / "tests" / "data" / "modhosts" / "hand_s2_catalog.json").read_text(encoding="utf-8")
HOMEP = Path(HOME)
_n = [0]


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


def sha1(seed):
    return hashlib.sha1(seed.encode()).hexdigest()


def _fill(match):
    kind, seed = match.group(1), match.group(2)
    return hashlib.new("sha1" if kind == "seed" else "sha512", seed.encode()).hexdigest()


# ------------------------------------------------------------------ the fake Modrinth

class Fake:
    """A net.TRANSPORT that answers the four Modrinth routes the update check uses, from hand_s2_catalog.json.
    `calls` records every request; `fail` maps a path fragment to (status, headers, body); `lie` maps a hash to the version
    id Modrinth wrongly answers for it; `boom` makes the transport raise."""

    def __init__(self):
        doc = json.loads(re.sub(r"@(seed|seed512):([^\"]+)", _fill, CATALOG_TEXT))
        self.projects = {p["id"]: p for p in doc["projects"]}
        self.versions = doc["versions"]
        self.by_id = {v["id"]: v for v in self.versions}
        self.by_hash = {f["hashes"]["sha1"]: v for v in self.versions for f in v["files"]}
        self.calls, self.fail, self.lie, self.boom = [], {}, {}, False

    def __enter__(self):
        self.saved = (net.TRANSPORT, net.RESOLVER, net.CONTACT_PROVIDER, net.SLEEP)
        net.TRANSPORT = self.transport
        net.RESOLVER = lambda host, port: ["93.184.216.34"]
        net.CONTACT_PROVIDER = lambda: "github.com/GoatAssim"
        net.SLEEP = lambda seconds: None
        return self

    def __exit__(self, *exc):
        net.TRANSPORT, net.RESOLVER, net.CONTACT_PROVIDER, net.SLEEP = self.saved

    @staticmethod
    def _reply(obj, status=200, headers=None):
        h = {"content-type": "application/json"}
        h.update(headers or {})
        return net.Reply(status, h, json.dumps(obj))

    def paths(self, method, route):
        """Requests of one method to exactly /v2<route> (so /version_files does not also match /version_files/update)."""
        return [c for c in self.calls if c["method"] == method and c["path"] == "/v2" + route]

    def transport(self, method, url, headers, json_body, timeout):
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        self.calls.append({"method": method, "host": parts.hostname, "path": parts.path, "query": query,
                           "json": copy.deepcopy(json_body), "headers": dict(headers), "url": url})
        if self.boom:
            raise RuntimeError("the fake network blew up")
        for fragment, (status, hdrs, body) in self.fail.items():
            if fragment in parts.path:
                return self._reply(body, status, hdrs)
        path = parts.path
        if parts.hostname != "api.modrinth.com":
            return self._reply({}, 404)
        if method == "POST" and path == "/v2/version_files":
            out = {}
            for h in json_body.get("hashes") or []:
                version = self.by_id.get(self.lie[h]) if h in self.lie else self.by_hash.get(h)
                if version:
                    out[h] = version
            return self._reply(out)
        if method == "POST" and path == "/v2/version_files/update":
            loaders, mcs = set(json_body.get("loaders") or []), set(json_body.get("game_versions") or [])
            out = {}
            for h in json_body.get("hashes") or []:
                have = self.by_hash.get(h)
                if not have:
                    continue
                pool = [v for v in self.versions if v["project_id"] == have["project_id"]
                        and loaders & set(v["loaders"]) and mcs & set(v["game_versions"])]
                if pool:
                    out[h] = max(pool, key=lambda v: v["date_published"])
            return self._reply(out)
        m = re.match(r"^/v2/project/([A-Za-z0-9]+)/version$", path)
        if method == "GET" and m:
            loaders, mcs = set(json.loads(query.get("loaders", "[]"))), set(json.loads(query.get("game_versions", "[]")))
            pool = [v for v in self.versions if v["project_id"] == m.group(1)
                    and (not loaders or loaders & set(v["loaders"])) and (not mcs or mcs & set(v["game_versions"]))]
            return self._reply(sorted(pool, key=lambda v: v["date_published"], reverse=True))
        if method == "GET" and path == "/v2/projects":
            ids = json.loads(query.get("ids", "[]"))
            return self._reply([self.projects[i] for i in ids if i in self.projects])
        return self._reply({}, 404)


class Sender:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get("JARVIS_CHANNEL_SENDER")
        if self.value is None:
            os.environ.pop("JARVIS_CHANNEL_SENDER", None)
        else:
            os.environ["JARVIS_CHANNEL_SENDER"] = self.value

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("JARVIS_CHANNEL_SENDER", None)
        else:
            os.environ["JARVIS_CHANNEL_SENDER"] = self.old


class Patch:
    """Replace attributes on a module for a `with` block."""

    def __init__(self, obj, **changes):
        self.obj, self.changes = obj, changes

    def __enter__(self):
        self.old = {k: getattr(self.obj, k) for k in self.changes}
        for k, v in self.changes.items():
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.obj, k, v)


def fresh(files=None):
    """A new folder under the temp HOME holding {name: seed}; returns its Path."""
    _n[0] += 1
    folder = HOMEP / ("mods%d" % _n[0])
    folder.mkdir()
    for name, seed in (files or {}).items():
        (folder / name).write_bytes(seed.encode())
    return folder


def state_file():
    return HOMEP / ".jarvis" / "modhosts" / "update_state.json"


def wipe_state():
    try:
        state_file().unlink()
    except OSError:
        pass


def snapshot(folder):
    out = []
    for dirpath, dirnames, filenames in os.walk(str(folder)):
        for name in sorted(dirnames + filenames):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out.append((os.path.relpath(p, str(folder)), st.st_size, st.st_mtime_ns))
    return sorted(out)


def run(folder, **kw):
    """The module function with HOME's defaults; returns its dict."""
    kw.setdefault("default_folder", str(HOMEP / "Downloads"))
    return updates.check_folder(folder=str(folder) if folder is not None else None, **kw)


def by_file(res, key):
    return {e["file"]: e for e in res.get(key, [])}


def try_symlink(target, link):
    try:
        os.symlink(str(target), str(link), target_is_directory=os.path.isdir(str(target)))
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


BIG = {  # name on disk -> content seed (the catalog's own seeds: "<slug>-<number>")
    "sodiumish-0.5.0.jar": "sodiumish-0.5.0", "lithiumish-1.0.0.jar": "lithiumish-1.0.0",
    "dualish-2.0.0.jar": "dualish-2.0.0", "fabonly-1.0.0.jar": "fabonly-1.0.0",
    "betaish-2.0.0-beta.1.jar": "betaish-2.0.0-beta.1", "relish-1.0.0.jar": "relish-1.0.0",
    "onlybeta-1.0.0.jar": "onlybeta-1.0.0", "alphaish-1.0.0-alpha.1.jar": "alphaish-1.0.0-alpha.1",
    "looky-1.0.0.zip": "looky-1.0.0", "shadery-1.0.0.zip": "shadery-1.0.0",
    "from-curseforge.jar": "bytes-modrinth-has-never-seen",
}
FAB = {"mc_version": "1.20.1", "loader": "fabric"}


# ------------------------------------------------------------------ registration

def test_registration_and_contract():
    name = "check_mc_updates"
    check("the tool is loaded", name in tools._tool_index())
    check("only this tool lives in the file", list(mu.TOOLS) == [name])
    check("group is minecraft", mu.TOOL_GROUP == "minecraft" and name in tools.AUTO_TOOL_GROUPS.get("minecraft", []),
          tools.AUTO_TOOL_GROUPS.get("minecraft"))
    kws = mu.TOOL_KEYWORDS[name]
    check("keywords, every weight at least 5", kws and all(isinstance(w, int) and w >= 5 for w in kws.values()))
    check("discovery took the keywords", name in tools.AUTO_TOOL_KEYWORDS)
    others = set()
    for other, entry in tools.AUTO_TOOL_KEYWORDS.items():
        if other != name and other in tools.AUTO_TOOL_GROUPS.get("minecraft", []):
            others |= set(entry)
    for module in (lk, dl, of, pk):
        for entry in module.TOOL_KEYWORDS.values():
            others |= set(entry)
    check("no keyword is shared with another minecraft tool", not (set(kws) & others), sorted(set(kws) & others))
    check("no forbidden word in the name",
          not [w for w in ("install", "write", "edit", "create", "run", "launch", "exec", "delete", "remove", "reset") if w in name])
    check("the risk scorer reads it as a read", name.startswith("check_") and policy._base_risk(name) == 5, policy._base_risk(name))
    check("a read: no confirmation, no AI review", mu.TOOL_CONFIRM_REQUIRED == set() and mu.TOOL_AI_REVIEW == set()
          and name not in tool_safety.DEFAULT_CONFIRM_REQUIRED)
    check("no second pack instruction and no setting declared here", not hasattr(mu, "TOOL_PACK_INSTRUCTION") and not hasattr(mu, "TOOL_SETTINGS"))
    check("the group still has lookup's ONE instruction", tools.AUTO_TOOL_PACK_INSTRUCTIONS.get("minecraft", "").count(lk.TOOL_PACK_INSTRUCTION.strip()) == 1)
    check("no setting of this file in the discovered settings", not [e for e in tools.AUTO_SETTINGS if e["file_stem"] == "minecraft_updates"])
    check("every setting the tool reads is declared by lookup", {"default_dest", "deadline_seconds", "quilt_fabric_fallback"} <= {e["key"] for e in lk.TOOL_SETTINGS})
    schema = mu.TOOL_SCHEMAS[0]
    props = schema["parameters"]["properties"]
    check("schema: nothing is required, the four arguments are there", schema["name"] == name and schema["parameters"]["required"] == []
          and set(props) == {"folder", "mc_version", "loader", "channel"})
    check("schema: enums match the code", props["loader"]["enum"] == list(sources.LOADERS) and props["channel"]["enum"] == list(updates.CHANNELS))
    check("the description says read-only, never 'updated', and where to get a file",
          "Read-only" in schema["description"] and "never 'updated'" in schema["description"] and "download_mc_file" in schema["description"])
    entry = mu.TEST_CHECKLIST[name]
    check("checklist entry passes the schema", checklist_schema.validate_entry(name, entry) == [], checklist_schema.validate_entry(name, entry))
    check("its own section id", entry["group"] == "minecraft_updates" and set(mu.TEST_CHECKLIST_GROUP) == {"minecraft_updates"}
          and mu.TEST_CHECKLIST_GROUP["minecraft_updates"]["label"] == "Minecraft: updates")
    check("discovery accepted entry and section", name in tools.AUTO_TEST_CHECKLIST and tools.AUTO_TEST_CHECKLIST_GROUPS.get("minecraft_updates"))
    seen = [s.get("ask") or json.dumps(s.get("run"), sort_keys=True) for s in entry["steps"]]
    check("checklist: no duplicate step", len(seen) == len(set(seen)))
    check("checklist: covers CurseForge, a protected folder and an empty folder",
          any("curseforge" in s["expect"].lower() for s in entry["steps"]) and any("bad_dest" in s["expect"] for s in entry["steps"])
          and any("checked: 0" in s["expect"] for s in entry["steps"]))
    check("owner-only: this tool and the two loaded Minecraft tools missing before are in the set",
          {name, "download_mc_file", "make_modpack_file"} <= tools.OWNER_ONLY_TOOLS)
    with Sender(GUEST):
        hidden = tools.hidden_from_sender()
        check("a guest is not offered any of the three", {name, "download_mc_file", "make_modpack_file"} <= set(hidden), sorted(hidden))
    with Sender(OWNER):
        check("the owner is offered it", name not in tools.hidden_from_sender())
    check("every failure reason this file uses is a contract reason",
          all(r in REASONS for r in ("unsupported", "bad_dest", "offline", "rate_limited", "vendor_changed")))


# ------------------------------------------------------------------ guests

def test_a_guest_is_refused_before_anything_is_read():
    folder = fresh({"sodiumish-0.5.0.jar": "sodiumish-0.5.0"})
    wipe_state()
    hashed = []
    real_hash = updates._hash_file
    with Fake() as fake, Patch(updates, _hash_file=lambda p: hashed.append(p) or real_hash(p)):
        for label, value in (("a chat guest", GUEST), ("an unreadable sender", "{not json"), ("a list instead of an object", "[1, 2]"),
                             ("deep nesting", "[" * 5000 + "]" * 5000)):
            with Sender(value):
                res = mu.tool_check_mc_updates({"folder": str(folder)})
            check("%s is refused" % label, res.get("ok") is False and res.get("reason") == "unsupported", res)
        check("no request was made", fake.calls == [], fake.calls)
        check("no file was hashed", hashed == [], hashed)
        check("no state was written", not state_file().exists())
        with Sender(OWNER):
            res = mu.tool_check_mc_updates({"folder": str(folder), **FAB})
        check("the owner is served", res.get("ok") is True and res.get("checked") == 1, res)
        with Sender(None):
            res = mu.tool_check_mc_updates({"folder": str(folder), **FAB})
        check("no chat context (the PC itself) is the owner's own", res.get("ok") is True, res)
        with Sender(json.dumps({"platform": "discord"})):
            res = mu.tool_check_mc_updates({"folder": str(folder), **FAB})
        check("a sender with no user id is not a chat message: served like the PC", res.get("ok") is True, res)


# ------------------------------------------------------------------ the folder rules

def test_folder_rules():
    jarvis_dir = HOMEP / ".jarvis"
    jarvis_dir.mkdir(exist_ok=True)
    real = fresh({"sodiumish-0.5.0.jar": "sodiumish-0.5.0"})
    a_file = HOMEP / "just-a-file.jar"
    a_file.write_bytes(b"x")
    cases = [("~/.jarvis", str(jarvis_dir)), ("a folder inside ~/.jarvis", str(jarvis_dir / "modhosts")),
             ("the home folder itself", str(HOMEP)), ("the home shortcut ~", "~"), ("a drive root", "/"),
             ("a relative path", "mods/here"), ("a '..' path", str(real / ".." / real.name)), ("an empty-ish name", "x\x00y"),
             ("a folder that does not exist", str(HOMEP / "no-such-folder")), ("a file, not a folder", str(a_file))]
    if sys.platform != "win32":
        cases += [("/etc", "/etc"), ("/usr/lib", "/usr/lib")]
    else:
        cases += [("C:/Windows", "C:/Windows"), ("a drive root C:/", "C:/")]
    with Fake() as fake:
        for label, text in cases:
            res = updates.check_folder(folder=text, default_folder=str(HOMEP / "Downloads"))
            check("%s is refused with bad_dest" % label, res.get("ok") is False and res.get("reason") == "bad_dest", res)
            res = mu.tool_check_mc_updates({"folder": text})
            check("the tool says the same for %s" % label, res.get("ok") is False and res.get("reason") == "bad_dest", res)
        link = HOMEP / "link-to-mods"
        if try_symlink(real, link):
            res = updates.check_folder(folder=str(link))
            check("a link to a folder is refused", res.get("ok") is False and res.get("reason") == "bad_dest", res)
        else:
            print("skipped (this system cannot make symlinks): a link to a folder")
        link2 = HOMEP / "link-to-jarvis"
        if try_symlink(jarvis_dir, link2):
            res = updates.check_folder(folder=str(link2))
            check("a link into ~/.jarvis is refused", res.get("ok") is False and res.get("reason") == "bad_dest", res)
        else:
            print("skipped (this system cannot make symlinks): a link into ~/.jarvis")
        check("no request was made for any of them", fake.calls == [], fake.calls[:2])
        wipe_state()
        res = updates.check_folder(folder=None, default_folder=str(HOMEP / "Downloads-that-is-missing"))
        check("a missing default folder is bad_dest too, in a plain sentence", res.get("ok") is False and res.get("reason") == "bad_dest"
              and isinstance(res.get("error"), str) and res["error"], res)
        check("nothing was requested for it either", fake.calls == [])
    check("the resolver takes a good folder", updates.resolve_folder(str(real), "~/Downloads")[2] is None)
    check("`updates.protected_reason` is the shared rule (outdir), asked for the real path too",
          updates.outdir.protected_reason(jarvis_dir, writing=True) is not None)


# ------------------------------------------------------------------ the scan

def test_the_scan_reads_one_level_only():
    folder = fresh({"good.jar": "sodiumish-0.5.0", ".hidden.jar": "x1", "notes.txt": "x2", "a.jar.disabled": "x3",
                    "pack.mrpack": "x4", "skin.ZIP": "x5"})
    sub = folder / "sub"
    sub.mkdir()
    (sub / "inside.jar").write_bytes(b"inside")
    links = 0
    if try_symlink(folder / "good.jar", folder / "link.jar"):
        links += 1
    if try_symlink(sub, folder / "linkdir"):
        links += 1
    names_before = sorted(os.listdir(str(folder)))
    hashed = []
    real_hash = updates._hash_file

    def spy(path):
        hashed.append(os.path.basename(path))
        return real_hash(path)

    with Patch(updates, _hash_file=spy):
        scan = updates.scan_folder(folder)
    check("the scan worked", scan.get("ok") is True and scan["partial"] is False, scan)
    check("only the plain .jar / .zip / .mrpack files at the top level are read (any case)",
          sorted(f["name"] for f in scan["files"]) == ["good.jar", "pack.mrpack", "skin.ZIP"], [f["name"] for f in scan["files"]])
    check("nothing else was opened: not the sub-folder's file, not a link, not a hidden file, not a text file",
          sorted(hashed) == ["good.jar", "pack.mrpack", "skin.ZIP"], hashed)
    check("they are counted instead", scan["ignored"] == {"other_files": 2, "subfolders": 1, "hidden": 1, "links": links}, scan["ignored"])
    check("the hash is the file's SHA-1", [f["sha1"] for f in scan["files"] if f["name"] == "good.jar"] == [sha1("sodiumish-0.5.0")])
    # a file that cannot be read is listed, not sent
    with Patch(updates, _hash_file=lambda p: (None, "unreadable") if p.endswith("pack.mrpack") else real_hash(p)):
        scan = updates.scan_folder(folder)
    check("an unreadable file is reported and left out", scan["unreadable"] == ["pack.mrpack"]
          and "pack.mrpack" not in [f["name"] for f in scan["files"]], scan)
    # a regular file that turns into something else between the listing and the open is refused by _hash_file
    check("_hash_file refuses a folder", updates._hash_file(str(sub))[0] is None)
    check("_hash_file never raises on a missing file", updates._hash_file(str(folder / "gone.jar")) == (None, "unreadable"))
    check("the scan fails plainly on a folder it cannot list", updates.scan_folder(folder / "missing").get("reason") == "bad_dest")
    check("the scan wrote nothing", sorted(os.listdir(str(folder))) == names_before, os.listdir(str(folder)))


# ------------------------------------------------------------------ matching by content

def test_matching_is_by_content_and_only_hashes_are_sent():
    folder = fresh({"renamed-whatever.jar": "sodiumish-0.5.0", "sodiumish-0.5.1.jar": "totally-different-bytes"})
    with Fake() as fake:
        res = run(folder, **FAB)
    check("it worked", res.get("ok") is True and res["checked"] == 1, res)
    check("a renamed jar still matches (by content) and has an update", [e["file"] for e in res["updates"]] == ["renamed-whatever.jar"], res["updates"])
    check("a look-alike name with other bytes is NOT matched: not checked, never guessed",
          [(e["file"], e["why"]) for e in res["not_checked"]] == [("sodiumish-0.5.1.jar", "not_on_modrinth")], res["not_checked"])
    sent = json.dumps([c["json"] for c in fake.calls] + [c["query"] for c in fake.calls] + [c["path"] for c in fake.calls])
    check("no file name and no path left the PC", "renamed" not in sent and "totally" not in sent and HOME not in sent and "mods" not in sent.replace("modrinth", ""), sent[:300])
    check("only hashes, the algorithm and the filters were sent in a body",
          all(set(c["json"]) <= {"hashes", "algorithm", "loaders", "game_versions"} for c in fake.calls if c["json"]), [c["json"] for c in fake.calls if c["json"]])
    check("the hashes are SHA-1 of the content", fake.paths("POST", "/version_files")[0]["json"]["hashes"] == sorted([sha1("sodiumish-0.5.0"), sha1("totally-different-bytes")])
          and fake.paths("POST", "/version_files")[0]["json"]["algorithm"] == "sha1")
    check("every request went to api.modrinth.com", {c["host"] for c in fake.calls} == {"api.modrinth.com"}, {c["host"] for c in fake.calls})
    check("an honest User-Agent and no key", all(c["headers"].get("User-Agent", "").startswith("Jarvis/") and "x-api-key" not in c["headers"] for c in fake.calls))
    # Modrinth answering with a version that does not list the hash is not a match
    folder2 = fresh({"liar.jar": "sodiumish-0.5.0"})
    with Fake() as fake:
        fake.lie[sha1("sodiumish-0.5.0")] = "VerLit100"
        res = run(folder2, **FAB)
    check("a version that does not list the hash is not a match", res.get("checked") == 0 and res["not_checked"][0]["why"] == "not_on_modrinth", res)
    # two copies of one file
    folder3 = fresh({"a.jar": "lithiumish-1.0.0", "b.jar": "lithiumish-1.0.0"})
    with Fake() as fake:
        res = run(folder3, **FAB)
    check("two copies of one file are both reported, from one hash", res["checked"] == 2 and fake.paths("POST", "/version_files")[0]["json"]["hashes"] == [sha1("lithiumish-1.0.0")], res)


# ------------------------------------------------------------------ the whole folder

def test_a_whole_folder():
    folder = fresh(BIG)
    before = snapshot(folder)
    with Fake() as fake:
        res = run(folder, **FAB)
    check("ok, one source", res.get("ok") is True and res["source"] == "modrinth" and res["folder"] == str(folder), res)
    ups, cur, unc = by_file(res, "updates"), by_file(res, "up_to_date"), by_file(res, "not_checked")
    check("eight files have a newer file", sorted(ups) == sorted(["sodiumish-0.5.0.jar", "dualish-2.0.0.jar", "fabonly-1.0.0.jar",
          "betaish-2.0.0-beta.1.jar", "relish-1.0.0.jar", "alphaish-1.0.0-alpha.1.jar", "looky-1.0.0.zip", "shadery-1.0.0.zip"]), sorted(ups))
    check("two are up to date", sorted(cur) == ["lithiumish-1.0.0.jar", "onlybeta-1.0.0.jar"], sorted(cur))
    check("one is not checked: the file Modrinth does not know", list(unc) == ["from-curseforge.jar"] and unc["from-curseforge.jar"]["why"] == "not_on_modrinth", unc)
    check("counts", res["checked"] == 10 and res["updates_available"] == 8, res)
    sod = ups["sodiumish-0.5.0.jar"]
    check("a newer beta is NOT offered to a release file: it gets the newest release for its own Minecraft version",
          sod["latest"]["version"] == "0.5.1" and sod["latest"]["file_id"] == "VerSod051" and sod["installed"]["version"] == "0.5.0", sod)
    check("get_with names the tool, the project and the exact file",
          sod["get_with"] == {"tool": "download_mc_file", "source": "modrinth", "project": "Pj1Sodium", "file": "VerSod051"}, sod["get_with"])
    check("project name, id and page", sod["project"]["name"] == "Sodiumish" and sod["project"]["id"] == "Pj1Sodium"
          and sod["project"]["page_url"] == "https://modrinth.com/project/Pj1Sodium", sod["project"])
    check("latest carries the file name, size, channel and day", sod["latest"]["file_name"] == "sodiumish-0.5.1.jar"
          and sod["latest"]["channel"] == "release" and sod["latest"]["date"] == "2026-03-01" and isinstance(sod["latest"]["size"], int), sod["latest"])
    check("a beta file sees the newer release", ups["betaish-2.0.0-beta.1.jar"]["latest"]["version"] == "2.0.0")
    check("a release file whose only newer file is a beta gets the newest RELEASE", ups["relish-1.0.0.jar"]["latest"]["version"] == "1.1.0")
    check("an alpha file sees the newer alpha", ups["alphaish-1.0.0-alpha.1.jar"]["latest"]["version"] == "1.0.0-alpha.2")
    check("a release file with only a newer beta is up to date on the default channel", "onlybeta-1.0.0.jar" in cur)
    check("a resource pack and a shader use their own loaders and the asked Minecraft version",
          ups["looky-1.0.0.zip"]["latest"]["version"] == "1.1.0" and ups["shadery-1.0.0.zip"]["latest"]["version"] == "1.1.0")
    check("no `via` without Quilt", all("via" not in e["latest"] for e in ups.values()))
    check("the CurseForge explanation is there once", "CurseForge" in res.get("curseforge_note", "") and "fingerprint" in res["curseforge_note"], res.get("curseforge_note"))
    check("the message says nothing was changed and where to get a file",
          "Nothing was downloaded, replaced, updated or installed" in res["message"] and "download_mc_file" in res["message"] and res["nothing_changed"] is True, res["message"])
    check("never says 'updated' as a result", "was updated" not in res["message"] and "have been updated" not in res["message"])
    check("vendor text is marked", res.get("untrusted_note") == untrusted.NOTE or res["untrusted_note"].startswith(untrusted.NOTE), res.get("untrusted_note"))
    check("request counts: one match call, one update call per Minecraft-version/loader group (3), the fallback listing for the three projects whose newest file was a less stable one, one names call",
          [len(fake.paths("POST", "/version_files")), len(fake.paths("POST", "/version_files/update")),
           len([c for c in fake.calls if c["method"] == "GET" and c["path"].endswith("/version")]), len(fake.paths("GET", "/projects"))] == [1, 3, 3, 1],
          [(c["method"], c["path"]) for c in fake.calls])
    check("the folder was not changed in any way", snapshot(folder) == before)
    check("the result is plain JSON", json.loads(json.dumps(res)) == res)


# ------------------------------------------------------------------ channels

def test_channels():
    old, newest = fresh({"s.jar": "sodiumish-0.5.0"}), fresh({"s.jar": "sodiumish-0.5.1"})
    onlybeta = fresh({"o.jar": "onlybeta-1.0.0"})
    with Fake() as fake:
        res = run(old, **FAB)
        check("release: 0.5.0 gets 0.5.1 (through the listing)", res["updates"][0]["latest"]["version"] == "0.5.1"
              and len([c for c in fake.calls if c["method"] == "GET" and c["path"].endswith("/version")]) == 1)
        fake.calls.clear()
        res = run(old, channel="beta", **FAB)
        check("beta: 0.5.0 gets the 0.6.0 beta straight from the update call, no listing needed",
              res["updates"][0]["latest"]["version"] == "0.6.0-beta.1" and res["updates"][0]["latest"]["channel"] == "beta"
              and not [c for c in fake.calls if c["method"] == "GET" and c["path"].endswith("/version")], res)
        res = run(newest, **FAB)
        check("release: 0.5.1 is up to date although a beta is newer", res["updates_available"] == 0 and res["up_to_date"][0]["version"] == "0.5.1", res)
        res = run(newest, channel="alpha", **FAB)
        check("alpha accepts everything: 0.5.1 sees the beta", res["updates"][0]["latest"]["version"] == "0.6.0-beta.1", res)
        res = run(onlybeta, **FAB)
        check("a release file whose only newer file is a beta is up to date", res["updates_available"] == 0 and res["checked"] == 1, res)
        res = run(onlybeta, channel="beta", **FAB)
        check("asking for betas offers it", res["updates"][0]["latest"]["version"] == "1.1.0-beta.1", res)
        res = run(old, channel="nightly", **FAB)
        check("an unknown channel is refused", res.get("ok") is False and res["reason"] == "unsupported", res)
        res = mu.tool_check_mc_updates({"folder": str(old), "channel": "nightly"})
        check("the tool refuses it too", res.get("ok") is False and res["reason"] == "unsupported", res)


# ------------------------------------------------------------------ asked, never guessed

def test_versions_and_loaders_are_asked_not_guessed():
    folder = fresh({"dualish-2.0.0.jar": "dualish-2.0.0", "looky-1.0.0.zip": "looky-1.0.0", "fabonly-1.0.0.jar": "fabonly-1.0.0"})
    with Fake() as fake:
        res = run(folder)
        nc = by_file(res, "not_checked")
        check("a file on several Minecraft versions, given no answer, is not checked",
              nc["dualish-2.0.0.jar"]["why"] == "needs_mc_version" and nc["looky-1.0.0.zip"]["why"] == "needs_mc_version", nc)
        check("a file listing exactly one version and one loader is checked on its own", "fabonly-1.0.0.jar" in by_file(res, "updates"), res)
        check("the answer asks, and says what", res["needs_clarification"] is True and "Minecraft version" in res["ask"], res.get("ask"))
        check("the sentence names the versions the file lists", "1.20.1" in nc["dualish-2.0.0.jar"]["detail"] and "1.20.2" in nc["dualish-2.0.0.jar"]["detail"], nc["dualish-2.0.0.jar"])
        check("no update was looked up for the ones it could not decide", all(
            "1.20.2" not in json.dumps(c["json"]) for c in fake.paths("POST", "/version_files/update")))
        res = run(folder, mc_version="1.20.1")
        nc, ups = by_file(res, "not_checked"), by_file(res, "updates")
        check("with the version given, a two-loader file still asks for the loader", nc["dualish-2.0.0.jar"]["why"] == "needs_loader"
              and "loader" in res["ask"] and "Minecraft version" not in res["ask"], res)
        check("a resource pack needs no loader", ups["looky-1.0.0.zip"]["latest"]["version"] == "1.1.0", ups.keys())
        res = run(folder, **FAB)
        check("with both given everything is checked", res["not_checked"] == [] and "needs_clarification" not in res and res["updates_available"] == 3, res)
        res = run(folder, mc_version="1.20.2", loader="fabric")
        oth = by_file(res, "other_version")
        check("a file built for another Minecraft version is reported as such, never as up to date",
              oth["fabonly-1.0.0.jar"]["why"] == "other_minecraft_version" and "1.20.1" in oth["fabonly-1.0.0.jar"]["detail"]
              and "fabonly-1.0.0.jar" not in by_file(res, "up_to_date") and "fabonly-1.0.0.jar" not in by_file(res, "updates"), res)
        res = run(folder, mc_version="1.20.1", loader="forge")
        oth = by_file(res, "other_version")
        check("a file built for another loader is reported as such", oth["fabonly-1.0.0.jar"]["why"] == "other_loader"
              and oth["dualish-2.0.0.jar"]["why"] == "other_loader", oth)
        check("the message counts them", "another Minecraft version or loader" in res["message"], res["message"])
        res = mu.tool_check_mc_updates({"folder": str(folder), "mc_version": "latest"})
        check("a snapshot / word is not a Minecraft version", res.get("ok") is False and res["reason"] == "unsupported", res)
        res = mu.tool_check_mc_updates({"folder": str(folder), "loader": "bukkit"})
        check("an unknown loader is refused", res.get("ok") is False and res["reason"] == "unsupported", res)
        res = mu.tool_check_mc_updates({"folder": str(folder), "mc_version": "26.3", "loader": "Neo Forge"})
        check("26.3 and a spelled-out loader are accepted", res.get("ok") is True and res["mc_version"] == "26.3" and res["loader"] == "neoforge", res)


# ------------------------------------------------------------------ Quilt

def test_quilt_falls_back_to_fabric_and_says_so():
    folder = fresh({"fabonly-1.0.0.jar": "fabonly-1.0.0"})
    dual = fresh({"dualish-2.0.0.jar": "dualish-2.0.0"})
    with Fake() as fake:
        res = run(folder, mc_version="1.20.1", loader="quilt", quilt_fallback=True)
        check("a Fabric-only file is offered its newer Fabric file, marked via fabric",
              res["updates"][0]["latest"]["version"] == "1.1.0" and res["updates"][0]["latest"]["via"] == "fabric", res)
        check("the update call asked for Quilt first and Fabric second", fake.paths("POST", "/version_files/update")[0]["json"]["loaders"] == ["quilt", "fabric"])
        res = run(folder, mc_version="1.20.1", loader="quilt", quilt_fallback=False)
        check("with the setting off a Fabric file is built for another loader", res["other_version"][0]["why"] == "other_loader" and res["updates"] == [], res)
        res = run(dual, mc_version="1.20.1", loader="quilt", quilt_fallback=True)
        check("a file that has a Quilt build needs no label", res["updates"][0]["latest"]["version"] == "2.1.0" and "via" not in res["updates"][0]["latest"], res)
    with Patch(mu, _setting=lambda key, default: {"quilt_fabric_fallback": False}.get(key, default)), Fake():
        res = mu.tool_check_mc_updates({"folder": str(folder), "mc_version": "1.20.1", "loader": "quilt"})
    check("the tool reads quilt_fabric_fallback", res["other_version"][0]["why"] == "other_loader", res)


# ------------------------------------------------------------------ CurseForge

def test_curseforge_files_are_not_checked_never_guessed():
    folder = fresh({"jei-1.20.1-forge-15.3.0.jar": "bytes-from-a-curseforge-download", "sodiumish-0.5.0.jar": "sodiumish-0.5.0"})
    with Fake() as fake:
        res = run(folder, **FAB)
    nc = by_file(res, "not_checked")
    check("a file Modrinth does not know is not checked, with the reason", nc["jei-1.20.1-forge-15.3.0.jar"]["why"] == "not_on_modrinth"
          and "does not know" in nc["jei-1.20.1-forge-15.3.0.jar"]["detail"], nc)
    check("the answer explains CurseForge files once: own fingerprint, not computed, nothing guessed",
          "fingerprint" in res["curseforge_note"] and "not checked" in res["curseforge_note"] and "guessed" in res["curseforge_note"], res["curseforge_note"])
    check("the name 'jei' (a CurseForge-looking name) led nowhere: no name was ever sent", "jei" not in json.dumps([c["json"] for c in fake.calls]))
    check("no other site was contacted and no key was used", {c["host"] for c in fake.calls} == {"api.modrinth.com"}
          and not any("x-api-key" in c["headers"] for c in fake.calls))
    check("no key anywhere in the result", FAKE_KEY not in json.dumps(res))
    check("the neighbour is still checked", [e["file"] for e in res["updates"]] == ["sodiumish-0.5.0.jar"], res["updates"])
    only = fresh({"x.jar": "only-curseforge"})
    with Fake():
        res = run(only, **FAB)
    check("a folder of only unknown files is still an answer (nothing checked), not an error", res.get("ok") is True and res["checked"] == 0
          and len(res["not_checked"]) == 1 and "curseforge_note" in res, res)


# ------------------------------------------------------------------ failures

def test_failures_and_partial_answers():
    folder = fresh({"sodiumish-0.5.0.jar": "sodiumish-0.5.0", "relish-1.0.0.jar": "relish-1.0.0", "lithiumish-1.0.0.jar": "lithiumish-1.0.0"})
    with Fake() as fake:
        fake.fail["/version_files"] = (429, {"retry-after": "120"}, {})
        res = run(folder, **FAB)
    check("rate limited at the first call: the whole check fails with rate_limited", res.get("ok") is False and res["reason"] == "rate_limited", res)
    with Fake() as fake:
        fake.fail["/version_files"] = (500, {}, {})
        res = run(folder, **FAB)
    check("a vendor error: offline", res.get("ok") is False and res["reason"] == "offline", res)
    with Fake() as fake:
        fake.boom = True
        res = run(folder, **FAB)
    check("a dead network: offline, one sentence", res.get("ok") is False and res["reason"] == "offline" and isinstance(res["error"], str), res)
    with Fake() as fake:
        fake.fail["/version_files"] = (200, {}, ["not", "an", "object"])
        res = run(folder, **FAB)
    check("an answer in the wrong shape: vendor_changed", res.get("ok") is False and res["reason"] == "vendor_changed", res)
    with Fake() as fake:
        fake.fail["/version_files/update"] = (500, {}, {})
        res = run(folder, **FAB)
    check("the update call failing leaves every file it covers as not checked (offline), never as up to date",
          res.get("ok") is True and res["updates"] == [] and res["up_to_date"] == []
          and sorted(e["why"] for e in res["not_checked"]) == ["offline"] * 3 and all(e["detail"] for e in res["not_checked"]), res)
    with Fake() as fake:
        fake.fail["/project/"] = (500, {}, {})
        res = run(folder, **FAB)
    nc, cur = by_file(res, "not_checked"), by_file(res, "up_to_date")
    check("a failing listing call affects only the files that needed it", sorted(nc) == ["relish-1.0.0.jar", "sodiumish-0.5.0.jar"]
          and all(e["why"] == "offline" for e in nc.values()), nc)
    check("the others are unaffected", list(cur) == ["lithiumish-1.0.0.jar"], cur)
    with Fake() as fake:
        fake.fail["/v2/projects"] = (500, {}, {})
        res = run(folder, **FAB)
    check("without names the answer still works, naming projects by id", res.get("ok") is True and res["updates_available"] == 2
          and res["up_to_date"][0]["project"] == "Pj2Lithium" and "name" not in res["updates"][0]["project"], res)


# ------------------------------------------------------------------ the deadline setting

def test_the_deadline_setting():
    folder = fresh({"sodiumish-0.5.0.jar": "sodiumish-0.5.0", "lithiumish-1.0.0.jar": "lithiumish-1.0.0"})
    with Fake() as fake:
        res = run(folder, deadline_seconds=0, **FAB)
        check("0 = no deadline: everything is checked", res["checked"] == 2 and "partial" not in res, res)
        check("the setting default is 0 and the tool reads it as no limit", mu._deadline_seconds() == 0.0)
        with Patch(mu, _setting=lambda key, default: {"deadline_seconds": "7"}.get(key, default)):
            check("the tool reads deadline_seconds", mu._deadline_seconds() == 7.0)
        with Patch(mu, _setting=lambda key, default: {"deadline_seconds": "junk"}.get(key, default)):
            check("junk is no limit", mu._deadline_seconds() == 0.0)
    with Fake() as fake, Patch(updates, _late=lambda d: d is not None):
        res = run(folder, deadline_seconds=5, **FAB)
        check("a deadline already passed: partial, nothing read, nothing requested", res.get("ok") is True and res["partial"] is True
              and res["checked"] == 0 and fake.calls == [] and "stopped early" in res["message"], res)
    flag = {"late": False}
    real_scan = updates.scan_folder

    def scan_then_late(*a, **k):
        out = real_scan(*a, **k)
        flag["late"] = True
        return out

    with Fake() as fake, Patch(updates, scan_folder=scan_then_late, _late=lambda d: flag["late"] and d is not None):
        res = run(folder, deadline_seconds=5, **FAB)
        check("a deadline passing after the scan: no request is made, every file is not checked, the list says it is partial",
              res.get("ok") is True and res["partial"] is True and fake.calls == [] and len(res["not_checked"]) == 2
              and all(e["why"] == "offline" for e in res["not_checked"]) and "stopped early" in res["message"], res)


# ------------------------------------------------------------------ the state file

def test_state_file():
    wipe_state()
    folder = fresh({"sodiumish-0.5.0.jar": "sodiumish-0.5.0", "lithiumish-1.0.0.jar": "lithiumish-1.0.0"})
    other = fresh({"fabonly-1.0.0.jar": "fabonly-1.0.0"})
    hashed = []
    real_hash = updates._hash_file
    with Fake(), Patch(updates, _hash_file=lambda p: hashed.append(os.path.basename(p)) or real_hash(p)):
        res = run(folder, **FAB)
        check("the first run read both files", sorted(hashed) == ["lithiumish-1.0.0.jar", "sodiumish-0.5.0.jar"] and res["checked"] == 2, hashed)
        check("the state file is under ~/.jarvis/modhosts", state_file().is_file())
        text = state_file().read_text(encoding="utf-8")
        data = json.loads(text)
        check("it holds the folder and the hash cache only", set(data) == {"v", "last_folder", "hashes"} and data["last_folder"] == str(folder)
              and len(data["hashes"]) == 2, data)
        check("no key, no vendor answer, no name of a project in it", FAKE_KEY not in text and "Sodiumish" not in text and "VerSod" not in text)
        check("every cached value is [size, mtime_ns, sha1]", all(isinstance(v, list) and len(v) == 3 and re.match(r"^[0-9a-f]{40}$", v[2])
                                                                for v in data["hashes"].values()))
        hashed.clear()
        run(folder, **FAB)
        check("the second run reads nothing again (cache hit)", hashed == [], hashed)
        (folder / "lithiumish-1.0.0.jar").write_bytes(b"changed bytes of another size")
        hashed.clear()
        res = run(folder, **FAB)
        check("a changed file is hashed again, and only that one", hashed == ["lithiumish-1.0.0.jar"], hashed)
        check("and it no longer matches Modrinth", [e["file"] for e in res["not_checked"]] == ["lithiumish-1.0.0.jar"], res)
        (folder / "lithiumish-1.0.0.jar").unlink()
        run(folder, **FAB)
        check("an entry of a deleted file leaves the state", len(json.loads(state_file().read_text(encoding="utf-8"))["hashes"]) == 1)
        # the last folder
        res = updates.check_folder(folder=None, default_folder=str(other))
        check("no folder named: the last one checked is used, and the answer says so", res.get("ok") is True and res["folder"] == str(folder)
              and res["folder_from"] == "last_checked", res)
        res = updates.check_folder(folder=str(other), default_folder=str(folder))
        check("a named folder wins and becomes the last one", res["folder"] == str(other) and res["folder_from"] == "given"
              and json.loads(state_file().read_text(encoding="utf-8"))["last_folder"] == str(other))
        import shutil
        shutil.rmtree(str(other))
        res = updates.check_folder(folder=None, default_folder=str(folder))
        check("a last folder that is gone is refused (it does not quietly check somewhere else)", res.get("ok") is False and res["reason"] == "bad_dest", res)
        # a corrupt file is ignored
        state_file().write_text("{not json at all", encoding="utf-8")
        check("a corrupt state file reads as empty", updates.load_state() == {"last_folder": None, "hashes": {}})
        res = updates.check_folder(folder=None, default_folder=str(folder))
        check("and the default folder is used again", res.get("ok") is True and res["folder_from"] == "default", res)
        check("the run rewrote a good file", isinstance(json.loads(state_file().read_text(encoding="utf-8")), dict))
        state_file().write_text(json.dumps({"v": 1, "last_folder": 5, "hashes": {"a": [1, 2, "nothex"], "b": "x", "c": [1, 2.5, "0" * 40]}}), encoding="utf-8")
        check("a state file with wrong types reads as empty", updates.load_state() == {"last_folder": None, "hashes": {}})
    # size cap
    with Patch(updates, STATE_MAX_HASHES=3):
        ok = updates.save_state({"last_folder": "x", "hashes": {"k%d" % i: [i, i, "a" * 40] for i in range(10)}})
    check("the hash cache is capped (oldest entries go first)", ok and sorted(json.loads(state_file().read_text(encoding="utf-8"))["hashes"]) == ["k7", "k8", "k9"])
    # the default folder
    wipe_state()
    downloads = HOMEP / "Downloads"
    downloads.mkdir(exist_ok=True)
    (downloads / "lithiumish-1.0.0.jar").write_bytes(b"lithiumish-1.0.0")
    with Fake():
        with Patch(mu, _setting=lambda key, default: {"default_dest": "~/Downloads"}.get(key, default)):
            res = mu.tool_check_mc_updates(dict(FAB))
    check("no folder and no state: the default download folder (~ expanded)", res.get("ok") is True and res["folder"] == str(downloads)
          and res["folder_from"] == "default" and res["checked"] == 1, res)


def test_a_state_file_that_cannot_be_written_breaks_nothing():
    blocker = HOMEP / "blocker.txt"
    blocker.write_text("a file where a folder would have to be", encoding="utf-8")
    folder = fresh({"lithiumish-1.0.0.jar": "lithiumish-1.0.0"})
    with Fake(), Patch(updates, state_path=lambda: blocker / "sub" / "update_state.json"):
        res = updates.check_folder(folder=str(folder), default_folder=str(folder), **FAB)
        check("the check still answers", res.get("ok") is True and res["checked"] == 1, res)
        check("save_state says it could not write, and does not raise", updates.save_state({"last_folder": "x", "hashes": {}}) is False)
        check("load_state of a path that cannot exist is empty", updates.load_state() == {"last_folder": None, "hashes": {}})


# ------------------------------------------------------------------ the folder is never changed

def test_the_folder_is_never_changed():
    folder = fresh(BIG)
    (folder / "sub").mkdir()
    (folder / "sub" / "inside.jar").write_bytes(b"inside")
    (folder / "notes.txt").write_text("hello", encoding="utf-8")
    before = snapshot(folder)
    with Fake():
        for args in (FAB, {}, {"mc_version": "1.20.1"}, {"mc_version": "1.20.2", "loader": "quilt"}, dict(FAB, channel="alpha")):
            run(folder, **args)
    check("the folder (names, sizes, modified times, sub-folders) is exactly as it was after five checks", snapshot(folder) == before)
    check("no temp or partial file was left in it", not [n for n in os.listdir(str(folder)) if n.endswith((".part", ".tmp"))])
    src = Path(updates.__file__).read_text(encoding="utf-8")
    check("the module has no way to delete, move or copy a file", "os.remove" not in src and "os.unlink" not in src
          and "shutil" not in src and "os.rename" not in src and "copyfile" not in src)
    check("the module opens no connection of its own", "requests" not in src and "urllib.request" not in src and "socket" not in src and "http.client" not in src)
    check("the module does not touch the hub or read the CurseForge key", "hub" not in re.sub(r"[#].*|\"\"\"[\s\S]*?\"\"\"", "", src).replace("netguard", "") and "get_key" not in src)


# ------------------------------------------------------------------ vendor text

def test_vendor_text_is_untrusted():
    folder = fresh({"evilish-1.0.0.jar": "evilish-1.0.0"})
    with Fake():
        res = run(folder, **FAB)
    entry = res["updates"][0]
    check("an instruction-like version name is flagged and the note says so", res.get("text_flagged") is True and "instruction" in res["untrusted_note"], res.get("untrusted_note"))
    check("the project name is stripped of markup and links and clipped", "<" not in entry["project"]["name"] and "http" not in entry["project"]["name"]
          and len(entry["project"]["name"]) <= untrusted.NAME_LEN, entry["project"])
    check("a vendor file name loses its path part and never says '..'", entry["latest"]["file_name"] == "evil.jar", entry["latest"])
    check("the version label is clipped", len(entry["latest"]["version"]) <= 60)
    check("the vendor's long version NAME (the instruction) is not passed on at all", "Ignore all previous" not in json.dumps(res))
    # a local file name with control characters is cleaned in the answer
    weird = fresh({"we\u202eird\x07.jar": "evilish-1.0.0"}) if sys.platform != "win32" else fresh({"we\u202eird.jar": "evilish-1.0.0"})
    with Fake():
        res = run(weird, **FAB)
    check("a local file name is cleaned of bidi and control characters in the answer",
          res["updates"][0]["file"] == "weird.jar", res["updates"][0]["file"])


# ------------------------------------------------------------------ no files / junk

def test_an_empty_folder_and_junk_arguments():
    empty = fresh()
    (empty / "readme.txt").write_text("x", encoding="utf-8")
    with Fake() as fake:
        res = run(empty, **FAB)
        check("an empty folder is an answer, not an error, and makes no request", res.get("ok") is True and res["checked"] == 0
              and "nothing to check" in res["message"] and fake.calls == [], res)
        for args in (None, "a string", 5, [], {"folder": 5}, {"folder": ["a"]}, {"folder": object()}, {"mc_version": 5, "loader": object()},
                     {"channel": 3}, {"folder": "x" * 5000}, {"folder": str(empty), "mc_version": ["1.20.1"], "loader": {"a": 1}}):
            res = mu.tool_check_mc_updates(args)
            check("junk arguments %r give a dict with ok" % (str(args)[:40],), isinstance(res, dict) and "ok" in res, res)
        res = updates.check_folder(folder=object(), default_folder=object(), mc_version=object(), loader=object(), channel=object(),
                                   quilt_fallback=object(), deadline_seconds=object())
        check("the module never raises either", isinstance(res, dict) and "ok" in res, res)


# ------------------------------------------------------------------ shaping

def test_result_shaping():
    folder = fresh(BIG)
    with Fake():
        res = run(folder, **FAB)
    for level in ("medium", "low"):
        shaped = tool_result_shaping.shape_result("check_mc_updates", copy.deepcopy(res), level)
        keep = all(k in shaped for k in ("message", "nothing_changed", "untrusted_note", "updates", "up_to_date", "not_checked", "curseforge_note", "checked"))
        check("%s keeps the message, the not-changed flag, the note, the lists and the CurseForge explanation" % level, keep, sorted(shaped))
        check("%s keeps every update's file id and get_with" % level, all("get_with" in u and "file_id" in u["latest"] for u in shaped["updates"]))
        check("%s keeps why a file was not checked" % level, all("why" in n and "detail" in n for n in shaped["not_checked"]))
        check("%s drops the ignored counts" % level, "ignored" not in shaped)
    low = tool_result_shaping.shape_result("check_mc_updates", copy.deepcopy(res), "low")
    check("low drops the installed block and the version label of up-to-date files",
          all("installed" not in u for u in low["updates"]) and all("version" not in c for c in low["up_to_date"]))
    check("full leaves it as it was", tool_result_shaping.shape_result("check_mc_updates", copy.deepcopy(res), "full") == res)
    drops = [x for level in (mu.TOOL_RESULT_SPECS["check_mc_updates"].get("drop_fields") or {}).values() for x in level]
    check("the spec never drops the note, the message, the flag or the asking fields",
          not ({"untrusted_note", "message", "nothing_changed", "curseforge_note", "ask", "needs_clarification", "ok"} & set(drops)), drops)


# --- runner (keep BELOW every test: it reads globals() when it executes) ----

if __name__ == "__main__":
    for name in sorted(n for n in list(globals()) if n.startswith("test_")):
        print(f"\n== {name}")
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append((name, f"raised {exc!r}"))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for item in FAIL:
        print("  FAILED:", item)
    sys.exit(1 if FAIL else 0)
