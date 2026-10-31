"""Tracks ONE battle from the Showdown protocol log plus the private `|request|`.

Singles only for now: a doubles request is recognised (`is_singles` is False) so the
engine can fall back to `/choose default` instead of guessing.

Pokemon are keyed by the SPECIES in the `details` field, never by the nickname: the
nickname is a stranger's text. A nickname is only used to look a Pokemon up again.
"""

import json

from .data import clean, to_id
from .protocol import parse_condition, parse_details, parse_ident

BOOST_NAMES = ("atk", "def", "spa", "spd", "spe", "accuracy", "evasion")
MAX_LOG = 60


class Mon:
    def __init__(self, species, level=100):
        self.species = clean(species, 40)
        self.level = level
        self.hp = None
        self.maxhp = None
        self.pct = 100.0
        self.status = None
        self.boosts = {}
        self.fainted = False
        self.active = False
        self.item = None
        self.ability = None
        self.moves = []            # ids: known moves (ours) or moves seen used (opponent's)
        self.stats = {}            # real stats, ours only (from the request)
        self.tera_type = None      # ours: the type we could terastallize into
        self.terastallized = None  # the type, once terastallized
        self.req_index = None      # 1-based position in the latest request (ours)

    def set_condition(self, condition):
        cur, mx, status = parse_condition(condition)
        if status == "fnt" or cur == 0:
            self.fainted, self.pct, self.hp, self.status = True, 0.0, 0, None
            return
        self.fainted = False
        if cur is not None:
            self.hp = cur
            if mx:
                self.maxhp = mx
                self.pct = max(0.0, min(100.0, cur * 100.0 / mx))
        self.status = status

    def brief(self):
        out = {"species": self.species, "level": self.level, "hp_pct": round(self.pct),
               "status": self.status, "fainted": self.fainted}
        if self.terastallized:
            out["tera"] = self.terastallized
        return out


class Side:
    def __init__(self, pid):
        self.pid = pid
        self.name = ""
        self.mons = {}     # species id -> Mon
        self.nicks = {}    # nickname -> species id
        self.active = None
        self.conditions = set()   # side conditions: stealthrock, spikes, ...
        self.teamsize = 6


class Battle:
    def __init__(self, room, my_name=""):
        self.room = room
        self.my_name = my_name
        self.my_player = None
        self.sides = {"p1": Side("p1"), "p2": Side("p2")}
        self.format_name = ""
        self.gametype = "singles"
        self.turn = 0
        self.weather = None
        self.terrain = None
        self.field = set()
        self.request = None
        self.rqid = None
        self.finished = False
        self.winner = None
        self.marker_seen = False   # a `turn` / `upkeep` line arrived since the last request
        self.log = []

    # -- sides ----------------------------------------------------------------------
    @property
    def me(self):
        return self.sides.get(self.my_player) if self.my_player else None

    @property
    def opp(self):
        if not self.my_player:
            return None
        return self.sides["p2" if self.my_player == "p1" else "p1"]

    @property
    def is_singles(self):
        if self.gametype != "singles":
            return False
        active = (self.request or {}).get("active")
        return not (isinstance(active, list) and len(active) > 1)

    def _note(self, text):
        self.log.append(clean(text, 120))
        del self.log[:-MAX_LOG]

    def _mon(self, ident):
        player, pos, nick = parse_ident(ident)
        side = self.sides.get(player)
        if side is None:
            return None
        key = side.nicks.get(nick)
        if key and key in side.mons:
            return side.mons[key]
        return side.active if pos else None

    def _get_or_add(self, side, species, level=100):
        key = to_id(species)
        mon = side.mons.get(key)
        if mon is None:
            mon = side.mons[key] = Mon(species, level)
        return mon

    # -- feeding messages -----------------------------------------------------------
    def apply(self, msg):
        handler = getattr(self, "_on_" + msg.cmd.lstrip("-").replace(":", "_"), None)
        if handler is None:
            return
        try:
            handler(msg.args)
        except Exception:  # noqa: BLE001 - one odd line must never break the whole battle
            pass

    def apply_all(self, msgs):
        for m in msgs:
            self.apply(m)

    def _on_player(self, a):
        if len(a) >= 2 and a[0] in self.sides:
            self.sides[a[0]].name = clean(a[1], 30)
            if self.my_name and to_id(a[1]) == to_id(self.my_name):
                self.my_player = a[0]

    def _on_gametype(self, a):
        self.gametype = to_id(a[0]) if a else "singles"

    def _on_tier(self, a):
        self.format_name = clean(a[0], 50) if a else ""

    def _on_teamsize(self, a):
        if len(a) >= 2 and a[0] in self.sides and a[1].isdigit():
            self.sides[a[0]].teamsize = int(a[1])

    def _on_poke(self, a):
        if len(a) >= 2 and a[0] in self.sides:
            species, level = parse_details(a[1])
            self._get_or_add(self.sides[a[0]], species, level)

    def _on_switch(self, a):
        player, _pos, nick = parse_ident(a[0])
        side = self.sides.get(player)
        if side is None or len(a) < 3:
            return
        species, level = parse_details(a[1])
        mon = self._get_or_add(side, species, level)
        mon.level = level
        mon.set_condition(a[2])
        if side.active is not None and side.active is not mon:
            side.active.active = False
            side.active.boosts = {}
        mon.active = True
        side.active = mon
        side.nicks[nick] = to_id(species)
        self._note("%s sent out %s" % (side.name or player, mon.species))

    _on_drag = _on_switch
    _on_replace = _on_switch

    def _on_detailschange(self, a):
        mon = self._mon(a[0])
        if mon and len(a) >= 2:
            species, level = parse_details(a[1])
            mon.species, mon.level = clean(species, 40), level

    _on_formechange = _on_detailschange

    def _on_move(self, a):
        mon = self._mon(a[0])
        if mon and len(a) >= 2:
            mid = to_id(a[1])
            if mid and mid not in mon.moves and len(mon.moves) < 4:
                mon.moves.append(mid)
            self._note("%s used %s" % (mon.species, clean(a[1], 30)))

    def _on_damage(self, a):
        mon = self._mon(a[0])
        if mon and len(a) >= 2:
            mon.set_condition(a[1])

    _on_heal = _on_damage
    _on_sethp = _on_damage

    def _on_faint(self, a):
        mon = self._mon(a[0])
        if mon:
            mon.fainted, mon.pct, mon.hp, mon.status = True, 0.0, 0, None
            self._note("%s fainted" % mon.species)

    def _on_status(self, a):
        mon = self._mon(a[0])
        if mon and len(a) >= 2:
            mon.status = clean(a[1], 5) or None

    def _on_curestatus(self, a):
        mon = self._mon(a[0])
        if mon:
            mon.status = None

    def _on_cureteam(self, a):
        player, _pos, _nick = parse_ident(a[0])
        side = self.sides.get(player)
        if side:
            for mon in side.mons.values():
                mon.status = None

    def _set_boost(self, a, sign):
        mon = self._mon(a[0])
        if mon and len(a) >= 3 and a[1] in BOOST_NAMES and a[2].lstrip("-").isdigit():
            cur = mon.boosts.get(a[1], 0) + sign * int(a[2])
            mon.boosts[a[1]] = max(-6, min(6, cur))

    def _on_boost(self, a):
        self._set_boost(a, 1)

    def _on_unboost(self, a):
        self._set_boost(a, -1)

    def _on_setboost(self, a):
        mon = self._mon(a[0])
        if mon and len(a) >= 3 and a[1] in BOOST_NAMES and a[2].lstrip("-").isdigit():
            mon.boosts[a[1]] = max(-6, min(6, int(a[2])))

    def _on_clearboost(self, a):
        mon = self._mon(a[0])
        if mon:
            mon.boosts = {}

    def _on_clearallboost(self, a):
        for side in self.sides.values():
            for mon in side.mons.values():
                mon.boosts = {}

    def _on_clearnegativeboost(self, a):
        mon = self._mon(a[0])
        if mon:
            mon.boosts = {k: v for k, v in mon.boosts.items() if v > 0}

    def _on_item(self, a):
        mon = self._mon(a[0])
        if mon and len(a) >= 2:
            mon.item = to_id(a[1])

    def _on_enditem(self, a):
        mon = self._mon(a[0])
        if mon:
            mon.item = None

    def _on_ability(self, a):
        mon = self._mon(a[0])
        if mon and len(a) >= 2:
            mon.ability = to_id(a[1])

    def _on_terastallize(self, a):
        mon = self._mon(a[0])
        if mon and len(a) >= 2:
            mon.terastallized = clean(a[1], 12)
            self._note("%s terastallized" % mon.species)

    def _on_weather(self, a):
        w = to_id(a[0]) if a else ""
        self.weather = None if w in ("", "none") else w

    def _on_fieldstart(self, a):
        name = to_id(str(a[0]).replace("move:", "")) if a else ""
        if name.endswith("terrain"):
            self.terrain = name
        elif name:
            self.field.add(name)

    def _on_fieldend(self, a):
        name = to_id(str(a[0]).replace("move:", "")) if a else ""
        if name.endswith("terrain"):
            self.terrain = None
        else:
            self.field.discard(name)

    def _on_sidestart(self, a):
        player, _pos, _n = parse_ident(a[0]) if a else ("", "", "")
        side = self.sides.get(player)
        if side and len(a) >= 2:
            side.conditions.add(to_id(str(a[1]).replace("move:", "")))

    def _on_sideend(self, a):
        player, _pos, _n = parse_ident(a[0]) if a else ("", "", "")
        side = self.sides.get(player)
        if side and len(a) >= 2:
            side.conditions.discard(to_id(str(a[1]).replace("move:", "")))

    def _on_turn(self, a):
        if a and a[0].isdigit():
            self.turn = int(a[0])
        self.marker_seen = True

    def _on_upkeep(self, a):
        self.marker_seen = True

    def _on_win(self, a):
        self.finished = True
        self.winner = clean(a[0], 30) if a else None
        self._note("%s won" % (self.winner or "nobody"))

    def _on_tie(self, a):
        self.finished = True
        self.winner = None
        self._note("tie")

    # -- the private request --------------------------------------------------------
    def set_request(self, raw):
        """Parse a `|request|` payload (JSON text). Returns the dict or None."""
        try:
            req = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return None
        if not isinstance(req, dict):
            return None
        self.request = req
        self.rqid = req.get("rqid")
        self.marker_seen = False
        side_info = req.get("side")
        if isinstance(side_info, dict):
            pid = side_info.get("id")
            if pid in self.sides:
                self.my_player = pid
                self.sides[pid].name = clean(side_info.get("name"), 30) or self.sides[pid].name
                self._read_my_team(self.sides[pid], side_info.get("pokemon") or [])
        return req

    def _read_my_team(self, side, pokemon):
        for idx, p in enumerate(pokemon, start=1):
            if not isinstance(p, dict):
                continue
            species, level = parse_details(p.get("details"))
            mon = self._get_or_add(side, species, level)
            mon.level = level
            mon.set_condition(p.get("condition"))
            mon.req_index = idx
            mon.active = bool(p.get("active"))
            if mon.active:
                side.active = mon
            stats = p.get("stats")
            if isinstance(stats, dict):
                mon.stats = {k: int(v) for k, v in stats.items() if isinstance(v, (int, float))}
            if mon.maxhp:
                mon.stats["hp"] = mon.maxhp
            moves = p.get("moves")
            if isinstance(moves, list):
                mon.moves = [to_id(m) for m in moves][:4]
            mon.item = to_id(p.get("item")) or None
            mon.ability = to_id(p.get("ability") or p.get("baseAbility")) or None
            mon.tera_type = clean(p.get("teraType"), 12) or None
            term = p.get("terastallized")
            mon.terastallized = clean(term, 12) or mon.terastallized if term else mon.terastallized

    # -- views ----------------------------------------------------------------------
    def summary(self):
        me, opp = self.me, self.opp
        def side_view(s):
            if s is None:
                return None
            return {"name": s.name, "active": s.active.brief() if s.active else None,
                    "bench": [m.brief() for m in s.mons.values() if m is not s.active],
                    "conditions": sorted(s.conditions)}
        return {"room": self.room, "format": self.format_name, "turn": self.turn,
                "finished": self.finished, "winner": self.winner, "weather": self.weather,
                "terrain": self.terrain, "field": sorted(self.field),
                "me": side_view(me), "opponent": side_view(opp),
                "waiting_for_choice": bool(self.request and not self.request.get("wait")
                                           and not self.finished),
                "recent": list(self.log[-8:])}
