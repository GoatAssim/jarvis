"""Runner for the Stremio streaming server daemon (what `DAEMONS` in stremio_stack.py launches).

Starts with an underscore, so the tool loader skips it.  It reads ~/.jarvis/stremio.json at
LAUNCH time (not when the tool file loads), so fixing a path there needs no re-registration:
the daemon is just `python _stremio_server.py`.

  * if something already answers on the server's port (the Stremio desktop app, a copy started by
    hand) it says so and exits 0 -- the daemon is not restarted, and no second server is spawned;
  * otherwise it finds the files the way `hub/stremio/detect.py` does (the saved paths first, then a running
    Stremio, Windows' install list, the Start Menu, the usual folders) and runs `<runtime> <server.js>` in the foreground and passes its exit code on, so
    the daemon supervisor's restart policy (on-failure) sees what really happened.
"""

import os
import subprocess
import sys


def _main():
    here = os.path.dirname(os.path.abspath(__file__))              # .../jarvis/actions
    root = os.path.dirname(os.path.dirname(here))                  # the folder holding `jarvis`
    if root not in sys.path:
        sys.path.insert(0, root)
    from jarvis.hub.stremio import config, server

    url = config.server_url()
    if server.port_answers(url):
        print("Stremio's server already answers at %s; not starting a second one." % url, flush=True)
        return 0
    info = config.runtime_info()
    if not info["found"]:
        print("I can't find Stremio's server files (a Stremio runtime next to server.js). I looked at: %s. "
              "Install the Stremio desktop app, or point Jarvis at it with Locate... (Settings > Downloads); until that exists, "
              "set server.runtime_exe and server.server_js in %s"
              % ("; ".join("%s: %s" % (t["probe"], t["detail"]) for t in info["tried"]) or "nothing", config.config_path()),
              file=sys.stderr, flush=True)
        return 2
    exe, js = info["runtime_exe"], info["server_js"]
    print("starting the Stremio server (%s, found %s): %s %s" % (url, info["how"] or info["where"], exe, js), flush=True)
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in ((config.load().get("server") or {}).get("env") or {}).items()
                if isinstance(v, (str, int, float))})
    try:
        proc = subprocess.Popen([exe, js], cwd=os.path.dirname(js), env=env)
    except OSError as exc:
        print("couldn't start %s: %s" % (exe, exc), file=sys.stderr, flush=True)
        return 3
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    sys.exit(_main())
