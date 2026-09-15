"""
Enrich matched candidates with real License Status and (for M&A hits)
Classifications, by fetching each license's detail page directly.

The bulk CSV export has neither field -- both only exist on the
per-record detail page (LicenseeDetail.aspx), which is a plain
unauthenticated GET built from License Number + License Type, already
present as `detail_url` in matches_mi.csv.

IMPORTANT -- this script is UNTESTED against the live site. I have no
network path to aca-prod.accela.com from where I'm writing this, so the
HTML/text parsing below is built from screenshots of the detail page,
not a live fetch. Run it on a SMALL batch first (--limit 5) and paste
back the output (including any parse warnings) before running it on
the full match list, so parsing can be corrected against what the page
actually sends back rather than what I assumed it looks like.

Usage:
    python enrich_details.py --matches matches_mi.csv --out enriched_mi.csv --limit 5
    # once that looks right:
    python enrich_details.py --matches matches_mi.csv --out enriched_mi.csv
"""

import argparse
import csv
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Section headers the detail page uses to bound the Classifications list --
# whatever comes after CLASSIFICATIONS and before one of these is the list.
SECTION_STOPS = ("Related Records", "Public Documents")


def fetch_detail(session: requests.Session, url: str) -> str:
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_status_and_classifications(html: str):
    """
    Returns (license_status, classifications_list, warnings).
    Parsing works on the page's visible text rather than specific CSS
    classes/IDs, since I don't have the live DOM to key off of -- more
    robust to markup differences, at the cost of being a bit loose.
    """
    warnings = []
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]  # drop blank lines

    # --- License Status ---
    status = None
    for i, ln in enumerate(lines):
        if ln.startswith("License Status"):
            # Value is often on the same line after the colon; if not,
            # take the next non-empty line.
            after_colon = ln.split(":", 1)
            if len(after_colon) == 2 and after_colon[1].strip():
                status = after_colon[1].strip()
            elif i + 1 < len(lines):
                status = lines[i + 1]
            break
    if status is None:
        warnings.append("Could not find 'License Status' label in page text")

    # --- Classifications (M&A trade codes) ---
    classifications = []
    if "CLASSIFICATIONS" in lines:
        start = lines.index("CLASSIFICATIONS") + 1
        for ln in lines[start:]:
            if any(ln.startswith(stop) for stop in SECTION_STOPS):
                break
            # Expected shape: "-: Roofing (M)" or "Roofing (M)"
            m = re.search(r"([A-Za-z][A-Za-z /&]*)\s*\(([A-Z])\)", ln)
            if m:
                classifications.append(f"{m.group(1).strip()} ({m.group(2)})")
    # Absence of a CLASSIFICATIONS section is expected/normal for
    # Residential Builder and Electrical Contractor rows -- only M&A
    # licenses carry trade classifications. Not a warning on its own.

    return status, classifications, warnings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--matches", required=True, help="matches_mi.csv from match_candidates.py")
    ap.add_argument("--out", default="enriched_mi.csv")
    ap.add_argument(
        "--min-confidence",
        choices=["high", "medium", "low"],
        default="high",
        help="Only enrich rows at or above this confidence tier (default: high)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N rows -- use this for the initial live-site test",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between requests (default 1.0 -- be polite to a state server)",
    )
    args = ap.parse_args()

    tier_rank = {"high": 2, "medium": 1, "low": 0}
    min_rank = tier_rank[args.min_confidence]

    with open(args.matches, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if tier_rank.get(r["confidence"], 0) >= min_rank]

    # Dedupe by (license_number, license_type) -- top-n=3 in the matcher
    # can list the same license under more than one candidate row.
    seen = set()
    unique_rows = []
    for r in rows:
        key = (r["license_number"], r["license_type"])
        if key not in seen:
            seen.add(key)
            unique_rows.append(r)

    if args.limit:
        unique_rows = unique_rows[: args.limit]

    if not unique_rows:
        sys.exit(f"No rows at or above confidence '{args.min_confidence}' found in {args.matches}")

    session = requests.Session()
    out_rows = []
    print(f"Fetching {len(unique_rows)} detail page(s)...")
    for i, row in enumerate(unique_rows, 1):
        url = row["detail_url"]
        status, classifications, warnings = None, [], []
        try:
            html = fetch_detail(session, url)
            status, classifications, warnings = parse_status_and_classifications(html)
        except requests.RequestException as e:
            warnings.append(f"Request failed: {e}")

        print(
            f"  [{i}/{len(unique_rows)}] {row['candidate_name']:30s} "
            f"({row['license_type']}) -> status={status!r}"
            + (f", classifications={classifications}" if classifications else "")
            + (f"  WARNINGS: {warnings}" if warnings else "")
        )

        out_rows.append(
            {
                **row,
                "license_status": status,
                "classifications": "; ".join(classifications),
                "fetch_warnings": "; ".join(warnings),
            }
        )
        if i < len(unique_rows):
            time.sleep(args.delay)

    fieldnames = list(out_rows[0].keys())
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    n_warned = sum(1 for r in out_rows if r["fetch_warnings"])
    print(f"\nWrote {len(out_rows)} rows to {args.out}")
    if n_warned:
        print(f"{n_warned} row(s) had warnings -- check fetch_warnings column before trusting them.")


if __name__ == "__main__":
    main()
