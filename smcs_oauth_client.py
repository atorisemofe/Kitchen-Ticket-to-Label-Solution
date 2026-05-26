import os
import json
import time
import ssl
import base64
import hashlib
import secrets
import threading
import webbrowser
import urllib.parse
import tempfile
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from filelock import FileLock
import requests

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    authorization_code = None
    returned_state = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if "code" in query and "state" in query:
            OAuthCallbackHandler.authorization_code = query["code"][0]
            OAuthCallbackHandler.returned_state = query["state"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authentication successful.")
        else:
            self.send_response(400)
            self.end_headers()

class SMCSOAuthClient:

    def __init__(self, client_id, auth_url, token_url, scopes,
                 redirect_uri="https://localhost:8888/callback",
                 token_file=".tokens/tokens.json"):

        self.client_id = client_id
        self.auth_url = auth_url
        self.token_url = token_url
        self.scopes = scopes
        self.redirect_uri = redirect_uri
        self.token_file = token_file
        self.tokens = self._load_tokens()

    def _generate_pkce(self):
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        return verifier, challenge

    def _save_tokens(self):

        lock = FileLock(self.token_file + ".lock")

        with lock:
            os.makedirs(os.path.dirname(self.token_file), exist_ok=True)

            dir_name = os.path.dirname(self.token_file)

            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as tmp:
                json.dump(self.tokens, tmp)
                temp_name = tmp.name

            shutil.move(temp_name, self.token_file)

    def _load_tokens(self):

        if os.path.exists(self.token_file):

            lock = FileLock(self.token_file + ".lock")

            with lock:
                with open(self.token_file, "r") as f:
                    return json.load(f)

        return None

    def _start_https_server(self):

        print("✅ Starting OAuth HTTPS server on https://localhost:8888/callback")
        server = HTTPServer(("localhost", 8888), OAuthCallbackHandler)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain("localhost.pem", "localhost-key.pem")

        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.handle_request()

    def authenticate(self):

        verifier, challenge = self._generate_pkce()
        state = secrets.token_urlsafe(32)

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "response_mode": "query",
            "scope": " ".join(self.scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }

        url = self.auth_url + "?" + urllib.parse.urlencode(params)

        thread = threading.Thread(target=self._start_https_server)
        thread.daemon = True
        thread.start()

        webbrowser.open(url)
        thread.join()

        if OAuthCallbackHandler.returned_state != state:
            raise Exception("State mismatch — possible CSRF attack")

        code = OAuthCallbackHandler.authorization_code

        token_data = {
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }

        response = requests.post(self.token_url, data=token_data)
        response.raise_for_status()

        self.tokens = response.json()
        self.tokens["expires_at"] = time.time() + self.tokens["expires_in"]

        self._save_tokens()

    def _refresh(self):

        refresh_data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.tokens["refresh_token"],
        }

        response = requests.post(self.token_url, data=refresh_data)
        response.raise_for_status()

        self.tokens = response.json()
        self.tokens["expires_at"] = time.time() + self.tokens["expires_in"]

        self._save_tokens()

    def get_access_token(self):

        if not self.tokens:
            raise Exception("No valid SMCS session. Please login.")

        if time.time() >= self.tokens["expires_at"] - 60:
            try:
                self._refresh()
            except Exception:
                raise Exception("SMCS session expired. Please login again.")

        return self.tokens["access_token"]

    def get_receipts(self, device_id, from_dt=None, to_dt=None, data_type="url"):

        base_url = "https://device-manager.smcs.io/printer"
        endpoint = f"/api/v1/devices/{device_id}/receipts"

        params = {}
        if from_dt:
            params["from"] = from_dt
        if to_dt:
            params["to"] = to_dt
        if data_type:
            params["data_type"] = data_type

        token = self.get_access_token()

        headers = {
            "Authorization": f"Bearer {token}"
        }

        response = requests.get(
            base_url + endpoint,
            headers=headers,
            params=params
        )

        response.raise_for_status()
        return response.json()