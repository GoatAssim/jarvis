"""HTTP client for the Playnite Bridge plugin (localhost:19821)."""

from . import playnite_config

DEFAULT_TIMEOUT = 12


def _request_cfg():
    cfg = playnite_config.load_config()
    if not cfg.get("enabled", True):
        return None, {"error": "Playnite integration is disabled in ~/.jarvis/playnite.json"}
    token = (cfg.get("token") or "").strip()
    if not token:
        return None, {
            "error": (
                "No Playnite API token. Copy it from Playnite "
                "(Main Menu > Playnite Bridge) into ~/.jarvis/playnite.json"
            )
        }
    base = (cfg.get("base_url") or "http://127.0.0.1:19821").rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    return (base, headers, cfg), None


def api(method, path, *, params=None, body=None, timeout=DEFAULT_TIMEOUT):
    try:
        import requests
    except ImportError:
        return {"error": "requests package not installed"}

    req, err = _request_cfg()
    if err:
        return err
    base, headers, _cfg = req
    url = f"{base}{path}"
    if body is not None:
        headers = {**headers, "Content-Type": "application/json"}

    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=body,
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError:
        return {"error": "Could not reach Playnite Bridge — is Playnite running with the plugin enabled?"}
    except requests.exceptions.Timeout:
        return {"error": f"Playnite Bridge timed out after {timeout}s"}
    except requests.exceptions.RequestException as e:
        return {"error": f"Playnite request failed: {e}"}

    if resp.status_code == 401:
        return {"error": "Playnite unauthorized — check token in ~/.jarvis/playnite.json"}
    if resp.status_code >= 400:
        snippet = (resp.text or "")[:200]
        try:
            err_msg = resp.json().get("error")
            if err_msg:
                snippet = err_msg
        except (ValueError, AttributeError):
            pass
        return {"error": f"Playnite HTTP {resp.status_code}: {snippet}"}

    if not resp.content:
        return {"ok": True}
    try:
        return resp.json()
    except ValueError:
        return {"ok": True, "raw": resp.text[:500]}


def api_text(method, path, *, params=None, timeout=DEFAULT_TIMEOUT, max_chars=120000):
    """Return plain-text response body or an error dict."""
    try:
        import requests
    except ImportError:
        return {"error": "requests package not installed"}

    req, err = _request_cfg()
    if err:
        return err
    base, headers, _cfg = req
    url = f"{base}{path}"

    try:
        resp = requests.request(method, url, headers=headers, params=params, timeout=timeout)
    except requests.exceptions.ConnectionError:
        return {"error": "Could not reach Playnite Bridge — is Playnite running with the plugin enabled?"}
    except requests.exceptions.Timeout:
        return {"error": f"Playnite Bridge timed out after {timeout}s"}
    except requests.exceptions.RequestException as e:
        return {"error": f"Playnite request failed: {e}"}

    if resp.status_code == 401:
        return {"error": "Playnite unauthorized — check token in ~/.jarvis/playnite.json"}
    if resp.status_code >= 400:
        return {"error": f"Playnite HTTP {resp.status_code}: {(resp.text or '')[:200]}"}

    text = resp.text or ""
    if len(text) > max_chars:
        text = text[:max_chars]
    return {"text": text, "path": path}


def api_binary(method, path, *, params=None, timeout=DEFAULT_TIMEOUT):
    """Return raw response bytes or an error dict."""
    try:
        import requests
    except ImportError:
        return None, {"error": "requests package not installed"}

    req, err = _request_cfg()
    if err:
        return None, err
    base, headers, _cfg = req
    url = f"{base}{path}"

    try:
        resp = requests.request(method, url, headers=headers, params=params, timeout=timeout)
    except requests.exceptions.ConnectionError:
        return None, {"error": "Could not reach Playnite Bridge — is Playnite running with the plugin enabled?"}
    except requests.exceptions.Timeout:
        return None, {"error": f"Playnite Bridge timed out after {timeout}s"}
    except requests.exceptions.RequestException as e:
        return None, {"error": f"Playnite request failed: {e}"}

    if resp.status_code == 401:
        return None, {"error": "Playnite unauthorized — check token in ~/.jarvis/playnite.json"}
    if resp.status_code >= 400:
        return None, {"error": f"Playnite HTTP {resp.status_code}: {(resp.text or '')[:200]}"}

    return resp, None
