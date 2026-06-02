"""
pact_permits.py — DOB job application filings at PACT developments

For each PACT development (post-conversion date), fetches all DOB job filings
from the NYC Open Data DOB Job Application Filings dataset (w9ak-ipjd) and
computes declared construction costs by year and year-since-conversion.

Outputs:
  pact_permits.csv           — raw records
  data/permits_agg.json      — years-since-conversion aggregate (≥3 devs)
  data/permits_by_year.json  — calendar-year totals across all PACT devs
  developments.geojson       — enriched with permit_data per PACT feature
"""

import csv, json, os, time, urllib.request, urllib.parse
from collections import defaultdict

_os = os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_SITE = _os.path.join(_HERE, "../laeo-net/public/data/nycha")

DOB_BASE = "https://data.cityofnewyork.us/resource/w9ak-ipjd.json"
MASTER_CSV = _os.path.join(_HERE, "pact_bbl_master.csv")
GEOJSON_PATH = _os.path.join(_SITE, "data/developments.geojson")
OUT_CSV = _os.path.join(_HERE, "pact_permits.csv")
OUT_AGG = _os.path.join(_SITE, "data/permits_agg.json")
OUT_BY_YEAR = _os.path.join(_SITE, "data/permits_by_year.json")
MIN_DEVS = 3
TIMEOUT = 45


def fetch_conv_dates():
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)
    return {
        feat["properties"]["development_name"]: feat["properties"]["conversion_date"]
        for feat in gj["features"]
        if feat["properties"].get("conversion_date")
    }


def load_bbl_map():
    bbl_to_dev = {}
    dev_to_bbls = {}
    with open(MASTER_CSV) as f:
        for row in csv.DictReader(f):
            dev = row["development_name"]
            bbls = [b.strip() for b in row["bbls"].split("|") if b.strip()]
            dev_to_bbls[dev] = bbls
            for bbl in bbls:
                bbl_to_dev[bbl] = dev
    return bbl_to_dev, dev_to_bbls


def fetch_permits_for_bbl(bbl, conv_date, retries=3):
    """Fetch all DOB filings for a single BBL on or after conv_date."""
    records = []
    offset = 0
    limit = 1000
    while True:
        params = urllib.parse.urlencode({
            "$where": f"bbl='{bbl}' AND filing_date >= '{conv_date}'",
            "$limit": str(limit),
            "$offset": str(offset),
            "$select": "bbl,filing_date,job_type,initial_cost,filing_status,general_construction_work_type_",
        })
        url = f"{DOB_BASE}?{params}"
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    batch = json.loads(r.read())
                    records.extend(batch)
                    if len(batch) < limit:
                        return records
                    offset += limit
                    break
            except Exception as e:
                if attempt == retries - 1:
                    print(f"    [warn] BBL {bbl} failed: {e}")
                    return records
                time.sleep(2)
    return records


def fetch_all_pact_permits():
    conv_dates = fetch_conv_dates()
    bbl_to_dev, dev_to_bbls = load_bbl_map()
    all_records = []
    devs = [(dev, conv_dates[dev], bbls)
            for dev, bbls in dev_to_bbls.items()
            if dev in conv_dates and bbls]
    devs.sort(key=lambda x: x[0])
    print(f"Fetching DOB permits for {len(devs)} developments...")
    for dev, conv, bbls in devs:
        print(f"  {dev} (conv: {conv}, {len(bbls)} BBLs)")
        dev_records = []
        for bbl in bbls:
            recs = fetch_permits_for_bbl(bbl, conv)
            for r in recs:
                r["development_name"] = dev
                r["conversion_date"] = conv
                dev_records.append(r)
            time.sleep(0.15)
        print(f"    → {len(dev_records)} filings")
        all_records.extend(dev_records)
    print(f"\nTotal: {len(all_records)} records")
    return all_records


def write_csv(records):
    if not records:
        return
    fields = ["development_name", "conversion_date", "bbl", "filing_date",
              "job_type", "initial_cost", "filing_status",
              "general_construction_work_type_"]
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"Wrote {OUT_CSV}")


def _cost(r):
    try:
        return float(r.get("initial_cost") or 0)
    except:
        return 0.0


def build_permits_agg(records, units_by_dev):
    """Year-since-conversion aggregate (pooling devs, ≥MIN_DEVS)."""
    # per dev per year_offset
    dev_year = defaultdict(lambda: defaultdict(float))  # dev -> year_offset -> cost
    dev_conv = {}
    for r in records:
        dev = r["development_name"]
        conv = r.get("conversion_date", "")
        if not conv:
            continue
        conv_year = int(conv[:4])
        filing_year = int(r.get("filing_date", "0000")[:4])
        if filing_year == 0:
            continue
        offset = filing_year - conv_year
        if offset < 0 or offset > 10:
            continue
        dev_year[dev][offset] += _cost(r)
        dev_conv[dev] = conv_year

    # pool by offset
    buckets = defaultdict(lambda: {"total_cost": 0.0, "total_units": 0, "dev_count": 0, "devs": []})
    for dev, yr_costs in dev_year.items():
        units = units_by_dev.get(dev, 0)
        for offset, cost in yr_costs.items():
            buckets[offset]["total_cost"] += cost
            buckets[offset]["total_units"] += units
            buckets[offset]["dev_count"] += 1
            buckets[offset]["devs"].append(dev)

    agg = []
    for offset in sorted(buckets):
        b = buckets[offset]
        if b["dev_count"] < MIN_DEVS:
            continue
        per_unit = round(b["total_cost"] / b["total_units"], 0) if b["total_units"] else 0
        agg.append({
            "year_since_conv": offset,
            "dev_count": b["dev_count"],
            "total_cost_dollars": round(b["total_cost"], 0),
            "total_units": b["total_units"],
            "cost_per_unit": per_unit,
        })
    return agg


def build_permits_by_year(records, units_by_dev):
    """Calendar-year totals across all PACT devs."""
    year_data = defaultdict(lambda: {"total_cost": 0.0, "total_units": set(), "devs": set()})
    dev_conv = {}
    for r in records:
        dev = r["development_name"]
        conv = r.get("conversion_date", "")
        yr = r.get("filing_date", "")[:4]
        if not yr or yr < "2016":
            continue
        year_data[yr]["total_cost"] += _cost(r)
        year_data[yr]["devs"].add(dev)
        dev_conv[dev] = conv

    result = []
    # For each year, count units of devs active in that year
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)
    units_by_name = {
        feat["properties"]["development_name"]: feat["properties"].get("total_units", 0)
        for feat in gj["features"]
    }
    for yr in sorted(year_data):
        devs = list(year_data[yr]["devs"])
        total_units = sum(units_by_name.get(d, 0) for d in devs)
        total_cost = round(year_data[yr]["total_cost"], 0)
        result.append({
            "year": yr,
            "dev_count": len(devs),
            "total_cost_dollars": total_cost,
            "total_units": total_units,
            "cost_per_unit": round(total_cost / total_units, 0) if total_units else 0,
        })
    return result


def build_per_dev(records):
    """Per-development permit data for GeoJSON enrichment."""
    dev_data = defaultdict(lambda: {
        "total_cost": 0.0, "filing_count": 0,
        "by_year": defaultdict(lambda: {"cost": 0.0, "count": 0}),
        "job_types": defaultdict(float),
    })
    for r in records:
        dev = r["development_name"]
        cost = _cost(r)
        yr = r.get("filing_date", "")[:4]
        jt = r.get("job_type", "Unknown") or "Unknown"
        dev_data[dev]["total_cost"] += cost
        dev_data[dev]["filing_count"] += 1
        if yr:
            dev_data[dev]["by_year"][yr]["cost"] += cost
            dev_data[dev]["by_year"][yr]["count"] += 1
        dev_data[dev]["job_types"][jt] += cost

    result = {}
    for dev, d in dev_data.items():
        by_year = {yr: {"cost": round(v["cost"]), "count": v["count"]}
                   for yr, v in sorted(d["by_year"].items())}
        top_types = sorted(
            [{"type": jt, "cost": round(c)} for jt, c in d["job_types"].items()],
            key=lambda x: -x["cost"]
        )[:5]
        result[dev] = {
            "total_cost": round(d["total_cost"]),
            "filing_count": d["filing_count"],
            "by_year": by_year,
            "top_types": top_types,
        }
    return result


def update_geojson(per_dev):
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)
    updated = 0
    for feat in gj["features"]:
        dev = feat["properties"]["development_name"]
        if dev in per_dev:
            feat["properties"]["permit_data"] = per_dev[dev]
            updated += 1
        elif feat["properties"].get("permit_data"):
            del feat["properties"]["permit_data"]
    with open(GEOJSON_PATH, "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    print(f"Updated {updated} features in GeoJSON")


if __name__ == "__main__":
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)
    units_by_dev = {
        feat["properties"]["development_name"]: feat["properties"].get("total_units", 0)
        for feat in gj["features"]
    }

    records = fetch_all_pact_permits()
    write_csv(records)

    agg = build_permits_agg(records, units_by_dev)
    with open(OUT_AGG, "w") as f:
        json.dump(agg, f, indent=2)
    print(f"Wrote {OUT_AGG} ({len(agg)} year buckets)")

    by_year = build_permits_by_year(records, units_by_dev)
    with open(OUT_BY_YEAR, "w") as f:
        json.dump(by_year, f, indent=2)
    print(f"Wrote {OUT_BY_YEAR} ({len(by_year)} years)")

    per_dev = build_per_dev(records)
    update_geojson(per_dev)
    print("Done.")
