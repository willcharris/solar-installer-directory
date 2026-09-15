#!/usr/bin/env python3
"""
Generate a one-page installer verification PDF for a Michigan solar
installer, pulling from enriched_mi.csv (built by match_candidates.py +
enrich_details.py).

Unlike Arizona -- where one license number is the whole identity -- a
Michigan installer is typically licensed under MULTIPLE, unrelated
license types at once (Residential Builder for structural/racking work,
Electrical Contractor for interconnection, sometimes an M&A trade
license too). This groups all matched license rows for one business
name and renders them together, rather than picking just one.

IMPORTANT -- no disciplinary/complaint data source has been built for
Michigan yet (no BBB, AG action, or bankruptcy-filing ingest exists for
MI the way it does for AZ). The report says so plainly rather than
showing an empty "no disciplinary actions on file" box, which would
falsely imply that source was checked and came back clean.

Usage:
    python generate_report.py --enriched enriched_mi.csv \
        --installer "Michigan Solar and Roofing LLC" --out report.pdf

    # List installer names available in the file:
    python generate_report.py --enriched enriched_mi.csv --list
"""

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from jinja2 import Template
from weasyprint import HTML

# A license row counts as "currently valid" for the overall badge if its
# status is one of these. Everything else (Lapsed, Expired, Inactive,
# Revoked, Suspended, or unrecognized) counts against it.
CURRENT_STATUSES = {"Issued", "Active"}


def load_rows(enriched_path: str, installer: str):
    with open(enriched_path, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["candidate_name"] == installer]
    return rows


def list_installers(enriched_path: str):
    with open(enriched_path, encoding="utf-8-sig", newline="") as f:
        names = sorted({r["candidate_name"] for r in csv.DictReader(f)})
    return names


def build_context(rows: list, installer: str) -> dict:
    if not rows:
        raise ValueError(f"No rows found for installer {installer!r} in the enriched file.")

    licenses = []
    any_current = False
    for r in rows:
        status = (r.get("license_status") or "").strip()
        is_current = status in CURRENT_STATUSES
        any_current = any_current or is_current
        classifications = [c.strip() for c in (r.get("classifications") or "").split(";") if c.strip()]
        licenses.append(
            {
                "license_type": r["license_type"],
                "license_number": r["license_number"],
                "status": status or "Unknown",
                "is_current": is_current,
                "expiration_date": r.get("expiration_date") or "Not available",
                "classifications": classifications,
                "match_confidence": r.get("confidence", ""),
                "match_score": r.get("match_score", ""),
            }
        )

    # Surface the lowest-confidence match used, so the report itself is
    # honest about how sure the name-matching was, not just the license data.
    confidences = {r.get("confidence") for r in rows}
    weakest_confidence = "medium" if "medium" in confidences else "high"

    return {
        "installer_name": installer,
        "licenses": licenses,
        "any_current": any_current,
        "weakest_confidence": weakest_confidence,
        "generated_date": date.today().isoformat(),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--enriched", required=True, help="enriched_mi.csv from enrich_details.py")
    ap.add_argument("--installer", help="Exact candidate_name to render (see --list)")
    ap.add_argument("--out", help="Output PDF path (required unless --list)")
    ap.add_argument("--list", action="store_true", help="List installer names available and exit")
    ap.add_argument(
        "--template",
        default=str(Path(__file__).parent / "report_template.html"),
    )
    args = ap.parse_args()

    if args.list:
        for name in list_installers(args.enriched):
            print(name)
        return

    if not args.installer or not args.out:
        sys.exit("--installer and --out are required (or use --list to see names)")

    rows = load_rows(args.enriched, args.installer)
    context = build_context(rows, args.installer)

    template = Template(Path(args.template).read_text(encoding="utf-8"))
    html_content = template.render(**context)

    HTML(string=html_content).write_pdf(args.out)
    overall = "at least one current license" if context["any_current"] else "NO current license found"
    print(f"Wrote {args.out} for {args.installer} ({len(rows)} license row(s), {overall})")


if __name__ == "__main__":
    main()
