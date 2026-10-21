"""The demo provider (plan section 11.6): fake downloads to check the panel by hand.

Seven rows covering every look the panel has -- a bar that climbs 0 -> 100 and then
"verifies", an indeterminate one, a post-processing one, a stalled one, a failing one, a
read-only game with a link, and one that stops reporting so it goes stale.

Everything is a pure function of the seconds since the demo started (`rows_at`), so it is
testable without a clock and the daemon only has to call `tick` once a second.  The demo
is an ordinary provider: it writes through `Store.upsert_provider_row` like any tool would
and its rows are deleted again with `clear`.

Pause / resume / cancel (brief 13, part A, N12).  The demo declares those three verbs and a
control tool for each (`CONTROLS`, thin tools in actions/hub_demo_controls.py that call `control`
here).  A row's own clock stops while it is paused (`rows_at(elapsed, controls)`), so a paused
bar holds still and carries on from the same place; a cancelled row stays cancelled until the
demo is started again.  The only state is the `ctl` block next to the demo switch in the hub
settings; `start` and `stop` drop it.  Nothing here touches another provider's rows: a control
only accepts a demo row's own local id.
"""

PROVIDER = "demo"
CLIMB_SECONDS = 60.0
VERIFY_SECONDS = 6.0
FAIL_AT = 15.0
SILENT_AFTER = 10.0
_MB = 1024 * 1024

# What the demo offers.  The tools are in actions/hub_demo_controls.py (tool name -> `control`).
CAPABILITIES = ["pause", "resume", "cancel"]
CONTROLS = {"pause": "hub_demo_pause", "resume": "hub_demo_resume", "cancel": "hub_demo_cancel"}


def _base_rows(elapsed):
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


ROW_IDS = tuple(_base_rows(0))

# Fields a paused or cancelled row must not keep reporting (a stopped row has no speed, phase or error).
_STOPPED_DROP = ("error", "phase", "phase_index", "phase_count", "speed_bps", "eta_s")


def _row_time(elapsed, ctl):
    """The row's own clock: the demo's elapsed seconds minus the time it spent paused."""
    if not ctl:
        return max(0.0, float(elapsed))
    held = float(ctl.get("paused_total") or 0.0)
    since = ctl.get("paused_since")
    if since is not None:
        held += max(0.0, float(elapsed) - float(since))
    return max(0.0, float(elapsed) - held)


def _base_row(local_id, t):
    """One row as the demo would report it at its own time `t`.  A row that has gone silent
    still has a title and a place to freeze at, so a paused or cancelled one uses its first report."""
    row = _base_rows(t)[local_id]
    return row if row is not None else _base_rows(0)[local_id]


def rows_at(elapsed, controls=None):
    """{local id: fields, or None when that row sends no report at this moment}.

    `controls` is the `ctl` block ({local id: {"paused_total", "paused_since", "cancelled",
    "cancel_t"}}); without it every row simply follows the demo's clock."""
    rows = _base_rows(elapsed)
    for local_id, ctl in (controls or {}).items():
        if local_id not in rows or not isinstance(ctl, dict):
            continue
        if ctl.get("cancelled"):
            fields = _base_row(local_id, float(ctl.get("cancel_t") or 0.0))
            fields = {k: v for k, v in fields.items() if k not in _STOPPED_DROP}
            fields.update({"state": "cancelled", "message": "cancelled"})
            rows[local_id] = fields
        elif ctl.get("paused_since") is not None:
            fields = _base_row(local_id, _row_time(elapsed, ctl))
            fields = {k: v for k, v in fields.items() if k not in _STOPPED_DROP}
            fields.update({"state": "paused", "message": "paused"})
            rows[local_id] = fields
        elif ctl.get("paused_total"):
            # resumed: the row follows its own (shifted) clock
            rows[local_id] = _base_rows(_row_time(elapsed, ctl))[local_id]
    return rows


def ensure_provider(store):
    return store.register_provider(
        PROVIDER, label="Demo provider", mode="push", kind="file", icon="D",
        capabilities=list(CAPABILITIES), controls=dict(CONTROLS), open_roots=[],
        source_file="download_hub.py")


def tick(store, now):
    """Advance the demo: write every row that has something to say right now."""
    demo = store.get_settings().get("demo") or {}
    if not demo.get("enabled"):
        return 0
    elapsed = now - float(demo.get("started_at") or now)
    written = 0
    for local_id, fields in rows_at(elapsed, demo.get("ctl")).items():
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


# --------------------------------------------------------------------------- controls

def _refuse(why):
    return {"ok": False, "error": why}


def control(store, verb, local_id, now=None):
    """Pause, resume or cancel one demo row (what the three demo control tools call).

    `local_id` is the id without the `demo:` prefix, exactly what the hub passes to a provider's
    control tool.  Anything that is not one of the demo's own rows is refused, so a control can
    never reach another provider's row.  Every refusal says why in plain words.  An accepted verb
    is written to the row straight away (the demo is the provider, so it reports like any other
    provider); the daemon's next tick keeps it that way.
    """
    verb = str(verb or "").strip().lower()
    if verb not in CONTROLS:
        return _refuse("the demo provider doesn't offer %s" % (verb or "that"))
    local = str(local_id or "").strip()
    if ":" in local:
        return _refuse("%s is not a demo download (a demo control only works on the demo's own rows)" % local[:60])
    if local not in ROW_IDS:
        return _refuse("there is no demo download called %s" % (local[:60] or "(no id)"))
    demo = store.get_settings().get("demo") or {}
    if not demo.get("enabled"):
        return _refuse("the demo isn't running; start it first")
    now = store.now() if now is None else now
    elapsed = max(0.0, now - float(demo.get("started_at") or now))
    block = dict(demo.get("ctl") or {})
    ctl = dict(block.get(local) or {})
    if ctl.get("cancelled"):
        return _refuse("already cancelled")
    paused = ctl.get("paused_since") is not None
    t = _row_time(elapsed, ctl)
    state = _base_row(local, t)["state"]

    if verb == "pause":
        if paused:
            return _refuse("already paused")
        if state in ("done", "failed"):
            return _refuse("already %s" % state)
        ctl["paused_since"] = elapsed
        message = "paused"
    elif verb == "resume":
        if not paused:
            return _refuse("not paused")
        ctl["paused_total"] = float(ctl.get("paused_total") or 0.0) + max(0.0, elapsed - float(ctl["paused_since"]))
        ctl["paused_since"] = None
        message = "resumed"
    else:  # cancel
        if state in ("done", "failed"):
            return _refuse("already %s" % state)
        ctl["cancelled"] = True
        ctl["cancel_t"] = t
        ctl["paused_since"] = None
        message = "cancelled"

    block[local] = ctl
    store.set_setting_raw("demo", {"enabled": True, "started_at": demo.get("started_at"), "ctl": block})
    fields = rows_at(elapsed, block).get(local)
    if fields is not None:
        store.upsert_provider_row(PROVIDER, local, fields, now=now)
    return {"ok": True, "message": message, "id": "%s:%s" % (PROVIDER, local)}
