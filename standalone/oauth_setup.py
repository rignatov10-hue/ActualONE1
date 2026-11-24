"""One-time setup of YOUR OWN Google Drive access (no third-party service).

1. Open https://console.cloud.google.com/ and create a project (free).
2. In "APIs & Services > Library" enable the "Google Drive API".
3. In "APIs & Services > OAuth consent screen" pick External, add yourself as a
   test user (enough for personal use).
4. In "Credentials" create an "OAuth client ID" of type "Desktop app" and
   download it as ``credentials.json`` into this project folder.
5. Run:  python -m standalone.oauth_setup
   A browser opens; sign in and allow read-only Drive access.
   The token is saved to ``google_token.json`` and auto-refreshes afterwards.

After that the agent reads any file in YOUR Drive, including the private ones,
directly from Google - no Mistral or other intermediary involved.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .drive import _GOOGLE_SCOPES, OAUTH_TOKEN_PATH

_REDIRECT_PORT = 8765
_REDIRECT_URI = f"http://localhost:{_REDIRECT_PORT}"


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state_error: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _CallbackHandler.code = params["code"][0]
            self.send_response(200)
        else:
            _CallbackHandler.state_error = str(params.get("error", ["unknown error"])[0])
            self.send_response(400)
        self.end_headers()
        self.wfile.write(
            b"<html><body><p>Google sign-in complete. You can close this tab.</p></body></html>"
        )

    def log_message(self, *args: object) -> None:
        pass


def _client_config(path: str = "credentials.json") -> tuple[str, str, str]:
    data = json.loads(open(path, encoding="utf-8").read())
    if "installed" in data:
        entry = data["installed"]
    elif "web" in data:
        entry = data["web"]
    else:
        raise SystemExit(f"{path} does not look like an OAuth client JSON.")
    return entry["client_id"], entry["client_secret"], entry["auth_uri"], entry["token_uri"]


def _wait_for_code(server: HTTPServer, timeout: float = 300.0) -> str:
    server.timeout = 1.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        server.handle_request()
        if _CallbackHandler.code or _CallbackHandler.state_error:
            break
    if _CallbackHandler.state_error:
        raise SystemExit(f"Google sign-in failed: {_CallbackHandler.state_error}")
    if not _CallbackHandler.code:
        raise SystemExit("Timed out waiting for the Google sign-in.")
    return _CallbackHandler.code


def _exchange(token_uri: str, client_id: str, client_secret: str, code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": _REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode()
    request = urllib.request.Request(
        token_uri, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.URLError as exc:
        raise SystemExit(f"Token exchange failed: {exc}.") from exc


def main() -> None:
    if not os.path.exists("credentials.json"):
        raise SystemExit(
            "Put your Google OAuth client file as credentials.json in this folder first. "
            "See the module docstring (`python -m standalone.oauth_setup`) for the 5 steps."
        )
    client_id, client_secret, auth_uri, token_uri = _client_config()
    server = HTTPServer(("127.0.0.1", _REDIRECT_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "scope": _GOOGLE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    url = f"{auth_uri}?{params}"
    print("Opening the browser for Google sign-in...")
    print(f"If nothing opens, visit: {url}")
    webbrowser.open(url)
    code = _wait_for_code(server)
    server.shutdown()
    tokens = _exchange(token_uri, client_id, client_secret, code)
    saved = {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": time.time() + float(tokens.get("expires_in", 3600)),
    }
    if not saved["refresh_token"]:
        raise SystemExit(
            "Google returned no refresh token. Revoke the app's access at "
            "https://myaccount.google.com/permissions and run this again."
        )
    OAUTH_TOKEN_PATH.write_text(json.dumps(saved, indent=2))
    print(f"Done. Google token saved to {OAUTH_TOKEN_PATH}. Private Drive files now work.")


if __name__ == "__main__":
    main()
