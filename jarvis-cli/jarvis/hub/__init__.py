"""The Download Hub (JARVIS_DOWNLOAD_HUB.md, phases 1-2).

One store, one daemon and one Menu -> Downloads panel for every download Jarvis
knows about: jobs the hub runs itself (engines) and jobs somebody else runs that the
hub only mirrors (providers).  Phases 1-2 ship the core, the HTTP engine, the panel and
a demo provider; yt-dlp (3), Stremio (4), Steam / Playnite / ``TOOL_DOWNLOADS`` (5) and
everything after plug in WITHOUT editing the files below -- see "Where phase N plugs in".

Module map (all import-light: nothing here imports ai_client, tool_router,
tool_registry or tools, so an action file can import this package at module level)
---------------------------------------------------------------------------------
    paths.py        ~/.jarvis/hub/... resolved at CALL time (tests redirect HOME)
    states.py       state vocabulary, the transition table, progress sanitising and
                    smoothing.  Pure: no I/O.  THE place that defines what a row may say.
    store.py        the sqlite store (WAL, busy timeout): items, groups, providers,
                    events, settings.  Every write path (engine, provider, tool, daemon)
                    goes through it, so every row is sanitised the same way.
    settings.py     defaults + validation for the hub settings (dl_settings, the panel)
    netguard.py     the SSRF guard (loopback / private / link-local refused, allow-list)
    naming.py       filename + destination safety (nothing is written outside library_root)
    router.py       "what engine takes this input?" -> {engine, reason, ...}
    engines/        one module per engine; discovered, not listed (see below)
    service.py      what the tools, the panel and a future CLI call (dicts in, dicts out)
    daemon.py       the long-lived process: scheduler, workers, retries, housekeeping
    demo.py         the fake-progress demo provider (a pure function of elapsed time)
    doctor.py       hub_doctor's checks
    ../hub_client.py  the small client a tool / provider process uses to REPORT progress

Who writes what (plan section 4.2)
----------------------------------
The daemon owns hub-engine rows (``owner = 'hub'``) and all housekeeping.  A provider
writes only rows whose ``provider`` is its own id (``owner = 'provider'``); its row id is
``<provider>:<local id>`` and provider ids contain no ":", so two providers can never
land on the same row.  Tool calls (dl_add / dl_pause ...) enqueue and request; they never
run a transfer, because every `jarvis` command is a short-lived process.

Control requests: a tool process cannot stop a worker thread living in the daemon, so
pause / cancel are written to ``items.control`` and the daemon's next tick applies them.
When no worker holds the row (queued, paused, failed, or the daemon is down) the request
is applied directly instead.

Where phase N plugs in
----------------------
* Phase 3 (yt-dlp) / Phase 4 (Stremio): add ``engines/<name>.py`` exporting ``ENGINE``
  (a subclass of ``engines.base.Engine``) and, optionally, ``ROUTE_RULES`` (a list of
  ``router.RouteRule``).  ``engines/__init__.py`` imports every non-underscore module in
  its folder; a module that fails to import is recorded in ``engines.LOAD_ERRORS`` (shown
  by hub_doctor) and never takes the hub down.  An engine only ever returns an
  ``Outcome``; it never writes states itself, so retry / stall / restart rules live in one
  place (daemon.py).  The Stremio engine can delegate the byte transfer to
  ``engines.http.stream_to_file``.
* Phase 5 (providers): ``hub_client.register_provider`` already exists; ``TOOL_DOWNLOADS``
  becomes a loader attribute calling it.  Provider controls (pause / cancel of a row a
  provider owns) are NOT wired yet: service.control_provider_row() is the one place to
  add them.  Poll mode needs a ``poll_tool`` runner next to ``daemon.Housekeeping``.
* Phase 6+: new tools go in new tool files (``stremio_suite.py`` ...) that call
  ``service.add_items`` -- the group and the idempotency key are arguments already.
* Phase 8: settings keys already exist for concurrency, rate limit, schedule, retention;
  ``settings.py`` is the one validator.

Deliberate differences from the plan text (so the next agent doesn't "fix" them)
--------------------------------------------------------------------------------
* The code lives in the repo (``jarvis/hub``, ``actions/download_hub.py``) instead of
  ``~/.jarvis/tools``: it ships as patches like everything else.  Its panel files sit in
  ``actions/hub_ui`` and are declared as package data in pyproject.toml, because a real
  ``pip install .`` (script.bat) copies nothing that isn't Python or declared.
* Row field names follow the provider row shape (``bytes_done`` / ``bytes_total``), not the
  sketch's ``progress_bytes`` / ``total_bytes``.
* ``dl_add``'s ``dest`` is a SUB-FOLDER of the library root, never an absolute path
  (section 14: force every write under library_root).
"""

SCHEMA_VERSION = 1
HUB_VERSION = "1.0-phase2"
