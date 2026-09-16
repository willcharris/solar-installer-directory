#!/usr/bin/env python3
"""
Generate a one-page installer verification PDF for a Texas TDLR Solar
Residential Retailer license, pulling from tx_tdlr_solar.db (built by
texas/load_tdlr_solar.py).

IMPORTANT SCOPE NOTE, carried into every report this generates: the
"Solar Residential Retailer" registration (created by SB 1036 / the
Residential Solar Retailer Regulatory Act) governs the SALES/LEASE
TRANSACTION -- misleading claims, right-to-cancel, contract disclosures.
It is explicitly NOT an installation-competency license, and licensed
Electrical Contractors (who do the actual installation work under a
separate TDLR licensing act) are exempt from registering as retailers
at all. So this report verifies sales-conduct registration only -- it
does NOT verify that whoever physically installs the panels holds a
valid electrical contractor license. That's a real, separate TDLR
dataset this pipeline has not yet integrated -- the report says so
plainly rather than implying full installer verification.

Also unresolved as of this writing: TDLR's export currently shows every
one of 56 retailers as "Current" (the program launched August 2026, so
this may simply be too new for anyone to have lapsed) -- unlike AZ,
this loader does not yet detect a retailer silently disappearing from
a future snapshot. Treat "Current" as what TDLR published on the
retrieval date, not a guarantee nothing has changed since.

Usage:
    python generate_report.py --db tx_tdlr_solar.db --license 1 --out report.pdf
    python generate_report.py --db tx_tdlr_solar.db --list
"""

import argparse
import sqlite3
from pathlib import Path

from jinja2 import Template
from weasyprint import HTML

# Only "Current" has actually been observed in live data as of this
# writing (56/56 rows). Other values are TDLR-plausible guesses, not
# confirmed -- anything not in this map falls back to "unknown" styling
# rather than crashing or mis-coloring a status we haven't seen yet.
STATUS_CLASS_MAP = {
    "Current": "current",
    "Active": "current",
    "Expired": "expired",
    "Revoked": "revoked",
    "Suspended": "revoked",
    "Cancelled": "expired",
    "Inactive": "expired",
}


def load_retailer_context(conn, license_number: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT license_number, business_name, address1, address2, city, state,
               zip_code, county, phone, expiration_date, first_seen_at, last_checked_at
        FROM tx_solar_retailers WHERE license_number = ?
        """,
        (license_number,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"License {license_number!r} not found in tx_solar_retailers.")

    (license_number, business_name, address1, address2, city, state,
     zip_code, county, phone, expiration_date, first_seen_at, last_checked_at) = row

    cur.execute(
        """
        SELECT status, scraped_at FROM tx_solar_retailer_snapshots
        WHERE license_number = ? ORDER BY scraped_at DESC LIMIT 1
        """,
        (license_number,),
    )
    snap = cur.fetchone()
    status, last_snapshot_at = (snap[0], snap[1]) if snap else (None, None)

    status_class = STATUS_CLASS_MAP.get(status, "unknown")
    status_label = status or "Unknown"

    return {
        "license_number": license_number,
        "business_name": business_name,
        "address1": address1,
        "address2": address2,
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "county": county,
        "phone": phone,
        "expiration_date": expiration_date,
        "first_seen_at": (first_seen_at or "")[:10],
        "last_snapshot_at": (last_snapshot_at or "")[:10],
        "status_class": status_class,
        "status_label": status_label,
    }


def list_retailers(conn):
    cur = conn.cursor()
    cur.execute("SELECT license_number, business_name, city FROM tx_solar_retailers ORDER BY business_name")
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--license", help="License number to render")
    ap.add_argument("--out", help="Output PDF path")
    ap.add_argument("--list", action="store_true", help="List all retailer license numbers and exit")
    ap.add_argument(
        "--template",
        default=str(Path(__file__).parent / "report_template.html"),
    )
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    if args.list:
        for license_number, business_name, city in list_retailers(conn):
            print(f"{license_number}\t{business_name}\t{city}")
        return

    if not args.license or not args.out:
        ap.error("--license and --out are required (or use --list)")

    context = load_retailer_context(conn, args.license)
    conn.close()

    template = Template(Path(args.template).read_text(encoding="utf-8"))
    html_content = template.render(**context)

    HTML(string=html_content).write_pdf(args.out)
    print(f"Wrote {args.out} for license {args.license} "
          f"({context['business_name']}, status: {context['status_label']})")


if __name__ == "__main__":
    main()
