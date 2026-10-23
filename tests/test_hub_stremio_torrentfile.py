"""Brief 22: hub/stremio/torrentfile.py on its own (no server).  Linux only; the Windows branches are not run.

Covers: the bencode check (a text file that starts with "d", cut-off data, a v2-only shape, nesting and size bounds), the
infohash (SHA-1 of the info dictionary's own bytes), the network-share decision from a mount table, the kept copies
(content-addressed, refused when they no longer hash to their name), and the path rules for a file on disk.

Run: python tests/test_hub_stremio_torrentfile.py
"""
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-torrentfile-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub.stremio import torrentfile as tf  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


INFO = b"d6:lengthi5000e4:name8:file.mp412:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"e"
BLOB = b"d8:announce13:udp://t.x:1/a4:info" + INFO + b"e"
SHA = hashlib.sha1(INFO).hexdigest()

info, why = tf.parse(BLOB)
check("a real-shaped torrent parses", info is not None and why is None and info["size"] == len(BLOB))
check("its infohash is the SHA-1 of the info dictionary's own bytes (not of the file)", info["infohash"] == SHA != hashlib.sha1(BLOB).hexdigest())
check("key order does not matter: info first, announce after", tf.parse(b"d4:info" + INFO + b"8:announce13:udp://t.x:1/ae")[0]["infohash"] == SHA)
for label, data in (("empty", b""), ("a text file that starts with d", b"dear diary, this is not a torrent"), ("JSON", b'{"info": 1}'),
                    ("a dictionary with no info", b"d3:fooi1ee"), ("an info with no pieces (v2-only shape)", b"d4:infod4:name1:aee"),
                    ("cut off in the middle", BLOB[:-9]), ("a string longer than the file", b"d4:info100:abce"),
                    ("a key that is not a string", b"di1ei2ee"), ("an integer with letters", b"d4:infod6:lengthixxe6:pieces0:ee"),
                    ("not even bytes", None)):
    got, problem = tf.parse(data)
    check("refused: %s" % label, got is None and problem)
deep = b"d4:info" + b"l" * 200 + b"e" * 200 + b"e"
check("refused: nesting beyond the depth limit (no recursion blow-up)", tf.parse(deep)[0] is None)
check("refused: a huge number of tiny items stops at the node limit",
      tf.parse(b"d4:infod6:pieces0:1:xl" + b"i0e" * (tf.MAX_NODES + 5) + b"eee")[0] is None)

# ---- the network decision (from a mount table; the share is never touched)
TABLE = "\n".join(["/dev/sda1 / ext4 rw 0 0", "server:/export /mnt/nas nfs4 rw 0 0", "//srv/share /mnt/smb\\040dir cifs rw 0 0",
                   "/dev/sdb1 /mnt/nas/local ext4 rw 0 0", "tmpfs /tmp tmpfs rw 0 0"])
check("a file under an nfs mount is on a network share", tf.on_network("/mnt/nas/a.torrent", TABLE))
check("a file under a cifs mount whose name has a space (octal escape) is too", tf.on_network("/mnt/smb dir/sub/a.torrent", TABLE))
check("the longest mount wins: a local disk mounted inside the share is not a share", not tf.on_network("/mnt/nas/local/a.torrent", TABLE))
check("an ordinary folder is not", not tf.on_network("/home/me/a.torrent", TABLE) and not tf.on_network("/tmp/a.torrent", TABLE))
check("a folder that merely starts with the mount's name is not inside it", not tf.on_network("/mnt/nas2/a.torrent", TABLE))
check("a UNC path is a share on every system, from its text alone", tf.on_network("\\\\server\\share\\a.torrent") and tf.on_network("//server/share/a.torrent"))
found, sentence = tf.local_torrent("/mnt/nas/a.torrent", TABLE)
check("local_torrent says so in one sentence, without touching the share", found is None and "network share" in sentence and "/mnt" not in sentence)

# ---- kept copies
good = Path(HOME) / "my folder" / "show.S01.torrent"
good.parent.mkdir()
good.write_bytes(BLOB)
found, problem = tf.local_torrent(str(good))
check("a good file on disk is found, with name, stem, size, infohash and the bytes (and no path)",
      problem is None and found["name"] == "show.S01.torrent" and found["stem"] == "show.S01" and found["size"] == len(BLOB)
      and found["infohash"] == SHA and found["blob"] == BLOB and "path" not in found and str(HOME) not in repr({k: v for k, v in found.items() if k != "blob"}))
check("quotes round it (Windows 'Copy as path') are stripped", tf.local_torrent('"%s"' % good)[0]["infohash"] == SHA)
check("a leading/trailing space is ignored", tf.local_torrent("  %s  " % good)[0]["infohash"] == SHA)
check("the extension test ignores case", tf.local_torrent(str(good.with_name("A.TORRENT")))[1] and "find" in tf.local_torrent(str(good.with_name("A.TORRENT")))[1])
check("nothing is kept yet", not tf.staging_dir().exists() or not list(tf.staging_dir().iterdir()))
check("stage() keeps a copy named by the infohash", tf.stage(found) and (tf.staging_dir() / (SHA + ".torrent")).read_bytes() == BLOB)
check("staging twice is the same one file", tf.stage(found) and [p.name for p in tf.staging_dir().iterdir()] == [SHA + ".torrent"])
check("staged_blob returns it for that infohash, in any case", tf.staged_blob(SHA) == BLOB and tf.staged_blob(SHA.upper()) == BLOB)
check("staged_blob for another or invalid hash is None", tf.staged_blob("11" * 20) is None and tf.staged_blob("../../etc/passwd") is None and tf.staged_blob("") is None)
(tf.staging_dir() / ("22" * 20 + ".torrent")).write_bytes(BLOB)
check("a kept file that no longer hashes to its name is not trusted", tf.staged_blob("22" * 20) is None)
(tf.staging_dir() / (SHA + ".torrent")).write_bytes(b"d4:infod6:pieces0:ee")
check("a kept file that was overwritten with something else is not trusted", tf.staged_blob(SHA) is None)

check("looks_like_path: drive, root, share, ~ and quotes", all(tf.looks_like_path(t) for t in ("C:\\a\\b", "d:/x", "/home/x", "\\\\s\\x", "//s/x", "~/x", '"C:\\a"'))
      and not any(tf.looks_like_path(t) for t in ("Sintel", "magnet:?xt=urn:btih:abc", "Show S01E01", "")))
check("a link whose scheme isn't http is still not a path to a file (file:// is not followed)", tf.local_torrent("file:///tmp/a.torrent") == (None, None))
check("a path with a newline or NUL is not this feature's business", tf.local_torrent("/tmp/a\n.torrent") == (None, None) and tf.local_torrent("/tmp/a\x00.torrent") == (None, None))

shutil.rmtree(HOME, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
