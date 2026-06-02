# PACT Eviction Analysis

Data pipeline comparing PACT-converted NYCHA developments to non-PACT public housing
across evictions, rodent inspections, HPD 311 complaints, DOB permits, and NYPD incidents.

Outputs feed the interactive site at **laeo.net/data/nycha** via `../laeo-net/public/data/nycha/data/`.

The full methodology is documented in `../laeo-net/public/data/nycha/methodology.md`.

---

## Scripts

### `analyze.py`
Main eviction analysis. Downloads NYCHA PACT PDF (S1), resolves BBLs via PLUTO (S4),
fetches marshal evictions (S6) and OCA court data (S7), computes annual PACT vs.
non-PACT eviction rates.

**Run:** `python analyze.py`  
**Outputs:** `pact_executions.csv`, `control_executions.csv`, `aggregate_execution_rates.csv`,
`analysis.md`, GeoJSON enrichment in `../laeo-net/public/data/nycha/data/developments.geojson`

### `pact_nypd.py`
NYPD complaint incident analysis. Uniform 150m-per-building circle methodology for both
PACT and non-PACT groups. Fetches 5 years pre-conversion and all post-conversion data
for PACT; all available years for non-PACT via NYCHA Residential Addresses (S2).

**Run:** `python pact_nypd.py`  
**Flags:** `--refresh` (re-fetch everything), `--refresh-ctrl` (re-fetch control only),
`--refresh-pact` (re-fetch PACT only)  
**Outputs:**
- `nypd_control.csv` — non-PACT incidents (~377k rows; cached, excluded from git)
- `nypd_pact.csv` — PACT post-conversion incidents
- `nypd_pact_pre.csv` — PACT pre-conversion incidents (5yr window)
- `../laeo-net/.../data/nypd_aggregate.json` — annual rates by type
- `../laeo-net/.../data/nypd_by_conversion.json` — pre/post conversion buckets

**Caution:** Full re-fetch of the control pool takes ~20 min (380k+ API records). The
CSVs are cached; subsequent runs re-use them unless `--refresh` is passed.

### `pact_311.py`
HPD 311 complaint analysis for PACT developments only. Non-PACT NYCHA is excluded
because residents use MyNYCHA, not 311 (see Limitation L9 in methodology.md).

**Run:** `python pact_311.py`  
**Dependencies:** `requests`, `pandas`  
**Outputs:** `pact_311.csv`, `../laeo-net/.../data/complaints_agg.json`, GeoJSON enrichment

### `pact_permits.py`
DOB job application filings at PACT development BBLs, post-conversion.

**Run:** `python pact_permits.py`  
**Outputs:** `pact_permits.csv`, `../laeo-net/.../data/permits_agg.json`,
`../laeo-net/.../data/permits_by_year.json`, GeoJSON enrichment

### `build_site_data.py`
Rebuilds aggregate JSON files from cached CSVs without re-fetching API data. Run after
editing reference files or correcting BBLs if API results are already cached.

**Run:** `python build_site_data.py`

---

## Reference Files

| File | Description |
|------|-------------|
| `pact_bbl_master.csv` | Authoritative BBL list per PACT development. Pipe-separated BBLs. Overrides automated PLUTO resolution in `analyze.py`. |
| `pact_reference.csv` | PACT development list from PDF (S1): name, status, units, property manager. |
| `pact_control_exclusions.csv` | Manual overrides for developments missing from S3 (NYCHA Development Data Book). |

---

## Annual Update Checklist

Each year, re-run the pipeline to refresh data:

1. **Update PACT PDF** — download latest from S1 URL; re-run `analyze.py` to update `pact_reference.csv`
2. **Re-run eviction analysis** — `python analyze.py` (fetches fresh marshal + OCA data)
3. **Re-run rodent analysis** — `python pact_rodent.py` if it exists; otherwise update manually
4. **Re-run 311** — `python pact_311.py`
5. **Re-run permits** — `python pact_permits.py`
6. **Re-run NYPD** — `python pact_nypd.py` (cached CSVs are auto-invalidated by `--refresh`)
   - The year boundary in `DATASETS` is dynamically computed; no code changes needed
7. **Verify GeoJSON** — check `developments.geojson` has current `nypd_data`, `permit_data`, `complaint_data`, `rodent_data`
8. **Deploy** — `cd ../laeo-net && git add -A && git commit && git push`

---

## Data Flow

```
S1 PDF        → analyze.py → pact_reference.csv
S3 NYCHA API  → analyze.py → conversion dates
S4 PLUTO      → analyze.py → pact_bbl_master.csv (auto) / pact_bbl_master.csv (manual overrides)
S6 Marshal    → analyze.py → pact_executions.csv / control_executions.csv
S7 OCA        → analyze.py → oca_docket_lookup.csv

S2 NYCHA Addr → pact_nypd.py → nypd_control.csv  ┐
S4 PLUTO      → pact_nypd.py → PACT circles       ├→ nypd_aggregate.json
S12 NYPD API  → pact_nypd.py → nypd_pact.csv      ┘    nypd_by_conversion.json

S9 311 API    → pact_311.py  → pact_311.csv → complaints_agg.json
S10 DOB API   → pact_permits.py → pact_permits.csv → permits_agg.json

All above → developments.geojson (enriched in-place)
```
