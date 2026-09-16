"""
Fuzzy-match a candidate solar installer list against AZ ROC's FULL
licenses table (az_roc.db, built by pipelines/az/ingest.py) -- not just
the rows ingest.py already flagged is_solar_relevant=1.

Why this matters: ingest.py's own docstring says plainly that its
"solar" text heuristic (explicit "solar" in classification or business
name/DBA) will never catch a real solar installer operating under a
generic name and a plain CR-11 Electrical classification -- that's
precisely the AZ version of the classification trap this whole project
exists to solve. This script is the fix: source real installer names
externally (the same way match_candidates.py did for Michigan) and
match them against every license in the roster, regardless of what the
text heuristic already flagged.

A candidate matching a license that ALREADY has is_solar_relevant=1 is
not new information -- ingest.py's heuristic already caught it. What
this script exists to surface is candidates matching a license where
is_solar_relevant=0: those are the CR-11-hiding installers the current
directory is silently missing.

Usage:
    python match_candidates.py --candidates candidates_az.csv \
        --db az_roc.db --out matches_az.csv
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
    # same -- same reasoning and same fix as Michigan's matcher.
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
    ap.add_argument("--out", default="matches_az.csv")
    ap.add_argument("--min-score", type=float, default=MEDIUM_CONFIDENCE)
    ap.add_argument("--top-n", type=int, default=3)
    args = ap.parse_args()

    with open(args.candidates, encoding="utf-8-sig", newline="") as f:
        candidates = [row for row in csv.DictReader(f)]

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    licenses = conn.execute(
        "SELECT license_no, class_code, business_name, dba, class_detail, "
        "city, status, is_solar_relevant, expiration_date "
        "FROM licenses"
    ).fetchall()

    # ALSO search disciplinary_actions -- a revoked/suspended license drops
    # out of the `licenses` table entirely once ROC's active roster refreshes
    # (confirmed real example: Envision Solar Inc, license 330401, Revoked,
    # exists ONLY here, not in `licenses`). Skipping this table means the
    # matcher systematically misses exactly the companies a verification
    # directory most needs to surface. One license can have multiple
    # disciplinary rows (multiple case numbers); take the most recent
    # description per license_no as its status.
    disc_rows_raw = conn.execute(
        "SELECT license_no, business_name, description, case_number, "
        "license_class, first_seen_snapshot FROM disciplinary_actions "
        "ORDER BY first_seen_snapshot DESC"
    ).fetchall()
    disc_by_license = {}
    for row in disc_rows_raw:
        disc_by_license.setdefault(row["license_no"], row)  # first hit = most recent, due to ORDER BY

    # Precompute each license's normalized business_name AND dba ONCE, up
    # front -- 58,147 rows x 2 fields, done a single time. Doing this inside
    # the candidate loop instead (the original version of this script did)
    # means redoing it once per candidate: 44 candidates x 58,147 rows x 2
    # fields x regex-substitution-and-SequenceMatcher is several million
    # redundant operations for no reason. This is the same fix MI's matcher
    # already had; AZ's larger roster just makes skipping it far more costly.
    lic_precomputed = []
    for row in licenses:
        biz_norm, biz_tokens = normalize(row["business_name"])
        dba_norm, dba_tokens = normalize(row["dba"]) if row["dba"] else (None, None)
        lic_precomputed.append((row, biz_norm, biz_tokens, dba_norm, dba_tokens))

    disc_precomputed = []
    for license_no, row in disc_by_license.items():
        biz_norm, biz_tokens = normalize(row["business_name"])
        disc_precomputed.append((row, biz_norm, biz_tokens))

    results = []
    unmatched = []
    n_already_flagged = 0
    n_newly_surfaced = 0
    n_from_disciplinary = 0

    for cand in candidates:
        cand_name = cand["business_name"]
        cand_norm, cand_tokens = normalize(cand_name)
        scored = []
        for row, biz_norm, biz_tokens, dba_norm, dba_tokens in lic_precomputed:
            # Skip the expensive string-similarity calculation entirely when
            # there's zero shared distinctive vocabulary -- two names that
            # share NO words in common are not a plausible match regardless
            # of surface-level string similarity, and this is the large
            # majority of all candidate x license pairs. This is the actual
            # remaining bottleneck once redundant renormalization is fixed:
            # SequenceMatcher.ratio() alone, called ~5 million times, is
            # what makes this slow -- skipping it when jaccard is already 0
            # cuts that down by roughly the fraction of genuinely unrelated
            # pairs, which is the vast majority.
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
                s, matched_field = dba_score, "dba"
            else:
                s, matched_field = biz_score, "business_name"

            if s >= args.min_score:
                scored.append((s, matched_field, "licenses", row))

        for row, biz_norm, biz_tokens in disc_precomputed:
            if not (cand_tokens & biz_tokens):
                continue
            raw, overlap = score(cand_norm, cand_tokens, biz_norm, biz_tokens)
            ratio = SequenceMatcher(None, cand_norm, biz_norm).ratio()
            s = apply_confidence_ceiling(raw, overlap, ratio)
            if s >= args.min_score:
                scored.append((s, "business_name", "disciplinary_actions", row))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: args.top_n]
        if not top:
            unmatched.append(cand_name)
            continue
        for s, matched_field, source_table, row in top:
            if source_table == "disciplinary_actions":
                n_from_disciplinary += 1
                results.append(
                    {
                        "candidate_name": cand_name,
                        "candidate_city": cand.get("city", ""),
                        "matched_field": matched_field,
                        "matched_name": row["business_name"] or "",
                        "license_no": row["license_no"],
                        "class_code": row["license_class"] or "",
                        "class_detail": row["description"],
                        "license_city": "",
                        "status": row["description"],
                        "expiration_date": "",
                        "already_flagged_solar_relevant": "",
                        "source_table": "disciplinary_actions",
                        "match_score": round(s, 3),
                        "confidence": confidence_tier(s),
                    }
                )
                continue
            already_flagged = bool(row["is_solar_relevant"])
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
                    "license_no": row["license_no"],
                    "class_code": row["class_code"],
                    "class_detail": row["class_detail"],
                    "license_city": row["city"],
                    "status": row["status"],
                    "expiration_date": row["expiration_date"],
                    "already_flagged_solar_relevant": int(already_flagged),
                    "source_table": "licenses",
                    "match_score": round(s, 3),
                    "confidence": confidence_tier(s),
                }
            )

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "candidate_name", "candidate_city", "matched_field", "matched_name",
                "license_no", "class_code", "class_detail", "license_city", "status",
                "expiration_date", "already_flagged_solar_relevant", "source_table",
                "match_score", "confidence",
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
    print(f"  already flagged is_solar_relevant=1 in licenses (heuristic already caught these): {n_already_flagged}")
    print(f"  NEWLY SURFACED in licenses, is_solar_relevant=0 (gap #1 this script closes): {n_newly_surfaced}")
    print(f"  found ONLY in disciplinary_actions -- dropped off the active roster entirely (gap #2): {n_from_disciplinary}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
