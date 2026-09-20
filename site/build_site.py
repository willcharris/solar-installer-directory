#!/usr/bin/env python3
"""
Builds the static site from all 5 states' data sources into ./docs/
(GitHub Pages convention: serve from /docs on the main branch).

Each state's loader decides, based on what that state's data actually
supports, what can honestly be shown -- not a uniform status scheme
forced across five very different data sources. Specifically:

  AZ  - heuristic-caught subset of a generic contractor roster
        (is_solar_relevant=1, status=Active). Real status data, but not
        a dedicated solar classification -- card shown as incomplete.
  TX  - complete dedicated Solar Residential Retailer registry.
        Sales-registration only; see the framing text on that page.
  FL  - CVC (dedicated solar classification) + EC name-matched rows,
        each labeled which is which in the table itself.
  CA  - complete dedicated C-46 registry, now with real status data
        from CSLB's statewide License Master file, plus a small set of
        manually-reviewed additions found under other classifications
        (C-10, B) via fuzzy-matching -- the same fix applied to AZ/FL.
  MI  - fully individually-verified curated set (19 businesses), the
        smallest and most manually-checked of the five.

Usage:
    python build_site.py --config sources.json --out docs
"""

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _title_city(city: str) -> str:
    """Normalize display capitalization -- source data is inconsistent
    (e.g. LARA's raw export mixes 'ALLENDALE' and 'portage')."""
    return (city or "").title()


def _slugify(name: str) -> str:
    """Same slugification as MI's generate_report.py --bulk-out -- must
    match exactly, or site links point at filenames that don't exist."""
    keep = "".join(c if c.isalnum() or c in " -" else "" for c in name)
    return "-".join(keep.lower().split())


def badge(label: str, kind: str) -> str:
    return f'<span class="badge badge-{kind}">{label}</span>'


def load_az(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = []
    for r in conn.execute(
        "SELECT license_no, business_name, dba, city, status FROM licenses "
        "WHERE is_solar_relevant = 1 AND status = 'Active' ORDER BY business_name"
    ):
        name = r["dba"] or r["business_name"]
        rows.append({
            "name": name, "city": _title_city(r["city"]), "detail_html": badge("Active", "green"),
            "report_url": f"reports/az/{r['license_no']}.pdf",
        })

    # Manually reviewed additions from the fuzzy-matching pass (candidates
    # not caught by the "solar" name/classification heuristic above --
    # e.g. plain-named electricians who genuinely do solar work). Each of
    # these 9 was individually checked against matches_az.csv and confirmed
    # to be a real, correct match, not a coincidental name overlap. Two
    # other high-scoring matches from that same pass -- "High Desert
    # Energy" (matched a business with an unrelated classification) and
    # "Prime Time Solar Energy LLC" (matched a differently-named company
    # on a single shared word, backed by only 1 online review) -- were
    # deliberately EXCLUDED as likely false positives. Do not add them
    # here without re-verifying independently.
    reviewed_additions = [
        ("355491", "River Sun Solutions LLC", "Lake Havasu City"),
        ("316266", "Clayco Electric Inc", "Tucson"),
        ("345197", "IntegrateSun LLC", "Houston"),
        ("319779", "T&K Electric Company LLC", "San Tan Valley"),
        ("328316", "Watt Masters LLC", "Phoenix"),
        ("310732", "Technicians for Sustainability Inc", "Tucson"),
        ("074683", "Goodman Electric", "Flagstaff"),
        ("332876", "EcoEnergy Solutions LLC", "Yuma"),
        ("293690", "Liggett Electrical Services LLC", "Somerton"),
    ]
    for license_no, name, city in reviewed_additions:
        rows.append({
            "name": name, "city": city, "detail_html": badge("Active", "green"),
            "report_url": f"reports/az/{license_no}.pdf",
        })

    rows.sort(key=lambda r: r["name"])
    conn.close()
    return rows


def load_tx(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = []
    for r in conn.execute(
        """
        SELECT t.license_number, t.business_name, t.city, s.status FROM tx_solar_retailers t
        LEFT JOIN (
            SELECT license_number, status,
                   ROW_NUMBER() OVER (PARTITION BY license_number ORDER BY scraped_at DESC) rn
            FROM tx_solar_retailer_snapshots
        ) s ON s.license_number = t.license_number AND s.rn = 1
        ORDER BY t.business_name
        """
    ):
        status = r["status"] or "Unknown"
        kind = "green" if status == "Current" else "neutral"
        rows.append({
            "name": r["business_name"], "city": _title_city(r["city"]), "detail_html": badge(status, kind),
            "report_url": f"reports/tx/{r['license_number']}.pdf",
        })
    conn.close()
    return rows


FL_CONFIRMED_STATUS = {("C", "A"): ("Active", "green"), ("C", "I"): ("Inactive", "amber")}


def load_fl(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = []
    for r in conn.execute(
        "SELECT license_number, licensee_name, dba_name, city, primary_status, secondary_status, match_type "
        "FROM fl_solar_licensees ORDER BY dba_name, licensee_name"
    ):
        name = (r["dba_name"] or "").strip() or r["licensee_name"]
        key = ((r["primary_status"] or "").strip(), (r["secondary_status"] or "").strip())
        if key in FL_CONFIRMED_STATUS:
            label, kind = FL_CONFIRMED_STATUS[key]
        else:
            label, kind = "Unverified status", "neutral"
        detail = badge(label, kind)
        if r["match_type"] == "EC name-matched":
            detail += ' <span style="font-size:0.75rem;color:var(--ink-soft);">(EC, name-matched)</span>'
        rows.append({
            "name": name, "city": _title_city(r["city"]), "detail_html": detail,
            "report_url": f"reports/fl/{r['license_number']}.pdf",
        })

    # Note: LUNEX POWER INC. (license 13014194) was manually reviewed and
    # added via the fuzzy-matching pass against FL's full EC roster (the
    # same fix already applied to AZ) -- it's inserted directly into the
    # fl_solar_licensees table (not appended here) so it flows through
    # this same query automatically. Of 5 unique high-confidence
    # candidates from that pass, only this one held up under review --
    # the other 4 (Tampa Bay Solar, Public Service Solar LLC, Coast To
    # Coast Solar, Coastal Energy) each matched an unrelated company that
    # happens to share a generic regional/civic name ("Tampa Bay", "Coast
    # to Coast", "Public Service") -- confirmed independently for at
    # least Tampa Bay Electric Inc via Florida's Sunbiz registry. Do not
    # add those without separately re-verifying each one.

    conn.close()
    return rows


def _ca_classify_status(primary: str, secondary: str):
    """Same logic as scraper/generate_report.py's classify_status --
    keep these two in sync. Only PRIMARY status triggers a red
    Suspended badge; a secondary flag containing "Susp" (e.g. "WC Susp
    Pending") describes a suspension that is pending, not yet in
    effect, and must not be shown as an active suspension."""
    primary = (primary or "").strip()
    secondary = (secondary or "").strip()
    if "Susp" in primary:
        return f"Suspended ({primary})", "red"
    if primary == "CLEAR" and not secondary:
        return "Active", "green"
    if primary == "CLEAR" and secondary:
        readable = "; ".join(s.strip() for s in secondary.split("|") if s.strip())
        return readable, "amber"
    if not primary:
        return "Unknown", "neutral"
    return f"{primary}" + (f" / {secondary}" if secondary else ""), "neutral"


def load_ca(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = []
    for r in conn.execute(
        "SELECT license_number, business_name, city, primary_status, secondary_status "
        "FROM licenses ORDER BY business_name"
    ):
        label, kind = _ca_classify_status(r["primary_status"], r["secondary_status"])
        rows.append({
            "name": r["business_name"], "city": _title_city(r["city"]), "detail_html": badge(label, kind),
            "report_url": f"reports/ca/{r['license_number']}.pdf",
        })
    conn.close()
    return rows


def load_mi(enriched_csv_path: str) -> list[dict]:
    by_name = defaultdict(list)
    with open(enriched_csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["confidence"] != "high":
                continue
            by_name[r["candidate_name"]].append(r)

    rows = []
    for name, recs in sorted(by_name.items()):
        badges = []
        for r in recs:
            status = (r.get("license_status") or "Unknown").strip()
            kind = "green" if status == "Issued" else ("amber" if status in ("Inactive",) else "red")
            short_type = r["license_type"].replace("Residential Builder", "RB").replace(" Company", "")
            badges.append(badge(f"{short_type}: {status}", kind))
        rows.append({
            "name": name, "city": _title_city(recs[0].get("license_city", "")), "detail_html": " ".join(badges),
            "report_url": f"reports/mi/{_slugify(name)}.pdf",
        })
    return rows


STATE_LOADERS = {
    "az": {
        "name": "Arizona", "loader": load_az, "complete": False,
        "framing": (
            "Arizona has no dedicated solar contractor classification. This list is every business "
            "in the state's full contractor roster whose name or classification text contains "
            "\"solar\" and whose license is currently Active — a reliable but not exhaustive signal, "
            "since a real installer with a generic business name (e.g. operating simply as an "
            "\"Electrical Contractor\") would not be caught by this method. A separate matching pass "
            "has identified additional likely installers not yet individually reviewed; they are not "
            "included here yet."
        ),
        "source_name": "AZ Registrar of Contractors public posting list",
        "blurb": "Heuristic match against the full contractor roster — not a dedicated solar license.",
    },
    "tx": {
        "name": "Texas", "loader": load_tx, "complete": True,
        "framing": (
            "Texas created a dedicated Solar Residential Retailer registration in 2026 (SB 1036). This "
            "is the complete list of registered retailers. Important: this registration covers the "
            "sales/lease transaction only — it is not an installation-competency license. The actual "
            "installation work is separately governed by licensed Electrical Contractors, who are "
            "exempt from registering here at all; this site does not yet cross-reference that "
            "separate dataset."
        ),
        "source_name": "Texas TDLR Residential Solar Retailer program",
        "blurb": "Complete registry — sales registration only, not an installation license.",
    },
    "fl": {
        "name": "Florida", "loader": load_fl, "complete": True,
        "framing": (
            "Florida has a dedicated Certified Solar Contractor (CVC) classification, shown here "
            "alongside Electrical Contractors whose name indicates solar work (Florida law allows EC "
            "licensees to perform solar installation without holding a CV credential). Rows marked "
            "\"EC, name-matched\" have not been checked against disciplinary records — only CVC-classified "
            "rows have a confirmed clean-or-flagged history."
        ),
        "source_name": "Florida DBPR / Construction Industry Licensing Board",
        "blurb": "Dedicated solar classification (CVC), supplemented by an electrical-contractor name match.",
    },
    "ca": {
        "name": "California", "loader": load_ca, "complete": True,
        "framing": (
            "California's C-46 Solar Contractor classification is dedicated and well established, "
            "shown here with real license status (Active/Suspended/flagged) from CSLB's own records — "
            "including a small number of businesses added after being found licensed under a related "
            "classification (C-10 Electrical or B General Building) instead of C-46 itself, the same "
            "gap found and fixed for Arizona and Florida. Complaint- and case-level disciplinary detail "
            "has not yet been loaded for California; the status shown is CSLB's own license standing, "
            "not a complaint history."
        ),
        "source_name": "California CSLB statewide License Master file",
        "blurb": "Dedicated C-46 solar classification, with real license status and a few name-matched additions.",
    },
    "mi": {
        "name": "Michigan", "loader": load_mi, "complete": False,
        "framing": (
            "Michigan has no solar-specific license at all — solar work legally requires licenses "
            "under up to three unrelated categories (Residential Builder, a Roofing-classified "
            "Maintenance & Alteration license, and Electrical Contractor), none of which mention solar "
            "anywhere. Every business below has been individually matched by name and its real license "
            "status confirmed directly. This is a small, hand-verified starting list, not a complete "
            "directory of Michigan solar installers."
        ),
        "source_name": "Michigan LARA / Bureau of Construction Codes",
        "blurb": "Individually hand-verified — smallest and most manually-checked list of the five.",
    },
}


BASE_URL = "https://willcharris.github.io/solar-installer-directory/"


def write_sitemap(out_dir: Path, urls: list, today: str):
    """Every HTML page AND every individual PDF report -- the PDFs are
    the actual long-tail content this whole SEO strategy depends on, and
    each one is independently indexable (its own URL, the business name
    already in the document title). Leaving them out of the sitemap
    would mean Google has to discover 2,500+ pages by chance instead of
    being told about them directly."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url in urls:
        lines.append(f"  <url><loc>{BASE_URL}{url}</loc><lastmod>{today}</lastmod></url>")
    lines.append("</urlset>")
    (out_dir / "sitemap.xml").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote sitemap.xml with {len(urls)} URLs")

    robots_path = out_dir / "robots.txt"
    if not robots_path.exists():
        robots_path.write_text(f"User-agent: *\nAllow: /\nSitemap: {BASE_URL}sitemap.xml\n", encoding="utf-8")
        print("Wrote robots.txt")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="JSON file mapping state slug -> data source path(s)")
    ap.add_argument("--out", default="docs")
    args = ap.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True)

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    today = date.today().isoformat()

    state_summaries = []
    sitemap_urls = ["index.html"]
    for slug, meta in STATE_LOADERS.items():
        if slug not in config:
            print(f"  (skipping {slug} -- not in config)")
            continue
        path = config[slug]
        rows = meta["loader"](path)
        print(f"  {slug}: {len(rows)} rows")

        state_summaries.append(
            {"slug": slug, "name": meta["name"], "count": len(rows),
             "complete": meta["complete"], "blurb": meta["blurb"]}
        )
        sitemap_urls.append(f"{slug}.html")
        sitemap_urls.extend(row["report_url"] for row in rows if row.get("report_url"))

        template = env.get_template("state.html")
        html = template.render(
            root="", state_name=meta["name"], state_slug=slug, framing=meta["framing"],
            source_name=meta["source_name"], retrieved_date=today, rows=rows,
        )
        (out_dir / f"{slug}.html").write_text(html, encoding="utf-8")

    index_template = env.get_template("index.html")
    index_html = index_template.render(root="", states=state_summaries)
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")

    print(f"\nWrote {len(state_summaries) + 1} pages to {out_dir}/")
    write_sitemap(out_dir, sitemap_urls, today)


if __name__ == "__main__":
    main()
