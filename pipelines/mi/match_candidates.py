"""
Fuzzy-match a candidate solar installer list against the licenses loaded
by ingest.py into mi_lara_solar.db.

Business names on the candidate list (sourced from EnergySage/Google
Places) frequently don't match LARA's registered name verbatim -- e.g.
"Palmetto Energy" (candidate) vs "Palmetto Solar LLC" (LARA), or a
company operating under a DBA that differs from its licensed legal
name. This does token-overlap + string-similarity scoring rather than
exact matching, and buckets results into confidence tiers so low-
confidence guesses get a human look rather than silently entering or
silently being dropped from the directory.

Usage:
    python match_candidates.py --candidates candidates_mi.csv \
        --db mi_lara_solar.db --out matches_mi.csv
"""

import argparse
import csv
import re
import sqlite3
from difflib import SequenceMatcher

# Suffixes/words stripped before comparison -- generic enough that they
# add noise to matching rather than signal (two unrelated companies both
# being "LLC" tells you nothing).
NOISE_WORDS = {
    "LLC", "INC", "CO", "CORP", "CORPORATION", "COMPANY", "LTD", "LLP",
    "THE", "OF", "AND", "&",
}

# Words that carry almost no distinguishing power in this industry --
# dozens of unrelated companies share them (e.g. "Cardinal Power Systems"
# vs "Luna Power Systems" share only GENERIC words and are not the same
# business). Stripped from the Jaccard token set so a match has to share
# something distinctive, not just the industry's vocabulary. Still present
# in the string used for the ratio score, so it isn't thrown away entirely.
GENERIC_WORDS = {
    "SOLAR", "ENERGY", "POWER", "ELECTRIC", "ELECTRICAL", "HOME", "HOMES",
    "SOLUTIONS", "SYSTEMS", "GREEN", "SUN", "RENEWABLE", "SERVICES",
}

HIGH_CONFIDENCE = 0.80
MEDIUM_CONFIDENCE = 0.55


def normalize(name: str):
    """Return (normalized_string, distinctive_token_set) for comparison."""
    name = name.upper()
    name = re.sub(r"[^\w\s&]", " ", name)  # drop punctuation except &
    all_tokens = [t for t in name.split() if t not in NOISE_WORDS]
    normalized = " ".join(all_tokens)
    distinctive = [t for t in all_tokens if t not in GENERIC_WORDS]
    # If stripping generic words leaves nothing (e.g. a name that's
    # literally just "Solar Solutions"), fall back to the full token set
    # rather than matching against an empty one.
    tokens = set(distinctive) if distinctive else set(all_tokens)
    return normalized, tokens


def score(cand_norm, cand_tokens, lic_norm, lic_tokens):
    if not cand_tokens or not lic_tokens:
        return 0.0, 0
    # Token Jaccard over DISTINCTIVE words only -- catches "Palmetto Energy"
    # vs "Palmetto Solar" via the shared proper noun, without letting two
    # unrelated companies match just because both say "Power Systems."
    overlap = cand_tokens & lic_tokens
    union = cand_tokens | lic_tokens
    jaccard = len(overlap) / len(union) if union else 0.0
    # String similarity -- catches near-identical spellings/typos that
    # token overlap alone would miss.
    ratio = SequenceMatcher(None, cand_norm, lic_norm).ratio()
    # Weighted toward token overlap: for short business names, sharing
    # the distinctive word(s) matters more than overall string shape.
    raw = 0.65 * jaccard + 0.35 * ratio
    return raw, len(overlap)


def apply_confidence_ceiling(raw_score, overlap_count, ratio):
    """
    A single shared word (e.g. both names happen to include "Absolute" or
    "Sky") is not enough to call two businesses the same -- "Absolute
    Solar" and "Absolute Electric" are two plausibly unrelated companies
    that happen to share one common word. Cap such matches at medium
    confidence regardless of the raw score, unless the full names are
    already near-identical (ratio very high, i.e. almost the same string
    overall, not just one overlapping token).
    """
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
    ap.add_argument("--candidates", required=True, help="Candidate CSV (business_name,city,...)")
    ap.add_argument("--db", required=True, help="mi_lara_solar.db from ingest.py")
    ap.add_argument("--out", default="matches_mi.csv", help="Output matches CSV")
    ap.add_argument(
        "--min-score",
        type=float,
        default=MEDIUM_CONFIDENCE,
        help="Drop matches scoring below this (default: medium-confidence floor, 0.55)",
    )
    ap.add_argument(
        "--top-n",
        type=int,
        default=3,
        help="Keep up to N best license matches per candidate (default 3, since one "
        "company legitimately holds licenses under all three types)",
    )
    args = ap.parse_args()

    with open(args.candidates, encoding="utf-8-sig", newline="") as f:
        candidates = [row for row in csv.DictReader(f)]

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    licenses = conn.execute(
        "SELECT license_number, license_type, license_bucket, display_name, "
        "expiration_date, is_expired, city, detail_url FROM licenses"
    ).fetchall()

    # Precompute normalization once per license row (38,913 of them --
    # doing this inside the candidate loop would be ~35x slower for no reason).
    lic_norms = [(normalize(row["display_name"]), row) for row in licenses]

    results = []
    unmatched = []
    for cand in candidates:
        cand_name = cand["business_name"]
        cand_norm, cand_tokens = normalize(cand_name)
        scored = []
        for (lic_norm, lic_tokens), row in lic_norms:
            raw, overlap_count = score(cand_norm, cand_tokens, lic_norm, lic_tokens)
            ratio = SequenceMatcher(None, cand_norm, lic_norm).ratio()
            s = apply_confidence_ceiling(raw, overlap_count, ratio)
            if s >= args.min_score:
                scored.append((s, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: args.top_n]
        if not top:
            unmatched.append(cand_name)
            continue
        for s, row in top:
            results.append(
                {
                    "candidate_name": cand_name,
                    "candidate_city": cand.get("city", ""),
                    "matched_display_name": row["display_name"],
                    "license_number": row["license_number"],
                    "license_type": row["license_type"],
                    "license_bucket": row["license_bucket"],
                    "expiration_date": row["expiration_date"],
                    "is_expired": row["is_expired"],
                    "license_city": row["city"],
                    "match_score": round(s, 3),
                    "confidence": confidence_tier(s),
                    "detail_url": row["detail_url"],
                }
            )

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "candidate_name", "candidate_city", "matched_display_name",
                "license_number", "license_type", "license_bucket",
                "expiration_date", "is_expired", "license_city",
                "match_score", "confidence", "detail_url",
            ],
        )
        writer.writeheader()
        writer.writerows(results)

    n_high = sum(1 for r in results if r["confidence"] == "high")
    n_med = sum(1 for r in results if r["confidence"] == "medium")
    print(f"Candidates: {len(candidates)}")
    print(f"Unmatched (no license scored >= {args.min_score}): {len(unmatched)}")
    for name in unmatched:
        print(f"    - {name}")
    print(f"Matched rows written: {len(results)}  (high: {n_high}, medium: {n_med})")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
