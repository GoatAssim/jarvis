"""Runner for the Download Hub daemon (what `DAEMONS` in download_hub.py launches).

Starts with an underscore, so the tool loader skips it (it is not a tool).  It adds the folder
that contains the `jarvis` package to sys.path before importing, so the same line works from a
source checkout and from an installed copy, whatever the daemon's working directory is.
"""

import os
import sys


def _main():
    here = os.path.dirname(os.path.abspath(__file__))              # .../jarvis/actions
    root = os.path.dirname(os.path.dirname(here))                  # the folder holding `jarvis`
    if root not in sys.path:
        sys.path.insert(0, root)
    from jarvis.hub import daemon
    return daemon.main()


if __name__ == "__main__":
    sys.exit(_main())
