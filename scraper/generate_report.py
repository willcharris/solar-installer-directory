#!/usr/bin/env python3
"""
Generate a one-page installer verification PDF for a California CSLB
license, pulling from cslb_solar.db (built by ingest_master.py from
CSLB's statewide License Master file).

STATUS: this data has real PrimaryStatus/SecondaryStatus fields (an
earlier version of this pipeline used a data source that lacked status
entirely -- that limitation is gone now that ingest_master.py loads
the full statewide file). Verified against the real distinct values in
this data (not guessed):
  - PrimaryStatus "CLEAR" with no secondary flag -> Active (the vast
    majority, 1,099 of 1,192 C-46 licenses)
  - PrimaryStatus "CLEAR" WITH a secondary flag (e.g. "Pending Case/CIT",
    "7073E Probation", "WC Susp Pending") -> shown as flagged, not a
    plain "Active" badge -- a pending suspension or probation status
    riding on an otherwise-clear primary status is exactly the kind of
    thing this report exists to surface, not hide.
  - PrimaryStatus containing "Susp" -> Suspended

Also shown: every classification this license actually holds (from the
license_classifications join table), not just C-46. A licensed solar
contractor commonly also holds C-10 (Electrical) or B (General
Building) -- showing the full set is directly relevant to this
project's whole premise that a single classification label doesn't
tell the full story.

DISCIPLINARY DATA: the disciplinary_actions table (complaint-level
detail, case numbers) has not been populated -- the status flags above
are real, but a complaint/case-history source is a separate, still-open
gap. The report says so honestly rather than implying a full check.

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
    "C-46": "C-46 (Solar)",
    "C-10": "C-10 (Electrical)",
    "B": "B (General Building)",
    "C-20": "C-20 (HVAC)",
    "C-39": "C-39 (Roofing)",
    "C-36": "C-36 (Plumbing)",
    "A": "A (General Engineering)",
}


def classify_status(primary: str, secondary: str):
    """Returns (label, css_class). Based on the real confirmed value
    distribution -- see module docstring.

    IMPORTANT: only PRIMARY status is checked for an actual suspension.
    Real confirmed data shows genuine suspensions always appear as the
    primary value ("Contr Bond Susp", "Work Comp Susp", etc.). A
    SECONDARY flag containing "Susp" (e.g. "WC Susp Pending") describes
    a suspension that is pending, not yet in effect -- treating that as
    an active "Suspended" label would overstate what CSLB is actually
    saying, which is exactly the kind of false claim this report exists
    to avoid making.
    """
    primary = (primary or "").strip()
    secondary = (secondary or "").strip()
    if "Susp" in primary:
        return f"Suspended ({primary})", "suspended"
    if primary == "CLEAR" and not secondary:
        return "Active", "active"
    if primary == "CLEAR" and secondary:
        readable = "; ".join(s.strip() for s in secondary.split("|") if s.strip())
        return readable, "flagged"
    if not primary:
        return "Unknown", "unknown"
    return f"{primary}" + (f" / {secondary}" if secondary else ""), "unknown"


def load_context(conn, license_number: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT license_number, business_name, address, city, state, zip_code,
               county, phone_number, issue_date, expiration_date, qualifier_name,
               primary_status, secondary_status
        FROM licenses WHERE license_number = ?
        """,
        (license_number,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"License {license_number!r} not found in licenses.")

    (license_number, business_name, address, city, state, zip_code,
     county, phone_number, issue_date, expiration_date, qualifier_name,
     primary_status, secondary_status) = row

    status_label, status_class = classify_status(primary_status, secondary_status)

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
        "status_label": status_label,
        "status_class": status_class,
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
    ap.add_argument("--bulk-out", help="Directory to write one PDF per license (all ~1,082 -- this is the slowest of the five)")
    ap.add_argument("--template", default=str(Path(__file__).parent / "report_template.html"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    if args.list:
        for license_number, business_name, city in list_licenses(conn):
            print(f"{license_number}\t{business_name}\t{city}")
        return

    template = Template(Path(args.template).read_text(encoding="utf-8"))

    if args.bulk_out:
        out_dir = Path(args.bulk_out)
        out_dir.mkdir(parents=True, exist_ok=True)
        license_numbers = [r[0] for r in list_licenses(conn)]
        print(f"Generating {len(license_numbers)} reports into {out_dir}/ ...")
        for i, license_number in enumerate(license_numbers, 1):
            context = load_context(conn, license_number)
            html_content = template.render(**context)
            HTML(string=html_content).write_pdf(out_dir / f"{license_number}.pdf")
            if i % 50 == 0 or i == len(license_numbers):
                print(f"  {i}/{len(license_numbers)}")
        print(f"Done. Wrote {len(license_numbers)} PDFs to {out_dir}/")
        return

    if not args.license or not args.out:
        ap.error("--license and --out are required (or use --list / --bulk-out)")

    context = load_context(conn, args.license)
    conn.close()

    html_content = template.render(**context)
    HTML(string=html_content).write_pdf(args.out)
    print(f"Wrote {args.out} for license {args.license} ({context['business_name']})")


if __name__ == "__main__":
    main()
