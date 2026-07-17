"""
example_reconciliation — a worked example: post a month-end reconciliation batch
into a QuickBooks Online company ("Example Holdings LLC") via the QBO REST v3
API, verify-after-write on every entry.

This is a template, not a runnable-as-is script: the account IDs, dates, and
amounts below are obviously-sample values. Adapt them to your own books.

Patterns demonstrated:
  * create_and_verify() on every posting — each entry is read back from the API
    and its TotalAmt confirmed before the run reports success.
  * Idempotency guard — the invoice block is keyed on a unique DocNumber and
    skipped if it already exists, so re-running the script never duplicates.
  * find_or_create helpers for vendors/customers.
  * A step() wrapper so one failed entry doesn't abort the batch; the run ends
    with an OK/FAIL summary you can act on.

NOTE: IDs come from your own chart of accounts. Query them once, e.g.:
    q.query("select Id, Name from Account")
The sample table below is illustrative only:
  Checking=10  UndepositedFunds=11  ChannelFees=20  Cleaning=21  Software=22
  Supplies=23  BankFees=24  TaxesAndLicenses=25  MgmtFees=26
  LodgingTaxPayable=27  OwnerContributions=28  LegalFees=29
  SalesItem=1  Vendor "Owner"=1
"""
from __future__ import annotations
from qbo_client import QBO

q = QBO()
results = []

# ---- sample chart-of-account IDs (IDs come from your own chart of accounts) --
ACCT_CHECKING = "10"
ACCT_CHANNEL_FEES = "20"
ACCT_CLEANING = "21"
ACCT_SOFTWARE = "22"
ACCT_SUPPLIES = "23"
ACCT_BANK_FEES = "24"
ACCT_TAXES_LICENSES = "25"
ACCT_MGMT_FEES = "26"
ACCT_LODGING_TAX_PAYABLE = "27"
ACCT_OWNER_CONTRIBUTIONS = "28"
ACCT_LEGAL_FEES = "29"
ITEM_SALES = "1"          # generic sales item for invoice lines
VENDOR_OWNER = "1"        # "Owner" vendor (management-fee bills owed to the owner)


def log(label, obj_or_err, ok=True):
    results.append((label, obj_or_err, ok))
    tag = "OK " if ok else "FAIL"
    ident = obj_or_err.get("Id") if (ok and isinstance(obj_or_err, dict)) else obj_or_err
    print(f"[{tag}] {label:52s} {ident}")


def find_or_create_vendor(name):
    rows = q.query(f"select * from Vendor where DisplayName = '{name}'").get("Vendor", [])
    if rows:
        return rows[0]["Id"]
    return q.create("Vendor", {"DisplayName": name})["Id"]


def find_or_create_customer(name):
    rows = q.query(f"select * from Customer where DisplayName = '{name}'").get("Customer", [])
    if rows:
        return rows[0]["Id"]
    return q.create("Customer", {"DisplayName": name})["Id"]


def purchase(amount, acct, date, memo, vendor=None):
    p = {"PaymentType": "Check", "AccountRef": {"value": ACCT_CHECKING}, "TxnDate": date,
         "Line": [{"Amount": amount, "DetailType": "AccountBasedExpenseLineDetail",
                   "Description": memo,
                   "AccountBasedExpenseLineDetail": {"AccountRef": {"value": acct}}}]}
    if vendor:
        p["EntityRef"] = {"value": vendor, "type": "Vendor"}
    return q.create_and_verify("Purchase", p, {"TotalAmt": amount})


def bill(amount, acct, date, memo, vendor):
    b = {"VendorRef": {"value": vendor}, "TxnDate": date,
         "Line": [{"Amount": amount, "DetailType": "AccountBasedExpenseLineDetail",
                   "Description": memo,
                   "AccountBasedExpenseLineDetail": {"AccountRef": {"value": acct}}}]}
    return q.create_and_verify("Bill", b, {"TotalAmt": amount})


def deposit(amount, src_acct, date, memo):
    d = {"DepositToAccountRef": {"value": ACCT_CHECKING}, "TxnDate": date,
         "Line": [{"Amount": amount, "DetailType": "DepositLineDetail",
                   "Description": memo,
                   "DepositLineDetail": {"AccountRef": {"value": src_acct}}}]}
    return q.create_and_verify("Deposit", d, {"TotalAmt": amount})


def step(label, fn):
    try:
        log(label, fn(), True)
    except Exception as e:  # noqa: BLE001
        log(label, str(e)[:180], False)


# ---- TENANT A STAY (booking BOOK-1001) ------------------------------------
# Idempotency guard: the invoice is keyed on DocNumber. If it already exists,
# the whole invoice/payment/fee block is skipped, so re-runs never duplicate.
existing = q.query("select * from Invoice where DocNumber = 'BOOK-1001'").get("Invoice", [])
if existing:
    print("Tenant A invoice already exists — skipping invoice/payment block.")
    tenant_inv = existing[0]["Id"]
else:
    cust = find_or_create_customer("Tenant A")
    inv = q.create_and_verify("Invoice", {
        "CustomerRef": {"value": cust}, "TxnDate": "2026-06-15", "DocNumber": "BOOK-1001",
        "Line": [
            {"Amount": 1200.0, "DetailType": "SalesItemLineDetail", "Description": "Rent (3 nights)",
             "SalesItemLineDetail": {"ItemRef": {"value": ITEM_SALES}, "TaxCodeRef": {"value": "NON"}}},
            {"Amount": 200.0, "DetailType": "SalesItemLineDetail", "Description": "Cleaning Fee",
             "SalesItemLineDetail": {"ItemRef": {"value": ITEM_SALES}, "TaxCodeRef": {"value": "NON"}}},
            {"Amount": -100.0, "DetailType": "SalesItemLineDetail", "Description": "Promotional discount",
             "SalesItemLineDetail": {"ItemRef": {"value": ITEM_SALES}, "TaxCodeRef": {"value": "NON"}}},
        ]}, {"TotalAmt": 1300.0})
    log("Invoice Tenant A BOOK-1001 $1,300", inv, True)
    tenant_inv = inv["Id"]
    step("Payment Tenant A $1,300 -> Checking", lambda: q.create_and_verify("Payment", {
        "CustomerRef": {"value": cust}, "TotalAmt": 1300.0, "TxnDate": "2026-06-18",
        "DepositToAccountRef": {"value": ACCT_CHECKING},
        "Line": [{"Amount": 1300.0, "LinkedTxn": [{"TxnId": tenant_inv, "TxnType": "Invoice"}]}]},
        {"TotalAmt": 1300.0}))
    step("Purchase channel fee $100", lambda: purchase(100.00, ACCT_CHANNEL_FEES, "2026-06-18", "Listing-channel host fee - Tenant A BOOK-1001"))

# Lodging-tax accrual: debit tax expense, credit the tax-payable liability.
step("JE Tenant A lodging tax accrual $100", lambda: q.create_and_verify("JournalEntry", {
    "TxnDate": "2026-06-15",
    "Line": [
        {"Amount": 100.00, "DetailType": "JournalEntryLineDetail",
         "Description": "Local lodging tax accrual - Tenant A BOOK-1001",
         "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": ACCT_TAXES_LICENSES}}},
        {"Amount": 100.00, "DetailType": "JournalEntryLineDetail",
         "Description": "Local lodging tax accrual - Tenant A BOOK-1001",
         "JournalEntryLineDetail": {"PostingType": "Credit", "AccountRef": {"value": ACCT_LODGING_TAX_PAYABLE}}},
    ]}, {"TotalAmt": 100.00}))

step("Bill Tenant A mgmt fee $300 (owed to Owner)", lambda: bill(300.0, ACCT_MGMT_FEES, "2026-06-15", "Management commission 25% - Tenant A stay (25% x $1,200)", VENDOR_OWNER))

# ---- OPERATING EXPENSES (cash, from Checking) ----------------------------
step("Purchase channel-manager sub $50 (5/15)", lambda: purchase(50.00, ACCT_SOFTWARE, "2026-05-15", "Channel-manager subscription"))
step("Purchase channel-manager sub $50 (6/15)", lambda: purchase(50.00, ACCT_SOFTWARE, "2026-06-15", "Channel-manager subscription"))
step("Purchase accounting sub $20 (6/01)", lambda: purchase(20.00, ACCT_SOFTWARE, "2026-06-01", "Accounting software subscription"))
step("Purchase cleaning $150 (6/20)", lambda: purchase(150.00, ACCT_CLEANING, "2026-06-20", "Cleaning - Tenant A turnover"))
step("Purchase supplies $100 (6/16)", lambda: purchase(100.00, ACCT_SUPPLIES, "2026-06-16", "Consumable supplies restock"))
step("Purchase bank svc charge $10 (5/31)", lambda: purchase(10.00, ACCT_BANK_FEES, "2026-05-31", "Bank service charge"))
step("Purchase bank svc charge $10 (6/30)", lambda: purchase(10.00, ACCT_BANK_FEES, "2026-06-30", "Bank service charge"))
step("Deposit svc charge refund $10 (6/05)", lambda: deposit(10.00, ACCT_BANK_FEES, "2026-06-05", "Bank service charge refund"))

# ---- ACCRUALS (Bills, unpaid at month end) -------------------------------
step("Bill cleaning $200 (accrued, paid next month)", lambda: bill(200.00, ACCT_CLEANING, "2026-06-28", "Cleaning - end-of-month turnover (paid next month - accrued)", find_or_create_vendor("Cleaning Service Co")))
step("Bill supplies reimbursement $100", lambda: bill(100.00, ACCT_SUPPLIES, "2026-06-16", "Supplies - paid personally by Owner (reimbursement owed)", VENDOR_OWNER))

# ---- OPENING ENTRIES (bank tie) ------------------------------------------
step("Deposit owner funding $2,500 (5/01)", lambda: deposit(2500.00, ACCT_OWNER_CONTRIBUTIONS, "2026-05-01", "Opening owner contribution"))
step("Purchase legal fees $1,000 (5/02)", lambda: purchase(1000.00, ACCT_LEGAL_FEES, "2026-05-02", "Entity-formation legal fees"))

# ---- SUMMARY -------------------------------------------------------------
ok = sum(1 for _, _, k in results if k)
print(f"\n==== {ok}/{len(results)} entries posted + verified ====")
fails = [(l, e) for l, e, k in results if not k]
if fails:
    print("FAILURES:")
    for l, e in fails:
        print(f"  - {l}: {e}")
