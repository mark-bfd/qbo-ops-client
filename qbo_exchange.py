"""
qbo_exchange — finish the QBO OAuth by exchanging a pasted authorization code.

Used with an HTTPS callback page (e.g. https://app.example.com/callback.html),
which is required because Intuit production redirect URIs must be HTTPS on a real
web server (localhost is not accepted for production).

Flow:
  1. User opens the consent URL, clicks Connect, and lands on the callback page.
  2. The callback page displays `code` and `realmId`.
  3. Run:  python qbo_exchange.py <CODE> <REALM_ID>
     -> exchanges the code for tokens, stores refresh_token + realm_id in
        the OS keyring, and verifies the connection.

client_id/client_secret are resolved env-first (QBO_CLIENT_ID/QBO_CLIENT_SECRET),
then from the OS keyring (service 'qbo-client').

Set QBO_REDIRECT_URI to the exact redirect URI registered on your Intuit app —
it must match what was used in the consent URL or the exchange is rejected.
"""
from __future__ import annotations

import base64
import os
import sys
import requests

from qbo_client import get_cred, set_cred, get_endpoints

REDIRECT_URI = os.environ.get("QBO_REDIRECT_URI", "https://app.example.com/callback.html")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Usage: python qbo_exchange.py <CODE> [REALM_ID]")
        return 1
    code = args[0]
    realm = args[1] if len(args) > 1 else None
    sandbox = "--sandbox" in sys.argv

    client_id = get_cred("client_id")
    client_secret = get_cred("client_secret")
    if not (client_id and client_secret):
        print("Missing client_id/client_secret (env or keyring).")
        return 2

    eps = get_endpoints(sandbox)
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
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    if not r.ok:
        print(f"Token exchange failed {r.status_code}: {r.text}")
        return 3
    tok = r.json()
    set_cred("refresh_token", tok["refresh_token"])
    if realm:
        set_cred("realm_id", realm)
    print("Stored refresh_token" + (f" + realm_id={realm}" if realm else ""))

    try:
        from qbo_client import QBO
        q = QBO(sandbox=sandbox)
        ci = q.company_info()
        print(f"VERIFIED — connected to: {ci.get('CompanyName')} (realm {q.realm_id})")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: tokens stored but verification failed: {e}")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
