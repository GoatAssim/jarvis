"""Download Hub N14 (brief 14): jarvis/hub_browse.py, the listing behind the folder / file picker, and its
two `jarvis hub-admin` operations (`browse`, `mkdir`).

Covers: sorting (folders first, natural order, no case); hidden and system entries; the entry cap and the scan
cap; a path that does not exist (nearest folder that does), a file where a folder is wanted, a relative path, a
network path, a bad kind; extension filtering for a file pick; links (a link back up the tree is left out);
names and kinds only (no contents); the roots on Windows / macOS / Linux (the platform, home, drives and
library root are passed in); the new-folder rules (one level, a plain name, never over something that exists);
`hub_admin.admin` for both ops, the refusal for an unattended process, and the JSON-in / JSON-out CLI entry;
and the plumbing (the routes in web/server.js, the picker files, the one script and stylesheet in index.html).

Run: python tests/test_hub_browse.py
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-browse-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_TASK_ID", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI))

from jarvis import hub_admin, hub_browse  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def names(answer, kind=None):
    return [e["name"] for e in answer["entries"] if kind is None or e["kind"] == kind]


def tree():
    """A temporary folder tree with the awkward cases in it."""
    base = Path(tempfile.mkdtemp(prefix="browse-tree-", dir=HOME)) / "root"
    base.mkdir(parents=True)
    for d in ("banana", "Apple", "cherry", "Disc 2", "Disc 10", "Disc 1", ".hidden", "$Recycle.Bin", "System Volume Information"):
        (base / d).mkdir()
    for f in ("setup.EXE", "tool.exe", "notes.txt", "readme.md", ".secret.exe", "image.iso"):
        (base / f).write_text("x" * 10, encoding="utf-8")
    return base


def test_pure():
    key = hub_browse.natural_key
    check("natural order: Disc 2 before Disc 10", sorted(["Disc 10", "Disc 2", "Disc 1"], key=key) == ["Disc 1", "Disc 2", "Disc 10"])
    check("sorting ignores case", sorted(["banana", "Apple", "cherry"], key=key) == ["Apple", "banana", "cherry"])
    check("a name that is only digits sorts without error", sorted(["10", "9", "a", "B"], key=key) == ["9", "10", "a", "B"])
    check("a dot-name is hidden", hub_browse.is_hidden(".git"))
    check("a Windows system folder is hidden by name", hub_browse.is_hidden("$Recycle.Bin") and hub_browse.is_hidden("System Volume Information"))
    check("the Windows hidden and system attributes hide an entry", hub_browse.is_hidden("x", attrs=0x2) and hub_browse.is_hidden("x", attrs=0x4) and not hub_browse.is_hidden("x", attrs=0x10))
    check("/proc, /sys and /dev are left out at the top of a POSIX tree only",
          hub_browse.is_hidden("proc", parent_is_root=True, platform="linux") and not hub_browse.is_hidden("proc", parent_is_root=False, platform="linux")
          and not hub_browse.is_hidden("proc", parent_is_root=True, platform="win32"))
    check("a normal name is not hidden", not hub_browse.is_hidden("Games") and not hub_browse.is_hidden("My Folder"))
    check("extensions: lower case, with a dot, no duplicates, junk dropped",
          hub_browse.norm_extensions([".EXE", "exe", "*.msi", "iso", 5, "bad name", "", None, ".EXE"]) == [".exe", ".msi", ".iso"])
    check("extensions can come as one text", hub_browse.norm_extensions(".exe; .bat, cmd") == [".exe", ".bat", ".cmd"])
    check("extensions are capped", len(hub_browse.norm_extensions(["e%d" % i for i in range(60)])) == hub_browse.MAX_EXTENSIONS)
    check("no extension filter keeps every file", hub_browse.wanted_file("a.bin", []))
    check("extension filter ignores case", hub_browse.wanted_file("A.EXE", [".exe"]) and not hub_browse.wanted_file("a.txt", [".exe"]))
    check("clean_path_text: empty is fine", hub_browse.clean_path_text("  ") == ("", None) and hub_browse.clean_path_text(None) == ("", None))
    check("clean_path_text: strips quotes", hub_browse.clean_path_text('"/tmp/x"') == ("/tmp/x", None))
    check("clean_path_text: NUL refused", hub_browse.clean_path_text("a\x00b")[1] is not None)
    check("clean_path_text: too long refused", hub_browse.clean_path_text("/" + "a" * 2000)[1] is not None)
    check("clean_path_text: a network path is refused with a hint", "mapped drive" in (hub_browse.clean_path_text("\\\\server\\share")[1] or ""))
    check("clean_path_text: not text refused", hub_browse.clean_path_text(5)[1] is not None)
    crumbs = hub_browse.breadcrumbs("/home/me/Games")
    check("breadcrumbs run from the root down", [c["name"] for c in crumbs] == ["/", "home", "me", "Games"] and crumbs[-1]["path"] == "/home/me/Games" and crumbs[0]["path"] == "/")
    check("parent_of stops at the root", hub_browse.parent_of("/home/me") == "/home" and hub_browse.parent_of("/") is None)


def test_roots():
    home = Path(tempfile.mkdtemp(prefix="browse-home-", dir=HOME))
    (home / "Desktop").mkdir()
    (home / "Documents").mkdir()
    (home / "Downloads").mkdir()
    lib = home / "Downloads" / "Jarvis"
    lib.mkdir()
    win = hub_browse.roots(platform="win32", home=home, library_root=lib, drives=["C:\\", "D:\\"])
    kinds = [r["kind"] for r in win]
    check("Windows: home, Desktop, Documents, Downloads, the library, then each drive", kinds == ["home", "desktop", "documents", "downloads", "library", "drive", "drive"])
    check("Windows: the drives are listed as given", [r["path"] for r in win if r["kind"] == "drive"] == ["C:\\", "D:\\"])
    mac = hub_browse.roots(platform="darwin", home=home, library_root=None)
    check("macOS: /Volumes after the home places", mac[-1]["kind"] == "volumes" and mac[-1]["path"] == "/Volumes" or not os.path.isdir("/Volumes"))
    lin = hub_browse.roots(platform="linux", home=home, library_root=str(lib))
    check("Linux: / and home", lin[-1] == {"name": "Computer (/)", "path": "/", "kind": "root"} and lin[0]["kind"] == "home")
    bare = Path(tempfile.mkdtemp(prefix="browse-bare-", dir=HOME))
    only_home = hub_browse.roots(platform="linux", home=bare, library_root=str(bare / "nope"))
    check("a place that does not exist is not offered", [r["kind"] for r in only_home] == ["home", "root"])
    one = Path(tempfile.mkdtemp(prefix="browse-onedrive-", dir=HOME))
    (one / "OneDrive" / "Desktop").mkdir(parents=True)
    check("Desktop is found under OneDrive when it moved there", any(r["kind"] == "desktop" and "OneDrive" in r["path"] for r in hub_browse.roots(platform="linux", home=one)))
    check("a drive list that is empty adds no drive", not [r for r in hub_browse.roots(platform="win32", home=bare, drives=[]) if r["kind"] == "drive"])


def test_listing():
    base = tree()
    a = hub_browse.browse({"path": str(base)}, library_root=None)
    check("a folder pick lists sub-folders only, folders first, in natural order",
          a["ok"] and names(a) == ["Apple", "banana", "cherry", "Disc 1", "Disc 2", "Disc 10"] and set(e["kind"] for e in a["entries"]) == {"folder"})
    check("hidden and system folders are left out", ".hidden" not in names(a) and "$Recycle.Bin" not in names(a) and "System Volume Information" not in names(a))
    check("the answer names the folder, its parent and a crumb trail", a["path"] == str(base) and a["parent"] == str(base.parent) and a["crumbs"][-1]["name"] == "root")
    check("every entry carries its full path", all(e["path"] == str(base / e["name"]) for e in a["entries"]))
    check("nothing is cut and nothing is missing", a["more"] == 0 and a["shown"] == 6 and a["scan_capped"] is False)

    shown = hub_browse.browse({"path": str(base), "hidden": True})
    check("hidden: true shows hidden folders too", ".hidden" in names(shown) and "$Recycle.Bin" in names(shown))

    f = hub_browse.browse({"path": str(base), "kind": "file", "extensions": [".exe", "iso"]})
    check("a file pick lists the folders and only the wanted files", names(f, "file") == ["image.iso", "setup.EXE", "tool.exe"])
    check("folders come before files", [e["kind"] for e in f["entries"]] == ["folder"] * 6 + ["file"] * 3)
    check("a hidden file is left out", ".secret.exe" not in names(f))
    file_entry = [e for e in f["entries"] if e["name"] == "tool.exe"][0]
    check("a file has a size and a modified time", file_entry["size"] == 10 and isinstance(file_entry["modified"], int))
    check("a folder has neither", all("size" not in e for e in f["entries"] if e["kind"] == "folder"))
    allf = hub_browse.browse({"path": str(base), "kind": "file"})
    check("a file pick with no extension filter lists every file", names(allf, "file") == ["image.iso", "notes.txt", "readme.md", "setup.EXE", "tool.exe"])
    check("a folder pick never lists files, whatever the extensions say", names(hub_browse.browse({"path": str(base), "extensions": [".exe"]}), "file") == [])
    allowed = {"name", "path", "kind", "link", "size", "modified", "place"}
    check("entries carry names, kinds, paths, a size and a date: never contents", all(set(e) <= allowed for e in f["entries"]))
    check("the answer never includes file contents", "xxxxxxxxxx" not in json.dumps(f))

    check("a bad kind is refused", hub_browse.browse({"path": str(base), "kind": "disk"})["ok"] is False)
    check("a request that is not an object still answers (the roots)", hub_browse.browse("nope")["ok"] is True)


def test_cap():
    base = Path(tempfile.mkdtemp(prefix="browse-cap-", dir=HOME))
    for i in range(hub_browse.MAX_ENTRIES + 25):
        (base / ("d%04d" % i)).mkdir()
    a = hub_browse.browse({"path": str(base)})
    check("the answer is capped at MAX_ENTRIES", len(a["entries"]) == hub_browse.MAX_ENTRIES and a["shown"] == hub_browse.MAX_ENTRIES)
    check("the answer says how many were left out", a["more"] == 25)
    check("the cap keeps the first names in order", a["entries"][0]["name"] == "d0000" and a["entries"][-1]["name"] == "d%04d" % (hub_browse.MAX_ENTRIES - 1))
    old = hub_browse.MAX_SCAN
    try:
        hub_browse.MAX_SCAN = 10
        b = hub_browse.browse({"path": str(base)})
        check("a huge folder stops being scanned and says so", b["scan_capped"] is True and b["shown"] <= 10)
    finally:
        hub_browse.MAX_SCAN = old
    old_cap = hub_browse.MAX_ENTRIES
    try:
        hub_browse.MAX_ENTRIES = 4
        mixed = Path(tempfile.mkdtemp(prefix="browse-cap2-", dir=HOME))
        for d in ("a", "b", "c"):
            (mixed / d).mkdir()
        for fl in ("x.txt", "y.txt", "z.txt"):
            (mixed / fl).write_text("1")
        c = hub_browse.browse({"path": str(mixed), "kind": "file"})
        check("the cap counts folders and files together, folders first", names(c) == ["a", "b", "c", "x.txt"] and c["more"] == 2)
    finally:
        hub_browse.MAX_ENTRIES = old_cap


def test_missing_and_odd_paths():
    base = tree()
    deep = base / "banana" / "no" / "such" / "place"
    a = hub_browse.browse({"path": str(deep)})
    check("a missing path answers with the nearest folder that exists", a["ok"] and a["path"] == str(base / "banana"))
    check("and says so", "doesn't exist" in a["note"])
    f = base / "notes.txt"
    b = hub_browse.browse({"path": str(f)})
    check("a file where a folder is wanted shows the folder it is in", b["ok"] and b["path"] == str(base))
    check("and says so, and names the file", "file, not a folder" in b["note"] and b["selected"] == "notes.txt")
    c = hub_browse.browse({"path": str(f), "kind": "file", "extensions": [".txt"]})
    check("a file pick pointed at a file shows its folder with the file chosen", c["path"] == str(base) and c["selected"] == "notes.txt" and c["note"] == "")
    under_file = hub_browse.browse({"path": str(f / "inside")})
    check("a path below a file falls back to the folder that holds the file", under_file["ok"] and under_file["path"] == str(base) and "doesn't exist" in under_file["note"])
    check("a relative path is refused with a hint", hub_browse.browse({"path": "Games/Old"})["ok"] is False and "full path" in hub_browse.browse({"path": "Games/Old"})["error"])
    check("a network path is refused", hub_browse.browse({"path": "\\\\nas\\media"})["ok"] is False)
    check("a path with NUL is refused", hub_browse.browse({"path": str(base) + "\x00"})["ok"] is False)
    check("a path nested absurdly deep is refused", hub_browse.browse({"path": "/" + "/".join(["d"] * 200)})["ok"] is False)
    home = hub_browse.browse({"path": "~"})
    check("~ means the home folder", home["ok"] and home["path"] == os.path.normpath(os.path.expanduser("~")))
    dots = hub_browse.browse({"path": str(base / "banana" / ".." / "cherry")})
    check("a path with .. is tidied before it is listed", dots["ok"] and dots["path"] == str(base / "cherry"))

    roots_answer = hub_browse.browse({"path": ""}, library_root=None)
    check("no path lists the roots", roots_answer["ok"] and roots_answer["path"] == "" and roots_answer["parent"] is None and roots_answer["crumbs"] == [] and len(roots_answer["entries"]) >= 1)
    check("the roots are folders the picker can open", all(e["kind"] == "folder" and e.get("path") for e in roots_answer["entries"]))
    check("the places come with every answer", len(a["places"]) >= 1 and a["places"] == hub_browse.roots(library_root=None))
    lib = hub_browse.browse({"path": str(base)}, library_root=str(base))
    check("the library root is one of the places", any(p["kind"] == "library" and p["path"] == str(base) for p in lib["places"]))


def test_links():
    base = Path(tempfile.mkdtemp(prefix="browse-links-", dir=HOME))
    (base / "real").mkdir()
    (base / "real" / "inner").mkdir()
    try:
        os.symlink(str(base), str(base / "real" / "back_to_top"))          # a link up the tree: a loop
        os.symlink(str(base / "real"), str(base / "shortcut"))             # a link to a sibling: fine
        os.symlink(str(base / "gone"), str(base / "broken"))               # a link to nothing
        os.symlink(str(base / "loop_b"), str(base / "loop_a"))             # a link cycle
        os.symlink(str(base / "loop_a"), str(base / "loop_b"))
    except (OSError, NotImplementedError, AttributeError):
        print("skipped symlink checks (links not allowed here)")
        return
    top = hub_browse.browse({"path": str(base)})
    check("a link to a folder is listed and marked as a link", "shortcut" in names(top) and [e for e in top["entries"] if e["name"] == "shortcut"][0]["link"] is True)
    check("a broken link and a link cycle are left out", "broken" not in names(top) and "loop_a" not in names(top) and "loop_b" not in names(top))
    inside = hub_browse.browse({"path": str(base / "real")})
    check("a link back up the tree is left out (it leads nowhere new)", "back_to_top" not in names(inside) and "inner" in names(inside))
    cyc = hub_browse.browse({"path": str(base / "loop_a")})
    check("asking for a link cycle answers with a folder that exists", cyc["ok"] and cyc["path"] == str(base))


def test_unreadable():
    # A folder the process may not open, simulated (a root user can read everything, and Windows differs):
    base = Path(tempfile.mkdtemp(prefix="browse-denied-", dir=HOME))
    (base / "secret").mkdir()
    real_scandir = os.scandir

    def denying(path="."):
        if os.path.normpath(str(path)) == str(base / "secret"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)
    os.scandir = denying
    try:
        a = hub_browse.browse({"path": str(base / "secret")})
        b = hub_browse.browse({"path": str(base / "secret" / "deeper")})
    finally:
        os.scandir = real_scandir
    check("a folder that cannot be opened falls back to its parent, with a note (simulated)", a["ok"] and a["path"] == str(base) and "can't open" in a["note"])
    check("a missing path below a folder that cannot be opened still lands somewhere that works (simulated)", b["ok"] and b["path"] == str(base))
    check("no Windows drive list is invented on another system", os.name == "nt" or hub_browse._windows_drives() == [])
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        print("skipped the real chmod check (running as root, or not POSIX)")
        return
    base = Path(tempfile.mkdtemp(prefix="browse-perm-", dir=HOME))
    (base / "locked").mkdir()
    (base / "locked" / "x").mkdir()
    os.chmod(base / "locked", 0)
    try:
        a = hub_browse.browse({"path": str(base / "locked")})
        check("a folder that cannot be read falls back to its parent, with a note", a["ok"] and a["path"] == str(base) and "can't open" in a["note"])
    finally:
        os.chmod(base / "locked", 0o700)


def test_new_folder():
    base = Path(tempfile.mkdtemp(prefix="browse-mk-", dir=HOME))
    ok = hub_browse.make_folder({"parent": str(base), "name": "Games"})
    check("a new folder is made", ok["ok"] and (base / "Games").is_dir() and ok["path"] == str(base / "Games") and ok["parent"] == str(base))
    again = hub_browse.make_folder({"parent": str(base), "name": "Games"})
    check("a name that exists is refused, not reused", again["ok"] is False and "already" in again["error"])
    (base / "file.txt").write_text("1")
    over_file = hub_browse.make_folder({"parent": str(base), "name": "file.txt"})
    check("a name that is a file is refused too", over_file["ok"] is False)
    for bad in ("", "   ", "a/b", "a\\b", "..", ".", "con", "NUL", "bad:name", 'say"hi"', "q?", "x" * 200, ".hidden", "trailing.", None, 5):
        r = hub_browse.make_folder({"parent": str(base), "name": bad})
        check("a bad name is refused: %r" % (bad if not isinstance(bad, str) or len(bad) < 20 else "long"), r["ok"] is False and bool(r["error"]))
    check("nothing was made by the refused names", sorted(p.name for p in base.iterdir()) == ["Games", "file.txt"])
    check("a missing parent is refused", hub_browse.make_folder({"parent": str(base / "nope"), "name": "x"})["ok"] is False)
    check("no parent is refused", hub_browse.make_folder({"name": "x"})["ok"] is False)
    check("a relative parent is refused", hub_browse.make_folder({"parent": "rel/path", "name": "x"})["ok"] is False)
    check("a parent that is a file is refused", hub_browse.make_folder({"parent": str(base / "file.txt"), "name": "x"})["ok"] is False)
    check("a network parent is refused", hub_browse.make_folder({"parent": "\\\\nas\\share", "name": "x"})["ok"] is False)
    spaced = hub_browse.make_folder({"parent": str(base), "name": "  My Games 2  "})
    check("spaces around a name are trimmed, spaces inside stay", spaced["ok"] and (base / "My Games 2").is_dir())
    check("only one level is made (no parents are invented)", not (base / "a").exists())


def test_admin_ops():
    base = tree()
    check("the picker operations are in OPS", "browse" in hub_admin.OPS and "mkdir" in hub_admin.OPS and hub_admin.PICKER_OPS == ("browse", "mkdir"))
    check("the older operations are unchanged", set(("card", "list", "settings", "history", "file", "thumb")) <= set(hub_admin.READ_OPS)
          and set(("history_remove", "history_clear", "redownload", "reorder")) <= set(hub_admin.WRITE_OPS)
          and not set(hub_admin.PICKER_OPS) & (set(hub_admin.READ_OPS) | set(hub_admin.WRITE_OPS)))
    a = hub_admin.admin("browse", {"path": str(base), "kind": "folder"})
    check("hub_admin.admin('browse') lists a folder", a["ok"] and "banana" in names(a))
    b = hub_admin.admin("mkdir", {"parent": str(base), "name": "fresh"})
    check("hub_admin.admin('mkdir') makes a folder", b["ok"] and (base / "fresh").is_dir())
    check("browse with no request still answers", hub_admin.admin("browse")["ok"] is True)
    for var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID"):
        os.environ[var] = "1"
        r = hub_admin.admin("browse", {"path": str(base)})
        m = hub_admin.admin("mkdir", {"parent": str(base), "name": "never-" + var.lower()})
        os.environ.pop(var, None)
        check("%s: browse is refused for an unattended process" % var, r["ok"] is False and r.get("code") == "unattended")
        check("%s: mkdir is refused for an unattended process, and nothing is made" % var, m["ok"] is False and m.get("code") == "unattended" and not (base / ("never-" + var.lower())).exists())
    os.environ["JARVIS_CONTEXT"] = "scheduled"
    check("a scheduled context is refused too", hub_admin.admin("browse", {})["ok"] is False)
    os.environ.pop("JARVIS_CONTEXT", None)
    check("after those, an attended process works again", hub_admin.admin("browse", {})["ok"] is True)
    check("the usage line lists the new operations", "browse" in hub_admin.admin("nope")["error"] and "mkdir" in hub_admin.admin("nope")["error"])

    out = io.StringIO()
    hub_admin.run_cli(["browse"], stdin=io.StringIO(json.dumps({"path": str(base), "kind": "file", "extensions": [".exe"]})), stdout=out)
    answer = json.loads(out.getvalue())
    check("`jarvis hub-admin browse`: one JSON object in, one out", answer["ok"] and names(answer, "file") == ["setup.EXE", "tool.exe"])
    out = io.StringIO()
    hub_admin.run_cli(["mkdir"], stdin=io.StringIO("[1]"), stdout=out)
    check("`jarvis hub-admin mkdir`: a body that isn't an object is refused", json.loads(out.getvalue())["ok"] is False)


def test_plumbing():
    mod = (CLI / "jarvis" / "hub_browse.py").read_text(encoding="utf-8")
    check("hub_browse.py never reads a file's contents", "open(" not in mod.replace("os.scandir", "") and ".read_text" not in mod and ".read_bytes" not in mod)
    check("hub_browse.py has no network or process code", "socket" not in mod and "subprocess" not in mod and "urllib" not in mod and "requests" not in mod)
    check("hub_browse.py deletes and renames nothing", "os.remove" not in mod and "os.unlink" not in mod and "rmtree" not in mod and "os.rename" not in mod and "shutil" not in mod)
    check("the only write is one os.mkdir (not makedirs)", mod.count("os.mkdir(") == 1 and "makedirs" not in mod)
    server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
    check("server.js has the two picker routes, both POST", 'app.post("/api/hub/browse", requireHubBrowse, requireJarvis' in server
          and 'app.post("/api/hub/browse/new-folder", requireHubBrowse, requireJarvis' in server)
    check("neither picker route is a GET (a path never goes in a URL)", 'app.get("/api/hub/browse' not in server)
    check("requireHubBrowse goes through requireHubWrite (loopback Host and the X-Jarvis-Hub header)", "function requireHubBrowse(req, res, next) { return requireHubWrite(req, res, next); }" in server)
    check("the picker routes call hub-admin ops 'browse' and 'mkdir'", 'hubAdmin(res, "browse"' in server and 'hubAdmin(res, "mkdir"' in server)
    page = (ROOT / "web" / "public" / "index.html").read_text(encoding="utf-8")
    check("index.html loads the picker once each (script and stylesheet)", page.count('src="path-picker.js"') == 1 and page.count('href="path-picker.css"') == 1)
    check("the picker files exist", (ROOT / "web" / "public" / "path-picker.js").is_file() and (ROOT / "web" / "public" / "path-picker.css").is_file())
    tool_html = (CLI / "jarvis" / "actions" / "hub_ui" / "tool.html").read_text(encoding="utf-8")
    tool_js = (CLI / "jarvis" / "actions" / "hub_ui" / "tool.js").read_text(encoding="utf-8")
    check("the add form has a Browse button beside Save in folder", 'id="addDestBrowse"' in tool_html and 'id="addDest"' in tool_html)
    check("tool.js opens the picker for the folder field and has a fallback when it is missing", 'picker.open({ kind: "folder"' in tool_js and "isn't available" in tool_js)
    check("tool.js still adds no engine-specific argument to dl_add", "args.dest = dest" in tool_js)


if __name__ == "__main__":
    for fn in (test_pure, test_roots, test_listing, test_cap, test_missing_and_odd_paths, test_links, test_unreadable,
               test_new_folder, test_admin_ops, test_plumbing):
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
