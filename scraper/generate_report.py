#!/usr/bin/env python3
"""
Generate a one-page installer verification PDF for a California CSLB
license, pulling from cslb_solar.db.

Two things specific to California's data:

1. NO STATUS FIELD EXISTS. The `licenses` table has issue_date and
   expiration_date but nothing else -- no Active/Expired/Suspended
   field the way AZ, TX, and FL all have. This report shows expiration
   date plainly and does NOT compute a guessed Active/Expired label
   from it -- a license past its printed expiration date could still
   be in a renewal grace period, and one within date could have been
   separately suspended. Asserting a status this data doesn't actually
   contain would be a claim this report can't back up.

2. DISCIPLINARY DATA HAS NEVER BEEN POPULATED. The `disciplinary_actions`
   table exists structurally (same schema shape as AZ's) but has zero
   rows across the entire database -- not because CA licensees are
   clean, but because no one has loaded real CSLB disciplinary data
   into it yet. The query below is written correctly so that if real
   data is loaded later this report will pick it up automatically
   without needing any code changes -- but as of this writing, every
   single report will hit the "not yet sourced" branch, and that branch
   says so honestly rather than claiming a clean record.

Also shown: every classification this license actually holds (from the
license_classifications join table), not just C-46. A licensed solar
contractor commonly also holds C-10 (Electrical) or B (General
Building) -- showing the full set is directly relevant to this
project's whole premise that a single classification label doesn't
tell the full story.

Usage:
    python generate_report.py --db cslb_solar.db --license 123456 --out report.pdf
    python generate_report.py --db cslb_solar.db --list
"""

import argparse
import sqlite3
from pathlib import Path

from jinja2 import Template
from weasyprint import HTML

CLASSIFICATION_LABELS = {
    "C46": "C-46 (Solar)",
    "C10": "C-10 (Electrical)",
    "B": "B (General Building)",
    "C20": "C-20 (HVAC)",
    "C39": "C-39 (Roofing)",
    "C36": "C-36 (Plumbing)",
    "A": "A (General Engineering)",
}


def load_context(conn, license_number: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT license_number, business_name, address, city, state, zip_code,
               county, phone_number, issue_date, expiration_date, qualifier_name
        FROM licenses WHERE license_number = ?
        """,
        (license_number,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"License {license_number!r} not found in licenses.")

    (license_number, business_name, address, city, state, zip_code,
     county, phone_number, issue_date, expiration_date, qualifier_name) = row

    cur.execute(
        "SELECT classification_code FROM license_classifications WHERE license_number = ? ORDER BY classification_code",
        (license_number,),
    )
    codes = [r[0] for r in cur.fetchall()]
    classifications = [CLASSIFICATION_LABELS.get(c, c) for c in codes]

    cur.execute(
        "SELECT action_date, description FROM disciplinary_actions WHERE license_number = ? ORDER BY action_date DESC",
        (license_number,),
    )
    disciplinary_records = [{"action_date": r[0], "description": r[1]} for r in cur.fetchall()]

    return {
        "license_number": license_number,
        "business_name": business_name,
        "address": address,
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "county": county,
        "phone_number": phone_number or "Not listed",
        "issue_date": issue_date or "Not available",
        "expiration_date": expiration_date or "Not available",
        "qualifier_name": qualifier_name or "Not listed",
        "classifications": classifications,
        "disciplinary_records": disciplinary_records,
    }


def list_licenses(conn):
    cur = conn.cursor()
    cur.execute("SELECT license_number, business_name, city FROM licenses ORDER BY business_name")
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--license", help="License number to render")
    ap.add_argument("--out", help="Output PDF path")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--template", default=str(Path(__file__).parent / "report_template.html"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    if args.list:
        for license_number, business_name, city in list_licenses(conn):
            print(f"{license_number}\t{business_name}\t{city}")
        return

    if not args.license or not args.out:
        ap.error("--license and --out are required (or use --list)")

    context = load_context(conn, args.license)
    conn.close()

    template = Template(Path(args.template).read_text(encoding="utf-8"))
    html_content = template.render(**context)
    HTML(string=html_content).write_pdf(args.out)
    print(f"Wrote {args.out} for license {args.license} ({context['business_name']})")


if __name__ == "__main__":
    main()
