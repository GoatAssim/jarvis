"""Helpers for the N24 tests (not a test itself): a tiny bencoder for hand-made .torrent files, and the 30-file torrent
(three seasons of eight episodes, extras, a sample clip, notes and subtitles) the fake Stremio server lists."""
import hashlib


def benc(value):
    if isinstance(value, bool):
        raise TypeError("no bools")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(benc(v) for v in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(benc(k) + benc(v) for k, v in sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())) + b"e"
    raise TypeError(type(value))


def torrent(name, files=None, length=None, extra=None):
    info = {"name": name, "piece length": 16384, "pieces": b"\x00" * 20}
    if files is not None:
        info["files"] = [dict({"length": n, "path": p.split("/") if isinstance(p, str) else p}, **(x or {})) for p, n, x in files]
    else:
        info["length"] = length
    info.update(extra or {})
    return benc({"announce": "http://tracker.invalid/a", "info": info})


def info_hash(blob):
    start = blob.index(b"4:info") + 6
    return hashlib.sha1(blob[start:-1]).hexdigest()


def episode_files():
    files = [("BigPack/Season %d/E%02d.mkv" % (s, e), 1000 + s * 10 + e) for s in (1, 2, 3) for e in range(1, 9)]
    files += [("BigPack/Extras/making of.mp4", 500), ("BigPack/Extras/deleted.mp4", 400), ("BigPack/Sample/sample.mkv", 20),
              ("BigPack/readme.txt", 3), ("BigPack/Subs/E01.srt", 2), ("BigPack/Subs/E02.srt", 2)]
    return files
