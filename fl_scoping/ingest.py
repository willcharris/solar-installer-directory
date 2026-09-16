"""
Florida DBPR solar-contractor ingest pipeline.

Rebuilds fl_solar_filter.py's exploration into a persistent pipeline
matching the convention used for AZ/TX/MI: everything lands in a SQLite
db, not just console output. The original script's diagnostic printouts
are kept (they're genuinely useful for catching a DBPR column reorder
before it silently corrupts the parse), but the fatal gap -- the script
built an out_path and then never actually wrote to it -- is fixed here.

Florida's situation is a hybrid unlike any other state in this project:
  - "CVC" (Certified Solar Contractor) is a REAL, DEDICATED solar
    occupation code -- the clean case CA/AZ/MI don't have.
  - BUT Florida law also lets a plain Electrical Contractor (EC) do PV
    installation work without holding a CV credential at all. A name
    heuristic (SOLAR_NAME_HINTS) catches SOME of these, but -- exactly
    like AZ's is_solar_relevant heuristic -- it will silently miss any
    EC-licensed installer with a non-obvious business name. That gap is
    NOT fixed here; it needs an AZ-style match_candidates.py pass against
    the full EC roster later. This ingest just makes the data persistent
    and queryable so that future step has something to build on.

Usage:
    python ingest.py --db fl_solar.db --out-dir fl_solar_out
"""

import argparse
import sqlite3
from pathlib import Path

import pandas as pd
import requests

CONSTRUCTION_URL = "https://www2.myfloridalicense.com/sto/file_download/extracts//CONSTRUCTIONLICENSE_1.csv"
ELECTRICAL_URL = "https://www2.myfloridalicense.com/sto/file_download/extracts/lic08el.csv"

DISCIPLINE_URLS = {
    "FY25_26": "https://www2.myfloridalicense.com/pro/cilb/reports/contractor_disc_lic_2526.csv",
    "FY24_25": "https://www2.myfloridalicense.com/pro/cilb/reports/contractor_disc_lic_2425.csv",
    "FY23_24": "https://www2.myfloridalicense.com/pro/cilb/reports/contractor_disc_lic_2324.csv",
}

LICENSEE_COLUMNS = [
    "board_number", "occupation_code", "licensee_name", "dba_name",
    "class_code", "addr1", "addr2", "addr3", "city", "state", "zip",
    "county_code", "license_number", "primary_status", "secondary_status",
    "orig_licensure_date", "effective_date", "expiration_date",
    "_blank", "renewal_period", "alt_license_number", "_trailing",
]

# Heuristic only -- catches SOME EC-licensed solar installers by name,
# will miss non-obvious names. This is FL's version of AZ's
# is_solar_relevant gap; not fixed by this ingest, flagged for a future
# match_candidates.py pass against the full EC roster.
SOLAR_NAME_HINTS = ["SOLAR", "PHOTOVOLTAIC", " PV ", "SUNPOWER", "SUN POWER"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS fl_solar_licensees (
    license_number TEXT,
    licensee_name TEXT,
    dba_name TEXT,
    occupation_code TEXT,
    class_code TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    county_code TEXT,
    primary_status TEXT,
    secondary_status TEXT,
    orig_licensure_date TEXT,
    expiration_date TEXT,
    match_type TEXT NOT NULL,          -- 'CVC' or 'EC name-matched'
    discipline_count INTEGER,          -- NULL for EC name-matched rows -- see discipline_note
    latest_discipline_date TEXT,
    dispositions TEXT,
    discipline_note TEXT,              -- explicit gap flag for EC rows (see module docstring)
    PRIMARY KEY (license_number, match_type)
);
"""


def download_csv(url: str, dest: Path) -> Path:
    if dest.exists():
        print(f"  (using cached) {dest}")
        return dest
    print(f"  downloading {url}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def load_licensee_extract(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, names=LICENSEE_COLUMNS, dtype=str,
                      low_memory=False, index_col=False)
    return df.drop(columns=["_blank", "_trailing"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="fl_solar.db")
    ap.add_argument("--out-dir", default="fl_solar_out")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)

    print("STEP 0: fetching raw extracts")
    constr_csv = download_csv(CONSTRUCTION_URL, out_dir / "construction_raw.csv")
    elec_csv = download_csv(ELECTRICAL_URL, out_dir / "electrical_raw.csv")

    print("\nFirst 2 raw rows of construction extract (eyeball this -- if columns")
    print("look shifted from LICENSEE_COLUMNS above, DBPR changed their layout):")
    print(pd.read_csv(constr_csv, header=None, nrows=2, dtype=str).to_string())

    print("\nSTEP 1: loading + parsing")
    constr = load_licensee_extract(constr_csv)
    elec = load_licensee_extract(elec_csv)
    print(f"  construction rows: {len(constr):,}  |  electrical rows: {len(elec):,}")

    print("\nSTEP 2: filtering to solar-relevant licensees")
    cv_solar = constr[constr["occupation_code"].str.strip().eq("CVC")].copy()
    cv_solar["match_type"] = "CVC"

    name_hits = elec[
        elec["licensee_name"].str.contains("|".join(SOLAR_NAME_HINTS), case=False, na=False)
        | elec["dba_name"].str.contains("|".join(SOLAR_NAME_HINTS), case=False, na=False)
    ].copy()
    name_hits["match_type"] = "EC name-matched"

    print(f"  CVC-classified: {len(cv_solar):,}  |  EC name-matched: {len(name_hits):,}")

    print("\nSTEP 3: loading disciplinary history")
    discipline_frames = []
    for label, url in DISCIPLINE_URLS.items():
        p = download_csv(url, out_dir / f"discipline_{label}.csv")
        d = pd.read_csv(p, dtype=str, low_memory=False)
        d["fiscal_year"] = label
        discipline_frames.append(d)
    discipline = pd.concat(discipline_frames, ignore_index=True)

    print("\nSTEP 4: joining disciplinary history, scoped to Certified Solar Contractor only")
    # Scoping by License Type (not just license number) matters -- FL
    # reuses license numbers across unrelated license types over time, so
    # an unscoped join could attach a plumbing or roofing discipline
    # record to a solar contractor's history by coincidence.
    solar_discipline = discipline[discipline["License Type"] == "Certified Solar Contractor"].copy()
    solar_discipline["license_nbr_stripped"] = solar_discipline["License Nbr"].str.strip().str.lstrip("0")
    cv_solar["license_nbr_stripped"] = cv_solar["license_number"].str.strip().str.lstrip("0")

    disc_summary = (
        solar_discipline.groupby("license_nbr_stripped")
        .agg(discipline_count=("Complaint Nbr", "nunique"),
             latest_discipline_date=("Disposition Date", "max"),
             dispositions=("Disposition", lambda s: "; ".join(sorted(set(s.dropna())))))
        .reset_index()
    )
    cv_solar = cv_solar.merge(disc_summary, on="license_nbr_stripped", how="left")
    cv_solar["discipline_count"] = cv_solar["discipline_count"].fillna(0).astype(int)
    cv_solar["discipline_note"] = None
    print(f"  CVC licensees with at least one disciplinary record: {(cv_solar['discipline_count'] > 0).sum()}")

    # EC name-matched rows: no discipline check has been done for these at
    # all -- flag explicitly so a report page never implies "clean" for a
    # source that was never actually checked.
    name_hits["discipline_count"] = None
    name_hits["latest_discipline_date"] = None
    name_hits["dispositions"] = None
    name_hits["discipline_note"] = "Not checked -- CILB discipline file scoping doesn't cover EC licenses"

    solar_all = pd.concat([cv_solar, name_hits], ignore_index=True)

    print(f"\nSTEP 5: writing {args.db}")
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    cols = [
        "license_number", "licensee_name", "dba_name", "occupation_code", "class_code",
        "city", "state", "zip", "county_code", "primary_status", "secondary_status",
        "orig_licensure_date", "expiration_date", "match_type", "discipline_count",
        "latest_discipline_date", "dispositions", "discipline_note",
    ]
    solar_all[cols].to_sql("fl_solar_licensees", conn, if_exists="replace", index=False)
    conn.close()
    print(f"  wrote {len(solar_all):,} rows to {args.db}")


if __name__ == "__main__":
    main()
