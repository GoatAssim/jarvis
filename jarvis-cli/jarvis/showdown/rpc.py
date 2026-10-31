"""The loopback channel between the tools (short-lived processes) and the worker (the
long-lived daemon that owns the Showdown connection).

Same wire format as browser_daemon: one newline-delimited JSON request per connection,
one JSON line back, on 127.0.0.1 only. The port is OS-assigned and written to
~/.jarvis/showdown-worker.port.

Standard library only, so the tools can import it at module level.
"""

import json
import socket
from pathlib import Path

ENCODING = "utf-8"
JARVIS_DIR = Path.home() / ".jarvis"
PORT_FILE = JARVIS_DIR / "showdown-worker.port"
CONNECT_TIMEOUT = 1.5
CALL_TIMEOUT = 30

NOT_RUNNING = ("The Showdown worker is not running. Start 'Showdown worker' in Menu > Daemons "
               "(or run `python -m jarvis.showdown.worker`), then try again.")


def read_port():
    try:
        data = json.loads(PORT_FILE.read_text(encoding=ENCODING))
        port = int(data.get("port"))
        return port if port > 0 else None
    except Exception:  # noqa: BLE001 - no file / stale / corrupt all mean "not running"
        return None


def call(op, args=None, timeout=CALL_TIMEOUT):
    """Round trip to the worker. Always returns a dict; never raises."""
    port = read_port()
    if port is None:
        return {"ok": False, "error": NOT_RUNNING, "worker_running": False}
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=CONNECT_TIMEOUT) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode(ENCODING))
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError:
        return {"ok": False, "error": NOT_RUNNING, "worker_running": False}
    lines = b"".join(chunks).decode(ENCODING, errors="replace").splitlines()
    if not lines:
        return {"ok": False, "error": "the Showdown worker gave no answer"}
    try:
        result = json.loads(lines[0])
    except ValueError:
        return {"ok": False, "error": "the Showdown worker gave an unreadable answer"}
    return result if isinstance(result, dict) else {"ok": False, "error": "unexpected worker answer"}
