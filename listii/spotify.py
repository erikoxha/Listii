import base64
import hashlib
import json
import secrets
import time
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs
import keyring
import requests
from .models import Playlist, Entry, parse_track, playlist_id

REDIRECT = "http://127.0.0.1:8765/callback"
API = "https://api.spotify.com/v1/"

class SpotifyError(RuntimeError):
    pass

class Spotify:
    def __init__(self, client_id):
        self.client_id = client_id.strip()
        self.token = {}
        self.lock = threading.RLock()
        try:
            backend = keyring.get_keyring()
            module = type(backend).__module__
            if module not in ("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService", "keyring.backends.kwallet"):
                raise SpotifyError("Select an OS credential-store backend; plaintext and alternate keyrings are not accepted.")
            saved = keyring.get_password("Listii Spotify", self.client_id)
            self.token = json.loads(saved) if saved else {}
        except Exception as exc:
            raise SpotifyError("Secure credential storage is unavailable. Enable your OS keychain.") from exc

    def save(self, token):
        token.setdefault("refresh_token", self.token.get("refresh_token"))
        token["expires_at"] = time.time() + token.get("expires_in", 3600) - 60
        keyring.set_password("Listii Spotify", self.client_id, json.dumps(token))
        self.token = token

    def login(self):
        if not self.client_id:
            raise SpotifyError("Enter your Spotify app Client ID first.")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(32)
        result = {}
        class Callback(BaseHTTPRequestHandler):
            def do_GET(handler):
                query = parse_qs(urlparse(handler.path).query)
                if urlparse(handler.path).path != "/callback" or query.get("state", [""])[0] != state:
                    handler.send_error(400, "Invalid authorization callback")
                    return
                result.update(query)
                handler.send_response(200)
                handler.send_header("Content-Type", "text/html; charset=utf-8")
                handler.end_headers()
                handler.wfile.write(b"<h2>Listii</h2><p>You can return to the app.</p>")
            def log_message(self, *args):
                pass
        try:
            server = HTTPServer(("127.0.0.1", 8765), Callback)
        except OSError as exc:
            raise SpotifyError("Port 8765 is in use. Close the other sign-in session and retry.") from exc
        server.timeout = 1
        try:
            webbrowser.open("https://accounts.spotify.com/authorize?" + urlencode({
                "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT,
                "scope": "playlist-read-private playlist-read-collaborative", "state": state,
                "code_challenge_method": "S256", "code_challenge": challenge}))
            deadline = time.time() + 180
            while not result and time.time() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if "code" not in result:
            raise SpotifyError("Sign-in was declined or timed out. Try connecting again.")
        response = requests.post("https://accounts.spotify.com/api/token", data={
            "grant_type": "authorization_code", "code": result["code"][0],
            "redirect_uri": REDIRECT, "client_id": self.client_id, "code_verifier": verifier}, timeout=30)
        if not response.ok:
            raise SpotifyError("Spotify could not complete sign-in. Check the Client ID and redirect URI.")
        self.save(response.json())

    def refresh(self):
        if not self.token.get("refresh_token"):
            raise SpotifyError("Connect Spotify to access this playlist.")
        response = requests.post("https://accounts.spotify.com/api/token", data={
            "grant_type": "refresh_token", "refresh_token": self.token["refresh_token"],
            "client_id": self.client_id}, timeout=30)
        if not response.ok:
            raise SpotifyError("Spotify authorization expired. Connect Spotify again.")
        self.save(response.json())

    def get(self, path, cancel=None):
        # Pagination URLs must remain on the Spotify API origin.
        url = path if path.startswith("https://") else API + path
        if not url.startswith(API):
            raise SpotifyError("Unexpected Spotify pagination address.")
        for attempt in range(6):
            if cancel and cancel.is_set():
                raise SpotifyError("Playlist import cancelled.")
            with self.lock:
                if time.time() >= self.token.get("expires_at", 0):
                    self.refresh()
                access = self.token["access_token"]
            response = requests.get(url, headers={"Authorization": "Bearer " + access}, timeout=30)
            if response.status_code == 401:
                with self.lock:
                    self.refresh()
                continue
            if response.status_code == 429 or response.status_code >= 500:
                delay = min(120, max(1, float(response.headers.get("Retry-After", 2 ** attempt))))
                if cancel:
                    if cancel.wait(delay):
                        raise SpotifyError("Playlist import cancelled.")
                else:
                    time.sleep(delay)
                continue
            if response.status_code in (403, 404):
                raise SpotifyError("Spotify denied access to this playlist. Check ownership/access, app permissions, and your app's API access tier.")
            if not response.ok:
                raise SpotifyError("Spotify request failed (HTTP %s). Try again." % response.status_code)
            return response.json()
        raise SpotifyError("Spotify is rate limited or temporarily unavailable. Retry later.")

    def playlist(self, link, cancel=None):
        pid = playlist_id(link)
        # Recheck snapshot so concurrent playlist edits cannot silently alter positions.
        for _ in range(3):
            info = self.get("playlists/" + pid, cancel)
            entries = []
            page = self.get("playlists/%s/items?limit=50&offset=0" % pid, cancel)
            while True:
                for item in page.get("items", []):
                    track = parse_track(item, len(entries) + 1)
                    entry = Entry(track)
                    if not track.supported:
                        entry.detail = "Removed entry or unsupported media type. No MP3 will be generated."
                    entries.append(entry)
                if not page.get("next"):
                    break
                page = self.get(page["next"], cancel)
            latest = self.get("playlists/" + pid, cancel)
            if latest.get("snapshot_id") == info.get("snapshot_id"):
                return Playlist(pid, info.get("name", "Playlist"), entries, info.get("snapshot_id", ""))
        raise SpotifyError("This playlist changed during import. Wait until editing stops and retry.")
