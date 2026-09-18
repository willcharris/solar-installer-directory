#!/usr/bin/env python3
"""
Ingest CSLB's statewide License Master file (downloaded from
https://www.cslb.ca.gov/onlineservices/dataportal/ContractorList ->
"Statewide Public Database Files" -> License Master, CSV format).

This REPLACES the earlier county-scoped approach (cslb_scraper.py /
load_cslb_export.py, which pulled one county+classification combination
at a time via a search form). The statewide master file is a single
~244,000-row CSV covering every license and classification in
California at once -- no per-county or per-classification splitting
needed, and it's re-downloadable with one click any time the data needs
refreshing.

Two real improvements over the previous cslb_solar.db this gives us:
  1. Real status data (PrimaryStatus / SecondaryStatus) -- the old data
     had no status field at all, so reports could only show expiration
     date. This file has it, including real disciplinary-adjacent
     signals like "7073E Probation" and "Pending Case/CIT" riding on an
     otherwise-CLEAR primary status.
  2. More complete: 1,192 C-46 rows here vs. 1,082 in the previous db.

Classifications are stored pipe-separated in one field (e.g.
"C10| C46"), inconsistently spaced -- parsed here into a proper
license_classifications join table, same shape as before.

Writes three tables:
  licenses                 -- C-46 holders only (the "solar" population),
                              now with real status
  license_classifications  -- every classification each C-46 license
                              holds (unchanged shape from before)
  ca_all_licenses           -- the FULL statewide roster (~244,000 rows),
                              kept for the fuzzy-matching pass that finds
                              solar installers hiding under a non-C-46
                              classification (the same fix already
                              applied to AZ and FL)

Usage:
    python ingest_master.py --master MasterLicenseData.csv --db cslb_solar.db
"""

import argparse
import re
import sqlite3
from datetime import datetime, timezone

import pandas as pd

SCHEMA = """
DROP TABLE IF EXISTS licenses;
DROP TABLE IF EXISTS license_classifications;
DROP TABLE IF EXISTS ca_all_licenses;

CREATE TABLE licenses (
    license_number TEXT PRIMARY KEY,
    business_type TEXT,
    business_name TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT,
    county TEXT,
    phone_number TEXT,
    issue_date TEXT,
    expiration_date TEXT,
    qualifier_name TEXT,
    primary_status TEXT,
    secondary_status TEXT,
    first_seen_at TEXT,
    last_checked_at TEXT
);

CREATE TABLE license_classifications (
    license_number TEXT,
    classification_code TEXT
);

CREATE TABLE ca_all_licenses (
    license_number TEXT PRIMARY KEY,
    business_name TEXT,
    city TEXT,
    county TEXT,
    classifications_raw TEXT,
    primary_status TEXT,
    secondary_status TEXT
);
"""


def parse_classifications(raw: str):
    """Classifications are pipe-separated with inconsistent spacing,
    e.g. 'C10| C46 ' or 'B| C39| C46 '. Normalize each to the state's
    hyphenated form where it's a two-letter-plus-digits code (C10 -> C-10),
    leaving single-letter codes (B, A) as-is."""
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


def is_c46(raw: str) -> bool:
    return "C46" in parse_classifications(raw) or "C-46" in parse_classifications(raw)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", required=True, help="MasterLicenseData.csv from CSLB's statewide download")
    ap.add_argument("--db", default="cslb_solar.db")
    args = ap.parse_args()

    print(f"Loading {args.master} ...")
    df = pd.read_csv(args.master, dtype=str, low_memory=False)
    df = df.fillna("")
    print(f"  {len(df):,} total license records")

    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    # --- ca_all_licenses: the full roster, for fuzzy-matching ---
    all_rows = [
        {
            "license_number": r["LicenseNo"],
            "business_name": r["BusinessName"],
            "city": r["City"],
            "county": r["County"],
            "classifications_raw": r["Classifications(s)"],
            "primary_status": r["PrimaryStatus"].strip(),
            "secondary_status": r["SecondaryStatus"].strip(),
        }
        for _, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT INTO ca_all_licenses VALUES (:license_number, :business_name, :city, :county, "
        ":classifications_raw, :primary_status, :secondary_status)",
        all_rows,
    )
    print(f"  wrote {len(all_rows):,} rows to ca_all_licenses (full statewide roster)")

    # --- licenses + license_classifications: C-46 holders only ---
    c46_df = df[df["Classifications(s)"].apply(is_c46)]
    print(f"  {len(c46_df):,} C-46 (solar) license records")

    license_rows = []
    classification_rows = []
    for _, r in c46_df.iterrows():
        license_rows.append(
            {
                "license_number": r["LicenseNo"],
                "business_type": r["BusinessType"],
                "business_name": r["BusinessName"],
                "address": r["MailingAddress"],
                "city": r["City"],
                "state": r["State"],
                "zip_code": r["ZIPCode"],
                "county": r["County"],
                "phone_number": r["BusinessPhone"],
                "issue_date": r["IssueDate"],
                "expiration_date": r["ExpirationDate"],
                "qualifier_name": "",  # not in this file's columns; left blank rather than guessed
                "primary_status": r["PrimaryStatus"].strip(),
                "secondary_status": r["SecondaryStatus"].strip(),
                "first_seen_at": now,
                "last_checked_at": now,
            }
        )
        for code in parse_classifications(r["Classifications(s)"]):
            classification_rows.append({"license_number": r["LicenseNo"], "classification_code": code})

    conn.executemany(
        """
        INSERT INTO licenses VALUES (
            :license_number, :business_type, :business_name, :address, :city, :state,
            :zip_code, :county, :phone_number, :issue_date, :expiration_date, :qualifier_name,
            :primary_status, :secondary_status, :first_seen_at, :last_checked_at
        )
        """,
        license_rows,
    )
    conn.executemany(
        "INSERT INTO license_classifications VALUES (:license_number, :classification_code)",
        classification_rows,
    )
    conn.commit()

    n_clear = sum(1 for r in license_rows if r["primary_status"] == "CLEAR" and not r["secondary_status"])
    n_flagged_clear = sum(1 for r in license_rows if r["primary_status"] == "CLEAR" and r["secondary_status"])
    n_susp = sum(1 for r in license_rows if "Susp" in r["primary_status"])
    n_other = len(license_rows) - n_clear - n_flagged_clear - n_susp

    print(f"\n  wrote {len(license_rows):,} rows to licenses")
    print(f"    Active (CLEAR, no flag): {n_clear:,}")
    print(f"    Clear but flagged (e.g. pending/probation): {n_flagged_clear:,}")
    print(f"    Suspended: {n_susp:,}")
    print(f"    Other/unrecognized status: {n_other:,}")
    print(f"  wrote {len(classification_rows):,} rows to license_classifications")
    conn.close()


if __name__ == "__main__":
    main()
