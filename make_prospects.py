#!/usr/bin/env python3
"""
make_prospects.py — build prospects.json for the MA dashboard's Prospecting tab.

Input:  the Cannabis Control Commission "licenses that commenced operations" CSV
        (https://masscannabiscontrol.com/resource/l_licenses_commence_ops.csv)
        The "All licenses" export (l_licenses_all_details_public.csv) also works.

Output: prospects.json — active Marijuana Retailers (+ Medical Treatment Centers),
        one record per license. The dashboard matches these against your book by
        LICENSE_NUMBER and shows only the ones you have never invoiced.

Usage:  python make_prospects.py <ccc_export.csv> [prospects.json]
"""
import csv, json, sys, datetime

# License types that actually buy finished product to resell.
KEEP_TYPES = {"Marijuana Retailer", "Medical Marijuana Treatment Center"}

def pick(row, *names):
    """First non-empty value among the given column names."""
    for n in names:
        v = (row.get(n) or "").strip()
        if v:
            return v
    return ""

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "prospects.json"

    with open(src, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    seen, records = set(), []
    for r in rows:
        ltype = pick(r, "LICENSE_TYPE", "APPROVED_LICENSE_TYPE")
        if KEEP_TYPES and ltype not in KEEP_TYPES:
            continue
        status = pick(r, "LICENSE_STATUS")
        if status and status.lower() != "active":
            continue
        lic = pick(r, "LICENSE_NUMBER", "LICENSE_NUMBER_BASE").upper()
        if not lic or lic in seen:
            continue
        seen.add(lic)
        records.append({
            "name":    pick(r, "BUSINESS_NAME"),
            "license": lic,
            "type":    ltype,
            "status":  status or "Active",
            "city":    pick(r, "BUSINESS_CITY", "PHYSICAL_CITY", "CITY"),
            "county":  pick(r, "county"),
            "address": pick(r, "BUSINESS_ADDRESS_1", "PHYSICAL_ADDRESS_1", "ADDRESS_1"),
            "zip":     pick(r, "BUSINESS_ZIP_CODE", "PHYSICAL_ZIP_CODE", "ZIP_CODE"),
        })

    records.sort(key=lambda x: (x["city"], x["name"]))
    payload = {
        "as_of": datetime.date.today().isoformat(),
        "source": "MA Cannabis Control Commission open data — " + src.split("/")[-1],
        "count": len(records),
        "records": records,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {out}: {len(records)} active retailer licenses "
          f"(from {len(rows)} rows in {src}).")

if __name__ == "__main__":
    main()
