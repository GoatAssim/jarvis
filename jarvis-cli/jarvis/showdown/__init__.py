"""Pokemon Showdown support for Jarvis.

LAYOUT
------
    data.py      type chart + the cached moves/pokedex JSON (fetched at runtime, never bundled)
    calc.py      stat and damage estimates (Gen 9 formula, Random Battle assumptions)
    protocol.py  splits a websocket frame into (room, [Msg]); drops chat and markup
    battle.py    BattleState: tracks one battle from the protocol log and `|request|`
    engine.py    candidate choices + heuristic score + optional model pick + fallback
    client.py    the connection: login, challenges, battle rooms, sending choices
    worker.py    the daemon: owns the client, exposes it on a loopback socket

WHY A DAEMON
------------
Every `jarvis ask` is its own short-lived process and a single ask may make at most
MAX_TOOL_ROUNDS tool calls, so a battle (many turns, minutes long) cannot live inside
one. The worker owns the websocket and the battle loop; the tools in
actions/showdown_tools.py only talk to it, the same way browser_tools talks to
browser_daemon.

UNTRUSTED TEXT
--------------
Everything the server sends about the OPPONENT (names, nicknames, chat) is a stranger's
text. protocol.py drops chat and markup lines before anything else sees them, battle.py
keys Pokemon by the species in `details` (never the nickname) and engine.py cleans every
name it puts in a prompt. The model never receives chat.
"""
