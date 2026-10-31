"""Split one websocket frame from the Showdown server into (room, [Msg]).

A frame is plain text. An optional first line `>ROOMID` names the room the rest belongs
to (no such line = the global lobby). Every following line is `|command|arg|arg...`.

Lines that carry a STRANGER'S text (chat, notices, html) are dropped here, before the
battle tracker or the model can see them: see UNTRUSTED TEXT in showdown/__init__.py.
"""

from collections import namedtuple

Msg = namedtuple("Msg", "cmd args")

# Commands whose payload is free text typed by a person (or html they sent).
DROPPED = frozenset({
    "c", "c:", "chat", "html", "uhtml", "uhtmlchange", "raw", "pm", "popup", "n", "name",
    "join", "j", "J", "leave", "l", "L", "formats", "b", "B", "battle", "queryresponse",
    "-message", "message", "inactive", "inactiveoff", "debug", "tournament", "card",
})

# Commands whose last argument is JSON, which may itself contain "|".
JSON_TAIL = frozenset({"request", "updatesearch", "updatechallenges", "updateuser_json"})


def parse_frame(text):
    """-> (room, [Msg]). `room` is "" for the global channel. Never raises."""
    room = ""
    msgs = []
    try:
        lines = str(text or "").split("\n")
        if lines and lines[0].startswith(">"):
            room = lines[0][1:].strip()
            lines = lines[1:]
        for line in lines:
            if not line.startswith("|"):
                continue  # plain-text continuation or blank: nothing we act on
            body = line[1:]
            cmd, _, rest = body.partition("|")
            if cmd in DROPPED:
                continue
            if cmd in JSON_TAIL:
                args = [rest]
            else:
                args = rest.split("|") if rest != "" else []
            msgs.append(Msg(cmd, args))
    except Exception:  # noqa: BLE001 - a malformed frame is skipped, never fatal
        return room, msgs
    return room, msgs


def parse_ident(ident):
    """'p2a: Some Name' -> ('p2', 'a', 'Some Name'); 'p1: Name' -> ('p1', '', 'Name')."""
    ident = str(ident or "")
    head, _, name = ident.partition(": ")
    player = head[:2] if head[:1] == "p" and head[1:2].isdigit() else ""
    pos = head[2:3] if len(head) > 2 else ""
    return player, pos, name


def parse_details(details):
    """'Garchomp, L78, M' -> ('Garchomp', 78). Level defaults to 100."""
    parts = [p.strip() for p in str(details or "").split(",")]
    species = parts[0] if parts else ""
    level = 100
    for p in parts[1:]:
        if p.startswith("L") and p[1:].isdigit():
            level = int(p[1:])
    return species, level


def parse_condition(condition):
    """'283/283 par' -> (283, 283, 'par'); '0 fnt' -> (0, None, 'fnt'); never raises."""
    text = str(condition or "").strip()
    hp_part, _, status = text.partition(" ")
    cur, _, mx = hp_part.partition("/")
    try:
        cur_i = int(cur)
    except ValueError:
        return None, None, status or None
    try:
        max_i = int(mx) if mx else None
    except ValueError:
        max_i = None
    return cur_i, max_i, status or None
