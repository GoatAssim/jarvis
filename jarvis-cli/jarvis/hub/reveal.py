"""Download Hub, N2: "Show in folder" -- the pure parts.

Nothing in this file starts a process or touches the disk, so tests can assert every string
exactly.  ``hub/service.py`` (``open_item`` and ``_default_launch``) uses it.

Two jobs:

* ``explorer_command(path)`` builds the Windows command line that opens Explorer with one item
  selected.  It is ONE string with the path quoted inside the switch::

      explorer /select,"C:\\Program Files (x86)\\Steam\\a b.txt"

  The old code passed a list, so Python quoted the whole switch (``"/select,C:\\a b"``),
  which Explorer does not understand; it then showed Documents.  Hand the string straight to
  ``subprocess.Popen`` (no ``shell=True``): then ``&``, ``(``, ``)``, ``,`` and ``^`` in a path
  are plain characters, because no ``cmd.exe`` ever sees them.

* ``plan_open(path, action, is_dir)`` decides WHAT to open for a row: which path, and whether it
  is "open" (default program / folder) or "reveal" (folder with the item selected).  A row's
  ``dest`` may be a folder (Steam's is), so ``reveal`` on a folder opens that folder itself and
  ``folder`` on a folder row does not take the parent.
"""
import ntpath
import os

OPEN, REVEAL = "open", "reveal"


def windows_path(path):
    """A Windows spelling of ``path``: absolute, backslashes, no ``.`` / ``..`` / doubled slashes.

    A path with no drive (relative, or rooted without a drive) is made absolute with the host's
    ``os.path.abspath`` first; on Windows that adds the drive.  A drive path (``D:/Games/x``) or a
    UNC path (``//server/share/x``) is never sent through ``abspath``, so this stays a pure
    string function for the paths the hub really stores.
    """
    text = str(path)
    if not ntpath.splitdrive(text)[0]:
        text = os.path.abspath(text)
    return ntpath.normpath(text)


def windows_folder(path):
    """The folder that holds ``path``, as a Windows path (the fallback when selecting fails)."""
    return ntpath.dirname(windows_path(path))


def explorer_command(path):
    """The command line that makes Explorer open the folder with ``path`` selected.

    Raises ValueError when no safe command can be built (empty path, a quote / control
    character, a path that is a drive root or ends in a backslash).  The caller then falls back
    to opening the folder and says so; it never passes a half-built command to Explorer.
    """
    text = str(path or "")
    if not text.strip():
        raise ValueError("empty path")
    if any(ch == '"' or ord(ch) < 32 for ch in text):
        raise ValueError("a quote or control character in the path")
    clean = windows_path(text)
    drive, rest = ntpath.splitdrive(clean)
    if not drive or rest in ("", "\\") or clean.endswith("\\"):
        raise ValueError("not a path to one item: %s" % clean)
    return 'explorer /select,"%s"' % clean


def plan_open(path, action, is_dir, pathmod=os.path):
    """``(target, launch_action)`` for one row.

    ``action``: ``open`` (the item itself), ``reveal`` (show it in its folder) or ``folder`` (open
    the folder it is in).  ``is_dir``: the row's ``dest`` is a folder.  ``launch_action`` is what
    the launcher is asked for: ``open`` or ``reveal``.

    =========  ===========================  ==============================
    action     file row                     folder row
    =========  ===========================  ==============================
    open       the file, open               the folder, open
    reveal     the file, reveal (selected)  the folder, open (no parent)
    folder     the file's folder, open      the folder, open (no parent)
    =========  ===========================  ==============================
    """
    path = str(path)
    if action not in (OPEN, REVEAL, "folder"):
        raise ValueError("action must be open, reveal or folder")
    if is_dir:
        return path, OPEN
    if action == OPEN:
        return path, OPEN
    if action == REVEAL:
        return path, REVEAL
    return pathmod.dirname(path), OPEN
