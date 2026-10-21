"""Record what YOUR Stremio addons really answer, so the marker table can be fixed from facts.

Brief 07 (phase 6) ranks streams by quality and by dub / sub, and neither is a field: both are read
from the stream's text with ONE table (jarvis-cli/jarvis/hub/stremio/markers.py).  That table is
currently a set of GUESSES.  This script is the experiment that replaces the guesses.

Not a test (no `test_` prefix, the runner skips it).  Needs the network and your addons; run it on
the PC where Stremio is set up.  It only READS: it asks your addons about the one title you give it.

    python tests/addon_checks.py "<a show you may download>" [--season 1] [--episode 1] [--out addon-answers.json]
    python tests/addon_checks.py tt0000000 --type series --season 1 --episode 1

It uses the same code Jarvis uses (guarded HTTP, the addon list from ~/.jarvis/stremio.json or your
account), so a refusal here is the refusal Jarvis would give.  Send back the output file (or just
the "streams" part).  The file contains stream names and descriptions only: no addon addresses, no
keys; direct-link streams are recorded WITHOUT their address.

Then: look at each row's `read_as` against its `text`.  Where the table read it wrong (a dub it
called unknown, a sub it called a dub), that wording belongs in markers.py, or in
~/.jarvis/hub/stream_markers.json without touching code.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import addons as A, discovery, ranking  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("title", help="a title to look up, or an id (tt... / kitsu:N)")
    ap.add_argument("--type", default=None)
    ap.add_argument("--season", type=int, default=1)
    ap.add_argument("--episode", type=int, default=1)
    ap.add_argument("--out", default="addon-answers.json")
    args = ap.parse_args()

    net = A.Net(allow=Store().get_settings().get("allow_private_hosts") or [])
    found, notes = A.installed(net)
    out = {"notes": notes, "addons": [a.public() for a in found]}
    print("addons: %s" % ", ".join(a.name for a in found) or "none")
    for note in notes:
        print("note:", note)
    if not found:
        sys.exit("no addons known; see the notes above")

    answer = discovery.find(net, found, args.title, args.type)
    out["search"] = [{k: r.get(k) for k in ("id", "type", "name", "year", "id_kind", "addon", "images")} for r in answer["results"][:8]]
    print("search results:")
    for r in out["search"]:
        print("   %-14s %-8s %s (%s)" % (r["id"], r["type"], r["name"], r["addon"]))
    if not answer["results"]:
        sys.exit("nothing found: %s" % answer["errors"])
    top = answer["results"][0]
    ep = discovery.episodes(net, found, top["type"], top["id"])
    out["title"] = {"id": ep["id"], "type": ep["type"], "name": ep["title"], "seasons": discovery.seasons_of(ep["videos"]),
                    "image_fields": sorted(ep["images"])}
    video = next((v for v in ep["videos"] if v["season"] == args.season and v["episode"] == args.episode), None)
    if video is None and ep["type"] == "movie":
        video = {"id": ep["id"]}
    if video is None:
        sys.exit("that title has no season %d episode %d; seasons: %s" % (args.season, args.episode, out["title"]["seasons"]))
    out["video_id"] = video["id"]
    print("asking every addon for", video["id"])
    streams = discovery.streams_for(net, found, top["type"], video["id"])
    out["answered"], out["failed"] = streams["answered"], streams["failed"]
    table = []
    for stream, addon_name in streams["streams"]:
        release = ranking.parse_release(stream)
        text = " | ".join(str(stream.get(k)) for k in ("name", "title", "description") if stream.get(k))
        hints = stream.get("behaviorHints") if isinstance(stream.get("behaviorHints"), dict) else {}
        table.append({"addon": addon_name, "text": text[:600], "filename": hints.get("filename"),
                      "fields": sorted(stream), "hint_fields": sorted(hints), "has_infoHash": "infoHash" in stream,
                      "has_url": "url" in stream, "has_fileIdx": "fileIdx" in stream,
                      "read_as": {k: release[k] for k in ("resolution", "audio", "codec", "source", "size", "seeders", "banned")}})
    out["streams"] = table
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\n%d streams from %d addon(s).  How the table read each one:" % (len(table), len(streams["answered"])))
    for row in table[:40]:
        r = row["read_as"]
        print("  [%s] %sp %-7s  %s" % (row["addon"][:14], r["resolution"] or "?", r["audio"], row["text"].replace("\n", " ")[:90]))
    print("\nwrote", args.out, "- send it back (it holds no addon addresses and no keys)")


if __name__ == "__main__":
    main()
