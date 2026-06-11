"""
Apex Trading Sales Report Scraper
----------------------------------
Pulls the "All Product Sales Report" from Apex Trading using your active
browser session cookie. Saves the result as sales_data.json for the dashboard.

USAGE:
    1. Open .env (or edit the constants below) and paste your Cookie value.
    2. Run:  python scraper.py
    3. Output: ./sales_data.json  (the dashboard reads this)

To refresh data automatically, schedule this script with cron (macOS/Linux)
or Task Scheduler (Windows). See README.md for the exact commands.
"""

import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
load_dotenv()

# Endpoint discovered from the Apex web app's network traffic
API_URL = "https://app.apextrading.com/b-api/reporting/run-all-product-sales-report"

# These come from your Apex account (visible in the request payload)
USER_ID = int(os.getenv("APEX_USER_ID", "10676"))
COMPANY_ID = int(os.getenv("APEX_COMPANY_ID", "4677"))

# The session cookie from your logged-in browser. NEVER commit this to git.
COOKIE = os.getenv("APEX_COOKIE", "")

# How many rows to pull per request. Apex's UI defaults to 50; we bump it.
ROW_LIMIT = int(os.getenv("APEX_ROW_LIMIT", "5000"))

# Date range for the pull.
# Preferred: set APEX_FROM_DATE to a fixed start date (e.g. "2025-05-01").
# Fallback: if not set, use rolling APEX_DAYS_BACK window (default 90).
FROM_DATE_FIXED = os.getenv("APEX_FROM_DATE", "")
DAYS_BACK = int(os.getenv("APEX_DAYS_BACK", "90"))

OUTPUT_FILE = Path(__file__).parent / "sales_data.json"


# ----------------------------------------------------------------------------
# Build the request payload (mirrors what the Apex UI sends)
# ----------------------------------------------------------------------------
def build_payload(from_date: str, to_date: str) -> dict:
    """Construct the JSON body the report endpoint expects."""
    return {
        "name": None,
        "userId": USER_ID,
        "companyId": COMPANY_ID,
        "selectedLimit": str(ROW_LIMIT),
        "dataExportType": "json",
        "isNewReportDefault": False,
        # Enable every product category so nothing is filtered out.
        "categoriesEnabled": {
            "flower": True,
            "plantMaterial": True,
            "prepack": True,
            "preroll": True,
            "seed": True,
            "clone": True,
            "concentrate": True,
            "edible": True,
            "topical": True,
            "tincture": True,
            "vape": True,
            "accessory": True,
            "other": True,
        },
        # Enable every column we want back in the response.
        "columnsEnabled": {
            "industry": True,
            "product_name": True,
            "product_category": True,
            "product_type": True,
            "operation": True,
            "brand": True,
            "batch_name": True,
            "batch_cost_of_goods": True,
            "batch_production_date": True,
            "batch_best_by_date": True,
            "buyer_name": True,
            "buyer_license": True,
            "buyer_state": True,
            "buyer_city": True,
            "buyer_group": True,
            "seller_license": True,
            "sales_rep": True,
            "order_id": True,
            "order_number": True,
            "order_date": True,
            "delivery_date": True,
            "payment_status": True,
            "payment_date": True,
            "quantity": True,
            "unit_price": True,
            "computed_sale_price": True,
            "discounts": True,
            "additional_discounts": True,
            "tax": True,
            "total": True,
        },
        # The actual filter criteria
        "reportQuery": {
            "operations": [],
            # Filter to only the Chill Medicated brand (id 2500 from Apex)
            "brands": [{"id": 2500, "name": "Chill Medicated"}],
            "salesReps": [],
            "categories": [],
            "buyers": [],
            "paymentStatus": [],
            "fromDate": from_date,
            "toDate": to_date,
            "parentOrderStatuses": [],
            "paymentReceivedFromDate": None,
            "paymentReceivedToDate": None,
            "deliveryFromDate": None,
            "deliveryToDate": None,
            "withinLastCount": None,
            "withinLastType": None,
            "timeZone": "America/New_York",
        },
    }


# ----------------------------------------------------------------------------
# Main pull
# ----------------------------------------------------------------------------
def extract_xsrf_token(cookie_str: str) -> str:
    """
    Apex uses Laravel-style CSRF protection: the XSRF-TOKEN cookie value must
    be sent back as the X-XSRF-TOKEN header (URL-decoded once).
    """
    for part in cookie_str.split(";"):
        part = part.strip()
        if part.startswith("XSRF-TOKEN="):
            raw = part[len("XSRF-TOKEN="):]
            # The cookie is URL-encoded; Laravel expects it decoded once.
            return urllib.parse.unquote(raw)
    return ""


def fetch_report() -> dict:
    if not COOKIE:
        print("ERROR: APEX_COOKIE is empty.")
        print("Open the .env file and paste your session cookie. See README.md.")
        sys.exit(1)

    xsrf = extract_xsrf_token(COOKIE)
    if not xsrf:
        print("WARNING: No XSRF-TOKEN found in your cookie string.")
        print("Make sure you copied the ENTIRE Cookie value, including the")
        print("'XSRF-TOKEN=...' part. Re-grab the cookie and try again.")
        sys.exit(1)

    today = datetime.now()
    if FROM_DATE_FIXED:
        # Use the configured fixed start date (e.g. "2025-05-01")
        from_date = FROM_DATE_FIXED
    else:
        # Fall back to rolling window
        from_date = (today - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    # Apex's report treats `toDate` as exclusive of that day, so using today's
    # date drops same-day orders. Add one day so today is fully included.
    to_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"Pulling sales from {from_date} → {to_date} (limit {ROW_LIMIT} rows)...")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://app.apextrading.com",
        "Referer": "https://app.apextrading.com/reports/all-product-sales",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Cookie": COOKIE,
        "X-XSRF-TOKEN": xsrf,
        "X-Requested-With": "XMLHttpRequest",
    }

    payload = build_payload(from_date, to_date)

    try:
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=60)
    except requests.RequestException as e:
        print(f"ERROR: network failure — {e}")
        sys.exit(1)

    if resp.status_code in (401, 403):
        print(f"ERROR: Apex rejected the request (status {resp.status_code}).")
        print("Your session cookie has likely expired. Log into Apex in your")
        print("browser, copy a fresh Cookie value into .env, and re-run.")
        sys.exit(1)

    if resp.status_code == 419:
        print("ERROR: CSRF token mismatch (status 419).")
        print("Your XSRF-TOKEN doesn't match the session. This usually means:")
        print(" 1. You copied the cookie from one tab and the session shifted, or")
        print(" 2. You copied an old cookie. Re-grab a fresh Cookie value")
        print("    from a request you JUST ran in the Apex UI, then re-run.")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"ERROR: unexpected status {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)

    data = resp.json()
    rows = data.get("data", {}).get("reportData", [])
    print(f"Fetched {len(rows)} rows.")

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "from_date": from_date,
        "to_date": to_date,
        "row_count": len(rows),
        "rows": rows,
    }


def main():
    payload = fetch_report()
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Saved → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
