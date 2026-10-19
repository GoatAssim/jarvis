"""Runner for the stremio-web daemon (what `DAEMONS` in stremio_stack.py launches).

Starts with an underscore, so the tool loader skips it.  Reads `web.dir` and `web.start_cmd` from
~/.jarvis/stremio.json when it LAUNCHES.  Exits 0 at once when something already answers on the
configured web address (a copy started by hand), so no duplicate is spawned.  The command is the
owner's own (`pnpm start`, or `pnpm build` + the bundled http_server for daily use); it is run
with a shell inside the owner's stremio-web folder, exactly as typed there.
"""

import os
import subprocess
import sys


def _main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    if root not in sys.path:
        sys.path.insert(0, root)
    from jarvis.hub.stremio import config, server

    url = config.web_url()
    if server.port_answers(url):
        print("stremio-web already answers at %s; not starting a second copy." % url, flush=True)
        return 0
    folder = config.web_dir()
    if not folder:
        print("stremio-web isn't set up: put its folder in web.dir of %s (and web.start_cmd if it isn't "
              "`pnpm start`)." % config.config_path(), file=sys.stderr, flush=True)
        return 2
    command = config.web_command()
    print("starting stremio-web (%s) in %s: %s" % (url, folder, command), flush=True)
    try:
        proc = subprocess.Popen(command, shell=True, cwd=folder)
    except OSError as exc:
        print("couldn't start it: %s" % exc, file=sys.stderr, flush=True)
        return 3
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    sys.exit(_main())
