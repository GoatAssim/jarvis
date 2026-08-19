"""Windows package-manager tools for Jarvis (winget, Chocolatey, Scoop, pip, pipx, npm).

Install/uninstall never run unless confirm=true. Package ids are sanitized so this
is not a general shell. Prefer web_search + package_search to resolve the real id
before asking the user to confirm.
"""

import json
import os
import re
import shutil
import subprocess
import sys

SEARCH_TIMEOUT = 45
INFO_TIMEOUT = 40
LIST_TIMEOUT = 60
MUTATE_TIMEOUT = 600
SEARCH_MAX = 12

_PKG_ID_RE = re.compile(r"^[@A-Za-z0-9][A-Za-z0-9._+\-@/]{0,127}$")
_MANAGERS = ("winget", "choco", "scoop", "pip", "pipx", "npm")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _which(name):
    return shutil.which(name)


def _python_exe():
    return sys.executable or "python"


def _manager_cmd(manager):
    """Return argv prefix for a manager, or None if it isn't installed."""
    if manager == "winget":
        path = _which("winget")
        return [path] if path else None
    if manager == "choco":
        path = _which("choco") or _which("choco.exe")
        return [path] if path else None
    if manager == "scoop":
        path = _which("scoop")
        if path:
            return [path]
        # scoop is often a .ps1 / shim; try powershell
        if _which("scoop.cmd"):
            return [_which("scoop.cmd")]
        return None
    if manager == "pip":
        return [_python_exe(), "-m", "pip"]
    if manager == "pipx":
        path = _which("pipx")
        return [path] if path else None
    if manager == "npm":
        path = _which("npm") or _which("npm.cmd")
        return [path] if path else None
    return None


def _available_managers():
    out = {}
    for name in _MANAGERS:
        cmd = _manager_cmd(name)
        out[name] = {"available": bool(cmd), "cmd": cmd}
    # pip is available if we can invoke python -m pip
    if out["pip"]["available"]:
        code, stdout, _ = _run(out["pip"]["cmd"] + ["--version"], timeout=12)
        out["pip"]["available"] = code == 0
        if code == 0:
            out["pip"]["version"] = (stdout or "").strip()
    return out


def _run(argv, timeout=30, extra_env=None):
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra_env:
        env.update(extra_env)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return None, "", f"Command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", f"Timed out after {timeout}s"
    except OSError as e:
        return None, "", str(e)
    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    return result.returncode, stdout, stderr


def _validate_id(package):
    if not isinstance(package, str) or not package.strip():
        return None, {"needs_clarification": True, "message": "Need a package id."}
    package = package.strip()
    if not _PKG_ID_RE.match(package):
        return None, {
            "error": "Package id has invalid characters. Use the exact id from package_search "
            "(letters, numbers, dots, hyphens — no shell syntax)."
        }
    return package, None


def _need_manager(args):
    manager = ((args or {}).get("manager") or "").strip().lower()
    if manager == "chocolatey":
        manager = "choco"
    if manager not in _MANAGERS:
        return None, {
            "needs_clarification": True,
            "message": f"Which manager? One of: {', '.join(_MANAGERS)}.",
            "available": [n for n, i in _available_managers().items() if i["available"]],
        }
    cmd = _manager_cmd(manager)
    if not cmd:
        return None, {
            "error": f"{manager} isn't installed or not on PATH.",
            "available": [n for n, i in _available_managers().items() if i["available"]],
        }
    return (manager, cmd), None


def _truncate(text, limit=4000):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(truncated)"


def _need_confirm(args, action, manager, package):
    confirm = (args or {}).get("confirm")
    if confirm is True or (isinstance(confirm, str) and confirm.lower() in ("true", "yes", "1")):
        return None
    return {
        "needs_confirmation": True,
        "manager": manager,
        "package": package,
        "action": action,
        "message": (
            f"Ask the user to confirm: {action} '{package}' via {manager}. "
            "If they say yes, call again with confirm=true."
        ),
    }


# --- search parsers -------------------------------------------------------


def _parse_winget_json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data.get("Sources") or data.get("Matches") or []
    results = []
    if isinstance(data.get("Sources"), list):
        for src in data["Sources"]:
            for m in src.get("Matches") or []:
                results.append({
                    "id": (m.get("PackageIdentifier") or m.get("Id") or "").strip(),
                    "name": (m.get("PackageName") or m.get("Name") or "").strip(),
                    "version": (m.get("Version") or "").strip(),
                    "source": src.get("SourceName") or m.get("Source") or "winget",
                })
    elif isinstance(items, list):
        for m in items:
            if not isinstance(m, dict):
                continue
            results.append({
                "id": (m.get("PackageIdentifier") or m.get("Id") or "").strip(),
                "name": (m.get("PackageName") or m.get("Name") or "").strip(),
                "version": (m.get("Version") or "").strip(),
                "source": m.get("Source") or "winget",
            })
    return [r for r in results if r.get("id")]


def _parse_winget_table(text):
    """Parse winget's aligned text table (Name / Id / Version / Source)."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    header_i = None
    for i, ln in enumerate(lines):
        if re.search(r"\bId\b", ln) and re.search(r"\bName\b", ln):
            header_i = i
            break
    if header_i is None:
        return []
    header = lines[header_i]
    # skip separator line of dashes
    start = header_i + 1
    if start < len(lines) and re.match(r"^[-+\s]+$", lines[start]):
        start += 1
    id_col = header.find("Id")
    ver_col = header.find("Version")
    src_col = header.find("Source")
    if id_col < 0:
        return []
    results = []
    for ln in lines[start:]:
        if not ln.strip() or ln.lower().startswith("no package"):
            continue
        name = ln[:id_col].strip()
        rest = ln[id_col:]
        if ver_col > id_col:
            ident = ln[id_col:ver_col].strip()
            if src_col > ver_col:
                version = ln[ver_col:src_col].strip()
                source = ln[src_col:].strip()
            else:
                version = ln[ver_col:].strip()
                source = "winget"
        else:
            ident = rest.split()[0] if rest.split() else ""
            version = ""
            source = "winget"
        if ident:
            results.append({"id": ident, "name": name, "version": version, "source": source})
        if len(results) >= SEARCH_MAX:
            break
    return results


def _parse_choco_limit(text):
    results = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or "|" not in ln:
            continue
        pid, ver = ln.split("|", 1)
        pid = pid.strip()
        if pid:
            results.append({"id": pid, "name": pid, "version": ver.strip(), "source": "chocolatey"})
        if len(results) >= SEARCH_MAX:
            break
    return results


def _parse_scoop_search(text):
    results = []
    current_bucket = ""
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.endswith("bucket:") or s.endswith("bucket"):
            current_bucket = s.replace("bucket:", "").replace("bucket", "").strip()
            continue
        # "name (version) extra"
        m = re.match(r"^([A-Za-z0-9._\-]+)\s+\(([^)]+)\)", s)
        if m:
            results.append({
                "id": m.group(1),
                "name": m.group(1),
                "version": m.group(2),
                "source": current_bucket or "scoop",
            })
        elif re.match(r"^[A-Za-z0-9._\-]+$", s):
            results.append({"id": s, "name": s, "version": "", "source": current_bucket or "scoop"})
        if len(results) >= SEARCH_MAX:
            break
    return results


def _pypi_lookup(query):
    """Exact PyPI name lookup (pip search is gone). Compact JSON from pypi.org."""
    try:
        import requests
    except ImportError:
        return [], "requests not installed"
    name = query.strip().replace(" ", "-")
    try:
        resp = requests.get(
            f"https://pypi.org/pypi/{name}/json",
            timeout=12,
            headers={"Accept": "application/json", "User-Agent": "jarvis-cli"},
        )
    except requests.exceptions.RequestException as e:
        return [], str(e)
    if resp.status_code == 404:
        return [], None
    if resp.status_code != 200:
        return [], f"PyPI HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return [], "PyPI returned invalid JSON"
    info = data.get("info") or {}
    pid = info.get("name") or name
    return [{
        "id": pid,
        "name": pid,
        "version": info.get("version") or "",
        "source": "pypi",
        "summary": (info.get("summary") or "")[:200],
    }], None


def _parse_npm_search(text):
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data if isinstance(data, list) else data.get("objects") or []
    results = []
    for item in items:
        pkg = item.get("package") if isinstance(item, dict) and "package" in item else item
        if not isinstance(pkg, dict):
            continue
        name = (pkg.get("name") or "").strip()
        if not name:
            continue
        results.append({
            "id": name,
            "name": name,
            "version": pkg.get("version") or "",
            "source": "npm",
            "summary": (pkg.get("description") or "")[:160],
        })
        if len(results) >= SEARCH_MAX:
            break
    return results


def _search_winget(cmd, query):
    argv = cmd + [
        "search", query, "--disable-interactivity",
        "--accept-source-agreements", "--output", "json",
    ]
    code, stdout, stderr = _run(argv, timeout=SEARCH_TIMEOUT)
    results = _parse_winget_json(stdout) if code == 0 else []
    if results:
        return results, None
    argv = cmd + [
        "search", query, "--disable-interactivity", "--accept-source-agreements",
    ]
    code, stdout, stderr = _run(argv, timeout=SEARCH_TIMEOUT)
    results = _parse_winget_table(stdout)
    if results:
        return results, None
    if code not in (0, None):
        return [], _truncate(stderr or stdout or f"winget search failed ({code})")
    return [], None


def _search_choco(cmd, query):
    argv = cmd + ["search", query, "--limit-output", "--page-size", str(SEARCH_MAX)]
    code, stdout, stderr = _run(argv, timeout=SEARCH_TIMEOUT)
    results = _parse_choco_limit(stdout)
    if results:
        return results, None
    if code not in (0, None):
        return [], _truncate(stderr or stdout or f"choco search failed ({code})")
    return [], None


def _search_scoop(cmd, query):
    code, stdout, stderr = _run(cmd + ["search", query], timeout=SEARCH_TIMEOUT)
    results = _parse_scoop_search(stdout)
    if results:
        return results, None
    if code not in (0, None):
        return [], _truncate(stderr or stdout or f"scoop search failed ({code})")
    return [], None


def _search_pipx(cmd, query):
    # pipx has no search; point at PyPI exact name
    results, err = _pypi_lookup(query)
    if results:
        for r in results:
            r["source"] = "pypi (install with pipx)"
        return results, None
    return [], err or "pipx has no search — web_search 'pypi <name>' then pipx install <id>."


def _search_npm(cmd, query):
    code, stdout, stderr = _run(cmd + ["search", "--json", query], timeout=SEARCH_TIMEOUT)
    if code == 0:
        results = _parse_npm_search(stdout)
        if results:
            return results, None
    return [], _truncate(stderr or stdout or "npm search failed")


# --- tools ----------------------------------------------------------------


def tool_package_managers(args=None):
    avail = _available_managers()
    managers = []
    for name, info in avail.items():
        entry = {"name": name, "available": info["available"]}
        if info.get("version"):
            entry["version"] = info["version"]
        managers.append(entry)
    return {
        "os": sys.platform,
        "managers": managers,
        "note": "Prefer winget for Windows apps, choco/scoop as fallback, pip/pipx for Python, npm -g for Node CLIs. "
                "Look up the exact package id with web_search + package_search before installing.",
    }


def tool_package_search(args):
    args = args or {}
    query = (args.get("query") or args.get("q") or "").strip()
    if len(query) < 2:
        return {"needs_clarification": True, "message": "What software / package should I search for?"}

    manager = (args.get("manager") or "").strip().lower()
    if manager == "chocolatey":
        manager = "choco"

    if manager:
        resolved, err = _need_manager({"manager": manager})
        if err:
            return err
        manager, cmd = resolved
        targets = [(manager, cmd)]
    else:
        avail = _available_managers()
        order = ["winget", "choco", "scoop", "pip", "npm", "pipx"]
        targets = [(n, avail[n]["cmd"]) for n in order if avail[n]["available"]]

    all_results = []
    errors = {}
    for name, cmd in targets:
        if name == "pip":
            results, err = _pypi_lookup(query)
        elif name == "winget":
            results, err = _search_winget(cmd, query)
        elif name == "choco":
            results, err = _search_choco(cmd, query)
        elif name == "scoop":
            results, err = _search_scoop(cmd, query)
        elif name == "pipx":
            results, err = _search_pipx(cmd, query)
        elif name == "npm":
            results, err = _search_npm(cmd, query)
        else:
            results, err = [], f"unknown manager {name}"
        if err:
            errors[name] = err
        for r in results[:SEARCH_MAX]:
            r = dict(r)
            r["manager"] = name
            all_results.append(r)

    if not all_results:
        return {
            "query": query,
            "results": [],
            "errors": errors or None,
            "note": "No local hits. Use web_search for 'winget package id <name>', "
                    "'chocolatey <name>', or 'pypi <name>', then package_search with the exact id.",
        }
    return {
        "query": query,
        "showing": len(all_results),
        "results": all_results[: SEARCH_MAX * 2],
        "note": "Use the exact 'id' + 'manager' with package_install. Confirm with the user first. Never guess ids.",
    }


def tool_package_info(args):
    args = args or {}
    resolved, err = _need_manager(args)
    if err:
        return err
    manager, cmd = resolved
    package, err = _validate_id(args.get("package") or args.get("id"))
    if err:
        return err

    if manager == "winget":
        argv = cmd + ["show", "--id", package, "-e", "--disable-interactivity", "--accept-source-agreements"]
    elif manager == "choco":
        argv = cmd + ["info", package, "--limit-output"]
    elif manager == "scoop":
        argv = cmd + ["info", package]
    elif manager == "pip":
        argv = cmd + ["show", package]
    elif manager == "pipx":
        argv = cmd + ["list"]
    elif manager == "npm":
        argv = cmd + ["view", package, "--json"]
    else:
        return {"error": f"info not supported for {manager}"}

    code, stdout, stderr = _run(argv, timeout=INFO_TIMEOUT)
    text = _truncate(stdout or stderr, 3500)
    if manager == "pip" and code != 0:
        results, lookup_err = _pypi_lookup(package)
        if results:
            return {"manager": manager, "package": package, "pypi": results[0], "installed": False}
        return {"error": lookup_err or text, "manager": manager, "package": package}
    if manager == "pipx" and code == 0:
        return {"manager": manager, "package": package, "list": _truncate(stdout, 2500)}
    return {
        "manager": manager,
        "package": package,
        "ok": code == 0,
        "info": text,
    }


def tool_package_list(args):
    args = args or {}
    resolved, err = _need_manager(args)
    if err:
        return err
    manager, cmd = resolved
    query = (args.get("query") or "").strip().lower()

    if manager == "winget":
        argv = cmd + ["list", "--disable-interactivity", "--accept-source-agreements"]
    elif manager == "choco":
        argv = cmd + ["list", "--local-only", "--limit-output"]
    elif manager == "scoop":
        argv = cmd + ["list"]
    elif manager == "pip":
        argv = cmd + ["list", "--format", "json"]
    elif manager == "pipx":
        argv = cmd + ["list"]
    elif manager == "npm":
        argv = cmd + ["list", "-g", "--depth", "0", "--json"]
    else:
        return {"error": f"list not supported for {manager}"}

    code, stdout, stderr = _run(argv, timeout=LIST_TIMEOUT)
    if code not in (0, None) and not stdout:
        return {"error": _truncate(stderr or stdout or f"{manager} list failed"), "manager": manager}

    packages = []
    if manager == "pip":
        try:
            items = json.loads(stdout or "[]")
        except ValueError:
            items = []
        for p in items:
            name = p.get("name") or ""
            if query and query not in name.lower():
                continue
            packages.append({"id": name, "version": p.get("version") or ""})
    elif manager == "choco":
        for r in _parse_choco_limit(stdout):
            if query and query not in r["id"].lower():
                continue
            packages.append(r)
    elif manager == "winget":
        for r in _parse_winget_table(stdout):
            blob = f"{r.get('id','')} {r.get('name','')}".lower()
            if query and query not in blob:
                continue
            packages.append(r)
    elif manager == "npm":
        try:
            data = json.loads(stdout or "{}")
        except ValueError:
            data = {}
        deps = data.get("dependencies") or {}
        for name, meta in deps.items():
            if query and query not in name.lower():
                continue
            packages.append({"id": name, "version": (meta or {}).get("version") or ""})
    else:
        lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
        for ln in lines:
            if query and query not in ln.lower():
                continue
            packages.append({"line": ln[:200]})

    return {
        "manager": manager,
        "showing": min(len(packages), 40),
        "total_matched": len(packages),
        "packages": packages[:40],
        "truncated": len(packages) > 40,
    }


def tool_package_install(args):
    args = args or {}
    resolved, err = _need_manager(args)
    if err:
        return err
    manager, cmd = resolved
    package, err = _validate_id(args.get("package") or args.get("id"))
    if err:
        return err
    blocked = _need_confirm(args, "install", manager, package)
    if blocked:
        return blocked

    version = (args.get("version") or "").strip()
    if version and not re.match(r"^[A-Za-z0-9._+\-]+$", version):
        return {"error": "Invalid version string."}

    extra_env = None
    if manager == "winget":
        argv = cmd + [
            "install", "--id", package, "-e", "--disable-interactivity",
            "--accept-package-agreements", "--accept-source-agreements",
        ]
        if version:
            argv += ["--version", version]
    elif manager == "choco":
        argv = cmd + ["install", package, "-y", "--no-progress"]
        if version:
            argv += ["--version", version]
    elif manager == "scoop":
        argv = cmd + ["install", package]
    elif manager == "pip":
        spec = f"{package}=={version}" if version else package
        argv = cmd + ["install", spec]
    elif manager == "pipx":
        argv = cmd + ["install", package]
    elif manager == "npm":
        spec = f"{package}@{version}" if version else package
        argv = cmd + ["install", "-g", spec]
    else:
        return {"error": f"install not supported for {manager}"}

    code, stdout, stderr = _run(argv, timeout=MUTATE_TIMEOUT, extra_env=extra_env)
    ok = code == 0
    return {
        "ok": ok,
        "manager": manager,
        "package": package,
        "exit_code": code,
        "stdout": _truncate(stdout, 2500),
        "stderr": _truncate(stderr, 1500),
        "note": None if ok else "If this needs admin, run a terminal as Administrator and retry, or install via winget/choco yourself.",
    }


def tool_package_uninstall(args):
    args = args or {}
    resolved, err = _need_manager(args)
    if err:
        return err
    manager, cmd = resolved
    package, err = _validate_id(args.get("package") or args.get("id"))
    if err:
        return err
    blocked = _need_confirm(args, "uninstall", manager, package)
    if blocked:
        return blocked

    if manager == "winget":
        argv = cmd + [
            "uninstall", "--id", package, "-e", "--disable-interactivity",
        ]
    elif manager == "choco":
        argv = cmd + ["uninstall", package, "-y", "--no-progress"]
    elif manager == "scoop":
        argv = cmd + ["uninstall", package]
    elif manager == "pip":
        argv = cmd + ["uninstall", package, "-y"]
    elif manager == "pipx":
        argv = cmd + ["uninstall", package]
    elif manager == "npm":
        argv = cmd + ["uninstall", "-g", package]
    else:
        return {"error": f"uninstall not supported for {manager}"}

    code, stdout, stderr = _run(argv, timeout=MUTATE_TIMEOUT)
    ok = code == 0
    return {
        "ok": ok,
        "manager": manager,
        "package": package,
        "exit_code": code,
        "stdout": _truncate(stdout, 2500),
        "stderr": _truncate(stderr, 1500),
    }


_PKG_PARAMS_SEARCH = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "App or library name, e.g. 'vscode' or '7zip'."},
        "manager": {
            "type": "string",
            "description": "Optional: winget, choco, scoop, pip, pipx, npm. Omit to search all available.",
        },
    },
    "required": ["query"],
}

_PKG_PARAMS_MUTATE = {
    "type": "object",
    "properties": {
        "manager": {"type": "string", "description": "winget | choco | scoop | pip | pipx | npm"},
        "package": {"type": "string", "description": "Exact package id from package_search (not a display name)."},
        "confirm": {"type": "boolean", "description": "Must be true. Ask the user first."},
        "version": {"type": "string", "description": "Optional exact version."},
    },
    "required": ["manager", "package", "confirm"],
}

PKG_TOOL_SCHEMAS = [
    {
        "name": "package_managers",
        "description": "List which Windows package managers are installed (winget, choco, scoop, pip, pipx, npm).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "package_search",
        "description": (
            "Search installed package managers for software. REQUIRED before install. "
            "If results are thin or the id is unclear, also web_search "
            "'winget package id <name>', 'chocolatey <name>', or 'pypi <name>' then search again. "
            "Never invent package ids."
        ),
        "parameters": _PKG_PARAMS_SEARCH,
    },
    {
        "name": "package_info",
        "description": "Show details for one exact package id on a manager.",
        "parameters": {
            "type": "object",
            "properties": {
                "manager": {"type": "string"},
                "package": {"type": "string", "description": "Exact package id."},
            },
            "required": ["manager", "package"],
        },
    },
    {
        "name": "package_list",
        "description": "List installed packages for one manager. Optional query filters the list.",
        "parameters": {
            "type": "object",
            "properties": {
                "manager": {"type": "string"},
                "query": {"type": "string", "description": "Optional substring filter."},
            },
            "required": ["manager"],
        },
    },
    {
        "name": "package_install",
        "description": (
            "Install a package. confirm must be true after the user agrees to the exact "
            "manager + id from package_search/web_search. Apps: winget then choco/scoop. "
            "Python: pip or pipx. Node CLIs: npm. Never run without confirmation."
        ),
        "parameters": _PKG_PARAMS_MUTATE,
    },
    {
        "name": "package_uninstall",
        "description": "Uninstall a package. confirm must be true after the user agrees to the exact manager + id.",
        "parameters": {
            "type": "object",
            "properties": {
                "manager": {"type": "string"},
                "package": {"type": "string"},
                "confirm": {"type": "boolean", "description": "Must be true. Ask the user first."},
            },
            "required": ["manager", "package", "confirm"],
        },
    },
]

PKG_TOOLS = {
    "package_managers": tool_package_managers,
    "package_search": tool_package_search,
    "package_info": tool_package_info,
    "package_list": tool_package_list,
    "package_install": tool_package_install,
    "package_uninstall": tool_package_uninstall,
}
