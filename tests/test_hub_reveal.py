"""Download Hub, N2: "Show in folder" opened Documents.

Covers (no network, nothing real is started):
* hub/reveal.py: `explorer_command` builds ONE string with the path quoted inside the switch, asserted
  exactly for paths with spaces, parentheses, `&`, `,`, `^`, `%`, non-ASCII characters, forward slashes, a
  UNC path, another drive; and refuses what it cannot quote safely; `plan_open` for a file row and a folder row.
* service.open_item with `LAUNCH` swapped: a file is revealed (selected); a folder row is opened itself and
  never its parent; `folder` on a file row opens the parent; a missing item, a failed launch and a launch
  that did less than asked are reported, not answered with ok: true.
* service._default_launch on a faked Windows / macOS / Linux: the Windows command is a STRING, a failed
  select falls back to the folder and says so; macOS and Linux are unchanged.
* the `dest_is_dir` marker: set from a provider report, unknown until the folder exists; and an old
  database (made before the column) is upgraded in place with its rows kept.

Run: python tests/test_hub_reveal.py
"""
import ntpath
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-reveal-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub import reveal, service  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
_n = [0]
PUBLIC = "http://93.184.216.34"


def check(name, cond, detail=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}" + (f"   -> {detail!r}" if detail is not None else ""))


def fresh_store():
    _n[0] += 1
    return Store(Path(HOME) / f"reveal-{_n[0]}.db", clock=time.time)


# ---------------------------------------------------------------- the pure command builder

def test_explorer_command():
    cases = [
        ("spaces",
         r"C:\Users\Me\Videos\My Holiday Film.mp4",
         'explorer /select,"C:\\Users\\Me\\Videos\\My Holiday Film.mp4"'),
        ("parentheses (Steam's own folder)",
         r"C:\Program Files (x86)\Steam\steamapps\common\Portal 2",
         'explorer /select,"C:\\Program Files (x86)\\Steam\\steamapps\\common\\Portal 2"'),
        ("an ampersand",
         r"D:\Downloads\Tom & Jerry (1940).mkv",
         'explorer /select,"D:\\Downloads\\Tom & Jerry (1940).mkv"'),
        ("a comma",
         r"D:\Downloads\Smith, John - Live.mp3",
         'explorer /select,"D:\\Downloads\\Smith, John - Live.mp3"'),
        ("a caret and percent signs",
         r"D:\Downloads\100% ^real^ %PATH%.txt",
         'explorer /select,"D:\\Downloads\\100% ^real^ %PATH%.txt"'),
        ("non-ASCII characters",
         "C:\\Users\\Zo\u00eb\\\u041f\u0440\u0438\u0432\u0435\u0442 \u4e16\u754c.mp4",
         'explorer /select,"C:\\Users\\Zo\u00eb\\\u041f\u0440\u0438\u0432\u0435\u0442 \u4e16\u754c.mp4"'),
        ("forward slashes become backslashes",
         "D:/Games/Some Game/setup.bin",
         'explorer /select,"D:\\Games\\Some Game\\setup.bin"'),
        ("mixed slashes and doubled separators",
         "D:/Games\\\\Some Game//setup.bin",
         'explorer /select,"D:\\Games\\Some Game\\setup.bin"'),
        ("a lower-case drive spelled with slashes (Steam's registry spelling)",
         "c:/program files (x86)/steam/steamapps/common/Portal 2",
         'explorer /select,"c:\\program files (x86)\\steam\\steamapps\\common\\Portal 2"'),
        ("a UNC path",
         r"\\nas\media\Films\A Film (2020).mkv",
         'explorer /select,"\\\\nas\\media\\Films\\A Film (2020).mkv"'),
        ("a UNC path written with forward slashes",
         "//nas/media/Films/a film.mkv",
         'explorer /select,"\\\\nas\\media\\Films\\a film.mkv"'),
        ("another drive",
         r"E:\Stuff\x.bin",
         'explorer /select,"E:\\Stuff\\x.bin"'),
        ("dot segments are resolved",
         r"D:\Downloads\Files\..\Files\.\x y.bin",
         'explorer /select,"D:\\Downloads\\Files\\x y.bin"'),
        ("a folder row's path (a trailing slash is dropped)",
         "D:/Games/Steam Library/steamapps/common/Portal 2/",
         'explorer /select,"D:\\Games\\Steam Library\\steamapps\\common\\Portal 2"'),
    ]
    for name, path, want in cases:
        got = reveal.explorer_command(path)
        check("explorer_command: " + name, got == want, got)
    check("it is one string, never a list", isinstance(reveal.explorer_command(r"C:\a b\c.txt"), str))
    check("the switch is not wrapped in quotes (the old bug)", '"/select,' not in reveal.explorer_command(r"C:\a b\c.txt"))

    for name, bad in (("an empty path", ""), ("only spaces", "   "), ("None", None),
                      ("a quote in the path", 'C:\\a"b\\c.txt'), ("a newline in the path", "C:\\a\nb.txt"),
                      ("a NUL in the path", "C:\\a\x00b.txt"), ("a drive root", "C:\\"), ("a drive root, slashes", "D:/"),
                      ("a UNC share root", r"\\nas\media")):
        try:
            reveal.explorer_command(bad)
            check("explorer_command refuses " + name, False)
        except ValueError:
            check("explorer_command refuses " + name, True)

    check("windows_folder: the parent, as a Windows path",
          reveal.windows_folder("D:/Games/a b/c (1).bin") == "D:\\Games\\a b"
          and reveal.windows_folder(r"\\nas\media\f\x.mkv") == "\\\\nas\\media\\f")
    check("windows_path is stable on its own output",
          reveal.windows_path(reveal.windows_path("D:/a b/c")) == "D:\\a b\\c")


# ---------------------------------------------------------------- what to open

def test_plan_open():
    f, d = r"D:\Lib\Files\a b.mp4", r"D:\Lib\Steam\common\Portal 2"
    plan = lambda p, a, is_dir: reveal.plan_open(p, a, is_dir, pathmod=ntpath)  # noqa: E731
    check("file row, open", plan(f, "open", False) == (f, "open"))
    check("file row, reveal: the file, selected", plan(f, "reveal", False) == (f, "reveal"))
    check("file row, folder: the file's folder", plan(f, "folder", False) == (r"D:\Lib\Files", "open"))
    check("folder row, open: the folder", plan(d, "open", True) == (d, "open"))
    check("folder row, reveal: the folder itself, opened, not selected inside its parent",
          plan(d, "reveal", True) == (d, "open"))
    check("folder row, folder: the folder itself, not its parent", plan(d, "folder", True) == (d, "open"))
    try:
        plan(f, "delete", False)
        check("plan_open refuses an unknown action", False)
    except ValueError:
        check("plan_open refuses an unknown action", True)
    check("on this system's paths too", reveal.plan_open("/a/b/c.txt", "folder", False) == ("/a/b", "open"))


# ---------------------------------------------------------------- open_item, with LAUNCH swapped

def test_open_item():
    store = fresh_store()
    lib = Path(HOME) / "Lib Root (x86)"
    store.set_settings({"library_root": str(lib)})
    launched = []
    original = service.LAUNCH
    service.LAUNCH = lambda path, action: launched.append((str(path), action)) or True
    try:
        (lib / "Files").mkdir(parents=True)
        good = lib / "Files" / "my file & more.bin"
        good.write_bytes(b"x")
        a = store.add_item(engine="http", source_url=PUBLIC + "/ok.bin", title="ok.bin")[0]
        store.transition(a["id"], "probing")
        store.transition(a["id"], "done", dest_file=str(good))

        res = service.open_item(a["id"], "reveal", store=store)
        check("a file is revealed: its own path, action reveal", res["ok"] and launched[-1] == (str(good), "reveal"), launched)
        check("and the answer carries that path, with no note", res["path"] == str(good) and "note" not in res)
        res = service.open_item(a["id"], "folder", store=store)
        check("folder on a file row opens its folder", res["ok"] and launched[-1] == (str(good.parent), "open"), launched)
        res = service.open_item(a["id"], "open", store=store)
        check("open still opens the file", res["ok"] and launched[-1] == (str(good), "open"))

        # a folder row: a provider whose dest is a folder (Steam's is)
        steam_root = Path(HOME) / "Steam Lib (x86)"
        game = steam_root / "steamapps" / "common" / "Portal 2"
        game.mkdir(parents=True)
        store.register_provider("steam", label="Steam", capabilities=["open", "reveal"], open_roots=[str(steam_root)])
        store.upsert_provider_row("steam", "620", {"state": "done", "dest": str(game)})
        row = store.get_item("steam:620")
        check("the report marked the row's dest as a folder", row["dest_is_dir"] == 1, row.get("dest_is_dir"))
        res = service.open_item("steam:620", "reveal", store=store)
        check("reveal on a folder row opens THAT folder (not 'common' with the game selected)",
              res["ok"] and launched[-1] == (str(game), "open") and res["path"] == str(game), launched)
        res = service.open_item("steam:620", "folder", store=store)
        check("folder on a folder row opens the folder itself, not its parent",
              res["ok"] and launched[-1] == (str(game), "open"), launched)
        check("neither of those asked the launcher to select anything",
              all(act == "open" for path, act in launched[-2:]))

        # a file row of a provider keeps being selected
        (steam_root / "readme.txt").write_text("x")
        store.upsert_provider_row("steam", "f1", {"state": "done", "dest": str(steam_root / "readme.txt")})
        check("a provider's FILE row is marked 0", store.get_item("steam:f1")["dest_is_dir"] == 0)
        res = service.open_item("steam:f1", "reveal", store=store)
        check("and is revealed as a file", res["ok"] and launched[-1] == (str(steam_root / "readme.txt"), "reveal"))

        # a folder that is not there yet (a Steam install that is only starting)
        later = steam_root / "steamapps" / "common" / "Later Game"
        store.upsert_provider_row("steam", "999", {"state": "queued", "dest": str(later)})
        check("a folder that doesn't exist yet: marker unknown", store.get_item("steam:999")["dest_is_dir"] is None)
        before = len(launched)
        res = service.open_item("steam:999", "reveal", store=store)
        check("... and the answer says so instead of ok: true",
              res["ok"] is False and "isn't there yet" in res["error"] and len(launched) == before, res)
        store.update_item("steam:999", {"dest_is_dir": 1})
        res = service.open_item("steam:999", "reveal", store=store)
        check("a row known to be a folder says 'folder', not 'file'", res["ok"] is False and "folder isn't there yet" in res["error"], res)
        later.mkdir()
        store.upsert_provider_row("steam", "999", {"state": "downloading", "dest": str(later)})
        check("the next report, once the folder exists, marks it", store.get_item("steam:999")["dest_is_dir"] == 1)
        res = service.open_item("steam:999", "reveal", store=store)
        check("and then it opens", res["ok"] and launched[-1] == (str(later), "open"))

        # a hub row whose dest is a folder but carries no marker (qBittorrent's content_path can be one)
        torrent = lib / "Files" / "Some Torrent"
        torrent.mkdir()
        t = store.add_item(engine="http", source_url=PUBLIC + "/t", title="t")[0]
        store.update_item(t["id"], {"dest_file": str(torrent)})
        check("(a hub row set without the marker has none)", store.get_item(t["id"])["dest_is_dir"] is None)
        res = service.open_item(t["id"], "folder", store=store)
        check("the disk decides when there is no marker: a folder is not taken to its parent",
              res["ok"] and launched[-1] == (str(torrent), "open"), launched)

        # honesty
        gone = store.add_item(engine="http", source_url=PUBLIC + "/g", title="g")[0]
        store.update_item(gone["id"], {"dest_file": str(lib / "Files" / "gone.bin")})
        before = len(launched)
        res = service.open_item(gone["id"], "reveal", store=store)
        check("a file that has gone is reported and nothing is launched",
              res["ok"] is False and "no longer there" in res["error"] and len(launched) == before, res)

        service.LAUNCH = lambda path, action: False
        res = service.open_item(a["id"], "reveal", store=store)
        check("a failed launch is reported", res["ok"] is False and "couldn't open" in res["error"], res)
        service.LAUNCH = lambda path, action: "couldn't select the file; its folder was opened instead"
        res = service.open_item(a["id"], "reveal", store=store)
        check("a launch that did less than asked: ok, but it says so and says nothing was selected",
              res["ok"] is True and "folder was opened instead" in res.get("note", "") and res.get("selected") is False, res)
        res = service.open_item(a["id"], "folder", store=store)
        check("(a note on a plain folder open adds no 'selected' flag)", res["ok"] and "selected" not in res and "note" in res)

        def boom(path, action):
            raise RuntimeError("launcher exploded")
        service.LAUNCH = boom
        res = service.open_item(a["id"], "reveal", store=store)
        check("a launcher that raises is an error answer, not a crash", res["ok"] is False and "exploded" in res["error"], res)
    finally:
        service.LAUNCH = original


# ---------------------------------------------------------------- the real launcher, on faked systems

class _Fake:
    """Swap sys.platform, subprocess.Popen and os.startfile for the length of one call."""

    def __init__(self, platform, popen_fails=False, startfile_fails=False):
        self.platform, self.popen_fails, self.startfile_fails = platform, popen_fails, startfile_fails
        self.popen, self.startfile = [], []

    def __enter__(self):
        self._old = (sys.platform, service.subprocess.Popen, getattr(os, "startfile", None))
        outer = self

        def popen(args, *a, **kw):
            outer.popen.append((args, kw))
            if outer.popen_fails:
                raise OSError("explorer is not available")
            return object()

        def startfile(path):
            outer.startfile.append(path)
            if outer.startfile_fails:
                raise OSError("no handler")

        sys.platform = self.platform
        service.subprocess.Popen = popen
        os.startfile = startfile
        return self

    def __exit__(self, *exc):
        sys.platform, service.subprocess.Popen = self._old[0], self._old[1]
        if self._old[2] is None:
            del os.startfile
        else:
            os.startfile = self._old[2]


def test_default_launch():
    p = r"D:\My Games (x86)\Tom & Jerry, 1940\a b.bin"
    with _Fake("win32") as fake:
        res = service._default_launch(p, "reveal")
    check("Windows reveal: True", res is True, res)
    args, kw = fake.popen[0]
    check("Windows reveal: the command is ONE string, path quoted inside the switch",
          args == 'explorer /select,"D:\\My Games (x86)\\Tom & Jerry, 1940\\a b.bin"', args)
    check("Windows reveal: no shell is involved", not kw.get("shell"), kw)
    check("Windows reveal: a normal select does not also open the folder", fake.startfile == [])

    with _Fake("win32") as fake:
        service._default_launch("D:/Games/a b/setup.bin", "reveal")
    check("Windows reveal with forward slashes is cleaned up", fake.popen[0][0] == 'explorer /select,"D:\\Games\\a b\\setup.bin"', fake.popen)

    with _Fake("win32", popen_fails=True) as fake:
        res = service._default_launch(p, "reveal")
    check("Windows: when Explorer cannot be started, the folder is opened and the answer says so",
          isinstance(res, str) and "folder was opened instead" in res and fake.startfile == [ntpath.dirname(p)], (res, fake.startfile))

    with _Fake("win32", popen_fails=True, startfile_fails=True) as fake:
        res = service._default_launch(p, "reveal")
    check("Windows: when the fallback fails too, it is False (never a silent True)", res is False, res)

    with _Fake("win32") as fake:
        res = service._default_launch(r"D:\a b\c.txt", "reveal-no-such")
    check("Windows: any action but reveal still goes to startfile",
          res is True and fake.startfile == [r"D:\a b\c.txt"] and fake.popen == [], (res, fake.startfile, fake.popen))
    with _Fake("win32") as fake:
        service._default_launch(r"D:\Steam Lib\common\Portal 2", "open")
    check("Windows: open on a folder is startfile on the folder", fake.startfile == [r"D:\Steam Lib\common\Portal 2"] and not fake.popen)

    with _Fake("darwin") as fake:
        service._default_launch("/Users/me/a b.txt", "reveal")
        service._default_launch("/Users/me/a b.txt", "open")
    check("macOS unchanged", [a for a, k in fake.popen] == [["open", "-R", "/Users/me/a b.txt"], ["open", "/Users/me/a b.txt"]], fake.popen)
    with _Fake("linux") as fake:
        service._default_launch("/home/me/a b.txt", "reveal")
        service._default_launch("/home/me/a b.txt", "open")
    check("Linux unchanged", [a for a, k in fake.popen] == [["xdg-open", "/home/me"], ["xdg-open", "/home/me/a b.txt"]], fake.popen)


# ---------------------------------------------------------------- the marker's migration

def test_migration():
    old = Path(HOME) / "old-schema.db"
    conn = sqlite3.connect(str(old))
    conn.executescript("""
        CREATE TABLE items(
            id TEXT PRIMARY KEY, group_id TEXT, provider TEXT NOT NULL DEFAULT 'hub', owner TEXT NOT NULL DEFAULT 'hub',
            kind TEXT NOT NULL DEFAULT 'file', engine TEXT NOT NULL DEFAULT '', source_url TEXT, source_ref_json TEXT,
            title TEXT NOT NULL DEFAULT '', subtitle TEXT NOT NULL DEFAULT '', options_json TEXT,
            state TEXT NOT NULL, phase TEXT, phase_index INTEGER, phase_count INTEGER,
            percent REAL, bytes_done INTEGER, bytes_total INTEGER, speed_bps REAL, eta_s REAL,
            peers INTEGER, seeds INTEGER, derived TEXT NOT NULL DEFAULT '',
            dest_dir TEXT, dest_file TEXT, thumb_ref TEXT, detail_json TEXT, children_json TEXT,
            links_json TEXT, capabilities_json TEXT, controls_json TEXT,
            message TEXT, error TEXT, control TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL, dedupe_key TEXT,
            created_at REAL NOT NULL, started_at REAL, finished_at REAL, updated_at REAL NOT NULL,
            last_report_at REAL, stale INTEGER NOT NULL DEFAULT 0, keep INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO items(id, state, title, dest_file, created_at, updated_at)
            VALUES('h_old', 'done', 'kept', 'D:/x/kept.bin', 1.0, 1.0);
        PRAGMA user_version=2;
    """)
    conn.commit()
    conn.close()
    store = Store(old)
    row = store.get_item("h_old")
    check("an old database opens and keeps its rows", row is not None and row["title"] == "kept" and row["dest_file"] == "D:/x/kept.bin")
    check("its rows have the new marker, unknown", "dest_is_dir" in row and row["dest_is_dir"] is None, row)
    store.update_item("h_old", {"dest_is_dir": 1})
    check("the marker can be written and read back", store.get_item("h_old")["dest_is_dir"] == 1)
    cols = [r[1] for r in sqlite3.connect(str(old)).execute("PRAGMA table_info(items)").fetchall()]
    check("exactly one dest_is_dir column", cols.count("dest_is_dir") == 1, cols)
    # a second process (modelled by forgetting what this one checked) must not fail on the existing column
    from jarvis.hub import store as store_mod
    store_mod._COLUMNS_CHECKED.clear()
    check("opening it again is harmless", Store(old).get_item("h_old")["dest_is_dir"] == 1)
    fresh = fresh_store()
    fresh.add_item(engine="http", source_url=PUBLIC + "/n", title="n")
    cols = [r[1] for r in sqlite3.connect(str(fresh.path)).execute("PRAGMA table_info(items)").fetchall()]
    check("a brand-new database has the column too, once", cols.count("dest_is_dir") == 1)


try:
    test_explorer_command()
    test_plan_open()
    test_open_item()
    test_default_launch()
    test_migration()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
