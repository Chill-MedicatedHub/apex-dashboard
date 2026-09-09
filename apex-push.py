"""
Push Apex data to Chill Desk after every scrape.

WHERE THIS GOES
---------------
Add these two lines to apex-dashboard/.env:

    CHILL_DESK_URL=https://chill-desk.onrender.com
    CHILL_INGEST_KEY=chill_ingest_8W5RvaTOSzfDQDaIqIlXf-3CqZ5DlwvN
    CHILL_MARKET=MI                          (MI, MA, MO or NJ — which state this repo covers)

The same ingest key works for all four repos. CHILL_MARKET is what keeps them
apart: it tags every row so Chill Desk can group retailers by state. Apex rows
usually carry a state already and that wins; CHILL_MARKET is the fallback, and
it is the only thing that places the LeafLink rows, which have no state field.

Then paste the push_to_chill() function below into scraper.py, and add one
line at the end of main(), right after the file is written:

    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Saved → {OUTPUT_FILE}")
    push_to_chill(payload)          # <-- add this

For the New Jersey repo, which pulls LeafLink rather than Apex, the call is
the same — just make sure CHILL_MARKET=NJ is in that repo's .env (or its
GitHub Actions secrets, since that one runs on a schedule):

    push_to_chill(payload, source="leaflink")

That's it. Every scrape now lands in Chill Desk, and every partner's
Purchase history page updates with it.

If the push fails, the scrape still succeeds and the dashboard still works —
it prints a warning and moves on. Getting data into Chill Desk should never
be the reason your own dashboard breaks.
"""

import os
import requests


def push_to_chill(payload, market=None, source="apex"):
    """Send the scraped rows to Chill Desk. Never raises."""
    url = os.getenv("CHILL_DESK_URL", "").rstrip("/")
    key = os.getenv("CHILL_INGEST_KEY", "")
    market = market or os.getenv("CHILL_MARKET", "")

    if not url or not key:
        print("  Chill Desk: not configured (set CHILL_DESK_URL and CHILL_INGEST_KEY); skipping.")
        return

    rows = payload.get("rows", [])
    if not rows:
        print("  Chill Desk: no rows to push; skipping.")
        return

    try:
        resp = requests.post(
            f"{url}/api/ingest/orders",
            json={"rows": rows, "market": market, "source": source},
            headers={"X-Ingest-Key": key, "Content-Type": "application/json"},
            timeout=180,
        )
    except requests.RequestException as e:
        print(f"  Chill Desk: could not reach the server ({e}). Data is still saved locally.")
        return

    if resp.status_code == 401:
        print("  Chill Desk: ingest key rejected. Generate a new one in Partner sales")
        print("             and update CHILL_INGEST_KEY in .env.")
        return

    if resp.status_code != 200:
        print(f"  Chill Desk: server returned {resp.status_code} — {resp.text[:200]}")
        return

    r = resp.json()
    print(f"  Chill Desk: pushed {r['imported']} rows; "
          f"{r['matched']} of {r['licences']} licences matched a partner account.")

    by_state = r.get("markets") or {}
    if by_state:
        print("  Chill Desk: " + ", ".join(f"{m} {n}" for m, n in by_state.items()))
    if r.get("noMarket"):
        print(f"  Chill Desk: {r['noMarket']} row(s) had no state — set CHILL_MARKET in .env.")

    unmatched = r.get("unmatched") or []
    if unmatched:
        print(f"  Chill Desk: {len(unmatched)} licence(s) have sales but no account yet —")
        for u in unmatched[:5]:
            print(f"               {u.get('buyer') or '?'}  ({u['license']}, {u['rows']} rows)")
        if len(unmatched) > 5:
            print(f"               …and {len(unmatched) - 5} more. See Partner sales.")
