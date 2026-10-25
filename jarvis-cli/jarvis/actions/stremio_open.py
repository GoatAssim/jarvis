"""Auto-discovered tool file: stremio_open (Download Hub plan, phase 7).

Opens a screen of stremio-web in the browser: a show or episode's stream list, search, the
library, discover, addons, settings.  The link is built here from an IMDb id and numbers (never
from text the model or an addon supplies) and only a link on the configured stremio-web address
is opened (`hub/stremio/player.open_in_browser`).  The Stremio server must be up for streams to
load, stremio-web for the page to exist; this tool says which is missing.
"""


def _fail(label, exc):
    return {"ok": False, "error": "%s failed: %s" % (label, exc)}


def tool_stremio_open(args):
    try:
        args = args or {}
        from ..hub.stremio import config, links, player, server
        screen = str(args.get("screen") or ("detail" if args.get("imdb_id") else "board")).strip().lower()
        kind = str(args.get("type") or "series").strip().lower()
        try:
            if screen == "detail":
                if not args.get("imdb_id"):
                    return {"needs_clarification": True, "message": "Which show or movie? I need its IMDb id (like tt0944947)."}
                season, episode = args.get("season"), args.get("episode")
                if kind == "series" and (season is None) != (episode is None):
                    return {"needs_clarification": True, "message": "Give both the season and the episode, or neither."}
                link = links.deep_link_detail(config.web_url(), args["imdb_id"], kind, season, episode)
            else:
                link = links.screen_link(config.web_url(), screen, args.get("query"))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        out = {"ok": True, "link": link}
        if args.get("open") is False:
            out["opened"] = False
            return out
        if not player.uses_app() and not server.port_answers(config.web_url()):
            out.update(ok=False, opened=False,
                       error="stremio-web isn't running at %s; start it with stack_start (what=web)" % config.web_url())
            return out
        opened, problem = player.open_link(link)
        out["opened"] = opened
        if not opened:
            out.update(ok=False, error=problem or ("the Stremio app didn't open" if player.uses_app() else "the browser didn't open"))
        up, _info = server.StremioServer().is_up()
        if not up:
            out["note"] = "Stremio's streaming server isn't running, so streams won't load until stack_start brings it up"
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail("stremio_open", exc)


TOOL_SCHEMAS = [
    {"name": "stremio_open",
     "description": ("Open a screen of stremio-web in the browser: a show or movie's page (with an episode: that episode's stream list), "
                     "search, the library, discover, addons or settings. A show needs its IMDb id (tt...). Use play_episode to actually start playing."),
     "parameters": {"type": "object", "properties": {
         "screen": {"type": "string", "enum": ["detail", "board", "discover", "library", "search", "addons", "settings", "calendar"],
                    "description": "Default: detail when imdb_id is given, otherwise the home board."},
         "imdb_id": {"type": "string", "description": "IMDb id like tt0944947 (screen=detail)."},
         "type": {"type": "string", "enum": ["series", "movie"], "description": "Default series."},
         "season": {"type": "integer"}, "episode": {"type": "integer"},
         "query": {"type": "string", "description": "Search text (screen=search)."},
         "open": {"type": "boolean", "description": "false = just return the link."}}}},
]

TOOLS = {"stremio_open": tool_stremio_open}
TOOL_GROUP = "stremio"
TOOL_KEYWORDS = {"stremio_open": {"open in stremio": 10, "open stremio": 9, "stremio library": 8, "stremio discover": 8, "open stremio web": 10}}

TEST_CHECKLIST = {
    "stremio_open": {
        "does": "Opens a stremio-web screen (a show / episode stream list, search, library ...) in the browser.",
        "steps": [
            {"run": {"imdb_id": "tt0944947", "season": 1, "episode": 1, "open": False},
             "expect": "ok: true with a link ending #/detail/series/tt0944947/tt0944947:1:1 and opened: false; nothing is opened and stremio-web does not need to be running."},
            {"run": {"screen": "search", "query": "sintel"}, "expect": "The browser opens stremio-web's search for 'sintel'."},
            {"run": {"imdb_id": "not-an-id"}, "expect": "ok: false, 'that isn't an IMDb id (expected something like tt0944947)'."},
            {"run": {"screen": "bogus"}, "expect": "ok: false, \"unknown screen 'bogus' (use one of: addons, board, calendar, discover, library, settings, search, detail)\"."},
            {"run": {"screen": "search", "query": "sintel", "open": False},
             "expect": "ok: true with a link ending #/search?search=sintel and opened: false; nothing is opened."},
        ],
        "needs": ["stremio-web running (stack_start what=web)"],
    },
}
