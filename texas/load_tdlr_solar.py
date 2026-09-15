"""
TDLR Solar Residential Retailer / Salesperson CSV loader.

Loads TDLR's own daily "Download License Files" exports for the two new
solar registrations created by SB 1036:
    - Solar Residential Retailers  (companies)
    - Solar Residential Salesperson (individuals)

Unlike CSLB, there's no county-splitting or classification-overlap problem
here — TDLR publishes one flat statewide CSV per license type, updated
daily. This loader just needs to read and store it, with the same
change-tracking pattern used for California (a snapshot per load, so a
license quietly disappearing from a future pull is detectable).

Usage:
    python load_tdlr_solar.py \
        --retailers vsResidentialSolarRetailers.csv \
        --salespersons vsResidentialSolarSalesperson.csv

Install:
    pip install pandas
"""

import argparse
import hashlib
import sqlite3
from datetime import datetime, timezone
import pandas as pd

DB_PATH = "tx_tdlr_solar.db"


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS tx_solar_retailers (
        license_number TEXT PRIMARY KEY,
        business_name TEXT,
        address1 TEXT,
        address2 TEXT,
        city TEXT,
        state TEXT,
        zip_code TEXT,
        county TEXT,
        phone TEXT,
        expiration_date TEXT,
        first_seen_at TEXT,
        last_checked_at TEXT
    );

    CREATE TABLE IF NOT EXISTS tx_solar_retailer_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_number TEXT,
        scraped_at TEXT,
        status TEXT,
        row_hash TEXT,
        FOREIGN KEY (license_number) REFERENCES tx_solar_retailers(license_number)
    );

    CREATE TABLE IF NOT EXISTS tx_solar_salespersons (
        license_number TEXT PRIMARY KEY,
        name TEXT,
        state TEXT,
        county TEXT,
        expiration_date TEXT,
        first_seen_at TEXT,
        last_checked_at TEXT
    );

    CREATE TABLE IF NOT EXISTS tx_solar_salesperson_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_number TEXT,
        scraped_at TEXT,
        status TEXT,
        row_hash TEXT,
        FOREIGN KEY (license_number) REFERENCES tx_solar_salespersons(license_number)
    );
    """)
    conn.commit()


def load_retailers(conn: sqlite3.Connection, path: str) -> int:
    df = pd.read_csv(path, dtype=str)
    now = datetime.now(timezone.utc).isoformat()
    count = 0

    for _, row in df.iterrows():
        license_number = str(row['License Number']).strip()

        cur = conn.execute(
            "SELECT first_seen_at FROM tx_solar_retailers WHERE license_number = ?",
            (license_number,),
        )
        existing = cur.fetchone()
        first_seen = existing[0] if existing else now

        conn.execute(
            """
            INSERT INTO tx_solar_retailers
                (license_number, business_name, address1, address2, city, state,
                 zip_code, county, phone, expiration_date, first_seen_at, last_checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(license_number) DO UPDATE SET
                business_name=excluded.business_name,
                address1=excluded.address1,
                address2=excluded.address2,
                city=excluded.city,
                state=excluded.state,
                zip_code=excluded.zip_code,
                county=excluded.county,
                phone=excluded.phone,
                expiration_date=excluded.expiration_date,
                last_checked_at=excluded.last_checked_at
            """,
            (license_number, row.get('Licensee'), row.get('Address1'), row.get('Address2'),
             row.get('City'), row.get('State'), row.get('Zip'), row.get('County'),
             row.get('Phone'), row.get('License Expiration Date'), first_seen, now),
        )

        row_hash = hashlib.sha256(str(row.to_dict()).encode('utf-8')).hexdigest()
        conn.execute(
            """
            INSERT INTO tx_solar_retailer_snapshots (license_number, scraped_at, status, row_hash)
            VALUES (?, ?, ?, ?)
            """,
            (license_number, now, row.get('License Status'), row_hash),
        )
        count += 1

    conn.commit()
    return count


def load_salespersons(conn: sqlite3.Connection, path: str) -> int:
    df = pd.read_csv(path, dtype=str)
    now = datetime.now(timezone.utc).isoformat()
    count = 0

    for _, row in df.iterrows():
        license_number = str(row['License Number']).strip()

        cur = conn.execute(
            "SELECT first_seen_at FROM tx_solar_salespersons WHERE license_number = ?",
            (license_number,),
        )
        existing = cur.fetchone()
        first_seen = existing[0] if existing else now

        conn.execute(
            """
            INSERT INTO tx_solar_salespersons
                (license_number, name, state, county, expiration_date, first_seen_at, last_checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(license_number) DO UPDATE SET
                name=excluded.name,
                state=excluded.state,
                county=excluded.county,
                expiration_date=excluded.expiration_date,
                last_checked_at=excluded.last_checked_at
            """,
            (license_number, row.get('Licensee'), row.get('State'), row.get('County'),
             row.get('License Expiration Date'), first_seen, now),
        )

        row_hash = hashlib.sha256(str(row.to_dict()).encode('utf-8')).hexdigest()
        conn.execute(
            """
            INSERT INTO tx_solar_salesperson_snapshots (license_number, scraped_at, status, row_hash)
            VALUES (?, ?, ?, ?)
            """,
            (license_number, now, row.get('License Status'), row_hash),
        )
        count += 1

    conn.commit()
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--retailers', required=True, help='Path to vsResidentialSolarRetailers.csv')
    parser.add_argument('--salespersons', required=True, help='Path to vsResidentialSolarSalesperson.csv')
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    n_retailers = load_retailers(conn, args.retailers)
    print(f"Loaded {n_retailers} retailer records into {DB_PATH}")

    n_salespersons = load_salespersons(conn, args.salespersons)
    print(f"Loaded {n_salespersons} salesperson records into {DB_PATH}")

    conn.close()


if __name__ == '__main__':
    main()
