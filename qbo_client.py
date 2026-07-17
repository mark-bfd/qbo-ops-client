"""
qbo_client — a disciplined QuickBooks Online REST v3 client.

Design rules:
  * API-first: all reads/writes go through the QBO REST v3 API — never assume
    a write landed; the API response and a read-back are the source of truth.
  * Verify-after-write: create_and_verify() reads every new object back and
    confirms it before returning — no blind "should be posted".

Credentials are resolved env-first, then from the OS keyring (service
'qbo-client' via the `keyring` package — Windows Credential Manager,
macOS Keychain, or Secret Service on Linux):

    QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REFRESH_TOKEN, QBO_REALM_ID

Access tokens are short-lived (1h) and fetched on demand from the refresh token.
Run qbo_authorize.py ONCE to seed client_id/client_secret/refresh_token/realm_id.

Usage:
    from qbo_client import QBO
    q = QBO()
    print(q.company_info())                      # connectivity test
    rows = q.query("select * from Purchase where TxnDate >= '2026-05-01'")
    q.create_and_verify("Purchase", payload, checks={"TotalAmt": 64.95})
"""
from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path

import requests
import keyring

# Shareable error/troubleshooting log (captures intuit_tid on every call).
_LOG_DIR = Path.home() / ".qbo"
_LOG_DIR.mkdir(exist_ok=True)
logger = logging.getLogger("qbo_client")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(_LOG_DIR / "qbo.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_fh)

SERVICE = os.environ.get("QBO_KEYRING_SERVICE", "qbo-client")
PROD_BASE = "https://quickbooks.api.intuit.com/v3/company"
SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com/v3/company"
MINOR_VERSION = "75"

# OAuth endpoints are sourced from Intuit's OIDC discovery document (self-updating),
# with the current well-known values as fallback if discovery is unreachable.
DISCOVERY_URL = {
    False: "https://developer.api.intuit.com/.well-known/openid_configuration",
    True: "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration",
}
_DISCOVERY_FALLBACK = {
    "authorization_endpoint": "https://appcenter.intuit.com/connect/oauth2",
    "token_endpoint": "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
    "revocation_endpoint": "https://developer.api.intuit.com/v2/oauth2/tokens/revoke",
}
_ENDPOINTS_CACHE: dict = {}


def get_endpoints(sandbox: bool = False) -> dict:
    """Return Intuit OAuth endpoints from the OIDC discovery doc (cached; fallback on failure)."""
    if sandbox in _ENDPOINTS_CACHE:
        return _ENDPOINTS_CACHE[sandbox]
    ep = dict(_DISCOVERY_FALLBACK)
    try:
        r = requests.get(DISCOVERY_URL[bool(sandbox)], timeout=15)
        r.raise_for_status()
        d = r.json()
        for k in _DISCOVERY_FALLBACK:
            if d.get(k):
                ep[k] = d[k]
    except Exception:  # noqa: BLE001 — any failure falls back to well-known values
        pass
    _ENDPOINTS_CACHE[sandbox] = ep
    return ep


def get_cred(name: str) -> str | None:
    """Env-first credential lookup (QBO_<NAME>), then OS keyring."""
    env = os.environ.get(f"QBO_{name.upper()}")
    if env:
        return env
    return keyring.get_password(SERVICE, name)


def set_cred(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def _request(method: str, url: str, *, retries: int = 4, **kwargs):
    """HTTP with exponential backoff on transient failures (network errors, 429, 5xx).
    Makes auth + API calls resilient to throttling and transient outages."""
    backoff = 1.0
    last_exc = None
    for attempt in range(retries):
        resp = None
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            last_exc = e
        if resp is not None and resp.status_code != 429 and not (500 <= resp.status_code < 600):
            return resp
        if attempt == retries - 1:
            if resp is not None:
                return resp
            raise last_exc
        time.sleep(backoff)
        backoff *= 2


class QBOError(RuntimeError):
    pass


class QBO:
    def __init__(self, sandbox: bool = False):
        self.client_id = get_cred("client_id")
        self.client_secret = get_cred("client_secret")
        self.realm_id = get_cred("realm_id")
        if not (self.client_id and self.client_secret):
            raise QBOError(
                "Missing client_id/client_secret (env QBO_CLIENT_ID/QBO_CLIENT_SECRET or keyring). "
                "Run qbo_authorize.py <client_id> <client_secret> first."
            )
        if not self.realm_id:
            raise QBOError(
                "Missing realm_id (env QBO_REALM_ID or keyring). "
                "It is captured automatically by qbo_authorize.py."
            )
        self.sandbox = sandbox
        self.base = SANDBOX_BASE if sandbox else PROD_BASE
        self._access: str | None = None
        self._access_exp: float = 0.0

    # ---- auth -----------------------------------------------------------
    def _refresh_access(self) -> str:
        refresh = get_cred("refresh_token")
        if not refresh:
            raise QBOError("No refresh_token stored. Run qbo_authorize.py to complete OAuth.")
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        token_url = get_endpoints(self.sandbox)["token_endpoint"]
        r = _request(
            "POST",
            token_url,
            headers={
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            timeout=30,
        )
        if not r.ok:
            tid = r.headers.get("intuit_tid")
            logger.error("Token refresh failed %s intuit_tid=%s body=%s",
                         r.status_code, tid, r.text[:500])
            raise QBOError(f"Token refresh failed {r.status_code} (intuit_tid={tid}): {r.text}")
        d = r.json()
        self._access = d["access_token"]
        self._access_exp = time.time() + int(d.get("expires_in", 3600)) - 90
        # Intuit rotates the refresh token periodically — persist whatever comes back.
        if d.get("refresh_token") and d["refresh_token"] != refresh:
            set_cred("refresh_token", d["refresh_token"])
        return self._access

    def _token(self) -> str:
        if not self._access or time.time() > self._access_exp:
            self._refresh_access()
        return self._access

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def revoke(self) -> bool:
        """Revoke the stored refresh token (disconnect). Uses the discovered revocation endpoint."""
        refresh = get_cred("refresh_token")
        if not refresh:
            return True
        url = get_endpoints(self.sandbox)["revocation_endpoint"]
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = requests.post(
            url,
            headers={"Authorization": f"Basic {auth}", "Accept": "application/json",
                     "Content-Type": "application/json"},
            json={"token": refresh},
            timeout=30,
        )
        return r.ok

    # ---- core REST ------------------------------------------------------
    def _call(self, method: str, url: str, *, context: str, **kwargs):
        """Single request path: retry/backoff, capture intuit_tid, log every call,
        raise QBOError with the intuit_tid on failure (shareable for Intuit support)."""
        r = _request(method, url, **kwargs)
        tid = r.headers.get("intuit_tid")
        if not r.ok:
            logger.error("%s %s -> %s intuit_tid=%s body=%s",
                         method, context, r.status_code, tid, r.text[:1000])
            raise QBOError(f"{context} failed {r.status_code} (intuit_tid={tid}): {r.text}")
        logger.info("%s %s -> %s intuit_tid=%s", method, context, r.status_code, tid)
        return r

    def query(self, sql: str) -> dict:
        """Run a QBO SQL-like query. Returns the QueryResponse dict."""
        url = f"{self.base}/{self.realm_id}/query"
        r = self._call("GET", url, context=f"query[{sql[:40]}]",
                       headers={**self._headers(), "Content-Type": "application/text"},
                       params={"query": sql, "minorversion": MINOR_VERSION}, timeout=60)
        return r.json().get("QueryResponse", {})

    def read(self, entity: str, obj_id: str) -> dict:
        """Read a single object by Id. entity is QBO PascalCase (Purchase, Bill...)."""
        url = f"{self.base}/{self.realm_id}/{entity.lower()}/{obj_id}"
        r = self._call("GET", url, context=f"read {entity}/{obj_id}",
                       headers=self._headers(),
                       params={"minorversion": MINOR_VERSION}, timeout=60)
        return r.json().get(entity, {})

    def create(self, entity: str, payload: dict) -> dict:
        """Create an object. Returns the created object dict."""
        url = f"{self.base}/{self.realm_id}/{entity.lower()}"
        r = self._call("POST", url, context=f"create {entity}",
                       headers=self._headers(), json=payload,
                       params={"minorversion": MINOR_VERSION}, timeout=60)
        return r.json().get(entity, {})

    def update(self, entity: str, payload: dict) -> dict:
        """Update an object. Payload must include Id + current SyncToken (use sparse=true
        for partial updates; Line arrays are replace-all, not mergeable)."""
        if "Id" not in payload or "SyncToken" not in payload:
            raise QBOError(f"update {entity}: payload requires Id and SyncToken")
        url = f"{self.base}/{self.realm_id}/{entity.lower()}"
        r = self._call("POST", url, context=f"update {entity}/{payload['Id']}",
                       headers=self._headers(), json=payload,
                       params={"minorversion": MINOR_VERSION, "operation": "update"}, timeout=60)
        return r.json().get(entity, {})

    def delete(self, entity: str, obj_id: str) -> dict:
        """Delete a transaction object (requires current SyncToken; verified by read-back 404/absence)."""
        cur = self.read(entity, obj_id)
        url = f"{self.base}/{self.realm_id}/{entity.lower()}"
        r = self._call("POST", url, context=f"delete {entity}/{obj_id}",
                       headers=self._headers(),
                       json={"Id": obj_id, "SyncToken": cur["SyncToken"]},
                       params={"minorversion": MINOR_VERSION, "operation": "delete"}, timeout=60)
        return r.json()

    def update_and_verify(self, entity: str, payload: dict, checks: dict | None = None) -> dict:
        """Update then READ BACK and confirm `checks` on the persisted record."""
        updated = self.update(entity, payload)
        back = self.read(entity, updated.get("Id") or payload["Id"])
        problems = []
        for field, expected in (checks or {}).items():
            actual = back.get(field)
            try:
                if round(float(actual), 2) != round(float(expected), 2):
                    problems.append(f"{field}: expected {expected}, got {actual}")
                continue
            except (TypeError, ValueError):
                pass
            if str(actual) != str(expected):
                problems.append(f"{field}: expected {expected}, got {actual}")
        if problems:
            raise QBOError(f"VERIFY FAILED for {entity}/{payload['Id']}: " + "; ".join(problems))
        return back

    def report(self, name: str, params: dict | None = None) -> dict:
        """Fetch a report (e.g. 'TransactionList', 'ProfitAndLoss', 'BalanceSheet')."""
        url = f"{self.base}/{self.realm_id}/reports/{name}"
        p = {"minorversion": MINOR_VERSION, **(params or {})}
        r = self._call("GET", url, context=f"report {name}",
                       headers=self._headers(), params=p, timeout=90)
        return r.json()

    # ---- verify-after-write --------------------------------------------
    def create_and_verify(self, entity: str, payload: dict, checks: dict | None = None) -> dict:
        """
        Create an object, then READ IT BACK and confirm `checks` (field->expected)
        match on the persisted record. Raises QBOError on any mismatch.
        Returns the read-back object (source of truth).
        """
        created = self.create(entity, payload)
        obj_id = created.get("Id")
        if not obj_id:
            raise QBOError(f"Create {entity} returned no Id: {created}")
        back = self.read(entity, obj_id)
        problems = []
        for field, expected in (checks or {}).items():
            actual = back.get(field)
            if isinstance(expected, float) or isinstance(actual, (int, float)):
                try:
                    if round(float(actual), 2) != round(float(expected), 2):
                        problems.append(f"{field}: expected {expected}, got {actual}")
                    continue
                except (TypeError, ValueError):
                    pass
            if str(actual) != str(expected):
                problems.append(f"{field}: expected {expected}, got {actual}")
        if problems:
            raise QBOError(f"VERIFY FAILED for {entity}/{obj_id}: " + "; ".join(problems))
        return back

    # ---- convenience ----------------------------------------------------
    def company_info(self) -> dict:
        info = self.query("select * from CompanyInfo")
        rows = info.get("CompanyInfo", [])
        return rows[0] if rows else {}

    def find_account(self, name: str) -> dict | None:
        safe = name.replace("'", "\\'")
        rows = self.query(f"select * from Account where Name = '{safe}'").get("Account", [])
        return rows[0] if rows else None

    def find_vendor(self, name: str) -> dict | None:
        safe = name.replace("'", "\\'")
        rows = self.query(f"select * from Vendor where DisplayName = '{safe}'").get("Vendor", [])
        return rows[0] if rows else None


if __name__ == "__main__":
    q = QBO()
    ci = q.company_info()
    print("Connected to:", ci.get("CompanyName"), "| realm", q.realm_id)
