"""
Match candidate solar-installer business names (sourced externally, e.g. from
Google Places) against a CSLB Data Portal export for a broad classification
(C-10, B) to find the subset that are actually solar installers operating
under a non-C-46 license.

This is a two-step, human-checked process by design — for a directory making
legal-standing claims about real businesses, a bad auto-match is worse than a
missed one. Step 1 (this script, `match` mode) produces a review CSV, not a
database write. You look it over, then step 2 (`load` mode) loads only the
rows you've confirmed.

Usage:
    # Step 1: generate a review file
    python match_candidates.py match \
        --candidates candidates.csv \
        --export scraper/exports/CSLBSearchData_101620438.xlsx \
        --out review_fresno_c10.csv

    # ... open review_fresno_c10.csv, check the "confirm" column for anything
    #     you want loaded, mark it "y" (rows already marked "y" by the
    #     auto-confidence threshold are pre-filled but still worth a glance) ...

    # Step 2: load only the confirmed rows into the database
    python match_candidates.py load \
        --review review_fresno_c10.csv \
        --export scraper/exports/CSLBSearchData_101620438.xlsx \
        --source-classification C-10

candidates.csv format (one row per candidate business, from Google Places or
any other source):
    name,address
    "Solar by Quality Home Services","4936 E Ashlan Ave Ste b, Fresno, CA 93726"
    "Stellar Solar, Inc.","6081 N First St STE 101, Fresno, CA 93710"
    ...

Install:
    pip install pandas openpyxl rapidfuzz
"""

import argparse
import re
import sys
import pandas as pd
from rapidfuzz import fuzz

from load_cslb_export import init_db, upsert_row, DB_PATH
import sqlite3

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Based on the Fresno C-10 test: 82 was a real match, everything below 80 in
# that sample was coincidental noise. Kept a buffer on both sides since one
# sample isn't enough to trust an exact cutoff.
AUTO_CONFIRM_THRESHOLD = 85   # pre-marks "confirm" = y, but still shown for review
REVIEW_THRESHOLD = 70         # below this, not even included in the review file

SUFFIX_WORDS = r'\b(INC|LLC|CO|CORP|CORPORATION|COMPANY)\b'


def normalize(name: str) -> str:
    name = str(name).upper()
    name = re.sub(r'[.,\'"]', '', name)
    name = re.sub(SUFFIX_WORDS, '', name)
    name = re.sub(r'[^A-Z0-9 ]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


# ---------------------------------------------------------------------------
# MATCH MODE
# ---------------------------------------------------------------------------

def run_match(candidates_path: str, export_path: str, out_path: str) -> None:
    candidates = pd.read_csv(candidates_path)
    export = pd.read_excel(export_path, dtype=str)
    export['norm_name'] = export['BusinessName'].apply(normalize)

    rows = []
    for _, cand in candidates.iterrows():
        cand_name = cand['name']
        cand_address = cand.get('address', '')
        norm_cand = normalize(cand_name)

        scores = export['norm_name'].apply(lambda x: fuzz.token_sort_ratio(norm_cand, x))
        best_idx = scores.idxmax()
        best_score = scores[best_idx]

        if best_score < REVIEW_THRESHOLD:
            continue  # not worth a human's time — clearly not a match

        matched = export.loc[best_idx]
        rows.append({
            'candidate_name': cand_name,
            'candidate_address': cand_address,
            'matched_license_number': matched['LicenseNumber'],
            'matched_business_name': matched['BusinessName'],
            'matched_address': f"{matched['Address']}, {matched['City']}, {matched['State']} {matched['ZIP Code']}",
            'matched_classifications': matched['Classification(s)'],
            'score': round(best_score, 1),
            'confirm': 'y' if best_score >= AUTO_CONFIRM_THRESHOLD else '',
        })

    if not rows:
        print(f"No candidates scored above {REVIEW_THRESHOLD} — nothing to review.")
        return

    review_df = pd.DataFrame(rows).sort_values('score', ascending=False)
    review_df.to_csv(out_path, index=False)
    print(f"Wrote {len(review_df)} candidate matches to {out_path}")
    print(f"  {sum(review_df['confirm'] == 'y')} pre-marked 'confirm=y' (score >= {AUTO_CONFIRM_THRESHOLD})")
    print(f"  {sum(review_df['confirm'] == '')} need your judgment (score {REVIEW_THRESHOLD}-{AUTO_CONFIRM_THRESHOLD})")
    print("Open the file, check the 'confirm' column, mark any you want loaded with 'y', then run 'load' mode.")


# ---------------------------------------------------------------------------
# LOAD MODE
# ---------------------------------------------------------------------------

def run_load(review_path: str, export_path: str, source_classification: str) -> None:
    review = pd.read_csv(review_path, dtype=str)
    confirmed = review[review['confirm'].str.lower() == 'y']

    if confirmed.empty:
        print("No rows marked confirm='y' in the review file — nothing to load.")
        return

    export = pd.read_excel(export_path, dtype=str)
    confirmed_license_numbers = set(confirmed['matched_license_number'].astype(str))
    matched_rows = export[export['LicenseNumber'].astype(str).isin(confirmed_license_numbers)]

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    for _, row in matched_rows.iterrows():
        upsert_row(conn, row)
    conn.commit()

    print(f"Loaded {len(matched_rows)} confirmed {source_classification} license(s) into {DB_PATH}")
    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='mode', required=True)

    match_p = sub.add_parser('match', help='Generate a review CSV of candidate matches')
    match_p.add_argument('--candidates', required=True, help='CSV of candidate businesses (name, address)')
    match_p.add_argument('--export', required=True, help='CSLB Data Portal export .xlsx (C-10 or B, one county)')
    match_p.add_argument('--out', required=True, help='Where to write the review CSV')

    load_p = sub.add_parser('load', help='Load confirmed matches from a reviewed CSV into the database')
    load_p.add_argument('--review', required=True, help='The review CSV, with confirm column filled in')
    load_p.add_argument('--export', required=True, help='The same .xlsx export used to generate the review CSV')
    load_p.add_argument('--source-classification', required=True, help='e.g. C-10 or B, for the log message only')

    args = parser.parse_args()

    if args.mode == 'match':
        run_match(args.candidates, args.export, args.out)
    elif args.mode == 'load':
        run_load(args.review, args.export, args.source_classification)


if __name__ == '__main__':
    main()
