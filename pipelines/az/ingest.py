#!/usr/bin/env python3
"""
Arizona ROC ingestion pipeline.

Loads ROC's four public CSV exports (posted at roc.az.gov/posting-list) into
a persistent SQLite store, and — this is the part that matters — never lets
a newer snapshot silently erase history. ROC's "Current Active Contractors"
file only ever shows what's active *right now*: a revoked or suspended
company simply disappears from it on the next refresh. If you just overwrite
your table each time you re-download it, you lose the exact signal a
verification directory exists to surface.

This script instead:
  1. Upserts the roster, tracking first_seen / last_seen snapshot dates per
     license.
  2. Detects licenses that were Active in the previous snapshot but are
     missing from the new one, and records that as a status-change event
     (license_dropped) rather than just deleting the row.
  3. Loads disciplinary actions as an append-only ledger, deduplicated on
     (license_no, case_number), so a revoked company's history persists
     forever even after ROC drops it from the active roster.
  4. Loads new-license and pending-application rows for reference (both are
     rolling/partial views, not authoritative history, so they're kept
     separate and not used to infer status changes).

Solar-relevance is flagged with a simple, transparent heuristic — explicit
"solar" in the classification description, OR "solar" in the business name
or DBA — deliberately excluding plain Electrical (C-11/R-11/CR-11) licenses,
which vastly outnumber actual solar installers (5,006 vs. 665 in the
2026-09-14 snapshot) and would swamp the list with unrelated electricians.
This is a heuristic, not ground truth — review flagged=0 rows near "solar"
adjacent trades (roofing, HVAC) periodically, since a real installer with a
generic name will never be caught by name-matching alone.

Usage:
    python ingest.py --db az_roc.db \
        --roster ROC_Posting-List_2026-09-14.csv \
        --disciplinary ROC_Disciplinary-Actions_2026-09-14.csv \
        --new-licenses ROC_New-Licenses-List_2026-09-14.csv \
        --pending ROC_Pending-Applications_2026-09-14.csv

Re-run with a later snapshot's files (same flags, new filenames) to update
in place and detect drops. Filenames must end in _YYYY-MM-DD.csv; the
snapshot date is parsed from the filename since none of the files carry a
per-row snapshot date column.
"""

import argparse
import csv
import re
import sqlite3
from pathlib import Path

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.csv$")


def snapshot_date_from_filename(path: Path) -> str:
    m = DATE_RE.search(path.name)
    if not m:
        raise ValueError(
            f"Can't find a YYYY-MM-DD date in filename: {path.name}. "
            "Rename it to match ROC's own convention, e.g. "
            "ROC_Posting-List_2026-09-14.csv"
        )
    return m.group(1)


def read_csv_after_preamble(path: Path):
    """ROC's exports have 1-2 free-text lines before the real header row.
    Find the header by looking for the first line that starts with a quoted
    field we recognize, then hand csv.reader everything from there."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = f.readlines()

    header_markers = ("\"#\",", "\"License No\",", "\"Business Name\",")
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith(header_markers):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"Couldn't locate a header row in {path.name}")

    reader = csv.reader(lines[header_idx:])
    header = next(reader)
    rows = [dict(zip(header, row)) for row in reader if any(c.strip() for c in row)]
    return rows


def is_solar_relevant(class_detail: str, business_name: str, dba: str) -> bool:
    text = f"{class_detail} {business_name} {dba}".lower()
    if "solar" in class_detail.lower():
        return True
    name = f"{business_name} {dba}".lower()
    return "solar" in name


SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
    license_no TEXT NOT NULL,
    class_code TEXT NOT NULL,
    business_name TEXT,
    dba TEXT,
    class_detail TEXT,
    class_type TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    qualifying_party TEXT,
    issued_date TEXT,
    expiration_date TEXT,
    status TEXT,
    is_solar_relevant INTEGER NOT NULL DEFAULT 0,
    first_seen_snapshot TEXT,
    last_seen_snapshot TEXT,
    PRIMARY KEY (license_no, class_code)
);

CREATE TABLE IF NOT EXISTS status_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_no TEXT NOT NULL,
    class_code TEXT,
    business_name TEXT,
    change_type TEXT NOT NULL,   -- 'dropped_from_active_roster'
    last_known_status TEXT,
    last_seen_snapshot TEXT,
    detected_snapshot TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disciplinary_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_no TEXT,
    business_name TEXT,
    dba TEXT,
    address TEXT,
    address2 TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    license_class TEXT,
    case_number TEXT,
    description TEXT,
    first_seen_snapshot TEXT,
    UNIQUE (license_no, case_number, description)
);

CREATE TABLE IF NOT EXISTS new_licenses_feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_no TEXT,
    business_name TEXT,
    dba TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    qualifying_party TEXT,
    class_code TEXT,
    class_detail TEXT,
    issued_date TEXT,
    expiration_date TEXT,
    status TEXT,
    snapshot_date TEXT,
    UNIQUE (license_no, class_code, issued_date)
);

CREATE TABLE IF NOT EXISTS pending_applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_name TEXT,
    dba TEXT,
    address TEXT,
    address2 TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    receipt_number TEXT,
    class_detail TEXT,
    application_date TEXT,
    qualifying_party TEXT,
    officers TEXT,
    snapshot_date TEXT,
    UNIQUE (receipt_number)
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_type TEXT,
    filename TEXT,
    snapshot_date TEXT,
    record_count INTEGER,
    ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def ingest_roster(conn, path: Path):
    snap = snapshot_date_from_filename(path)
    rows = read_csv_after_preamble(path)
    cur = conn.cursor()

    # Which licenses were Active as of the *previous* snapshot?
    cur.execute(
        "SELECT license_no, class_code, business_name, status, last_seen_snapshot "
        "FROM licenses WHERE status = 'Active'"
    )
    previously_active = {(r[0], r[1]): r for r in cur.fetchall()}

    seen_this_snapshot = set()
    for row in rows:
        license_no = row["License No"].strip()
        class_code = row["Class"].strip()
        business_name = row["Business Name"].strip()
        dba = row.get("Doing Business As", "").strip()
        class_detail = row.get("Class Detail", "").strip()
        solar = int(is_solar_relevant(class_detail, business_name, dba))
        key = (license_no, class_code)
        seen_this_snapshot.add(key)

        cur.execute(
            "SELECT first_seen_snapshot FROM licenses WHERE license_no=? AND class_code=?",
            (license_no, class_code),
        )
        existing = cur.fetchone()
        first_seen = existing[0] if existing else snap

        cur.execute(
            """
            INSERT INTO licenses (license_no, class_code, business_name, dba,
                class_detail, class_type, address, city, state, zip,
                qualifying_party, issued_date, expiration_date, status,
                is_solar_relevant, first_seen_snapshot, last_seen_snapshot)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(license_no, class_code) DO UPDATE SET
                business_name=excluded.business_name,
                dba=excluded.dba,
                class_detail=excluded.class_detail,
                class_type=excluded.class_type,
                address=excluded.address,
                city=excluded.city,
                state=excluded.state,
                zip=excluded.zip,
                qualifying_party=excluded.qualifying_party,
                issued_date=excluded.issued_date,
                expiration_date=excluded.expiration_date,
                status=excluded.status,
                is_solar_relevant=excluded.is_solar_relevant,
                last_seen_snapshot=excluded.last_seen_snapshot
            """,
            (
                license_no, class_code, business_name, dba, class_detail,
                row.get("Class Type", ""), row.get("Address", ""),
                row.get("City", ""), row.get("State", ""), row.get("Zip", ""),
                row.get("Qualifying Party", ""), row.get("Issued Date", ""),
                row.get("Expiration Date", ""), row.get("Status", ""),
                solar, first_seen, snap,
            ),
        )

    # Anything that was Active before but isn't in this snapshot at all has
    # dropped off ROC's active list -- log it rather than silently losing it.
    dropped = set(previously_active) - seen_this_snapshot
    for key in dropped:
        license_no, class_code = key
        _, _, business_name, last_status, last_seen = previously_active[key]
        cur.execute(
            """
            INSERT INTO status_changes
                (license_no, class_code, business_name, change_type,
                 last_known_status, last_seen_snapshot, detected_snapshot)
            VALUES (?,?,?,?,?,?,?)
            """,
            (license_no, class_code, business_name, "dropped_from_active_roster",
             last_status, last_seen, snap),
        )

    cur.execute(
        "INSERT INTO ingest_log (file_type, filename, snapshot_date, record_count) "
        "VALUES (?,?,?,?)",
        ("roster", path.name, snap, len(rows)),
    )
    conn.commit()
    return len(rows), len(dropped)


def ingest_disciplinary(conn, path: Path):
    snap = snapshot_date_from_filename(path)
    rows = read_csv_after_preamble(path)
    cur = conn.cursor()
    inserted = 0
    for row in rows:
        cur.execute(
            """
            INSERT OR IGNORE INTO disciplinary_actions
                (license_no, business_name, dba, address, address2, city,
                 state, zip, license_class, case_number, description,
                 first_seen_snapshot)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row.get("License No", ""), row.get("Business Name", ""),
                row.get("Doing Business As", ""), row.get("Address", ""),
                row.get("Address 2", ""), row.get("City", ""),
                row.get("State", ""), row.get("Zip", ""),
                row.get("License Class", ""), row.get("Case Number", ""),
                row.get("Description", ""), snap,
            ),
        )
        if cur.rowcount:
            inserted += 1
    cur.execute(
        "INSERT INTO ingest_log (file_type, filename, snapshot_date, record_count) "
        "VALUES (?,?,?,?)",
        ("disciplinary", path.name, snap, len(rows)),
    )
    conn.commit()
    return len(rows), inserted


def ingest_new_licenses(conn, path: Path):
    snap = snapshot_date_from_filename(path)
    rows = read_csv_after_preamble(path)
    cur = conn.cursor()
    inserted = 0
    for row in rows:
        cur.execute(
            """
            INSERT OR IGNORE INTO new_licenses_feed
                (license_no, business_name, dba, address, city, state, zip,
                 qualifying_party, class_code, class_detail, issued_date,
                 expiration_date, status, snapshot_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row.get("License No", ""), row.get("Business Name", ""),
                row.get("Doing Business As", ""), row.get("Address", ""),
                row.get("City", ""), row.get("State", ""), row.get("Zip", ""),
                row.get("Qualifying Party", ""), row.get("Class", ""),
                row.get("Class Detail", ""), row.get("Issued Date", ""),
                row.get("Expiration Date", ""), row.get("Status", ""), snap,
            ),
        )
        if cur.rowcount:
            inserted += 1
    cur.execute(
        "INSERT INTO ingest_log (file_type, filename, snapshot_date, record_count) "
        "VALUES (?,?,?,?)",
        ("new_licenses", path.name, snap, len(rows)),
    )
    conn.commit()
    return len(rows), inserted


def ingest_pending(conn, path: Path):
    snap = snapshot_date_from_filename(path)
    rows = read_csv_after_preamble(path)
    cur = conn.cursor()
    inserted = 0
    for row in rows:
        cur.execute(
            """
            INSERT OR IGNORE INTO pending_applications
                (business_name, dba, address, address2, city, state, zip,
                 receipt_number, class_detail, application_date,
                 qualifying_party, officers, snapshot_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row.get("Business Name", ""), row.get("Doing Business As", ""),
                row.get("Address", ""), row.get("Address 2", ""),
                row.get("City", ""), row.get("State", ""), row.get("Zip", ""),
                row.get("Receipt Number", ""), row.get("Class", ""),
                row.get("Application Date", ""), row.get("Qualifying Party", ""),
                row.get("Officers", ""), snap,
            ),
        )
        if cur.rowcount:
            inserted += 1
    cur.execute(
        "INSERT INTO ingest_log (file_type, filename, snapshot_date, record_count) "
        "VALUES (?,?,?,?)",
        ("pending", path.name, snap, len(rows)),
    )
    conn.commit()
    return len(rows), inserted


def print_summary(conn):
    cur = conn.cursor()
    print("\n=== Summary ===")

    cur.execute("SELECT COUNT(*) FROM licenses WHERE status='Active'")
    print(f"Active licenses in store: {cur.fetchone()[0]:,}")

    cur.execute(
        "SELECT COUNT(*) FROM licenses WHERE status='Active' AND is_solar_relevant=1"
    )
    print(f"  ...of which solar-relevant: {cur.fetchone()[0]:,}")

    cur.execute("SELECT COUNT(DISTINCT business_name) FROM licenses "
                "WHERE status='Active' AND is_solar_relevant=1")
    print(f"  ...unique solar-relevant business names: {cur.fetchone()[0]:,}")

    cur.execute("SELECT COUNT(*) FROM disciplinary_actions")
    print(f"Disciplinary action records (ledger, all-time): {cur.fetchone()[0]:,}")

    cur.execute(
        """
        SELECT COUNT(DISTINCT d.license_no) FROM disciplinary_actions d
        WHERE d.license_no IN (
            SELECT license_no FROM licenses WHERE is_solar_relevant=1
        )
        """
    )
    print(f"  ...tied to a solar-relevant license: {cur.fetchone()[0]:,}")

    cur.execute("SELECT COUNT(*) FROM status_changes WHERE change_type='dropped_from_active_roster'")
    print(f"Licenses dropped from active roster since last snapshot: {cur.fetchone()[0]:,}")

    cur.execute(
        "SELECT business_name, license_no, license_class, description "
        "FROM disciplinary_actions "
        "WHERE lower(business_name || ' ' || dba) LIKE '%solar%' "
        "   OR lower(license_class) LIKE '%solar%' "
        "GROUP BY license_no, description ORDER BY business_name"
    )
    hits = cur.fetchall()
    if hits:
        print(f"\nSolar-flagged disciplinary history ({len(hits)} rows):")
        for biz, lic, cls, desc in hits:
            print(f"  - {biz} (lic {lic}, {cls}): {desc}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="SQLite file to create/update")
    ap.add_argument("--roster", help="ROC_Posting-List_*.csv (full active roster)")
    ap.add_argument("--disciplinary", help="ROC_Disciplinary-Actions_*.csv")
    ap.add_argument("--new-licenses", help="ROC_New-Licenses-List_*.csv")
    ap.add_argument("--pending", help="ROC_Pending-Applications_*.csv")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    if args.roster:
        total, dropped = ingest_roster(conn, Path(args.roster))
        print(f"Roster: {total:,} rows ingested, {dropped:,} licenses dropped off since prior snapshot")
    if args.disciplinary:
        total, new = ingest_disciplinary(conn, Path(args.disciplinary))
        print(f"Disciplinary: {total:,} rows in file, {new:,} new to the ledger")
    if getattr(args, "new_licenses"):
        total, new = ingest_new_licenses(conn, Path(getattr(args, "new_licenses")))
        print(f"New licenses feed: {total:,} rows in file, {new:,} new")
    if args.pending:
        total, new = ingest_pending(conn, Path(args.pending))
        print(f"Pending applications: {total:,} rows in file, {new:,} new")

    print_summary(conn)
    conn.close()


if __name__ == "__main__":
    main()
