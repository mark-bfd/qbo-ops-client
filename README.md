# qbo-ops-client

**Part 2 of a portfolio series: disciplined REST integration — OAuth2 token lifecycle, verify-after-write, idempotent re-runs.**

A small, dependency-light QuickBooks Online (QBO) REST v3 client built for unattended bookkeeping automation, plus a worked example that posts a full month-end reconciliation batch. The point of this repo is not "call an API" — it's the operating discipline around calling an API that moves real money records.

## What it does

- `qbo_client.py` — the client. OAuth2 refresh-token lifecycle, retry/backoff, structured logging with Intuit's `intuit_tid` trace IDs, and `create_and_verify()` / `update_and_verify()` primitives that read every write back before reporting success.
- `qbo_authorize.py` — one-time OAuth2 authorization-code flow with a temporary localhost callback server (CSRF `state` check included). Seeds credentials, then verifies the connection end-to-end.
- `qbo_exchange.py` — alternate one-time setup for production apps, where Intuit requires an HTTPS redirect URI: paste the `code`/`realmId` from your hosted callback page and exchange them for tokens.
- `example_reconciliation.py` — a worked example: invoices, payments, purchases, bills, deposits, and a journal-entry tax accrual posted as one batch, every entry verified, with an idempotency guard so re-runs never duplicate.

## Design rules

1. **API-first.** Every read and write goes through the REST API. No UI assumptions, no "it probably posted".
2. **Verify-after-write.** A write is not done until the object has been read back from the API and its key fields (e.g. `TotalAmt`) confirmed against what was intended. `create_and_verify()` raises on any mismatch and returns the *read-back* object as the source of truth.
3. **Idempotent re-runs.** Batch scripts key their entries on natural unique identifiers (invoice `DocNumber`, vendor `DisplayName`) and skip what already exists. A crashed run is safely re-runnable.
4. **Fail loud, fail traceable.** Every API call logs its `intuit_tid`; every raised error carries it, so any failure is directly shareable with Intuit support.
5. **Resilient transport.** Exponential backoff on network errors, 429 throttling, and 5xx — auth calls included.
6. **Self-updating endpoints.** OAuth endpoints come from Intuit's OIDC discovery document at runtime, with pinned well-known values as fallback.

## Architecture

```
qbo_authorize.py / qbo_exchange.py     (one-time: auth-code -> refresh token)
              |
              v
        OS keyring / env vars          (client_id, client_secret,
              |                         refresh_token, realm_id)
              v
         qbo_client.QBO                (refresh -> short-lived access token,
              |                         retry/backoff, tid logging)
              v
   QuickBooks Online REST v3 API
              ^
              |  create -> read-back -> field checks
   example_reconciliation.py           (idempotent batch posting)
```

Token lifecycle: access tokens live ~1 hour and are refreshed on demand with a 90-second safety margin. Intuit rotates refresh tokens periodically; whatever comes back from a refresh is persisted immediately, so the stored token never goes stale.

## Setup

```bash
pip install requests keyring
```

Credentials resolve **env-first**, then from the OS keyring (service `qbo-client`):

| Env var | Purpose |
|---|---|
| `QBO_CLIENT_ID` / `QBO_CLIENT_SECRET` | App credentials from developer.intuit.com |
| `QBO_REFRESH_TOKEN` | Long-lived refresh token (seeded by the authorize scripts) |
| `QBO_REALM_ID` | Company (realm) ID, captured during authorization |
| `QBO_REDIRECT_URI` | HTTPS redirect URI for `qbo_exchange.py` (production apps) |
| `QBO_KEYRING_SERVICE` | Optional: override the keyring service name |

One-time authorization (development, localhost redirect):

```bash
python qbo_authorize.py <CLIENT_ID> <CLIENT_SECRET> [--sandbox]
```

Then:

```python
from qbo_client import QBO

q = QBO()
print(q.company_info())   # connectivity check
q.create_and_verify("Purchase", payload, checks={"TotalAmt": 64.95})
```

## Usage example

`example_reconciliation.py` shows the full pattern on generic entities ("Example Holdings LLC", "Tenant A", an "Owner" vendor). All account IDs in it are samples — IDs come from your own chart of accounts (`q.query("select Id, Name from Account")`). Output is a per-entry OK/FAIL ledger and a summary:

```
[OK ] Invoice Tenant A BOOK-1001 $1,300                  145
[OK ] Payment Tenant A $1,300 -> Checking                146
...
==== 15/15 entries posted + verified ====
```

## Stack

- Python 3.10+ (`from __future__ import annotations`, `X | None` unions)
- `requests` for HTTP, `keyring` for OS-native credential storage
- QuickBooks Online REST v3, minor version 75; OAuth2 authorization-code + refresh-token grants

## License

MIT — see [LICENSE](LICENSE).
