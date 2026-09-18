#!/usr/bin/env python3
"""
Insert the manually-reviewed CA matches (found by match_statewide.py,
confirmed correct by eye -- exact or near-exact name AND city match)
into the main `licenses` table, pulling their full details from the
original MasterLicenseData.csv.

Run this once, after ingest_master.py, before regenerating reports.

Usage:
    python apply_approved_ca_matches.py --master MasterLicenseData.csv --db cslb_solar.db
"""

import argparse
import csv
import re
import sqlite3
from datetime import datetime, timezone

# License numbers manually reviewed and confirmed from match_statewide.py's
# output -- each is an exact or near-exact name match AND city match (or
# same metro area), holding C-10 and/or B but not C-46. Two other
# high-confidence matches from that same pass (Five Star Solar -> Five
# Star Electric in a different city 60 miles away; North Valley Solar
# Power -> North Valley Services in a different city 120 miles away,
# under an unrelated C-12 Earthwork classification) were deliberately
# EXCLUDED as likely coincidental name collisions. Do not add those
# without independently re-verifying each one.
APPROVED_LICENSE_NUMBERS = {
    "1013576",  # Solectric -> SOLECTRIC CORP (El Dorado Hills, same metro as Sacramento candidate)
    "1080746",  # Continuum -> CONTINUUM (Sacramento, exact city match)
    "1086763",  # TN Electrical and Solar Services (Milpitas, exact match)
    "855672",   # Green Leaf Solar & Electric (San Jose, exact match)
    "1024460",  # Better Earth Solar -> BETTER EARTH ELECTRIC INC (Cerritos, exact city match)
    "1025073",  # Fuzion Energy (Bakersfield, exact match)
    "1053172",  # Ameco Solar LLC (Valley Village, exact match -- license number independently
                # confirmed against a third-party article mentioning this same number)
    "1008374",  # Sunlux (Corona, exact match)
}


def parse_classifications(raw: str):
    codes = []
    for part in (raw or "").split("|"):
        code = part.strip()
        if not code:
            continue
        m = re.match(r"^([A-Z]+)-?(\d+)$", code)
        if m:
            code = f"{m.group(1)}-{m.group(2)}"
        codes.append(code)
    return codes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", required=True)
    ap.add_argument("--db", default="cslb_solar.db")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).isoformat()
    found = []
    with open(args.master, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["LicenseNo"] in APPROVED_LICENSE_NUMBERS:
                found.append(r)

    print(f"Found {len(found)} of {len(APPROVED_LICENSE_NUMBERS)} approved license numbers in {args.master}")
    if len(found) < len(APPROVED_LICENSE_NUMBERS):
        missing = APPROVED_LICENSE_NUMBERS - {r["LicenseNo"] for r in found}
        print(f"  MISSING: {missing} -- check these weren't dropped in a newer CSLB export")

    conn = sqlite3.connect(args.db)
    for r in found:
        conn.execute(
            "INSERT OR REPLACE INTO licenses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["LicenseNo"], r["BusinessType"], r["BusinessName"], r["MailingAddress"],
                r["City"], r["State"], r["ZIPCode"], r["County"], r["BusinessPhone"],
                r["IssueDate"], r["ExpirationDate"], "",
                r["PrimaryStatus"].strip(), r["SecondaryStatus"].strip(), now, now,
            ),
        )
        conn.execute("DELETE FROM license_classifications WHERE license_number = ?", (r["LicenseNo"],))
        for code in parse_classifications(r["Classifications(s)"]):
            conn.execute("INSERT INTO license_classifications VALUES (?, ?)", (r["LicenseNo"], code))
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
    print(f"Inserted/updated {len(found)} rows. licenses table now has {total:,} total rows.")
    conn.close()


if __name__ == "__main__":
    main()
