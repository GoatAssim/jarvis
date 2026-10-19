"""Download Hub, phase 9: the pure helpers of tests/store_checks.py (the Epic / GOG probe).

Covers: JSON flattening with secret masking, snapshot diffs, user-name redaction, folder listings,
and the SQLite inventory (columns and counts for every table, rows only for small download-looking
tables, secret-looking columns masked, the launcher's own file left untouched).

Run: python tests/test_hub_store_checks.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store_checks as sc  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


tmp = Path(tempfile.mkdtemp(prefix="store-checks-test-"))
try:
    flat = sc.flat({"AppName": "Fortnite", "InstallSize": 12, "auth_token": "abc", "Files": [{"a": 1}, {"a": 2}],
                    "nested": {"password": "pw", "ok": True}})
    check("flat gives dotted keys", flat["AppName"] == "Fortnite" and flat["nested.ok"] == "True")
    check("secret-looking keys are masked", flat["auth_token"] == "<masked>" and flat["nested.password"] == "<masked>")
    check("lists are summarised and their first items shown", flat["Files[]"] == "list of 2" and flat["Files[0].a"] == "1")

    old, new = {"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "9", "d": "4"}
    check("diff shows changed, new and gone keys",
          sc.diff_maps(old, new) == ["~ b: 2 -> 9", "- c", "+ d = 4"])
    check("redact replaces the user name", sc.redact(r"C:\Users\Alice\x", "alice") == r"C:\Users\<user>\x")

    (tmp / "dir" / "sub").mkdir(parents=True)
    (tmp / "dir" / "a.item").write_text("x")
    (tmp / "dir" / "sub" / "b.bin").write_text("yy")
    listing = sc.list_dir(tmp / "dir", "t")
    check("list_dir lists files and sub-folders with sizes",
          listing["t:a.item"].startswith("size=1 ") and listing["t:sub/"] == "dir" and "size=2" in listing["t:sub/b.bin"])
    check("a missing folder lists nothing", sc.list_dir(tmp / "nope", "t") == {})

    db = tmp / "galaxy.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE InstalledProducts (productId INTEGER, installPath TEXT, authToken TEXT)")
    conn.execute("INSERT INTO InstalledProducts VALUES (1, 'D:/Games/X', 'SECRETVALUE')")
    conn.execute("CREATE TABLE Misc (a TEXT)")
    conn.execute("INSERT INTO Misc VALUES ('hidden')")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    inv = sc.db_inventory(db, "g")
    check("every table gets its columns and row count",
          inv["g:Misc:rows"] == "1" and "productId" in inv["g:InstalledProducts:columns"])
    check("rows of a download-looking small table are shown, secret columns masked",
          "installPath=D:/Games/X" in inv["g:InstalledProducts:row00"] and "SECRETVALUE" not in inv["g:InstalledProducts:row00"])
    check("rows of other tables are not dumped", "g:Misc:row00" not in inv)
    check("the original database file is untouched", db.read_bytes() == before)
    check("a missing database is just empty", sc.db_inventory(tmp / "none.db", "g") == {})

    logs = tmp / "logs"
    logs.mkdir()
    (logs / "a.log").write_text("boring line\nDownloading chunk 3 of 9\n")
    tail = sc.LogTail()
    first = tail.look(logs, "l", first=True)
    check("the first look shows recent download lines", any("Downloading chunk 3" in v for v in first.values()))
    with open(logs / "a.log", "a") as handle:
        handle.write("other\nInstall progress 40%\n")
    again = tail.look(logs, "l")
    check("later looks show only new download lines",
          any("progress 40%" in v for v in again.values()) and not any("chunk 3" in v for v in again.values()))

    roots = sc.epic_roots({"PROGRAMDATA": str(tmp / "pd"), "LOCALAPPDATA": str(tmp / "la")})
    snap = sc.snapshot_epic(roots, sc.LogTail())
    check("a launcher that isn't installed is reported as missing, not as an error",
          any(k.startswith("epic:missing:") for k in snap))
    manifests = tmp / "pd" / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    manifests.mkdir(parents=True)
    (manifests / "X.item").write_text('{"AppName": "X", "InstallLocation": "%s", "token": "t"}'
                                      % str(tmp / "game").replace("\\", "/"))
    (tmp / "game" / ".egstore").mkdir(parents=True)
    (tmp / "game" / ".egstore" / "a.manifest").write_text("m")
    snap = sc.snapshot_epic(roots, sc.LogTail())
    check("Epic .item files are flattened with secrets masked",
          snap["epic:item:X.item:AppName"] == "X" and snap["epic:item:X.item:token"] == "<masked>")
    check("the .egstore folder of each install location is listed",
          any(k.startswith("epic:egstore:X.item:a.manifest") for k in snap))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
