"""Spotify Web API + PKCE login + Windows desktop app helpers."""

import base64
import hashlib
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import spotify_config

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
TIMEOUT = 20


def _requests():
    import requests
    return requests


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _compact_track(item):
    if not isinstance(item, dict):
        return None
    track = item.get("track") if "track" in item and isinstance(item.get("track"), dict) else item
    artists = ", ".join(
        a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict)
    )
    album = (track.get("album") or {}) if isinstance(track.get("album"), dict) else {}
    uri = track.get("uri") or ""
    return {
        "name": track.get("name") or "",
        "artists": artists,
        "album": album.get("name") or "",
        "uri": uri,
        "id": track.get("id") or "",
        "url": ((track.get("external_urls") or {}).get("spotify") or ""),
    }


def _compact_playlist(p):
    if not isinstance(p, dict):
        return None
    return {
        "name": p.get("name") or "",
        "uri": p.get("uri") or "",
        "id": p.get("id") or "",
        "tracks": ((p.get("tracks") or {}).get("total") if isinstance(p.get("tracks"), dict) else None),
        "owner": ((p.get("owner") or {}).get("display_name") or ""),
        "url": ((p.get("external_urls") or {}).get("spotify") or ""),
    }


def _compact_artist(a):
    if not isinstance(a, dict):
        return None
    return {
        "name": a.get("name") or "",
        "uri": a.get("uri") or "",
        "id": a.get("id") or "",
        "genres": (a.get("genres") or [])[:6],
        "url": ((a.get("external_urls") or {}).get("spotify") or ""),
    }


def _compact_album(a):
    if not isinstance(a, dict):
        return None
    artists = ", ".join(
        x.get("name", "") for x in (a.get("artists") or []) if isinstance(x, dict)
    )
    return {
        "name": a.get("name") or "",
        "artists": artists,
        "uri": a.get("uri") or "",
        "id": a.get("id") or "",
        "url": ((a.get("external_urls") or {}).get("spotify") or ""),
    }


def _save_tokens(cfg, data):
    cfg["access_token"] = data.get("access_token") or cfg.get("access_token") or ""
    if data.get("refresh_token"):
        cfg["refresh_token"] = data["refresh_token"]
    expires_in = int(data.get("expires_in") or 3600)
    cfg["expires_at"] = time.time() + expires_in
    spotify_config.save_config(cfg)


def _token_request(payload):
    req = _requests()
    cfg = spotify_config.load_config()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    secret = (cfg.get("client_secret") or "").strip()
    if secret:
        basic = base64.b64encode(
            f"{cfg.get('client_id', '')}:{secret}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"
    try:
        resp = req.post(
            f"{ACCOUNTS}/api/token",
            data=payload,
            headers=headers,
            timeout=TIMEOUT,
        )
    except req.exceptions.RequestException as e:
        return None, {"error": f"Spotify token request failed: {e}"}
    if resp.status_code >= 400:
        return None, {"error": f"Spotify auth HTTP {resp.status_code}: {(resp.text or '')[:240]}"}
    try:
        return resp.json(), None
    except ValueError:
        return None, {"error": "Spotify token response wasn't JSON."}


def refresh_access_token():
    cfg = spotify_config.load_config()
    refresh = (cfg.get("refresh_token") or "").strip()
    client_id = (cfg.get("client_id") or "").strip()
    if not refresh or not client_id:
        return None, {
            "error": "Spotify isn't logged in. Add client_id in ~/.jarvis/spotify.json, "
            "then run: jarvis spotify-login  (use the same account as the Spotify app on this PC)."
        }
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
    }
    data, err = _token_request(payload)
    if err:
        return None, err
    _save_tokens(cfg, data)
    return cfg.get("access_token"), None


def get_access_token():
    cfg = spotify_config.load_config()
    if not cfg.get("enabled", True):
        return None, {"error": "Spotify integration is disabled in ~/.jarvis/spotify.json"}
    if not (cfg.get("client_id") or "").strip():
        return None, {
            "needs_setup": True,
            "message": (
                "Spotify needs a Developer app. 1) https://developer.spotify.com/dashboard "
                "Create app. 2) Add redirect URI exactly: "
                f"{cfg.get('redirect_uri') or 'http://127.0.0.1:19823/callback'} "
                "3) Paste client_id into ~/.jarvis/spotify.json  "
                "4) Run: jarvis spotify-login  — sign in with the SAME account as Spotify on this PC."
            ),
        }
    if spotify_config.token_valid(cfg):
        return cfg.get("access_token"), None
    if (cfg.get("refresh_token") or "").strip():
        return refresh_access_token()
    return None, {
        "needs_setup": True,
        "message": "Spotify client_id is set but you're not logged in. Run: jarvis spotify-login",
    }


def api(method, path, *, params=None, json_body=None, retry=True):
    token, err = get_access_token()
    if err:
        return err
    req = _requests()
    url = path if path.startswith("http") else f"{API}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = req.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=TIMEOUT,
        )
    except req.exceptions.RequestException as e:
        return {"error": f"Spotify request failed: {e}"}

    if resp.status_code == 401 and retry:
        _, err = refresh_access_token()
        if err:
            return err
        return api(method, path, params=params, json_body=json_body, retry=False)

    if resp.status_code == 204 or not (resp.content or b"").strip():
        return {"ok": True, "status": resp.status_code}

    if resp.status_code == 404:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        msg = ((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else None)
        return {"error": msg or "Spotify returned 404 (no active device? open Spotify on this PC).", "status": 404}

    if resp.status_code == 403:
        return {"error": "Spotify 403 — Premium is required for remote play/skip/queue on this account.", "status": 403}

    if resp.status_code >= 400:
        snippet = (resp.text or "").replace("\n", " ")[:240]
        return {"error": f"Spotify HTTP {resp.status_code}: {snippet}"}

    try:
        return resp.json()
    except ValueError:
        return {"ok": True, "status": resp.status_code}


def is_premium():
    """False for free/open/unknown — we then use the desktop app + media keys."""
    cfg = spotify_config.load_config()
    product = (cfg.get("product") or "").strip().lower()
    if product:
        return product == "premium"
    me = api("GET", "/me")
    if isinstance(me, dict) and me.get("product") and not me.get("error"):
        cfg = spotify_config.load_config()
        cfg["product"] = me.get("product") or ""
        if me.get("display_name"):
            cfg["display_name"] = me["display_name"]
        spotify_config.save_config(cfg)
        return (me.get("product") or "").lower() == "premium"
    return False


def search_uri(query):
    q = urllib.parse.quote((query or "").strip())
    return f"spotify:search:{q}"


def send_media_key(action):
    """Windows media keys — work on Free if Spotify is the current media session."""
    if sys.platform != "win32":
        return False, "Media keys are only wired on Windows."
    vk = {
        "pause": 0xB3,
        "stop": 0xB2,
        "resume": 0xB3,
        "play": 0xB3,
        "unpause": 0xB3,
        "next": 0xB0,
        "skip": 0xB0,
        "previous": 0xB1,
        "prev": 0xB1,
    }.get((action or "").lower())
    if vk is None:
        return False, f"No media key for '{action}'."
    try:
        import ctypes
        user32 = ctypes.windll.user32
        ext, up = 0x0001, 0x0002
        user32.keybd_event(vk, 0, ext, 0)
        user32.keybd_event(vk, 0, ext | up, 0)
        return True, None
    except Exception as e:
        return False, str(e)


def start_desktop_app():
    """Open the local Spotify app (logged-in Windows account)."""
    if sys.platform == "win32":
        try:
            os.startfile("spotify:")
            time.sleep(2.2)
            return True
        except OSError:
            pass
        for candidate in (
            Path(os.environ.get("APPDATA", "")) / "Spotify" / "Spotify.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "Spotify.exe",
        ):
            if candidate.exists():
                try:
                    subprocess.Popen([str(candidate)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(2.5)
                    return True
                except OSError:
                    continue
        return False
    try:
        subprocess.Popen(["spotify"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        return True
    except OSError:
        return False


def open_spotify_uri(uri):
    """Hand a spotify: URI to the desktop app (current Windows login)."""
    if not uri:
        return {"error": "Need a spotify: URI."}
    if uri.startswith("https://open.spotify.com/"):
        uri = _https_to_spotify_uri(uri)
    if not uri.startswith("spotify:"):
        return {"error": f"Not a Spotify URI: {uri}"}
    if sys.platform == "win32":
        try:
            os.startfile(uri)
            return {"ok": True, "opened": uri, "via": "desktop app"}
        except OSError as e:
            return {"error": f"Couldn't open Spotify URI: {e}"}
    try:
        subprocess.Popen(["spotify", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "opened": uri, "via": "desktop app"}
    except OSError:
        try:
            webbrowser.open(uri.replace("spotify:", "https://open.spotify.com/"))
            return {"ok": True, "opened": uri, "via": "browser"}
        except Exception as e:
            return {"error": str(e)}


def _https_to_spotify_uri(url):
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        kind, sid = parts[0], parts[1].split("?")[0]
        kind = {"track": "track", "playlist": "playlist", "album": "album", "artist": "artist", "episode": "episode", "show": "show"}.get(kind, kind)
        return f"spotify:{kind}:{sid}"
    return url


def pick_local_device(devices):
    items = devices if isinstance(devices, list) else []
    if not items:
        return None
    active = [d for d in items if d.get("is_active")]
    if active:
        return active[0]
    computers = [d for d in items if (d.get("type") or "").lower() == "computer"]
    if computers:
        return computers[0]
    return items[0]


def ensure_device():
    data = api("GET", "/me/player/devices")
    if isinstance(data, dict) and data.get("error"):
        return None, data
    devices = (data or {}).get("devices") or []
    chosen = pick_local_device(devices)
    if chosen:
        return chosen, None
    start_desktop_app()
    data = api("GET", "/me/player/devices")
    if isinstance(data, dict) and data.get("error"):
        return None, data
    chosen = pick_local_device((data or {}).get("devices") or [])
    if chosen:
        return chosen, None
    return None, {
        "error": "No Spotify device. Open the Spotify app on this PC, play any track once, then retry.",
        "hint": "Free accounts can still use spotify_play with desktop_uri fallback.",
    }


def login_interactive():
    """PKCE browser login on localhost. Blocks until callback or timeout."""
    cfg = spotify_config.load_config()
    client_id = (cfg.get("client_id") or "").strip()
    redirect = (cfg.get("redirect_uri") or "http://127.0.0.1:19823/callback").strip()
    if not client_id:
        return False, (
            "Set client_id in ~/.jarvis/spotify.json first.\n"
            "Dashboard: https://developer.spotify.com/dashboard\n"
            f"Redirect URI must be exactly: {redirect}"
        )

    parsed = urllib.parse.urlparse(redirect)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (19823 if parsed.scheme == "http" else 443)

    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(16)
    result = {"code": None, "error": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(q.query)
            if qs.get("state", [None])[0] != state:
                result["error"] = "State mismatch — try login again."
                self._ok("Login failed (state). You can close this tab.")
                return
            if qs.get("error"):
                result["error"] = qs["error"][0]
                self._ok(f"Spotify said: {result['error']}")
                return
            result["code"] = qs.get("code", [None])[0]
            self._ok("Jarvis is connected to Spotify. You can close this tab.")

        def _ok(self, text):
            body = f"<html><body><p>{text}</p></body></html>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    try:
        server = HTTPServer((host, port), Handler)
    except OSError as e:
        return False, f"Couldn't bind {host}:{port} — {e}"

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": " ".join(spotify_config.SCOPES),
    }
    url = f"{ACCOUNTS}/authorize?{urllib.parse.urlencode(params)}"
    webbrowser.open(url)
    thread.join(timeout=180)
    try:
        server.server_close()
    except Exception:
        pass

    if result["error"]:
        return False, f"Spotify login error: {result['error']}"
    if not result["code"]:
        return False, "Timed out waiting for the browser login."

    payload = {
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    data, err = _token_request(payload)
    if err:
        return False, err.get("error") or str(err)
    cfg = spotify_config.load_config()
    _save_tokens(cfg, data)

    me = api("GET", "/me")
    if isinstance(me, dict) and not me.get("error"):
        cfg = spotify_config.load_config()
        cfg["display_name"] = me.get("display_name") or me.get("id") or ""
        cfg["user_id"] = me.get("id") or ""
        cfg["product"] = me.get("product") or ""
        spotify_config.save_config(cfg)
        who = cfg["display_name"]
        plan = cfg["product"] or "unknown"
    else:
        who = "your account"
        plan = "unknown"

    extra = ""
    if str(plan).lower() != "premium":
        extra = (
            " Free account: Jarvis will open songs in the desktop app (not the remote Web API). "
            "Pause/skip uses Windows media keys."
        )
    return True, f"Logged in as {who} ({plan}). Playback uses the Spotify app on this PC.{extra}"
