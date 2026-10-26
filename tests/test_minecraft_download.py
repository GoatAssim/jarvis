"""actions/minecraft_download.py: `download_mc_file` (brief P4), end to end against two in-process fake adapters and the REAL
Download Hub store (a temp HOME).  Plan scenarios 2, 5 and 8.

Covers:
  * registration: TOOL_GROUP "minecraft", keywords of weight 5 or more that do not collide with the other minecraft tools,
    confirm-gated by name, NO second pack instruction and NO setting declared here, its own checklist section
    `minecraft_download`, a schema that matches the code, result specs that keep the message / verify / untrusted note;
  * a guest (and an unreadable sender) is refused BEFORE any adapter call or hub row;
  * scenario 2: a full-path dest asks the hub's "Save outside the downloads library?" once, ONE row appears with
    `filename`, `verify` (sha512 for Modrinth) and `dest_abs` and nothing else, the words "queued in Downloads; not
    installed", required dependencies listed and not queued;
  * `dry_run` queues nothing and asks nothing; the Minecraft version and loader are asked for instead of guessed; a pinned
    file, a shader, a modpack; the Quilt-to-Fabric fallback labelled (and the setting);
  * scenario 5: a null download link is `not_distributable` with the project page and NO workaround (the adapter's
    download-url route is never called); a link on a host off the allow-list, plain http, credentials, a wrong
    extension, a vendor name the hub would change, a bad `filename`;
  * verify: sha512 / sha1 / CurseForge sha1 (never the fingerprint), no checksum published, a vendor hash that is not a
    hash, the hub saying it ignored `verify` (read from its `note`), a duplicate row (earlier options kept): never a
    claim of verification the row does not have;
  * the destination: ~/.jarvis, the home folder, a drive root, an OS folder and '..' refused BEFORE any question or
    call; a sub-folder of the library needs no question; the default folder; a declined question queues nothing;
  * size limit setting (0 = unlimited); dependencies listed vs queued (`with_dependencies`), skipped ones with a reason;
  * ambiguity, not found, no compatible file with alternatives, argument errors, adapter failures passed through, CurseForge
    not installed; vendor text clipped and marked; the CurseForge key never in a result or a hub row; no header or Referer
    on a row; a tool never raises.

No network.  Run: python tests/test_minecraft_download.py
"""
import copy
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-minecraft-download-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER",
             "JARVIS_TOOL_MAKER_CONTEXT_FILE", "CURSEFORGE_API_KEY"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import checklist_schema, tool_result_shaping, tool_safety, tools, ui_bridge  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.actions import minecraft_download as dl  # noqa: E402
from jarvis.actions import minecraft_lookup as lk  # noqa: E402
from jarvis.hub import service  # noqa: E402
from jarvis.modhosts import REASONS, resolve, sources, untrusted  # noqa: E402

PASS, FAIL = [], []
FAKE_KEY = "FAKE-KEY-0123456789abcdef"
GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})
CATALOG = json.loads((ROOT / "tests" / "data" / "modhosts" / "hand_p4_catalog.json").read_text(encoding="utf-8"))
LIB = Path(HOME) / "library"
_n = [0]

# The hub refuses a link whose host does not resolve to a public address; the tests never touch DNS.
socket.getaddrinfo = lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]
service._store(None).set_settings({"library_root": str(LIB)})


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


def h(algo, seed):
    return hashlib.new(algo, seed.encode()).hexdigest()


# ------------------------------------------------------------------ fakes

class FakeAdapter:
    """The contract's adapter functions over tests/data/modhosts/hand_p4_catalog.json.  `calls` records every call;
    `fail` maps a function name to the failure dict it answers; `boom` makes every call raise."""

    def __init__(self, source):
        block = CATALOG[source]
        self.source = source
        self.projects = copy.deepcopy(block["projects"])
        self.files = copy.deepcopy(block["files"])
        self.calls = []
        self.fail = {}
        self.boom = False

    def _enter(self, name, *info):
        self.calls.append((name,) + info)
        if self.boom:
            raise RuntimeError("adapter blew up")
        return self.fail.get(name)

    def _project(self, ref):
        for p in self.projects:
            if ref in (p["id"], p["slug"]):
                return copy.deepcopy(p)
        return None

    def search(self, query, kind="mod", mc_version=None, loader=None, sort="relevance", limit=5, offset=0):
        bad = self._enter("search", query)
        if bad:
            return bad
        q = re.sub(r"[^0-9a-z]+", "", str(query).lower())
        hits = [copy.deepcopy(p) for p in self.projects if p["kind"] == kind
                and (q in re.sub(r"[^0-9a-z]+", "", p["name"].lower()) or q in re.sub(r"[^0-9a-z]+", "", p["slug"].lower()))]
        return {"ok": True, "hits": hits[:limit], "total": len(hits)}

    def get_project(self, ref):
        bad = self._enter("get_project", ref)
        if bad:
            return bad
        p = self._project(str(ref))
        return {"ok": True, "project": p} if p else {"ok": False, "reason": "not_found", "error": "No such project."}

    def list_files(self, project_id, mc_version=None, loader=None):
        bad = self._enter("list_files", project_id, mc_version, loader)
        if bad:
            return bad
        files = [copy.deepcopy(f) for f in self.files.get(str(project_id), [])]
        if mc_version:
            files = [f for f in files if mc_version in f["mc_versions"]]
        if loader:
            files = [f for f in files if loader in f["loaders"]]
        return {"ok": True, "files": files}

    def get_file(self, project_id, file_id):
        bad = self._enter("get_file", project_id, file_id)
        if bad:
            return bad
        for files in self.files.values():          # the vendor answers for ANY id: the tool checks whose file it is
            for f in files:
                if f["file_id"] == str(file_id):
                    return {"ok": True, "file": copy.deepcopy(f)}
        return {"ok": False, "reason": "not_found", "error": "No such file."}

    def bulk_projects(self, refs):
        bad = self._enter("bulk_projects", tuple(refs))
        if bad:
            return bad
        return {"ok": True, "projects": {r: self._project(str(r)) for r in refs}}

    def bulk_list_files(self, project_ids, mc_version=None, loader=None):
        bad = self._enter("bulk_list_files", tuple(project_ids), mc_version, loader)
        if bad:
            return bad
        out = {}
        for pid in project_ids:
            out[pid] = self.list_files(pid, mc_version, loader)["files"]
        return {"ok": True, "files": out}

    def bulk_get_files(self, pairs):
        bad = self._enter("bulk_get_files", tuple(pairs))
        if bad:
            return bad
        return {"ok": True, "files": {"%s:%s" % (p, f): self.get_file(p, f).get("file") for p, f in pairs}}

    def download_url(self, project_id, file_id):
        """The CurseForge adapter's extra route.  A null link must NEVER lead here: calling it is the workaround."""
        self.calls.append(("download_url", project_id, file_id))
        return {"ok": False, "reason": "not_distributable", "error": "no"}


class Adapters:
    """sources.get answers the fakes; `modrinth=False` / `curseforge=False` = that adapter is not installed."""

    def __init__(self, modrinth=True, curseforge=True):
        self.m = FakeAdapter("modrinth") if modrinth else None
        self.c = FakeAdapter("curseforge") if curseforge else None

    def __enter__(self):
        self.real = sources.get
        table = {"modrinth": self.m, "curseforge": self.c}
        sources.get = lambda name: table.get(str(name).lower())
        return self

    def __exit__(self, *exc):
        sources.get = self.real

    @property
    def calls(self):
        return (self.m.calls if self.m else []) + (self.c.calls if self.c else [])

    def names(self):
        return [c[0] for c in self.calls]


class Settings:
    """The minecraft_lookup tool settings the tool reads, replaced for the block."""

    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        self.real = dl._setting
        values = self.values
        dl._setting = lambda key, default=None: values.get(key, default)
        return self

    def __exit__(self, *exc):
        dl._setting = self.real


class Ask:
    """ui_bridge.confirm answers `answers` in turn, then `default`; every question is recorded."""

    def __init__(self, *answers, default=True):
        self.answers, self.default, self.calls = list(answers), default, []

    def __enter__(self):
        self.real = ui_bridge.confirm

        def fake(question, body="", **kw):
            self.calls.append({"title": question, "body": body})
            return self.answers.pop(0) if self.answers else self.default
        ui_bridge.confirm = fake
        return self

    def __exit__(self, *exc):
        ui_bridge.confirm = self.real


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


def call(**args):
    return dl.tool_download_mc_file(args)


def all_rows():
    return service._store(None).list_items(limit=5000, include_disabled=True, include_dismissed=True)


def count():
    return len(all_rows())


def row(item_id):
    return service._store(None).get_item(item_id) or {}


def opts(item_id):
    return row(item_id).get("options") or {}


def fresh(label):
    _n[0] += 1
    return str(Path(HOME) / "mc" / ("%s-%d" % (label, _n[0])))


FAB = dict(mc_version="1.20.1", loader="fabric")


# ------------------------------------------------------------------ registration

def test_registration_and_contract():
    check("the tool is loaded", "download_mc_file" in tools._tool_index())
    check("only this tool lives in the file (download_optifine is another file)", list(dl.TOOLS) == ["download_mc_file"])
    check("group is minecraft", dl.TOOL_GROUP == "minecraft" and "download_mc_file" in tools.AUTO_TOOL_GROUPS.get("minecraft", []),
          tools.AUTO_TOOL_GROUPS.get("minecraft"))
    kws = dl.TOOL_KEYWORDS["download_mc_file"]
    check("keywords, every weight at least 5", kws and all(isinstance(w, int) and w >= 5 for w in kws.values()))
    check("discovery took the keywords", "download_mc_file" in tools.AUTO_TOOL_KEYWORDS)
    others = set()
    for name, entry in tools.AUTO_TOOL_KEYWORDS.items():
        if name != "download_mc_file" and name in tools.AUTO_TOOL_GROUPS.get("minecraft", []):
            others |= set(entry)
    check("no keyword is shared with another minecraft tool", not (set(kws) & others), sorted(set(kws) & others))
    check("confirm-gated by name, and the framework took it",
          dl.TOOL_CONFIRM_REQUIRED == {"download_mc_file"} and "download_mc_file" in tool_safety.DEFAULT_CONFIRM_REQUIRED)
    check("no second pack instruction and no setting declared here", not hasattr(dl, "TOOL_PACK_INSTRUCTION") and not hasattr(dl, "TOOL_SETTINGS"))
    check("the group still has lookup's ONE instruction", tools.AUTO_TOOL_PACK_INSTRUCTIONS.get("minecraft", "").count(lk.TOOL_PACK_INSTRUCTION.strip()) == 1)
    check("no setting of this file in the discovered settings", not [e for e in tools.AUTO_SETTINGS if e["file_stem"] == "minecraft_download"])
    check("every setting the tool reads is declared by lookup", {"default_dest", "download_max_mb", "quilt_fabric_fallback"} <= {e["key"] for e in lk.TOOL_SETTINGS})
    check("the name does not say install", "install" not in "download_mc_file")
    schema = dl.TOOL_SCHEMAS[0]
    props = schema["parameters"]["properties"]
    check("schema: project is the only required argument", schema["name"] == "download_mc_file" and schema["parameters"]["required"] == ["project"])
    check("schema: enums match the code", props["loader"]["enum"] == list(sources.LOADERS) and props["kind"]["enum"] == list(sources.KINDS)
          and props["source"]["enum"] == list(sources.SOURCES) and props["channel"]["enum"] == list(resolve.CHANNELS))
    check("schema: dest, filename, file, with_dependencies, dry_run are there", {"dest", "filename", "file", "with_dependencies", "dry_run"} <= set(props))
    check("the description says queued, not installed", "not installed" in schema["description"] and "never 'installed'" in schema["description"])
    entry = dl.TEST_CHECKLIST["download_mc_file"]
    check("checklist entry passes the schema", checklist_schema.validate_entry("download_mc_file", entry) == [], checklist_schema.validate_entry("download_mc_file", entry))
    check("its own section id", entry["group"] == "minecraft_download" and set(dl.TEST_CHECKLIST_GROUP) == {"minecraft_download"}
          and dl.TEST_CHECKLIST_GROUP["minecraft_download"]["label"] == "Minecraft: downloads")
    check("discovery accepted entry and section", "download_mc_file" in tools.AUTO_TEST_CHECKLIST and tools.AUTO_TEST_CHECKLIST_GROUPS.get("minecraft_download"))
    steps = entry["steps"]
    check("checklist: a dry run, a real run to a folder and the CurseForge watch step",
          any((s.get("run") or {}).get("dry_run") for s in steps) and any("Save outside" in s["expect"] for s in steps)
          and any("HTTP 404" in s["expect"] for s in steps))
    seen = [s.get("ask") or json.dumps(s.get("run"), sort_keys=True) for s in steps]
    check("checklist: no duplicate step", len(seen) == len(set(seen)))
    if "download_mc_file" in tools.OWNER_ONLY_TOOLS:      # P7 adds the name (see the report); before that only the handler guards it
        with Sender(GUEST):
            check("a guest is not offered it", "download_mc_file" in tools.hidden_from_sender())
    check("every failure reason this file uses is a contract reason",
          all(r in REASONS for r in ("unsupported", "not_distributable", "vendor_changed", "bad_dest", "too_big", "no_compatible_file",
                                     "ambiguous", "not_found", "offline")))


# ------------------------------------------------------------------ guests

def test_a_guest_is_refused_before_any_call():
    with Adapters() as ad, Ask() as ask:
        n = count()
        for label, sender in (("a guest", GUEST), ("bad JSON", "{not json"), ("JSON that is not an object", "[1, 2]"),
                              ("nested too deep", "[" * 5000 + "]" * 5000)):
            with Sender(sender):
                res = call(project="sodium", dest=fresh("guest"), **FAB)
            check("%s is refused" % label, res["ok"] is False and res["reason"] == "unsupported"
                  and "owner" in res["error"] and "download_mc_file" in res["error"], res)
        check("no adapter was called, nothing was asked, no row appeared", ad.calls == [] and ask.calls == [] and count() == n)
        with Sender(OWNER):
            check("the owner on a chat platform is let through", call(project="sodium", dry_run=True, **FAB)["ok"] is True)
        with Sender(None):
            check("no chat context (the PC) is the owner", call(project="sodium", dry_run=True, **FAB)["ok"] is True)


# ------------------------------------------------------------------ scenario 2

def test_scenario_2_sodium_into_a_full_path():
    dest = fresh("scenario2")
    with Adapters() as ad, Ask() as ask:
        n = count()
        res = call(project="sodium", dest=dest, **FAB)
        check("ok, queued, not installed", res.get("ok") is True and res.get("queued") is True and res.get("installed") is False, res)
        msg = res.get("message", "")
        check("the words 'queued in Downloads; not installed'", "queued in Downloads; not installed" in msg, msg)
        check("it never says it installed anything", not re.search(r"\b(was|been|is|now) installed\b", msg), msg)
        check("one question, the hub's, naming the folder", len(ask.calls) == 1 and ask.calls[0]["title"] == "Save outside the downloads library?"
              and dest in ask.calls[0]["body"], ask.calls)
        check("exactly one row", count() == n + 1 and len(res["hub"]["ids"]) == 1, res.get("hub"))
        o = opts(res["hub"]["ids"][0])
        check("the row carries filename, verify (sha512 + size) and the folder, nothing else",
              o.get("filename") == "sodium-fabric-0.5.3+mc1.20.1.jar" and o.get("dest_abs") == dest
              and o.get("verify") == {"algo": "sha512", "value": h("sha512", "vSod0001"), "size": 1234}
              and set(o) <= {"filename", "verify", "dest_abs"}, o)
        check("the row's link is the vendor's own", row(res["hub"]["ids"][0]).get("source_url") == CATALOG["modrinth"]["files"]["AANobbMI"][0]["download_url"])
        check("the result names the algorithm and says it was set on the row", res["verify"] == {"algo": "sha512", "applied": True}, res.get("verify"))
        check("the newest compatible RELEASE file was picked (not the beta, not the Forge one)", res["file"]["file_id"] == "vSod0001"
              and res["saved_as"] == "sodium-fabric-0.5.3+mc1.20.1.jar" and res["dest"] == dest, res.get("file"))
        check("required dependencies are listed by name, optional ones are not",
              res["requires"] == [{"project_id": "P7dR8mSH", "name": "Fabric API"}], res.get("requires"))
        check("...and NOT queued", "dependencies_queued" not in res and "with_dependencies" in res.get("dependencies_note", ""))
        check("filters echoed, source named", (res["mc_version"], res["loader"], res["source"]) == ("1.20.1", "fabric", "modrinth"))
        check("the untrusted note marks vendor text", res["untrusted_note"] == untrusted.NOTE and "text_flagged" not in res)
        check("the result carries no key and no download link", FAKE_KEY not in json.dumps(res) and "cdn.modrinth.com" not in json.dumps(res), res)


def test_dry_run_resolves_and_queues_nothing():
    dest = fresh("dry")
    with Adapters() as ad, Ask() as ask:
        n = count()
        res = call(project="sodium", dest=dest, dry_run=True, **FAB)
        check("ok, dry_run, not queued, nothing asked, no row", res["ok"] is True and res["dry_run"] is True and res["queued"] is False
              and ask.calls == [] and count() == n, res)
        check("it says nothing was queued", "nothing was queued" in res["message"], res["message"])
        check("the exact file and the algorithm are reported", res["file"]["file_id"] == "vSod0001" and res["verify"]["algo"] == "sha512"
              and res["verify"]["applied"] is False and res["item_options"] == {"filename": "sodium-fabric-0.5.3+mc1.20.1.jar", "verify": {"algo": "sha512"}}, res)
        check("the hub's own routing decision is reported", res["hub_check"]["ok"] is True and res["hub_check"]["engine"] == "http", res.get("hub_check"))
        deps = call(project="sodium", dest=dest, dry_run=True, with_dependencies=True, **FAB)
        check("with_dependencies on a dry run lists what WOULD be queued", [d["project_id"] for d in deps["dependencies_would_queue"]] == ["P7dR8mSH"]
              and deps["dependencies_would_queue"][0]["name_of_project"] == "Fabric API" and count() == n, deps)
        bad = call(project="sodium", dest="~/.jarvis", dry_run=True, **FAB)
        check("a dry run refuses a protected folder too", bad["ok"] is False and bad["reason"] == "bad_dest", bad)


# ------------------------------------------------------------------ what is asked, not guessed

def test_minecraft_version_and_loader_are_asked_for_not_guessed():
    with Adapters() as ad, Ask() as ask:
        n = count()
        res = call(project="sodium", dest=fresh("clar"))
        check("neither given: a question, no file listed, nothing queued", res["ok"] is True and res["needs_clarification"] is True
              and "Minecraft version and loader" in res["question"] and "list_files" not in ad.names() and count() == n, res)
        res = call(project="sodium", mc_version="1.20.1", dest=fresh("clar"))
        check("only the loader missing: it names the loader", res.get("needs_clarification") and "loader" in res["question"] and "Minecraft version" not in res["question"], res)
        check("nothing was asked of the owner and nothing queued", ask.calls == [] and count() == n)


def test_a_pinned_file_a_shader_and_a_modpack():
    with Adapters() as ad, Ask():
        res = call(project="sodium", file="vSod0004", dest=fresh("pin"))
        check("a pinned file needs no filters and is labelled unfiltered", res["ok"] and res["queued"] and res["file"]["file_id"] == "vSod0004"
              and res["unfiltered"] is True and res["saved_as"] == "sodium-fabric-0.7.0+mc26.3.jar", res)
        res = call(project="sodium", file="vSod0003", dest=fresh("pin"), **FAB)
        check("a pinned file that does not match the filters is still the one asked for, with a warning",
              res["ok"] and res["queued"] and res["file"]["file_id"] == "vSod0003" and "fabric" in res["warning"], res)
        n = count()
        res = call(project="sodium", file="vFab0001", dest=fresh("pin"), **FAB)
        check("a file of ANOTHER project is refused", res["ok"] is False and res["reason"] == "not_found" and count() == n, res)
        res = call(project="sodium", file="../x", dest=fresh("pin"), **FAB)
        check("a file id is letters and digits only", res["ok"] is False and res["reason"] == "unsupported", res)
        res = call(project="pretty-shader", dest=fresh("shader"), loader="fabric")
        check("a shader needs no loader and no Minecraft version (unfiltered, loader not applied)", res["ok"] and res["queued"]
              and res["unfiltered"] is True and "loader" not in res and res["saved_as"] == "pretty-shader-1.2.zip", res)
        res = call(project="fabulous-pack", dest=fresh("pack"), **FAB)
        check("a Modrinth modpack is its .mrpack file", res["ok"] and res["queued"] and res["saved_as"] == "Fabulous-Pack-1.0.mrpack"
              and res["project"]["kind"] == "modpack", res)
        check("its row carries the .mrpack name and a checksum", opts(res["hub"]["ids"][0]).get("filename") == "Fabulous-Pack-1.0.mrpack"
              and opts(res["hub"]["ids"][0]).get("verify", {}).get("algo") == "sha512")


def test_quilt_falls_back_to_fabric_and_says_so():
    with Adapters() as ad, Ask():
        res = call(project="lithium", dest=fresh("quilt"), mc_version="1.20.1", loader="quilt")
        check("the Fabric file is offered for Quilt, labelled", res["ok"] and res["queued"] and res["file"]["via"] == "fabric"
              and "Fabric build" in res["via_note"] and res["loader"] == "quilt", res)
        check("the Quilt query did not carry a loader filter (Quilt queries never return Fabric files)",
              [c for c in ad.m.calls if c[0] == "list_files"][-1][3] is None)
        n = count()
        with Settings(quilt_fabric_fallback=False):
            res = call(project="lithium", dest=fresh("quilt"), mc_version="1.20.1", loader="quilt")
        check("fallback off: no_compatible_file with the alternatives, nothing queued", res["ok"] is False and res["reason"] == "no_compatible_file"
              and res["alternatives"] and "via" not in res["alternatives"][0] and count() == n, res)


# ------------------------------------------------------------------ scenario 5: no link, no workaround

def test_scenario_5_a_null_download_link_is_never_worked_around():
    with Adapters() as ad, Ask() as ask:
        n = count()
        res = call(project="448233", source="curseforge", dest=fresh("nodist"), **FAB)
        page = "https://www.curseforge.com/minecraft/mc-mods/entity-culling"
        check("not_distributable, with the project page", res["ok"] is False and res["reason"] == "not_distributable"
              and res["project_page"] == page and page in res["error"], res)
        check("no download-url route, no second try, nothing asked, nothing queued",
              "download_url" not in ad.names() and ask.calls == [] and count() == n, ad.calls)
        check("the project is named", res["project"]["id"] == "448233" and res["untrusted_note"])
        res = call(project="no-dist", dest=fresh("nodist"), **FAB)
        check("the same for a Modrinth file with no link", res["ok"] is False and res["reason"] == "not_distributable" and count() == n, res)
        res = call(project="odd-host", source="curseforge", dest=fresh("nodist"), **FAB)
        check("a link CurseForge's adapter dropped for its host is vendor_changed, not 'not distributable'",
              res["ok"] is False and res["reason"] == "vendor_changed" and "host" in res["error"] and count() == n, res)


def test_the_link_the_name_and_the_extension_are_checked():
    with Adapters() as ad, Ask():
        n = count()
        res = call(project="evil-host", dest=fresh("chk"), **FAB)
        check("a host off the allow-list is refused and named", res["ok"] is False and res["reason"] == "vendor_changed"
              and "evil.example.com" in res["error"] and count() == n, res)
        link = ad.m.files["Evil0001"][0]
        for label, url in (("plain http", "http://cdn.modrinth.com/x.jar"), ("credentials in the link", "https://u:p@cdn.modrinth.com/x.jar"),
                           ("a look-alike host", "https://cdn.modrinth.com.evil.example.com/x.jar")):
            link["download_url"] = url
            res = call(project="evil-host", dest=fresh("chk"), **FAB)
            check("%s is refused" % label, res["ok"] is False and res["reason"] == "vendor_changed" and count() == n, res)
        link["download_url"] = "https://cdn.modrinth.com/data/Evil0001/versions/vEvl0001/x.jar"
        check("the same file on the vendor's own host goes through", call(project="evil-host", dest=fresh("chk"), **FAB)["queued"] is True)
        res = call(project="not-a-jar", dest=fresh("chk"), **FAB)
        check("a file that is not .jar, .zip or .mrpack is refused", res["ok"] is False and res["reason"] == "unsupported"
              and ".mrpack" in res["error"], res)
        res = call(project="odd-name", dest=fresh("chk"), **FAB)
        check("a vendor name the hub would have to change is refused, not fixed", res["ok"] is False and res["reason"] == "unsupported"
              and "filename" in res["error"], res)
        res = call(project="odd-name", dest=fresh("chk"), filename="odd-name.jar", **FAB)
        check("...but an explicit safe filename works and is what the row carries", res["ok"] and res["queued"] and res["saved_as"] == "odd-name.jar"
              and opts(res["hub"]["ids"][0])["filename"] == "odd-name.jar", res)
        n = count()
        for label, name in (("a wrong extension", "sodium.txt"), ("a path", "../x.jar"), ("a reserved device name", "con.jar"),
                            ("a forbidden character", "a<b>.jar")):
            res = call(project="sodium", dest=fresh("chk"), filename=name, **FAB)
            check("filename with %s is refused" % label, res["ok"] is False and res["reason"] == "bad_dest" and count() == n, res)
        res = call(project="sodium", dest=fresh("chk"), filename=5, **FAB)
        check("a filename that is not text is refused", res["ok"] is False and res["reason"] == "unsupported")


# ------------------------------------------------------------------ the checksum

def test_checksums_by_vendor_and_what_is_never_claimed():
    with Adapters() as ad, Ask():
        res = call(project="sha1-only", dest=fresh("sum"), **FAB)
        o = opts(res["hub"]["ids"][0])
        check("Modrinth without sha512 falls back to sha1", res["verify"]["algo"] == "sha1" and o["verify"]["algo"] == "sha1"
              and o["verify"]["value"] == h("sha1", "s1only"), (res.get("verify"), o))
        res = call(project="jei", source="curseforge", dest=fresh("sum"), mc_version="1.20.1", loader="forge")
        o = opts(res["hub"]["ids"][0])
        check("CurseForge verifies with sha1, never with its fingerprint", res["ok"] and res["verify"]["algo"] == "sha1"
              and o["verify"] == {"algo": "sha1", "value": h("sha1", "5101"), "size": 777} and "2951345678" not in json.dumps(o), o)
        check("a CurseForge result carries the CDN note (the CDN has never served a file to the probe)", "404" in res["cdn_note"]
              and "vendor_changed" in res["cdn_note"] and "no other site" in res["cdn_note"], res.get("cdn_note"))
        check("the row's link is the CurseForge CDN link the adapter gave", row(res["hub"]["ids"][0])["source_url"].startswith("https://edge.forgecdn.net/files/5/101/"))
        check("a Modrinth result has no CDN note", "cdn_note" not in call(project="sodium", dest=fresh("sum"), dry_run=True, **FAB))

        res = call(project="no-hash", dest=fresh("sum"), **FAB)
        check("no published checksum: queued WITHOUT one and said so in words", res["ok"] and res["queued"] and "verify" not in opts(res["hub"]["ids"][0])
              and res["verify"]["algo"] is None and res["verify"]["applied"] is False and "not verified" in res["message"]
              and "NOT covered" in res["warning"] and "not be verified" in res["verify_note"], res)
        n = count()
        res = call(project="bad-hash", dest=fresh("sum"), **FAB)
        check("a vendor hash that is not a hash: nothing is queued", res["ok"] is False and res["reason"] == "vendor_changed"
              and "checksum" in res["error"] and count() == n, res)


def test_the_hub_ignoring_verify_is_read_from_its_note_and_reported():
    real = dh._queue

    def ignoring(args, urls, engine, engine_options, label, item_options=None):
        return {"ok": True, "added": [{"id": "x1", "title": "t", "engine": "ytdlp", "state": "queued", "duplicate": False}],
                "note": "verify only applies to links the http engine takes; it was ignored for the other links."}
    dh._queue = ignoring
    try:
        with Adapters(), Ask():
            res = call(project="sodium", dest=fresh("note"), **FAB)
    finally:
        dh._queue = real
    msg = res.get("message", "")
    check("still queued, but the checksum is reported as NOT applied", res["ok"] and res["queued"] is True and res["verify"]["applied"] is False
          and "why" in res["verify"], res)
    check("the warning and the message do not claim a check", "NOT covered" in res["warning"] and "not verified" in msg
          and "is checked against" not in msg and "checked against its" not in msg, msg)
    check("the hub's note is passed on", "verify only applies" in res["hub_note"], res)


def test_a_duplicate_row_is_never_claimed_as_verified():
    dest = fresh("dup")
    with Adapters(), Ask():
        first = call(project="sodium", dest=dest, **FAB)
        n = count()
        second = call(project="sodium", dest=dest, **FAB)
        check("the first call queued with its checksum", first["queued"] is True and first["verify"]["applied"] is True)
        check("the second finds the row already there: nothing new, said in words, and no checksum claim",
              second["ok"] and second["queued"] is False and second["hub"]["duplicate"] is True and "already in Downloads" in second["message"]
              and second["verify"]["applied"] is False and "can't promise" in second["message"] and count() == n, second)
        check("...with the same id", second["hub"]["ids"] == first["hub"]["ids"])
        dest2 = fresh("dup")
        name = "sodium-fabric-0.5.3+mc1.20.1.jar"
        url = CATALOG["modrinth"]["files"]["AANobbMI"][0]["download_url"]
        early = dh._queue({"dest": dest2}, [url], "http", None, "dl_add_http", item_options={"filename": name})
        again = call(project="sodium", dest=dest2, **FAB)
        check("a row an earlier tool queued WITHOUT a checksum is not reported as verified", early["ok"] and again["queued"] is False
              and again["verify"]["applied"] is False and "verify" not in opts(early["added"][0]["id"]), again)


# ------------------------------------------------------------------ the destination

def test_the_destination_rules_are_the_hubs_and_come_first():
    with Adapters() as ad, Ask() as ask:
        n = count()
        cases = [("~/.jarvis", "bad_dest"), ("~/.jarvis/x", "bad_dest"), (HOME, "bad_dest"), ("/", "bad_dest"),
                 ("/etc/mods", "bad_dest"), (HOME + "/a/../b", "bad_dest"), ({"a": 1}, "unsupported")]
        for dest, reason in cases:
            res = call(project="sodium", dest=dest, **FAB)
            check("dest %r is refused as %s" % (dest, reason), res["ok"] is False and res["reason"] == reason, res)
        check("refused before any adapter call or question, nothing queued", ad.calls == [] and ask.calls == [] and count() == n, (ad.calls, ask.calls))
        res = call(project="sodium", dest=HOME, **FAB)
        check("the home folder's refusal says why", "home folder" in res["error"], res)
        res = call(project="sodium", dest="Minecraft/mods-test", **FAB)
        check("a sub-folder of the library needs no question", res["ok"] and res["queued"] and ask.calls == []
              and opts(res["hub"]["ids"][0]).get("subdir") == "Minecraft/mods-test" and res["dest"] == str(LIB / "Minecraft" / "mods-test"), res)
    with Adapters(), Ask(False) as ask:
        n = count()
        res = call(project="sodium", dest=fresh("no"), **FAB)
        check("a declined 'Save outside the downloads library?' queues nothing and says so", res["ok"] is False and res["reason"] == "bad_dest"
              and res.get("cancelled") is True and "nothing was queued" in res["error"] and count() == n and len(ask.calls) == 1, res)
    with Adapters(), Ask() as ask:
        res = call(project="sodium", **FAB)
        want = str(Path(HOME) / "Downloads")
        check("dest omitted = the default folder (~/Downloads), through the hub's question", res["ok"] and res["queued"] and res["dest"] == want
              and opts(res["hub"]["ids"][0]).get("dest_abs") == want and len(ask.calls) == 1, (res.get("dest"), ask.calls))
        mine = fresh("mydefault")
        with Settings(default_dest=mine):
            res = call(project="lithium", **FAB)
        check("the default_dest setting is honoured", res["ok"] and res["dest"] == mine and opts(res["hub"]["ids"][0]).get("dest_abs") == mine, res)


def test_the_size_limit_setting_zero_means_unlimited():
    with Adapters(), Ask():
        res = call(project="big-mod", dest=fresh("size"), **FAB)
        check("no limit by default: a 5 MB file is queued, its size becomes the row's ceiling", res["ok"] and res["queued"]
              and opts(res["hub"]["ids"][0])["verify"]["size"] == 5 * 1024 * 1024, res)
        n = count()
        with Settings(download_max_mb=1):
            res = call(project="big-mod", dest=fresh("size"), **FAB)
            check("over the limit: too_big, nothing queued", res["ok"] is False and res["reason"] == "too_big" and "download_max_mb" in res["error"]
                  and count() == n, res)
            res = call(project="no-size", dest=fresh("size"), **FAB)
            check("size unknown with a limit: the limit becomes the row's ceiling", res["ok"] and res["queued"]
                  and opts(res["hub"]["ids"][0])["verify"]["size"] == 1024 * 1024, res)
        res = call(project="no-size", dest=fresh("size"), **FAB)
        check("size unknown and no limit: no size on the row", res["ok"] and "size" not in opts(res["hub"]["ids"][0])["verify"], res)


# ------------------------------------------------------------------ dependencies

def test_dependencies_are_listed_and_queued_only_when_asked():
    with Adapters(), Ask() as ask:
        n = count()
        res = call(project="needs-things", dest=fresh("deps"), **FAB)
        names = {r["project_id"]: r["name"] for r in res["requires"]}
        check("listed by name (an unknown one with an empty name), one row only", names == {"P7dR8mSH": "Fabric API", "NoDist01": "No Dist", "GhostPr1": ""}
              and count() == n + 1 and "dependencies_queued" not in res, res)
    with Adapters(), Ask() as ask:
        n = count()
        res = call(project="needs-things", dest=fresh("deps"), with_dependencies=True, **FAB)
        queued = res["dependencies_queued"]
        check("with_dependencies queues each placeable dependency as its own row, with its own checksum",
              res["ok"] and count() == n + 2 and [d["project_id"] for d in queued] == ["P7dR8mSH"] and queued[0]["verify"] == "sha512"
              and queued[0]["checksum_set"] is True and opts(queued[0]["hub_ids"][0])["filename"] == "fabric-api-0.92.0+1.20.1.jar"
              and opts(queued[0]["hub_ids"][0])["verify"]["value"] == h("sha512", "vFab0001"), res)
        skipped = {s["project_id"]: s["reason"] for s in res["dependencies_skipped"]}
        check("the ones that cannot be placed are skipped with a reason", skipped == {"GhostPr1": "no_compatible_file", "NoDist01": "not_distributable"}, skipped)
        check("each row asked its own question and the message says so", len(ask.calls) == 2 and "Also queued 1 required dependency" in res["message"], ask.calls)
    with Adapters(), Ask(True, False):
        n = count()
        res = call(project="needs-things", dest=fresh("deps"), with_dependencies=True, **FAB)
        check("declining the folder for a dependency keeps the main file and reports the rest as not confirmed",
              res["ok"] and res["queued"] and count() == n + 1 and res["dependencies_queued"] == []
              and {"project_id": "P7dR8mSH", "name": "Fabric API", "reason": "not_confirmed"} in res["dependencies_skipped"], res)


# ------------------------------------------------------------------ failures

def test_ambiguity_not_found_and_no_compatible_file():
    with Adapters() as ad, Ask():
        n = count()
        res = call(project="Twin Mod", dest=fresh("fail"), **FAB)
        check("two projects with the same name: the candidates, never a pick", res["ok"] is False and res["reason"] == "ambiguous"
              and sorted(c["id"] for c in res["candidates"]) == ["TwinA001", "TwinB002"] and "list_files" not in ad.names()
              and res["untrusted_note"] and count() == n, res)
        res = call(project="zzz-nothing-xyz", dest=fresh("fail"), **FAB)
        check("an unknown project is not_found", res["ok"] is False and res["reason"] == "not_found" and count() == n, res)
        res = call(project="sodium", dest=fresh("fail"), mc_version="1.2.3", loader="fabric")
        check("no compatible file lists the closest alternatives, nothing queued", res["ok"] is False and res["reason"] == "no_compatible_file"
              and res["project"]["slug"] == "sodium" and 0 < len(res["alternatives"]) <= 5 and count() == n, res)
        res = call(project="neo-only", dest=fresh("fail"), mc_version="1.20.4", loader="forge")
        check("Forge is not NeoForge", res["ok"] is False and res["reason"] == "no_compatible_file", res)
        res = call(project="sodium", dest=fresh("fail"), mc_version="26.3", loader="fabric", dry_run=True)
        check("a 26.3 style version resolves", res["ok"] and res["file"]["file_id"] == "vSod0004", res)


def test_argument_errors_make_no_call():
    with Adapters() as ad, Ask() as ask:
        for label, args in (("no project", {}), ("a bad source", {"project": "x", "source": "bing"}), ("a bad kind", {"project": "x", "kind": "plugin"}),
                            ("a bad channel", {"project": "x", "channel": "nightly"}), ("a Minecraft version that is 'latest'", {"project": "x", "mc_version": "latest"}),
                            ("a version range", {"project": "x", "mc_version": "1.20.x"}), ("a bad loader", {"project": "x", "loader": "liteloader"}),
                            ("a project that is a list", {"project": ["x"]})):
            res = call(**args)
            check("%s: unsupported, one sentence" % label, res["ok"] is False and res["reason"] == "unsupported" and res["error"], res)
        for junk in (None, "sodium", 5, ["sodium"]):
            res = dl.tool_download_mc_file(junk)
            check("arguments that are not an object (%r) do not raise" % (junk,), res["ok"] is False)
        check("no adapter call, no question", ad.calls == [] and ask.calls == [])


def test_adapters_that_fail_or_are_missing():
    with Adapters(curseforge=False), Ask():
        res = call(project="jei", source="curseforge", dest=fresh("miss"), mc_version="1.20.1", loader="forge")
        check("CurseForge not installed: one plain sentence", res["ok"] is False and res["reason"] == "unsupported"
              and res["error"] == "CurseForge support is not installed yet.", res)
    with Adapters() as ad, Ask():
        ad.c.fail["get_project"] = {"ok": False, "reason": "no_key", "error": "CurseForge needs a personal API key, and none is set."}
        res = call(project="jei", source="curseforge", dest=fresh("miss"), mc_version="1.20.1", loader="forge")
        check("no key: the adapter's reason and sentence pass through", res["ok"] is False and res["reason"] == "no_key" and "personal API key" in res["error"], res)
        ad.m.fail["list_files"] = {"ok": False, "reason": "rate_limited", "error": "Modrinth asks to wait 30 seconds.", "body": "SECRET BODY"}
        res = call(project="sodium", dest=fresh("miss"), **FAB)
        check("rate_limited passes through and a vendor body never does", res["ok"] is False and res["reason"] == "rate_limited"
              and "30 seconds" in res["error"] and "SECRET" not in json.dumps(res), res)
        ad.m.fail.clear()
        ad.m.boom = True
        res = call(project="sodium", dest=fresh("miss"), **FAB)
        check("an adapter that raises becomes a failure dict", res["ok"] is False and res["reason"] in REASONS and res["error"], res)


# ------------------------------------------------------------------ vendor text, secrets, results

def test_vendor_text_is_clipped_and_marked():
    with Adapters(), Ask():
        res = call(project="inject", dest=fresh("txt"), dry_run=True, **FAB)
        check("injection phrasing is flagged and still only text", res["ok"] and res.get("text_flagged") is True
              and untrusted.FLAGGED_NOTE.strip() in res["untrusted_note"] and res["queued"] is False, res)


def test_the_key_and_headers_never_reach_a_result_or_a_row():
    cfg = Path(HOME) / ".jarvis"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "curseforge.json").write_text(json.dumps({"api_key": FAKE_KEY}), encoding="utf-8")
    os.environ["CURSEFORGE_API_KEY"] = FAKE_KEY
    try:
        results = []
        with Adapters() as ad, Ask():
            results.append(call(project="jei", source="curseforge", dest=fresh("key"), mc_version="1.20.1", loader="forge"))
            results.append(call(project="448233", source="curseforge", dest=fresh("key"), **FAB))
            results.append(call(project="sodium", dest=fresh("key"), with_dependencies=True, **FAB))
            results.append(call(project="sodium", dest=fresh("key"), dry_run=True, **FAB))
            rows = [row(r["id"]) for r in all_rows()]
            blob = json.dumps([results, rows, ad.calls], default=str)
        check("the key is in no result, no hub row and no adapter call", FAKE_KEY not in blob and "x-api-key" not in blob.lower(), blob[:200])
        allowed = {"filename", "verify", "dest_abs", "subdir"}
        stored = [opts(r["id"]) for r in all_rows()]
        check("every row this tool queued carries only filename / verify / folder (no header, no Referer)",
              stored and all(set(o) <= allowed for o in stored), [sorted(o) for o in stored][:3])
    finally:
        os.environ.pop("CURSEFORGE_API_KEY", None)


def test_result_specs_shrink_but_never_drop_what_was_and_was_not_done():
    with Adapters(), Ask():
        res = call(project="needs-things", dest=fresh("spec"), with_dependencies=True, **FAB)
        dry = call(project="sodium", dest=fresh("spec"), dry_run=True, **FAB)
        for level in ("medium", "low"):
            shaped = tool_result_shaping.shape_result("download_mc_file", res, level)
            keep = all(k in shaped for k in ("message", "verify", "queued", "installed", "untrusted_note", "project", "file", "saved_as", "dest"))
            check("%s keeps the message, the checksum state, the not-installed flag and the untrusted note" % level, keep, sorted(shaped))
            check("%s keeps every skipped dependency's reason" % level, all("reason" in s for s in shaped["dependencies_skipped"]), shaped.get("dependencies_skipped"))
        check("medium drops the hub check, low also the hub note", "hub_check" not in tool_result_shaping.shape_result("download_mc_file", dry, "medium")
              and "hub_note" not in tool_result_shaping.shape_result("download_mc_file", res, "low"))
        low = tool_result_shaping.shape_result("download_mc_file", res, "low")
        check("low shortens the dependency entries but keeps their ids", all("hub_ids" in d and "channel" not in d for d in low["dependencies_queued"]), low["dependencies_queued"])
        check("full leaves it as it was", tool_result_shaping.shape_result("download_mc_file", res, "full") == res)
        drops = [x for level in (dl.TOOL_RESULT_SPECS["download_mc_file"].get("drop_fields") or {}).values() for x in level]
        check("the spec never drops the note, the message, the warning or the CDN note",
              not ({"untrusted_note", "message", "warning", "cdn_note", "verify", "queued", "ok"} & set(drops)), drops)


def test_a_tool_never_raises():
    with Adapters(), Ask():
        for args in ({"project": "sodium", "dest": fresh("raise"), "mc_version": 5, "loader": object()},
                     {"project": "sodium", "dest": fresh("raise"), "with_dependencies": "yes", "dry_run": "false", **FAB},
                     {"project": "sodium", "dest": "x" * 5000, **FAB}, {"project": 12345, "dest": fresh("raise"), **FAB}):
            res = dl.tool_download_mc_file(args)
            check("junk arguments %s give a dict with ok" % list(args)[:3], isinstance(res, dict) and "ok" in res)
    check("every reason the tool answers with is in the contract's list", all(r in REASONS for r in ("unsupported", "vendor_changed", "bad_dest")))


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
