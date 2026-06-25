#!/usr/bin/env python3
"""
NYCHA PACT Eviction Analysis
Compares eviction filings and executions at PACT-converted developments
vs. non-PACT NYCHA sites, 2022–2026.
"""

import re
import time
import logging
import requests
import datetime
import unicodedata
from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd
import pdfplumber
from rapidfuzz import fuzz, process
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

OUT = Path(__file__).parent
TODAY = datetime.date.today().isoformat()
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "pact-eviction-analysis/1.0 (research)"


# ─── helpers ──────────────────────────────────────────────────────────────────

def get_json(url, params=None, retries=4, backoff=2):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=90)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                raise
            log.warning("Retry %d for %s: %s", attempt + 1, url, e)
            time.sleep(backoff ** attempt)


def socrata_paginate(url, where=None, limit=50000, order_col=None):
    """Fetch all rows from a Socrata JSON endpoint."""
    rows = []
    offset = 0
    params = {"$limit": limit}
    if order_col:
        params["$order"] = order_col
    if where:
        params["$where"] = where
    while True:
        params["$offset"] = offset
        batch = get_json(url, params=params)
        if not batch:
            break
        rows.extend(batch)
        log.info("  …fetched %d rows (total %d)", len(batch), len(rows))
        if len(batch) < limit:
            break
        offset += limit
    return rows


def normalize_addr(s):
    """Lower-case, collapse whitespace, expand abbreviations for fuzzy matching."""
    if pd.isna(s):
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^\w\s]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    abbr = [
        (r"\bst\b", "street"), (r"\bave\b", "avenue"), (r"\bblvd\b", "boulevard"),
        (r"\brd\b", "road"), (r"\bdr\b", "drive"), (r"\bpl\b", "place"),
        (r"\bln\b", "lane"), (r"\bct\b", "court"), (r"\bpkwy\b", "parkway"),
        (r"\bexpy\b", "expressway"), (r"\bn\b", "north"), (r"\bs\b", "south"),
        (r"\be\b", "east"), (r"\bw\b", "west"),
    ]
    for pat, rep in abbr:
        s = re.sub(pat, rep, s)
    return s


def clean_bbl(raw):
    """Normalize a BBL value to exactly 10 digits or return None."""
    s = re.sub(r"[^\d]", "", str(raw) if raw else "")
    # PLUTO returns "3014950013.00000000" style — strip decimal
    s = s[:10]
    return s if len(s) == 10 else None


# ─── 1. NYCHA PACT PDF ────────────────────────────────────────────────────────

PACT_PDF_URL = "https://www.nyc.gov/assets/nycha/downloads/pdf/PACT_Dataset.pdf"
PACT_ACTIVE_STATUSES = {"Construction Complete", "Under Construction"}


def fetch_pact_pdf():
    log.info("Downloading PACT Dataset PDF…")
    r = SESSION.get(PACT_PDF_URL, timeout=120)
    r.raise_for_status()
    return r.content


def parse_pact_pdf(pdf_bytes):
    """
    Extract rows from the PACT PDF using pdfplumber table extraction.
    The PDF has a multi-page table with a header repeated on each page.
    """
    log.info("Parsing PACT PDF…")
    all_rows = []
    header = None

    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                first = [str(c).strip() if c else "" for c in table[0]]
                is_header = any(
                    kw in " ".join(first).lower()
                    for kw in ("development", "status", "units", "manager", "conversion")
                )
                if is_header:
                    if header is None:
                        header = first
                    data_rows = table[1:]
                else:
                    data_rows = table

                for row in data_rows:
                    cleaned = [str(c).strip() if c else "" for c in row]
                    if any(cleaned):
                        all_rows.append(cleaned)

    if not all_rows:
        raise ValueError("No rows extracted from PACT PDF — check layout.")

    if header:
        # Normalize header names to snake_case
        norm_header = []
        for h in header:
            h2 = re.sub(r"\s+", "_", h.lower().strip())
            h2 = re.sub(r"[^\w]", "", h2)
            norm_header.append(h2 or f"col{len(norm_header)}")
        n = len(norm_header)
        padded = [r[:n] + [""] * max(0, n - len(r)) for r in all_rows]
        df = pd.DataFrame(padded, columns=norm_header)
    else:
        df = pd.DataFrame(all_rows)

    log.info("  Raw PDF rows: %d  columns: %s", len(df), list(df.columns))
    return df


def clean_pact_df(raw_df):
    """
    Normalize column names and status; filter to active vs. excluded.
    Returns (active_df, excluded_df).
    """
    df = raw_df.copy()
    cols = list(df.columns)

    col_map = {}
    for c in cols:
        lc = c.lower()
        if "development" in lc and "name" in lc:
            col_map["development_name"] = c
        elif "project" in lc and "name" in lc:
            col_map["project_name"] = c
        elif "status" in lc:
            col_map["status"] = c
        elif "unit" in lc:
            col_map["total_units"] = c
        elif "manager" in lc or "operator" in lc:
            col_map["property_manager"] = c
        elif "developer" in lc and "property" not in lc:
            col_map["developers"] = c
        elif "conversion" in lc or "date" in lc:
            col_map["conversion_date"] = c
        elif "borough" in lc or "boro" in lc:
            col_map["borough"] = c

    df = df.rename(columns={v: k for k, v in col_map.items()})

    def norm_status(s):
        s = str(s).strip()
        if re.search(r"complete", s, re.I):
            return "Construction Complete"
        if re.search(r"under.?construction", s, re.I):
            return "Under Construction"
        if re.search(r"planning|engagement", s, re.I):
            return "Planning and Engagement"
        return s or "Unknown"

    if "status" in df.columns:
        df["status_normalized"] = df["status"].apply(norm_status)
    else:
        df["status_normalized"] = "Unknown"

    if "total_units" in df.columns:
        df["total_units"] = pd.to_numeric(
            df["total_units"].astype(str).str.replace(r"[^\d]", "", regex=True),
            errors="coerce"
        )

    # Carry status/manager forward (PDF rows for sub-developments have blank status)
    for col in ("status", "status_normalized", "property_manager", "developers",
                "project_name", "conversion_date"):
        if col in df.columns:
            df[col] = df[col].replace("", pd.NA).ffill()

    # Drop header-repeat rows
    if "development_name" in df.columns:
        df = df[~df["development_name"].str.lower().str.strip().isin(
            {"development name", "development_name", ""}
        )]
        df = df[df["development_name"].notna()]

    active   = df[df["status_normalized"].isin(PACT_ACTIVE_STATUSES)].copy()
    excluded = df[~df["status_normalized"].isin(PACT_ACTIVE_STATUSES)].copy()

    log.info("  Active PACT developments: %d", len(active))
    log.info("  Excluded (Planning/Engagement/Unknown): %d", len(excluded))
    return active, excluded


# ─── 2. NYCHA datasets ────────────────────────────────────────────────────────

NYCHA_ADDR_URL = "https://data.cityofnewyork.us/resource/3ub5-4ph8.json"
NYCHA_DEV_URL  = "https://data.cityofnewyork.us/resource/evjd-dqpz.json"


def fetch_nycha_addresses():
    log.info("Fetching NYCHA residential address dataset…")
    rows = socrata_paginate(NYCHA_ADDR_URL, order_col="development")
    df = pd.DataFrame(rows)
    log.info("  Rows: %d  Columns: %s", len(df), list(df.columns))
    return df


def fetch_nycha_developments():
    log.info("Fetching NYCHA development list…")
    rows = socrata_paginate(NYCHA_DEV_URL, order_col="development")
    df = pd.DataFrame(rows)
    log.info("  Rows: %d  Columns: %s", len(df), list(df.columns))
    return df


def normalize_nycha_addr_df(addr_df):
    """
    Produce a flat DataFrame: development_name, bbl, address, zip, addr_norm.
    The NYCHA address dataset uses borough_block_lot (already 10 digits).
    """
    df = addr_df.copy()
    cols_lc = {c.lower(): c for c in df.columns}

    bbl_col  = cols_lc.get("borough_block_lot") or cols_lc.get("bbl")
    dev_col  = cols_lc.get("development")
    addr_col = cols_lc.get("address")
    zip_col  = cols_lc.get("zip_code") or cols_lc.get("zip") or cols_lc.get("zipcode")

    out = pd.DataFrame()
    out["development_name"] = df[dev_col].astype(str).str.strip() if dev_col else ""
    out["bbl"]              = df[bbl_col].apply(clean_bbl) if bbl_col else None
    out["address"]          = df[addr_col].astype(str).str.strip() if addr_col else ""
    out["zip"]              = df[zip_col].astype(str).str[:5] if zip_col else ""
    out["addr_norm"]        = out["address"].apply(normalize_addr)
    out["dev_norm"]         = out["development_name"].apply(normalize_addr)

    # Mark privately managed
    pm_col = cols_lc.get("privately_managed")
    out["privately_managed"] = df[pm_col].str.upper().eq("YES") if pm_col else False

    return out


# ─── 3. PACT BBL resolution via PLUTO ─────────────────────────────────────────

PLUTO_URL = "https://data.cityofnewyork.us/resource/64uk-42ks.json"
GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"

BORO_CODE = {
    "MANHATTAN": 1, "NEW YORK": 1, "MN": 1,
    "BRONX": 2, "BX": 2,
    "BROOKLYN": 3, "KINGS": 3, "BK": 3,
    "QUEENS": 4, "QN": 4,
    "STATEN ISLAND": 5, "RICHMOND": 5, "SI": 5,
}


PLUTO_BORO_ABBR = {
    1: "MN", 2: "BX", 3: "BK", 4: "QN", 5: "SI",
    "MANHATTAN": "MN", "NEW YORK": "MN", "MN": "MN",
    "BRONX": "BX", "BX": "BX",
    "BROOKLYN": "BK", "KINGS": "BK", "BK": "BK",
    "QUEENS": "QN", "QN": "QN",
    "STATEN ISLAND": "SI", "RICHMOND": "SI", "SI": "SI",
}

# ── Hardcoded PLUTO owner-name patterns discovered through manual research ─────
# Maps owner-name keyword → list of (development_name, borough_filter)
# Used to supplement the automated PLUTO keyword search.
PACT_OWNER_MAP = {
    # Ocean Bay Bayside — Queens
    "OCEAN BAY RAD":             [("OCEAN BAY APARTMENTS (BAYSIDE)", "QN")],
    # Brooklyn scattered-site PACT bundle (Tapscott, Ocean Hill, Belmont-Sutter, Crown Heights)
    "NYC PACT PRESERVATION PARTNERS": [
        ("104-14 TAPSCOTT STREET",  "BK"),
        ("OCEAN HILL APARTMENTS",   "BK"),
        ("BELMONT-SUTTER AREA",     "BK"),
    ],
    # Manhattan scattered-site bundle (Manhattanville, Metro North area)
    # Samuel (City) BBLs are manually overridden below — removed from this sweep
    # because PACT RENAISSANCE COLLABORATIVE owns lots across all of Manhattan,
    # causing the automated search to pull in wrong developments.
    "PACT RENAISSANCE COLLABORATIVE": [
        ("MANHATTANVILLE",          "MN"),
    ],
    # Edenwald, Bronx
    "CSA PRESERVATION PARTNERS":  [("EDENWALD",          "BX")],
    # Sack Wern, Bronx
    "SEWARD HOUSING":              [("SACK WERN",         "BX")],
    # West Brighton I, Staten Island
    "MARKHAM GARDENS":             [("WEST BRIGHTON I",   "SI")],
    # Bay View, Brooklyn
    "BSC HOUSING COMPANY":         [("BAY VIEW",          "BK")],
    # Metro North Plaza, Manhattan
    "METRO NORTH OWNERS":          [("METRO NORTH PLAZA", "MN")],
    "METRO NORTH GARDENS":         [("METRO NORTH PLAZA", "MN")],
    # Boston Secor, Bronx — two contiguous lots total ~534 units (expected 538)
    "BOSTON TREMONT HOUSING":      [("BOSTON SECOR", "BX")],
    # Eagle Ave-E 163rd Street, Bronx
    "1018 E 163RD STREET HOUSING": [("EAGLE AVENUE-EAST 163RD STREET", "BX")],
    # Morris Park Senior Citizens Home, Bronx
    "437 MORRIS PARK":             [("MORRIS PARK SENIOR CITIZENS HOME", "BX")],
    # Franklin Avenue I Conventional, Bronx — ~75 unit building closest to 61
    "1068 FRANKLIN AVE HOUSING":   [("FRANKLIN AVENUE I CONVENTIONAL", "BX")],
}

# ── BBL master loader ─────────────────────────────────────────────────────────
# pact_bbl_master.csv is the authoritative source for BBL overrides.
# Any development with non-empty BBLs in that file overrides automated resolution.
# Developments with no BBLs in the CSV fall through to automated resolution.

def load_bbl_master():
    """
    Load BBL overrides from pact_bbl_master.csv.
    Returns dict: development_name → list[bbl] for rows that have BBLs.
    Rows with empty bbls column are omitted so auto-resolution can still run.
    """
    master_path = OUT / "pact_bbl_master.csv"
    if not master_path.exists():
        log.warning("pact_bbl_master.csv not found — no manual overrides applied")
        return {}
    df = pd.read_csv(master_path, dtype=str)
    result = {}
    for _, row in df.iterrows():
        dev = str(row.get("development_name", "")).strip()
        bbls_raw = str(row.get("bbls", "")).strip()
        if dev and bbls_raw and bbls_raw not in ("", "nan"):
            bbls = [b.strip() for b in bbls_raw.split("|") if b.strip()]
            if bbls:
                result[dev] = bbls
    log.info("Loaded %d BBL overrides from pact_bbl_master.csv", len(result))
    return result


def load_control_exclusions():
    """
    Load pact_control_exclusions.csv.
    Returns dict: bbl → conversion_date (pd.Timestamp or None).
    conversion_date is the date the development transferred to PACT.
      - Before that date: executions at the BBL count as control (NYCHA).
      - On/after that date: executions count as PACT.
      - None means not yet transferred; all executions remain control.
    """
    excl_path = OUT / "pact_control_exclusions.csv"
    if not excl_path.exists():
        return {}
    df = pd.read_csv(excl_path, dtype=str)
    result = {}
    for _, row in df.iterrows():
        bbl = str(row.get("bbl", "")).strip()
        if not bbl or bbl in ("", "nan"):
            continue
        raw_date = str(row.get("conversion_date", "")).strip()
        conv_date = pd.Timestamp(raw_date) if raw_date and raw_date != "nan" else None
        result[bbl] = conv_date
    log.info("Loaded %d control exclusion BBLs from pact_control_exclusions.csv", len(result))
    return result


def pluto_bbls_by_ownername(keyword, borough_code=None, min_units=50):
    """
    Search PLUTO for lots whose owner name contains keyword.
    Optionally restrict to a borough (text name or numeric 1-5 code).
    """
    kw_safe = keyword.upper().replace("'", "''")
    where = f"upper(ownername) LIKE '%{kw_safe}%' AND unitsres >= {min_units}"
    if borough_code:
        boro_abbr = PLUTO_BORO_ABBR.get(borough_code) or PLUTO_BORO_ABBR.get(str(borough_code).upper())
        if boro_abbr:
            where += f" AND borough = '{boro_abbr}'"
    rows = get_json(PLUTO_URL, params={
        "$where": where,
        "$select": "bbl, ownername, address, borough, unitsres",
        "$limit": 500,
    })
    if not rows:
        return []
    return [{"bbl": clean_bbl(r["bbl"]), "ownername": r.get("ownername", ""),
             "address": r.get("address", ""), "borough": r.get("borough", "")}
            for r in rows if clean_bbl(r.get("bbl"))]


def geosearch_bbl(address_text, borough):
    """
    Use NYC Planning Geosearch to convert an address to a BBL.
    Returns BBL string or None.
    """
    try:
        query = f"{address_text}, {borough}, NY"
        r = SESSION.get(GEOSEARCH_URL, params={"text": query, "size": 1}, timeout=15)
        if r.status_code != 200:
            return None
        features = r.json().get("features", [])
        if not features:
            return None
        props = features[0].get("properties", {})
        pad_id = props.get("pad_bbl") or props.get("bbl")
        return clean_bbl(pad_id)
    except Exception:
        return None


def resolve_pact_bbls(pact_active, nycha_dev_df):
    """
    Build a mapping of {development_name → list[bbl]} for PACT developments.

    Strategy (in priority order):
    1. NYCHA address dataset: exact development name match
    2. PLUTO: search ownername for development name keywords
    3. NYC Geosearch: geocode cross-street intersections from NYCHA dev dataset
    """
    # Build cross-street / borough lookup from NYCHA development dataset
    dev_meta = {}
    if nycha_dev_df is not None and len(nycha_dev_df) > 0:
        cols_lc = {c.lower(): c for c in nycha_dev_df.columns}
        dev_col    = cols_lc.get("development")
        boro_col   = cols_lc.get("borough")
        street_a   = cols_lc.get("location_street_a")
        street_b   = cols_lc.get("location_street_b")
        units_col  = cols_lc.get("total_number_of_apartments") or cols_lc.get("number_of_current_apartments")
        for _, row in nycha_dev_df.iterrows():
            name = str(row.get(dev_col, "")).strip().upper() if dev_col else ""
            dev_meta[name] = {
                "borough": str(row.get(boro_col, "")).strip().upper() if boro_col else "",
                "street_a": str(row.get(street_a, "")).strip() if street_a else "",
                "street_b": str(row.get(street_b, "")).strip() if street_b else "",
                "units": row.get(units_col) if units_col else None,
            }

    results = {}  # dev_name → set of BBLs

    # Generic words to skip when building PLUTO keyword
    SKIP_WORDS = {
        "I", "II", "III", "IV", "V", "VI", "VII", "VIII",
        "CITY", "AREA", "SITE", "SITES", "GROUP", "GROUPS",
        "ADDITION", "SECTION", "CONVENTIONAL", "CDA", "SENIOR",
        "HOUSES", "HOUSE", "PLAZA", "PARK", "GARDENS", "HEIGHTS",
        "HOME", "HOMES", "APARTMENTS", "APTS", "AND",
    }

    for _, row in pact_active.iterrows():
        dev_name = str(row.get("development_name", "")).strip()
        if not dev_name:
            continue

        bbls = set()
        dev_name_up = dev_name.upper()

        # Get borough for filtering
        meta = dev_meta.get(dev_name_up, {})
        if not meta and dev_meta:
            best_meta = process.extractOne(dev_name_up, list(dev_meta.keys()),
                                           scorer=fuzz.token_sort_ratio)
            if best_meta and best_meta[1] >= 82:
                meta = dev_meta[best_meta[0]]

        boro_text = meta.get("borough", "")
        boro_code = boro_text.upper() if boro_text else None

        # ── Strategy 1: PLUTO owner-name keyword search ───────────────────
        words = re.sub(r"[^\w\s]", " ", dev_name_up).split()
        # len >= 3 (not > 3) so "BAY", "ELM" etc. are included
        kw_words = [w for w in words if w not in SKIP_WORDS and len(w) >= 3]

        # For address-style names ("572 WARREN STREET"), skip the house number
        if re.match(r"^\d+", dev_name_up):
            kw_words = [w for w in words[1:] if w not in SKIP_WORDS and len(w) >= 3]

        keyword = " ".join(kw_words[:2]) if kw_words else dev_name_up[:15]

        pluto_hits = pluto_bbls_by_ownername(keyword, borough_code=boro_code)
        # Cap: 40 when borough-filtered (reducing false-positive risk), 15 without
        cap = 40 if boro_code else 15
        if len(pluto_hits) <= cap:
            for hit in pluto_hits:
                if hit["bbl"]:
                    bbls.add(hit["bbl"])
            if bbls:
                log.info("  PLUTO match for '%s': %d BBLs (keyword='%s', boro=%s)",
                         dev_name, len(bbls), keyword, boro_code)
        else:
            log.warning("  PLUTO keyword '%s' too broad (%d hits, cap %d) — skipping",
                        keyword, len(pluto_hits), cap)

        # ── Strategy 2: NYC Geosearch using cross-street ──────────────────
        if not bbls and meta.get("street_a") and meta.get("borough"):
            addr_text = f"{meta['street_a']} & {meta.get('street_b', '')}"
            bbl = geosearch_bbl(addr_text, meta["borough"])
            if bbl:
                bbls.add(bbl)
                log.info("  Geosearch match for '%s': BBL %s", dev_name, bbl)

        results[dev_name] = list(bbls)
        if not bbls:
            log.warning("  No BBLs found for PACT development: %s", dev_name)

    # ── Second pass: hardcoded owner-name map ─────────────────────────────────
    log.info("Applying hardcoded PACT owner-name map…")
    for owner_kw, dev_list in PACT_OWNER_MAP.items():
        for boro in set(b for _, b in dev_list):
            hits = pluto_bbls_by_ownername(owner_kw, borough_code=boro, min_units=20)
            for hit in hits:
                bbl = hit["bbl"]
                if not bbl:
                    continue
                # Assign to all listed developments for this owner+boro combo
                for dev_name, dev_boro in dev_list:
                    if dev_boro == boro:
                        if dev_name not in results:
                            results[dev_name] = []
                        if bbl not in results[dev_name]:
                            results[dev_name].append(bbl)
    for dev, bbls in results.items():
        if bbls:
            log.info("  %s: %d BBLs total after owner-map pass", dev, len(bbls))

    # ── Third pass: pact_bbl_master.csv overrides (replace auto results) ────────
    # Any development with non-empty BBLs in the master file takes precedence.
    # Developments with no BBLs in the master fall through to whatever auto found.
    master_overrides = load_bbl_master()
    for dev_name, override_bbls in master_overrides.items():
        results[dev_name] = list(override_bbls)
        log.info("  %s: %d BBLs from pact_bbl_master.csv", dev_name, len(override_bbls))

    return results


# ─── 4. Marshal Evictions ─────────────────────────────────────────────────────

MARSHAL_URL = "https://data.cityofnewyork.us/resource/6z8x-wfk4.json"


def fetch_marshal_evictions():
    log.info("Fetching marshal evictions (residential, 2017+)…")
    # residential_commercial_ind values: 'Residential', 'R'
    where = (
        "executed_date >= '2017-01-01' AND "
        "(residential_commercial_ind = 'Residential' OR residential_commercial_ind = 'R')"
    )
    rows = socrata_paginate(MARSHAL_URL, where=where, order_col="executed_date")
    df = pd.DataFrame(rows)
    log.info("  Marshal rows: %d", len(df))

    cols_lc = {c.lower(): c for c in df.columns}

    # BBL
    bbl_col = cols_lc.get("bbl")
    df["bbl"] = df[bbl_col].apply(clean_bbl) if bbl_col else None

    # Date
    date_col = cols_lc.get("executed_date") or cols_lc.get("eviction_date")
    df["executed_date"] = pd.to_datetime(df[date_col], errors="coerce") if date_col else pd.NaT
    df["year"] = df["executed_date"].dt.year

    # Address + zip for fallback matching
    addr_col = cols_lc.get("eviction_address") or cols_lc.get("address")
    zip_col  = cols_lc.get("eviction_zip") or cols_lc.get("eviction_zip_code") or cols_lc.get("zip")
    df["addr_norm"] = df[addr_col].apply(normalize_addr) if addr_col else ""
    df["zip"]       = df[zip_col].astype(str).str[:5] if zip_col else ""

    return df


# ─── 5. OCA Housing Court Filings (HDC S3 CSVs) ──────────────────────────────
#
# The HDC OCA Data Collective publishes three tables on S3:
#   oca_index   – case ID, court (→ county), filing date, case classification
#   oca_causes  – case ID, cause of action type
#   oca_addresses – case ID, city, state, postalcode (ZIP)
#
# IMPORTANT: oca_addresses contains respondent mailing ZIP, not property address.
# Street-level address matching is therefore NOT possible via these tables.
# Matching is done at ZIP-code level (confidence tier: "zip").
# OCA direct download (ww2.nycourts.gov) returns 403 — HDC S3 is the only
# publicly accessible source.

HDC_S3 = "https://oca-2-dev.s3.amazonaws.com/public"

LANDLORD_TYPES = {"nonpayment", "holdover", "non-payment", "non payment"}
TENANT_TYPES   = {
    "hp", "breach of warranty", "illegal lockout", "harassment",
    "article 7a", "article7a", "7a",
}

NYC_COURT_PATTERN = re.compile(
    r"bronx|kings|new york|queens|richmond|manhattan|brooklyn|staten island",
    re.IGNORECASE
)


def classify_case_type(cause):
    if not cause:
        return "other"
    c = str(cause).lower().strip()
    for lt in LANDLORD_TYPES:
        if lt in c:
            return "landlord_initiated"
    for tt in TENANT_TYPES:
        if tt in c:
            return "tenant_initiated"
    return "other"


def _stream_csv_chunks(url, chunksize=100_000, usecols=None):
    """Stream a large CSV from URL in pandas chunks."""
    log.info("  Streaming %s …", url)
    r = SESSION.get(url, stream=True, timeout=300)
    r.raise_for_status()
    return pd.read_csv(
        BytesIO(r.content),
        usecols=usecols,
        low_memory=False,
        chunksize=chunksize,
        on_bad_lines="skip",
    )


def fetch_oca_data():
    """
    Download OCA data from HDC S3.
    Returns (DataFrame, source_string).

    Returned DataFrame columns:
      case_id, filing_date, year, court, cause, case_type_class, zip
    NOTE: no street address — ZIP is respondent mailing ZIP.
    """
    log.info("Fetching OCA data from HDC S3…")

    # ── 1. oca_index: case_id, court, filing_date, classification ─────────────
    log.info("  Reading oca_index (filtering to 2022+ NYC)…")
    idx_chunks = []
    try:
        for chunk in _stream_csv_chunks(
            f"{HDC_S3}/oca_index.csv",
            usecols=["indexnumberid", "court", "fileddate", "classification"],
        ):
            # Filter to NYC courts and 2017+
            nyc = chunk[chunk["court"].str.contains(NYC_COURT_PATTERN, na=False)].copy()
            nyc["fileddate"] = pd.to_datetime(nyc["fileddate"], errors="coerce")
            nyc = nyc[nyc["fileddate"].dt.year >= 2017]
            if len(nyc):
                idx_chunks.append(nyc)
    except Exception as e:
        log.warning("  Failed to stream oca_index: %s", e)
        return _empty_oca_df(), "unavailable (oca_index error)"

    if not idx_chunks:
        log.warning("  No 2017+ NYC cases in oca_index.")
        return _empty_oca_df(), "unavailable (no 2017+ NYC rows)"

    oca_idx = pd.concat(idx_chunks, ignore_index=True)
    log.info("  oca_index: %d NYC rows (2017+)", len(oca_idx))
    valid_ids = set(oca_idx["indexnumberid"])

    # ── 2. oca_causes: case_id, cause_of_action ───────────────────────────────
    log.info("  Reading oca_causes…")
    cause_chunks = []
    try:
        for chunk in _stream_csv_chunks(
            f"{HDC_S3}/oca_causes.csv",
            usecols=["indexnumberid", "causeofactiontype"],
        ):
            sub = chunk[chunk["indexnumberid"].isin(valid_ids)]
            if len(sub):
                cause_chunks.append(sub)
    except Exception as e:
        log.warning("  Failed to stream oca_causes: %s", e)

    if cause_chunks:
        oca_causes = pd.concat(cause_chunks, ignore_index=True)
        # Keep first cause per case
        oca_causes = oca_causes.drop_duplicates("indexnumberid")
    else:
        oca_causes = pd.DataFrame(columns=["indexnumberid", "causeofactiontype"])
    log.info("  oca_causes: %d matched rows", len(oca_causes))

    # ── 3. oca_addresses: case_id, postalcode (ZIP) ───────────────────────────
    log.info("  Reading oca_addresses…")
    addr_chunks = []
    try:
        for chunk in _stream_csv_chunks(
            f"{HDC_S3}/oca_addresses.csv",
            usecols=["indexnumberid", "postalcode"],
        ):
            sub = chunk[chunk["indexnumberid"].isin(valid_ids)]
            if len(sub):
                addr_chunks.append(sub)
    except Exception as e:
        log.warning("  Failed to stream oca_addresses: %s", e)

    if addr_chunks:
        oca_addrs = pd.concat(addr_chunks, ignore_index=True)
        oca_addrs = oca_addrs.drop_duplicates("indexnumberid")
        oca_addrs["zip"] = oca_addrs["postalcode"].astype(str).str[:5]
    else:
        oca_addrs = pd.DataFrame(columns=["indexnumberid", "zip"])
    log.info("  oca_addresses: %d matched rows", len(oca_addrs))

    # ── 4. Join ───────────────────────────────────────────────────────────────
    df = oca_idx.merge(oca_causes, on="indexnumberid", how="left")
    df = df.merge(oca_addrs,  on="indexnumberid", how="left")

    df = df.rename(columns={
        "indexnumberid": "case_id",
        "fileddate":     "filing_date",
        "court":         "court",
        "classification":"cause",
        "causeofactiontype": "cause_detail",
    })
    # Prefer cause_detail (oca_causes) for classification; fall back to classification
    df["cause"] = df["cause_detail"].where(df["cause_detail"].notna(), df["cause"])
    df["case_type_class"] = df["cause"].apply(classify_case_type)
    df["year"] = pd.to_datetime(df["filing_date"], errors="coerce").dt.year
    df["zip"]  = df.get("zip", pd.Series([""] * len(df))).fillna("").astype(str).str[:5]

    # NOTE: no street address — addr_norm is blank; match_method will be "zip"
    df["addr_norm"] = ""
    df["address"]   = ""

    log.info("  OCA joined: %d rows", len(df))

    try:
        freshness = requests.get(f"{HDC_S3}/last-updated-date.txt", timeout=10).text.strip()
    except Exception:
        freshness = "unknown"

    source = f"HDC S3 (last updated {freshness}); ZIP-level match only — no street address"
    return df, source


def _empty_oca_df():
    return pd.DataFrame(columns=["case_id", "filing_date", "year", "court",
                                  "cause", "case_type_class", "zip",
                                  "addr_norm", "address"])


# ─── 6. Matching ──────────────────────────────────────────────────────────────

def build_address_index(addr_flat):
    """
    Returns:
    - exact_index: {(addr_norm, zip5) → list of {development_name, bbl}}
    - fuzzy_keys: list of "addr_norm zip5" strings
    - fuzzy_meta: parallel list of {development_name, bbl}
    """
    exact = {}
    for _, row in addr_flat.iterrows():
        key = (row["addr_norm"], str(row["zip"])[:5])
        exact.setdefault(key, []).append({
            "development_name": row.get("development_name", ""),
            "bbl": row.get("bbl"),
        })
    fuzzy_keys = [f"{k[0]} {k[1]}" for k in exact.keys()]
    fuzzy_meta = [v[0] for v in exact.values()]  # first hit per address
    return exact, fuzzy_keys, fuzzy_meta


def match_events_to_sites(events_df, addr_flat, label="PACT", fuzzy_thresh=85):
    """
    Match a DataFrame of eviction events to development addresses.
    Tiers (in priority order):
      1. BBL exact match (confidence: high)
      2. Normalized address + zip exact match (confidence: high)
      3. Fuzzy address + zip match, score ≥ fuzzy_thresh (confidence: medium)
      4. ZIP-only match — for OCA records that have no street address
         (confidence: low; returned with match_method="zip")
    Returns (matched_df, unmatched_df).
    """
    if len(events_df) == 0:
        return events_df.copy(), events_df.copy()

    exact_index, fuzzy_keys, fuzzy_meta = build_address_index(addr_flat)

    # BBL index
    bbl_index = {}
    for _, row in addr_flat.iterrows():
        if row.get("bbl"):
            bbl_index.setdefault(row["bbl"], []).append({
                "development_name": row.get("development_name", ""),
                "bbl": row["bbl"],
            })

    # ZIP → development(s) index (for OCA ZIP-only matching)
    zip_index = {}
    for _, row in addr_flat.iterrows():
        z = str(row.get("zip", ""))[:5]
        if z and z != "nan":
            zip_index.setdefault(z, []).append({
                "development_name": row.get("development_name", ""),
                "bbl": row.get("bbl"),
            })

    matched = []
    unmatched = []

    for _, row in tqdm(events_df.iterrows(), total=len(events_df), desc=f"Match→{label}"):
        row_d = row.to_dict()
        addr_norm = row_d.get("addr_norm", "")
        zip5 = str(row_d.get("zip", ""))[:5]

        # Tier 1: BBL match
        event_bbl = row_d.get("bbl")
        if event_bbl and event_bbl in bbl_index:
            for hit in bbl_index[event_bbl]:
                matched.append({**row_d, **hit, "match_method": "bbl", "match_score": 100})
            continue

        # Tier 2: Exact address + zip
        key = (addr_norm, zip5)
        if addr_norm and key in exact_index:
            for hit in exact_index[key]:
                matched.append({**row_d, **hit, "match_method": "addr_exact", "match_score": 100})
            continue

        # Tier 3: Fuzzy address + zip
        query = f"{addr_norm} {zip5}"
        if addr_norm and fuzzy_keys and query.strip():
            best = process.extractOne(query, fuzzy_keys, scorer=fuzz.token_sort_ratio)
            if best and best[1] >= fuzzy_thresh:
                idx = fuzzy_keys.index(best[0])
                matched.append({**row_d, **fuzzy_meta[idx],
                                 "match_method": "addr_fuzzy", "match_score": best[1]})
                continue

        # Tier 4: ZIP-level matching is intentionally omitted for development-level analysis.
        # A ZIP code typically contains hundreds of buildings; matching all OCA filings in
        # a ZIP to a single PACT development would misattribute the vast majority.
        # Borough-level OCA context is in oca_borough_summary.csv instead.

        unmatched.append(row_d)

    return pd.DataFrame(matched), pd.DataFrame(unmatched)


# ─── 7. Analysis ──────────────────────────────────────────────────────────────

def summarize_by_development(pact_ref, pact_filings, pact_execs,
                              control_filings, control_execs, nycha_dev_df):
    """Build summary_by_development DataFrame."""
    current_year = datetime.date.today().year
    ytd_note = f"YTD (OCA lag ~4-6 weeks)"

    # Build unit-count lookup from pact_ref
    def units_lookup(ref_df):
        lu = {}
        if "development_name" in ref_df.columns and "total_units" in ref_df.columns:
            for _, r in ref_df.iterrows():
                name = str(r.get("development_name", ""))
                val  = r.get("total_units")
                if name and pd.notna(val):
                    lu[name] = float(val)
        return lu

    # Also get units from NYCHA dev dataset for control group
    ctrl_units = {}
    if nycha_dev_df is not None and len(nycha_dev_df) > 0:
        cols_lc = {c.lower(): c for c in nycha_dev_df.columns}
        dev_col = cols_lc.get("development")
        u_col   = cols_lc.get("total_number_of_apartments") or cols_lc.get("number_of_current_apartments")
        if dev_col and u_col:
            for _, r in nycha_dev_df.iterrows():
                name = str(r.get(dev_col, "")).strip()
                raw  = str(r.get(u_col, "")).replace(",", "")
                try:
                    ctrl_units[name] = float(raw)
                except ValueError:
                    pass

    rows = []

    def process_group(filings_df, execs_df, ref_df, unit_lu, group_label, status_filter=None):
        dev_names = set()
        for df in (filings_df, execs_df, ref_df):
            if "development_name" in df.columns:
                dev_names |= set(df["development_name"].dropna().astype(str))

        # Status and manager lookup
        meta = {}
        if "development_name" in ref_df.columns:
            for _, r in ref_df.iterrows():
                name = str(r.get("development_name", ""))
                meta[name] = {
                    "status":   r.get("status_normalized", group_label),
                    "manager":  r.get("property_manager", ""),
                }

        for dev in sorted(dev_names):
            if not dev:
                continue
            units = unit_lu.get(dev)

            for year in range(2017, current_year + 1):
                def sub(df, dev_col="development_name"):
                    if dev_col not in df.columns or "year" not in df.columns:
                        return pd.DataFrame()
                    m = (df[dev_col].astype(str) == dev) & (df["year"].astype(str) == str(year))
                    return df[m]

                f_yr = sub(filings_df)
                e_yr = sub(execs_df)

                n_f = len(f_yr)
                n_e = len(e_yr)
                filing_rate = round(n_f / units, 4) if units and units > 0 else None
                exec_rate   = round(n_e / units, 4) if units and units > 0 else None
                conv_rate   = round(n_e / n_f, 4)   if n_f > 0 else None

                ll_n = ten_n = oth_n = ll_pct = ten_pct = None
                if "case_type_class" in f_yr.columns and n_f > 0:
                    vc = f_yr["case_type_class"].value_counts()
                    ll_n  = int(vc.get("landlord_initiated", 0))
                    ten_n = int(vc.get("tenant_initiated", 0))
                    oth_n = int(vc.get("other", 0))
                    ll_pct  = round(ll_n / n_f * 100, 1)
                    ten_pct = round(ten_n / n_f * 100, 1)

                rows.append({
                    "group":                   group_label,
                    "status":                  meta.get(dev, {}).get("status", group_label),
                    "development_name":        dev,
                    "property_manager":        meta.get(dev, {}).get("manager", ""),
                    "total_units":             units,
                    "year":                    year,
                    "ytd_flag":                ytd_note if year == current_year else "",
                    "filings":                 n_f,
                    "executions":              n_e,
                    "filing_rate_per_unit":    filing_rate,
                    "execution_rate_per_unit": exec_rate,
                    "filing_to_exec_conv_rate":conv_rate,
                    "landlord_initiated_n":    ll_n,
                    "tenant_initiated_n":      ten_n,
                    "other_n":                 oth_n,
                    "landlord_initiated_pct":  ll_pct,
                    "tenant_initiated_pct":    ten_pct,
                })

    pact_units = units_lookup(pact_ref)

    for status_val in ("Construction Complete", "Under Construction"):
        sub_ref = pact_ref[pact_ref["status_normalized"] == status_val] \
            if "status_normalized" in pact_ref.columns else pact_ref
        sub_f = pact_filings[pact_filings["status_normalized"] == status_val] \
            if ("status_normalized" in pact_filings.columns and len(pact_filings) > 0) else pact_filings
        sub_e = pact_execs[pact_execs["status_normalized"] == status_val] \
            if ("status_normalized" in pact_execs.columns and len(pact_execs) > 0) else pact_execs
        process_group(sub_f, sub_e, sub_ref, pact_units, f"PACT ({status_val})")

    # Build control ref_df: all NYCHA developments, column renamed so process_group
    # picks up all dev names (not just those with executions) for the denominator.
    # Exclude PACT development names so they don't inflate the control unit count.
    pact_dev_names_up = {str(n).strip().upper() for n in pact_ref["development_name"].dropna()}
    if nycha_dev_df is not None and len(nycha_dev_df) > 0 and "development" in nycha_dev_df.columns:
        ctrl_ref = nycha_dev_df.copy().rename(columns={"development": "development_name"})
        ctrl_ref = ctrl_ref[~ctrl_ref["development_name"].str.strip().str.upper().isin(pact_dev_names_up)]
    else:
        ctrl_ref = pd.DataFrame(columns=["development_name"])

    process_group(control_filings, control_execs, ctrl_ref, ctrl_units, "Non-PACT NYCHA")

    if not rows:
        return pd.DataFrame()

    summary = pd.DataFrame(rows).sort_values(["development_name", "year"])
    summary["prev_rate"] = summary.groupby("development_name")["filing_rate_per_unit"].shift(1)
    summary["yoy_filing_rate_change_pct"] = (
        (summary["filing_rate_per_unit"] - summary["prev_rate"]) /
        summary["prev_rate"].replace(0, float("nan")) * 100
    ).round(1)
    summary["flag_50pct_increase"] = summary["yoy_filing_rate_change_pct"] >= 50
    summary = summary.drop(columns=["prev_rate"], errors="ignore")

    return summary


def summarize_oca_by_borough(oca_df):
    """
    Borough-level OCA aggregate — more reliable than development-level ZIP matching.
    Uses oca_index 'court' field to derive borough.
    Returns DataFrame: borough × year × case_type with counts.
    """
    if oca_df is None or len(oca_df) == 0:
        return pd.DataFrame()

    df = oca_df.copy()
    # Map court → borough
    court_to_borough = {
        r"bronx":         "Bronx",
        r"kings|brooklyn":"Brooklyn",
        r"new york|manhattan": "Manhattan",
        r"queens":        "Queens",
        r"richmond|staten island": "Staten Island",
    }
    df["borough"] = "Unknown"
    court_col = "court" if "court" in df.columns else None
    if court_col:
        for pattern, boro in court_to_borough.items():
            mask = df[court_col].str.contains(pattern, case=False, na=False)
            df.loc[mask, "borough"] = boro

    df["year"] = pd.to_numeric(df.get("year", pd.Series()), errors="coerce")
    df = df[df["year"] >= 2017]

    agg = (
        df.groupby(["borough", "year", "case_type_class"])
        .size()
        .reset_index(name="filings")
    )
    # Pivot case types
    pivot = agg.pivot_table(
        index=["borough", "year"], columns="case_type_class",
        values="filings", fill_value=0
    ).reset_index()
    pivot.columns.name = None
    pivot["total_filings"] = pivot.get("landlord_initiated", 0) + \
                             pivot.get("tenant_initiated", 0) + \
                             pivot.get("other", 0)
    return pivot


def summarize_by_manager(summary_dev):
    """Aggregate to property_manager × year."""
    if "property_manager" not in summary_dev.columns:
        return pd.DataFrame()
    grp = summary_dev[summary_dev["group"].str.startswith("PACT")].copy()
    agg = grp.groupby(["property_manager", "year"]).agg(
        total_developments=("development_name", "nunique"),
        total_units=("total_units", "sum"),
        total_filings=("filings", "sum"),
        total_executions=("executions", "sum"),
        landlord_initiated_n=("landlord_initiated_n", "sum"),
        tenant_initiated_n=("tenant_initiated_n", "sum"),
    ).reset_index()
    agg["filing_rate_per_unit"] = (
        agg["total_filings"] / agg["total_units"].replace(0, float("nan"))
    ).round(4)
    agg["execution_rate_per_unit"] = (
        agg["total_executions"] / agg["total_units"].replace(0, float("nan"))
    ).round(4)
    agg["filing_to_exec_conv_rate"] = (
        agg["total_executions"] / agg["total_filings"].replace(0, float("nan"))
    ).round(4)
    return agg


# ─── 8. Write outputs ─────────────────────────────────────────────────────────

def write_analysis_md(pact_ref, excluded, summary_dev, summary_mgr,
                      marshal_freshness, oca_source, match_stats, bbl_coverage):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# NYCHA PACT Eviction Analysis",
        f"_Generated: {ts}_",
        "",
        "## Data Sources & Freshness",
        f"- **PACT Dataset PDF**: {PACT_PDF_URL} (downloaded {TODAY})",
        f"- **Marshal Evictions**: NYC Open Data resource `6z8x-wfk4`, last record: {marshal_freshness}",
        f"- **OCA Filings**: {oca_source}",
        f"- **NYCHA Address Dataset**: `3ub5-4ph8`",
        f"- **NYCHA Development Dataset**: `evjd-dqpz`",
        f"- **PLUTO**: `64uk-42ks` (owner-name BBL lookup)",
        f"- **NYC Geosearch**: geosearch.planninglabs.nyc (intersection geocoding)",
        "",
        "## Coverage",
        f"- Active PACT developments (Construction Complete + Under Construction): {len(pact_ref)}",
        f"- Excluded (Planning and Engagement): {len(excluded)}",
        "",
        "## BBL Coverage",
    ]
    for k, v in bbl_coverage.items():
        lines.append(f"- {k}: {v}")

    lines += [
        "",
        "## Match Rates",
    ]
    for k, v in match_stats.items():
        lines.append(f"- {k}: {v}")

    lines += ["", "## Key Findings"]
    if len(summary_dev) > 0:
        for group in summary_dev["group"].unique():
            g = summary_dev[summary_dev["group"] == group]
            for year in range(2025, 2021, -1):
                yr = g[g["year"] == year]
                if len(yr) == 0:
                    continue
                avg_fr = yr["filing_rate_per_unit"].mean()
                avg_er = yr["execution_rate_per_unit"].mean()
                lines.append(
                    f"- **{group}** ({year}): avg filing rate "
                    f"{avg_fr:.4f}/unit, avg execution rate {avg_er:.4f}/unit"
                )
                break

        flagged = summary_dev[summary_dev["flag_50pct_increase"] == True] \
            if "flag_50pct_increase" in summary_dev.columns else pd.DataFrame()
        if len(flagged):
            lines += ["", f"## Developments with ≥50% YoY Filing Rate Increase ({len(flagged)} instances)"]
            for _, row in flagged.iterrows():
                lines.append(
                    f"- {row['development_name']} ({row['group']}): "
                    f"{row['yoy_filing_rate_change_pct']:.1f}% increase in {row['year']}"
                )

    lines += [
        "",
        "## Caveats",
        "- 2026 figures are YTD; OCA data has a ~4–6 week reporting lag.",
        "- PACT developments are not present in the NYCHA address dataset (removed after transfer).",
        "  BBLs resolved via PLUTO owner-name search and NYC Geosearch geocoding — "
          "multi-building developments may have partial BBL coverage.",
        "- Marshal executions use BBL as primary join key (high confidence).",
        "- OCA filings lack BBLs; matched on normalized address + zip.",
        "  Fuzzy threshold: 85/100 token-sort ratio.",
        "- Unit counts from PACT PDF; control group counts from NYCHA development dataset.",
        "- PACT PDF status/manager columns forward-filled across sub-development rows.",
    ]

    (OUT / "analysis.md").write_text("\n".join(lines))
    log.info("Wrote analysis.md")


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=== NYCHA PACT Eviction Analysis ===")

    # ── Step 1: Parse PACT PDF ────────────────────────────────────────────────
    pdf_bytes = fetch_pact_pdf()
    raw_pact  = parse_pact_pdf(pdf_bytes)
    pact_ref, excluded = clean_pact_df(raw_pact)

    # ── Step 2: NYCHA datasets ────────────────────────────────────────────────
    addr_raw     = fetch_nycha_addresses()
    addr_flat    = normalize_nycha_addr_df(addr_raw)
    nycha_dev_df = fetch_nycha_developments()

    # ── Step 3: Resolve BBLs for PACT developments ────────────────────────────
    log.info("Resolving BBLs for PACT developments via PLUTO + Geosearch…")
    pact_bbl_map = resolve_pact_bbls(pact_ref, nycha_dev_df)

    # Flatten PACT BBLs
    pact_bbl_rows = []
    for _, row in pact_ref.iterrows():
        dev = str(row.get("development_name", ""))
        bbls = pact_bbl_map.get(dev, [])
        for bbl in bbls:
            pact_bbl_rows.append({
                "development_name":   dev,
                "bbl":                bbl,
                "status_normalized":  row.get("status_normalized", ""),
                "property_manager":   row.get("property_manager", ""),
                "total_units":        row.get("total_units"),
                "conversion_date":    row.get("conversion_date", ""),
            })
    pact_bbl_flat = pd.DataFrame(pact_bbl_rows) if pact_bbl_rows else pd.DataFrame(
        columns=["development_name", "bbl", "status_normalized",
                 "property_manager", "total_units", "conversion_date"]
    )

    # Add address + zip to pact_bbl_flat for OCA matching
    pact_bbl_flat["addr_norm"] = ""
    pact_bbl_flat["zip"] = ""

    # Build control group addr_flat (non-PACT NYCHA buildings)
    pact_dev_norms = set(pact_ref["development_name"].apply(normalize_addr) if "development_name" in pact_ref.columns else [])
    ctrl_addr = addr_flat[~addr_flat["dev_norm"].isin(pact_dev_norms)].copy()

    pact_bbls    = set(pact_bbl_flat["bbl"].dropna())

    # control_excl: BBLs still NYCHA-owned in PLUTO that would otherwise appear in
    # both pact_bbls and addr_flat; removed from the static control sweep.
    # Also the authoritative conversion-date source for Under Construction BBLs.
    control_excl = load_control_exclusions()  # {bbl → pd.Timestamp or None}
    control_bbls = set(addr_flat["bbl"].dropna()) - pact_bbls - set(control_excl.keys())

    # Build per-BBL conversion date.
    # Priority: (1) PACT PDF conversion_date, (2) pact_bbl_master.csv conversion_date,
    # (3) rad_transferred_date from the NYCHA development dataset,
    # (4) pact_control_exclusions.csv (highest — authoritative for Under Construction BBLs).
    # The master CSV dates are sourced from the NYCHA Development Data Book and are the
    # most reliable local source; they supersede the online NYCHA dataset (step 3).
    bbl_conv_date: dict = {}
    for _, r in pact_bbl_flat.drop_duplicates("bbl").iterrows():
        bbl = r.get("bbl")
        if not bbl:
            continue
        raw = str(r.get("conversion_date", "")).strip()
        bbl_conv_date[bbl] = pd.Timestamp(raw) if raw and raw not in ("", "nan") else None

    # Overlay pact_bbl_master.csv conversion_date for BBLs still missing a date.
    # Dates were populated from the NYCHA Development Data Book (2026-06-25).
    master_dates_raw = {}
    master_csv = OUT / "pact_bbl_master.csv"
    if master_csv.exists():
        import csv as _csv
        with open(master_csv) as _f:
            for _row in _csv.DictReader(_f):
                _dev = _row.get("development_name", "").strip()
                _d   = _row.get("conversion_date", "").strip()
                if _dev and _d and _d not in ("", "nan"):
                    master_dates_raw[_dev] = pd.Timestamp(_d)
    for _, r in pact_bbl_flat.drop_duplicates("bbl").iterrows():
        bbl = r.get("bbl")
        if not bbl or bbl_conv_date.get(bbl) is not None:
            continue
        dev = str(r.get("development_name", "")).strip()
        if dev in master_dates_raw:
            bbl_conv_date[bbl] = master_dates_raw[dev]
            log.debug("    bbl %s → master date %s (via %s)", bbl, master_dates_raw[dev].date(), dev)

    # Overlay rad_transferred_date from NYCHA dev dataset for BBLs still missing a date
    rad_lu = {}
    if nycha_dev_df is not None and "rad_transferred_date" in nycha_dev_df.columns:
        for _, r in nycha_dev_df.iterrows():
            raw = str(r.get("rad_transferred_date", "")).strip()
            if raw and raw != "nan":
                rad_lu[str(r.get("development", "")).strip().upper()] = pd.Timestamp(raw[:10])
    for _, r in pact_bbl_flat.drop_duplicates("bbl").iterrows():
        bbl = r.get("bbl")
        if not bbl or bbl_conv_date.get(bbl) is not None:
            continue
        dev = str(r.get("development_name", "")).strip().upper()
        if dev in rad_lu:
            bbl_conv_date[bbl] = rad_lu[dev]
            log.debug("    bbl %s → rad date %s (via %s)", bbl, rad_lu[dev].date(), dev)

    # Overlay control_excl dates (authoritative for Under Construction BBLs)
    for bbl, conv_date in control_excl.items():
        if conv_date is not None:
            bbl_conv_date[bbl] = conv_date
        elif bbl not in bbl_conv_date:
            bbl_conv_date[bbl] = None

    dated = sum(1 for v in bbl_conv_date.values() if v is not None)
    log.info("  bbl_conv_date: %d BBLs total, %d with known conversion date", len(bbl_conv_date), dated)

    bbl_coverage = {
        "PACT developments with ≥1 BBL": sum(1 for v in pact_bbl_map.values() if v),
        "PACT developments without BBL": sum(1 for v in pact_bbl_map.values() if not v),
        "Total PACT BBLs": len(pact_bbls),
        "Total control BBLs": len(control_bbls),
    }
    log.info("BBL coverage: %s", bbl_coverage)

    # ── Step 4: Marshal evictions ─────────────────────────────────────────────
    marshal_df = fetch_marshal_evictions()

    marshal_freshness = "unknown"
    if "executed_date" in marshal_df.columns:
        max_d = marshal_df["executed_date"].max()
        if pd.notna(max_d):
            marshal_freshness = str(max_d.date())

    # Date-aware split: executions at PACT BBLs before their conversion date
    # count as control (the building was still NYCHA-managed then).
    # control_excl BBLs (PLUTO owner still NYCHA): no conv_date → all control.
    # Non-excl PACT BBLs (PLUTO owner transferred): no conv_date → all PACT.
    control_excl_bbls = set(control_excl.keys())
    pact_rows, ctrl_rows, pre_conv_rows = [], [], []
    for _, row in marshal_df.iterrows():
        bbl = row.get("bbl")
        exec_date = row.get("executed_date")
        if bbl in pact_bbls:
            conv_date = bbl_conv_date.get(bbl)
            if conv_date is None:
                # No known transfer date: route by PLUTO ownership signal.
                # Still-NYCHA-owned BBLs (control_excl) → control; others → PACT.
                if bbl in control_excl_bbls:
                    pre_conv_rows.append(row)
                else:
                    pact_rows.append(row)
            elif not pd.isna(exec_date) and exec_date < conv_date:
                # Pre-conversion execution → treat as control
                pre_conv_rows.append(row)
            else:
                pact_rows.append(row)
        elif bbl in control_bbls:
            ctrl_rows.append(row)
        # Any remaining BBL (e.g. control_excl that somehow escaped pact_bbls) is dropped.

    pact_exec    = pd.DataFrame(pact_rows) if pact_rows else marshal_df.iloc[:0].copy()
    control_exec = pd.concat(
        [pd.DataFrame(ctrl_rows), pd.DataFrame(pre_conv_rows)], ignore_index=True
    ) if (ctrl_rows or pre_conv_rows) else marshal_df.iloc[:0].copy()

    log.info("Marshal at PACT: %d   Control: %d  (incl %d pre-conversion PACT BBLs)",
             len(pact_exec), len(control_exec), len(pre_conv_rows))

    # Annotate with development metadata
    # Build BBL→dev lookup (deduplicate: first row per BBL wins)
    bbl_to_dev = {}
    if len(pact_bbl_flat) > 0:
        for _, r in pact_bbl_flat.drop_duplicates("bbl").iterrows():
            bbl_to_dev[r["bbl"]] = {
                "development_name":  r.get("development_name"),
                "status_normalized": r.get("status_normalized"),
                "property_manager":  r.get("property_manager"),
            }
    for col in ("development_name", "status_normalized", "property_manager"):
        pact_exec[col] = pact_exec["bbl"].map(lambda b: bbl_to_dev.get(b, {}).get(col))

    ctrl_bbl_to_dev = {}
    if "bbl" in addr_flat.columns:
        for _, r in addr_flat.dropna(subset=["bbl"]).drop_duplicates("bbl").iterrows():
            ctrl_bbl_to_dev[r["bbl"]] = {"development_name": r.get("development_name")}
    control_exec["development_name"] = control_exec["bbl"].map(
        lambda b: ctrl_bbl_to_dev.get(b, {}).get("development_name")
    )

    # ── Step 5: OCA filings ───────────────────────────────────────────────────
    oca_df, oca_source = fetch_oca_data()

    pact_filings    = pd.DataFrame()
    control_filings = pd.DataFrame()
    unmatched_oca   = pd.DataFrame()
    match_stats     = {}

    if len(oca_df) > 0:
        log.info("Matching OCA filings to addresses (%d rows)…", len(oca_df))

        # Build a PACT addr_flat that includes:
        #   a) privately_managed=YES buildings from NYCHA addr dataset
        #   b) Synthetic rows for each PACT BBL we resolved (no street address,
        #      but zip from PLUTO lookup will be filled below)
        pact_pm_addr = addr_flat[addr_flat["privately_managed"]].copy()

        # For development-level OCA matching, only use address+BBL matches.
        # ZIP-level matching is omitted (see comment in match_events_to_sites).
        pact_addr_for_match = pact_pm_addr

        pact_filings, oca_unmatched_1 = match_events_to_sites(
            oca_df, pact_addr_for_match, "PACT"
        )

        log.info("Matching remaining OCA filings to control NYCHA addresses…")
        control_filings, unmatched_oca = match_events_to_sites(
            oca_unmatched_1, ctrl_addr, "Control"
        )

        total = len(oca_df)
        match_stats = {
            "OCA total NYC filings (2022+)":      total,
            "Matched to PACT (any tier)":         len(pact_filings),
            "Matched to control NYCHA (any tier)":len(control_filings),
            "Unmatched":                          len(unmatched_oca),
            "PACT match rate":    f"{len(pact_filings)/total*100:.2f}%" if total else "0%",
            "Control match rate": f"{len(control_filings)/total*100:.2f}%" if total else "0%",
            "NOTE": "OCA matches are ZIP-level only (no street address in source data)",
        }
        log.info("OCA match stats: %s", match_stats)
    else:
        match_stats = {"OCA data": oca_source}

    # Annotate pact_filings with metadata
    if len(pact_filings) > 0 and "development_name" in pact_filings.columns:
        meta_map = {}
        if "development_name" in pact_ref.columns:
            for _, r in pact_ref.iterrows():
                meta_map[str(r["development_name"])] = {
                    "status_normalized": r.get("status_normalized"),
                    "property_manager":  r.get("property_manager"),
                    "total_units":       r.get("total_units"),
                }
        for col in ("status_normalized", "property_manager", "total_units"):
            pact_filings[col] = pact_filings["development_name"].map(
                lambda d: meta_map.get(str(d), {}).get(col)
            )

    # ── Step 6: Summaries ─────────────────────────────────────────────────────
    log.info("Computing summaries…")
    summary_dev = summarize_by_development(
        pact_ref, pact_filings, pact_exec,
        control_filings, control_exec, nycha_dev_df
    )
    summary_mgr = summarize_by_manager(summary_dev) if len(summary_dev) > 0 else pd.DataFrame()
    oca_borough  = summarize_oca_by_borough(oca_df)

    # ── Step 7: Write outputs ─────────────────────────────────────────────────
    log.info("Writing output files…")

    def save_bbls_col(df):
        df = df.copy()
        if "bbls" in df.columns:
            df["bbls"] = df["bbls"].apply(
                lambda x: "|".join(x) if isinstance(x, list) else (x or "")
            )
        return df

    save_bbls_col(pact_ref).to_csv(OUT / "pact_reference.csv", index=False)
    log.info("  pact_reference.csv (%d rows)", len(pact_ref))

    save_bbls_col(excluded).to_csv(OUT / "excluded_developments.csv", index=False)
    log.info("  excluded_developments.csv (%d rows)", len(excluded))

    pact_filings.to_csv(OUT / "pact_filings.csv", index=False)
    log.info("  pact_filings.csv (%d rows — ZIP-matched only)", len(pact_filings))

    pact_exec.to_csv(OUT / "pact_executions.csv", index=False)
    log.info("  pact_executions.csv (%d rows)", len(pact_exec))

    control_filings.to_csv(OUT / "control_filings.csv", index=False)
    log.info("  control_filings.csv (%d rows — ZIP-matched only)", len(control_filings))

    control_exec.to_csv(OUT / "control_executions.csv", index=False)
    log.info("  control_executions.csv (%d rows)", len(control_exec))

    summary_dev.to_csv(OUT / "summary_by_development.csv", index=False)
    log.info("  summary_by_development.csv (%d rows)", len(summary_dev))

    # aggregate_execution_rates.csv — one row per year, group totals
    agg_rows = []
    for year in sorted(summary_dev["year"].unique()):
        yr = summary_dev[summary_dev["year"] == year]
        pact_all      = yr[yr["group"].str.startswith("PACT")]
        pact_complete = yr[yr["group"] == "PACT (Construction Complete)"]
        ctrl          = yr[yr["group"] == "Non-PACT NYCHA"]
        pact_units     = pact_all["total_units"].sum()
        complete_units = pact_complete["total_units"].sum()
        ctrl_units_yr  = ctrl["total_units"].sum()
        pact_exec_n     = pact_all["executions"].sum()
        complete_exec_n = pact_complete["executions"].sum()
        ctrl_exec_n     = ctrl["executions"].sum()
        pact_per_1k     = round(pact_exec_n / pact_units * 1000, 2) if pact_units > 0 else 0.0
        complete_per_1k = round(complete_exec_n / complete_units * 1000, 2) if complete_units > 0 else 0.0
        ctrl_per_1k     = round(ctrl_exec_n / ctrl_units_yr * 1000, 2) if ctrl_units_yr > 0 else 0.0
        ratio           = round(complete_per_1k / ctrl_per_1k, 1) if ctrl_per_1k > 0 else None
        agg_rows.append({
            "year":                    int(year),
            "pact_devs":               int(pact_all["development_name"].nunique()),
            "pact_units":              float(pact_units),
            "pact_exec":               int(pact_exec_n),
            "pact_per_1k":             pact_per_1k,
            "complete_devs":           int(pact_complete["development_name"].nunique()),
            "complete_units":          float(complete_units),
            "complete_exec":           int(complete_exec_n),
            "complete_per_1k":         complete_per_1k,
            "ctrl_units":              float(ctrl_units_yr),
            "ctrl_exec":               int(ctrl_exec_n),
            "ctrl_per_1k":             ctrl_per_1k,
            "ratio_complete_vs_ctrl":  ratio,
        })
    pd.DataFrame(agg_rows).to_csv(OUT / "aggregate_execution_rates.csv", index=False)
    log.info("  aggregate_execution_rates.csv (%d rows)", len(agg_rows))

    summary_mgr.to_csv(OUT / "summary_by_manager.csv", index=False)
    log.info("  summary_by_manager.csv (%d rows)", len(summary_mgr))

    oca_borough.to_csv(OUT / "oca_borough_summary.csv", index=False)
    log.info("  oca_borough_summary.csv (%d rows)", len(oca_borough))

    unmatched_oca.to_csv(OUT / "unmatched_oca.csv", index=False)
    log.info("  unmatched_oca.csv (%d rows)", len(unmatched_oca))

    write_analysis_md(pact_ref, excluded, summary_dev, summary_mgr,
                      marshal_freshness, oca_source, match_stats, bbl_coverage)

    log.info("=== Done. Outputs in %s ===", OUT)
    print("\n── Summary ──────────────────────────────────────────────────────")
    print(f"  PACT active developments  : {len(pact_ref)}")
    print(f"  PACT BBL coverage         : {bbl_coverage}")
    print(f"  Marshal evictions (PACT)  : {len(pact_exec)}")
    print(f"  Marshal evictions (ctrl)  : {len(control_exec)}")
    print(f"  OCA total NYC filings     : {len(oca_df)}")
    print(f"  OCA → PACT (zip-matched)  : {len(pact_filings)}")
    print(f"  OCA → control (zip-matched): {len(control_filings)}")
    print(f"  OCA unmatched             : {len(unmatched_oca)}")
    print(f"  OCA source                : {oca_source}")


if __name__ == "__main__":
    main()
