"""The Showdown worker daemon (what DAEMONS in actions/showdown_tools.py launches).

Owns ONE ShowdownClient and serves the loopback socket described in rpc.py. The tools
never touch Showdown themselves; they send an op here and relay the answer.

OPS: ping, status, connect, disconnect, challenge, ladder, set_mode, suggest, choose,
     forfeit, fetch_data, stop.

Run by hand with:  python -m jarvis.showdown.worker
"""

import json
import os
import signal
import socketserver
import sys
import threading
import time

from . import config as cfgmod
from . import rpc
from .client import ShowdownClient
from .data import DataStore
from .engine import Engine

ENCODING = "utf-8"

_stop = threading.Event()
_state = {"client": None, "data": DataStore(), "engine": None}
_lock = threading.Lock()


def _model_call(prompt):
    """One cheap, history-free model call returning a JSON object (or None)."""
    try:
        from ..channels import cheap_call
        return cheap_call.ask_json(prompt, max_chars=6000)
    except Exception:  # noqa: BLE001 - no provider / no key: the engine falls back
        return None


def _load_data_async():
    store = _state["data"]
    threading.Thread(target=store.ensure, name="showdown-data", daemon=True).start()


# --------------------------------------------------------------------------- ops
def op_ping(args):
    return {"ok": True, "pong": True}


def op_status(args):
    client = _state["client"]
    out = {"ok": True, "connected": client is not None,
           "data_ready": _state["data"].ready, "configured": bool(cfgmod.load_config().get("username"))}
    if client is not None:
        out.update(client.status())
    return out


def op_connect(args):
    cfg = cfgmod.load_config()
    if not cfg.get("username"):
        cfgmod.ensure_config()
        return {"ok": False, "error": "no bot username yet: set \"username\" (and your own account "
                                      "under \"allowed_users\") in %s" % cfgmod.CONFIG_FILE}
    if not cfg.get("allowed_users"):
        return {"ok": False, "error": "\"allowed_users\" in %s is empty, so the bot would reject "
                                      "every challenge. Put your own Showdown name there." % cfgmod.CONFIG_FILE}
    if args.get("server"):
        cfg["server"] = str(args["server"])
    if args.get("format"):
        cfg["default_format"] = str(args["format"])
    try:
        import websocket  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "the websocket-client package is missing: "
                                      "pip install websocket-client  (or: pip install .[showdown])"}
    with _lock:
        old = _state["client"]
        if old is not None:
            old.stop()
        _load_data_async()
        engine = Engine(_state["data"], model_call=_model_call if cfg.get("use_model", True) else None)
        client = ShowdownClient(cfg, engine)
        _state["client"], _state["engine"] = client, engine
        client.start()
    return {"ok": True, "message": "connecting as %s to %s" % (cfg["username"], cfgmod.server_url(cfg))}


def _need_client():
    client = _state["client"]
    if client is None:
        return None, {"ok": False, "error": "not connected: run showdown_connect first"}
    return client, None


def op_disconnect(args):
    with _lock:
        client = _state["client"]
        if client is None:
            return {"ok": True, "message": "was not connected"}
        client.stop()
        _state["client"] = None
    return {"ok": True, "message": "disconnected"}


def op_challenge(args):
    client, err = _need_client()
    return err or client.challenge(args.get("user"), args.get("format"))


def op_ladder(args):
    client, err = _need_client()
    return err or client.ladder(args.get("format"), bool(args.get("enabled", True)))


def op_set_mode(args):
    client, err = _need_client()
    return err or client.set_mode(args.get("mode"))


def op_suggest(args):
    client, err = _need_client()
    return err or client.suggest(args.get("room"))


def op_choose(args):
    client, err = _need_client()
    return err or client.choose(args.get("room"), args.get("option"), args.get("choice"))


def op_forfeit(args):
    client, err = _need_client()
    return err or client.forfeit(args.get("room"))


def op_fetch_data(args):
    result = _state["data"].fetch()
    result["data_ready"] = _state["data"].ready
    return result


def op_stop(args):
    _stop.set()
    return {"ok": True, "message": "stopping"}


OPS = {k[3:]: v for k, v in list(globals().items()) if k.startswith("op_")}


class _Handler(socketserver.StreamRequestHandler):
    timeout = 60

    def handle(self):
        try:
            raw = self.rfile.readline()
        except OSError:
            return
        if not raw:
            return
        try:
            request = json.loads(raw.decode(ENCODING, errors="replace"))
        except ValueError:
            return self._reply({"ok": False, "error": "bad request (not JSON)"})
        if not isinstance(request, dict):
            return self._reply({"ok": False, "error": "bad request (not an object)"})
        fn = OPS.get(request.get("op"))
        if fn is None:
            return self._reply({"ok": False, "error": "unknown op: %r" % request.get("op")})
        try:
            args = request.get("args")
            result = fn(args if isinstance(args, dict) else {})
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": "showdown-worker internal error: %s" % e}
        self._reply(result if isinstance(result, dict) else {"ok": False, "error": "internal error"})

    def _reply(self, result):
        try:
            self.wfile.write((json.dumps(result) + "\n").encode(ENCODING))
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _handle_stop(signum, frame):
    _stop.set()


def main():
    try:
        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)
    except ValueError:
        pass
    try:
        server = _Server(("127.0.0.1", 0), _Handler)
    except OSError as e:
        print("showdown-worker: could not bind a loopback port: %s" % e, flush=True)
        return 1
    port = server.server_address[1]
    try:
        rpc.JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        from .. import atomic_io
        atomic_io.write_json(rpc.PORT_FILE, {"port": port, "pid": os.getpid(), "started": time.time()})
    except Exception as e:  # noqa: BLE001
        print("showdown-worker: could not write the port file: %s" % e, flush=True)
        return 1
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    print("showdown-worker: running on 127.0.0.1:%d" % port, flush=True)
    try:
        while not _stop.is_set():
            _stop.wait(0.5)
    finally:
        client = _state["client"]
        if client is not None:
            client.stop()
        server.shutdown()
        server.server_close()
        try:
            rpc.PORT_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        print("showdown-worker: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
