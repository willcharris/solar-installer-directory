"""
Michigan LARA (Bureau of Construction Codes) ingest pipeline.

Loads the raw "Download results" CSV exports from LARA's Accela Citizen
Access licensee search (aca-prod.accela.com/LARA) for the three license
types relevant to solar installers, normalizes them into one table, and
constructs the deterministic per-record detail-page URL for each row
(License Status and, for M&A, trade Classifications only live on that
detail page — not in the bulk CSV).

No solar-specific classification exists anywhere in this license
scheme, at the statute or the live system level. Solar installers are
required to hold licenses under one or more of these three UNRELATED
types, and none of them will ever say "solar":

  - Residential Builder Company       (full-scope structural/racking work)
  - Residential Builder M and A Company (trade-specific; only counts if
                                          "Roofing (M)" is among its
                                          classifications -- not visible
                                          without a detail-page fetch)
  - Electrical Contractor             (interconnection/wiring)

Because there is no classification to filter on, business-name matching
against an external candidate installer list is not a fallback for a
coverage gap here -- it is the only viable strategy, for all three
license types, from the start.

Usage:
    python ingest.py --raw-dir /path/to/raw/csvs --db mi_lara_solar.db

Expects the raw directory to contain the three files as downloaded from
LARA (filenames are matched loosely by license type via CSV content --
the actual filenames LARA/your browser gives them don't matter).
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

DETAIL_URL_TMPL = (
    "https://aca-prod.accela.com/LARA/GeneralProperty/LicenseeDetail.aspx"
    "?LicenseeNumber={license_number}&LicenseeType={license_type_q}"
)

# License types we ingest, and which of the three "buckets" they map to.
# Only these three are in scope for the solar directory -- other BCC
# license types (Mechanical, Plumbing, Elevator, etc.) are out of scope.
RELEVANT_TYPES = {
    "Residential Builder Company": "residential_builder",
    "Residential Builder M and A Company": "ma_contractor",
    "Electrical Contractor": "electrical_contractor",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
    license_number      TEXT NOT NULL,
    license_type        TEXT NOT NULL,
    license_bucket       TEXT NOT NULL,
    business_name        TEXT,
    first_name           TEXT,
    middle_initial       TEXT,
    last_name            TEXT,
    display_name         TEXT NOT NULL,
    expiration_date_raw  TEXT,
    expiration_date      TEXT,
    is_expired           INTEGER,
    city                 TEXT,
    state                TEXT,
    zip                  TEXT,
    detail_url           TEXT NOT NULL,
    source_file          TEXT NOT NULL,
    PRIMARY KEY (license_number, license_type)
);

CREATE INDEX IF NOT EXISTS idx_licenses_display_name
    ON licenses (display_name);

CREATE INDEX IF NOT EXISTS idx_licenses_bucket
    ON licenses (license_bucket);
"""


def parse_expiration(raw: str, today: datetime):
    """Return (iso_date_or_None, is_expired_int_or_None)."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        dt = datetime.strptime(raw, "%m/%d/%Y")
    except ValueError:
        return None, None
    return dt.date().isoformat(), int(dt.date() < today.date())


def display_name_for(business_name: str, first: str, middle: str, last: str) -> str:
    business_name = (business_name or "").strip()
    if business_name:
        return business_name
    parts = [p.strip() for p in (first, middle, last) if p and p.strip()]
    return " ".join(parts) if parts else "(unnamed licensee)"


def detail_url_for(license_number: str, license_type: str) -> str:
    return DETAIL_URL_TMPL.format(
        license_number=quote_plus(license_number.strip()),
        license_type_q=quote_plus(license_type.strip()),
    )


def load_csv(path: Path, today: datetime):
    """Yield normalized row dicts from one raw LARA export CSV."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        # LARA exports a trailing empty-header column; DictReader files
        # it under None or '' depending on csv module version -- ignore it.
        for raw_row in reader:
            row = {(k or "").strip(): (v or "").strip() for k, v in raw_row.items() if k}
            license_type = row.get("License Type", "")
            bucket = RELEVANT_TYPES.get(license_type)
            if bucket is None:
                # Not one of the three types this pipeline cares about --
                # skip rather than silently mis-bucket it.
                continue
            license_number = row.get("License Number", "")
            if not license_number:
                continue
            exp_raw = row.get("License Expiration Date", "")
            exp_iso, expired = parse_expiration(exp_raw, today)
            yield {
                "license_number": license_number,
                "license_type": license_type,
                "license_bucket": bucket,
                "business_name": row.get("Business Name") or None,
                "first_name": row.get("First Name") or None,
                "middle_initial": row.get("Middle Initial") or None,
                "last_name": row.get("Last Name") or None,
                "display_name": display_name_for(
                    row.get("Business Name", ""),
                    row.get("First Name", ""),
                    row.get("Middle Initial", ""),
                    row.get("Last Name", ""),
                ),
                "expiration_date_raw": exp_raw or None,
                "expiration_date": exp_iso,
                "is_expired": expired,
                "city": row.get("City") or None,
                "state": row.get("State") or None,
                "zip": row.get("Zip") or None,
                "detail_url": detail_url_for(license_number, license_type),
                "source_file": path.name,
            }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--raw-dir",
        required=True,
        help="Directory containing the raw LARA 'Download results' CSVs.",
    )
    ap.add_argument(
        "--db",
        default="mi_lara_solar.db",
        help="Output SQLite database path (default: mi_lara_solar.db)",
    )
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir)
    csv_paths = sorted(raw_dir.glob("*.csv"))
    if not csv_paths:
        sys.exit(f"No CSV files found in {raw_dir}")

    today = datetime.now()

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    counts = {}
    expired_counts = {}
    total = 0
    with conn:
        for path in csv_paths:
            rows = list(load_csv(path, today))
            if not rows:
                print(f"  (skipped, no relevant rows) {path.name}")
                continue
            conn.executemany(
                """
                INSERT OR REPLACE INTO licenses (
                    license_number, license_type, license_bucket,
                    business_name, first_name, middle_initial, last_name,
                    display_name, expiration_date_raw, expiration_date,
                    is_expired, city, state, zip, detail_url, source_file
                ) VALUES (
                    :license_number, :license_type, :license_bucket,
                    :business_name, :first_name, :middle_initial, :last_name,
                    :display_name, :expiration_date_raw, :expiration_date,
                    :is_expired, :city, :state, :zip, :detail_url, :source_file
                )
                """,
                rows,
            )
            bucket = rows[0]["license_bucket"]
            counts[bucket] = counts.get(bucket, 0) + len(rows)
            expired_counts[bucket] = expired_counts.get(bucket, 0) + sum(
                1 for r in rows if r["is_expired"] == 1
            )
            total += len(rows)
            print(f"  loaded {len(rows):>6} rows from {path.name}  (bucket: {bucket})")

    print()
    print(f"Total rows loaded: {total}")
    for bucket, n in counts.items():
        exp = expired_counts.get(bucket, 0)
        pct = (exp / n * 100) if n else 0
        print(f"  {bucket:22s} {n:>6} rows  ({exp} expired as of today, {pct:.0f}%)")
    print(f"\nWrote {args.db}")


if __name__ == "__main__":
    main()
