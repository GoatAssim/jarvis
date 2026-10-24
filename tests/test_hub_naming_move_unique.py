"""Download Hub, brief 7B: `naming.move_unique`, the shared "give a finished file its final name without ever
replacing a file" helper (added to hub/naming.py as a NEW function; `unique_path` and everything else there is
unchanged, and this file pins that too).

Pinned: the plain move, a taken name, `.part` reservations, `own_part` (the browser intake's case), the
interrupted-finish case (the two names are already one file), many threads claiming one name together, the
fallbacks for a file system without hard links and for another disk, a failed move that leaves no empty claim
behind, and an error that is not "can't link" being raised with the source untouched.

Run: python tests/test_hub_naming_move_unique.py        (Linux; not run on Windows)
"""
import errno
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub import naming  # noqa: E402

passed = failed = 0
TMP = Path(tempfile.mkdtemp(prefix="jarvis-naming-move-"))
_n = [0]


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def folder():
    _n[0] += 1
    path = TMP / ("d%d" % _n[0])
    path.mkdir()
    return path


def make(path, data):
    Path(path).write_bytes(data)
    return Path(path)


class Patched:
    """Replace attributes for a block (os.link and friends), always put them back."""

    def __init__(self, obj, **repl):
        self.obj, self.repl, self.old = obj, repl, {}

    def __enter__(self):
        for k, v in self.repl.items():
            self.old[k] = getattr(self.obj, k)
            setattr(self.obj, k, v)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.obj, k, v)


def raiser(code):
    def fail(*a, **k):
        raise OSError(code, os.strerror(code))
    return fail


def test_basics():
    d = folder()
    src = make(d / "src.tmp", b"ONE")
    out = naming.move_unique(src, d / "a.mp4")
    check("a free name: the file is there under it and the source is gone",
          out == d / "a.mp4" and out.read_bytes() == b"ONE" and not src.exists())

    src = make(d / "src2.tmp", b"TWO")
    out = naming.move_unique(src, d / "a.mp4")
    check("a taken name: 'name (1).ext', the old file untouched",
          out == d / "a (1).mp4" and out.read_bytes() == b"TWO" and (d / "a.mp4").read_bytes() == b"ONE")

    src = make(d / "src3.tmp", b"THREE")
    out = naming.move_unique(src, d / "a.mp4")
    check("then 'name (2).ext'", out == d / "a (2).mp4" and out.read_bytes() == b"THREE")

    d = folder()
    make(d / "b.mp4.part", b"someone else's download")
    src = make(d / "s.tmp", b"X")
    out = naming.move_unique(src, d / "b.mp4")
    check("another download's .part makes the name taken (as unique_path does)",
          out == d / "b (1).mp4" and (d / "b.mp4.part").read_bytes() == b"someone else's download")

    d = folder()
    part = make(d / "c.mp4.part", b"MINE")
    out = naming.move_unique(part, d / "c.mp4", own_part=True)
    check("own_part: the source IS the .part of the wanted name, so the name is offered",
          out == d / "c.mp4" and out.read_bytes() == b"MINE" and not part.exists())

    d = folder()
    make(d / "c.mp4", b"TAKEN")
    part = make(d / "c.mp4.part", b"MINE")
    make(d / "c (1).mp4.part", b"a neighbour's reservation")
    out = naming.move_unique(part, d / "c.mp4", own_part=True)
    check("own_part with the name taken: skips a name another download reserved, uses the next free one",
          out == d / "c (2).mp4" and out.read_bytes() == b"MINE" and (d / "c.mp4").read_bytes() == b"TAKEN"
          and (d / "c (1).mp4.part").exists())

    d = folder()
    check("unique_path is unchanged: free name, taken name, .part counted, ignore_part",
          naming.unique_path(d / "u.txt") == d / "u.txt"
          and (make(d / "u.txt", b"1") and naming.unique_path(d / "u.txt") == d / "u (1).txt")
          and (make(d / "u (1).txt.part", b"") and naming.unique_path(d / "u.txt") == d / "u (2).txt")
          and naming.unique_path(d / "w.txt") == d / "w.txt"
          and (make(d / "w.txt.part", b"") and naming.unique_path(d / "w.txt") == d / "w (1).txt"
               and naming.unique_path(d / "w.txt", ignore_part=True) == d / "w.txt"))


def test_interrupted_finish():
    d = folder()
    src = make(d / "x.mp4.part", b"WHOLE")
    os.link(src, d / "x.mp4")                       # the link happened, the removal did not
    out = naming.move_unique(src, d / "x.mp4", own_part=True)
    check("an interrupted finish (the two names are one file) is completed, not repeated as '(1)'",
          out == d / "x.mp4" and not src.exists() and not (d / "x (1).mp4").exists())

    d = folder()
    src = make(d / "y.tmp", b"WHOLE")
    os.link(src, d / "y (1).mp4")
    make(d / "y.mp4", b"OTHER")
    out = naming.move_unique(src, d / "y.mp4")
    check("...also when the linked name was a '(1)' name", out == d / "y (1).mp4" and not src.exists()
          and (d / "y.mp4").read_bytes() == b"OTHER" and not (d / "y (2).mp4").exists())


def test_threads():
    lost = 0
    for _ in range(50):
        d = folder()
        n = 6
        sources = [make(d / ("s%d.tmp" % i), ("data%d" % i).encode()) for i in range(n)]
        barrier = threading.Barrier(n)
        got, errors = [], []

        def one(src):
            try:
                barrier.wait(10)
                got.append(naming.move_unique(src, d / "same.mp4"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=one, args=(s,)) for s in sources]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        data = sorted(p.read_bytes() for p in d.iterdir() if p.suffix == ".mp4")
        if errors or data != sorted(("data%d" % i).encode() for i in range(n)) or len(set(got)) != n:
            lost += 1
        if [p for p in d.iterdir() if p.suffix == ".tmp"]:
            lost += 1
    check("six threads claiming one name together, 50 times: six files, six names, nothing lost, no source left",
          lost == 0)


def test_fallbacks():
    # no hard links on this file system
    for code in (errno.EPERM, errno.ENOTSUP, errno.ENOSYS):
        d = folder()
        make(d / "a.mp4", b"OLD")
        src = make(d / "s.tmp", b"NEW")
        with Patched(os, link=raiser(code)):
            out = naming.move_unique(src, d / "a.mp4")
        check("no hard links (%s): moved by an exclusive create, the old file untouched, no source left"
              % errno.errorcode[code],
              out == d / "a (1).mp4" and out.read_bytes() == b"NEW" and (d / "a.mp4").read_bytes() == b"OLD"
              and not src.exists())

    # no hard links AND a rival takes the name between the check and the claim: the claim itself is exclusive
    d = folder()
    src = make(d / "s.tmp", b"NEW")
    real_open = os.open
    state = {"done": False}

    def rival_open(path, flags, *a, **k):
        if not state["done"] and str(path).endswith("r.mp4") and flags & os.O_EXCL:
            state["done"] = True
            (d / "r.mp4").write_bytes(b"RIVAL")
        return real_open(path, flags, *a, **k)

    with Patched(os, link=raiser(errno.EPERM), open=rival_open):
        out = naming.move_unique(src, d / "r.mp4")
    check("no hard links and a rival takes the name first: the exclusive create refuses, the rival is untouched",
          (d / "r.mp4").read_bytes() == b"RIVAL" and out == d / "r (1).mp4" and out.read_bytes() == b"NEW")

    # another disk: link says EXDEV, the rename says EXDEV too, shutil.move copies
    d = folder()
    src = make(d / "s.tmp", b"FAR")
    moved = []
    real_move = shutil.move

    def fake_move(a, b, *x, **k):
        moved.append((str(a), str(b)))
        return real_move(a, b, *x, **k)

    with Patched(os, link=raiser(errno.EXDEV), replace=raiser(errno.EXDEV)), Patched(shutil, move=fake_move):
        out = naming.move_unique(src, d / "far.mp4")
    check("another disk (EXDEV): copied over the claim with shutil.move, source gone",
          out == d / "far.mp4" and out.read_bytes() == b"FAR" and not src.exists() and len(moved) == 1)

    # a failed move leaves no empty claim behind and keeps the source
    d = folder()
    src = make(d / "s.tmp", b"KEEP")

    def boom(*a, **k):
        raise OSError(errno.ENOSPC, "no space left")

    with Patched(os, link=raiser(errno.EPERM), replace=raiser(errno.EXDEV)), Patched(shutil, move=boom):
        try:
            naming.move_unique(src, d / "f.mp4")
            raised = False
        except OSError as exc:
            raised = exc.errno == errno.ENOSPC
    check("a move that fails raises, keeps the source and removes its own empty claim",
          raised and src.read_bytes() == b"KEEP" and not (d / "f.mp4").exists())

    # an error that is not "can't link" is raised, nothing is created
    d = folder()
    src = make(d / "s.tmp", b"KEEP")
    with Patched(os, link=raiser(errno.EIO)):
        try:
            naming.move_unique(src, d / "e.mp4")
            raised = False
        except OSError as exc:
            raised = exc.errno == errno.EIO
    check("a real disk error is raised (not hidden by a fallback); the source is untouched, no file created",
          raised and src.read_bytes() == b"KEEP" and not (d / "e.mp4").exists())

    try:
        naming.move_unique(d / "does-not-exist.tmp", d / "m.mp4")
        raised = False
    except FileNotFoundError:
        raised = True
    check("a missing source raises FileNotFoundError and creates nothing", raised and not (d / "m.mp4").exists())


def main():
    test_basics()
    test_interrupted_finish()
    test_threads()
    test_fallbacks()
    shutil.rmtree(TMP, ignore_errors=True)
    print()
    print("%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
