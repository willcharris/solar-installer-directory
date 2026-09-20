# Solar Installer Directory

**Live site:** https://willcharris.github.io/solar-installer-directory/

A consumer-facing tool that cross-references public state licensing records to help
someone check a solar installer before signing a contract — built because most
states have no clean "solar installer" data source, and the ones that look clean
are usually hiding something.

This is a verification tool, not a sales site. It doesn't sell leads, doesn't rank
installers, and doesn't accept payment from anyone listed. Every claim it makes is
sourced directly to a public government record, linked from the report itself.

---

## The classification trap

This project exists because of one recurring discovery: **most states have no
dedicated "solar installer" license at all**, and the ones that do usually don't
cover everyone who actually does solar work. A directory built by trusting a
state's own classification labels will silently miss real installers — often a
large fraction of them.

Every state in this project turned out to have its own version of this problem:

| State | The trap |
|---|---|
| **Arizona** | No solar classification. A name/text heuristic ("does 'solar' appear in the business name or classification?") catches the obvious cases but misses installers with generic names — fuzzy-matching real business names against the *full* contractor roster surfaced dozens more, including one whose license had been **revoked** and had silently dropped off the state's active roster entirely. |
| **Michigan** | No solar classification anywhere in ~50 license types. Solar work legally requires licenses under up to **three unrelated categories** at once (Residential Builder, a roofing-classified Maintenance & Alteration license, and Electrical Contractor) — none of which mention solar. |
| **Florida** | Has a real dedicated classification (CVC, "Certified Solar Contractor") — but state law also lets any Electrical Contractor install solar without holding it. A name-hint filter catches some of these; fuzzy-matching against the full ~19,700-record EC roster caught more. |
| **California** | Has a real dedicated classification (C-46) — but a licensed C-10 (Electrical) or B (General Building) contractor can also legally do solar work without it. One confirmed example: a company with "Solar" literally in its own name holds **no C-46 classification at all**, only C-10 and B. |
| **Texas** | The cleanest case — a real, dedicated "Solar Residential Retailer" registration exists. But it only covers the *sales/lease transaction*, not installation competency. The people who actually install the panels are licensed Electrical Contractors under a completely separate law, and are explicitly *exempt* from this registration. |

The throughline: **never trust a single classification label as the full picture.**
Every state's pipeline either fuzzy-matches real business names against the full
underlying roster (not just a pre-filtered "solar" subset), or documents plainly
why it can't yet, rather than quietly showing an incomplete list as if it were
complete.

---

## What each state's data actually is

Not all five listings carry the same weight — the site says so explicitly on each
state's page, but it's worth stating plainly here too:

| State | Installers shown | Basis |
|---|---|---|
| Texas | 56 | Complete official registry |
| Florida | 618 | Dedicated classification + reviewed name-matches |
| California | 1,200 | Complete dedicated classification + reviewed name-matches, real license status |
| Arizona | 674 | Heuristic-caught roster subset + reviewed name-matches |
| Michigan | 19 | Fully hand-verified, **not** a complete directory — the smallest, most conservative list of the five |

None of the five have disciplinary/complaint/bankruptcy data loaded yet beyond
what each state's own licensing status already reveals (revocations, suspensions).
Every report page says so honestly rather than implying a clean record it hasn't
actually checked.

---

## Repo structure

Each state's pipeline lives in its own folder, named as it was first set up —
this isn't fully standardized across states (a known quirk, not a design choice):

```
pipelines/az/       Arizona — ingest.py, match_candidates.py, generate_report.py
pipelines/mi/       Michigan — ingest.py, match_candidates.py, enrich_details.py, generate_report.py
texas/              Texas — load_tdlr_solar.py, generate_report.py
fl_scoping/         Florida — ingest.py, match_candidates.py, generate_report.py
scraper/            California — ingest_master.py, match_statewide.py, apply_approved_ca_matches.py, generate_report.py
site/               Site builder — build_site.py, templates/ (Jinja2)
docs/               The built, live site (served by GitHub Pages from this folder)
docs/reports/       All 2,567 generated per-installer PDF reports, one subfolder per state
```

Raw downloaded data (CSVs, `.xlsx` exports, the CA statewide master file) and
built `.db` files are gitignored — they're regenerable from a fresh download of
each state's public data, not tracked in git.

## How it works

Every state follows roughly the same four-stage pipeline, adapted to what that
state's data actually supports:

1. **Ingest** — download the state's own public license data and load it into a
   local SQLite database.
2. **Match** *(where the state has no dedicated classification, or the
   dedicated one doesn't cover everyone)* — fuzzy-match a list of real installer
   names (sourced from Google Places / EnergySage) against the full underlying
   license roster, not just a pre-filtered subset. Every high-confidence match is
   manually reviewed before being added — a bad auto-match is worse than a missed
   one for a directory making claims about real businesses.
3. **Enrich** *(Michigan only, so far)* — some states' bulk data exports are
   missing fields (license status, trade classifications) that only exist on a
   per-record detail page; a separate step fetches those directly.
4. **Report + site build** — `generate_report.py` renders one PDF per installer
   (Jinja2 + WeasyPrint), and `site/build_site.py` builds the static HTML pages
   that link to them.

## Running it yourself

Each state's scripts are run independently. General shape (Arizona shown, others
follow the same pattern — see that state's script `--help` for exact flags):

```bash
# 1. Ingest the state's raw data into a local db
python pipelines/az/ingest.py --db az_roc.db --roster <path-to-downloaded-csv>

# 2. (where applicable) fuzzy-match real installer names against the full roster
python pipelines/az/match_candidates.py --candidates candidates_az.csv --db az_roc.db --out matches_az.csv
# review matches_az.csv by hand before trusting any of it

# 3. Generate all report PDFs
python pipelines/az/generate_report.py --db az_roc.db --bulk-out docs/reports/az

# 4. Rebuild the site
python site/build_site.py --config site/sources.json --out docs
```

Dependencies: `pandas`, `jinja2`, `weasyprint`, `requests`, `beautifulsoup4`,
`rapidfuzz`, `openpyxl` (not all states need all of these — install as prompted).

## Known gaps

Stated plainly, not buried:

- **Arizona & Florida**: only high-confidence fuzzy-matches were reviewed and
  added; each state's medium-confidence tier remains unreviewed (spot-checks
  showed a meaningfully higher false-positive rate there — not worth trusting
  without individual review).
- **California**: same as above — 52 medium-confidence matches from the
  statewide pass remain unreviewed.
- **Michigan**: only "Company" license variants were pulled, not "Individual";
  17 of the original 36 sourced candidates were never conclusively matched.
- **No disciplinary/complaint/bankruptcy source** has been built for Michigan,
  Texas, or California beyond what each state's own license status already
  reveals. Arizona has real disciplinary-ledger data; Florida has real
  complaint counts for its dedicated classification only.
- **No automated refresh** exists yet — this is a one-time snapshot per state,
  not a live feed. Re-running each state's pipeline against a fresh download
  updates it manually.

## Disclaimer

This project is not affiliated with, endorsed by, or operated on behalf of any
state licensing agency. Every report reflects what that state's public data
showed as of the retrieval date printed on it, and is not a substitute for
confirming a contractor's current status directly with the relevant state board
before hiring.
