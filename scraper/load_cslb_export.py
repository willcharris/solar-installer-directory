"""
CSLB Data Portal Excel loader.

Reads one or more Excel exports from CSLB's "LIST of CONTRACTORS by
CLASSIFICATION and COUNTY" (or by-classification-only) data portal tool,
and loads them into the SQLite schema.

This REPLACES the Playwright scraping approach for the core dataset —
CSLB's own export already contains license status, bond, and workers'
comp info, which is everything the original scraper was going to have
to visit individual detail pages for. Playwright may still be useful
later, but only for enrichment (qualifier/personnel name, disciplinary
actions) which aren't in this export — that's a separate, smaller
follow-up step, not needed for the MVP.

Usage:
    python load_cslb_export.py path/to/export1.xlsx path/to/export2.xlsx ...

Install:
    pip install pandas openpyxl
"""

import sqlite3
import sys
import hashlib
import logging
from datetime import datetime, timezone
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cslb_loader")

DB_PATH = "cslb_solar.db"

# ---------------------------------------------------------------------------
# DATABASE SCHEMA — updated to match the real CSLB export columns
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS licenses (
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
        qualifier_name TEXT,          -- filled later via detail-page enrichment, nullable for now
        first_seen_at TEXT,
        last_checked_at TEXT
    );

    CREATE TABLE IF NOT EXISTS license_classifications (
        license_number TEXT,
        classification_code TEXT,
        PRIMARY KEY (license_number, classification_code),
        FOREIGN KEY (license_number) REFERENCES licenses(license_number)
    );

    CREATE TABLE IF NOT EXISTS license_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_number TEXT,
        scraped_at TEXT,
        status TEXT,
        surety_company TEXT,
        bond_number TEXT,
        bond_effective_date TEXT,
        bond_cancellation_date TEXT,
        workers_comp_coverage_type TEXT,
        workers_comp_insurance_company TEXT,
        workers_comp_policy_number TEXT,
        workers_comp_effective_date TEXT,
        workers_comp_expiration_date TEXT,
        workers_comp_cancellation_date TEXT,
        workers_comp_suspend_date TEXT,
        row_hash TEXT,
        FOREIGN KEY (license_number) REFERENCES licenses(license_number)
    );

    CREATE TABLE IF NOT EXISTS disciplinary_actions (
        license_number TEXT,
        action_date TEXT,
        description TEXT,
        FOREIGN KEY (license_number) REFERENCES licenses(license_number)
    );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# LOADING
# ---------------------------------------------------------------------------

EXPECTED_COLUMNS = [
    "LicenseNumber", "BusinessType", "BusinessName", "Address", "City", "State",
    "ZIP Code", "County", "PhoneNumber", "IssueDate", "ExpirationDate",
    "Classification(s)", "Status", "SuretyCompany", "ContractorBondNumber",
    "BondEffectiveDate", "BondCancellationDate", "WorkersCompCoverageType",
    "WorkersCompInsuranceCompany", "WorkersCompPolicyNumber", "EffectiveDate",
    "ExpirationDate1", "CancellationDate", "WorkersCompSuspendDate",
]


def load_file(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, dtype=str)  # dtype=str avoids pandas mangling license numbers/dates
    missing = set(EXPECTED_COLUMNS) - set(df.columns)
    if missing:
        log.warning("File %s is missing expected columns: %s — check for a CSLB export format change", path, missing)
    return df


def upsert_row(conn: sqlite3.Connection, row: pd.Series) -> None:
    now = datetime.now(timezone.utc).isoformat()
    license_number = str(row["LicenseNumber"]).strip()

    cur = conn.execute(
        "SELECT first_seen_at FROM licenses WHERE license_number = ?",
        (license_number,),
    )
    existing = cur.fetchone()
    first_seen = existing[0] if existing else now

    conn.execute(
        """
        INSERT INTO licenses (license_number, business_type, business_name, address,
                               city, state, zip_code, county, phone_number,
                               issue_date, expiration_date, first_seen_at, last_checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(license_number) DO UPDATE SET
            business_type=excluded.business_type,
            business_name=excluded.business_name,
            address=excluded.address,
            city=excluded.city,
            state=excluded.state,
            zip_code=excluded.zip_code,
            county=excluded.county,
            phone_number=excluded.phone_number,
            issue_date=excluded.issue_date,
            expiration_date=excluded.expiration_date,
            last_checked_at=excluded.last_checked_at
        """,
        (license_number, row.get("BusinessType"), row.get("BusinessName"), row.get("Address"),
         row.get("City"), row.get("State"), row.get("ZIP Code"), row.get("County"),
         row.get("PhoneNumber"), row.get("IssueDate"), row.get("ExpirationDate"),
         first_seen, now),
    )

    # Classification(s) column is pipe-separated, e.g. " B | C10 | C46"
    raw_class = row.get("Classification(s)") or ""
    for code in raw_class.split("|"):
        code = code.strip()
        if code:
            conn.execute(
                """
                INSERT OR IGNORE INTO license_classifications (license_number, classification_code)
                VALUES (?, ?)
                """,
                (license_number, code),
            )

    row_hash = hashlib.sha256(str(row.to_dict()).encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO license_snapshots
            (license_number, scraped_at, status, surety_company, bond_number,
             bond_effective_date, bond_cancellation_date, workers_comp_coverage_type,
             workers_comp_insurance_company, workers_comp_policy_number,
             workers_comp_effective_date, workers_comp_expiration_date,
             workers_comp_cancellation_date, workers_comp_suspend_date, row_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (license_number, now, row.get("Status"), row.get("SuretyCompany"),
         row.get("ContractorBondNumber"), row.get("BondEffectiveDate"),
         row.get("BondCancellationDate"), row.get("WorkersCompCoverageType"),
         row.get("WorkersCompInsuranceCompany"), row.get("WorkersCompPolicyNumber"),
         row.get("EffectiveDate"), row.get("ExpirationDate1"),
         row.get("CancellationDate"), row.get("WorkersCompSuspendDate"), row_hash),
    )


def main(paths: list[str]) -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    total = 0
    for path in paths:
        log.info("Loading %s", path)
        df = load_file(path)
        for _, row in df.iterrows():
            upsert_row(conn, row)
            total += 1
        conn.commit()
        log.info("Loaded %d rows from %s", len(df), path)

    log.info("Done. %d total rows processed into %s", total, DB_PATH)
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python load_cslb_export.py file1.xlsx [file2.xlsx ...]")
        sys.exit(1)
    main(sys.argv[1:])
