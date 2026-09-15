"""
CSLB C-46 (Solar) scraper — metro-region pilot.

Pipeline:
  1. For each ZIP in TARGET_ZIPS, search "Find My Licensed Contractor"
     for classification C-46, collect license numbers from results.
  2. For each unique license number, open the license detail page and
     parse the full record.
  3. Write into SQLite using the licenses / license_classifications /
     license_snapshots schema.

IMPORTANT — before running:
  The CSLB search form is a legacy ASP.NET WebForms page (postback +
  viewstate), and it's JS-rendered enough that I can't verify exact
  selectors without a live browser session. The SELECTOR block below
  has placeholders marked TODO. Fill them in by running:

      playwright install chromium
      playwright codegen https://www2.cslb.ca.gov/OnlineServices/CheckLicenseII/ZipCodeSearch.aspx

  That opens a real browser + a recorder window. Click through one
  search manually (pick classification C-46, enter a ZIP, submit,
  click into one result). Codegen will print the actual Playwright
  selectors it used — copy those into the TODOs below. Same idea for
  the detail-page selectors: click around the fields you need copied
  from your metro-region-crawl schema.

Install:
  pip install playwright
  playwright install chromium
"""

import sqlite3
import time
import logging
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, Page

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cslb_scraper")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DB_PATH = "cslb_solar.db"
CLASSIFICATION = "C-46"

# Pilot metro region — pick one to validate the pipeline before going statewide.
# Fresno metro ZIPs as a placeholder; swap for whatever metro you want to pilot.
TARGET_ZIPS = [
    "93701", "93702", "93703", "93704", "93705",
    "93706", "93710", "93711", "93720", "93721",
]

SEARCH_URL = "https://www2.cslb.ca.gov/OnlineServices/CheckLicenseII/ZipCodeSearch.aspx"

# Be a polite scraper — this is a small state agency site, not a CDN.
REQUEST_DELAY_SECONDS = 3.0

# ---------------------------------------------------------------------------
# SELECTORS — fill these in with playwright codegen (see docstring above)
# ---------------------------------------------------------------------------

SELECTORS = {
    # On the ZipCodeSearch page:
    "classification_dropdown": "TODO",   # e.g. "#ddlClassification" or a role/label locator
    "zip_input": "TODO",                 # e.g. "#txtZipCode"
    "submit_button": "TODO",             # e.g. "#btnSearch"
    "results_table_rows": "TODO",        # e.g. "table#gvResults tr"
    "results_license_link": "TODO",      # link to a license's detail page, within each row

    # On the license detail page:
    "detail_business_name": "TODO",
    "detail_status": "TODO",
    "detail_entity_type": "TODO",
    "detail_qualifier_name": "TODO",
    "detail_address": "TODO",
    "detail_phone": "TODO",
    "detail_classifications_block": "TODO",  # usually a repeated section, one per classification
    "detail_bond_block": "TODO",
    "detail_workers_comp_block": "TODO",
    "detail_disciplinary_block": "TODO",     # may be absent for clean licenses — handle None
}

# ---------------------------------------------------------------------------
# DATA MODEL
# ---------------------------------------------------------------------------

@dataclass
class LicenseRecord:
    license_number: str
    business_name: str = ""
    entity_type: str = ""
    status: str = ""
    qualifier_name: str = ""
    address: str = ""
    phone: str = ""
    classifications: list[tuple[str, str]] = field(default_factory=list)  # (code, issue_date)
    bond_company: str = ""
    bond_number: str = ""
    bond_amount: float | None = None
    bond_expiration: str = ""
    workers_comp_status: str = ""
    disciplinary_actions: list[tuple[str, str]] = field(default_factory=list)  # (date, description)
    raw_html: str = ""


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS licenses (
        license_number TEXT PRIMARY KEY,
        business_name TEXT,
        entity_type TEXT,
        status TEXT,
        qualifier_name TEXT,
        address TEXT,
        phone TEXT,
        first_seen_at TEXT,
        last_checked_at TEXT
    );

    CREATE TABLE IF NOT EXISTS license_classifications (
        license_number TEXT,
        classification_code TEXT,
        issue_date TEXT,
        PRIMARY KEY (license_number, classification_code),
        FOREIGN KEY (license_number) REFERENCES licenses(license_number)
    );

    CREATE TABLE IF NOT EXISTS license_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_number TEXT,
        scraped_at TEXT,
        status TEXT,
        bond_company TEXT,
        bond_number TEXT,
        bond_amount REAL,
        bond_expiration TEXT,
        workers_comp_status TEXT,
        raw_html_hash TEXT,
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


def upsert_license(conn: sqlite3.Connection, rec: LicenseRecord) -> None:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "SELECT first_seen_at FROM licenses WHERE license_number = ?",
        (rec.license_number,),
    )
    row = cur.fetchone()
    first_seen = row[0] if row else now

    conn.execute(
        """
        INSERT INTO licenses (license_number, business_name, entity_type, status,
                               qualifier_name, address, phone, first_seen_at, last_checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(license_number) DO UPDATE SET
            business_name=excluded.business_name,
            entity_type=excluded.entity_type,
            status=excluded.status,
            qualifier_name=excluded.qualifier_name,
            address=excluded.address,
            phone=excluded.phone,
            last_checked_at=excluded.last_checked_at
        """,
        (rec.license_number, rec.business_name, rec.entity_type, rec.status,
         rec.qualifier_name, rec.address, rec.phone, first_seen, now),
    )

    for code, issue_date in rec.classifications:
        conn.execute(
            """
            INSERT OR IGNORE INTO license_classifications
                (license_number, classification_code, issue_date)
            VALUES (?, ?, ?)
            """,
            (rec.license_number, code, issue_date),
        )

    html_hash = hashlib.sha256(rec.raw_html.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO license_snapshots
            (license_number, scraped_at, status, bond_company, bond_number,
             bond_amount, bond_expiration, workers_comp_status, raw_html_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (rec.license_number, now, rec.status, rec.bond_company, rec.bond_number,
         rec.bond_amount, rec.bond_expiration, rec.workers_comp_status, html_hash),
    )

    for action_date, description in rec.disciplinary_actions:
        conn.execute(
            """
            INSERT INTO disciplinary_actions (license_number, action_date, description)
            VALUES (?, ?, ?)
            """,
            (rec.license_number, action_date, description),
        )

    conn.commit()


# ---------------------------------------------------------------------------
# SCRAPING
# ---------------------------------------------------------------------------

def search_zip(page: Page, zip_code: str) -> list[str]:
    """Run one ZIP+classification search, return license numbers found."""
    page.goto(SEARCH_URL)
    page.select_option(SELECTORS["classification_dropdown"], label=CLASSIFICATION)
    page.fill(SELECTORS["zip_input"], zip_code)
    page.click(SELECTORS["submit_button"])
    page.wait_for_load_state("networkidle")

    license_numbers = []
    rows = page.locator(SELECTORS["results_table_rows"])
    count = rows.count()
    for i in range(count):
        row = rows.nth(i)
        link = row.locator(SELECTORS["results_license_link"])
        if link.count() == 0:
            continue
        # License number is usually the link text or embedded in the href —
        # verify against what codegen shows you and adjust this extraction.
        license_numbers.append(link.inner_text().strip())

    log.info("ZIP %s: found %d %s licenses", zip_code, len(license_numbers), CLASSIFICATION)
    return license_numbers


def parse_detail_page(page: Page, license_number: str) -> LicenseRecord:
    """Parse a single license's detail page into a LicenseRecord."""
    rec = LicenseRecord(license_number=license_number)
    rec.raw_html = page.content()

    rec.business_name = page.locator(SELECTORS["detail_business_name"]).inner_text().strip()
    rec.status = page.locator(SELECTORS["detail_status"]).inner_text().strip()
    rec.entity_type = page.locator(SELECTORS["detail_entity_type"]).inner_text().strip()
    rec.qualifier_name = page.locator(SELECTORS["detail_qualifier_name"]).inner_text().strip()
    rec.address = page.locator(SELECTORS["detail_address"]).inner_text().strip()
    rec.phone = page.locator(SELECTORS["detail_phone"]).inner_text().strip()

    # Classifications, bond, workers' comp, and disciplinary sections are
    # each likely a repeated block on the page — once you see the real
    # markup via codegen, loop over the block similarly to results_table_rows
    # above rather than reading a single element.
    rec.classifications = []       # TODO: fill from detail_classifications_block
    rec.bond_company = ""          # TODO
    rec.bond_number = ""           # TODO
    rec.bond_amount = None         # TODO
    rec.bond_expiration = ""       # TODO
    rec.workers_comp_status = ""   # TODO
    rec.disciplinary_actions = []  # TODO: often absent — leave empty list if no block found

    return rec


def run_pilot():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        all_license_numbers: set[str] = set()

        for zip_code in TARGET_ZIPS:
            try:
                found = search_zip(page, zip_code)
                all_license_numbers.update(found)
            except Exception:
                log.exception("Search failed for ZIP %s", zip_code)
            time.sleep(REQUEST_DELAY_SECONDS)

        log.info("Total unique %s licenses in pilot region: %d",
                  CLASSIFICATION, len(all_license_numbers))

        for i, license_number in enumerate(sorted(all_license_numbers), start=1):
            try:
                detail_url = f"https://www2.cslb.ca.gov/OnlineServices/CheckLicenseII/LicenseDetail.aspx?LicNum={license_number}"
                page.goto(detail_url)
                page.wait_for_load_state("networkidle")
                rec = parse_detail_page(page, license_number)
                upsert_license(conn, rec)
                log.info("(%d/%d) stored %s — %s", i, len(all_license_numbers),
                          license_number, rec.business_name or "?")
            except Exception:
                log.exception("Failed on license %s", license_number)
            time.sleep(REQUEST_DELAY_SECONDS)

        browser.close()

    conn.close()
    log.info("Done. Data in %s", DB_PATH)


if __name__ == "__main__":
    run_pilot()
