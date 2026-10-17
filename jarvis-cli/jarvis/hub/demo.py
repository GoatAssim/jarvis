"""The demo provider (plan section 11.6): fake downloads to check the panel by hand.

Seven rows covering every look the panel has -- a bar that climbs 0 -> 100 and then
"verifies", an indeterminate one, a post-processing one, a stalled one, a failing one, a
read-only game with a link, and one that stops reporting so it goes stale.

Everything is a pure function of the seconds since the demo started (`rows_at`), so it is
testable without a clock and the daemon only has to call `tick` once a second.  The demo
is an ordinary provider: it writes through `Store.upsert_provider_row` like any tool would,
declares no capabilities (so every row shows the read-only badge) and its rows are
deleted again with `clear`.
"""

PROVIDER = "demo"
CLIMB_SECONDS = 60.0
VERIFY_SECONDS = 6.0
FAIL_AT = 15.0
SILENT_AFTER = 10.0
_MB = 1024 * 1024


def rows_at(elapsed):
    """{local id: fields, or None when that row sends no report at this moment}."""
    t = max(0.0, float(elapsed))
    total = 600 * _MB
    rows = {}

    # 1. determinate: only bytes are reported, so percent / speed / ETA are the hub's
    #    estimates and show with a "≈"; then a "verifying" phase at 100 %; then done.
    if t < CLIMB_SECONDS:
        done = int(total * t / CLIMB_SECONDS)
        rows["climb"] = {"title": "demo-model.gguf", "subtitle": "determinate", "kind": "file",
                         "state": "downloading", "bytes_done": done, "bytes_total": total}
    elif t < CLIMB_SECONDS + VERIFY_SECONDS:
        rows["climb"] = {"title": "demo-model.gguf", "subtitle": "determinate", "kind": "file",
                         "state": "postprocessing", "phase": "verifying", "phase_index": 2,
                         "phase_count": 2, "bytes_done": total, "bytes_total": total, "percent": 100}
    else:
        rows["climb"] = {"title": "demo-model.gguf", "subtitle": "determinate", "kind": "file",
                         "state": "done", "bytes_done": total, "bytes_total": total}

    # 2. indeterminate: running, no percent to give.
    rows["indeterminate"] = {"title": "Store download", "subtitle": "no progress available", "kind": "file",
                             "state": "downloading", "percent": None, "message": "the store gives no progress"}

    # 3. post-processing: striped bar at 100 %.
    rows["postproc"] = {"title": "Demo installer", "subtitle": "installing", "kind": "file",
                        "state": "postprocessing", "phase": "installing", "phase_index": 2,
                        "phase_count": 3, "percent": 100}

    # 4. stalled: amber.
    rows["stalled"] = {"title": "Demo torrent", "subtitle": "stalled", "kind": "torrent", "state": "stalled",
                       "percent": 37, "peers": 0, "message": "0 peers for 90 s"}

    # 5. failing: climbs for a while, then fails with a message.
    if t < FAIL_AT:
        rows["failing"] = {"title": "Demo broken.zip", "subtitle": "fails after a few seconds", "kind": "file",
                           "state": "downloading", "percent": round(41.0 * t / FAIL_AT, 1)}
    else:
        rows["failing"] = {"title": "Demo broken.zip", "subtitle": "fails after a few seconds", "kind": "file",
                           "state": "failed", "percent": 41, "error": "checksum mismatch (demo)"}

    # 6. read-only game with a link, a detail list and per-file children.
    rows["game"] = {"title": "Demo Game", "subtitle": "update", "kind": "game", "state": "downloading",
                    "speed_bps": 12 * _MB, "eta_s": 180,
                    "bytes_done": int(3.2 * 1024 * _MB + t * 12 * _MB), "bytes_total": int(14.1 * 1024 * _MB),
                    "detail": [{"label": "Store", "value": "Demo store"}, {"label": "Version", "value": "1.4 -> 1.5"}],
                    "children": [{"name": "depot_1.bin", "percent": 80, "state": "downloading"},
                                 {"name": "depot_2.bin", "percent": 12, "state": "queued"}],
                    "links": [{"label": "Open store page", "kind": "url", "target": "https://example.com/"}]}

    # 7. silent: reports at the start, then never again -> the hub marks it stale, not failed.
    if t < SILENT_AFTER:
        rows["silent"] = {"title": "Demo silent provider", "subtitle": "stops reporting", "kind": "file",
                          "state": "downloading", "percent": round(2.0 * t, 1)}
    else:
        rows["silent"] = None
    return rows


def ensure_provider(store):
    return store.register_provider(
        PROVIDER, label="Demo provider", mode="push", kind="file", icon="D",
        capabilities=[], controls={}, open_roots=[], source_file="download_hub.py")


def tick(store, now):
    """Advance the demo: write every row that has something to say right now."""
    demo = store.get_settings().get("demo") or {}
    if not demo.get("enabled"):
        return 0
    elapsed = now - float(demo.get("started_at") or now)
    written = 0
    for local_id, fields in rows_at(elapsed).items():
        if fields is None:
            continue
        item, _ = store.upsert_provider_row(PROVIDER, local_id, fields, now=now)
        written += 1 if item else 0
    return written


def _rows(store):
    return store.list_items(provider=PROVIDER, include_disabled=True, limit=100)


def start(store):
    """(Re)start the demo: old demo rows go, new ones appear immediately."""
    provider, err = ensure_provider(store)
    if err:
        return {"ok": False, "error": err}
    store.delete_items([r["id"] for r in _rows(store)])
    now = store.now()
    store.set_setting_raw("demo", {"enabled": True, "started_at": now})
    written = tick(store, now)
    return {"ok": True, "rows": written, "state": "running"}


def stop(store):
    """Stop advancing; anything unfinished is marked cancelled so it stops looking busy."""
    store.set_setting_raw("demo", {"enabled": False, "started_at": 0})
    changed = 0
    for row in _rows(store):
        if row["state"] not in ("done", "failed", "cancelled"):
            if store.update_item(row["id"], {"state": "cancelled", "message": "demo stopped"}):
                changed += 1
    return {"ok": True, "cancelled": changed, "state": "stopped"}


def clear(store):
    stop(store)
    removed = store.delete_items([r["id"] for r in _rows(store)])
    return {"ok": True, "removed": removed, "state": "cleared"}


def status(store):
    demo = store.get_settings().get("demo") or {}
    rows = _rows(store)
    return {"ok": True, "running": bool(demo.get("enabled")), "rows": len(rows),
            "elapsed_s": round(store.now() - float(demo["started_at"]), 1)
            if demo.get("enabled") and demo.get("started_at") else None}
