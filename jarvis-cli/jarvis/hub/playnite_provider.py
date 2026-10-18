"""The Playnite provider (hub plan 6.2): installs Jarvis started through `playnite_install_game`.

Playnite's bridge can START an install (`POST /api/games/{id}/install`) but exposes no
progress (plan 3.2, P4).  So the row is honest about that: an indeterminate "installing" bar,
no percent, no pretend numbers.  What can be known:

  * the install was requested (the tool tells us: `track_install`);
  * when it is over: `GET /api/games/{id}` says `isInstalled: true` (polled about every 10 s
    while a row is open);
  * that it never finished: after `TIMEOUT_S` (6 hours) the row becomes `unknown`, not failed.

A Steam-sourced game also shows up in the Steam provider with real numbers; the two rows are
separate (the Steam row is the one with a percent).  Installs started by hand in a non-Steam
store are invisible here (P4): only what Jarvis started is tracked.

Read-only: no capabilities, so no pause / cancel button.  Everything is written through the
store like any provider.  `track_install` never raises into the tool that calls it.
"""

import time

from . import providers, states

PROVIDER_ID = "playnite"
TIMEOUT_S = 6 * 3600
POLL_INTERVAL_S = 10.0


def _register(store):
    return store.register_provider(PROVIDER_ID, label="Playnite", mode="poll", kind="game", icon="P",
                                   capabilities=[], interval_s=POLL_INTERVAL_S, source_file="hub.playnite")


def track_install(game_id, name=None, store=None, now=None):
    """Make (or refresh) the row for an install that was just started.  True when it was written."""
    try:
        gid = states.clean_text(game_id, 100)
        if not gid:
            return False
        if store is None:
            from .store import Store
            store = Store()
        provider, _err = _register(store)
        if provider is None or not provider["enabled"]:
            return False
        fields = {"title": states.clean_text(name, 200) or "Playnite game %s" % gid[:8], "kind": "game",
                  "state": "downloading", "percent": None, "subtitle": "install",
                  "message": "Playnite gives no progress; this finishes when the game shows as installed",
                  "detail": [{"label": "Game id", "value": gid}]}
        row, _problems = store.upsert_provider_row(PROVIDER_ID, gid, fields, create=True, now=now)
        return row is not None
    except Exception:  # noqa: BLE001 - the install tool must never fail because of this
        return False


class PlayniteProvider(providers.BuiltinProvider):
    id = PROVIDER_ID

    def __init__(self):
        self.api = None            # tests: a fake bridge  api(method, path) -> dict

    def _api(self):
        if self.api is not None:
            return self.api
        from .. import playnite_http
        return lambda method, path: playnite_http.api(method, path)

    def registration(self, store):
        # Registered by track_install (the first time an install is started), not at start-up:
        # people without Playnite never get an empty provider.
        return None

    def idle(self, store):
        return not any(r["state"] not in states.FINAL_STATES and r["state"] != "unknown"
                       for r in store.list_items(provider=PROVIDER_ID, limit=20, include_disabled=True))

    def poll(self, store, now):
        rows = [r for r in store.list_items(provider=PROVIDER_ID, limit=200, include_disabled=True)
                if r["state"] not in states.FINAL_STATES and r["state"] != "unknown"]
        if not rows:
            return {"items": [], "complete": False}
        api, items, last_error = self._api(), [], None
        for row in rows:
            local = row["id"].split(":", 1)[1]
            if now - (row.get("created_at") or now) > TIMEOUT_S:
                items.append({"id": local, "title": row["title"], "state": "unknown",
                              "message": "no sign the install finished after %d hours" % (TIMEOUT_S // 3600)})
                continue
            game = api("GET", "/api/games/%s" % local)
            if not isinstance(game, dict) or game.get("error"):
                last_error = (game or {}).get("error") if isinstance(game, dict) else "no answer"
                continue                                  # Playnite not running: the row goes stale
            title = game.get("name") or row["title"]
            if game.get("isInstalled", game.get("installed")):
                items.append({"id": local, "title": title, "state": "done", "percent": 100, "message": None})
            else:
                items.append({"id": local, "title": title, "state": "downloading", "percent": None})
        if not items and last_error:
            return {"error": states.clean_text(last_error, 200)}
        return {"items": items, "complete": False}


PROVIDER = PlayniteProvider()
