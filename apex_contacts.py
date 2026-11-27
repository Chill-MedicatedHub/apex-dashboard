"""
Apex contacts -> Chill Hub (Massachusetts)
------------------------------------------
Reads every buyer contact from Apex - the same list as Apex's Buyers >
Contacts screen - and posts them to the Chill Hub app, where the CRM shows
them on each store's page and in the Contact directory (labelled "Apex").

Uses the scraper's saved Apex login (APEX_COOKIE), like the sales and
inventory pulls: GET /b-api/buyer-contacts/company, 15 contacts a time,
following next_page_url (a cursor) until it comes back empty. Each contact
lists its stores ("buyers", with Apex buyer ids); a contact is filed under
each of them, with the licence and name that store has in the sales data.

The contacts are NOT written to sales_data.json or anywhere in this repo: the
repo and its Actions logs are public, and these are people's emails and phone
numbers. They go straight to the app, which keeps them behind its sign-in.
This script only ever logs counts and field NAMES, never anyone's details.

Settings (environment / GitHub secrets):
    APEX_COOKIE             the saved Apex login the scraper already uses
    CHILL_HUB_URL           https://sales-intelligence.chill-hub.workers.dev
    CHILL_HUB_CONTACTS_KEY  same value as the Worker's CONTACTS_INGEST_KEY secret
Optional:
    APEX_COMPANY_ID         our Apex company id (default 4677)
    APEX_CONTACTS_STATE     default ma
    APEX_CONTACTS_SECONDS   time budget, default 300; slower = keep the old list
    BRAND_DOMAINS           our own email domains, left out of store contacts
"""

import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone

import requests

LIST_URL = os.getenv("APEX_CONTACTS_URL", "https://app.apextrading.com/b-api/buyer-contacts/company")
COOKIE = os.getenv("APEX_COOKIE", "")
COMPANY_ID = os.getenv("APEX_COMPANY_ID", "4677")
STATE = os.getenv("APEX_CONTACTS_STATE", "ma").lower()
HUB_URL = os.getenv("CHILL_HUB_URL", "").rstrip("/")
HUB_KEY = os.getenv("CHILL_HUB_CONTACTS_KEY", "").strip()
TIME_BUDGET = int(os.getenv("APEX_CONTACTS_SECONDS", "300"))
MAX_PAGES = int(os.getenv("APEX_CONTACTS_MAX_PAGES", "2000"))
BRAND_DOMAINS = [d.strip().lower() for d in
                 os.getenv("BRAND_DOMAINS", "medfarms.com,chillmedicated.com").split(",") if d.strip()]


def _xsrf(cookie):
    """The XSRF-TOKEN value from the cookie string (Laravel wants it as a header)."""
    m = re.search(r"(?:^|;\s*)XSRF-TOKEN=([^;]+)", cookie or "")
    return urllib.parse.unquote(m.group(1)) if m else ""


def _headers():
    return {
        "Accept": "application/json",
        "Origin": "https://app.apextrading.com",
        "Referer": "https://app.apextrading.com/buyers",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Cookie": COOKIE,
        "X-XSRF-TOKEN": _xsrf(COOKIE),
        "X-Requested-With": "XMLHttpRequest",
        "current-company-id": str(COMPANY_ID),
    }


def _s(v):
    if v is None or isinstance(v, (dict, list, bool)):
        return ""
    return str(v).strip()


def _is_ours(email):
    at = str(email or "").split("@")
    return len(at) > 1 and at[1].lower() in BRAND_DOMAINS


def _looks_like_phone(v):
    digits = re.sub(r"\D", "", v or "")
    return len(digits) >= 7 and not re.search(r"[A-Za-z]", v or "")


def _get(url, session, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, headers=_headers(), timeout=45)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 + attempt * 3)
            continue
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After") or 10), 60))
            continue
        return r
    return r


def _store_index(payload):
    """Apex buyer id -> {name, license}, from the sales rows just pulled."""
    idx = {}
    for r in (payload or {}).get("rows", []) or []:
        bid = _s(r.get("buyer_id"))
        if bid and bid not in idx:
            idx[bid] = {"name": _s(r.get("buyer_name")), "license": _s(r.get("buyer_license"))}
    return idx


def _records(c, stores):
    """One contact as flat CRM records, one per store it belongs to."""
    if not isinstance(c, dict) or c.get("deleted_at"):
        return []
    first, last = _s(c.get("name")), _s(c.get("lname"))
    phone = _s(c.get("primary_phone")) or _s(c.get("secondary_phone"))
    # Apex makes some contacts from a buyer's user with the phone number as
    # the name; then it's the phone, not a name.
    if first and not last and _looks_like_phone(first):
        phone = phone or first
        first = ""
    name = " ".join(x for x in (first, last) if x)
    email = _s(c.get("email"))
    title = _s(c.get("title"))
    if not (name or email or phone) or _is_ours(email):
        return []
    out = []
    for b in (c.get("buyers") or []):
        if not isinstance(b, dict):
            continue
        bid = _s(b.get("id"))
        known = stores.get(bid, {})
        customer = known.get("name") or _s(b.get("name"))
        license_ = known.get("license") or _s(b.get("license_number") or b.get("license"))
        if not (customer or license_):
            continue
        out.append({"customer_id": "apex:" + bid if bid else "", "customer": customer,
                    "license": license_, "name": name, "title": title, "email": email,
                    "phone": phone, "source": "Apex"})
    return out


def pull(payload=None):
    """(contacts, note). contacts is None when the pull didn't work."""
    started = time.time()
    stores = _store_index(payload)
    session = requests.Session()
    url, pages, contacts_seen, no_store = LIST_URL, 0, 0, 0
    out, seen = [], set()
    while url:
        if time.time() - started > TIME_BUDGET:
            return None, f"Apex was too slow ({pages} pages in {TIME_BUDGET}s); kept the previous contact list."
        if pages >= MAX_PAGES:
            return None, "Too many pages - stopped; kept the previous contact list."
        r = _get(url, session)
        if r.status_code in (401, 403, 419):
            return None, f"Apex refused the saved login ({r.status_code}) - the APEX_COOKIE secret may have expired."
        if r.status_code != 200:
            return None, f"Apex answered {r.status_code} on page {pages + 1}."
        try:
            data = r.json()
        except ValueError:
            return None, "Apex sent something that isn't JSON (signed out?)."
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return None, "Apex's contact list came back in an unexpected shape."
        if pages == 0 and items:
            print(f"  Apex contact fields: {sorted(items[0].keys())}")
            b0 = next((b for c in items for b in (c.get('buyers') or []) if isinstance(b, dict)), None)
            if b0:
                print(f"  Apex contact store fields: {sorted(b0.keys())}")
        pages += 1
        for c in items:
            contacts_seen += 1
            recs = _records(c, stores)
            if not recs and isinstance(c, dict) and not c.get("deleted_at") and not (c.get("buyers") or []):
                no_store += 1
            for rec in recs:
                key = (rec["customer_id"], rec["email"].lower(), rec["name"].lower(), rec["phone"])
                if key not in seen:
                    seen.add(key)
                    out.append(rec)
        nxt = data.get("next_page_url") if isinstance(data, dict) else None
        if not nxt and isinstance(data, dict) and data.get("next_cursor"):
            nxt = LIST_URL + "?cursor=" + urllib.parse.quote(str(data["next_cursor"]))
        url = nxt or None

    matched = sum(1 for x in out if x["license"])
    print(f"  Apex contacts: {contacts_seen} contacts on {pages} pages -> {len(out)} store listings "
          f"({matched} with a licence); {no_store} contact(s) with no store.")
    return out, "ok"


def post(contacts):
    body = {"state": STATE, "source": "Apex",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "contacts": contacts}
    r = requests.post(f"{HUB_URL}/api/feed-contacts", json=body,
                      headers={"X-Contacts-Key": HUB_KEY, "Content-Type": "application/json"}, timeout=90)
    if r.status_code != 200:
        print(f"  Chill Hub: contacts not saved ({r.status_code}): {r.text[:200]}")
        return False
    print(f"  Chill Hub: saved {r.json().get('count')} contact listings.")
    return True


def run(payload=None):
    """Pull and post. Never raises - the sales data must not depend on this."""
    if not COOKIE:
        print("  Apex contacts: no APEX_COOKIE; skipping.")
        return
    if not HUB_URL or not HUB_KEY:
        print("  Apex contacts: CHILL_HUB_URL / CHILL_HUB_CONTACTS_KEY not set; skipping.")
        return
    try:
        contacts, note = pull(payload)
    except Exception as e:
        print(f"  Apex contacts: pull errored ({type(e).__name__}); the app keeps its previous list.")
        return
    if contacts is None:
        print(f"  Apex contacts: {note} The app keeps its previous list.")
        return
    if not contacts:
        print("  Apex contacts: Apex listed no contacts with a store; the app keeps its previous list.")
        return
    try:
        post(contacts)
    except Exception as e:
        print(f"  Apex contacts: could not reach Chill Hub ({type(e).__name__}).")


if __name__ == "__main__":
    run()
