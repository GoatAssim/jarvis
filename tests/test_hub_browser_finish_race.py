"""Download Hub, brief 7B step 4: the end of a browser download must never replace a file.

`hub/browser_intake.py` reserves `<name>.part` with an exclusive create, saves into it, and at the end gave it its
final name with `if target.exists(): target = unique_path(target, ignore_part=True)` followed by `os.replace`.
A file that appears between that check and the replace was replaced, and `ignore_part=True` also ignored the
`.part` another download had reserved for the next name.  Now the final name is claimed first with an exclusive
create (`naming.claim_name`) and the file is moved over our own empty claim (so the retry on a rename that fails
is as before).

Fakes only (no browser): `bi._save_download` is called with a fake Playwright download, as
test_hub_browser_intake.py does.  Run: python tests/test_hub_browser_finish_race.py   (Linux; not run on Windows)
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-browser-race-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import hub_client  # noqa: E402
from jarvis.hub import browser_intake as bi  # noqa: E402
from jarvis.hub import naming  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

ROUNDS = 50
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


_n = [0]


def new_library():
    """A fresh library folder per scenario (the settings are the hub's own, in this test's HOME)."""
    _n[0] += 1
    lib = Path(HOME) / ("library%d" % _n[0])
    Store().set_settings({"library_root": str(lib)})
    with bi._lock:
        bi._saving.clear()
        del bi._notes[:]
    hub_client.set_store_factory(None)
    return lib


class FakeDownload:
    """suggested_filename + save_as(path); `after_write` runs once the bytes are in the .part (the download is
    "whole" at that moment and about to be given its name)."""

    def __init__(self, name, data, after_write=None):
        self.suggested_filename = name
        self.url = "https://files.example.com/get"
        self._data, self._after = data, after_write

    def save_as(self, path):
        Path(path).write_bytes(self._data)
        if self._after:
            self._after()


def files_of(folder):
    return {p.name: p.read_bytes() for p in Path(folder).iterdir() if p.is_file()}


_REAL_REPLACE = os.replace


class ReplaceHook:
    """Run `fn(src, dst)` right before the first `os.replace` of a browser `.part` (the old code's claim)."""

    def __init__(self, fn):
        self.fn, self.done = fn, False

    def __enter__(self):
        def hooked(src, dst, *a, **k):
            if not self.done and str(src).endswith(".part"):
                self.done = True
                self.fn(src, dst)
            return _REAL_REPLACE(src, dst, *a, **k)
        os.replace = hooked
        return self

    def __exit__(self, *exc):
        os.replace = _REAL_REPLACE


def exclusive_create(path, data):
    """What a well-behaved rival does (the HTTP engine's reservation, `move_unique`'s claim): create or fail."""
    try:
        with open(path, "xb") as fh:
            fh.write(data)
        return True
    except FileExistsError:
        return False


# --------------------------------------------------------------------------- 1: a name taken in the narrow window

def test_window():
    lost = 0
    taken_by_rival = refused = 0
    for _ in range(ROUNDS):
        lib = new_library()
        outcome = {}

        def rival(src, dst):
            outcome["created"] = exclusive_create(Path(dst).parent / "report.pdf", b"RIVAL")

        with ReplaceHook(rival):
            bi._save_download(FakeDownload("report.pdf", b"OURS"))
        got = files_of(lib / "Files")
        if outcome.get("created"):
            taken_by_rival += 1
            ok = got.get("report.pdf") == b"RIVAL" and sorted(v for v in got.values() if v == b"OURS") == [b"OURS"]
        else:
            refused += 1
            ok = got.get("report.pdf") == b"OURS" and b"RIVAL" not in got.values()
        if not ok or [n for n in got if n.endswith(".part")]:
            lost += 1
    check("nothing is overwritten when a rival tries to take the name just before the move (%d rounds: "
          "the claim refused the rival %d times, the rival got the name %d times)" % (ROUNDS, refused, taken_by_rival),
          lost == 0)
    check("the claim won the name every time (the rival's exclusive create was refused)", refused == ROUNDS)


# --------------------------------------------------------------------------- 2: a name taken while saving

def test_taken_while_saving():
    lib = new_library()
    folder = lib / "Files"

    def take():
        (folder / "report.pdf").write_bytes(b"RIVAL")           # appears while the file is being saved

    bi._save_download(FakeDownload("report.pdf", b"OURS", after_write=take))
    got = files_of(folder)
    check("a file that appears while the download is being saved is not replaced",
          got.get("report.pdf") == b"RIVAL")
    check("...the download lands under the next free name with its own bytes, and no .part is left",
          got.get("report (1).pdf") == b"OURS" and not [n for n in got if n.endswith(".part")])

    # a neighbour has reserved the NEXT name: it must not be landed on
    lib = new_library()
    folder = lib / "Files"
    state = {}

    def take_and_neighbour():
        (folder / "report.pdf").write_bytes(b"RIVAL")
        state["neighbour"] = folder / "report (1).pdf.part"
        state["neighbour"].write_bytes(b"NEIGHBOUR, half way")

    bi._save_download(FakeDownload("report.pdf", b"OURS", after_write=take_and_neighbour))
    got = files_of(folder)
    check("another download's reservation of the next name is respected (the old ignore_part=True ignored it)",
          got.get("report (2).pdf") == b"OURS" and got.get("report (1).pdf.part") == b"NEIGHBOUR, half way"
          and "report (1).pdf" not in got and got.get("report.pdf") == b"RIVAL")


# --------------------------------------------------------------------------- 3: three finishers, one name

def test_three_finishers():
    lost = 0
    for _ in range(ROUNDS):
        lib = new_library()
        folder = lib / "Files"
        folder.mkdir(parents=True, exist_ok=True)
        barrier = threading.Barrier(3)
        errors = []

        def wait():
            try:
                barrier.wait(10)
            except threading.BrokenBarrierError:
                pass

        def browser(tag):
            try:
                bi._save_download(FakeDownload("same.pdf", tag, after_write=wait))
            except BaseException as exc:  # noqa: BLE001
                errors.append(repr(exc))

        def other_engine():
            try:
                src = folder / "other-engine.tmp"
                src.write_bytes(b"ENGINE")
                wait()
                naming.move_unique(src, folder / "same.pdf")
            except BaseException as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=browser, args=(b"BROWSER-1",)),
                   threading.Thread(target=browser, args=(b"BROWSER-2",)),
                   threading.Thread(target=other_engine)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        got = files_of(folder)
        if errors or sorted(got.values()) != [b"BROWSER-1", b"BROWSER-2", b"ENGINE"] or len(got) != 3:
            lost += 1
    check("two browser downloads and another engine's finish, one name, together, %d times: three files, none lost"
          % ROUNDS, lost == 0)


# --------------------------------------------------------------------------- 4: as before

def test_as_before():
    lib = new_library()
    bi._save_download(FakeDownload("plain.pdf", b"PLAIN"))
    got = files_of(lib / "Files")
    check("the plain case: <library>/Files/<name> with its bytes, no .part", got == {"plain.pdf": b"PLAIN"})

    # a rename that fails twice is retried; the file lands and nothing else is left
    lib = new_library()
    bi.REPLACE_WAIT_S = 0.01
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("held by another process")
        return _REAL_REPLACE(src, dst, *a, **k)

    os.replace = flaky
    try:
        bi._save_download(FakeDownload("held.bin", b"held-bytes"))
    finally:
        os.replace = _REAL_REPLACE
    check("a rename that fails twice is retried and lands (no stray empty file)",
          files_of(lib / "Files") == {"held.bin": b"held-bytes"} and calls["n"] == 3)

    # a rename that never works: the bytes are kept in the .part, and OUR empty claim does not stay behind
    lib = new_library()

    def never(src, dst, *a, **k):
        raise PermissionError("held by another process")

    os.replace = never
    try:
        bi._save_download(FakeDownload("stuck.bin", b"stuck-bytes"))
    finally:
        os.replace = _REAL_REPLACE
    got = files_of(lib / "Files")
    check("a rename that never works keeps the bytes in the .part and leaves no empty 'stuck.bin' behind",
          got == {"stuck.bin.part": b"stuck-bytes"})


def main():
    test_window()
    test_taken_while_saving()
    test_three_finishers()
    test_as_before()
    print()
    print("%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
