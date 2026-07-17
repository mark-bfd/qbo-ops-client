"""
qbo_authorize — ONE-TIME QuickBooks Online OAuth2 setup.

Run this once to authorize the app and seed the OS keyring (service
'qbo-client') with client_id, client_secret, refresh_token, realm_id.
After that, qbo_client.QBO refreshes access tokens automatically — no browser.

PREREQ (in developer.intuit.com):
  * Create/confirm an app with the QuickBooks Online Accounting API.
  * Add Redirect URI EXACTLY:  http://localhost:8000/callback
  * Copy the Production (or Sandbox) Client ID + Client Secret.

USAGE:
  python qbo_authorize.py <CLIENT_ID> <CLIENT_SECRET> [--sandbox]

Flow: prints an Authorize URL -> you open it and click Connect ->
Intuit redirects to localhost:8000 -> this script captures the code +
realmId, exchanges for tokens, stores them, and verifies the connection.
"""
from __future__ import annotations

import base64
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
import keyring

from qbo_client import SERVICE, get_endpoints

REDIRECT_URI = "http://localhost:8000/callback"
SCOPE = "com.intuit.quickbooks.accounting"

_result: dict = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        _result["code"] = qs.get("code", [None])[0]
        _result["realmId"] = qs.get("realmId", [None])[0]
        _result["state"] = qs.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = bool(_result["code"])
        msg = ("Authorized — QuickBooks is connected. You can close this tab."
               if ok else "Authorization failed — check the terminal.")
        self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *args):  # silence server logging
        pass


def main() -> int:
    sandbox = "--sandbox" in sys.argv
    pos = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(pos) >= 2:
        client_id, client_secret = pos[0], pos[1]
        keyring.set_password(SERVICE, "client_id", client_id)
        keyring.set_password(SERVICE, "client_secret", client_secret)
    else:
        client_id = keyring.get_password(SERVICE, "client_id")
        client_secret = keyring.get_password(SERVICE, "client_secret")
        if not (client_id and client_secret):
            print("No client_id/client_secret in argv or the OS keyring.")
            print(__doc__)
            return 1
    eps = get_endpoints(sandbox)

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    authorize_url = f"{eps['authorization_endpoint']}?{urllib.parse.urlencode(params)}"
    url_file = Path.home() / ".qbo" / "authorize_url.txt"
    url_file.parent.mkdir(exist_ok=True)
    url_file.write_text(authorize_url, encoding="utf-8")

    server = HTTPServer(("localhost", 8000), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    print("\n1) Open this URL in a browser and click Connect:\n")
    print(authorize_url + "\n")
    try:
        webbrowser.open(authorize_url)
    except Exception:
        pass
    print("2) Waiting for the redirect to http://localhost:8000/callback ...")

    while "code" not in _result:
        threading.Event().wait(0.5)
    server.shutdown()

    if _result.get("state") != state:
        print("ERROR: state mismatch — aborting for safety.")
        return 2
    if not _result.get("code"):
        print("ERROR: no authorization code received.")
        return 2

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(
        eps["token_endpoint"],
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": _result["code"],
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    if not r.ok:
        print(f"ERROR: token exchange failed {r.status_code}: {r.text}")
        return 3
    tok = r.json()
    keyring.set_password(SERVICE, "refresh_token", tok["refresh_token"])
    if _result.get("realmId"):
        keyring.set_password(SERVICE, "realm_id", _result["realmId"])

    print(f"\nStored client_id, client_secret, refresh_token, realm_id in the OS keyring ({SERVICE}).")
    print(f"realm_id = {_result.get('realmId')}")

    # verify-after-setup
    try:
        from qbo_client import QBO
        q = QBO(sandbox=sandbox)
        ci = q.company_info()
        print(f"VERIFIED — connected to: {ci.get('CompanyName')} (realm {q.realm_id})")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: stored tokens but verification call failed: {e}")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
