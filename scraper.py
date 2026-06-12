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

# --- Current inventory pull -------------------------------------------------
# Apex exposes current on-hand stock at a separate endpoint than the sales
# report. POST /b-api/inventory/paginated with the same session, plus a
# `current-company-id` header, returns the seller's inventory rows. We filter
# to the Chill Medicated brand and page through everything. ON by default.
INVENTORY_URL = "https://app.apextrading.com/b-api/inventory/paginated"
PULL_INVENTORY = os.getenv("APEX_PULL_INVENTORY", "1") == "1"
BRAND_ID = int(os.getenv("APEX_BRAND_ID", "2500"))
BRAND_NAME = os.getenv("APEX_BRAND_NAME", "Chill Medicated")
INV_PER_PAGE = int(os.getenv("APEX_INV_PER_PAGE", "100"))
INV_MAX_PAGES = int(os.getenv("APEX_INV_MAX_PAGES", "50"))
# Candidate field names for the quantity columns (confirmed/locked on first run
# via the diagnostics this scraper prints). Tried in order; first hit wins.
INV_AVAILABLE_FIELDS = [f.strip() for f in os.getenv(
    "APEX_INV_AVAILABLE_FIELDS",
    "available,available_quantity,available_inventory,quantity_available,quantity,on_hand,on_hand_quantity,inventory_quantity,total_quantity"
).split(",") if f.strip()]
INV_RESERVED_FIELDS = [f.strip() for f in os.getenv(
    "APEX_INV_RESERVED_FIELDS",
    "reserved,reserved_quantity,committed,committed_quantity,allocated,allocated_quantity"
).split(",") if f.strip()]
INV_ONHAND_FIELDS = [f.strip() for f in os.getenv(
    "APEX_INV_ONHAND_FIELDS",
    "on_hand,on_hand_quantity,total_quantity,quantity,quantity_on_hand,inventory_quantity"
).split(",") if f.strip()]


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


def _inv_num(v):
    """Coerce a possibly-stringy/nested numeric value to float, else None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        # Sometimes a qty is nested like {"value": 30} or {"amount": 30}
        for k in ("value", "amount", "quantity", "qty"):
            if k in v:
                return _inv_num(v[k])
        return None
    try:
        s = str(v).replace(",", "").replace("$", "").strip()
        return float(s) if s not in ("", "-") else None
    except (TypeError, ValueError):
        return None


def _pick(row, fields):
    """First recognizable numeric field from a candidate list -> (value, name)."""
    for f in fields:
        if f in row and row[f] is not None:
            v = _inv_num(row[f])
            if v is not None:
                return v, f
    return None, None


def fetch_inventory(xsrf: str) -> dict:
    """Pull current on-hand inventory from Apex's inventory endpoint.

    Returns {"by_sku": {...}, "by_name": {...}, "catalog": [...]}. Defensive:
    any failure leaves the maps empty and the dashboard column shows '—'. Tries
    several candidate field names for available/reserved/on-hand and prints the
    first row's keys so the exact field mapping can be locked on a real run.
    """
    empty = {"by_sku": {}, "by_name": {}, "catalog": []}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://app.apextrading.com",
        "Referer": "https://app.apextrading.com/inventory",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Cookie": COOKIE,
        "X-XSRF-TOKEN": xsrf,
        "X-Requested-With": "XMLHttpRequest",
        # This endpoint requires the active company id as a header.
        "current-company-id": str(COMPANY_ID),
    }

    by_sku, by_name, catalog, seen = {}, {}, [], set()
    avail_hits, sample_keys, total_rows = {}, None, 0
    debug_first_row, debug_first_detail = None, None

    for page in range(1, INV_MAX_PAGES + 1):
        body = {
            "page": page,
            "per-page": INV_PER_PAGE,
            "sort": "name",
            "order": "desc",
            "brand": [{"id": BRAND_ID, "name": BRAND_NAME}],
        }
        try:
            resp = requests.post(INVENTORY_URL, json=body, headers=headers, timeout=60)
        except requests.RequestException as e:
            print(f"  inventory: network error on page {page} ({e}); skipping inventory.")
            break
        if resp.status_code in (401, 403, 419):
            print(f"  inventory: status {resp.status_code} on /inventory/paginated — "
                  f"session/permission issue; skipping inventory (sales unaffected).")
            break
        if resp.status_code != 200:
            print(f"  inventory: unexpected status {resp.status_code}; skipping. "
                  f"{resp.text[:200]}")
            break
        data = resp.json()
        # Response shape unknown — handle the common Laravel/paginator variants.
        items = (data.get("data") if isinstance(data, dict) else None)
        if isinstance(items, dict):  # e.g. {"data": {"data": [...]}}
            items = items.get("data") or items.get("results") or items.get("rows")
        if items is None and isinstance(data, dict):
            items = data.get("results") or data.get("rows") or data.get("inventory")
        if items is None and isinstance(data, list):
            items = data
        if not items:
            break

        for it in items:
            if not isinstance(it, dict):
                continue
            if sample_keys is None:
                sample_keys = sorted(it.keys())
            if debug_first_row is None:
                # One-time debug: capture the first raw list row and its detail
                # response so the exact quantity fields (often nested per-batch)
                # can be confirmed from the committed JSON. Removed once locked.
                debug_first_row = it
                _id = it.get("id") or it.get("product_id") or it.get("inventory_id")
                if _id is not None:
                    try:
                        dr = requests.get(f"https://app.apextrading.com/b-api/inventory/{_id}",
                                          headers=headers, timeout=60)
                        debug_first_detail = (dr.json() if dr.status_code == 200
                                              else {"_status": dr.status_code, "_text": dr.text[:500]})
                    except requests.RequestException as e:
                        debug_first_detail = {"_error": str(e)}
            total_rows += 1
            name = (it.get("product_name") or it.get("name") or it.get("productName")
                    or it.get("title") or "")
            sku = str(it.get("sku") or it.get("product_sku") or it.get("productSku") or "").strip()
            line = (it.get("product_type") or it.get("category") or it.get("product_category")
                    or it.get("type") or "")
            avail, af = _pick(it, INV_AVAILABLE_FIELDS)
            reserved, _ = _pick(it, INV_RESERVED_FIELDS)
            onhand, _ = _pick(it, INV_ONHAND_FIELDS)
            if af:
                avail_hits[af] = avail_hits.get(af, 0) + 1
            if avail is not None:
                if sku:
                    by_sku[sku] = avail
                if name:
                    by_name[name] = avail
            key = sku or name
            if key and key not in seen:
                seen.add(key)
                catalog.append({
                    "name": name or "—", "sku": sku, "line": _name_of(line),
                    "available": avail, "reserved": reserved, "on_hand": onhand,
                })

        # Stop when the page wasn't full (last page) unless the API echoes paging.
        if len(items) < INV_PER_PAGE:
            break

    if catalog or by_sku or by_name:
        print(f"Inventory: {total_rows} '{BRAND_NAME}' rows; {len(catalog)} catalog entries "
              f"(available field: {avail_hits or 'NONE FOUND'}).")
        if not avail_hits:
            print(f"  WARNING: couldn't find an availability field. First row keys: "
                  f"{sample_keys}. Set APEX_INV_AVAILABLE_FIELDS to the right name.")
    else:
        print("  inventory: nothing returned — column will show '—'.")
    return {"by_sku": by_sku, "by_name": by_name, "catalog": catalog,
            "debug": {"first_row": debug_first_row, "first_detail": debug_first_detail}}


def _name_of(v):
    """Unwrap {'name': ...} / {'label': ...} objects to a display string."""
    if isinstance(v, dict):
        return v.get("name") or v.get("label") or v.get("title") or ""
    return v or ""


def main():
    payload = fetch_report()

    # Current inventory (separate Apex endpoint). Non-fatal: if it fails, the
    # sales data still writes and the dashboard inventory section shows '—'.
    if PULL_INVENTORY:
        print("Pulling current inventory...")
        try:
            inv = fetch_inventory(extract_xsrf_token(COOKIE))
        except Exception as e:
            print(f"  inventory pull errored ({e}); continuing without it.")
            inv = {"by_sku": {}, "by_name": {}, "catalog": []}
        by_sku, by_name = inv["by_sku"], inv["by_name"]
        if by_sku or by_name:
            stamped = 0
            for r in payload["rows"]:
                sku = str(r.get("product_sku") or "").strip()
                nm = r.get("product_name") or ""
                cur = (by_sku.get(sku) if sku else None)
                if cur is None and nm:
                    cur = by_name.get(nm)
                if cur is not None:
                    r["current_inventory"] = cur
                    stamped += 1
            print(f"  Stamped current_inventory on {stamped} of {len(payload['rows'])} rows.")
        payload["inventory"] = inv["catalog"]
        # One-time field-discovery aid (safe to leave; small). Lets the raw
        # inventory shape be read from the committed JSON, then removed.
        payload["_inventory_debug"] = inv.get("debug")

    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Saved → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
