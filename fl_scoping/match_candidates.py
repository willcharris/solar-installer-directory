"""
Fuzzy-match a candidate solar installer list against FL's FULL Electrical
Contractor roster (fl_electrical_contractors_all table in fl_solar.db,
persisted by ingest.py) -- not just the SOLAR_NAME_HINTS-filtered subset
already in fl_solar_licensees.

This is FL's version of the same fix already applied to AZ: Florida law
lets a licensed Electrical Contractor perform solar installation work
without holding the separate Certified Solar Contractor (CVC) credential,
and ingest.py's own docstring admits its name-hint heuristic "will miss
installers with non-obvious names." This script is the fix -- source
real installer names externally and match them against every EC license,
regardless of what the name-hint heuristic already caught.

Usage:
    python match_candidates.py --candidates candidates_fl.csv \
        --db fl_solar.db --out matches_fl.csv
"""

import argparse
import csv
import re
import sqlite3
from difflib import SequenceMatcher

NOISE_WORDS = {
    "LLC", "INC", "CO", "CORP", "CORPORATION", "COMPANY", "LTD", "LLP",
    "THE", "OF", "AND", "&",
}

GENERIC_WORDS = {
    "SOLAR", "ENERGY", "POWER", "ELECTRIC", "ELECTRICAL", "HOME", "HOMES",
    "SOLUTIONS", "SYSTEMS", "GREEN", "SUN", "RENEWABLE", "SERVICES",
}

HIGH_CONFIDENCE = 0.80
MEDIUM_CONFIDENCE = 0.55


def normalize(name: str):
    name = (name or "").upper()
    name = re.sub(r"[^\w\s&]", " ", name)
    all_tokens = [t for t in name.split() if t not in NOISE_WORDS]
    normalized = " ".join(all_tokens)
    distinctive = [t for t in all_tokens if t not in GENERIC_WORDS]
    tokens = set(distinctive) if distinctive else set(all_tokens)
    return normalized, tokens


def score(cand_norm, cand_tokens, lic_norm, lic_tokens):
    if not cand_tokens or not lic_tokens:
        return 0.0, 0
    overlap = cand_tokens & lic_tokens
    union = cand_tokens | lic_tokens
    jaccard = len(overlap) / len(union) if union else 0.0
    ratio = SequenceMatcher(None, cand_norm, lic_norm).ratio()
    raw = 0.65 * jaccard + 0.35 * ratio
    return raw, len(overlap)


def apply_confidence_ceiling(raw_score, overlap_count, ratio):
    # A single shared common word isn't enough to call two businesses the
    # same -- same reasoning and fix as AZ's and MI's matchers.
    if overlap_count < 2 and ratio < 0.90:
        return min(raw_score, MEDIUM_CONFIDENCE + 0.05)
    return raw_score


def confidence_tier(s: float) -> str:
    if s >= HIGH_CONFIDENCE:
        return "high"
    if s >= MEDIUM_CONFIDENCE:
        return "medium"
    return "low"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default="matches_fl.csv")
    ap.add_argument("--min-score", type=float, default=MEDIUM_CONFIDENCE)
    ap.add_argument("--top-n", type=int, default=3)
    args = ap.parse_args()

    with open(args.candidates, encoding="utf-8-sig", newline="") as f:
        candidates = [row for row in csv.DictReader(f)]

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    licenses = conn.execute(
        "SELECT license_number, licensee_name, dba_name, city, primary_status, "
        "secondary_status, expiration_date FROM fl_electrical_contractors_all"
    ).fetchall()

    # Also pull the license numbers already in fl_solar_licensees as
    # "EC name-matched" -- a candidate matching one of these is not new
    # information, the existing heuristic already caught it.
    already_caught = {
        r[0] for r in conn.execute(
            "SELECT license_number FROM fl_solar_licensees WHERE match_type = 'EC name-matched'"
        ).fetchall()
    }

    # Precompute normalization once, up front, for both licensee_name and
    # dba_name -- same performance fix already applied to AZ's matcher.
    # FL's roster is ~19,700 rows; skipping this would make the same
    # mistake that made AZ's first pass take 2+ minutes before the fix.
    lic_precomputed = []
    for row in licenses:
        biz_norm, biz_tokens = normalize(row["licensee_name"])
        dba_norm, dba_tokens = normalize(row["dba_name"]) if row["dba_name"] else (None, None)
        lic_precomputed.append((row, biz_norm, biz_tokens, dba_norm, dba_tokens))

    results = []
    unmatched = []
    n_already_flagged = 0
    n_newly_surfaced = 0

    for cand in candidates:
        cand_name = cand["business_name"]
        cand_norm, cand_tokens = normalize(cand_name)
        scored = []
        for row, biz_norm, biz_tokens, dba_norm, dba_tokens in lic_precomputed:
            biz_score = 0.0
            if cand_tokens & biz_tokens:
                biz_raw, biz_overlap = score(cand_norm, cand_tokens, biz_norm, biz_tokens)
                biz_ratio = SequenceMatcher(None, cand_norm, biz_norm).ratio()
                biz_score = apply_confidence_ceiling(biz_raw, biz_overlap, biz_ratio)

            dba_score = 0.0
            if dba_tokens and (cand_tokens & dba_tokens):
                dba_raw, dba_overlap = score(cand_norm, cand_tokens, dba_norm, dba_tokens)
                dba_ratio = SequenceMatcher(None, cand_norm, dba_norm).ratio()
                dba_score = apply_confidence_ceiling(dba_raw, dba_overlap, dba_ratio)

            if dba_score > biz_score:
                s, matched_field = dba_score, "dba_name"
            else:
                s, matched_field = biz_score, "licensee_name"

            if s >= args.min_score:
                scored.append((s, matched_field, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: args.top_n]
        if not top:
            unmatched.append(cand_name)
            continue
        for s, matched_field, row in top:
            already_flagged = row["license_number"] in already_caught
            if already_flagged:
                n_already_flagged += 1
            else:
                n_newly_surfaced += 1
            results.append(
                {
                    "candidate_name": cand_name,
                    "candidate_city": cand.get("city", ""),
                    "matched_field": matched_field,
                    "matched_name": row[matched_field] or "",
                    "license_number": row["license_number"],
                    "license_city": row["city"],
                    "primary_status": row["primary_status"],
                    "secondary_status": row["secondary_status"],
                    "expiration_date": row["expiration_date"],
                    "already_flagged_ec_name_matched": int(already_flagged),
                    "match_score": round(s, 3),
                    "confidence": confidence_tier(s),
                }
            )

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "candidate_name", "candidate_city", "matched_field", "matched_name",
                "license_number", "license_city", "primary_status", "secondary_status",
                "expiration_date", "already_flagged_ec_name_matched", "match_score", "confidence",
            ],
        )
        writer.writeheader()
        writer.writerows(results)

    n_high = sum(1 for r in results if r["confidence"] == "high")
    n_med = sum(1 for r in results if r["confidence"] == "medium")
    print(f"Candidates: {len(candidates)}")
    print(f"Unmatched: {len(unmatched)}")
    for name in unmatched:
        print(f"    - {name}")
    print(f"Matched rows written: {len(results)}  (high: {n_high}, medium: {n_med})")
    print(f"  already flagged by name-hint heuristic (not new): {n_already_flagged}")
    print(f"  NEWLY SURFACED -- this is the actual gap this script closes: {n_newly_surfaced}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
