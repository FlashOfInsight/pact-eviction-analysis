# Methodology: PACT vs. Non-PACT NYCHA Eviction Execution Rates

**Produced:** 2026-05-03  
**Analyst:** laeoc  
**Output file:** `aggregate_execution_rates.csv`

---

## Overview

This document describes every step taken to compute annual marshal eviction execution
rates (per 1,000 residential units) for:

- **PACT developments** — NYCHA buildings converted to private management under the
  Permanent Affordability Commitment Together (RAD/PACT) program, filtered to
  "Construction Complete" and "Under Construction" status only
- **Non-PACT NYCHA** — all remaining publicly-managed NYCHA developments

Each year's rate uses a changing denominator that reflects how many units were in each
pool *at the end of that year*, as developments convert from NYCHA to PACT over time.

---

## Data Sources

### S1 — NYCHA PACT Dataset PDF
- **URL:** `https://www.nyc.gov/assets/nycha/downloads/pdf/PACT_Dataset.pdf`
- **Publisher:** NYC Housing Authority
- **Updated:** April 2026 (downloaded 2026-04-30)
- **Contents:** One row per development, with columns: development name, project name,
  conversion date (anticipated), total units, repair cost, status, developers,
  general contractor, property manager, social service provider
- **Used for:** List of active PACT developments, unit counts, property manager names,
  status filter

### S2 — NYCHA Residential Address Dataset
- **URL:** `https://data.cityofnewyork.us/resource/3ub5-4ph8.json`
- **Publisher:** NYC Housing Authority via NYC Open Data (Socrata)
- **Contents:** Building-level records for current NYCHA-managed buildings.
  Key fields: `development`, `address`, `zip_code`, `borough_block_lot` (BBL),
  `borough`, `block`, `lot`, `privately_managed`
- **Used for:** Initial attempt at PACT BBL lookup (abandoned — see §4.1);
  control group BBL lookup (superseded — see §4.3); address-normalization reference

### S3 — NYCHA Development Data Book
- **URL:** `https://data.cityofnewyork.us/resource/evjd-dqpz.json`
- **Publisher:** NYC Housing Authority via NYC Open Data (Socrata)
- **Contents:** One row per development. Key fields: `development`, `borough`,
  `location_street_a/b/c/d`, `total_number_of_apartments`,
  `number_of_current_apartments`, `rad_transferred_date`, `private_management`
- **Used for:** RAD/PACT conversion dates (the primary source for annual
  denominator calculation); unit counts for non-PACT developments;
  cross-street addresses used in geosearch geocoding

### S4 — NYC MapPLUTO
- **URL:** `https://data.cityofnewyork.us/resource/64uk-42ks.json`
- **Publisher:** NYC Department of City Planning via NYC Open Data (Socrata)
- **Contents:** Tax lot–level property data. Key fields: `bbl`, `ownername`,
  `address`, `borough`, `unitsres`, `latitude`, `longitude`
- **Used for:** (a) Resolving BBLs for PACT developments via owner-name pattern
  matching; (b) Resolving the complete non-PACT NYCHA BBL set via the
  `NYC HOUSING AUTHORITY` owner name

### S5 — NYC Planning Geosearch API
- **URL:** `https://geosearch.planninglabs.nyc/v2/search`
- **Publisher:** NYC Department of City Planning (free, no key required)
- **Used for:** Geocoding cross-street intersections from S3 to lat/lon coordinates
  for spatial PLUTO bounding-box queries, as a fallback when owner-name search
  in PLUTO failed

### S6 — NYC Marshal Evictions
- **URL:** `https://data.cityofnewyork.us/resource/6z8x-wfk4.json`
- **Publisher:** NYC Department of Investigation via NYC Open Data (Socrata)
- **Updated:** Daily (last record in dataset: 2026-04-24)
- **Contents:** One row per executed eviction. Key fields: `bbl`, `docket_number`,
  `court_index_number`, `eviction_address`, `eviction_zip`, `executed_date`,
  `residential_commercial_ind`
- **Used for:** Primary numerator — count of executed evictions at PACT and
  non-PACT NYCHA BBLs, 2022–present

### S7 — OCA Housing Court Data (HDC S3 CSVs)
- **URL base:** `https://oca-2-dev.s3.amazonaws.com/public/`
- **Publisher:** NYS Office of Court Administration, processed and published by the
  OCA Data Collective (Housing Data Coalition, Right to Counsel Coalition, JustFix,
  ANHD, BetaNYC, UNHP)
- **Last updated:** 2026-04-26
- **Tables used:**
  - `oca_warrants.csv` — warrant issuance records including
    `enforcementofficerdocketnumber` (the join key to marshal data),
    `executiondate`, `executiontype`
  - `oca_index.csv` — case-level records: `indexnumberid`, `court`,
    `fileddate`, `classification` (Non-Payment / Holdover / etc.),
    `disposedreason`, `specialtydesignationtypes`, `firstpaper`, `status`
  - `oca_causes.csv` — cause of action per case: `indexnumberid`,
    `causeofactiontype`
- **Used for:** Enriching marshal records with case classification (Non-Payment
  vs. Holdover), disposition method, and court metadata
- **Key limitation:** The `indexnumberid` in OCA is an opaque XML ID assigned by
  the court system — it is **not** a hash of the human-readable court index number
  (format `NNNNNN/YY`) that appears in the marshal dataset. The join key is
  `marshal.docket_number` ↔ `oca_warrants.enforcementofficerdocketnumber`.

---

## Step 1 — Build the PACT Development Reference List

**Script:** `analyze.py` → `parse_pact_pdf()`, `clean_pact_df()`  
**Output:** `pact_reference.csv` (28 rows)

1. Download `PACT_Dataset.pdf` from S1.
2. Extract tables from all PDF pages using `pdfplumber.extract_tables()`.
3. Detect the header row by checking for keywords "development", "status", "units".
4. Normalize column names to snake_case. Key columns identified:
   - `development_name`, `status`, `total_units`, `property_manager`,
     `conversion_date`
5. Forward-fill `status`, `property_manager`, `conversion_date` across
   sub-development rows (the PDF groups related rows under a single parent row
   that carries those values).
6. Normalize status strings:
   - Contains "complete" → `"Construction Complete"`
   - Contains "under construction" → `"Under Construction"`
   - Contains "planning" or "engagement" → `"Planning and Engagement"`
7. **Filter:** Keep only `"Construction Complete"` and `"Under Construction"`.
   Exclude `"Planning and Engagement"` sites (not yet under private management).
8. Strip non-digit characters from `total_units` and convert to numeric.

**Result:** 28 active PACT developments, 146 excluded (Planning and Engagement).

**Audit check:** Open `pact_reference.csv`. Verify development names, unit counts,
and status values against the source PDF at the S1 URL.

---

## Step 2 — Attach Conversion Dates

**Script:** `analyze.py` → main flow  
**Source:** S3 (NYCHA Development Data Book, `rad_transferred_date` field)

1. Fetch all rows from S3 (`$limit=500`).
2. Normalize `development` field to uppercase and strip whitespace.
3. Join to `pact_reference.csv` on `development_name` (uppercase) =
   `development` (uppercase).
4. Use `rad_transferred_date` as the conversion date for denominator calculation.
5. Fall back to `total_number_of_apartments` from S3 for unit counts where the
   PDF value is missing.

**Matched:** All 28 PACT developments matched to S3 except 5 that lack a
`rad_transferred_date`: OCEAN HILL APARTMENTS, METRO NORTH PLAZA, MORRIS PARK
SENIOR CITIZENS HOME, BAY VIEW, CAMPOS PLAZA II. These 5 are **excluded from
both pools** in the annual rate calculation.

**Audit check:** In S3, query:  
`https://data.cityofnewyork.us/resource/evjd-dqpz.json?$where=rad_transferred_date IS NOT NULL&$select=development,rad_transferred_date,total_number_of_apartments`  
Confirm conversion dates for each PACT development name.

---

## Step 3 — Resolve BBLs for PACT Developments

**Script:** `analyze.py` → `resolve_pact_bbls()`

PACT developments have been removed from the NYCHA address dataset (S2) after
transfer to private management. BBLs must be resolved through PLUTO (S4).

### 3.1 — Automated PLUTO keyword search

For each PACT development:

1. Extract meaningful words from the development name, skipping generic terms
   (HOUSES, APARTMENTS, PARK, SECTION, etc.) and short words (< 3 characters).
2. For address-style names (e.g., "572 WARREN STREET"), skip the house number
   and use the street name words.
3. Query PLUTO: `upper(ownername) LIKE '%{keyword}%' AND borough='{boro}' AND unitsres >= 20`
4. Borough filter is derived from S3's `borough` field for that development.
5. Cap results at 40 BBLs when borough-filtered (15 without) to limit false positives.

### 3.2 — Hardcoded owner-name map (manual research)

For developments where automated keyword search failed, owner entity names were
identified through targeted PLUTO queries and added to a hardcoded map:

| PLUTO owner keyword | Development(s) |
|---|---|
| `OCEAN BAY RAD` | OCEAN BAY APARTMENTS (BAYSIDE) |
| `NYC PACT PRESERVATION PARTNERS` | 104-14 TAPSCOTT ST, OCEAN HILL APTS, BELMONT-SUTTER AREA |
| `PACT RENAISSANCE COLLABORATIVE` | MANHATTANVILLE, SAMUEL (CITY) |
| `CSA PRESERVATION PARTNERS` | EDENWALD |
| `SEWARD HOUSING` | SACK WERN |
| `MARKHAM GARDENS` | WEST BRIGHTON I |
| `BSC HOUSING COMPANY` | BAY VIEW |
| `METRO NORTH OWNERS` / `METRO NORTH GARDENS` | METRO NORTH PLAZA |
| `BOSTON TREMONT HOUSING` | BOSTON SECOR |
| `1018 E 163RD STREET HOUSING` | EAGLE AVENUE-EAST 163RD STREET |
| `437 MORRIS PARK` | MORRIS PARK SENIOR CITIZENS HOME |
| `1068 FRANKLIN AVE HOUSING` | FRANKLIN AVENUE I CONVENTIONAL |

All BBLs returned for each keyword (borough-filtered) are assigned to the
corresponding development(s).

### 3.3 — NYC Geosearch fallback

For developments where PLUTO keyword search returned nothing, the cross-street
intersection from S3 (`location_street_a` & `location_street_b`) was geocoded
using the S5 Geosearch API. The returned lat/lon was used to query PLUTO for
all residential lots within a ~120–400m bounding box.

### 3.4 — BBL coverage outcome

| Coverage status | Developments | Approximate units |
|---|---|---|
| BBLs resolved | 26 of 28 | ~15,700 |
| No BBLs found | 2 (335 E 111TH ST, FRANKLIN AVE I*) | ~127 |

*335 E 111TH STREET and FRANKLIN AVENUE I CONVENTIONAL had no recoverable PLUTO
match. Their executions cannot be counted; their units remain in the PACT
denominator.

**Note on shared-entity BBLs:** Several PACT developments share a single PLUTO
owner entity (e.g., NYC PACT PRESERVATION PARTNERS LLC owns lots attributed to
three separate developments). When a BBL appears in multiple developments' lists,
the development attribution in `pact_executions.csv` goes to whichever development
appears first after deduplication. This affects development-level attribution but
**not** the aggregate PACT totals, since all those BBLs are correctly included
in the PACT set.

**Audit check:** For each development, query PLUTO directly:  
`https://data.cityofnewyork.us/resource/64uk-42ks.json?$where=upper(ownername) LIKE '%{KEYWORD}%'&$select=bbl,ownername,address,unitsres`  
Confirm that the returned lots are plausibly associated with the named development
(address, borough, unit count).

---

## Step 3.5 — Manual BBL Verification

**Authoritative file:** `pact_bbl_master.csv`  
**Updated:** manually, development by development  
**Pipeline integration:** `analyze.py` → `load_bbl_master()` — any development with non-empty BBLs in this file overrides automated resolution; developments with no BBLs fall through to auto.

### Purpose

Automated PLUTO resolution (Steps 3.1–3.3) produces incorrect or incomplete results
for many PACT developments. Errors fall into two categories:

- **Overcounts:** the automated search pulls in lots that belong to a neighboring
  development, inflating the eviction numerator for one site and potentially
  deflating another.
- **Undercounts / missing:** only one lot of a multi-lot development is found, or
  no lots are found at all, causing evictions to be silently undercounted.

Manual verification cross-references PLUTO lot data against NYCHA building map PDFs
and known physical addresses to establish the correct, complete set of BBLs for each
development.

### Priority order (least to most confident)

Verification proceeds in this order within each tier:

**Tier 1 — Overcounts (actively wrong; most urgent)**

| # | Development | Issue |
|---|---|---|
| 1 | BAY VIEW | +8,027u over expected; BSC Housing lots include non-PACT buildings |
| 2 | EASTCHESTER GARDENS | +539u over; Eastchester Heights HDF covers both Gardens and Heights |

**Tier 2 — No BBLs found (development absent from analysis)**

| # | Development | Expected units |
|---|---|---|
| 3 | EAGLE AVENUE-EAST 163RD STREET | 66u |
| 4 | 104-14 TAPSCOTT STREET | 30u |
| 5 | MANHATTANVILLE | 1,272u |
| 6 | OCEAN HILL APARTMENTS | 236u |
| 7 | MORRIS PARK SENIOR CITIZENS HOME | 97u |
| 8 | CAMPOS PLAZA II | 224u |

**Tier 3 — Large unit gap (significantly incomplete)**

| # | Development | Gap |
|---|---|---|
| 9 | EDENWALD | -1,016u (only 1 BBL found) |
| 10 | BAYCHESTER | -369u (only 1 BBL found) |
| 11 | WEST BRIGHTON I | -250u (only 1 BBL found) |
| 12 | BOSTON SECOR | -241u (only 1 BBL found) |
| 13 | SACK WERN | -234u (only 1 BBL found) |
| 14 | METRO NORTH PLAZA | -227u (only 1 BBL found) |
| 15 | 1010 EAST 178TH STREET | -141u (only 1 BBL found) |
| 16 | 572 WARREN STREET | -132u (only 1 BBL found) |
| 17 | FRANKLIN AVENUE I CONVENTIONAL | -21u |

**Tier 4 — Auto-unreviewed (automated result, not yet confirmed)**

| # | Development | Unit gap | Current BBLs |
|---|---|---|---|
| 18 | TWIN PARKS WEST (SITES 1 & 2) | -38u | 2031000065, 2031010023 |
| 19 | HARLEM RIVER | -4u | 1020160060, 1020370011 |
| 20 | 335 EAST 111TH STREET | 0u | 1016830018 |
| 21 | OCEAN BAY APARTMENTS (BAYSIDE) | 0u | 4160010002, 4160020001 |

**Already confirmed (skip):** BETANCES I, BUSHWICK II (GROUPS A & C), BELMONT-SUTTER
AREA, LINDEN, WILLIAMSBURG, AUDUBON, SAMUEL (CITY)

### Verification procedure (per development)

1. Look up current PLUTO data for all BBLs in `pact_bbl_master.csv`:  
   `https://data.cityofnewyork.us/resource/64uk-42ks.json?$where=bbl='XXXXXXXXXX'&$select=bbl,ownername,address,unitsres`
2. Cross-reference with NYCHA building map PDFs (nyc.gov/nycha → developments) or
   the NYCHA Development Portal.
3. For missing lots: search PLUTO by address or owner name to find additional BBLs.
4. Accept/reject/replace BBLs in `pact_bbl_master.csv`:
   - Update `bbls` (pipe-separated)
   - Update `bbl_count`
   - Update `pluto_units` and `unit_gap`
   - Set `review_status` to `confirmed`
   - Add a `notes` entry describing what was added, removed, or accepted

### How `pact_bbl_master.csv` drives the pipeline

When `analyze.py` runs, `load_bbl_master()` reads this file and overrides
the automated PLUTO/Geosearch resolution for any development that has BBLs recorded.
To re-run the pipeline after updating verified BBLs, simply run `python analyze.py`.

---

## Step 4 — Resolve BBLs for Non-PACT NYCHA (Control Group)

**Script:** standalone query at pipeline runtime  
**Output:** `control_bbls_pluto.csv` (651 rows)

1. Query PLUTO for all lots owned by NYC Housing Authority:  
   `upper(ownername) LIKE '%HOUSING AUTHORITY%' AND unitsres > 0`
2. This returns 651 lots totalling 176,766 residential units.
3. Verify zero overlap with PACT BBLs (confirmed: 0 overlap). Because PACT
   transfers change the PLUTO owner from NYC Housing Authority to the new private
   entity, the two sets are mutually exclusive by construction.

**Rationale for using PLUTO over the NYCHA address dataset (S2):**  
S2 only covered 218 of 245 non-PACT developments (~89%). PLUTO's owner-name
query is complete by definition — any lot still owned by NYC Housing Authority
is non-PACT NYCHA, and any transferred lot will no longer carry that owner name.

**Audit check:** Query PLUTO directly and confirm the count:  
`https://data.cityofnewyork.us/resource/64uk-42ks.json?$select=count(*)&$where=upper(ownername) LIKE '%HOUSING AUTHORITY%' AND unitsres > 0`  
Should return ~651 rows and ~176,766 total units.

---

## Step 5 — Fetch Marshal Eviction Data

**Script:** `analyze.py` → `fetch_marshal_evictions()`  
**Source:** S6

1. Query the Socrata API with filter:  
   `executed_date >= '2022-01-01' AND (residential_commercial_ind = 'Residential' OR residential_commercial_ind = 'R')`
2. Paginate in batches of 50,000 rows (`$limit/$offset`).
3. Normalize `bbl` field: strip non-digits, keep only if exactly 10 characters.
4. Parse `executed_date` to datetime; extract `year`.
5. Normalize `eviction_address` with `normalize_addr()` (lowercase, expand
   abbreviations, collapse whitespace) for potential fallback matching.

**Result:** 54,697 residential eviction records, 2022–2026-04-24.

**Audit check:** Count residential evictions in S6 directly:  
`https://data.cityofnewyork.us/resource/6z8x-wfk4.json?$select=count(*)&$where=executed_date >= '2022-01-01' AND (residential_commercial_ind = 'Residential' OR residential_commercial_ind = 'R')`

---

## Step 6 — Split Marshal Evictions into PACT and Control

**Script:** `analyze.py` → main flow

1. Build `pact_bbls`: the set of all BBLs resolved in Step 3.
2. Build `control_bbls`: all 651 BBLs from `control_bbls_pluto.csv`.
3. Split the full marshal dataset:
   - `pact_exec`: rows where `bbl ∈ pact_bbls` → **659 rows**
   - `control_exec`: rows where `bbl ∈ control_bbls` → **729 rows**
4. Annotate `pact_exec` rows with `development_name`, `status_normalized`,
   `property_manager` via a `{bbl → development}` lookup built from the
   resolved PACT BBL table.

**Audit check:** For any row in `pact_executions.csv`, take its `bbl` value and
confirm that BBL appears in the PACT owner-name search output for the named
development. For any row in `control_executions.csv`, confirm the BBL returns
"NYC HOUSING AUTHORITY" as owner in PLUTO.

---

## Step 7 — Enrich with OCA Case Classification

**Script:** `analyze.py` (post-processing) and interactive session  
**Sources:** S7 (oca_warrants, oca_index, oca_causes)  
**Output:** `oca_docket_lookup.csv`; enriched `pact_executions.csv` and
`control_executions.csv`

### Join chain

```
marshal.docket_number
  → oca_warrants.enforcementofficerdocketnumber   [latest executiondate per docket]
  → oca_warrants.indexnumberid
  → oca_index.classification                      (Non-Payment / Holdover / etc.)
  → oca_index.disposedreason                      (judgment type and stay status)
  → oca_index.specialtydesignationtypes           (NYCHA flag, Nuisance flag, etc.)
  → oca_causes.causeofactiontype                  (same-level as classification
                                                   for most cases; concatenated
                                                   with " | " if multiple rows)
```

### Steps

1. Normalize `docket_number` in marshal data: strip to integer string
   (e.g., `"027767"` → `"27767"`).
2. Stream `oca_warrants.csv` in 100k-row chunks. For each chunk:
   - Normalize `enforcementofficerdocketnumber` to integer string.
   - Filter to rows whose docket string is in the marshal docket set.
3. From matched warrant rows, keep **one row per docket** by selecting the row
   with the **latest `executiondate`** (most recent warrant action).
4. Collect the `indexnumberid` values from Step 3.
5. Stream `oca_index.csv`, filter to matched `indexnumberid` values, keep
   one row per case (deduplicated).
6. Stream `oca_causes.csv`, filter to matched IDs. Aggregate per case:
   concatenate all `causeofactiontype` values with `" | "`.
7. Apply `bucket_disposition()` to `disposedreason` to create
   `disposition_bucket`:

   | Raw disposedreason contains | Bucket |
   |---|---|
   | "No Execution Stay" or "Execution Stayed 0 days" | Judgment - Immediate |
   | "Stipulation" or "Stip" | Judgment - Stipulation |
   | "Failure to Appear" or "Failure to Answer" | Judgment - Failure to Appear |
   | "Inquest" | Judgment - Inquest |
   | "Trial" or "Hearing" or "Decision" | Judgment - Contested |
   | "Dismissed" | Dismissed |
   | "Settled" | Settled |
   | "Execution Stayed" | Judgment - Stayed |
   | None of the above | Other |

8. Flag `flagged_nycha_in_court = True` if `specialtydesignationtypes`
   contains "NYCHA".

**Match rate:** 1,370 of 1,372 unique marshal dockets matched to OCA warrants
(99.9%). Of those, 1,304 matched to `oca_index` (the remaining 66 have warrant
records but no corresponding index entry, likely very old cases pre-dating the
OCA digital records).

**Audit check:** Pick any row in `pact_executions.csv` where `classification`
is populated. Take `docket_number` and search `oca_warrants.csv` for that value
in `enforcementofficerdocketnumber`. Confirm the `indexnumberid` matches. Then
search `oca_index.csv` for that `indexnumberid` and confirm `classification`
matches.

---

## Step 8 — Compute Annual Unit Denominators

**Script:** `aggregate_execution_rates.csv` generation  
**Sources:** S1 (PACT unit counts), S3 (conversion dates + unit counts), S4 (PLUTO unit counts)

### PACT denominator (per year)

For each year Y:

```
PACT_units(Y) = sum of units_final for all developments where
                rad_transferred_date ≤ Dec 31 of year Y
```

`units_final` = `total_units` from S1 (PACT PDF), falling back to
`total_number_of_apartments` from S3 when the PDF value is missing.

Developments without a `rad_transferred_date` in S3 (5 developments; see Step 2)
are **excluded** — not counted in PACT units for any year.

### Non-PACT NYCHA denominator (per year)

For each year Y:

```
non_PACT_units(Y) = 176,766   [current PLUTO NYCHA-owned total]
                  + sum of units_final for PACT developments where
                    rad_transferred_date > Dec 31 of year Y
```

The logic: PLUTO reflects the current state (post-all-transfers). For past years,
those buildings had not yet been transferred, so their units belonged to the
non-PACT pool. Adding them back reconstructs the historical non-PACT universe.

**Audit check:** For year 2023, manually sum:
- 176,766 (current PLUTO non-PACT total)
- Plus units for developments with `rad_transferred_date` in 2024 and 2025
- Should equal the `Non-PACT units` column value for 2023 in
  `aggregate_execution_rates.csv`

---

## Step 9 — Compute Annual Rates

**Script:** `aggregate_execution_rates.csv` generation

For each year Y, for each group G ∈ {PACT, Non-PACT NYCHA}:

```
executions(G, Y)  = count of rows in {pact,control}_executions.csv
                    where year == Y

units(G, Y)       = denominator from Step 8

rate(G, Y)        = executions(G, Y) / units(G, Y) × 1,000
```

**2026 is flagged as YTD.** The denominator is the same as 2025 (no new
conversions have been recorded in S3 after 2025-06-24 as of the data pull date).
The OCA data has an approximately 4–6 week reporting lag; marshal data is
current to 2026-04-24.

---

## Final Output Table

`aggregate_execution_rates.csv`

| Year | PACT devs | PACT units | PACT exec | PACT /1k | Non-PACT units | Non-PACT exec | Non-PACT /1k | Ratio |
|---|---|---|---|---|---|---|---|---|
| 2022 | 12 | 6,940 | 28 | 4.03 | 183,538 | 8 | 0.04 | 100.8× |
| 2023 | 16 | 9,238 | 107 | 11.58 | 181,240 | 82 | 0.45 | 25.7× |
| 2024 | 21 | 12,615 | 196 | 15.54 | 177,863 | 273 | 1.53 | 10.2× |
| 2025 | 23 | 13,712 | 230 | 16.77 | 176,766 | 333 | 1.88 | 8.9× |
| 2026 YTD | 23 | 13,712 | 98 | 7.15 | 176,766 | 121 | 0.68 | 10.5× |

---

## Known Limitations and Caveats

### L1 — Incomplete PACT BBL coverage
2 of 28 active PACT developments (335 EAST 111TH STREET, 61 units; and
FRANKLIN AVENUE I CONVENTIONAL, 61 units; ~127 units combined) have no
resolved BBLs. Executions at those addresses are not captured in
`pact_executions.csv`. Their units are included in the PACT denominator,
which understates the true PACT rate by a small margin.

### L2 — Shared-entity BBLs
Three Brooklyn developments (104-14 TAPSCOTT STREET, OCEAN HILL APARTMENTS,
BELMONT-SUTTER AREA) share the PLUTO entity "NYC PACT PRESERVATION PARTNERS LLC."
Two Manhattan developments (MANHATTANVILLE, SAMUEL (CITY)) share "PACT RENAISSANCE
COLLABORATIVE LLC." Within `pact_executions.csv`, each BBL is attributed to one
development only (first-match deduplication). The aggregate PACT counts and rates
are unaffected.

### L3 — PLUTO snapshot is current, not historical
PLUTO reflects ownership as of the data pull (2026-04-30). A development that
converted in, say, late 2025 may appear as private-entity-owned in PLUTO now,
meaning its buildings are correctly excluded from the non-PACT NYCHA BBL set.
However, if any PACT transfer had not yet updated in PLUTO at the time of the
pull, those lots would appear in both the PACT denominator (via S3 conversion
date) and the non-PACT BBL numerator (via PLUTO owner search) — a minor
double-counting risk. Zero overlap was confirmed at pull time.

### L4 — 2022 ratio is not reliable
Only 12 PACT developments were active in 2022, most converted in 2016–2019.
Post-moratorium dynamics (courts reopening after COVID closures) produced a
spike in eviction activity concentrated in those early-converted sites. The
134× ratio for 2022 should not be interpreted as a stable signal.

### L5 — OCA filing data not available at development level
The HDC/OCA pre-processed CSVs carry respondent mailing ZIP codes but no
street-level property address. Development-level filing rate analysis (as
distinct from execution rate) is therefore not feasible with this data pipeline.
The file `oca_borough_summary.csv` provides borough-level OCA filing counts
(Non-Payment, Holdover, other) as contextual information only.

### L6 — OCA holdover sub-type not available
`oca_causes.causeofactiontype` stores "Holdover" as a single undifferentiated
value for all holdover proceedings in our matched set. The specific grounds
(lease expiration, unauthorized occupant, licensee, nuisance, etc.) are encoded
in predicate notice documents attached to case files, which are not structured
fields in the OCA electronic system or the HDC-published CSVs.

### L7 — Non-PACT NYCHA denominator uses current PLUTO unit counts
The 176,766 figure comes from PLUTO's current snapshot of NYCHA-owned units.
Actual unit counts in past years may differ slightly due to demolitions,
new construction, or unreported changes not yet reflected in PLUTO. S3's
`total_number_of_apartments` could be used as an alternative denominator source
for non-PACT developments; the two sources agree within ~2%.
