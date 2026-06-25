"""
pact_rodent.py — DOHMH rodent inspection analysis: PACT vs. non-PACT NYCHA

Fetches rodent inspection records from NYC Open Data (DOHMH Rodent Inspections,
dataset p937-wjvj) for PACT and non-PACT NYCHA BBLs. Classifies inspections
where result is 'Failed for Rat Activity' or 'Failed for Rat Activity and Other
Reason' as rat fails. Computes annual fail rates and updates site outputs.

Outputs:
  pact_rodent.csv        — per-inspection records for PACT BBLs (gitignored)
  ctrl_rodent.csv        — per-inspection records for control NYCHA BBLs (gitignored)
  rodent_aggregate.json  — annual pact_complete vs. non_pact fail rates
  developments.geojson   — enriched with rodent_data per PACT development

Pass --refresh to force re-fetch even if CSVs already exist.
"""

import csv, json, os, sys, time, urllib.request, urllib.parse
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_SITE = os.path.join(_HERE, '../laeo-net/public/data/nycha')

RODENT_URL   = 'https://data.cityofnewyork.us/resource/p937-wjvj.json'
GEOJSON_PATH = os.path.join(_SITE, 'data/developments.geojson')
MASTER_CSV   = os.path.join(_HERE, 'pact_bbl_master.csv')
CTRL_BBLS    = os.path.join(_HERE, 'control_bbls_pluto.csv')
OUT_PACT     = os.path.join(_HERE, 'pact_rodent.csv')
OUT_CTRL     = os.path.join(_HERE, 'ctrl_rodent.csv')
OUT_AGG      = os.path.join(_SITE, 'data/rodent_aggregate.json')

PAGE       = 50000
TIMEOUT    = 90
RETRIES    = 3
BBL_CHUNK  = 100   # BBLs per IN clause; keeps URLs well under length limits

RAT_FAIL_RESULTS = frozenset({
    'Failed for Rat Activity',
    'Failed for Rat Activity and Other Reason',
})


# ── helpers ───────────────────────────────────────────────────────────────────

def clean_bbl(raw):
    """Normalize a BBL to exactly 10 digits, or return None."""
    s = ''.join(c for c in str(raw or '') if c.isdigit())
    s = s[:10]
    return s if len(s) == 10 else None


def fetch(url, attempt=0):
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except Exception as e:
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt)
            return fetch(url, attempt + 1)
        raise


def fetch_for_bbls(bbl_set):
    """
    Fetch all rodent inspections for a set of BBLs from the DOHMH dataset.
    Queries in chunks of BBL_CHUNK to stay within URL length limits.
    Returns list of raw API dicts.
    """
    bbls = sorted(bbl_set)
    n_chunks = -(-len(bbls) // BBL_CHUNK)   # ceiling division
    all_records = []
    for i in range(0, len(bbls), BBL_CHUNK):
        chunk = bbls[i:i+BBL_CHUNK]
        chunk_num = i // BBL_CHUNK + 1
        in_clause = ','.join(f"'{b}'" for b in chunk)
        where  = f'bbl in({in_clause})'
        offset = 0
        while True:
            params = urllib.parse.urlencode({
                '$where':  where,
                '$select': 'bbl,inspection_date,result,inspectiontype',
                '$limit':  PAGE,
                '$offset': offset,
            })
            batch = fetch(f'{RODENT_URL}?{params}')
            all_records.extend(batch)
            print(f'  chunk {chunk_num}/{n_chunks}: {len(all_records):,} records total', end='\r', flush=True)
            if len(batch) < PAGE:
                break
            offset += PAGE
    print()
    return all_records


def parse_record(raw, bbl_to_dev=None):
    """Convert a raw API dict to a normalized record dict."""
    bbl    = clean_bbl(raw.get('bbl', ''))
    date   = (raw.get('inspection_date') or '')[:10]
    year   = date[:4] if len(date) >= 4 else ''
    result = raw.get('result', '')
    r = {
        'bbl':             bbl or '',
        'inspection_date': raw.get('inspection_date', ''),
        'result':          result,
        'inspection_type': raw.get('inspectiontype', ''),
        'year':            year,
        'rat_fail':        result in RAT_FAIL_RESULTS,
    }
    if bbl_to_dev is not None:
        r['development_name'] = bbl_to_dev.get(bbl, '')
    return r


# ── loaders ───────────────────────────────────────────────────────────────────

def load_pact_bbl_map():
    """Return {bbl: development_name} from pact_bbl_master.csv."""
    bbl_to_dev = {}
    with open(MASTER_CSV) as f:
        for row in csv.DictReader(f):
            dev      = row['development_name']
            bbls_raw = row.get('bbls', '')
            for bbl in (b.strip() for b in bbls_raw.split('|') if b.strip()):
                bbl_to_dev[bbl] = dev
    return bbl_to_dev


def load_ctrl_bbl_set():
    """Return set of control NYCHA BBLs from control_bbls_pluto.csv."""
    bbls = set()
    with open(CTRL_BBLS) as f:
        for row in csv.DictReader(f):
            bbl = clean_bbl(row.get('bbl', ''))
            if bbl:
                bbls.add(bbl)
    return bbls


def load_dev_status():
    """Return {development_name: status} for PACT features from GeoJSON."""
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)
    return {
        feat['properties']['development_name']: feat['properties'].get('status', '')
        for feat in gj['features']
        if feat['properties'].get('type') == 'pact'
    }


def load_csv_cache(path, has_dev):
    """Load a cached rodent CSV back into normalized record dicts."""
    records = []
    with open(path) as f:
        for row in csv.DictReader(f):
            date = (row.get('inspection_date') or '')[:10]
            r = {
                'bbl':             row.get('bbl', ''),
                'inspection_date': row.get('inspection_date', ''),
                'result':          row.get('result', ''),
                'inspection_type': row.get('inspection_type', ''),
                'year':            date[:4] if len(date) >= 4 else '',
                'rat_fail':        row.get('result', '') in RAT_FAIL_RESULTS,
            }
            if has_dev:
                r['development_name'] = row.get('development_name', '')
            records.append(r)
    return records


# ── aggregation ───────────────────────────────────────────────────────────────

def build_aggregate(pact_records, ctrl_records, dev_status):
    """
    Build rodent_aggregate.json.
    PACT side uses Construction Complete developments only (pact_complete).
    fail_rate_pct is None for years with fewer than 5 inspections.
    """
    pact_by_yr = defaultdict(lambda: {'inspections': 0, 'rat_fails': 0})
    ctrl_by_yr = defaultdict(lambda: {'inspections': 0, 'rat_fails': 0})

    for r in pact_records:
        if dev_status.get(r.get('development_name', '')) != 'Construction Complete':
            continue
        yr = r['year']
        if not yr:
            continue
        pact_by_yr[yr]['inspections'] += 1
        if r['rat_fail']:
            pact_by_yr[yr]['rat_fails'] += 1

    for r in ctrl_records:
        yr = r['year']
        if not yr:
            continue
        ctrl_by_yr[yr]['inspections'] += 1
        if r['rat_fail']:
            ctrl_by_yr[yr]['rat_fails'] += 1

    years = sorted(set(list(pact_by_yr.keys()) + list(ctrl_by_yr.keys())))
    result = []
    for yr in years:
        p = pact_by_yr[yr]
        c = ctrl_by_yr[yr]
        pn, pf = p['inspections'], p['rat_fails']
        cn, cf = c['inspections'], c['rat_fails']
        row = {
            'year': yr,
            'pact_complete': {
                'inspections':   pn,
                'rat_fails':     pf,
                'fail_rate_pct': round(pf / pn * 100, 1) if pn >= 5 else None,
            },
            'non_pact': {
                'inspections':   cn,
                'rat_fails':     cf,
                'fail_rate_pct': round(cf / cn * 100, 1) if cn >= 5 else None,
            },
        }
        result.append(row)
        print(f'  {yr}: pact {pf}/{pn} = {row["pact_complete"]["fail_rate_pct"]}%'
              f'  ctrl {cf}/{cn} = {row["non_pact"]["fail_rate_pct"]}%')
    return result


def update_geojson(pact_records):
    """Enrich each PACT feature in developments.geojson with rodent_data."""
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)

    by_dev = defaultdict(list)
    for r in pact_records:
        dev = r.get('development_name', '')
        if dev:
            by_dev[dev].append(r)

    updated = 0
    for feat in gj['features']:
        if feat['properties'].get('type') != 'pact':
            continue
        dev  = feat['properties']['development_name']
        recs = by_dev.get(dev, [])
        if not recs:
            feat['properties'].pop('rodent_data', None)
            continue

        total = len(recs)
        fails = sum(1 for r in recs if r['rat_fail'])
        by_yr = defaultdict(lambda: {'inspections': 0, 'rat_fails': 0})
        for r in recs:
            if r['year']:
                by_yr[r['year']]['inspections'] += 1
                if r['rat_fail']:
                    by_yr[r['year']]['rat_fails'] += 1

        by_yr_out = {}
        for yr, v in sorted(by_yr.items()):
            n, f = v['inspections'], v['rat_fails']
            by_yr_out[yr] = {
                'inspections':   n,
                'rat_fails':     f,
                'fail_rate_pct': round(f / n * 100, 1) if n else 0.0,
            }

        feat['properties']['rodent_data'] = {
            'total_inspections': total,
            'total_rat_fails':   fails,
            'fail_rate_pct':     round(fails / total * 100, 1) if total else None,
            'by_year':           by_yr_out,
        }
        updated += 1

    with open(GEOJSON_PATH, 'w') as f:
        json.dump(gj, f, separators=(',', ':'))
    print(f'Updated {updated} PACT features in GeoJSON')


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    refresh = '--refresh' in sys.argv

    print('Loading BBL lists...')
    bbl_to_dev   = load_pact_bbl_map()
    ctrl_bbl_set = load_ctrl_bbl_set()
    pact_bbl_set = set(bbl_to_dev.keys())
    dev_status   = load_dev_status()
    print(f'  PACT BBLs: {len(pact_bbl_set)}  Ctrl BBLs: {len(ctrl_bbl_set)}')
    print(f'  PACT developments: {len(set(bbl_to_dev.values()))}')

    if not refresh and os.path.exists(OUT_PACT) and os.path.exists(OUT_CTRL):
        print(f'Loading cached CSVs (pass --refresh to re-fetch)...')
        pact_records = load_csv_cache(OUT_PACT, has_dev=True)
        ctrl_records = load_csv_cache(OUT_CTRL, has_dev=False)
        print(f'  PACT: {len(pact_records):,}  Ctrl: {len(ctrl_records):,}')
    else:
        print('Fetching PACT rodent inspections...')
        raw_pact     = fetch_for_bbls(pact_bbl_set)
        pact_records = [parse_record(r, bbl_to_dev) for r in raw_pact]
        with open(OUT_PACT, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=[
                'bbl', 'inspection_date', 'result', 'inspection_type', 'development_name',
            ], extrasaction='ignore')
            w.writeheader()
            w.writerows(pact_records)
        print(f'Wrote {OUT_PACT} ({len(pact_records):,} rows)')

        print('Fetching ctrl rodent inspections...')
        raw_ctrl     = fetch_for_bbls(ctrl_bbl_set)
        ctrl_records = [parse_record(r) for r in raw_ctrl]
        with open(OUT_CTRL, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=[
                'bbl', 'inspection_date', 'result', 'inspection_type',
            ], extrasaction='ignore')
            w.writeheader()
            w.writerows(ctrl_records)
        print(f'Wrote {OUT_CTRL} ({len(ctrl_records):,} rows)')

    print('\nBuilding rodent_aggregate.json...')
    agg = build_aggregate(pact_records, ctrl_records, dev_status)
    with open(OUT_AGG, 'w') as f:
        json.dump(agg, f, indent=2)
    print(f'Wrote {OUT_AGG}')

    print('\nUpdating GeoJSON...')
    update_geojson(pact_records)

    print('\nDone.')
