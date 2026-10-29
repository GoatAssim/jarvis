"""The hub's sqlite store (plan section 4.2).

Several processes touch download state at once -- the daemon (writes), tool calls and
provider processes (enqueue, report), the web server (reads) -- which is the reason this is
sqlite in WAL mode with a busy timeout and not one of Jarvis's usual JSON files.

Rules this module keeps so nobody has to remember them:

* A connection lives for one operation (open, work, close).  Cheap for sqlite, and it means
  a thread never shares one: workers, the supervisor and a tool call can all use one Store.
* Every multi-step change is one `BEGIN IMMEDIATE` transaction.
* EVERY row write -- an engine's progress, a provider's report, a state change -- goes
  through `_update`, so all of them are sanitised by `states.sanitize_report` and state
  changes are checked against the transition table.
* A provider writes only rows it owns: `upsert_provider_row` builds the id as
  `<provider>:<local id>`, refuses anything that exists under a different owner, and
  whitelists the fields a provider may set.  Nothing here takes a destination path from a
  provider without checking it against the provider's `open_roots`.
"""

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from . import SCHEMA_VERSION, paths, settings as settings_mod, states

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS items(
        id TEXT PRIMARY KEY,
        group_id TEXT, provider TEXT NOT NULL DEFAULT 'hub', owner TEXT NOT NULL DEFAULT 'hub',
        kind TEXT NOT NULL DEFAULT 'file', engine TEXT NOT NULL DEFAULT '',
        source_url TEXT, source_ref_json TEXT, title TEXT NOT NULL DEFAULT '',
        subtitle TEXT NOT NULL DEFAULT '', options_json TEXT,
        state TEXT NOT NULL, phase TEXT, phase_index INTEGER, phase_count INTEGER,
        percent REAL, bytes_done INTEGER, bytes_total INTEGER, speed_bps REAL, eta_s REAL,
        peers INTEGER, seeds INTEGER, derived TEXT NOT NULL DEFAULT '',
        dest_dir TEXT, dest_file TEXT, thumb_ref TEXT, detail_json TEXT, children_json TEXT,
        links_json TEXT, capabilities_json TEXT, controls_json TEXT,
        message TEXT, error TEXT, control TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL, dedupe_key TEXT,
        created_at REAL NOT NULL, started_at REAL, finished_at REAL, updated_at REAL NOT NULL,
        last_report_at REAL, stale INTEGER NOT NULL DEFAULT 0, keep INTEGER NOT NULL DEFAULT 0,
        dest_is_dir INTEGER, dismissed_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS items_state ON items(state)",
    "CREATE INDEX IF NOT EXISTS items_provider ON items(provider, state)",
    "CREATE INDEX IF NOT EXISTS items_group ON items(group_id)",
    "CREATE INDEX IF NOT EXISTS items_dedupe ON items(dedupe_key)",
    """CREATE TABLE IF NOT EXISTS groups(
        id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'batch',
        meta_json TEXT, created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS providers(
        id TEXT PRIMARY KEY, label TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'push',
        kind TEXT NOT NULL DEFAULT 'file', icon TEXT NOT NULL DEFAULT '', source_file TEXT,
        enabled INTEGER NOT NULL DEFAULT 1, interval_s REAL NOT NULL DEFAULT 3,
        poll_tool TEXT, capabilities_json TEXT, controls_json TEXT, open_roots_json TEXT,
        last_ok_at REAL, last_error TEXT, last_report_at REAL, settings_json TEXT,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, ts REAL NOT NULL,
        level TEXT NOT NULL DEFAULT 'info', message TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS events_item ON events(item_id, id)",
    "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)",
    # Once-only bookkeeping: "this row already got its <kind> notification / auto-open".
    # `stamp` tells one occurrence from the next (a retried download finishes again with a
    # new finished_at, so it is announced again; a restart of the daemon is not).
    """CREATE TABLE IF NOT EXISTS notices(
        item_id TEXT NOT NULL, kind TEXT NOT NULL, stamp REAL NOT NULL DEFAULT 0, at REAL NOT NULL,
        PRIMARY KEY(item_id, kind, stamp)
    )""",
)

ITEM_COLUMNS = frozenset((
    "id", "group_id", "provider", "owner", "kind", "engine", "source_url", "source_ref_json",
    "title", "subtitle", "options_json", "state", "phase", "phase_index", "phase_count",
    "percent", "bytes_done", "bytes_total", "speed_bps", "eta_s", "peers", "seeds", "derived",
    "dest_dir", "dest_file", "thumb_ref", "detail_json", "children_json", "links_json",
    "capabilities_json", "controls_json", "message", "error", "control", "attempts",
    "priority", "next_attempt_at", "dedupe_key", "created_at", "started_at", "finished_at",
    "updated_at", "last_report_at", "stale", "keep", "dest_is_dir", "dismissed_at",
))
# python name -> column, for the structured values stored as JSON text
_JSON_FIELDS = {
    "options": "options_json", "source_ref": "source_ref_json", "detail": "detail_json",
    "children": "children_json", "links": "links_json", "capabilities": "capabilities_json",
    "controls": "controls_json",
}
_JSON_DEFAULTS = {
    "options": {}, "source_ref": None, "detail": [], "children": [], "links": [],
    "capabilities": None, "controls": None,
}
# what sanitize_report handles (its keys are also column names)
_REPORT_KEYS = frozenset((
    "title", "subtitle", "phase", "message", "error", "state", "kind", "percent",
    "bytes_done", "bytes_total", "peers", "seeds", "phase_index", "phase_count",
    "speed_bps", "eta_s",
))
# plain columns a trusted caller (daemon, engine, service) may set through _update
_INTERNAL_COLUMNS = frozenset((
    "dest_dir", "dest_file", "next_attempt_at", "attempts", "control", "priority", "group_id",
    "keep", "thumb_ref", "dedupe_key", "started_at", "finished_at", "last_report_at", "stale",
    "source_url", "engine", "dest_is_dir", "dismissed_at",
))
_PROVIDER_JSON = {"capabilities": "capabilities_json", "controls": "controls_json",
                  "open_roots": "open_roots_json", "settings": "settings_json"}

EVENT_KEEP_PER_ITEM = 100
# Rows of the settings table that are runtime state kept next to the settings, not settings.
RUNTIME_KEYS = ("daemon", "notify_state")


# Columns added after SCHEMA_VERSION 1 shipped.  A database made before one existed gets it added
# on the first connection in each process.  This is keyed on the column itself, not on PRAGMA
# user_version, so none of them needs a SCHEMA_VERSION bump.
#   dest_is_dir    N2: NULL = not known, 1 = dest is a folder, 0 = dest is a file
#   dismissed_at   a provider row the owner hid (epoch seconds); NULL = shown
_LATE_COLUMNS = (("dest_is_dir", "INTEGER"), ("dismissed_at", "REAL"))
_COLUMNS_CHECKED = set()


def _ensure_columns(conn, db_path):
    key = str(db_path)
    if key in _COLUMNS_CHECKED:
        return
    names = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    for column, kind in _LATE_COLUMNS:
        if names and column not in names:
            try:
                conn.execute("ALTER TABLE items ADD COLUMN %s %s" % (column, kind))
            except sqlite3.OperationalError as exc:     # the daemon or the web server got there first
                if "duplicate column" not in str(exc).lower():
                    raise
    if names:
        _COLUMNS_CHECKED.add(key)


# Tables added after SCHEMA_VERSION 2 shipped (N23, update findings).  A database that is already at the
# current user_version never runs `_SCHEMA` again, so these are created on the first connection in which
# they are missing (one cheap lookup per connection, then nothing).  Additive only: no existing table or
# column is touched, so no SCHEMA_VERSION bump and an older Jarvis simply never looks at them.
#   updates        one row per available update a source reported: "<provider> has a newer <thing>".  It
#                  is a CURRENT list, not a log.  `available` / `installed` are the source's own words and
#                  are never compared.  dismissed_* = "Ignore this version" (hidden until `available` differs)
#   update_checks  one row per provider: what the update runner knows about it (its update tool, when it
#                  was last checked, whether that worked, what failed) plus the two flags a tool call or
#                  the panel uses to ask the daemon for a check (requested_at) and to be held to the
#                  manual-check limit (manual_at).  Kept apart from `providers` so a poll's health line and a
#                  check's never overwrite each other.
_LATE_TABLES = (
    """CREATE TABLE IF NOT EXISTS updates(
        provider TEXT NOT NULL, local_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
        installed TEXT NOT NULL DEFAULT '', available TEXT NOT NULL DEFAULT '', size_bytes INTEGER,
        source TEXT NOT NULL DEFAULT '', detail_json TEXT,
        checked_at REAL NOT NULL, first_seen_at REAL NOT NULL,
        dismissed_at REAL, dismissed_version TEXT,
        PRIMARY KEY(provider, local_id)
    )""",
    "CREATE INDEX IF NOT EXISTS updates_provider ON updates(provider)",
    """CREATE TABLE IF NOT EXISTS update_checks(
        provider TEXT PRIMARY KEY, update_tool TEXT, start_interval_h REAL,
        running_since REAL, requested_at REAL, manual_at REAL, started_at REAL,
        checked_at REAL, ok_at REAL, error TEXT, problems_json TEXT
    )""",
    # The Updates rework: one row per finding the owner (or the auto-update step) asked to APPLY.  It is the apply
    # queue (state 'queued', read by the daemon's ApplyRunner) and the memory of what happened (done / started /
    # failed), kept after the finding itself is cleared, so "never retry the same failed `available` version" holds
    # even when a check drops the finding and lists it again.  `available` is the finding's version text at the time.
    """CREATE TABLE IF NOT EXISTS update_applies(
        provider TEXT NOT NULL, local_id TEXT NOT NULL, available TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', at REAL NOT NULL,
        batch TEXT, origin TEXT NOT NULL DEFAULT 'manual', queued_at REAL,
        PRIMARY KEY(provider, local_id)
    )""",
    "CREATE INDEX IF NOT EXISTS update_applies_state ON update_applies(state, queued_at)",
)
MAX_UPDATES_PER_PROVIDER = 500          # findings per provider (the same cap as polled rows)
# Columns of `update_checks` added by the Updates rework (what a provider declared about applying updates, copied in
# by the update runner's sync so the panel, the admin op and the chat tool never need the tool registry).  Added to a
# database that already has the table with ALTER TABLE ... ADD COLUMN, once per process, the same way `_LATE_COLUMNS`
# does for `items`; additive only, so no SCHEMA_VERSION bump and an older Jarvis never looks at them.
#   update_group      mods | games | packages | apps | other
#   update_args_json  the arguments of every update_tool call
#   apply_tool        the confirm-gated tool that applies ONE finding (NULL = this provider cannot apply)
#   apply_args_json   arguments merged into every apply call
#   apply_timeout_s   seconds the ApplyRunner waits for one apply call
#   updates_only      1 = the provider only reports / applies updates (no downloads)
#   apply_problem     why an apply_tool that was declared is NOT usable (its schema lacks `id` / `dry_run`), or NULL
_LATE_CHECK_COLUMNS = (("update_group", "TEXT"), ("update_args_json", "TEXT"), ("apply_tool", "TEXT"),
                       ("apply_args_json", "TEXT"), ("apply_timeout_s", "REAL"), ("updates_only", "INTEGER"),
                       ("apply_problem", "TEXT"))
_CHECK_COLUMNS_CHECKED = set()
_CHECK_FIELDS = frozenset((
    "update_tool", "start_interval_h", "running_since", "requested_at", "manual_at", "started_at",
    "checked_at", "ok_at", "error", "problems_json",
    "update_group", "update_args_json", "apply_tool", "apply_args_json", "apply_timeout_s", "updates_only",
    "apply_problem"))
APPLY_STATES = ("queued", "running", "done", "started", "failed")
APPLY_KEEP_S = 90 * 86400.0             # a finished apply's row is forgotten after this long


def _ensure_tables(conn):
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('updates','update_checks','update_applies')"
    ).fetchall()}
    if len(have) == 3:
        return
    for statement in _LATE_TABLES:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:         # another process created it a moment ago
            if "already exists" not in str(exc).lower():
                raise


def _ensure_check_columns(conn, db_path):
    """Add the Updates rework's columns to `update_checks` when this database predates them (see
    _LATE_CHECK_COLUMNS).  One cheap PRAGMA per process and database, then nothing."""
    key = str(db_path)
    if key in _CHECK_COLUMNS_CHECKED:
        return
    names = {r[1] for r in conn.execute("PRAGMA table_info(update_checks)").fetchall()}
    for column, kind in _LATE_CHECK_COLUMNS:
        if names and column not in names:
            try:
                conn.execute("ALTER TABLE update_checks ADD COLUMN %s %s" % (column, kind))
            except sqlite3.OperationalError as exc:     # the daemon or the web server got there first
                if "duplicate column" not in str(exc).lower():
                    raise
    if names:
        _CHECK_COLUMNS_CHECKED.add(key)


def _dest_kind(dest):
    """1 if `dest` is a folder on disk, 0 if a file, None if it is not there (or cannot be asked)."""
    if not dest:
        return None
    try:
        p = Path(dest)
        return None if not p.exists() else int(p.is_dir())
    except (OSError, ValueError):
        return None


def _dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _loads(text, default=None):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def new_id(prefix="h"):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:10])


def row_to_item(row):
    """sqlite row -> the dict everything else uses (JSON columns expanded)."""
    item = dict(row)
    for name, column in _JSON_FIELDS.items():
        item[name] = _loads(item.pop(column, None), _JSON_DEFAULTS[name])
    item["stale"] = bool(item.get("stale"))
    item["keep"] = bool(item.get("keep"))
    item["derived"] = [d for d in (item.get("derived") or "").split(",") if d]
    return item


def _row_to_provider(row):
    prov = dict(row)
    prov["capabilities"] = _loads(prov.pop("capabilities_json", None), [])
    prov["controls"] = _loads(prov.pop("controls_json", None), {})
    prov["open_roots"] = _loads(prov.pop("open_roots_json", None), [])
    prov["settings"] = _loads(prov.pop("settings_json", None), {})
    prov["enabled"] = bool(prov.get("enabled"))
    return prov


def _row_to_update(row):
    """sqlite row -> the finding dict.  `hidden` = the owner chose "Ignore this version" for exactly the
    `available` text this row holds now (a different text shows it again: the store clears the mark)."""
    item = dict(row)
    item["detail"] = _loads(item.pop("detail_json", None), [])
    item["provider_label"] = item.pop("provider_label", None) or item["provider"]
    item["provider_enabled"] = item.pop("provider_enabled", None)
    item["provider_settings"] = _loads(item.pop("provider_settings_json", None), {})
    item["hidden"] = item.get("dismissed_at") is not None and (item.get("dismissed_version") or "") == (item.get("available") or "")
    return item


def _like(text):
    return "%" + str(text).lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Store:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else paths.db_path()
        self._clock = clock

    # ------------------------------------------------------------------ connections

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            # WAL: the web server and the panel read while the daemon writes, and neither waits.
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        conn.execute("PRAGMA synchronous=NORMAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        _ensure_columns(conn, self.path)
        _ensure_tables(conn)
        _ensure_check_columns(conn, self.path)
        return conn

    @contextlib.contextmanager
    def _read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def now(self):
        return self._clock()

    # ------------------------------------------------------------------ events

    def _event(self, conn, item_id, level, message, now):
        conn.execute("INSERT INTO events(item_id, ts, level, message) VALUES(?,?,?,?)",
                     (item_id, now, level, states.clean_text(message, 300) or ""))
        n = conn.execute("SELECT COUNT(*) FROM events WHERE item_id IS ?", (item_id,)).fetchone()[0]
        if n > EVENT_KEEP_PER_ITEM + 20:
            conn.execute(
                "DELETE FROM events WHERE item_id IS ? AND id NOT IN "
                "(SELECT id FROM events WHERE item_id IS ? ORDER BY id DESC LIMIT ?)",
                (item_id, item_id, EVENT_KEEP_PER_ITEM))

    def add_event(self, item_id, level, message):
        with self._write() as conn:
            self._event(conn, item_id, level, message, self.now())

    def list_events(self, item_id, limit=50):
        with self._read() as conn:
            rows = conn.execute(
                "SELECT ts, level, message FROM events WHERE item_id IS ? ORDER BY id DESC LIMIT ?",
                (item_id, int(limit))).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ------------------------------------------------------------------ settings

    def raw_settings(self):
        with self._read() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out = {}
        for row in rows:
            if row["key"] in RUNTIME_KEYS:      # runtime state, not a setting
                continue
            out[row["key"]] = _loads(row["value"], None)
        return out

    def get_settings(self):
        return settings_mod.merged(self.raw_settings())

    @staticmethod
    def _settings_in(conn):
        """The merged settings, read inside a transaction that is already open."""
        stored = {}
        for row in conn.execute("SELECT key, value FROM settings"):
            if row["key"] not in RUNTIME_KEYS:
                stored[row["key"]] = _loads(row["value"], None)
        return settings_mod.merged(stored)

    def set_settings(self, clean):
        """Apply a validated {"a.b": value} update.  Returns the top-level keys changed."""
        with self._write() as conn:
            stored = {}
            for head in {k.partition(".")[0] for k in clean}:
                row = conn.execute("SELECT value FROM settings WHERE key=?", (head,)).fetchone()
                stored[head] = _loads(row["value"], None) if row else None
            current = {k: v for k, v in stored.items() if v is not None}
            changed = settings_mod.apply_dotted(current, clean)
            for head in changed:
                conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (head, _dumps(current[head])))
        return changed

    def set_setting_raw(self, key, value):
        """Internal state kept next to the settings (the demo switch).  Not user-validated."""
        with self._write() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _dumps(value)))

    def get_raw_setting(self, key):
        """One internal runtime value (see RUNTIME_KEYS), or None."""
        with self._read() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return _loads(row["value"], None) if row else None

    # ------------------------------------------------------------------ daemon heartbeat

    def daemon_beat(self, pid, started_at):
        self.set_setting_raw("daemon", {"pid": int(pid), "started_at": started_at, "beat_at": self.now()})

    def daemon_info(self, fresh_within=15.0):
        with self._read() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='daemon'").fetchone()
        info = _loads(row["value"], None) if row else None
        if not isinstance(info, dict) or "beat_at" not in info:
            return {"pid": None, "started_at": None, "beat_at": None, "age_s": None, "alive": False}
        age = max(0.0, self.now() - float(info["beat_at"]))
        return {"pid": info.get("pid"), "started_at": info.get("started_at"),
                "beat_at": info["beat_at"], "age_s": age, "alive": age <= fresh_within}

    # ------------------------------------------------------------------ groups

    def ensure_group(self, title, kind="batch", meta=None, group_id=None):
        now = self.now()
        gid = group_id or new_id("g")
        with self._write() as conn:
            row = conn.execute("SELECT id FROM groups WHERE id=?", (gid,)).fetchone()
            if not row:
                conn.execute("INSERT INTO groups(id, title, kind, meta_json, created_at) VALUES(?,?,?,?,?)",
                             (gid, states.clean_text(title, 120) or "Group", kind, _dumps(meta or {}), now))
        return gid

    def list_groups(self):
        with self._read() as conn:
            rows = conn.execute("SELECT id, title, kind, meta_json, created_at FROM groups").fetchall()
        return {r["id"]: {"id": r["id"], "title": r["title"], "kind": r["kind"],
                          "meta": _loads(r["meta_json"], {}), "created_at": r["created_at"]} for r in rows}

    # ------------------------------------------------------------------ providers

    def register_provider(self, provider_id, label=None, mode="push", kind="file", icon="",
                          capabilities=None, controls=None, open_roots=None, interval_s=None,
                          poll_tool=None, settings=None, source_file=""):
        """Create a provider, or refresh what the TOOL supplies about an existing one.

        What the tool supplies (label, mode, kind, icon, capabilities, controls, roots,
        poll tool) is re-applied on every call.  What the OWNER may change (enabled, polling
        interval, settings) is a starting value written once and never overwritten (plan 5.2).
        Returns (provider, error).
        """
        pid = str(provider_id or "")
        if not states.PROVIDER_ID_RE.match(pid):
            return None, "provider id must be lower_snake (a-z, 0-9, _), at most 32 characters"
        if pid in states.RESERVED_PROVIDERS:
            return None, "%r is reserved for the hub's own engines" % pid
        if mode not in ("push", "poll", "both"):
            return None, "mode must be push, poll or both"
        caps = states.clean_capabilities(capabilities)
        ctrl = {c: t for c, t in states.clean_controls(controls).items() if c in caps}
        roots = [str(r) for r in (open_roots or []) if isinstance(r, (str, Path))][:8]
        now = self.now()
        with self._write() as conn:
            existing = conn.execute("SELECT id FROM providers WHERE id=?", (pid,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE providers SET label=?, mode=?, kind=?, icon=?, source_file=?, poll_tool=?, "
                    "capabilities_json=?, controls_json=?, open_roots_json=? WHERE id=?",
                    (states.clean_text(label or pid, 60), mode,
                     kind if kind in states.KINDS else "file", states.clean_text(icon, 4) or "",
                     states.clean_text(source_file, 120) or "", poll_tool,
                     _dumps(caps), _dumps(ctrl), _dumps(roots), pid))
            else:
                interval = states.clean_number(interval_s, 0.5, 3600) or 3.0
                conn.execute(
                    "INSERT INTO providers(id, label, mode, kind, icon, source_file, enabled, interval_s, "
                    "poll_tool, capabilities_json, controls_json, open_roots_json, settings_json, created_at) "
                    "VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
                    (pid, states.clean_text(label or pid, 60), mode,
                     kind if kind in states.KINDS else "file", states.clean_text(icon, 4) or "",
                     states.clean_text(source_file, 120) or "", interval, poll_tool,
                     _dumps(caps), _dumps(ctrl), _dumps(roots), _dumps(settings or {}), now))
            row = conn.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
        return _row_to_provider(row), None

    def get_provider(self, provider_id):
        with self._read() as conn:
            row = conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        return _row_to_provider(row) if row else None

    def list_providers(self):
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM providers ORDER BY label COLLATE NOCASE").fetchall()
        return [_row_to_provider(r) for r in rows]

    def set_provider_enabled(self, provider_id, enabled):
        with self._write() as conn:
            cur = conn.execute("UPDATE providers SET enabled=? WHERE id=?", (1 if enabled else 0, provider_id))
            return cur.rowcount > 0

    def update_provider(self, provider_id, *, interval_s=None, settings=None):
        """The owner's own choices for a provider (never the tool's): the polling interval and
        its settings block (merged key by key).  Returns the provider or None."""
        with self._write() as conn:
            row = conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
            if row is None:
                return None
            if interval_s is not None:
                conn.execute("UPDATE providers SET interval_s=? WHERE id=?", (interval_s, provider_id))
            if settings is not None:
                merged = dict(_loads(row["settings_json"], {}) or {})
                merged.update(settings)
                conn.execute("UPDATE providers SET settings_json=? WHERE id=?", (_dumps(merged), provider_id))
            return _row_to_provider(conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone())

    def note_provider_health(self, provider_id, ok=True, error=None):
        now = self.now()
        with self._write() as conn:
            if ok:
                conn.execute("UPDATE providers SET last_ok_at=?, last_report_at=?, last_error=NULL WHERE id=?",
                             (now, now, provider_id))
            else:
                conn.execute("UPDATE providers SET last_error=? WHERE id=?",
                             (states.clean_text(error, 200), provider_id))

    # ------------------------------------------------------------------ items: create

    def add_item(self, *, engine, source_url, title="", subtitle="", kind="file", options=None,
                 source_ref=None, group_id=None, state="queued", priority=0, dedupe_key=None):
        """Queue a hub-owned download.  Returns (item, created).

        With a `dedupe_key`, a live (not done / failed / cancelled) item with the same key is
        returned instead of a second one: "never queue the same thing twice" (plan 9.3).
        """
        if state not in ("queued", "paused"):
            state = "queued"
        now = self.now()
        with self._write() as conn:
            if dedupe_key:
                row = conn.execute(
                    "SELECT * FROM items WHERE dedupe_key=? AND state NOT IN ('done','failed','cancelled') "
                    "ORDER BY created_at LIMIT 1", (dedupe_key,)).fetchone()
                if row:
                    return row_to_item(row), False
            item_id = new_id("h")
            conn.execute(
                "INSERT INTO items(id, group_id, provider, owner, kind, engine, source_url, source_ref_json, "
                "title, subtitle, options_json, state, priority, dedupe_key, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, group_id, "hub", "hub", kind if kind in states.KINDS else "file", engine,
                 source_url, _dumps(source_ref) if source_ref is not None else None,
                 states.clean_text(title, states.MAX_TITLE) or "", states.clean_text(subtitle, states.MAX_SUBTITLE) or "",
                 _dumps(options or {}), state, int(priority), dedupe_key, now, now))
            self._event(conn, item_id, "info", "added (%s)" % engine, now)
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return row_to_item(row), True

    # ------------------------------------------------------------------ items: read

    def get_item(self, item_id):
        with self._read() as conn:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return row_to_item(row) if row else None

    def list_items(self, *, states_in=None, engine=None, provider=None, owner=None, kind=None,
                   group_id=None, search=None, stale=None, include_disabled=False,
                   include_dismissed=False, limit=200, offset=0, order="recent"):
        """Items for the panel and the model.  Rows of a switched-off provider are hidden
        unless `include_disabled` (turning a provider off hides its rows, plan 5.2); rows the
        owner dismissed are hidden unless `include_dismissed`."""
        where, args = [], []
        if states_in:
            where.append("i.state IN (%s)" % ",".join("?" * len(states_in)))
            args += list(states_in)
        for column, value in (("i.engine", engine), ("i.provider", provider), ("i.owner", owner),
                              ("i.kind", kind), ("i.group_id", group_id)):
            if value:
                where.append("%s = ?" % column)
                args.append(value)
        if stale is not None:
            where.append("i.stale = ?")
            args.append(1 if stale else 0)
        if search:
            like = _like(search)
            where.append("(LOWER(i.title) LIKE ? ESCAPE '\\' OR LOWER(i.subtitle) LIKE ? ESCAPE '\\' "
                         "OR LOWER(COALESCE(i.source_url,'')) LIKE ? ESCAPE '\\')")
            args += [like, like, like]
        if not include_disabled:
            where.append("(p.id IS NULL OR p.enabled = 1)")
        if not include_dismissed:
            where.append("i.dismissed_at IS NULL")
        sql = ("SELECT i.*, p.label AS provider_label, p.icon AS provider_icon, g.title AS group_title "
               "FROM items i LEFT JOIN providers p ON p.id = i.provider "
               "LEFT JOIN groups g ON g.id = i.group_id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (" ORDER BY i.priority DESC, i.created_at ASC, i.id" if order == "queue"
                else " ORDER BY i.created_at DESC, i.id")
        sql += " LIMIT ? OFFSET ?"
        args += [max(1, min(int(limit), 2000)), max(0, int(offset))]
        with self._read() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [row_to_item(r) for r in rows]

    def counts(self):
        """Totals for the status bar and dl_status.  Rows of disabled providers and dismissed
        rows don't count."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT i.state, i.owner, i.provider, i.engine, i.stale, i.speed_bps, p.label AS plabel "
                "FROM items i LEFT JOIN providers p ON p.id = i.provider "
                "WHERE (p.id IS NULL OR p.enabled = 1) AND i.dismissed_at IS NULL").fetchall()
        out = {"total": 0, "by_state": {}, "by_provider": {}, "by_engine": {}, "active": 0,
               "queued": 0, "failed": 0, "stale": 0, "paused": 0, "done": 0,
               "speed_hub_bps": 0.0, "speed_other_bps": 0.0}
        for r in rows:
            out["total"] += 1
            state = r["state"]
            out["by_state"][state] = out["by_state"].get(state, 0) + 1
            live = state in states.ACTIVE_STATES and not r["stale"]
            if live:
                out["active"] += 1
                speed = r["speed_bps"] or 0.0
                out["speed_hub_bps" if r["owner"] == "hub" else "speed_other_bps"] += speed
            if state == "queued":
                out["queued"] += 1
            elif state == "failed":
                out["failed"] += 1
            elif state == "paused":
                out["paused"] += 1
            elif state == "done":
                out["done"] += 1
            if r["stale"] and state not in states.FINAL_STATES:
                out["stale"] += 1
            if r["owner"] == "hub":
                bucket = out["by_engine"].setdefault(r["engine"] or "?", {"count": 0, "active": 0})
                bucket["count"] += 1
                bucket["active"] += 1 if live else 0
            else:
                label = r["plabel"] or r["provider"]
                bucket = out["by_provider"].setdefault(r["provider"], {"label": label, "count": 0, "active": 0})
                bucket["count"] += 1
                bucket["active"] += 1 if live else 0
        return out

    # ------------------------------------------------------------------ items: change

    def _update(self, conn, row, fields, now, *, touch=True, event=None):
        """The single write path for an existing row.  Returns the updated item, or None
        when the requested state change isn't allowed (then NOTHING is written)."""
        prev = row_to_item(row)
        fields = dict(fields)
        reported = {k: fields.pop(k) for k in list(fields) if k in _REPORT_KEYS}
        clean, notes, derived = states.sanitize_report(reported, prev=prev, now=now)

        old_state = prev["state"]
        new_state = clean.get("state", old_state)
        if new_state != old_state and not states.can_transition(old_state, new_state, prev["owner"]):
            self._event(conn, prev["id"], "debug",
                        "refused state change %s -> %s" % (old_state, new_state), now)
            return None

        updates = dict(clean)
        provided = set()
        if "percent" in reported:
            provided.add("percent")
        if "speed_bps" in reported:
            provided.add("speed")
        if "eta_s" in reported:
            provided.add("eta")
        keep_derived = (set(prev["derived"]) - provided) | derived
        if new_state in states.NO_RATE_STATES:
            keep_derived -= {"speed", "eta"}
        updates["derived"] = ",".join(sorted(keep_derived))

        for name, column in _JSON_FIELDS.items():
            if name in fields:
                updates[column] = _dumps(fields.pop(name)) if fields[name] is not None else None
        for key in list(fields):
            if key in _INTERNAL_COLUMNS:
                updates[key] = fields.pop(key)
        updates["updated_at"] = now
        if touch:
            updates.setdefault("last_report_at", now)
            updates.setdefault("stale", 0)
        # A dismissed row stays hidden while the provider keeps saying it is finished or unknown, and
        # comes back the moment it reports the row live again (a later update of the same Steam game
        # reuses the id `steam:<appid>`).
        came_back = (prev.get("dismissed_at") is not None and "dismissed_at" not in updates
                     and new_state not in states.DISMISSABLE_STATES)
        if came_back:
            updates["dismissed_at"] = None

        if new_state != old_state:
            if new_state in states.ACTIVE_STATES and not prev["started_at"]:
                updates.setdefault("started_at", now)
            if new_state in states.FINAL_STATES:
                updates.setdefault("finished_at", now)
            if old_state in states.FINAL_STATES and new_state == "queued":
                updates["finished_at"] = None
            if new_state not in states.ACTIVE_STATES:
                # A pause / cancel request is answered once the worker lets go; progress
                # writes and active -> active moves must not swallow it.
                updates.setdefault("control", "")

        for key in updates:
            if key not in ITEM_COLUMNS:
                raise ValueError("unknown item column %r" % key)
        names = sorted(updates)
        conn.execute("UPDATE items SET %s WHERE id=?" % ", ".join("%s=?" % n for n in names),
                     [updates[n] for n in names] + [prev["id"]])

        if new_state != old_state:
            line = "%s -> %s" % (old_state, new_state)
            tail = event or updates.get("error") or updates.get("message")
            self._event(conn, prev["id"], "error" if new_state == "failed" else "info",
                        "%s: %s" % (line, tail) if tail else line, now)
        elif event:
            self._event(conn, prev["id"], "info", event, now)
        if came_back:
            self._event(conn, prev["id"], "info", "reported again by its provider; shown again", now)
        for note in notes:
            self._event(conn, prev["id"], "debug", note, now)
        return row_to_item(conn.execute("SELECT * FROM items WHERE id=?", (prev["id"],)).fetchone())

    def update_item(self, item_id, fields, *, expect=None, event=None, touch=True):
        """Change a row (progress, text, internal columns, optionally `state`).

        `expect`: a state or tuple of states the row must currently be in, else nothing
        happens (compare-and-set: two actors can't both win a transition).  Returns the
        updated item, or None if the row is missing, the expectation failed, or the state
        change isn't allowed.
        """
        now = self.now()
        with self._write() as conn:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                return None
            if expect is not None:
                wanted = (expect,) if isinstance(expect, str) else tuple(expect)
                if row["state"] not in wanted:
                    return None
            return self._update(conn, row, fields, now, touch=touch, event=event)

    def transition(self, item_id, new_state, *, expect=None, event=None, **fields):
        fields["state"] = new_state
        return self.update_item(item_id, fields, expect=expect, event=event)

    def report_progress(self, item_id, **fields):
        """An engine's progress write.  Never changes state unless `state` is given."""
        return self.update_item(item_id, fields)

    def set_control(self, item_id, control):
        """Ask the worker holding the row to pause or cancel ('' clears the request)."""
        if control not in ("", "pause", "cancel"):
            return None
        with self._write() as conn:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE items SET control=?, updated_at=? WHERE id=?", (control, self.now(), item_id))
            if control:
                self._event(conn, item_id, "info", "%s requested" % control, self.now())
            return row_to_item(conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())

    def pending_controls(self):
        with self._read() as conn:
            rows = conn.execute("SELECT id, control, state, owner FROM items WHERE control != ''").fetchall()
        return [dict(r) for r in rows]

    def claim_next(self, caps, total_cap, now=None):
        """Pick the next waiting hub item the concurrency limits allow, mark it `probing`
        and count the attempt.  Returns the item or None.  Only the daemon calls this."""
        now = self.now() if now is None else now
        with self._write() as conn:
            active = conn.execute(
                "SELECT engine, COUNT(*) AS n FROM items WHERE owner='hub' AND state IN (%s) GROUP BY engine"
                % ",".join("?" * len(states.ACTIVE_STATES)), states.ACTIVE_STATES).fetchall()
            per_engine = {r["engine"]: r["n"] for r in active}
            if sum(per_engine.values()) >= total_cap:
                return None
            rows = conn.execute(
                "SELECT * FROM items WHERE owner='hub' AND state IN ('queued','stalled') AND control='' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY priority DESC, created_at ASC, id LIMIT 50", (now,)).fetchall()
            for row in rows:
                cap = caps.get(row["engine"], caps.get("default", 2))
                if per_engine.get(row["engine"], 0) >= cap:
                    continue
                conn.execute(
                    "UPDATE items SET state='probing', attempts=attempts+1, next_attempt_at=NULL, "
                    "error=NULL, started_at=COALESCE(started_at, ?), updated_at=?, last_report_at=?, "
                    "stale=0 WHERE id=?", (now, now, now, row["id"]))
                self._event(conn, row["id"], "info", "%s -> probing (attempt %d)"
                            % (row["state"], row["attempts"] + 1), now)
                return row_to_item(conn.execute("SELECT * FROM items WHERE id=?", (row["id"],)).fetchone())
        return None

    def recover_after_restart(self):
        """A restart of the daemon puts everything that was running back in the queue (the
        engine resumes from its .part).  Returns how many rows moved."""
        now = self.now()
        with self._write() as conn:
            rows = conn.execute(
                "SELECT id, state FROM items WHERE owner='hub' AND state IN (%s)"
                % ",".join("?" * (len(states.ACTIVE_STATES) + 1)),
                states.ACTIVE_STATES + ("stalled",)).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE items SET state='queued', speed_bps=NULL, eta_s=NULL, control='', "
                    "next_attempt_at=NULL, message='resuming after a restart', updated_at=? WHERE id=?",
                    (now, row["id"]))
                self._event(conn, row["id"], "info", "%s -> queued (hub restarted)" % row["state"], now)
        return len(rows)

    def delete_items(self, ids, only_states=None):
        """Delete rows with their events.  `only_states`: a row that is NOT in one of these states
        by the time the transaction runs is left alone (retention uses it, so a row retried a
        moment after it was picked is never deleted)."""
        ids = [i for i in ids if i]
        if not ids:
            return 0
        with self._write() as conn:
            if only_states:
                got = ",".join("?" * len(ids))
                ok = ",".join("?" * len(only_states))
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM items WHERE id IN (%s) AND state IN (%s)" % (got, ok),
                    list(ids) + list(only_states)).fetchall()]
                if not ids:
                    return 0
            marks = ",".join("?" * len(ids))
            conn.execute("DELETE FROM events WHERE item_id IN (%s)" % marks, ids)
            conn.execute("DELETE FROM notices WHERE item_id IN (%s)" % marks, ids)
            return conn.execute("DELETE FROM items WHERE id IN (%s)" % marks, ids).rowcount

    def dismiss_items(self, ids, now=None):
        """Hide provider rows that are `unknown` or finished.  Returns the ids actually hidden.

        The rule lives here as well as in the service, so no caller can hide a hub row or a row that
        is still running.  Nothing is deleted: the row stays in the database (a later report from the
        provider may bring it back, see `_update`), and no file is touched.  A row already hidden
        is not counted again."""
        ids = [i for i in dict.fromkeys(ids or []) if i]
        if not ids:
            return []
        now = self.now() if now is None else now
        done = []
        with self._write() as conn:
            for item_id in ids:
                row = conn.execute("SELECT id, owner, state, dismissed_at FROM items WHERE id=?", (item_id,)).fetchone()
                if row is None or row["dismissed_at"] is not None or not states.is_dismissable(row["owner"], row["state"]):
                    continue
                conn.execute("UPDATE items SET dismissed_at=? WHERE id=?", (now, item_id))
                self._event(conn, item_id, "info", "dismissed (hidden; nothing was deleted)", now)
                done.append(item_id)
        return done

    def prune_history(self, keep_days, now=None):
        """Retention: delete history ROWS (and their events) of downloads that finished more than
        `keep_days` days ago.  Returns how many rows went.

        Only rows in a final state (done, failed, cancelled) are candidates, dated by `finished_at`
        and, when a row has none, `updated_at`.  Active, queued, paused, stalled and `unknown` rows
        are never touched, nor is a row flagged `keep`.  `keep_days` of 0 or less (or not a number)
        means keep forever.  This never deletes a file: it only trims the database.  Dismissed rows
        are pruned like any other."""
        try:
            days = float(keep_days)
        except (TypeError, ValueError):
            return 0
        if not days > 0 or days != days:
            return 0
        now = self.now() if now is None else now
        cutoff = now - days * 86400.0
        marks = ",".join("?" * len(states.FINAL_STATES))
        with self._read() as conn:
            rows = conn.execute(
                "SELECT id FROM items WHERE state IN (%s) AND keep = 0 AND COALESCE(finished_at, updated_at) < ?"
                % marks, states.FINAL_STATES + (cutoff,)).fetchall()
        ids = [r["id"] for r in rows]
        gone = 0
        for start in range(0, len(ids), 400):          # well under sqlite's variable limit
            gone += self.delete_items(ids[start:start + 400], only_states=states.FINAL_STATES)
        return gone

    def mark_stale(self, now=None, stale_after=30.0, unknown_after=86400.0):
        """Provider rows that went quiet: flag them (never failed), and after a day move a
        still-unfinished one to `unknown` so it leaves the active counters (plan 5.7)."""
        now = self.now() if now is None else now
        flagged = unknown = 0
        with self._write() as conn:
            rows = conn.execute(
                "SELECT i.id, i.state, i.stale, i.last_report_at, i.created_at, p.interval_s "
                "FROM items i LEFT JOIN providers p ON p.id = i.provider "
                "WHERE i.owner='provider' AND i.state NOT IN ('done','failed','cancelled','unknown')").fetchall()
            for r in rows:
                last = r["last_report_at"] or r["created_at"]
                age = now - last
                limit = max(stale_after, 5.0 * (r["interval_s"] or 3.0))
                if age > unknown_after:
                    conn.execute("UPDATE items SET state='unknown', stale=1, updated_at=? WHERE id=?", (now, r["id"]))
                    self._event(conn, r["id"], "info", "no report for a day; now unknown", now)
                    unknown += 1
                elif age > limit and not r["stale"]:
                    conn.execute("UPDATE items SET stale=1, updated_at=? WHERE id=?", (now, r["id"]))
                    self._event(conn, r["id"], "info", "no report for %d s; marked stale" % int(age), now)
                    flagged += 1
        return {"stale": flagged, "unknown": unknown}

    # ------------------------------------------------------------------ provider rows

    def upsert_provider_row(self, provider_id, local_id, fields, *, create=True, now=None):
        """A provider's report.  Returns (item or None, problems).

        The only way a provider's words reach a row.  The id is `<provider>:<local id>`;
        an existing row under another owner is never touched; a switched-off provider is
        ignored; size / row-count caps and the open_roots / link-scheme checks happen here.
        """
        problems = []
        pid = str(provider_id or "")
        if not states.PROVIDER_ID_RE.match(pid) or pid in states.RESERVED_PROVIDERS:
            return None, ["invalid provider id"]
        local = states.clean_text(local_id, 120)
        if not local:
            return None, ["a download needs a local id"]
        fields = fields if isinstance(fields, dict) else {}
        now = self.now() if now is None else now
        item_id = "%s:%s" % (pid, local)

        with self._write() as conn:
            prow = conn.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
            if prow is None:
                # A tool using hub_client without registering first still works, read-only.
                conn.execute("INSERT INTO providers(id, label, mode, kind, enabled, interval_s, "
                             "capabilities_json, controls_json, open_roots_json, settings_json, created_at) "
                             "VALUES(?,?,?,?,1,3,'[]','{}','[]','{}',?)", (pid, pid, "push", "file", now))
                prow = conn.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
            provider = _row_to_provider(prow)
            if not provider["enabled"]:
                return None, ["provider %s is switched off" % pid]

            settings = self._settings_in(conn)
            limits = settings["provider_limits"]
            if len(_dumps(fields)) > int(limits["max_report_kb"]) * 1024:
                self._provider_error(conn, pid, "report over %s KB dropped" % limits["max_report_kb"])
                return None, ["report too large"]

            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is not None and (row["owner"] != "provider" or row["provider"] != pid):
                return None, ["that row belongs to someone else"]
            if row is None:
                if not create:
                    return None, ["no such download"]
                live = conn.execute(
                    "SELECT COUNT(*) FROM items WHERE provider=? AND state NOT IN ('done','failed','cancelled')",
                    (pid,)).fetchone()[0]
                if live >= int(limits["max_rows"]):
                    self._provider_error(conn, pid, "over %s live rows; new ones dropped" % limits["max_rows"])
                    return None, ["too many live downloads for this provider"]
                conn.execute(
                    "INSERT INTO items(id, provider, owner, kind, engine, title, state, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (item_id, pid, "provider", provider["kind"], "", local[:states.MAX_TITLE],
                     fields.get("state") if fields.get("state") in states.STATES else "queued", now, now))
                row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                self._event(conn, item_id, "info", "first report from %s" % provider["label"], now)

            allowed = {k: fields[k] for k in fields if k in _REPORT_KEYS}
            extra = {}
            if "detail" in fields:
                extra["detail"] = states.clean_detail(fields["detail"])
            if "children" in fields:
                extra["children"] = states.clean_children(fields["children"])
            if "links" in fields:
                extra["links"] = states.clean_links(fields["links"], settings["allowed_link_schemes"])
            if "capabilities" in fields:
                declared = set(provider["capabilities"])
                extra["capabilities"] = [c for c in states.clean_capabilities(fields["capabilities"]) if c in declared]
            if "thumb" in fields:
                thumb = states.clean_thumb(fields["thumb"])
                extra["thumb_ref"] = thumb
                if thumb is None and fields["thumb"]:
                    problems.append("thumbnail dropped (only small image data-URIs are accepted)")
            if "dest" in fields:
                dest = states.clean_text(fields["dest"], 1024)
                roots = provider["open_roots"]
                if dest and roots and not any(_inside(dest, r) for r in roots):
                    problems.append("dest outside this provider's open_roots; dropped")
                    dest = None
                extra["dest_file"] = dest
                # N2: is it a folder?  Steam's dest is.  Re-worked on every report, so a folder that
                # did not exist yet is recognised once it does; unknown (NULL) until then.
                extra["dest_is_dir"] = _dest_kind(dest)
            if fields.get("group"):
                gid = "%s:%s" % (pid, states.clean_text(fields["group"], 80))
                if not conn.execute("SELECT 1 FROM groups WHERE id=?", (gid,)).fetchone():
                    conn.execute("INSERT INTO groups(id, title, kind, meta_json, created_at) VALUES(?,?,?,?,?)",
                                 (gid, states.clean_text(fields.get("group_title") or fields["group"], 120),
                                  "provider", "{}", now))
                extra["group_id"] = gid

            item = self._update(conn, row, {**allowed, **extra}, now)
            conn.execute("UPDATE providers SET last_ok_at=?, last_report_at=?, last_error=NULL WHERE id=?",
                         (now, now, pid))
            for p in problems:
                self._event(conn, item_id, "debug", p, now)
            return item, problems

    @staticmethod
    def _provider_error(conn, pid, message):
        conn.execute("UPDATE providers SET last_error=? WHERE id=?", (states.clean_text(message, 200), pid))

    # ------------------------------------------------------------------ update findings (N23)

    _FINDING_SELECT = ("SELECT u.*, p.label AS provider_label, p.enabled AS provider_enabled, "
                       "p.settings_json AS provider_settings_json FROM updates u "
                       "LEFT JOIN providers p ON p.id = u.provider ")

    @staticmethod
    def _update_put(conn, pid, local, f, now):
        """Write one clean finding (see updates.clean_finding).  'new', 'replaced' or 'full'.

        The same id again REPLACES the row (its first_seen_at stays).  A finding the owner ignored stays
        ignored only while `available` is the very text they ignored; any other text clears the mark."""
        available = f.get("available") or ""
        row = conn.execute("SELECT dismissed_at, dismissed_version FROM updates WHERE provider=? AND local_id=?",
                           (pid, local)).fetchone()
        detail = _dumps(f["detail"]) if f.get("detail") else None
        if row is None:
            count = conn.execute("SELECT COUNT(*) FROM updates WHERE provider=?", (pid,)).fetchone()[0]
            if count >= MAX_UPDATES_PER_PROVIDER:
                return "full"
            conn.execute(
                "INSERT INTO updates(provider, local_id, title, installed, available, size_bytes, source, "
                "detail_json, checked_at, first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pid, local, f["title"], f.get("installed") or "", available, f.get("size_bytes"),
                 f.get("source") or "", detail, now, now))
            return "new"
        keep = row["dismissed_at"] is not None and (row["dismissed_version"] or "") == available
        conn.execute(
            "UPDATE updates SET title=?, installed=?, available=?, size_bytes=?, source=?, detail_json=?, "
            "checked_at=?, dismissed_at=?, dismissed_version=? WHERE provider=? AND local_id=?",
            (f["title"], f.get("installed") or "", available, f.get("size_bytes"), f.get("source") or "",
             detail, now, row["dismissed_at"] if keep else None, row["dismissed_version"] if keep else None,
             pid, local))
        return "replaced"

    def update_offer(self, provider_id, local_id, fields, *, now=None):
        """One finding from a provider (hub_client.offer_update, an update tool's answer goes through
        `update_apply`).  Returns (finding or None, problems).  Sanitised by updates.clean_finding; a
        switched-off provider is ignored; at most MAX_UPDATES_PER_PROVIDER findings; one answer at most
        `provider_limits.max_report_kb`."""
        from . import updates as updates_mod
        pid = str(provider_id or "")
        if not states.PROVIDER_ID_RE.match(pid):
            return None, ["invalid provider id"]
        raw = dict(fields) if isinstance(fields, dict) else {}
        raw["id"] = local_id
        local, cleaned = updates_mod.clean_finding(raw)
        if local is None:
            return None, [cleaned]
        now = self.now() if now is None else now
        with self._write() as conn:
            prow = conn.execute("SELECT enabled FROM providers WHERE id=?", (pid,)).fetchone()
            if prow is not None and not prow["enabled"]:
                return None, ["provider %s is switched off" % pid]
            limits = self._settings_in(conn)["provider_limits"]
            if len(_dumps(raw)) > int(limits["max_report_kb"]) * 1024:
                return None, ["report too large"]
            state = self._update_put(conn, pid, local, cleaned, now)
            if state == "full":
                return None, ["over %d findings for this provider; new ones dropped" % MAX_UPDATES_PER_PROVIDER]
            row = conn.execute(self._FINDING_SELECT + "WHERE u.provider=? AND u.local_id=?", (pid, local)).fetchone()
        return _row_to_update(row), []

    def update_apply(self, provider_id, raw_items, *, complete=False, failed_sources=(), now=None):
        """One update tool's answer, in one transaction.  Returns {"applied", "dropped", "cleared", "problems"}.

        Every item is replaced by id.  `complete=True` means "this is every update I know of": a finding
        the hub holds that the answer no longer lists is CLEARED, with two exceptions that keep a partial
        answer from erasing good data: a finding whose `source` is one of `failed_sources` (that source
        could not be asked), and every finding when any item of the answer was dropped as unusable (the
        hub cannot tell what the dropped item was).  A switched-off provider is ignored."""
        from . import updates as updates_mod
        pid = str(provider_id or "")
        out = {"applied": 0, "dropped": 0, "cleared": 0, "problems": []}
        if not states.PROVIDER_ID_RE.match(pid):
            out["problems"].append("invalid provider id")
            return out
        items = list(raw_items) if isinstance(raw_items, (list, tuple)) else []
        now = self.now() if now is None else now
        failed = {states.clean_text(x, 40).lower() for x in failed_sources if states.clean_text(x, 40)}
        with self._write() as conn:
            prow = conn.execute("SELECT enabled FROM providers WHERE id=?", (pid,)).fetchone()
            if prow is not None and not prow["enabled"]:
                out["problems"].append("provider %s is switched off" % pid)
                return out
            seen = set()
            for raw in items[:MAX_UPDATES_PER_PROVIDER]:
                local, cleaned = updates_mod.clean_finding(raw)
                if local is None:
                    out["dropped"] += 1
                    continue
                if self._update_put(conn, pid, local, cleaned, now) == "full":
                    out["dropped"] += 1
                    continue
                seen.add(local)
                out["applied"] += 1
            out["dropped"] += max(0, len(items) - MAX_UPDATES_PER_PROVIDER)
            if complete:
                if out["dropped"]:
                    out["problems"].append("%d item(s) of the answer were unusable, so nothing was cleared this time"
                                           % out["dropped"])
                else:
                    for row in conn.execute("SELECT local_id, source FROM updates WHERE provider=?", (pid,)).fetchall():
                        if row["local_id"] in seen or (row["source"] or "").lower() in failed:
                            continue
                        conn.execute("DELETE FROM updates WHERE provider=? AND local_id=?", (pid, row["local_id"]))
                        out["cleared"] += 1
        return out

    def update_clear(self, provider_id, local_id):
        """Remove one finding (the source says it is gone).  True if there was one."""
        local = states.clean_text(local_id, 120)
        if not local:
            return False
        with self._write() as conn:
            return conn.execute("DELETE FROM updates WHERE provider=? AND local_id=?",
                                (str(provider_id or ""), local)).rowcount > 0

    def update_dismiss(self, provider_id, local_id, now=None):
        """\"Ignore this version\": hide the finding until a DIFFERENT `available` arrives.  The finding or None."""
        local = states.clean_text(local_id, 120)
        if not local:
            return None
        now = self.now() if now is None else now
        pid = str(provider_id or "")
        with self._write() as conn:
            row = conn.execute("SELECT available FROM updates WHERE provider=? AND local_id=?", (pid, local)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE updates SET dismissed_at=?, dismissed_version=? WHERE provider=? AND local_id=?",
                         (now, row["available"] or "", pid, local))
            out = conn.execute(self._FINDING_SELECT + "WHERE u.provider=? AND u.local_id=?", (pid, local)).fetchone()
        return _row_to_update(out)

    def list_updates(self, provider=None, include_hidden=False):
        """Findings, newest first.  Those of a switched-off provider are never listed.  `include_hidden`
        adds the ones the owner ignored (they carry `hidden: True`).  A provider with no row in
        `providers` is listed (a tool that used hub_client without registering)."""
        sql = self._FINDING_SELECT + "WHERE (p.id IS NULL OR p.enabled=1)"
        args = []
        if provider:
            sql += " AND u.provider=?"
            args.append(str(provider))
        sql += " ORDER BY u.first_seen_at DESC, u.title COLLATE NOCASE LIMIT 5000"
        with self._read() as conn:
            rows = conn.execute(sql, args).fetchall()
        found = [_row_to_update(r) for r in rows]
        return found if include_hidden else [f for f in found if not f["hidden"]]

    def update_check_state(self):
        """{provider id: state} for every provider the update runner knows (see _LATE_TABLES)."""
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM update_checks").fetchall()
        out = {}
        for row in rows:
            st = dict(row)
            st["problems"] = _loads(st.pop("problems_json", None), [])
            # The Updates rework's columns, decoded (a database row made before them reads as "no apply tool")
            st["update_args"] = _loads(st.pop("update_args_json", None), {}) or {}
            st["apply_args"] = _loads(st.pop("apply_args_json", None), {}) or {}
            st["updates_only"] = bool(st.get("updates_only"))
            out[st["provider"]] = st
        return out

    def update_check_put(self, provider_id, **fields):
        """Set some columns of a provider's check state (created when missing).  Only the known columns."""
        pid = str(provider_id or "")
        bad = set(fields) - _CHECK_FIELDS
        if bad:
            raise ValueError("unknown update_checks column(s): %s" % ", ".join(sorted(bad)))
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO update_checks(provider) VALUES(?)", (pid,))
            for column, value in fields.items():
                conn.execute("UPDATE update_checks SET %s=? WHERE provider=?" % column, (value, pid))

    def update_check_reset_running(self):
        """The daemon has just started: no check of an earlier run is still going."""
        with self._write() as conn:
            conn.execute("UPDATE update_checks SET running_since=NULL")

    def update_check_claim(self, provider_id, *, now, limit_s, stale_s):
        """A person asks for a check now (the tool, the panel).  One transaction, so two asks can't both
        win.  Returns (status, seconds): "requested" (flag set), "checking" (one is running: seconds
        since it began) or "limited" (asked too recently: seconds until it is allowed)."""
        pid = str(provider_id or "")
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO update_checks(provider) VALUES(?)", (pid,))
            row = conn.execute("SELECT running_since, manual_at, requested_at FROM update_checks "
                               "WHERE provider=?", (pid,)).fetchone()
            running = row["running_since"]
            if running is not None and now - float(running) < stale_s:
                return "checking", now - float(running)
            if row["requested_at"] is not None:   # asked a moment ago; the runner clears it when it begins
                return "checking", 0.0
            if row["manual_at"] is not None and now - float(row["manual_at"]) < limit_s:
                return "limited", limit_s - (now - float(row["manual_at"]))
            conn.execute("UPDATE update_checks SET requested_at=?, manual_at=? WHERE provider=?", (now, now, pid))
            return "requested", 0.0

    # ------------------------------------------------------------------ applying updates (the Updates rework)

    def applying_info(self, provider=None):
        """{(provider, local_id): row} of every finding that was ever asked to apply (state, message, time, the
        `available` text it was for, batch, origin).  Rows outlive the finding (see _LATE_TABLES)."""
        sql, args = "SELECT * FROM update_applies", []
        if provider:
            sql += " WHERE provider=?"
            args.append(str(provider))
        with self._read() as conn:
            rows = conn.execute(sql, args).fetchall()
        return {(r["provider"], r["local_id"]): dict(r) for r in rows}

    def applying_queue(self, provider_id, local_id, *, batch, origin, now=None):
        """Ask for ONE finding to be applied: one transaction, so two asks (the panel and an auto-update step, say)
        can't both queue it.  Returns (status, row): 'queued', 'already_running' (it is queued or running now),
        'missing' (no such finding) or 'hidden' (the owner ignored this version: it is never applied)."""
        pid = str(provider_id or "")
        local = states.clean_text(local_id, 120)
        now = self.now() if now is None else now
        with self._write() as conn:
            found = conn.execute("SELECT available, dismissed_at, dismissed_version FROM updates "
                                 "WHERE provider=? AND local_id=?", (pid, local or "")).fetchone()
            if found is None:
                return "missing", None
            available = found["available"] or ""
            if found["dismissed_at"] is not None and (found["dismissed_version"] or "") == available:
                return "hidden", None
            row = conn.execute("SELECT state FROM update_applies WHERE provider=? AND local_id=?", (pid, local)).fetchone()
            if row is not None and row["state"] in ("queued", "running"):
                return "already_running", dict(row)
            conn.execute(
                "INSERT INTO update_applies(provider, local_id, available, state, message, at, batch, origin, queued_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(provider, local_id) DO UPDATE SET available=excluded.available, "
                "state=excluded.state, message=excluded.message, at=excluded.at, batch=excluded.batch, "
                "origin=excluded.origin, queued_at=excluded.queued_at",
                (pid, local, available, "queued", "", now, str(batch or ""), "auto" if origin == "auto" else "manual", now))
            return "queued", {"provider": pid, "local_id": local, "available": available, "state": "queued"}

    def applying_claim(self, now=None):
        """The daemon's ApplyRunner takes the OLDEST queued apply and marks it running (one transaction).  Returns
        {provider, local_id, available, batch, origin, title} or None.  A queued apply whose finding has gone (a check
        cleared it meanwhile) is dropped without being run."""
        now = self.now() if now is None else now
        with self._write() as conn:
            while True:
                row = conn.execute("SELECT * FROM update_applies WHERE state='queued' ORDER BY queued_at, rowid LIMIT 1").fetchone()
                if row is None:
                    return None
                found = conn.execute("SELECT title, available FROM updates WHERE provider=? AND local_id=?",
                                     (row["provider"], row["local_id"])).fetchone()
                if found is None:
                    conn.execute("DELETE FROM update_applies WHERE provider=? AND local_id=?", (row["provider"], row["local_id"]))
                    continue
                available = found["available"] or ""
                conn.execute("UPDATE update_applies SET state='running', at=?, message='', available=? "
                             "WHERE provider=? AND local_id=?", (now, available, row["provider"], row["local_id"]))
                out = dict(row)
                out["title"] = found["title"]
                out["available"] = available
                return out

    def applying_finish(self, provider_id, local_id, state, message, *, now=None):
        """Record how an apply ended (done / started / failed).  True when the row was there."""
        if state not in APPLY_STATES or state in ("queued", "running"):
            raise ValueError("an apply ends in done, started or failed")
        now = self.now() if now is None else now
        with self._write() as conn:
            return conn.execute("UPDATE update_applies SET state=?, message=?, at=? WHERE provider=? AND local_id=?",
                                (state, states.clean_text(message, 200) or "", now, str(provider_id or ""),
                                 str(local_id or ""))).rowcount > 0

    def applying_pending(self, batch):
        """How many applies of this batch are still queued or running."""
        with self._read() as conn:
            return conn.execute("SELECT COUNT(*) FROM update_applies WHERE batch=? AND state IN ('queued','running')",
                                (str(batch or ""),)).fetchone()[0]

    def applying_counts(self):
        """(queued, running) over every provider."""
        with self._read() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM update_applies WHERE state IN ('queued','running') "
                                "GROUP BY state").fetchall()
        got = {r["state"]: r["n"] for r in rows}
        return got.get("queued", 0), got.get("running", 0)

    def applying_recover(self, now=None):
        """The daemon has just started: nothing of an earlier run is still going.  A queued apply is forgotten (it never
        ran, so the finding can be asked again); one that was RUNNING is marked failed with a plain sentence, because
        nobody knows whether it finished (and it then is not retried by itself).  Returns (dropped, interrupted)."""
        now = self.now() if now is None else now
        with self._write() as conn:
            dropped = conn.execute("DELETE FROM update_applies WHERE state='queued'").rowcount
            interrupted = conn.execute(
                "UPDATE update_applies SET state='failed', at=?, message=? WHERE state='running'",
                (now, "the hub stopped while this was running; check whether it finished")).rowcount
            conn.execute("DELETE FROM update_applies WHERE state IN ('done','started','failed') AND at < ?",
                         (now - APPLY_KEEP_S,))
        return dropped, interrupted

    # ------------------------------------------------------------------ notices (notify.py)

    def claim_notice(self, item_id, kind, stamp=0.0):
        """Record that (item, kind, stamp) has been handled.  True the first time only, so two
        daemon passes can never announce the same finish twice."""
        with self._write() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO notices(item_id, kind, stamp, at) VALUES(?,?,?,?)",
                               (item_id, kind, float(stamp or 0), self.now()))
            return cur.rowcount > 0

    def unclaimed(self, kind, state_names, *, since, stamp_column):
        """Rows in one of `state_names` that have no `kind` notice for their current
        `stamp_column` value and that changed at or after `since` (so switching the feature on
        never announces the history).  `stamp_column`: finished_at, started_at or attempts
        (a retry count; `updated_at` is what `since` is compared with then)."""
        if stamp_column not in ("finished_at", "started_at", "attempts"):
            raise ValueError("bad stamp column")
        names = (state_names,) if isinstance(state_names, str) else tuple(state_names)
        when = "updated_at" if stamp_column == "attempts" else stamp_column
        sql = ("SELECT i.*, p.label AS provider_label, g.title AS group_title FROM items i "
               "LEFT JOIN providers p ON p.id = i.provider LEFT JOIN groups g ON g.id = i.group_id "
               "LEFT JOIN notices n ON n.item_id = i.id AND n.kind = ? AND n.stamp = COALESCE(i.%s, 0) "
               "WHERE i.state IN (%s) AND n.item_id IS NULL AND COALESCE(i.%s, 0) >= ? "
               "AND (p.id IS NULL OR p.enabled = 1) ORDER BY COALESCE(i.%s, 0), i.id LIMIT 200"
               % (stamp_column, ",".join("?" * len(names)), when, when))
        with self._read() as conn:
            rows = conn.execute(sql, (kind,) + names + (float(since),)).fetchall()
        return [row_to_item(r) for r in rows]

    def group_progress(self, group_id):
        """{total, final, failed, done, cancelled, last_finished} for the rows of a group
        (batch / season / playlist)."""
        with self._read() as conn:
            rows = conn.execute("SELECT state, finished_at FROM items WHERE group_id = ?", (group_id,)).fetchall()
            title = conn.execute("SELECT title FROM groups WHERE id = ?", (group_id,)).fetchone()
        out = {"total": len(rows), "final": 0, "failed": 0, "done": 0, "cancelled": 0, "last_finished": 0.0,
               "title": title["title"] if title else ""}
        for r in rows:
            if r["state"] in states.FINAL_STATES:
                out["final"] += 1
                out[r["state"]] += 1
                out["last_finished"] = max(out["last_finished"], r["finished_at"] or 0.0)
        return out

    def waiting_work(self):
        """How many rows the hub itself still has to run (queued, running, stalled), not counting
        paused ones: zero means \"the queue is empty\"."""
        marks = ",".join("?" * (len(states.ACTIVE_STATES) + 2))
        with self._read() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM items WHERE owner='hub' AND state IN (%s)" % marks,
                states.ACTIVE_STATES + ("queued", "stalled")).fetchone()[0]

    def running_hub(self):
        """How many hub downloads a worker is holding right now (what `concurrency.total` limits)."""
        marks = ",".join("?" * len(states.ACTIVE_STATES))
        with self._read() as conn:
            return conn.execute("SELECT COUNT(*) FROM items WHERE owner='hub' AND state IN (%s)" % marks,
                                states.ACTIVE_STATES).fetchone()[0]

    def list_scheduled(self):
        """Hub downloads waiting for a start time the owner set (paused, with `options.start_at`),
        soonest first, as [(item_id, start_epoch)].  A paused row that merely carries a retry
        time is not a scheduled one: the marker is the option, never the column alone."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT id, next_attempt_at, options_json FROM items WHERE owner='hub' AND state='paused' "
                "AND next_attempt_at IS NOT NULL ORDER BY next_attempt_at, id").fetchall()
        out = []
        for row in rows:
            if (_loads(row["options_json"], {}) or {}).get("start_at"):
                out.append((row["id"], float(row["next_attempt_at"])))
        return out

    def due_scheduled(self, now=None):
        """The scheduled downloads whose time has come (or passed while the daemon was down)."""
        now = self.now() if now is None else now
        return [item_id for item_id, when in self.list_scheduled() if when <= now]

    def forget_notices(self, item_ids):
        ids = [i for i in item_ids if i]
        if ids:
            with self._write() as conn:
                conn.execute("DELETE FROM notices WHERE item_id IN (%s)" % ",".join("?" * len(ids)), ids)

    # ------------------------------------------------------------------ diagnostics

    def schema_version(self):
        with self._read() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def quick_check(self):
        with self._read() as conn:
            return conn.execute("PRAGMA quick_check").fetchone()[0]

    def journal_mode(self):
        with self._read() as conn:
            return conn.execute("PRAGMA journal_mode").fetchone()[0]


def _inside(path, root):
    """Is `path` under `root`?  Resolved, so `..` and symlinks can't escape."""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False
