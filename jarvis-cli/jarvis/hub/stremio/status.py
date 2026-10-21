"""The Stremio stack in plain words (brief 12 / N9, step 2).

One place decides what to SAY about Stremio, so `stack_status`, `stack_start`, `stack_doctor`, the
`hub-admin stremio_status` block (and, later, the panel card) never disagree:

    collect()   asks the system (the port, the probes in detect.py, the daemon) and returns FACTS
    describe()  turns facts into one state, one sentence, what to do next, and the step that failed

`describe()` is pure, so every state has a test that feeds it facts and checks its sentence.

States (`state`), most specific first:

    running        a Stremio server answers at the configured address
    other_port     it answers, but at another loopback port than Jarvis is set to
    port_busy      something listens at the configured address but is not a Stremio server
    start_failed   Jarvis started the program and it stopped again (the console's last line is shown)
    starting       Jarvis started it a moment ago and it has not answered yet
    silent         Jarvis started it, the wait is over, and it still does not answer
    app_open       the Stremio app is open but its server does not answer (Jarvis starts nothing)
    files_missing  Stremio was found, but not the runtime + server.js Jarvis needs to start a server
    not_found      no sign of Stremio anywhere
    ready          Stremio is installed and its server is not running: Start works

Nothing here starts or stops anything.  Secrets are never read.
"""

import time

from . import config, detect

DEFAULT_WAIT_S = 60

# Until the Locate... button exists in the Downloads settings, the sentence says how to do it by hand.
LOCATE_HINT = ("Point Jarvis at it with Locate... (Settings > Downloads). Until that button exists, "
               "set server.runtime_exe and server.server_js in %s")

STEP_LABELS = {"find": "finding Stremio", "launch": "starting the program", "wait": "waiting for its server",
               "port": "the server's port", "": ""}

TITLES = {
    "running": "Running",
    "other_port": "Running on another port",
    "port_busy": "Port in use",
    "start_failed": "Could not start",
    "starting": "Starting",
    "silent": "Started but not answering",
    "app_open": "App open, server silent",
    "files_missing": "Found, but not its server files",
    "not_found": "Not found",
    "ready": "Installed, not running",
}


def _locate_hint():
    return LOCATE_HINT % config.config_path()


def _short(text, limit=160):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# --------------------------------------------------------------------------- facts

def _daemon_facts(daemon_id, daemon_info=None):
    """The server daemon's state plus, when it stopped badly, the last line of its console."""
    from . import server
    info = dict((daemon_info or server.daemon_state)(daemon_id))
    info.setdefault("registered", False)
    info.setdefault("running", False)
    if info.get("registered") and not info.get("running"):
        try:
            from ... import daemons
            full = daemons.status(daemon_id)
            info["exit_code"] = full.get("exit_code")
            info["started_at"] = full.get("started_at")
            info["last_error"] = full.get("last_error") or ""
            if info.get("status") in ("crashed", "failed", "restarting") or full.get("exit_code") not in (None, 0):
                tail = [ln for ln in daemons.read_console(daemon_id, lines=6) if ln.strip()]
                info["console_tail"] = _short(tail[-1]) if tail else ""
        except Exception:  # noqa: BLE001 - the console being unreadable must not hide the state
            pass
    elif info.get("registered") and info.get("running"):
        try:
            from ... import daemons
            info["started_at"] = daemons.status(daemon_id).get("started_at")
        except Exception:  # noqa: BLE001
            pass
    return info


def collect(cfg=None, check=None, port_busy=None, daemon_info=None, wait_seconds=None, now=None,
            waited=None, exited=False, **detect_inputs):
    """The facts `describe()` needs.  Every argument after `cfg` is a hook for tests: `check(url)`
    for the port, `port_busy(url)` for a bare connect, `daemon_info(id)` for the daemon, and the
    detect inputs (`processes`, `registry`, `shortcuts`, `folders`, `fs`, `ports`).  `waited` (seconds)
    says "Jarvis just started it and waited this long"; `exited` says the program stopped while waiting."""
    from . import server
    cfg = cfg or config.load()
    url = config.server_url(cfg)
    found = detect.detect(url, check=check, **detect_inputs)
    answer, files = found["server"], found["files"]
    pair = config.configured_pair(cfg)
    if pair:
        path = {"found": True, "runtime_exe": pair[0], "server_js": pair[1], "folder": detect.dirname(pair[1]), "where": "config"}
    else:
        path = {"found": files["found"], "runtime_exe": files["runtime_exe"], "server_js": files["server_js"],
                "folder": files["folder"], "where": files["how"]}
    up = bool(answer.get("answers"))
    busy = False
    if not up and not answer.get("other_url"):
        busy = bool((port_busy or server.port_answers)(url))
    return {
        "url": url, "up": up, "version": answer.get("version") or "", "reason": answer.get("reason") or "",
        "other_url": answer.get("other_url") or "", "other_version": answer.get("other_version") or "",
        "port_busy": busy, "path": path, "app_running": bool(files.get("app_running")),
        "install_folder": files.get("install_folder") or "", "installed_version": files.get("installed_version") or "",
        "daemon": _daemon_facts(server.SERVER_DAEMON, daemon_info), "problems": config.problems(cfg),
        "tried": found["tried"], "wait_seconds": int(wait_seconds or DEFAULT_WAIT_S),
        "now": time.time() if now is None else now, "waited": waited, "exited": bool(exited),
    }


# --------------------------------------------------------------------------- the words

def describe(facts):
    """{'state', 'title', 'sentence', 'next', 'action', 'step', 'address', 'version', 'managed_by',
    'path', 'problems', 'can_start'} for a facts dict.  Pure."""
    url = facts.get("url") or ""
    daemon = facts.get("daemon") or {}
    path = dict(facts.get("path") or {})
    problems = list(facts.get("problems") or [])
    wait = int(facts.get("wait_seconds") or DEFAULT_WAIT_S)
    started = daemon.get("started_at")
    age = None
    if daemon.get("running") and started:
        try:
            age = max(0, int(float(facts.get("now") or time.time()) - float(started)))
        except (TypeError, ValueError):
            age = None
    out = {"address": url, "version": facts.get("version") or "", "managed_by": "", "path": path,
           "problems": problems, "can_start": False, "action": "", "step": "", "next": ""}

    def done(state, sentence, next_="", action="", step="", can_start=False):
        out.update(state=state, title=TITLES[state], sentence=sentence, next=next_, action=action, step=step,
                   can_start=can_start)
        out["step_label"] = STEP_LABELS.get(step, "")
        return out

    if facts.get("up"):
        outside = not daemon.get("running")
        out["managed_by"] = "outside" if outside else "jarvis"
        who = ("It was started outside Jarvis (probably the Stremio app); Jarvis starts nothing and never stops it."
               if outside else "Jarvis started it.")
        return done("running", "Stremio's server is running at %s (version %s). %s" % (url, facts.get("version") or "unknown", who))

    if facts.get("other_url"):
        return done("other_port",
                    "Stremio's server is running, but not where Jarvis looks: it answers at %s (version %s) while Jarvis is set to %s."
                    % (facts["other_url"], facts.get("other_version") or "unknown", url),
                    "Save that address so Jarvis uses it (Locate... saves it together with the paths).", "save_port", "port")

    if facts.get("port_busy"):
        return done("port_busy",
                    "Something on this PC is already using %s, but it doesn't answer like a Stremio server, so a Stremio server can't start there."
                    % url, "Close the program that uses that port, then try again.", "free_port", "port")

    if daemon.get("running"):
        if facts.get("waited"):
            age = max(age or 0, int(facts["waited"]))
        if age is not None and age < wait:
            return done("starting", "Jarvis started Stremio's server %d second(s) ago and it hasn't answered at %s yet (waiting up to %d seconds)."
                        % (age, url, wait), "Check again in a few seconds.", "wait", "wait")
        since = ("%d seconds" % age) if age is not None else ("more than %d seconds" % wait)
        return done("silent", "Jarvis started Stremio's server %s ago and it still doesn't answer at %s. The program is running, so it may be stuck."
                    % (since, url), "Look at its console (Menu > Daemons > Stremio streaming server), or stop it there and start it again.",
                    "wait", "wait")

    exit_code = daemon.get("exit_code")
    if daemon.get("status") in ("crashed", "failed", "restarting") or exit_code not in (None, 0) or facts.get("exited"):
        tail = daemon.get("console_tail") or daemon.get("last_error") or ""
        return done("start_failed",
                    "Jarvis started the Stremio program but it stopped again%s%s."
                    % ((" (exit code %s)" % exit_code) if exit_code not in (None, 0) else "",
                       (". Its console says: %s" % tail) if tail else ""),
                    "Read its console (Menu > Daemons > Stremio streaming server); if the paths are wrong, Locate... fixes them.",
                    "start" if path.get("found") else "locate", "launch", can_start=bool(path.get("found")))

    if facts.get("app_running"):
        return done("app_open",
                    "The Stremio app is open, but its server isn't answering at %s. It may still be starting. "
                    "I won't start a second server while the app is open." % url,
                    "Wait a few seconds, or close Stremio and reopen it (or close it and press Start).", "wait", "wait")

    if path.get("found"):
        how = detect.HOW.get(path.get("where") or "", "")
        return done("ready", "Stremio is installed (found %s, in %s) and its server isn't running."
                    % (how or "on this PC", path.get("folder") or "?"), "Press Start.", "start", "", can_start=True)

    if facts.get("install_folder"):
        return done("files_missing",
                    "I found Stremio (in %s) but not the two files Jarvis needs to start its server: a Stremio runtime and server.js. %s"
                    % (facts["install_folder"], _locate_hint()), "", "locate", "find")

    return done("not_found",
                "I couldn't find Stremio on this PC. I looked for a running Stremio program, in Windows' list of installed programs, "
                "in the Start Menu and in Stremio's usual install folders. If it is installed somewhere else, %s. "
                "If it isn't installed, install the Stremio app." % _locate_hint().replace("Point", "point", 1), "", "locate", "find")


def snapshot(**kwargs):
    """collect() + describe() in one call, with the probes' own lines kept for the doctor."""
    facts = collect(**kwargs)
    out = describe(facts)
    out["tried"] = facts["tried"]
    out["installed_version"] = facts.get("installed_version") or ""
    return out


# --------------------------------------------------------------------------- the diagnostic text

def diagnostic_text(state, extra_lines=()):
    """A plain-text block the owner can copy and send: the state, the address, what each probe found.
    Never contains a secret (config.public_view blanks them; nothing here reads the account key)."""
    lines = ["Stremio status: %s" % state.get("title", "?"), state.get("sentence", ""),
             "address: %s" % (state.get("address") or "?")]
    if state.get("version"):
        lines.append("server version: %s" % state["version"])
    path = state.get("path") or {}
    if path.get("found"):
        lines.append("program: %s" % path.get("runtime_exe"))
        lines.append("server.js: %s" % path.get("server_js"))
        lines.append("found: %s" % (detect.HOW.get(path.get("where") or "", "") or path.get("where") or ""))
    if state.get("step"):
        lines.append("failed step: %s" % (state.get("step_label") or state["step"]))
    for problem in state.get("problems") or []:
        lines.append("config problem: %s" % problem)
    lines.append("what I looked at:")
    for probe in state.get("tried") or []:
        lines.append("  - %s: %s" % (probe.get("probe"), probe.get("detail")))
    lines += [str(x) for x in extra_lines]
    return "\n".join(line for line in lines if line is not None)
