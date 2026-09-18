"""
Fuzzy-match a candidate solar installer list against CA's FULL statewide
license roster (ca_all_licenses table in cslb_solar.db, persisted by
ingest_master.py) -- not just the C-46 (Solar) classification.

This is CA's version of the same fix already applied to AZ and FL: a
licensed C-10 (Electrical) or B (General Building) contractor can
legally perform solar installation work without holding a separate
C-46 credential. This script finds real candidate installer names that
match a CA license under ANY classification, and flags whether that
license already holds C-46 (not new information) or doesn't (the
actual gap this closes).

Usage:
    python match_statewide.py --candidates candidates.csv \
        --db cslb_solar.db --out matches_ca.csv
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
    ap.add_argument("--out", default="matches_ca_statewide.csv")
    ap.add_argument("--min-score", type=float, default=MEDIUM_CONFIDENCE)
    ap.add_argument("--top-n", type=int, default=3)
    args = ap.parse_args()

    with open(args.candidates, encoding="utf-8-sig", newline="") as f:
        candidates = list(csv.DictReader(f))

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    licenses = conn.execute(
        "SELECT license_number, business_name, city, county, classifications_raw, "
        "primary_status, secondary_status FROM ca_all_licenses"
    ).fetchall()
    print(f"Matching against {len(licenses):,} statewide license records ...")

    lic_precomputed = []
    for row in licenses:
        biz_norm, biz_tokens = normalize(row["business_name"])
        lic_precomputed.append((row, biz_norm, biz_tokens))

    results = []
    unmatched = []
    n_already_c46 = 0
    n_newly_surfaced = 0

    for cand in candidates:
        cand_name = cand["name"]
        cand_norm, cand_tokens = normalize(cand_name)
        scored = []
        for row, biz_norm, biz_tokens in lic_precomputed:
            if not (cand_tokens & biz_tokens):
                continue
            raw, overlap = score(cand_norm, cand_tokens, biz_norm, biz_tokens)
            ratio = SequenceMatcher(None, cand_norm, biz_norm).ratio()
            s = apply_confidence_ceiling(raw, overlap, ratio)
            if s >= args.min_score:
                scored.append((s, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: args.top_n]
        if not top:
            unmatched.append(cand_name)
            continue
        for s, row in top:
            classifications = row["classifications_raw"] or ""
            has_c46 = "C46" in classifications.replace("-", "").replace(" ", "")
            if has_c46:
                n_already_c46 += 1
            else:
                n_newly_surfaced += 1
            results.append(
                {
                    "candidate_name": cand_name,
                    "candidate_address": cand.get("address", ""),
                    "matched_name": row["business_name"],
                    "license_number": row["license_number"],
                    "license_city": row["city"],
                    "county": row["county"],
                    "classifications": classifications.strip(),
                    "already_has_c46": int(has_c46),
                    "primary_status": row["primary_status"],
                    "secondary_status": row["secondary_status"],
                    "match_score": round(s, 3),
                    "confidence": confidence_tier(s),
                }
            )

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "candidate_name", "candidate_address", "matched_name", "license_number",
                "license_city", "county", "classifications", "already_has_c46",
                "primary_status", "secondary_status", "match_score", "confidence",
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
    print(f"  already hold C-46 (not new): {n_already_c46}")
    print(f"  NEWLY SURFACED -- solar-relevant but no C-46 on file: {n_newly_surfaced}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
