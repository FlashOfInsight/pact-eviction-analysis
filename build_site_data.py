"""
Generate static data files for the laeo.net NYCHA eviction project page.

Outputs:
  laeo-net/public/data/nycha/data/aggregate_rates.json
  laeo-net/public/data/nycha/data/developments.geojson
"""

import json, re, requests, time
import pandas as pd
from pathlib import Path

ANALYSIS_DIR = Path(__file__).parent
SITE_DATA    = Path(__file__).parent.parent / "laeo-net" / "public" / "data" / "nycha" / "data"

def clean_bbl(raw):
    s = re.sub(r"[^\d]", "", str(raw) if raw else "")
    s = s[:10]
    return s if len(s) == 10 else None


# ── 1. Aggregate rates JSON ───────────────────────────────────────────────────

def build_aggregate_rates():
    agg = pd.read_csv(ANALYSIS_DIR / "aggregate_execution_rates.csv")
    out = []
    for _, r in agg.iterrows():
        year = str(int(r["year"])) if r["year"] != 2026 else "2026 (YTD)"
        out.append({
            "Year":                    year,
            "PACT devs":               int(r["pact_devs"]),
            "PACT units":              f"{int(r['pact_units']):,}",
            "PACT exec":               int(r["pact_exec"]),
            "PACT /1k units":          round(float(r["pact_per_1k"]), 2),
            "PACT Complete /1k units": round(float(r["complete_per_1k"]), 2),
            "Non-PACT units":          f"{int(r['ctrl_units']):,}",
            "Non-PACT exec":           int(r["ctrl_exec"]),
            "Non-PACT /1k units":      round(float(r["ctrl_per_1k"]), 2),
            "Ratio (PACT:non-PACT)":   round(float(r["ratio_complete_vs_ctrl"]), 1)
                                       if pd.notna(r["ratio_complete_vs_ctrl"]) else None,
        })
    return out


# ── 2. PLUTO centroid lookup ──────────────────────────────────────────────────

_pluto_cache = {}

def bbl_centroid(bbl):
    """Return (lat, lon) centroid for a BBL from PLUTO, or None."""
    if bbl in _pluto_cache:
        return _pluto_cache[bbl]
    url = (
        f"https://data.cityofnewyork.us/resource/64uk-42ks.json"
        f"?bbl={bbl}&$select=latitude,longitude&$limit=1"
    )
    try:
        r = requests.get(url, timeout=10).json()
        if r and r[0].get("latitude"):
            result = (float(r[0]["latitude"]), float(r[0]["longitude"]))
        else:
            result = None
    except Exception:
        result = None
    _pluto_cache[bbl] = result
    time.sleep(0.05)
    return result


def dev_centroid(bbls):
    """Average centroid across a list of BBLs."""
    lats, lons = [], []
    for bbl in bbls:
        pt = bbl_centroid(bbl)
        if pt:
            lats.append(pt[0])
            lons.append(pt[1])
    if not lats:
        return None, None
    return sum(lats)/len(lats), sum(lons)/len(lons)


# ── 3. Developments GeoJSON ───────────────────────────────────────────────────

def build_geojson(agg_rates):
    summary   = pd.read_csv(ANALYSIS_DIR / "summary_by_development.csv")
    master    = pd.read_csv(ANALYSIS_DIR / "pact_bbl_master.csv", dtype=str)
    excl      = pd.read_csv(ANALYSIS_DIR / "pact_control_exclusions.csv", dtype=str)
    pact_exec = pd.read_csv(ANALYSIS_DIR / "pact_executions.csv")
    pact_exec["executed_date"] = pd.to_datetime(pact_exec["executed_date"], errors="coerce")

    # BBL list per dev
    bbl_map = {}
    for _, r in master.iterrows():
        dev = r["development_name"].strip()
        bbl_map[dev] = [b.strip() for b in str(r["bbls"]).split("|") if b.strip()]

    # Conversion dates: rad_transferred_date from NYCHA dev dataset (authoritative)
    print("Fetching NYCHA dev dataset for conversion dates…")
    dev_url = "https://data.cityofnewyork.us/resource/evjd-dqpz.json?$limit=400"
    dev_df  = pd.DataFrame(requests.get(dev_url, timeout=30).json())
    rad_lu  = {}  # upper dev name → date string "YYYY-MM-DD"
    if "rad_transferred_date" in dev_df.columns:
        for _, r in dev_df.iterrows():
            raw = str(r.get("rad_transferred_date", "")).strip()
            if raw and raw != "nan":
                rad_lu[str(r["development"]).strip().upper()] = raw[:10]

    # Also overlay pact_control_exclusions.csv dates (manually verified)
    excl_dates = {}  # dev name → date string
    for _, r in excl.iterrows():
        dev = str(r.get("development_name", "")).strip()
        raw = str(r.get("conversion_date", "")).strip()
        if dev and raw and raw != "nan":
            excl_dates[dev.upper()] = raw

    def get_conv_date(dev_name):
        key = dev_name.strip().upper()
        return excl_dates.get(key) or rad_lu.get(key)

    # Per-dev execution type breakdown (all years combined)
    exec_by_dev = {}
    monthly_by_dev = {}
    if "development_name" in pact_exec.columns:
        for dev, grp in pact_exec.groupby("development_name"):
            nonpay = holdover = 0
            if "cause_of_action" in grp.columns:
                coa = grp["cause_of_action"].fillna("").str.upper()
                nonpay   = int(coa.str.contains(r"NON.?PAY|NONPAY", na=False).sum())
                holdover = int(coa.str.contains(r"HOLDOVER|NUISANCE", na=False).sum())
            elif "classification" in grp.columns:
                cl = grp["classification"].fillna("").str.upper()
                nonpay   = int(cl.str.contains(r"NON.?PAY", na=False).sum())
                holdover = int(cl.str.contains(r"HOLDOVER", na=False).sum())
            exec_by_dev[dev] = {"total": len(grp), "nonpayment": nonpay, "holdover": holdover}

            # Monthly counts: {"2022-03": 2, ...}
            dated = grp.dropna(subset=["executed_date"])
            monthly = (
                dated.set_index("executed_date")
                .resample("ME")
                .size()
                .rename_axis("month")
                .reset_index(name="count")
            )
            monthly["month_str"] = monthly["month"].dt.strftime("%Y-%m")
            monthly_by_dev[dev] = {
                r["month_str"]: int(r["count"])
                for _, r in monthly.iterrows()
                if r["count"] > 0
            }

    # PACT features
    pact_devs = summary[summary["group"].str.startswith("PACT")]["development_name"].unique()
    features = []

    for dev in pact_devs:
        rows = summary[summary["development_name"] == dev]
        if rows.empty:
            continue

        meta = rows.iloc[0]
        bbls = bbl_map.get(dev, [])
        lat, lon = dev_centroid(bbls)
        if lat is None:
            print(f"  No coords for PACT: {dev}")
            continue

        by_year = {}
        for _, yr in rows.iterrows():
            y = str(int(yr["year"]))
            executions = int(yr["executions"])
            units = float(yr["total_units"]) if pd.notna(yr["total_units"]) else None
            rate  = round(executions / units * 1000, 2) if units and units > 0 else None
            by_year[y] = {"executions": executions, "rate_per_1k": rate}

        exec_data = exec_by_dev.get(dev, {})
        has_data  = exec_data.get("total", 0) > 0
        conv_date = get_conv_date(dev)

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            "properties": {
                "type":                  "pact",
                "development_name":      dev,
                "status":                str(meta.get("status", "")),
                "property_manager":      str(meta.get("property_manager", "")),
                "total_units":           int(meta["total_units"]) if pd.notna(meta.get("total_units")) else None,
                "conversion_date":       conv_date,
                "has_data":              has_data,
                "by_year":               by_year,
                "monthly_executions":    monthly_by_dev.get(dev, {}),
                "total_executions":      exec_data.get("total", 0),
                "nonpayment_executions": exec_data.get("nonpayment", 0),
                "holdover_executions":   exec_data.get("holdover", 0),
            }
        })

    # Non-PACT NYCHA features — one point per development from addr dataset
    print("Fetching NYCHA address data for non-PACT points…")
    addr_url = "https://data.cityofnewyork.us/resource/3ub5-4ph8.json?$limit=3000"
    addr_rows = requests.get(addr_url, timeout=30).json()
    addr_df   = pd.DataFrame(addr_rows)

    pact_names_up = {d.upper() for d in pact_devs}

    if "development" in addr_df.columns and "latitude" in addr_df.columns:
        addr_df["lat"] = pd.to_numeric(addr_df["latitude"], errors="coerce")
        addr_df["lon"] = pd.to_numeric(addr_df["longitude"], errors="coerce")
        # One centroid per non-PACT development
        ctrl_devs = addr_df[~addr_df["development"].str.upper().isin(pact_names_up)]
        centroids = ctrl_devs.groupby("development").agg(lat=("lat","mean"), lon=("lon","mean")).reset_index()

        # Unit counts from NYCHA dev dataset
        dev_url = "https://data.cityofnewyork.us/resource/evjd-dqpz.json?$limit=400"
        dev_rows = requests.get(dev_url, timeout=30).json()
        dev_df   = pd.DataFrame(dev_rows)
        unit_col = "total_number_of_apartments"
        dev_df[unit_col] = pd.to_numeric(dev_df[unit_col].astype(str).str.replace(",",""), errors="coerce")
        unit_lu  = dict(zip(dev_df["development"].str.upper(), dev_df[unit_col]))

        for _, r in centroids.iterrows():
            if pd.isna(r["lat"]) or pd.isna(r["lon"]):
                continue
            dev_up = str(r["development"]).upper()
            units  = unit_lu.get(dev_up)
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(float(r["lon"]),6), round(float(r["lat"]),6)]},
                "properties": {
                    "type":             "non_pact",
                    "development_name": str(r["development"]),
                    "total_units":      int(units) if units and pd.notna(units) else None,
                    "has_data":         False,
                    "by_year":          {},
                }
            })

    return {"type": "FeatureCollection", "features": features}


# ── 4. Control monthly timeseries ─────────────────────────────────────────────

def build_control_monthly():
    ctrl = pd.read_csv(ANALYSIS_DIR / "control_executions.csv")
    ctrl["executed_date"] = pd.to_datetime(ctrl["executed_date"], errors="coerce")

    # Total non-PACT units — read from summary (all Non-PACT NYCHA rows, any year)
    summary = pd.read_csv(ANALYSIS_DIR / "summary_by_development.csv")
    ctrl_units = summary[summary["group"] == "Non-PACT NYCHA"]["total_units"].sum() / \
                 summary[summary["group"] == "Non-PACT NYCHA"]["year"].nunique()
    ctrl_units = round(float(ctrl_units))

    monthly = (
        ctrl.dropna(subset=["executed_date"])
        .set_index("executed_date")
        .resample("ME")
        .size()
        .rename_axis("month")
        .reset_index(name="count")
    )
    monthly["month_str"] = monthly["month"].dt.strftime("%Y-%m")
    monthly["rate_per_1k"] = (monthly["count"] / ctrl_units * 1000).round(3)

    return {
        "units": ctrl_units,
        "monthly": {
            r["month_str"]: {"executions": int(r["count"]), "rate_per_1k": float(r["rate_per_1k"])}
            for _, r in monthly.iterrows()
        }
    }


# ── 5. HUD occupancy timeseries ───────────────────────────────────────────────

def build_hud_occupancy():
    summary_path = ANALYSIS_DIR / "hud_summary.json"
    if not summary_path.exists():
        print("  hud_summary.json not found; skipping hud_occupancy.json")
        return None
    with open(summary_path) as f:
        summary = json.load(f)
    non_pact = summary.get("non_pact_nycha", {})
    return [
        {
            "year":           yr,
            "pct_occupied":   round(d["pct_occupied_wavg"], 1),
            "n_developments": d["n_developments"],
            "total_units":    d["total_units"],
        }
        for yr in sorted(non_pact.keys())
        for d in [non_pact[yr]]
    ]


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    SITE_DATA.mkdir(parents=True, exist_ok=True)

    print("Building aggregate_rates.json…")
    rates = build_aggregate_rates()
    (SITE_DATA / "aggregate_rates.json").write_text(json.dumps(rates, indent=2))
    print(f"  {len(rates)} rows written")

    print("Building control_monthly.json…")
    ctrl_monthly = build_control_monthly()
    (SITE_DATA / "control_monthly.json").write_text(json.dumps(ctrl_monthly, indent=2))
    print(f"  {len(ctrl_monthly['monthly'])} months written")

    print("Building developments.geojson…")
    fc = build_geojson(rates)
    pact_n = sum(1 for f in fc["features"] if f["properties"]["type"]=="pact")
    ctrl_n = sum(1 for f in fc["features"] if f["properties"]["type"]=="non_pact")
    (SITE_DATA / "developments.geojson").write_text(json.dumps(fc))
    print(f"  {pact_n} PACT + {ctrl_n} non-PACT features written")

    print("Building hud_occupancy.json…")
    occ = build_hud_occupancy()
    if occ:
        (SITE_DATA / "hud_occupancy.json").write_text(json.dumps(occ, indent=2))
        print(f"  {len(occ)} years written")

    print("Done.")
