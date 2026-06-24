# PACT Eviction Analysis — Runbook

Operational guide for running and refreshing each pipeline script.

---

## Quick Reference

| Script | Cache flags | Full re-fetch time | SSL-sensitive phases |
|---|---|---|---|
| `analyze.py` | _(none; reruns fully each time)_ | ~2 min | BBL lookups (PLUTO), marshal data |
| `pact_nypd.py` | `--refresh`, `--refresh-ctrl`, `--refresh-pact` | ~20 min | Phase 0a (PLUTO coords), Phase 0b (NYCHA addrs), Phase A/B (NYPD API) |
| `pact_311.py` | `--refresh` | ~5 min | 311 Socrata endpoint |
| `pact_permits.py` | `--refresh` | ~3 min | DOB Socrata endpoint |
| `build_site_data.py` | _(no API calls; reads cached CSVs only)_ | <1 min | none |

After any script run, push updated site JSONs:

```bash
cd ../laeo-net && git add -A && git commit -m "Refresh NYCHA data" && git push
```

---

## SSL Certificate Issue (this machine)

**Symptom:** SSL certificate verification errors when any script makes outbound
Socrata or PLUTO API calls. Affects all four data-fetching scripts when they
reach a network call.

**Root cause:** A machine-level SSL trust issue with the NYC Open Data / Socrata
certificate chain. Not a code bug; other machines run fine.

**Which scripts are affected:**
- `pact_nypd.py` — Phases 0a, 0b, A, B all make API calls
- `analyze.py`, `pact_311.py`, `pact_permits.py` — all make API calls on every run

**Workaround options:**

1. **Run from a different machine** — all scripts work normally elsewhere. Cache
   flags apply: use `--refresh` only when a fresh pull is needed.

2. **Standalone rebuild from cache (pact_nypd.py only)** — regenerates the three
   site JSONs from the cached incident CSVs without any API calls. See below.

3. **`build_site_data.py`** — always safe to run on this machine; makes no API
   calls and only reads CSVs already on disk.

---

## pact_nypd.py — Standalone Rebuild from Cache

Use this when cached CSVs (`nypd_control.csv`, `nypd_pact.csv`,
`nypd_pact_pre.csv`) are up to date but the SSL issue prevents running the
script normally. Regenerates `nypd_aggregate.json`, `nypd_by_conversion.json`,
`nypd_cohort.json`, and the GeoJSON NYPD enrichment without touching any API.

Open a Python shell in the `pact-eviction-analysis/` directory:

```python
import sys, json
sys.path.insert(0, '.')
import pact_nypd as nypd

# Build minimal pact_geo from GeoJSON — no PLUTO API call needed.
# Only conv_date and units are required for the aggregate functions.
with open(nypd.GEOJSON_PATH) as f:
    gj = json.load(f)

pact_geo = {
    feat['properties']['development_name']: {
        'conv_date': feat['properties'].get('conversion_date', ''),
        'units':     feat['properties'].get('total_units', 0) or 0,
        'circles':   [],   # not needed for aggregate-only rebuild
    }
    for feat in gj['features']
    if feat['properties'].get('type') == 'pact'
}

print(f'{len(pact_geo)} PACT developments loaded from GeoJSON')

# Load cached incident CSVs (no API calls)
ctrl_by_year             = nypd._load_ctrl_cache()
pact_by_year, post_recs  = nypd._load_pact_post_cache()
pre_by_dev_year          = nypd._load_pact_pre_cache()
pre_recs                 = nypd._load_pact_pre_recs()

ctrl_total = sum(v['all'] for v in ctrl_by_year.values())
pact_total = sum(v['all'] for v in pact_by_year.values())
print(f'Cache: {ctrl_total:,} ctrl + {pact_total:,} pact post-conv + {len(pre_recs):,} pact pre-conv')

# Rebuild site JSONs
print('Building nypd_aggregate.json...')
agg = nypd.build_agg(ctrl_by_year, pact_by_year)
with open(nypd.OUT_AGG, 'w') as f:
    json.dump(agg, f, indent=2)

print('Building nypd_by_conversion.json...')
conv_agg = nypd.build_conv_agg(pact_geo, pre_recs, post_recs)
with open(nypd.OUT_CONV, 'w') as f:
    json.dump(conv_agg, f, indent=2)

print('Building nypd_cohort.json...')
cohort_agg = nypd.build_cohort_agg(pact_geo, pre_by_dev_year, post_recs, agg)
with open(nypd.OUT_COHORT, 'w') as f:
    json.dump(cohort_agg, f, indent=2)

print('Updating GeoJSON NYPD enrichment...')
nypd.update_geojson(post_recs)

print('Done. Verify ytd_through in nypd_cohort.json is close to the cache pull date.')
```

**Verify the output:** open `nypd_cohort.json` and check that `ytd_through` in
the current-year entry is close to the date of the most recent data pull. If it
is stale by months, the cache itself needs refreshing from a machine without the
SSL issue using `python pact_nypd.py --refresh-pact`.

---

## Annual Update Checklist

Run these in order. Steps 2–5 require a machine without the SSL issue, or use
the standalone rebuild workaround for step 5.

1. Download fresh PACT PDF → `analyze.py` reads it at runtime from the NYCHA URL.
2. `python analyze.py` — eviction execution rates
3. `python pact_311.py` — 311 complaints
4. `python pact_permits.py` — DOB job filings
5. `python pact_nypd.py` — NYPD incidents (no code changes needed; year boundary
   is dynamically computed). If caches are recent, run without flags. If a full
   refresh is needed: `python pact_nypd.py --refresh-pact` for PACT only (~5 min),
   `python pact_nypd.py --refresh-ctrl` for control only (~15 min),
   `python pact_nypd.py --refresh` for both (~20 min).
6. If only the site JSONs need regenerating from existing caches (e.g. after a
   BBL master update or GeoJSON edit), use the standalone rebuild above or run
   `python build_site_data.py`.
7. Verify GeoJSON enrichment looks correct for a few developments.
8. `cd ../laeo-net && git add -A && git commit -m "Refresh NYCHA data [date]" && git push`
