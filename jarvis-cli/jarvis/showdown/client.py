"""The Showdown connection: log in, accept challenges from the owner, follow battle
rooms, and (in auto mode) answer every request through the Engine.

Everything that touches the outside world is injectable so the whole thing can be
driven by a fake transport in tests: `transport_factory(url) -> Transport`,
`http_get(url)` / `http_post(url, data)` for the login server.

A Transport has `send(text)`, `recv() -> str | "" | None` ("" = nothing yet, None =
closed) and `close()`.

TIMING: the server sends `|request|` BEFORE the log lines that end the turn, so deciding
the moment a request arrives would use stale state. The client waits for a `turn` /
`upkeep` line after the request, or ACT_DELAY seconds, whichever comes first.

UNVERIFIED against a live server from the build sandbox (no internet there): the login
endpoint, the `/trn` form and `/choose` without a request id follow the public protocol
as I know it. tests/test_showdown.py pins the behaviour against a fake server;
`jarvis showdown` on a real one is the real test.
"""

import json
import threading
import time
from urllib.parse import quote

from . import config as cfgmod
from .battle import Battle
from .data import clean, to_id
from .protocol import parse_frame

AUTH_URL = "https://play.pokemonshowdown.com/action.php"
ACT_DELAY = 0.7
RECV_TICK = 5
LOGIN_TIMEOUT = 20
MAX_RETRIES_PER_REQUEST = 3
MAX_EVENTS = 100
MAX_RESULTS = 20


class WebSocketTransport:
    """websocket-client (`pip install websocket-client`), imported only when used."""

    def __init__(self, url):
        import websocket
        self._mod = websocket
        self.ws = websocket.create_connection(url, timeout=RECV_TICK)

    def send(self, text):
        self.ws.send(text)

    def recv(self):
        try:
            return self.ws.recv()
        except self._mod.WebSocketTimeoutException:
            return ""
        except Exception:  # noqa: BLE001 - closed or broken: the caller reconnects
            return None

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def _default_get(url):
    import requests
    return requests.get(url, timeout=15).text


def _default_post(url, data):
    import requests
    return requests.post(url, data=data, timeout=15).text


class ShowdownClient:
    def __init__(self, config, engine, transport_factory=None, http_get=None, http_post=None,
                 act_delay=ACT_DELAY):
        self.cfg = dict(config)
        self.engine = engine
        self.transport_factory = transport_factory or WebSocketTransport
        self.http_get = http_get or _default_get
        self.http_post = http_post or _default_post
        self.act_delay = act_delay

        self.lock = threading.RLock()
        self.transport = None
        self.state = "stopped"        # stopped | connecting | logging_in | ready | reconnecting
        self.username = self.cfg.get("username", "")
        self.mode = self.cfg.get("mode", "auto")
        self.battles = {}
        self.suggestions = {}         # room -> Decision (manual mode)
        self.results = []
        self.events = []
        self.on_event = None
        self._pending = {}            # room -> rqid waiting for a decision
        self._decided = {}            # room -> rqid already answered
        self._sent = {}               # room -> {"rqid", "tried": [...]}
        self._finished = set()
        self._stop = threading.Event()
        self._thread = None
        self._login_started = None

    # ================================================================== lifecycle
    def start(self):
        with self.lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="showdown-client", daemon=True)
            self._thread.start()
            return True

    def stop(self):
        self._stop.set()
        with self.lock:
            t = self.transport
        if t is not None:
            t.close()
        self.state = "stopped"

    def _run(self):
        backoff = 2
        while not self._stop.is_set():
            self.state = "connecting"
            try:
                url = cfgmod.server_url(self.cfg)
                self.transport = self.transport_factory(url)
            except Exception as e:  # noqa: BLE001
                self._event("could not connect: %s" % clean(e, 100))
                self.state = "reconnecting"
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30)
                continue
            backoff = 2
            self.state = "logging_in"
            self._event("connected")
            self._read_loop()
            if self._stop.is_set():
                break
            self.state = "reconnecting"
            self._event("connection lost, reconnecting")
            self._stop.wait(2)
        self.state = "stopped"

    def _read_loop(self):
        t = self.transport
        while not self._stop.is_set():
            raw = t.recv()
            if raw is None:
                return
            if raw == "":
                self._tick()
                continue
            room, msgs = parse_frame(raw)
            with self.lock:
                try:
                    if room.startswith("battle-"):
                        self._handle_battle(room, msgs)
                    else:
                        self._handle_global(msgs)
                except Exception as e:  # noqa: BLE001 - one bad frame never ends the session
                    self._event("frame error: %s" % clean(e, 100))

    def _tick(self):
        if self.state == "logging_in" and self._login_started and \
                time.time() - self._login_started > LOGIN_TIMEOUT:
            self._login_started = None
            self._event("login did not complete: check username / password in showdown.json")

    # ================================================================== sending
    def _send(self, text):
        t = self.transport
        if t is None:
            return False
        try:
            t.send(text)
            return True
        except Exception as e:  # noqa: BLE001
            self._event("send failed: %s" % clean(e, 80))
            return False

    def _send_global(self, command):
        return self._send("|" + command)

    def _send_room(self, room, command):
        return self._send("%s|%s" % (room, command))

    def _event(self, text):
        line = clean(text, 160)
        with self.lock:
            self.events.append(line)
            del self.events[:-MAX_EVENTS]
        cb = self.on_event
        if cb:
            try:
                cb(line)
            except Exception:  # noqa: BLE001
                pass

    # ================================================================== lobby
    def _handle_global(self, msgs):
        for m in msgs:
            if m.cmd == "challstr":
                self._login("|".join(m.args))
            elif m.cmd == "updateuser" and len(m.args) >= 2:
                if m.args[1] == "1" and to_id(m.args[0]) == to_id(self.username):
                    if self.state != "ready":
                        self.state = "ready"
                        self._login_started = None
                        self._event("logged in as %s" % clean(m.args[0], 20))
            elif m.cmd == "updatechallenges":
                self._on_challenges(m.args[0] if m.args else "")

    def _login(self, challstr):
        name = self.username
        if not name:
            self._event("no username set in showdown.json")
            return
        self._login_started = time.time()
        try:
            if self.cfg.get("auth") == "none":
                self._send_global("/trn %s,0," % name)
                return
            pw = self.cfg.get("password")
            if pw:
                text = self.http_post(AUTH_URL, {"act": "login", "name": name, "pass": pw,
                                                 "challstr": challstr})
                body = text[1:] if text.startswith("]") else text
                data = json.loads(body)
                assertion = data.get("assertion") if isinstance(data, dict) else None
                if not assertion or str(assertion).startswith(";;"):
                    self._event("the login server refused the name or password")
                    return
            else:
                assertion = self.http_get("%s?act=getassertion&userid=%s&challstr=%s"
                                          % (AUTH_URL, quote(to_id(name)), quote(challstr)))
                if not assertion or str(assertion).startswith(";"):
                    self._event("that name is registered: add its password to showdown.json")
                    return
            self._send_global("/trn %s,0,%s" % (name, assertion))
        except Exception as e:  # noqa: BLE001
            self._event("login failed: %s" % clean(e, 100))

    def _allowed(self, user):
        allowed = {to_id(u) for u in self.cfg.get("allowed_users", [])}
        return to_id(user) in allowed

    def _on_challenges(self, raw):
        try:
            data = json.loads(raw)
        except ValueError:
            return
        incoming = data.get("challengesFrom") if isinstance(data, dict) else None
        if not isinstance(incoming, dict):
            return
        for user, fmt in incoming.items():
            if not self.cfg.get("auto_accept", True) or not self._allowed(user):
                self._send_global("/reject %s" % user)
                self._event("rejected a challenge from %s" % clean(user, 20))
                continue
            if not cfgmod.valid_format(to_id(fmt)):
                self._send_global("/reject %s" % user)
                continue
            if "random" in to_id(fmt):
                self._send_global("/utm null")
            self._send_global("/accept %s" % user)
            self._event("accepted a %s challenge from %s" % (clean(fmt, 30), clean(user, 20)))

    # ---- actions the worker exposes -------------------------------------------------
    def challenge(self, user, fmt=None):
        fmt = str(fmt or self.cfg.get("default_format") or "").strip().lower()
        if not cfgmod.valid_name(user):
            return {"ok": False, "error": "that is not a valid Showdown name"}
        if not cfgmod.valid_format(fmt):
            return {"ok": False, "error": "that is not a valid format id (e.g. gen9randombattle)"}
        if self.state != "ready":
            return {"ok": False, "error": "not logged in yet (state: %s)" % self.state}
        if "random" in fmt:
            self._send_global("/utm null")
        self._send_global("/challenge %s, %s" % (user, fmt))
        return {"ok": True, "challenged": user, "format": fmt}

    def ladder(self, fmt=None, enabled=True):
        fmt = str(fmt or self.cfg.get("default_format") or "").strip().lower()
        if self.state != "ready":
            return {"ok": False, "error": "not logged in yet (state: %s)" % self.state}
        if not cfgmod.valid_format(fmt):
            return {"ok": False, "error": "that is not a valid format id"}
        if enabled:
            if "random" in fmt:
                self._send_global("/utm null")
            self._send_global("/search %s" % fmt)
            return {"ok": True, "searching": fmt}
        self._send_global("/cancelsearch")
        return {"ok": True, "searching": None}

    def set_mode(self, mode):
        if mode not in ("auto", "manual"):
            return {"ok": False, "error": "mode must be auto or manual"}
        with self.lock:
            self.mode = mode
            if mode == "auto":
                for room in list(self.battles):
                    self._maybe_act(room)
        return {"ok": True, "mode": mode}

    def _room_for(self, room=None):
        with self.lock:
            if room:
                return room if room in self.battles else None
            live = [r for r, b in self.battles.items() if not b.finished]
            return live[-1] if live else None

    def suggest(self, room=None):
        with self.lock:
            room = self._room_for(room)
            if room is None:
                return {"ok": False, "error": "no battle in progress"}
            battle = self.battles[room]
            if not battle.request or battle.request.get("wait") or battle.finished:
                return {"ok": False, "error": "it is not your turn to choose"}
            decision = self.engine.decide(battle, use_model=bool(self.cfg.get("use_model", True)))
            self.suggestions[room] = decision
            return {"ok": True, "room": room, "decision": decision.as_dict()}

    def choose(self, room=None, option=None, choice=None):
        """Send a choice for the pending request. `option` is a candidate id from
        showdown_suggest ("A"); `choice` is a choice string ("move 2"). Only choices the
        engine itself lists as legal are ever sent."""
        with self.lock:
            room = self._room_for(room)
            if room is None:
                return {"ok": False, "error": "no battle in progress"}
            battle = self.battles[room]
            if not battle.request or battle.request.get("wait") or battle.finished:
                return {"ok": False, "error": "it is not your turn to choose"}
            legal = self.engine._rank(self.engine.candidates(battle))
            wanted = None
            for c in legal:
                if (option and c.id == str(option).strip().upper()) or \
                        (choice and c.choice == str(choice).strip().lower()):
                    wanted = c
            if wanted is None:
                return {"ok": False, "error": "not a legal choice right now",
                        "legal": [c.as_dict() for c in legal]}
            self._send_choice(room, battle, wanted.choice)
            return {"ok": True, "sent": wanted.choice, "room": room}

    def forfeit(self, room=None):
        with self.lock:
            room = self._room_for(room)
            if room is None:
                return {"ok": False, "error": "no battle in progress"}
            self._send_room(room, "/forfeit")
            return {"ok": True, "room": room}

    # ================================================================== battles
    def _handle_battle(self, room, msgs):
        battle = self.battles.get(room)
        if battle is None:
            battle = self.battles[room] = Battle(room, my_name=self.username)
            self._event("battle started: %s" % clean(room, 50))
        for m in msgs:
            if m.cmd == "request":
                if battle.set_request(m.args[0] if m.args else ""):
                    self._pending[room] = battle.rqid
            elif m.cmd == "error":
                self._on_error(room, battle, m.args)
            elif m.cmd == "deinit":
                self.battles.pop(room, None)
                return
            else:
                battle.apply(m)
        self._maybe_act(room)

    def _maybe_act(self, room):
        battle = self.battles.get(room)
        if battle is None:
            return
        if battle.finished:
            self._finish(room, battle)
            return
        rqid = self._pending.get(room)
        req = battle.request
        if rqid is None or not req:
            return
        if req.get("wait"):
            self._pending.pop(room, None)
            return
        if self._decided.get(room) == rqid:
            return
        if battle.marker_seen or self.act_delay <= 0:
            self._begin_act(room, rqid)
        else:
            timer = threading.Timer(self.act_delay, self._timer_act, (room, rqid))
            timer.daemon = True
            timer.start()

    def _timer_act(self, room, rqid):
        with self.lock:
            if self._pending.get(room) == rqid and self._decided.get(room) != rqid:
                self._begin_act(room, rqid)

    def _begin_act(self, room, rqid):
        self._decided[room] = rqid
        threading.Thread(target=self._act, args=(room, rqid), daemon=True).start()

    def _act(self, room, rqid, exclude=()):
        with self.lock:
            battle = self.battles.get(room)
            if battle is None or battle.rqid != rqid or battle.finished:
                return
            use_model = bool(self.cfg.get("use_model", True)) and self.mode == "auto"
        decision = self.engine.decide(battle, use_model=use_model, exclude=exclude)  # may take a while
        with self.lock:
            battle = self.battles.get(room)
            if battle is None or battle.rqid != rqid or battle.finished:
                return
            self.suggestions[room] = decision
            if self.mode != "auto":
                self._event("waiting for you (suggestion %s)" % clean(decision.choice, 30))
                return
            self._send_choice(room, battle, decision.choice)
            self._event("turn %d: %s (%s)" % (battle.turn, clean(decision.choice, 30), decision.source))

    def _send_choice(self, room, battle, choice):
        sent = self._sent.get(room)
        if not sent or sent.get("rqid") != battle.rqid:
            sent = self._sent[room] = {"rqid": battle.rqid, "tried": []}
        sent["tried"].append(choice)
        self._send_room(room, "/choose %s" % choice)

    def _on_error(self, room, battle, args):
        """The server refused a choice. Try the next-best candidate, then `default`."""
        sent = self._sent.get(room)
        text = clean(" ".join(args), 100)
        if not sent or sent.get("rqid") != battle.rqid or self.mode != "auto":
            self._event("server: %s" % text)
            return
        tried = sent["tried"]
        self._event("choice %s refused (%s)" % (clean(tried[-1], 20), text))
        if "default" in tried:
            return
        rqid = battle.rqid
        if len(tried) >= MAX_RETRIES_PER_REQUEST:
            self._send_choice(room, battle, "default")
            return
        self._decided[room] = rqid
        threading.Thread(target=self._act, args=(room, rqid, tuple(tried)), daemon=True).start()

    def _finish(self, room, battle):
        if room in self._finished:
            return
        self._finished.add(room)
        result = {"room": room, "winner": battle.winner, "turns": battle.turn,
                  "format": battle.format_name, "won": bool(battle.winner and battle.me
                                                            and to_id(battle.winner) == to_id(battle.me.name)),
                  "at": int(time.time())}
        self.results.append(result)
        del self.results[:-MAX_RESULTS]
        self._event("battle over: %s won (turn %d)" % (clean(battle.winner or "nobody", 20), battle.turn))
        self._send_room(room, "/leave")

    # ================================================================== status
    def status(self):
        with self.lock:
            live = [b.summary() for b in self.battles.values() if not b.finished]
            return {"state": self.state, "username": self.username, "mode": self.mode,
                    "server": cfgmod.server_url(self.cfg), "format": self.cfg.get("default_format"),
                    "allowed_users": [clean(u, 20) for u in self.cfg.get("allowed_users", [])],
                    "use_model": bool(self.cfg.get("use_model", True)),
                    "battles": live, "results": list(self.results[-5:]), "events": list(self.events[-10:])}
