#!/usr/bin/env python3
"""
Generate a one-page installer verification PDF for a given AZ ROC license
number, pulling from az_roc.db (built by pipelines/az/ingest.py).

Handles two real cases found in the actual data:
  1. A license present in the active roster (`licenses` table) -- the
     common case.
  2. A license that ONLY exists in `disciplinary_actions` because it was
     revoked/suspended and has since dropped out of ROC's active roster
     entirely (confirmed this happens: SunDial Solar, Verde Solaris,
     Envision Solar, and others are real examples). The report still
     renders in this case, using whatever the disciplinary ledger has,
     and says plainly that the license isn't in the current roster.

Usage:
    python generate_report.py --db az_roc.db --license 367457 --out report.pdf
"""

import argparse
import sqlite3
from pathlib import Path

from jinja2 import Template
from weasyprint import HTML

STATUS_CLASS_MAP = {
    "Active": "active",
    "Revoked": "revoked",
    "Suspended": "suspended",
}


def get_latest_snapshot_date(conn) -> str:
    cur = conn.cursor()
    cur.execute("SELECT MAX(snapshot_date) FROM ingest_log")
    row = cur.fetchone()
    return row[0] if row and row[0] else "unknown"


def load_license_context(conn, license_no: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT license_no, business_name, dba, class_detail, city, state,
               issued_date, expiration_date, qualifying_party, status
        FROM licenses WHERE license_no = ?
        """,
        (license_no,),
    )
    row = cur.fetchone()

    cur.execute(
        """
        SELECT description, case_number, license_class, first_seen_snapshot
        FROM disciplinary_actions
        WHERE license_no = ?
        ORDER BY first_seen_snapshot DESC
        """,
        (license_no,),
    )
    disciplinary_records = [
        {
            "description": d[0],
            "case_number": d[1],
            "license_class": d[2],
            "first_seen_snapshot": d[3],
        }
        for d in cur.fetchall()
    ]

    if row:
        (license_no, business_name, dba, class_detail, city, state,
         issued_date, expiration_date, qualifying_party, status) = row
        not_in_active_roster = False
    else:
        cur.execute(
            """
            SELECT business_name, dba, city, state, license_class
            FROM disciplinary_actions WHERE license_no = ?
            ORDER BY first_seen_snapshot DESC LIMIT 1
            """,
            (license_no,),
        )
        fallback = cur.fetchone()
        if not fallback:
            raise ValueError(
                f"License {license_no} not found in the roster OR the "
                "disciplinary ledger -- check the license number."
            )
        business_name, dba, city, state, class_detail = fallback
        issued_date = expiration_date = qualifying_party = None
        descriptions = {d["description"] for d in disciplinary_records}
        status = "Revoked" if "Revoked" in descriptions else (
            disciplinary_records[0]["description"] if disciplinary_records else "Unknown"
        )
        not_in_active_roster = True

    status_class = STATUS_CLASS_MAP.get(status, "unknown")
    status_label = status if status in STATUS_CLASS_MAP else f"Status: {status}"

    return {
        "license_no": license_no,
        "business_name": business_name,
        "dba": dba,
        "class_detail": class_detail,
        "city": city,
        "state": state,
        "issued_date": issued_date,
        "expiration_date": expiration_date,
        "qualifying_party": qualifying_party,
        "status_class": status_class,
        "status_label": status_label,
        "not_in_active_roster": not_in_active_roster,
        "disciplinary_records": disciplinary_records,
        "snapshot_date": get_latest_snapshot_date(conn),
    }


def publishable_license_numbers(conn):
    """The same set build_site.py's load_az() shows on the live site --
    bulk generation should never produce a PDF for a business that isn't
    even linkable from the site yet."""
    cur = conn.cursor()
    cur.execute("SELECT license_no FROM licenses WHERE is_solar_relevant = 1 AND status = 'Active'")
    return [r[0] for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--license", help="License number to render")
    ap.add_argument("--out", help="Output PDF path")
    ap.add_argument(
        "--bulk-out",
        help="Directory to write one PDF per publishable license (same set shown on the live site)",
    )
    ap.add_argument(
        "--template",
        default=str(Path(__file__).parent / "report_template.html"),
    )
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    template = Template(Path(args.template).read_text(encoding="utf-8"))

    if args.bulk_out:
        out_dir = Path(args.bulk_out)
        out_dir.mkdir(parents=True, exist_ok=True)
        license_numbers = publishable_license_numbers(conn)
        print(f"Generating {len(license_numbers)} reports into {out_dir}/ ...")
        for i, license_no in enumerate(license_numbers, 1):
            context = load_license_context(conn, license_no)
            html_content = template.render(**context)
            HTML(string=html_content).write_pdf(out_dir / f"{license_no}.pdf")
            if i % 50 == 0 or i == len(license_numbers):
                print(f"  {i}/{len(license_numbers)}")
        print(f"Done. Wrote {len(license_numbers)} PDFs to {out_dir}/")
        return

    if not args.license or not args.out:
        ap.error("--license and --out are required (or use --bulk-out)")

    context = load_license_context(conn, args.license)
    html_content = template.render(**context)
    HTML(string=html_content).write_pdf(args.out)
    print(f"Wrote {args.out} for license {args.license} "
          f"({context['business_name']}, status: {context['status_label']})")


if __name__ == "__main__":
    main()
