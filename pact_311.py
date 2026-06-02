"""
Fetch HPD 311 complaints for all PACT BBLs (post-conversion only).

Outputs
-------
  pact_311.csv                        raw complaint records with dev metadata
  laeo-net/public/data/nycha/data/complaints_agg.json
      years-since-conversion aggregate across all PACT developments
  laeo-net/public/data/nycha/data/developments.geojson  (enriched in-place)
      adds complaint_data property to each PACT feature
"""

import json, re, requests, time
import pandas as pd
from pathlib import Path
from datetime import datetime, date

ANALYSIS_DIR = Path(__file__).parent
SITE_DATA    = Path(__file__).parent.parent / "laeo-net" / "public" / "data" / "nycha" / "data"

API_311  = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
API_DEV  = "https://data.cityofnewyork.us/resource/evjd-dqpz.json?$limit=400"
EXCL_CSV = ANALYSIS_DIR / "pact_control_exclusions.csv"
MASTER   = ANALYSIS_DIR / "pact_bbl_master.csv"

# 311 dataset begins 2020-01-01
DATASET_START = date(2020, 1, 1)


# ── helpers ───────────────────────────────────────────────────────────────────

def fetch_conv_dates():
    """Return dict: upper(dev_name) → 'YYYY-MM-DD' from NYCHA dev API + exclusions CSV."""
    dev_df = pd.DataFrame(requests.get(API_DEV, timeout=30).json())
    lu = {}
    if "rad_transferred_date" in dev_df.columns:
        for _, r in dev_df.iterrows():
            raw = str(r.get("rad_transferred_date", "")).strip()
            if raw and raw != "nan":
                lu[str(r["development"]).strip().upper()] = raw[:10]
    excl = pd.read_csv(EXCL_CSV, dtype=str)
    for _, r in excl.iterrows():
        raw = str(r.get("conversion_date", "")).strip()
        if raw and raw != "nan":
            lu[str(r.get("bbl", "")).strip()] = raw   # keyed by BBL for exclusions
    return lu


def load_bbl_map(conv_lu):
    """
    Returns list of dicts:
      {dev, bbls:[str], conv_date:'YYYY-MM-DD'|None, units:int}
    Only developments with at least one BBL and a known conversion date are included.
    """
    master = pd.read_csv(MASTER, dtype=str)
    rows = []
    for _, r in master.iterrows():
        dev   = r["development_name"].strip()
        bbls  = [b.strip() for b in str(r.get("bbls", "")).split("|") if b.strip() and len(b.strip()) == 10]
        units = int(r["pact_ref_units"]) if str(r.get("pact_ref_units", "")).strip().isdigit() else 0
        key   = dev.upper()
        conv  = conv_lu.get(key)
        if not conv:
            print(f"  (no conv date) {dev}")
            continue
        if not bbls:
            print(f"  (no BBLs) {dev}")
            continue
        rows.append({"dev": dev, "bbls": bbls, "conv_date": conv, "units": units})
    return rows


# ── 311 fetch ─────────────────────────────────────────────────────────────────

def fetch_311_batch(bbls, after_date="2020-01-01", retries=3):
    """Fetch all HPD complaints for a list of BBLs after after_date, paginating."""
    bbl_clause = ",".join(f"'{b}'" for b in bbls)
    base = (
        f"{API_311}?$where=agency='HPD' AND bbl in({bbl_clause})"
        f" AND created_date >= '{after_date}'"
        f"&$select=bbl,created_date,complaint_type,descriptor,status"
        f"&$limit=50000&$offset="
    )
    all_rows = []
    offset = 0
    while True:
        for attempt in range(retries):
            try:
                r = requests.get(base + str(offset), timeout=45).json()
                break
            except Exception as e:
                if attempt < retries - 1:
                    print(f"    retry {attempt+1} at offset {offset}: {e}")
                    time.sleep(3)
                else:
                    print(f"    ERROR after {retries} retries at offset {offset}: {e}")
                    return all_rows
        if not r:
            break
        all_rows.extend(r)
        if len(r) < 50000:
            break
        offset += 50000
        time.sleep(0.1)
    return all_rows


def fetch_all_pact_311(dev_rows):
    """
    Fetch 311 for all PACT BBLs in batches of 30.
    Returns DataFrame with dev metadata attached and pre-conversion rows dropped.
    """
    # Build flat bbl→dev map for routing
    bbl_dev = {}
    for d in dev_rows:
        for bbl in d["bbls"]:
            bbl_dev[bbl] = d

    all_bbls = list(bbl_dev.keys())
    print(f"  Fetching 311 for {len(all_bbls)} PACT BBLs in batches of 30…")

    all_records = []
    batch_size = 30
    for i in range(0, len(all_bbls), batch_size):
        batch = all_bbls[i:i + batch_size]
        print(f"    batch {i//batch_size + 1}/{-(-len(all_bbls)//batch_size)}: BBLs {i+1}–{min(i+batch_size, len(all_bbls))}", end="", flush=True)
        rows = fetch_311_batch(batch)
        print(f"  → {len(rows)} records")
        all_records.extend(rows)
        time.sleep(0.15)

    if not all_records:
        return pd.DataFrame()

    df = pd.DataFrame(all_records)
    df["created_date"] = pd.to_datetime(df["created_date"], errors="coerce")
    df = df.dropna(subset=["created_date", "bbl"])

    # Attach dev info
    df["development_name"] = df["bbl"].map(lambda b: bbl_dev[b]["dev"] if b in bbl_dev else None)
    df["conv_date"]        = df["bbl"].map(lambda b: bbl_dev[b]["conv_date"] if b in bbl_dev else None)
    df["units"]            = df["bbl"].map(lambda b: bbl_dev[b]["units"] if b in bbl_dev else 0)
    df = df.dropna(subset=["development_name", "conv_date"])

    # Drop pre-conversion complaints
    df["conv_dt"] = pd.to_datetime(df["conv_date"], errors="coerce")
    pre = (df["created_date"] < df["conv_dt"]).sum()
    df = df[df["created_date"] >= df["conv_dt"]].copy()
    print(f"  Dropped {pre} pre-conversion records. Remaining: {len(df)}")

    df["year"]      = df["created_date"].dt.year
    df["month_str"] = df["created_date"].dt.strftime("%Y-%m")
    df["conv_year"] = df["conv_dt"].dt.year
    df["years_since_conv"] = df["year"] - df["conv_year"]

    return df


# ── aggregations ──────────────────────────────────────────────────────────────

def build_complaints_agg(df, dev_rows):
    """
    Years-since-conversion aggregate: for each offset Y (0,1,2,...),
    sum complaints across all devs that have data for that offset,
    divided by their combined units, × 1000.
    Only include offset Y if ≥ 3 developments contribute.
    """
    if df.empty:
        return []

    # Per-dev, per-year-offset
    dev_unit_map = {d["dev"]: d["units"] for d in dev_rows}
    grp = df.groupby(["development_name", "years_since_conv"]).size().reset_index(name="complaints")

    out = []
    for offset, sub in grp.groupby("years_since_conv"):
        if offset < 0 or offset > 9:
            continue
        total_complaints = int(sub["complaints"].sum())
        total_units      = sum(dev_unit_map.get(d, 0) for d in sub["development_name"])
        dev_count        = len(sub)
        if dev_count < 3 or total_units == 0:
            continue
        out.append({
            "year_since_conv": int(offset),
            "dev_count":       dev_count,
            "total_complaints": total_complaints,
            "total_units":     total_units,
            "per_1k_units":    round(total_complaints / total_units * 1000, 1),
        })

    return sorted(out, key=lambda x: x["year_since_conv"])


def build_per_dev(df, dev_rows):
    """
    Per-development complaint data dict keyed by dev name:
      {total, conv_year, by_year:{year:{complaints,per_1k}}, monthly:{YYYY-MM:N},
       top_types:[{type,count}]}
    """
    if df.empty:
        return {}

    dev_unit_map  = {d["dev"]: d["units"] for d in dev_rows}
    dev_conv_map  = {d["dev"]: d["conv_date"][:4] for d in dev_rows}
    result = {}

    for dev, grp in df.groupby("development_name"):
        units = dev_unit_map.get(dev, 0)
        conv_year = int(dev_conv_map.get(dev, 0))

        by_year = {}
        for yr, ygrp in grp.groupby("year"):
            cnt  = len(ygrp)
            rate = round(cnt / units * 1000, 1) if units > 0 else None
            by_year[str(yr)] = {"complaints": cnt, "per_1k": rate}

        monthly = {}
        for mo, mgrp in grp.groupby("month_str"):
            monthly[mo] = int(len(mgrp))

        top_types = (
            grp["complaint_type"].value_counts()
            .head(5)
            .reset_index()
            .rename(columns={"complaint_type": "type", "count": "count"})
            .to_dict("records")
        )

        result[dev] = {
            "total":     len(grp),
            "conv_year": conv_year,
            "by_year":   by_year,
            "monthly":   monthly,
            "top_types": top_types,
        }

    return result


# ── GeoJSON update ────────────────────────────────────────────────────────────

def update_geojson(per_dev):
    """Inject complaint_data into each PACT feature in developments.geojson."""
    path = SITE_DATA / "developments.geojson"
    fc   = json.loads(path.read_text())
    updated = 0
    for feat in fc["features"]:
        if feat["properties"].get("type") != "pact":
            continue
        dev = feat["properties"].get("development_name", "")
        if dev in per_dev:
            feat["properties"]["complaint_data"] = per_dev[dev]
            updated += 1
    path.write_text(json.dumps(fc))
    print(f"  Updated {updated} PACT features in developments.geojson")


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    SITE_DATA.mkdir(parents=True, exist_ok=True)

    print("Fetching conversion dates…")
    conv_lu  = fetch_conv_dates()

    print("Loading PACT BBL master…")
    dev_rows = load_bbl_map(conv_lu)
    print(f"  {len(dev_rows)} developments with BBLs and conversion dates")

    print("Fetching HPD 311 complaints…")
    df = fetch_all_pact_311(dev_rows)
    if not df.empty:
        out_cols = ["bbl", "development_name", "conv_date", "units",
                    "created_date", "complaint_type", "descriptor",
                    "status", "year", "month_str", "years_since_conv"]
        df[out_cols].to_csv(ANALYSIS_DIR / "pact_311.csv", index=False)
        print(f"  Saved pact_311.csv  ({len(df)} records)")
    else:
        print("  No records fetched.")

    print("Building complaints_agg.json…")
    agg = build_complaints_agg(df, dev_rows)
    (SITE_DATA / "complaints_agg.json").write_text(json.dumps(agg, indent=2))
    print(f"  {len(agg)} year-offset buckets written")

    print("Building per-dev complaint data…")
    per_dev = build_per_dev(df, dev_rows)

    print("Updating developments.geojson…")
    update_geojson(per_dev)

    print("Done.")
