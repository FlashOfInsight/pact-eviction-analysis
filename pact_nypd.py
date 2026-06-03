"""
pact_nypd.py  —  NYPD complaint analysis: PACT vs. non-PACT NYCHA

Uniform 150m-per-building circle methodology for both groups.
No prem_typ_desc filter; captures incidents at and around each building.

Phase 0a — PACT spatial index (PLUTO BBL lookup, per-lot coordinates)
Phase 0b — Non-PACT spatial index (NYCHA Residential Addresses, 3ub5-4ph8)
Phase A  — Non-PACT incident counts via per-building circles  [cacheable]
Phase B  — PACT incident counts: pre-conv (5yr) + post-conv  [cacheable]
Phase C  — Annual PACT vs. non-PACT aggregate rates + type breakdown
Phase D  — Pre/post bucket aggregates (years relative to conversion)
Phase E  — GeoJSON enrichment

Re-run skips Phases A and B if output CSVs exist.
Pass --refresh, --refresh-ctrl, or --refresh-pact to force re-fetch.
"""

import csv, json, math, os, sys, time, urllib.request, urllib.parse
from collections import defaultdict
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_SITE = os.path.join(_HERE, '../laeo-net/public/data/nycha')

# ── endpoints ─────────────────────────────────────────────────────────────────
NYPD_HIST  = 'https://data.cityofnewyork.us/resource/qgea-i56i.json'
NYPD_CURR  = 'https://data.cityofnewyork.us/resource/5uac-w243.json'
PLUTO_BASE = 'https://data.cityofnewyork.us/resource/64uk-42ks.json'
NYCHA_ADDR = 'https://data.cityofnewyork.us/resource/3ub5-4ph8.json'

# ── paths ─────────────────────────────────────────────────────────────────────
MASTER_CSV   = os.path.join(_HERE, 'pact_bbl_master.csv')
GEOJSON_PATH = os.path.join(_SITE, 'data/developments.geojson')
OUT_CTRL     = os.path.join(_HERE, 'nypd_control.csv')
OUT_PACT_PRE = os.path.join(_HERE, 'nypd_pact_pre.csv')
OUT_PACT     = os.path.join(_HERE, 'nypd_pact.csv')
OUT_AGG      = os.path.join(_SITE, 'data/nypd_aggregate.json')
OUT_CONV     = os.path.join(_SITE, 'data/nypd_by_conversion.json')
OUT_COHORT   = os.path.join(_SITE, 'data/nypd_cohort.json')

# ── config ────────────────────────────────────────────────────────────────────
MIN_YEAR        = 2015
PRE_CONV_YEARS  = 5
MIN_RADIUS_M    = 150
MAX_RADIUS_M    = 500
RADIUS_BUFFER_M = 100
PAGE            = 50000
TIMEOUT         = 90
RETRIES         = 3
MIN_PACT_DEVS   = 3

TYPES     = ['FELONY', 'MISDEMEANOR', 'VIOLATION']
CURR_YEAR = date.today().year

DATASETS = [
    ('hist', NYPD_HIST, 'lat_lon',        f"cmplnt_fr_dt >= '2015-01-01T00:00:00' AND cmplnt_fr_dt < '{CURR_YEAR}-01-01T00:00:00'"),
    ('curr', NYPD_CURR, 'geocoded_column', f"cmplnt_fr_dt >= '{CURR_YEAR}-01-01T00:00:00'"),
]

# ── http ──────────────────────────────────────────────────────────────────────
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

def paginate(base, where, select):
    records, offset = [], 0
    while True:
        params = urllib.parse.urlencode({
            '$where': where, '$select': ','.join(select),
            '$limit': PAGE, '$offset': offset,
        })
        batch = fetch(f'{base}?{params}')
        records.extend(batch)
        print(f'      {len(records):,} records', end='\r', flush=True)
        if len(batch) < PAGE:
            print()
            return records
        offset += PAGE

# ── geometry ──────────────────────────────────────────────────────────────────
def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))

def _make_circles(pts):
    """Dedup points; return (circles, outer_r, clat, clon, mode_str)."""
    seen_keys = set()
    unique = []
    for p in pts:
        k = (round(p[0], 4), round(p[1], 4))
        if k not in seen_keys:
            seen_keys.add(k)
            unique.append(p)
    pts = unique
    if not pts:
        return [], 0, 0.0, 0.0, 'empty'
    clat = sum(p[0] for p in pts) / len(pts)
    clon = sum(p[1] for p in pts) / len(pts)
    spread = max((haversine_m(clat, clon, p[0], p[1]) for p in pts), default=0)
    single_r = max(spread + RADIUS_BUFFER_M, MIN_RADIUS_M)
    if single_r > MAX_RADIUS_M:
        circles = [(p[0], p[1], MIN_RADIUS_M) for p in pts]
        outer_r = round(spread + MIN_RADIUS_M)
        mode    = f'per-parcel ({len(circles)}×{MIN_RADIUS_M}m, spread={spread:.0f}m)'
    else:
        circles = [(clat, clon, round(single_r))]
        outer_r = round(single_r)
        mode    = f'centroid r={single_r:.0f}m'
    return circles, outer_r, clat, clon, mode

# ── Phase 0a: PACT spatial index ──────────────────────────────────────────────
def build_pact_geo():
    """Return (geo, pact_bbl_set).
    geo: {dev_name: {circles, lat, lon, radius_m, units, conv_date}}
    """
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)
    dev_meta = {
        feat['properties']['development_name']: feat['properties']
        for feat in gj['features']
        if feat['properties'].get('type') == 'pact'
    }
    with open(MASTER_CSV) as f:
        bbl_map = {}
        for row in csv.DictReader(f):
            bbls = [b.strip() for b in row['bbls'].split('|') if b.strip()]
            if bbls:
                bbl_map[row['development_name']] = bbls

    all_bbls = [b for bbls in bbl_map.values() for b in bbls]
    print(f'  Querying PLUTO for {len(all_bbls)} BBLs...')
    bbl_coords = {}
    for i in range(0, len(all_bbls), 50):
        chunk = all_bbls[i:i+50]
        in_clause = ','.join(f"'{b}'" for b in chunk)
        params = urllib.parse.urlencode({
            '$where':  f'bbl in({in_clause})',
            '$select': 'bbl,latitude,longitude',
            '$limit':  '100',
        })
        for row in fetch(f'{PLUTO_BASE}?{params}'):
            if row.get('latitude') and row.get('longitude'):
                bbl_key = str(int(float(row['bbl'])))
                bbl_coords[bbl_key] = (float(row['latitude']), float(row['longitude']))
        time.sleep(0.2)
    print(f'  Resolved {len(bbl_coords)}/{len(all_bbls)} BBL coordinates')

    pact_bbl_set = set(all_bbls)

    geo = {}
    for dev, bbls in bbl_map.items():
        if dev not in dev_meta:
            continue
        pts = [bbl_coords[b] for b in bbls if b in bbl_coords]
        if not pts:
            print(f'  [warn] no coordinates for {dev}')
            continue
        circles, outer_r, clat, clon, mode = _make_circles(pts)
        props = dev_meta[dev]
        geo[dev] = {
            'lat': clat, 'lon': clon, 'radius_m': outer_r,
            'circles':   circles,
            'units':     props.get('total_units') or 0,
            'conv_date': props.get('conversion_date') or '',
        }
        print(f'  {dev}: {mode}')

    return geo, pact_bbl_set

# ── Phase 0b: non-PACT spatial index ─────────────────────────────────────────
def build_ctrl_geo(pact_bbl_set):
    """Per-building spatial index from NYCHA Residential Addresses (3ub5-4ph8)."""
    print('  Fetching NYCHA building list...')
    params = urllib.parse.urlencode({
        '$select': 'development,borough_block_lot,latitude,longitude',
        '$limit':  '5000',
    })
    records = fetch(f'{NYCHA_ADDR}?{params}')
    print(f'  {len(records):,} buildings in dataset')

    dev_pts = defaultdict(list)
    for r in records:
        bbl = r.get('borough_block_lot', '').strip()
        if bbl in pact_bbl_set:
            continue
        dev = r.get('development', '').strip()
        if not dev or not r.get('latitude') or not r.get('longitude'):
            continue
        try:
            dev_pts[dev].append((float(r['latitude']), float(r['longitude'])))
        except (TypeError, ValueError):
            continue

    geo = {}
    per_parcel_n = 0
    for dev, pts in sorted(dev_pts.items()):
        circles, outer_r, clat, clon, mode = _make_circles(pts)
        if not circles:
            continue
        if len(circles) > 1:
            per_parcel_n += 1
        geo[dev] = {
            'lat': clat, 'lon': clon, 'radius_m': outer_r,
            'circles': circles,
        }

    print(f'  {len(geo)} non-PACT developments indexed ({per_parcel_n} in per-parcel mode)')
    return geo

# ── circle query ──────────────────────────────────────────────────────────────
_FIELDS = ['cmplnt_num', 'cmplnt_fr_dt', 'law_cat_cd', 'latitude', 'longitude']

def query_dev_circles(circles, date_from, date_to, seen):
    """
    Query all circles for date_from..date_to, deduplicating via shared `seen` set.
    Returns list of {cmplnt_num, date, year, type}.
    date_from/date_to: 'YYYY-MM-DD'; date_to=None means open-ended.
    """
    date_clause = f"cmplnt_fr_dt >= '{date_from}T00:00:00'"
    if date_to:
        date_clause += f" AND cmplnt_fr_dt < '{date_to}T00:00:00'"

    recs = []
    for _lbl, base, geo_col, date_bound in DATASETS:
        for clat, clon, cr in circles:
            where = (
                f"within_circle({geo_col},{clat},{clon},{cr})"
                f" AND {date_clause} AND {date_bound}"
                f" AND latitude IS NOT NULL"
            )
            try:
                batch = paginate(base, where, _FIELDS)
                for r in batch:
                    cid = r.get('cmplnt_num', '')
                    if cid and cid in seen:
                        continue
                    if cid:
                        seen.add(cid)
                    date_str = (r.get('cmplnt_fr_dt') or '')[:10]
                    year = int(date_str[:4]) if len(date_str) >= 4 else 0
                    if year < MIN_YEAR:
                        continue
                    t = (r.get('law_cat_cd') or '').strip().upper()
                    recs.append({'cmplnt_num': cid, 'date': date_str, 'year': year, 'type': t})
            except Exception as e:
                print(f'    [{_lbl}] FAILED: {e}', flush=True)
            time.sleep(0.3)
    return recs

# ── Phase A: non-PACT incident counts ─────────────────────────────────────────
def fetch_control(ctrl_geo):
    seen = set()
    ctrl_by_year = defaultdict(lambda: defaultdict(int))  # year -> type -> n
    all_recs = []
    total = len(ctrl_geo)
    for i, (dev, g) in enumerate(sorted(ctrl_geo.items()), 1):
        print(f'  [{i}/{total}] {dev}', flush=True)
        recs = query_dev_circles(g['circles'], f'{MIN_YEAR}-01-01', None, seen)
        for r in recs:
            ctrl_by_year[r['year']]['all'] += 1
            if r['type'] in TYPES:
                ctrl_by_year[r['year']][r['type']] += 1
        all_recs.extend({'dev': dev, **r} for r in recs)
        print(f'    → {len(recs):,}  ({len(seen):,} total unique)', flush=True)
    return ctrl_by_year, all_recs

# ── Phase B: PACT incident counts ─────────────────────────────────────────────
def fetch_pact(pact_geo):
    post_seen    = set()
    pact_by_year = defaultdict(lambda: defaultdict(int))  # year -> type -> n
    post_recs    = []

    pre_by_dev_year = {}   # dev -> year -> type -> n
    pre_recs        = []

    for dev, g in sorted(pact_geo.items()):
        conv_date = g['conv_date']
        if not conv_date:
            print(f'  [skip] {dev} — no conversion date')
            continue
        conv_year = int(conv_date[:4])
        n_circles = len(g['circles'])
        mode = f'{n_circles} parcels' if n_circles > 1 else f'r={g["circles"][0][2]}m'
        print(f'  {dev} (conv {conv_date}, {mode})', flush=True)

        # Post-conversion
        post = query_dev_circles(g['circles'], conv_date, None, post_seen)
        for r in post:
            pact_by_year[r['year']]['all'] += 1
            if r['type'] in TYPES:
                pact_by_year[r['year']][r['type']] += 1
        post_recs.extend({'development_name': dev, **r} for r in post)
        print(f'    post: {len(post):,}', flush=True)

        # Pre-conversion (up to PRE_CONV_YEARS back, floored at MIN_YEAR)
        pre_start = max(conv_year - PRE_CONV_YEARS, MIN_YEAR)
        pre_seen  = set()
        pre = query_dev_circles(g['circles'], f'{pre_start}-01-01', conv_date, pre_seen)
        by_year = defaultdict(lambda: defaultdict(int))
        for r in pre:
            by_year[r['year']]['all'] += 1
            if r['type'] in TYPES:
                by_year[r['year']][r['type']] += 1
        pre_by_dev_year[dev] = {yr: dict(counts) for yr, counts in by_year.items()}
        pre_recs.extend({'development_name': dev, **r} for r in pre)
        print(f'    pre ({pre_start}–{conv_year - 1}): {len(pre):,}', flush=True)

    return pact_by_year, post_recs, pre_by_dev_year, pre_recs

# ── Phase C: annual aggregate rates ───────────────────────────────────────────
def build_agg(ctrl_by_year, pact_by_year):
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)
    pact_props = [
        p['properties'] for p in gj['features']
        if p['properties'].get('type') == 'pact' and p['properties'].get('conversion_date')
    ]
    ctrl_base_units = 176_766

    def _rates(n_by_type, units):
        return {k: round(v / units * 1000, 2) if units else None
                for k, v in n_by_type.items()}

    result = []
    for year in range(MIN_YEAR, CURR_YEAR + 1):
        pact_units = sum(p.get('total_units') or 0 for p in pact_props if int(p['conversion_date'][:4]) <= year)
        extra      = sum(p.get('total_units') or 0 for p in pact_props if int(p['conversion_date'][:4]) > year)
        ctrl_units = ctrl_base_units + extra
        pact_devs  = sum(1 for p in pact_props if int(p['conversion_date'][:4]) <= year)

        cn = {t: ctrl_by_year.get(year, {}).get(t, 0) for t in ['all'] + TYPES}
        pn = {t: pact_by_year.get(year, {}).get(t, 0) for t in ['all'] + TYPES}
        use_pact = pact_units > 0 and pact_devs >= MIN_PACT_DEVS

        row = {
            'year':       year,
            'pact_devs':  pact_devs,
            'pact_units': pact_units,
            'pact_n':     pn,
            'pact_rate':  _rates(pn, pact_units) if use_pact else {k: None for k in ['all'] + TYPES},
            'ctrl_units': ctrl_units,
            'ctrl_n':     cn,
            'ctrl_rate':  _rates(cn, ctrl_units),
        }
        result.append(row)
        print(f'  {year}: PACT {pn["all"]}/{pact_units}u={row["pact_rate"]["all"]}  ctrl {cn["all"]}/{ctrl_units}u={row["ctrl_rate"]["all"]}')

    return result

# ── Phase D: pre/post conversion bucket aggregates ────────────────────────────
def build_conv_agg(pact_geo, pre_recs, post_recs):
    """Anniversary-year buckets: bucket = floor((incident_date - conv_date).days / 365).
    Every bucket represents exactly 365 days of exposure, so rates are directly comparable."""
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)
    dev_units = {
        feat['properties']['development_name']: feat['properties'].get('total_units') or 0
        for feat in gj['features']
        if feat['properties'].get('type') == 'pact'
    }

    conv_dates = {dev: g.get('conv_date', '') for dev, g in pact_geo.items()}

    # bucket -> set of devs + incident counts
    bucket_devs = defaultdict(set)
    bucket_n    = defaultdict(lambda: defaultdict(int))

    for r in pre_recs + list(post_recs):
        dev = r['development_name']
        conv_date = conv_dates.get(dev, '')
        if not conv_date or not dev_units.get(dev, 0):
            continue
        inc_date = (r.get('date') or '')[:10]
        if len(inc_date) < 10:
            continue
        try:
            bucket = (date.fromisoformat(inc_date) - date.fromisoformat(conv_date)).days // 365
        except Exception:
            continue
        if bucket < -PRE_CONV_YEARS:
            continue
        t = (r.get('type') or '').strip().upper()
        bucket_devs[bucket].add(dev)
        bucket_n[bucket]['all'] += 1
        if t in TYPES:
            bucket_n[bucket][t] += 1

    result = []
    for bucket in sorted(bucket_devs.keys()):
        devs = bucket_devs[bucket]
        u = sum(dev_units.get(d, 0) for d in devs)
        n = {t: bucket_n[bucket].get(t, 0) for t in ['all'] + TYPES}
        result.append({
            'bucket': bucket,
            'devs':   len(devs),
            'units':  u,
            'n':      n,
            'rate':   {t: round(n[t] / u * 1000, 2) if u else None for t in ['all'] + TYPES},
        })
        print(f'  bucket {bucket:+d}: {len(devs)} devs, n={n["all"]}, rate={result[-1]["rate"]["all"]}')

    return result

# ── Phase D2: cohort calendar-year aggregate ─────────────────────────────────
def build_cohort_agg(pact_geo, pre_by_dev_year, post_recs, agg):
    """
    For each calendar year, pool incidents across all PACT-destined buildings:
    - Year >= conv_year: post-conversion incidents for that dev
    - conv_year-PRE_CONV_YEARS <= year < conv_year: pre-conversion incidents
    Units denominator = sum of units for developments with data in that year.
    Ctrl data reused from Phase C aggregate to avoid recomputation.
    """
    # Index post-conv by (dev, year)
    post_by_dev_yr = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for r in post_recs:
        dev = r['development_name']
        yr  = int(r['year'])
        t   = (r.get('type') or '').strip().upper()
        post_by_dev_yr[dev][yr]['all'] += 1
        if t in TYPES:
            post_by_dev_yr[dev][yr][t] += 1

    # Ctrl lookup from Phase C
    ctrl_by_yr = {row['year']: row for row in agg}

    result = []
    for year in range(MIN_YEAR, CURR_YEAR + 1):
        n = {'all': 0, **{t: 0 for t in TYPES}}
        total_units = 0
        devs_in_scope = 0
        devs_converted = 0

        for dev, g in pact_geo.items():
            conv_date = g.get('conv_date', '')
            if not conv_date:
                continue
            conv_year = int(conv_date[:4])
            units     = g.get('units', 0) or 0
            if not units:
                continue
            pre_start = max(conv_year - PRE_CONV_YEARS, MIN_YEAR)

            if year >= conv_year:
                devs_converted += 1
                devs_in_scope  += 1
                total_units    += units
                for t in n:
                    n[t] += post_by_dev_yr[dev][year].get(t, 0)
            elif year >= pre_start:
                devs_in_scope += 1
                total_units   += units
                pre_yr = pre_by_dev_year.get(dev, {}).get(year, {})
                for t in n:
                    n[t] += pre_yr.get(t, 0)

        if total_units == 0:
            continue

        ctrl_row = ctrl_by_yr.get(year, {})
        rate = {t: round(n[t] / total_units * 1000, 2) for t in n}
        entry = {
            'year':                year,
            'pact_devs_in_scope':  devs_in_scope,
            'pact_devs_converted': devs_converted,
            'pact_units':          total_units,
            'pact_n':              n,
            'pact_rate':           rate,
            'ctrl_units':          ctrl_row.get('ctrl_units'),
            'ctrl_n':              ctrl_row.get('ctrl_n'),
            'ctrl_rate':           ctrl_row.get('ctrl_rate'),
        }
        if year == CURR_YEAR:
            # Use the latest actual record date, not today — data has a reporting lag
            # and the cache may not extend to the current date.
            curr_dates = [r['date'][:10] for r in post_recs
                          if int(r['year']) == CURR_YEAR and r.get('date', '')[:4] == str(CURR_YEAR)]
            if curr_dates:
                max_dt   = date.fromisoformat(max(curr_dates))
                ytd_days = (max_dt - date(CURR_YEAR, 1, 1)).days + 1
            else:
                ytd_days = (date.today() - date(CURR_YEAR, 1, 1)).days
            frac = ytd_days / 365.25
            entry['ytd_days']      = ytd_days
            entry['ytd_through']   = max_dt.isoformat() if curr_dates else None
            entry['pact_annualized_rate'] = {
                t: round(n[t] / total_units / frac * 1000, 2) for t in n
            }
            ctrl_n = ctrl_row.get('ctrl_n') or {}
            ctrl_u = ctrl_row.get('ctrl_units') or 0
            if ctrl_u:
                entry['ctrl_annualized_rate'] = {
                    t: round(ctrl_n.get(t, 0) / ctrl_u / frac * 1000, 2)
                    for t in ['all'] + TYPES
                }
        result.append(entry)
        c = ctrl_row.get('ctrl_rate', {})
        print(f'  {year}: {devs_converted}/{devs_in_scope} converted  '
              f'n={n["all"]}/{total_units}u={rate["all"]}  '
              f'ctrl={c.get("all")}')

    return result

# ── Phase E: GeoJSON enrichment ───────────────────────────────────────────────
def update_geojson(post_recs):
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)
    by_dev = defaultdict(list)
    for r in post_recs:
        by_dev[r['development_name']].append(r)

    updated = 0
    for feat in gj['features']:
        dev  = feat['properties']['development_name']
        recs = by_dev.get(dev, [])
        if not recs:
            feat['properties'].pop('nypd_data', None)
            continue
        by_year = defaultdict(lambda: {'n': 0, 'FELONY': 0, 'MISDEMEANOR': 0, 'VIOLATION': 0})
        for r in recs:
            yr = str(r['year'])
            by_year[yr]['n'] += 1
            t = (r.get('type') or '').strip().upper()
            if t in TYPES:
                by_year[yr][t] += 1
        feat['properties']['nypd_data'] = {
            'total':   len(recs),
            'by_year': {yr: dict(v) for yr, v in sorted(by_year.items())},
        }
        updated += 1

    with open(GEOJSON_PATH, 'w') as f:
        json.dump(gj, f, separators=(',', ':'))
    print(f'Updated {updated} features in GeoJSON')

# ── cache loaders ─────────────────────────────────────────────────────────────
def _load_ctrl_cache():
    ctrl_by_year = defaultdict(lambda: defaultdict(int))
    with open(OUT_CTRL) as f:
        for row in csv.DictReader(f):
            yr = int(row['year'])
            t  = (row.get('type') or '').strip().upper()
            ctrl_by_year[yr]['all'] += 1
            if t in TYPES:
                ctrl_by_year[yr][t] += 1
    return ctrl_by_year

def _load_pact_post_cache():
    pact_by_year = defaultdict(lambda: defaultdict(int))
    post_recs = []
    with open(OUT_PACT) as f:
        for row in csv.DictReader(f):
            yr = int(row['year'])
            t  = (row.get('type') or '').strip().upper()
            pact_by_year[yr]['all'] += 1
            if t in TYPES:
                pact_by_year[yr][t] += 1
            post_recs.append(dict(row))
    return pact_by_year, post_recs

def _load_pact_pre_cache():
    pre = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    with open(OUT_PACT_PRE) as f:
        for row in csv.DictReader(f):
            dev = row['development_name']
            yr  = int(row['year'])
            t   = (row.get('type') or '').strip().upper()
            pre[dev][yr]['all'] += 1
            if t in TYPES:
                pre[dev][yr][t] += 1
    return {dev: {yr: dict(counts) for yr, counts in by_yr.items()}
            for dev, by_yr in pre.items()}

def _load_pact_pre_recs():
    with open(OUT_PACT_PRE) as f:
        return [dict(row) for row in csv.DictReader(f)]

# ── main ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    refresh_ctrl = '--refresh' in sys.argv or '--refresh-ctrl' in sys.argv
    refresh_pact = '--refresh' in sys.argv or '--refresh-pact' in sys.argv

    print('=== Phase 0a: PACT spatial index ===')
    pact_geo, pact_bbl_set = build_pact_geo()
    print(f'  {len(pact_geo)} PACT developments indexed\n')

    print('=== Phase 0b: non-PACT spatial index ===')
    ctrl_geo = build_ctrl_geo(pact_bbl_set)
    print()

    if not refresh_ctrl and os.path.exists(OUT_CTRL):
        print('=== Phase A: loading cached non-PACT data ===')
        ctrl_by_year = _load_ctrl_cache()
        total = sum(v['all'] for v in ctrl_by_year.values())
        print(f'  {total:,} records from cache\n')
    else:
        print('=== Phase A: non-PACT incident counts ===')
        ctrl_by_year, ctrl_recs = fetch_control(ctrl_geo)
        ctrl_fields = ['dev', 'cmplnt_num', 'date', 'year', 'type']
        with open(OUT_CTRL, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=ctrl_fields, extrasaction='ignore')
            w.writeheader(); w.writerows(ctrl_recs)
        total = sum(v['all'] for v in ctrl_by_year.values())
        print(f'Wrote {OUT_CTRL} ({total:,} rows)\n')

    if not refresh_pact and os.path.exists(OUT_PACT) and os.path.exists(OUT_PACT_PRE):
        print('=== Phase B: loading cached PACT data ===')
        pact_by_year, post_recs = _load_pact_post_cache()
        pre_by_dev_year          = _load_pact_pre_cache()
        pre_recs                 = _load_pact_pre_recs()
        post_n = sum(v['all'] for v in pact_by_year.values())
        pre_n  = len(pre_recs)
        print(f'  {post_n:,} post-conv + {pre_n:,} pre-conv from cache\n')
    else:
        print('=== Phase B: PACT incident counts ===')
        pact_by_year, post_recs, pre_by_dev_year, pre_recs = fetch_pact(pact_geo)
        pact_fields = ['development_name', 'cmplnt_num', 'date', 'year', 'type']
        with open(OUT_PACT, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=pact_fields, extrasaction='ignore')
            w.writeheader(); w.writerows(post_recs)
        with open(OUT_PACT_PRE, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=pact_fields, extrasaction='ignore')
            w.writeheader(); w.writerows(pre_recs)
        print(f'Wrote {OUT_PACT} ({len(post_recs):,} post-conv rows)')
        print(f'Wrote {OUT_PACT_PRE} ({len(pre_recs):,} pre-conv rows)\n')

    print('=== Phase C: annual aggregate rates ===')
    agg = build_agg(ctrl_by_year, pact_by_year)
    with open(OUT_AGG, 'w') as f:
        json.dump(agg, f, indent=2)
    print(f'Wrote {OUT_AGG}\n')

    print('=== Phase D: pre/post conversion buckets ===')
    conv_agg = build_conv_agg(pact_geo, pre_recs, post_recs)
    with open(OUT_CONV, 'w') as f:
        json.dump(conv_agg, f, indent=2)
    print(f'Wrote {OUT_CONV}\n')

    print('=== Phase D2: cohort calendar-year aggregate ===')
    cohort_agg = build_cohort_agg(pact_geo, pre_by_dev_year, post_recs, agg)
    with open(OUT_COHORT, 'w') as f:
        json.dump(cohort_agg, f, indent=2)
    print(f'Wrote {OUT_COHORT}\n')

    print('=== Phase E: GeoJSON enrichment ===')
    update_geojson(post_recs)

    print('\nDone.')
