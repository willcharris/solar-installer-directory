#!/usr/bin/env python3
"""
Generate a one-page installer verification PDF for a Florida DBPR
solar-relevant licensee, pulling from fl_solar.db (built by
fl_scoping/ingest.py).

Two things this report is careful about, both specific to Florida's data:

1. IDENTITY: DBPR's extract is qualifier-centric -- `licensee_name` is
   the individual person holding the license, and the actual public-
   facing business name lives in `dba_name` (when present). Showing
   the person's name as the headline would be wrong for a consumer
   directory; this report shows the DBA when one exists and falls back
   to the individual's name only for a sole practitioner with none.

2. STATUS CODES: only (C, A) = Active and (C, I) = Inactive are
   confirmed against DBPR's own published status model. The rarer
   codes seen in real data (P, S as primary status) are NOT verified
   against any official source as of this writing -- rather than guess
   a color-coded label that could be wrong in either direction (falsely
   reassuring OR falsely alarming), this report shows the raw codes
   plainly for anything outside the two confirmed combinations and
   says to verify directly with DBPR.

Usage:
    python generate_report.py --db fl_solar.db --license 0056967 --out report.pdf
    python generate_report.py --db fl_solar.db --list
"""

import argparse
import sqlite3
from pathlib import Path

from jinja2 import Template
from weasyprint import HTML

# Only these two combinations are confirmed against DBPR's published
# status model (Primary: Current/Involuntarily Inactive/Delinquent/Null
# and Void; Secondary: Active/Inactive). Anything else shows the raw
# codes rather than a guessed label.
CONFIRMED_STATUS_LABELS = {
    ("C", "A"): ("Active", "active"),
    ("C", "I"): ("Inactive", "inactive"),
}


def display_name_for(licensee_name: str, dba_name: str) -> str:
    dba_name = (dba_name or "").strip()
    return dba_name if dba_name else (licensee_name or "").strip()


def status_display(primary_status: str, secondary_status: str):
    key = ((primary_status or "").strip(), (secondary_status or "").strip())
    if key in CONFIRMED_STATUS_LABELS:
        label, css_class = CONFIRMED_STATUS_LABELS[key]
        return label, css_class, False
    # Unconfirmed combination -- show raw codes, flag for verification
    # rather than guessing.
    raw = f"{key[0] or '?'} / {key[1] or '?'}"
    return raw, "unverified", True


def load_context(conn, license_number: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT license_number, licensee_name, dba_name, occupation_code,
               city, state, zip, primary_status, secondary_status,
               orig_licensure_date, expiration_date, match_type,
               discipline_count, latest_discipline_date, dispositions,
               discipline_note
        FROM fl_solar_licensees WHERE license_number = ?
        """,
        (license_number,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"License {license_number!r} not found in fl_solar_licensees.")

    (license_number, licensee_name, dba_name, occupation_code, city, state, zip_code,
     primary_status, secondary_status, orig_licensure_date, expiration_date, match_type,
     discipline_count, latest_discipline_date, dispositions, discipline_note) = row

    status_label, status_class, status_unverified = status_display(primary_status, secondary_status)
    dispositions_list = [d.strip() for d in (dispositions or "").split(";") if d.strip()]

    return {
        "license_number": license_number,
        "display_name": display_name_for(licensee_name, dba_name),
        "licensee_name": licensee_name,
        "has_dba": bool((dba_name or "").strip()),
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "orig_licensure_date": orig_licensure_date or "Not available",
        "expiration_date": expiration_date or "Not available",
        "match_type": match_type,
        "status_label": status_label,
        "status_class": status_class,
        "status_unverified": status_unverified,
        "raw_primary_status": primary_status,
        "raw_secondary_status": secondary_status,
        "discipline_count": discipline_count,
        "discipline_checked": discipline_count is not None,
        "latest_discipline_date": latest_discipline_date,
        "dispositions_list": dispositions_list,
        "discipline_note": discipline_note,
    }


def list_licensees(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT license_number, licensee_name, dba_name, match_type, city "
        "FROM fl_solar_licensees ORDER BY dba_name, licensee_name"
    )
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--license", help="License number to render")
    ap.add_argument("--out", help="Output PDF path")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--bulk-out", help="Directory to write one PDF per licensee (all ~617)")
    ap.add_argument("--template", default=str(Path(__file__).parent / "report_template.html"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    if args.list:
        for license_number, licensee_name, dba_name, match_type, city in list_licensees(conn):
            name = display_name_for(licensee_name, dba_name)
            print(f"{license_number}\t{name}\t{match_type}\t{city}")
        return

    template = Template(Path(args.template).read_text(encoding="utf-8"))

    if args.bulk_out:
        out_dir = Path(args.bulk_out)
        out_dir.mkdir(parents=True, exist_ok=True)
        license_numbers = [r[0] for r in list_licensees(conn)]
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
    print(f"Wrote {args.out} for license {args.license} "
          f"({context['display_name']}, status: {context['status_label']})")


if __name__ == "__main__":
    main()
