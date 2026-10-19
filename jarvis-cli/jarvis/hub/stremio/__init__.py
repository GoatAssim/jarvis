"""The Stremio half of the Download Hub (plan phases 4 and 7).

    config.py   ~/.jarvis/stremio.json: where the streaming server and stremio-web live
                (loopback only), which player to use.  Secrets stay in that file.
    links.py    pure: magnets, infohashes, Stremio stream objects, the server's URLs, picking
                the right file of a pack, deep links into stremio-web, `encode_stream`.
    server.py   the streaming server's HTTP API (settings, create, stats) and "is it up?".
    player.py   mpv / VLC: finding them and launching one on a local file or the server URL.

The Stremio engine itself is `hub/engines/stremio.py` (engines are discovered by file); the
model-facing tools are `actions/stremio_stack.py` (stack_*, stremio_prepare, server_cache*),
`actions/stremio_open.py` and `actions/play_media.py`.

Import-light on purpose: `requests` is imported inside the functions that need it, so a
`jarvis` command that never touches Stremio pays nothing.
"""
