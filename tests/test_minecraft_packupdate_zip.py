"""MC-C: update_modpack_file copies the old pack's overrides through the pack writer's ZIP SOURCE (packs.overrides_from_zip +
packs.write_pack) instead of code of its own.  What these tests pin:

  * the pack that comes out is byte for byte what an independent reference writer (zipfile, the documented layout) makes
    from the same input: manifest first, then the old pack's overrides in the OLD pack's order, under their own folder
    names (overrides/, client-overrides/, server-overrides/), directory entries and files outside overrides not carried;
  * the old pack is only read; a dry run writes nothing; the new file never replaces an existing one;
  * the writer's name rules and the three overrides caps now apply to an update too (refused up front, dry run included);
  * the old pack changing or being damaged between the report and the write stops the write and leaves nothing behind.

Plain asserts, no network, a throwaway HOME, fake adapter in memory.  No fixture file is read.
Run: python tests/test_minecraft_packupdate_zip.py"""
import copy
import hashlib
import json
import os
import random
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path

HOME = os.path.realpath(tempfile.mkdtemp(prefix="jarvis-packupdate-zip-"))
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER", "CURSEFORGE_API_KEY"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tools, ui_bridge  # noqa: E402
from jarvis.actions import minecraft_packread as mpr  # noqa: E402
from jarvis.modhosts import packread, packs, sources  # noqa: E402

tools._tool_index()
PASS, FAIL = [], []
COUNTER = [0]


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


def new_dir(label="d"):
    COUNTER[0] += 1
    path = Path(HOME) / ("%s%d" % (label, COUNTER[0]))
    path.mkdir(parents=True, exist_ok=True)
    return path


def listing(folder):
    return sorted(p.name for p in Path(folder).iterdir()) if Path(folder).exists() else []


def set_settings(**values):
    path = Path(HOME) / ".jarvis" / "tool_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if values:
        path.write_text(json.dumps({"minecraft_lookup": values}), encoding="utf-8")
    elif path.exists():
        path.unlink()


def sha1(text):
    return hashlib.sha1(text.encode()).hexdigest()


def sha512(text):
    return hashlib.sha512(text.encode()).hexdigest()


# --------------------------------------------------------------------------- one project, an old file and a new one

SOD = "AANobbMI"


def _file(fid, number, date):
    name = "sodium-%s.jar" % number
    return {"source": "modrinth", "project_id": SOD, "file_id": fid, "file_name": name, "version_label": number,
            "mc_versions": ["1.20.1"], "loaders": ["fabric"], "channel": "release", "date": date, "size": 1000 + len(name),
            "download_url": "https://cdn.modrinth.com/data/%s/versions/%s/%s" % (SOD, fid, name), "downloadable": True,
            "hashes": {"sha1": sha1(fid), "sha512": sha512(fid)}, "dependencies": []}


PROJECT = {"source": "modrinth", "id": SOD, "slug": "sodium", "name": "Sodium", "summary": "", "author": "someone", "downloads": 1,
           "updated": "", "kind": "mod", "mc_versions": ["1.20.1"], "loaders": ["fabric"], "page_url": "https://modrinth.com/mod/sodium",
           "distributable": True, "client_side": "required", "server_side": "unsupported", "categories": []}
FILES = [_file("sodOld01", "0.5.2", "2023-06-01T10:00:00Z"), _file("sodNew01", "0.5.8", "2024-01-10T10:00:00Z")]


class Fake:
    def bulk_projects(self, refs):
        return {"ok": True, "projects": {r: (copy.deepcopy(PROJECT) if str(r) in (SOD, "sodium") else None) for r in refs}}

    def bulk_list_files(self, ids, mc_version=None, loader=None):
        return {"ok": True, "files": {p: sorted(copy.deepcopy(FILES), key=lambda f: f["date"], reverse=True) for p in ids}}

    def bulk_get_files(self, pairs):
        return {"ok": True, "files": {"%s:%s" % (p, f): next((copy.deepcopy(x) for x in FILES if x["file_id"] == str(f)), None) for p, f in pairs}}

    def versions_by_hashes(self, hashes, algorithm="sha1"):
        found = {f["hashes"]["sha1"]: copy.deepcopy(f) for f in FILES if f["hashes"]["sha1"] in hashes}
        return {"ok": True, "files": found, "primary": {k: True for k in found}}

    def get_project(self, ref):
        return {"ok": True, "project": copy.deepcopy(PROJECT)}


class World:
    def __init__(self, answer=True):
        self.answer, self.asked = answer, []

    def __enter__(self):
        self.saved = (sources.get, sources.has_curseforge_key, ui_bridge.confirm)
        fake = Fake()
        sources.get = lambda name: fake if str(name).strip().lower() == "modrinth" else None
        sources.has_curseforge_key = lambda: False

        def confirm(question, body="", level="warn", confirm_label="Yes", cancel_label="No", default=False, timeout=None):
            self.asked.append(question)
            return self.answer
        ui_bridge.confirm = confirm
        return self

    def __exit__(self, *exc):
        sources.get, sources.has_curseforge_key, ui_bridge.confirm = self.saved
        set_settings()


INDEX = {"formatVersion": 1, "game": "minecraft", "versionId": "1.0.0", "name": "Hand Pack", "summary": "s",
         "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.15.11"},
         "files": [{"path": "mods/sodium-0.5.2.jar", "hashes": {"sha1": sha1("sodOld01"), "sha512": sha512("sodOld01")},
                    "fileSize": 1234, "downloads": ["https://cdn.modrinth.com/data/x/versions/sodOld01/f.jar"]}]}

# the old pack's order is on purpose not sorted, and it has things that are NOT carried (a folder entry, a README)
OVERRIDES = [("overrides/zeta.txt", b"z=1\n" * 50), ("overrides/config/alpha.toml", b"a=1\n" * 500),
             ("client-overrides/options.txt", b"fov:70\n"), ("server-overrides/server.properties", bytes(range(256)) * 300),
             ("overrides/mods/inner.jar", b"PK\x03\x04 not really a jar"), ("overrides/run.bat", b"@echo off\r\n")]


def old_pack(folder, name="old.mrpack", overrides=OVERRIDES, index=INDEX, stored=False, extra=()):
    path = Path(folder) / name
    kind = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(path, "w", kind) as z:
            z.writestr("modrinth.index.json", json.dumps(index))
            z.writestr("README.txt", b"not carried")
            z.writestr("overrides/empty-folder/", b"")
            for member, data in overrides:
                z.writestr(member, data)
            for info, data in extra:
                z.writestr(info, data)
    return path


def run(pack, dest, **over):
    args = {"path": str(pack), "dest": str(dest)}
    args.update(over)
    return mpr.tool_update_modpack_file(args)


def reference_bytes(new_pack, overrides):
    """What the new pack must be, made by zipfile alone: the manifest document the writer produced (re-serialised the
    documented way), then every override of the old pack in the old pack's order, with the documented entry settings."""
    import stat
    with zipfile.ZipFile(new_pack) as z:
        doc = json.loads(z.read("modrinth.index.json").decode("utf-8"))
    import io
    buf = io.BytesIO()

    def info(name):
        i = zipfile.ZipInfo(name, date_time=packs.FIXED_TIME)
        i.compress_type = zipfile.ZIP_DEFLATED
        i.create_system = 3
        i.external_attr = (stat.S_IFREG | 0o644) << 16
        return i
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(info("modrinth.index.json"), (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        for member, data in overrides:
            z.writestr(info(member), data)
    return buf.getvalue()


# --------------------------------------------------------------------------- tests

def test_the_new_pack_is_byte_for_byte_the_reference_layout():
    with World() as w:
        old = old_pack(new_dir("pk"))
        out = new_dir("out")
        before = old.read_bytes()
        res = run(old, out)
        check("written", res["ok"] and res["written"] is True and res["counts"]["override_files"] == 6, res)
        check("the bytes are exactly the reference writer's: manifest first, the old order, own folder names, nothing else",
              Path(res["path"]).read_bytes() == reference_bytes(res["path"], OVERRIDES), listing(out))
        with zipfile.ZipFile(res["path"]) as z:
            names = z.namelist()
        check("README and the folder entry were not carried", "README.txt" not in names and "overrides/empty-folder/" not in names, names)
        check("client-overrides/ and server-overrides/ keep their names", "client-overrides/options.txt" in names and "server-overrides/server.properties" in names, names)
        info = packread.read_pack(res["path"])
        check("the new pack re-reads clean", info["ok"] and info["complete"], info.get("problems"))
        check("the old pack was only read", old.read_bytes() == before)
        check("exactly one new file in the destination", listing(out) == [Path(res["path"]).name], listing(out))
        again = run(old, out)
        check("a second run takes the next free name and leaves the first alone",
              again["ok"] and again["filename"].endswith("-updated-2.mrpack") and len(listing(out)) == 2
              and Path(res["path"]).read_bytes() == reference_bytes(res["path"], OVERRIDES), again)


def test_a_dry_run_reads_everything_and_writes_nothing():
    with World() as w:
        old = old_pack(new_dir("pk"))
        out = new_dir("out")
        res = run(old, out, dry_run=True)
        check("ok, dry run, nothing written, nothing asked, the destination not even created or touched",
              res["ok"] and res["dry_run"] is True and res["written"] is False and w.asked == [] and listing(out) == [], res)
        check("it still reports the override files and their size", res["counts"]["override_files"] == 6 and res["overrides"]["bytes"] > 0, res.get("overrides"))


def test_unsafe_override_names_are_refused_before_anything_is_written():
    with World() as w:
        for label, member in (("a reserved device name", "overrides/config/CON.txt"), ("a trailing dot", "overrides/dir./file.txt")):
            old = old_pack(new_dir("pk"), overrides=OVERRIDES + [(member, b"x")])
            for dry in (True, False):
                out = new_dir("out")
                res = run(old, out, dry_run=dry)
                check("%s: refused (dry_run=%s), nothing written, nothing asked" % (label, dry), res["ok"] is False and res.get("written") is not True
                      and listing(out) == [] and w.asked == [], res)
        old = old_pack(new_dir("pk"), overrides=OVERRIDES + [("overrides/A.txt", b"1"), ("overrides/a.txt", b"2")])
        res = run(old, new_dir("out"))
        check("two names that differ only by case are never both written", res["ok"] is False and res.get("written") is not True, res)


def test_the_three_overrides_caps_apply_to_an_update_too():
    with World() as w:
        small = old_pack(new_dir("pk"))
        rnd = random.Random(7)                      # not compressible: a block of zeros would trip packread's zip-bomb check instead
        big = old_pack(new_dir("pk"), overrides=OVERRIDES + [("overrides/big.bin", bytes(rnd.getrandbits(8) for _ in range(1024 * 1024 + 10)))])
        for label, pack, settings in (("overrides_max_files", small, {"overrides_max_files": 3}),
                                      ("overrides_max_file_mb", big, {"overrides_max_file_mb": 1}),
                                      ("overrides_max_total_mb", big, {"overrides_max_total_mb": 1})):
            set_settings(**settings)
            for dry in (True, False):
                out = new_dir("out")
                res = run(pack, out, dry_run=dry)
                check("%s: too_big (dry_run=%s), nothing written, nothing asked" % (label, dry), res["ok"] is False and res.get("reason") == "too_big"
                      and res.get("written") is not True and listing(out) == [] and w.asked == [], res)
        set_settings()
        res = run(big, new_dir("out"), dry_run=True)
        check("without a cap (the default) the same big pack is fine", res["ok"], res)


def test_the_old_pack_changing_after_the_report_stops_the_write():
    with World() as w:
        scenarios = []
        old = old_pack(new_dir("pk"))
        scenarios.append(("one override with other bytes of the same size", old, lambda p: old_pack(p.parent, p.name, overrides=[
            (m, d if m != "overrides/zeta.txt" else b"y=2\n" * 50) for m, d in OVERRIDES])))
        old2 = old_pack(new_dir("pk"))
        scenarios.append(("one override gone", old2, lambda p: old_pack(p.parent, p.name, overrides=OVERRIDES[1:])))
        old3 = old_pack(new_dir("pk"), stored=True)

        def flip(p):
            raw = bytearray(p.read_bytes())
            with zipfile.ZipFile(p) as z:
                i = z.getinfo("overrides/config/alpha.toml")
            raw[i.header_offset + 30 + len(i.filename) + len(i.extra) + 7] ^= 0xFF
            p.write_bytes(bytes(raw))
        scenarios.append(("bytes damaged inside the pack", old3, flip))
        old4 = old_pack(new_dir("pk"))
        scenarios.append(("the pack deleted", old4, lambda p: p.unlink()))
        for label, old, damage in scenarios:
            out = new_dir("out")
            prepared = packupdate_prepare(old, out)
            damage(old)
            kept = old.read_bytes() if old.exists() else None
            from jarvis.modhosts import packupdate
            got = packupdate.write(prepared)
            check("%s: the write is refused and says nothing was kept" % label, got["ok"] is False and got["written"] is False
                  and got["reason"] == "verify_failed", got)
            check("%s: nothing is left in the destination, the old pack is as the damage left it" % label,
                  listing(out) == [] and (old.read_bytes() if old.exists() else None) == kept, listing(out))


def packupdate_prepare(old, out):
    from jarvis.modhosts import packupdate
    got = packupdate.prepare({"path": str(old), "dest": str(out)})
    assert got["ok"], got
    return got["prepared"]


def test_no_override_member_is_ever_opened_extracted_or_run():
    with World() as w:
        nested = new_dir("n") / "nested.zip"
        with zipfile.ZipFile(nested, "w") as z:
            z.writestr("x.txt", b"x")
        members = OVERRIDES + [("overrides/mods/nested.jar", nested.read_bytes())]
        old = old_pack(new_dir("pk"), overrides=members)
        out = new_dir("out")
        opened, real = [], zipfile.ZipFile

        class Spy(real):
            def __init__(self, file, *a, **k):
                opened.append(str(file) if isinstance(file, (str, Path)) else "<handle>")
                super().__init__(file, *a, **k)
        packs.zipfile.ZipFile = Spy
        try:
            res = run(old, out)
        finally:
            packs.zipfile.ZipFile = real
        check("written, and the nested archive and the scripts are copied byte for byte", res["ok"] and Path(res["path"]).read_bytes() == reference_bytes(res["path"], members), res)
        by_name = [o for o in opened if o != "<handle>"]
        check("by name only the old pack (to copy) and the temp file (to re-check) were opened by the writer",
              all(o == str(old) or o.endswith(".part") for o in by_name), by_name)
        check("nothing but the new pack is in the destination", listing(out) == [Path(res["path"]).name], listing(out))


def test_the_update_reaches_the_zip_source_through_the_writer_only():
    seen = []
    real_from, real_write = packs.overrides_from_zip, packs.write_pack
    packs.overrides_from_zip = lambda *a, **k: seen.append("source") or real_from(*a, **k)
    packs.write_pack = lambda plan, describe_with=None: seen.append("write:%s:%s" % (plan["overwrite"], plan["overrides"]["kind"])) or real_write(plan, describe_with=describe_with)
    try:
        with World():
            old = old_pack(new_dir("pk"))
            res = run(old, new_dir("out"))
    finally:
        packs.overrides_from_zip, packs.write_pack = real_from, real_write
    check("one source plan, then one write with overwrite False and a zip source", res["ok"] and seen == ["source", "write:False:zip"], seen)


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
