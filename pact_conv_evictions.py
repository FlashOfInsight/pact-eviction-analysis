"""
pact_conv_evictions.py — Eviction rate before and after PACT conversion

Fetches all residential marshal evictions (2017–present) for PACT BBLs,
computes execution rate per development by year offset relative to each
development's conversion date, and outputs conv_evictions.json for the
before/after dashboard chart.

Usage:
  python pact_conv_evictions.py              # use cache; skip API if fresh
  python pact_conv_evictions.py --refresh    # re-fetch from marshal API
"""

import csv, json, re, sys, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

HERE      = Path(__file__).parent
SITE_DATA = HERE.parent / "laeo-net" / "public" / "data" / "nycha" / "data"
MASTER    = HERE / "pact_bbl_master.csv"
CACHE     = HERE / "pact_executions_conv.csv"
OUT_JSON  = SITE_DATA / "conv_evictions.json"

MARSHAL_URL = "https://data.cityofnewyork.us/resource/6z8x-wfk4.json"
PAGE        = 50_000
DATA_FLOOR  = 2017


# ── Helpers ────────────────────────────────────────────────────────────────────

def clean_bbl(raw):
    s = re.sub(r"[^\d]", "", str(raw or ""))
    return s[:10] if len(s) >= 10 else ""


def load_master():
    """Returns bbl_set, bbl_to_dev dict, and dev_info dict."""
    bbl_to_dev = {}
    dev_info   = {}
    with open(MASTER) as f:
        for row in csv.DictReader(f):
            dev  = row["development_name"].strip()
            date = row.get("conversion_date", "").strip()
            if not date or date in ("", "nan"):
                continue
            try:
                units = int(float(row.get("pact_ref_units") or 0))
            except (ValueError, TypeError):
                units = 0
            if not units:
                continue
            bbls = [b.strip() for b in str(row.get("bbls", "")).split("|") if b.strip()]
            if not bbls:
                continue
            dev_info[dev] = {
                "conv_year": int(date[:4]),
                "units":     units,
                "status":    row.get("status_normalized", "").strip(),
            }
            for bbl in bbls:
                bbl_to_dev[bbl] = dev
    return set(bbl_to_dev), bbl_to_dev, dev_info


# ── Fetch / cache ──────────────────────────────────────────────────────────────

def fetch_marshal(bbl_set):
    """Pull all 2017+ residential evictions; filter to PACT BBLs."""
    records, offset = [], 0
    while True:
        params = {
            "$where":  "executed_date >= '2017-01-01' AND "
                       "(residential_commercial_ind = 'Residential' OR "
                       " residential_commercial_ind = 'R')",
            "$select": "bbl,executed_date",
            "$limit":  PAGE,
            "$offset": offset,
            "$order":  "executed_date",
        }
        resp  = requests.get(MARSHAL_URL, params=params, timeout=60)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for rec in batch:
            bbl  = clean_bbl(rec.get("bbl"))
            date = str(rec.get("executed_date") or "")[:10]
            if bbl and date and bbl in bbl_set:
                records.append({"bbl": bbl, "executed_date": date})
        print(f"  scanned {offset + len(batch):,} records, {len(records):,} PACT hits",
              end="\r", flush=True)
        if len(batch) < PAGE:
            break
        offset += PAGE
        time.sleep(0.1)
    print()
    return records


def save_cache(records):
    with open(CACHE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["bbl", "executed_date"])
        w.writeheader()
        w.writerows(records)


def load_cache(bbl_set):
    records = []
    with open(CACHE) as f:
        for row in csv.DictReader(f):
            bbl  = clean_bbl(row.get("bbl"))
            date = str(row.get("executed_date") or "")[:10]
            if bbl and date and bbl in bbl_set:
                records.append({"bbl": bbl, "executed_date": date})
    return records


# ── Aggregation ────────────────────────────────────────────────────────────────

def aggregate(records, bbl_to_dev, dev_info):
    today_year = datetime.now().year

    # Execution counts per (dev, exec_year)
    counts = defaultdict(lambda: defaultdict(int))
    for rec in records:
        dev = bbl_to_dev.get(rec["bbl"])
        if not dev or dev not in dev_info:
            continue
        try:
            counts[dev][int(rec["executed_date"][:4])] += 1
        except ValueError:
            pass

    # Per-dev data points
    all_offsets = set()
    by_dev = {}
    for dev, info in sorted(dev_info.items()):
        conv_year = info["conv_year"]
        units     = info["units"]
        min_off   = DATA_FLOOR - conv_year
        max_off   = today_year - conv_year
        points    = []
        for o in range(min_off, max_off + 1):
            n    = counts[dev].get(conv_year + o, 0)
            rate = round(n / units * 1000, 3)
            points.append({"offset": o, "executions": int(n), "rate": rate})
            all_offsets.add(o)
        by_dev[dev] = {
            "conv_year": conv_year,
            "units":     units,
            "status":    info["status"],
            "data":      points,
        }

    # Pooled rates by offset
    pooled = []
    for o in sorted(all_offsets):
        total_exec = total_units = n_devs = 0
        for dev, info in dev_info.items():
            min_off = DATA_FLOOR - info["conv_year"]
            max_off = today_year - info["conv_year"]
            if min_off <= o <= max_off:
                total_exec  += counts[dev].get(info["conv_year"] + o, 0)
                total_units += info["units"]
                n_devs      += 1
        pooled.append({
            "offset":     o,
            "rate":       round(total_exec / total_units * 1000, 3) if total_units else None,
            "executions": int(total_exec),
            "units":      int(total_units),
            "n_devs":     n_devs,
        })

    return {"pooled": pooled, "by_dev": by_dev}


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    refresh = "--refresh" in sys.argv

    print("Loading PACT BBL master…")
    bbl_set, bbl_to_dev, dev_info = load_master()
    print(f"  {len(dev_info)} developments, {len(bbl_set)} BBLs")

    if refresh or not CACHE.exists():
        print("Fetching 2017+ residential evictions from marshal API…")
        records = fetch_marshal(bbl_set)
        save_cache(records)
        print(f"  {len(records):,} PACT records cached → {CACHE.name}")
    else:
        print(f"Loading cache: {CACHE.name}…")
        records = load_cache(bbl_set)
        print(f"  {len(records):,} records")

    print("Aggregating by development and year offset…")
    data = aggregate(records, bbl_to_dev, dev_info)

    SITE_DATA.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, indent=2))

    offsets = [p["offset"] for p in data["pooled"]]
    print(f"  {len(data['by_dev'])} developments, offsets {min(offsets)} to {max(offsets)}")
    print(f"  Wrote {OUT_JSON}")
    print()
    print("Pooled rates:")
    for p in data["pooled"]:
        lbl = f"+{p['offset']}" if p["offset"] > 0 else str(p["offset"])
        print(f"  {lbl:>4}  {str(p['rate']):>6}/1k  n={p['n_devs']:2d}  exec={p['executions']}")
