"""
Florida DBPR solar-contractor filter
-------------------------------------
Pulls the two public CSV extracts DBPR publishes (no scraping needed),
isolates solar-relevant licensees, and joins in disciplinary history.

DBPR ships these extracts WITHOUT a header row, so column names below
are assigned positionally from the "File Layout Information" published
on each board's Public Records page. If DBPR ever reorders or adds a
column, this will silently misalign -- that's why STEP 0 prints the raw
first few rows before anything else, so you can eyeball it before
trusting the parsed output.
"""

import pandas as pd
import requests
from pathlib import Path

OUT_DIR = Path("fl_solar_out")
OUT_DIR.mkdir(exist_ok=True)

# ---- Source URLs (direct CSV downloads, refreshed weekly by DBPR) ----
CONSTRUCTION_URL = "https://www2.myfloridalicense.com/sto/file_download/extracts//CONSTRUCTIONLICENSE_1.csv"
ELECTRICAL_URL = "https://www2.myfloridalicense.com/sto/file_download/extracts/lic08el.csv"

DISCIPLINE_URLS = {
    "FY25_26": "https://www2.myfloridalicense.com/pro/cilb/reports/contractor_disc_lic_2526.csv",
    "FY24_25": "https://www2.myfloridalicense.com/pro/cilb/reports/contractor_disc_lic_2425.csv",
    "FY23_24": "https://www2.myfloridalicense.com/pro/cilb/reports/contractor_disc_lic_2324.csv",
}

# Positional column names per DBPR's published "File Layout Information"
# for the Construction and Electrical licensee extracts.
LICENSEE_COLUMNS = [
    "board_number", "occupation_code", "licensee_name", "dba_name",
    "class_code", "addr1", "addr2", "addr3", "city", "state", "zip",
    "county_code", "license_number", "primary_status", "secondary_status",
    "orig_licensure_date", "effective_date", "expiration_date",
    "_blank", "renewal_period", "alt_license_number", "_trailing",
]

# Names/DBAs that clearly signal a solar business even without the CV
# class code -- catches PV installers licensed only as Electrical
# Contractors (FL statute lets EC-licensed contractors do PV work
# without holding CV). This is a heuristic, not a legal determination --
# it will miss installers with non-obvious names and may over-flag
# unrelated "solar" branding. Expand this list as you spot-check results.
SOLAR_NAME_HINTS = ["SOLAR", "PHOTOVOLTAIC", " PV ", "SUNPOWER", "SUN POWER"]


def download_csv(url: str, dest: Path) -> Path:
    """Download a file if we don't already have a local copy."""
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
    print("STEP 0: fetching raw extracts")
    constr_csv = download_csv(CONSTRUCTION_URL, OUT_DIR / "construction_raw.csv")
    elec_csv = download_csv(ELECTRICAL_URL, OUT_DIR / "electrical_raw.csv")

    # Sanity check before trusting the positional column mapping.
    print("\nFirst 2 raw rows of construction extract (eyeball this):")
    print(pd.read_csv(constr_csv, header=None, nrows=2, dtype=str).to_string())

    print("\nSTEP 1: loading + parsing")
    constr = load_licensee_extract(constr_csv)
    elec = load_licensee_extract(elec_csv)
    print(f"  construction rows: {len(constr):,}  |  electrical rows: {len(elec):,}")

    print("\n  top occupation_code values:")
    print(constr["occupation_code"].value_counts().head(15))
    print("\n  top class_code values (non-null):")
    print(constr["class_code"].dropna().value_counts().head(15))
    print("\n  exact 'CV' matches:")
    print("  in occupation_code:", (constr["occupation_code"].str.strip() == "CV").sum())
    print("  in class_code:", (constr["class_code"].str.strip() == "CV").sum())

    print("\n  all unique occupation_code values containing 'V':")
    print(sorted(constr["occupation_code"].dropna().str.strip().unique()))
    print("\nSTEP 2: filtering to solar-relevant licensees")
    cv_solar = constr[constr["occupation_code"].str.strip().eq("CVC")].copy()
    cv_solar["source"] = "CILB occupation CVC"

    name_hits = elec[
        elec["licensee_name"].str.contains("|".join(SOLAR_NAME_HINTS), case=False, na=False)
        | elec["dba_name"].str.contains("|".join(SOLAR_NAME_HINTS), case=False, na=False)
    ].copy()
    name_hits["source"] = "EC name-matched"

    solar_all = pd.concat([cv_solar, name_hits], ignore_index=True)
    print(f"  CV-classified: {len(cv_solar):,}  |  EC name-matched: {len(name_hits):,}"
          f"  |  combined: {len(solar_all):,}")

    print("\nSTEP 3: joining disciplinary history by license number")
    discipline_frames = []
    for label, url in DISCIPLINE_URLS.items():
        p = download_csv(url, OUT_DIR / f"discipline_{label}.csv")
        d = pd.read_csv(p, dtype=str, low_memory=False)
        d["fiscal_year"] = label
        discipline_frames.append(d)
    discipline = pd.concat(discipline_frames, ignore_index=True)

    print(f"  discipline file columns: {list(discipline.columns)}")
    print("\n  sample 'License Nbr' values from discipline file:")
    print(discipline["License Nbr"].dropna().head(10).tolist())

    print("\nSTEP 4: joining disciplinary history, correctly scoped")

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
    print(f"  CVC licensees with at least one disciplinary record: {(cv_solar['discipline_count'] > 0).sum()}")

    # EC name-matched rows have no discipline source yet -- flag explicitly
    # rather than leaving a silently-empty column that looks like "clean."
    name_hits["discipline_count"] = pd.NA
    name_hits["discipline_note"] = "not checked -- CILB discipline file doesn't cover EC licenses"

    solar_all = pd.concat([cv_solar, name_hits], ignore_index=True)

    out_path = OUT_DIR / "fl_solar_licensees.csv"
    
if __name__ == "__main__":
    main()
