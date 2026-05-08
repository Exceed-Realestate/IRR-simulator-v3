#!/usr/bin/env python3
"""
Daily DLD scraper for IRR Simulator v3.

Fetches Dubai Pulse Open Data API:
  - dld_transactions-open-api  → sale transactions (price, sqft, area)
  - dld_rental_contracts-open-api → rental contracts (annual rent, sqft, area)

Aggregates per-area:
  - avg AED/sqft (apartment, villa)
  - YoY % change (last 12 months vs previous 12 months)
  - 5-yr % change
  - tx count (last 12 months)
  - avg gross rental yield % (apartment, villa)

Writes live-areas.json next to areas.json. The frontend reads both and
merges live values on top of curated estimates (and agent-verified
overrides on top of live).

Auth:
  Reads DLD_API_KEY and DLD_API_SECRET from environment. If missing,
  runs in MOCK mode and emits structurally-correct placeholder data so
  frontend integration can be developed end-to-end.

Usage:
  python3 scripts/scrape-dld.py [--mock] [--output PATH]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ----- Curated → DLD area-name mapping -----
# DLD uses Arabic-flavored names (e.g. "Nakhlat Jumeira" for Palm Jumeirah).
# When the real API arrives we'll verify these against the dld_lkp_areas-open
# lookup table. For now, MOCK mode ignores this and just produces fake numbers.
CURATED_TO_DLD_AREA = {
    "palmJumeirah":      ["Nakhlat Jumeira", "Palm Jumeirah"],
    "dubaiCreekHarbour": ["Al Khairan First", "Dubai Creek Harbour"],  # tentative
    "zaabeel":           ["Za'abeel First", "Za'abeel Second", "Trade Center First"],
    "palmJebelAli":      ["Palm Jebel Ali", "Mina Jebel Ali"],
    "dubaiHills":        ["Hadaeq Sheikh Mohammed Bin Rashid", "Dubai Hills"],
    "businessBay":       ["Business Bay"],
    "downtownDubai":     ["Burj Khalifa", "Downtown Dubai"],
    "dubaiMarina":       ["Marsa Dubai", "Dubai Marina"],
    "jvc":               ["Al Barsha South Fourth", "Jumeirah Village Circle"],
    "jbr":               ["Marsa Dubai"],  # JBR is a sub-community of Marsa Dubai in DLD
    "damacHills":        ["Hadaeq Sheikh Mohammed Bin Rashid", "DAMAC Hills"],
    "bluewatersIsland":  ["Marsa Dubai"],  # Bluewaters is also a sub-community
    "difc":              ["DIFC", "Trade Center 2"],
}

API_BASE = "https://api.dubaipulse.gov.ae/open/dld"
OAUTH_URL = "https://api.dubaipulse.gov.ae/oauth/client_credential/accesstoken?grant_type=client_credentials"


def get_access_token(api_key: str, api_secret: str) -> str:
    import urllib.request
    import urllib.parse
    body = urllib.parse.urlencode({"client_id": api_key, "client_secret": api_secret}).encode()
    req = urllib.request.Request(OAUTH_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
        return data["access_token"]


def fetch_dld(endpoint: str, token: str, params: dict) -> list:
    """Fetch a paginated DLD endpoint. Returns concatenated rows."""
    import urllib.request
    import urllib.parse
    rows = []
    offset = 0
    page_size = 1000
    while True:
        q = dict(params)
        q.update({"limit": page_size, "offset": offset})
        url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            page = json.loads(resp.read())
        if not isinstance(page, list):
            page = page.get("records", []) or page.get("data", [])
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
        # Safety: cap at 200k rows so a single area never blows memory
        if len(rows) > 200_000:
            print(f"[warn] {endpoint}: hit 200k row cap; truncating", file=sys.stderr)
            break
    return rows


def aggregate_area(curated_key: str, dld_areas: list, transactions: list, rentals: list) -> dict:
    """Compute per-area aggregates from raw rows. Real implementation."""
    # Filter transactions to this area
    area_set = set(dld_areas)
    area_tx = [t for t in transactions if t.get("area_name_en") in area_set]
    if not area_tx:
        return {}

    # Last 12 months
    now = datetime.now(timezone.utc)
    cutoff_12mo = now.timestamp() - 365 * 86400
    cutoff_24mo = now.timestamp() - 730 * 86400
    cutoff_5yr = now.timestamp() - 365 * 86400 * 5

    def parse_date(s):
        if not s: return 0
        try: return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception: return 0

    def avg_psf(rows):
        psfs = []
        for r in rows:
            try:
                amt = float(r.get("trans_value") or r.get("actual_worth") or 0)
                sqft = float(r.get("procedure_area") or r.get("actual_area") or 0)
                if amt > 0 and sqft > 100:
                    psfs.append(amt / sqft)
            except (TypeError, ValueError):
                continue
        if not psfs: return None
        # Trimmed mean — drop top/bottom 10% to reduce outlier influence
        psfs.sort()
        trim = max(1, len(psfs) // 10)
        psfs = psfs[trim:-trim] if len(psfs) > 2 * trim else psfs
        return sum(psfs) / len(psfs)

    last_12 = [t for t in area_tx if parse_date(t.get("instance_date")) > cutoff_12mo]
    prev_12 = [t for t in area_tx if cutoff_24mo < parse_date(t.get("instance_date")) <= cutoff_12mo]
    five_yr_ago = [t for t in area_tx if cutoff_5yr - 86400 * 90 < parse_date(t.get("instance_date")) < cutoff_5yr + 86400 * 90]

    apt_last = [t for t in last_12 if (t.get("property_sub_type_en") or "").lower().startswith("flat")]
    villa_last = [t for t in last_12 if (t.get("property_sub_type_en") or "").lower().startswith("villa")]
    apt_prev = [t for t in prev_12 if (t.get("property_sub_type_en") or "").lower().startswith("flat")]
    villa_prev = [t for t in prev_12 if (t.get("property_sub_type_en") or "").lower().startswith("villa")]
    apt_5yr = [t for t in five_yr_ago if (t.get("property_sub_type_en") or "").lower().startswith("flat")]

    apt_psf_now = avg_psf(apt_last)
    villa_psf_now = avg_psf(villa_last)
    apt_psf_prev = avg_psf(apt_prev)
    apt_psf_5yr = avg_psf(apt_5yr)

    def pct_change(now, then):
        if not now or not then: return None
        return round((now - then) / then * 100, 1)

    # Rentals → yield = avg annual rent / avg price for same property type
    area_rent = [r for r in rentals if r.get("area_name_en") in area_set]
    last_12_rent = [r for r in area_rent if parse_date(r.get("registration_date")) > cutoff_12mo]
    apt_rents = [float(r.get("annual_amount") or 0) for r in last_12_rent if (r.get("property_sub_type_en") or "").lower().startswith("flat")]
    villa_rents = [float(r.get("annual_amount") or 0) for r in last_12_rent if (r.get("property_sub_type_en") or "").lower().startswith("villa")]
    apt_rents = [r for r in apt_rents if r > 0]
    villa_rents = [r for r in villa_rents if r > 0]
    apt_yield = None
    villa_yield = None
    if apt_rents and apt_psf_now:
        # Approximate: avg-rent / avg-price (using same set's avg price)
        apt_avg_rent = sum(apt_rents) / len(apt_rents)
        # Need approximate avg sale price — use avg sqft × psf
        apt_avg_sqft = sum(float(t.get("procedure_area") or 0) for t in apt_last if t.get("procedure_area")) / max(1, len([t for t in apt_last if t.get("procedure_area")]))
        if apt_avg_sqft > 0:
            apt_avg_price = apt_avg_sqft * apt_psf_now
            apt_yield = round(apt_avg_rent / apt_avg_price * 100, 1)
    if villa_rents and villa_psf_now:
        villa_avg_rent = sum(villa_rents) / len(villa_rents)
        villa_avg_sqft = sum(float(t.get("procedure_area") or 0) for t in villa_last if t.get("procedure_area")) / max(1, len([t for t in villa_last if t.get("procedure_area")]))
        if villa_avg_sqft > 0:
            villa_avg_price = villa_avg_sqft * villa_psf_now
            villa_yield = round(villa_avg_rent / villa_avg_price * 100, 1)

    out = {}
    if apt_psf_now: out.setdefault("avgPriceSqftAed", {})["apartment"] = round(apt_psf_now, 0)
    if villa_psf_now: out.setdefault("avgPriceSqftAed", {})["villa"] = round(villa_psf_now, 0)
    yoy = pct_change(apt_psf_now, apt_psf_prev)
    fiveYr = pct_change(apt_psf_now, apt_psf_5yr)
    if yoy is not None: out["priceSqftYoYPct"] = yoy
    if fiveYr is not None: out["priceSqft5yrPct"] = fiveYr
    if apt_yield: out.setdefault("avgRentalYieldPct", {})["apartment"] = apt_yield
    if villa_yield: out.setdefault("avgRentalYieldPct", {})["villa"] = villa_yield
    if last_12: out["txCount12mo"] = len(last_12)
    out["lastComputed"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


def run_real(token: str) -> dict:
    """Real API mode."""
    print("[scrape-dld] fetching dld_transactions-open-api…", file=sys.stderr)
    transactions = fetch_dld("dld_transactions-open-api", token, {})
    print(f"[scrape-dld] {len(transactions)} transactions", file=sys.stderr)

    print("[scrape-dld] fetching dld_rental_contracts-open-api…", file=sys.stderr)
    rentals = fetch_dld("dld_rental_contracts-open-api", token, {})
    print(f"[scrape-dld] {len(rentals)} rental contracts", file=sys.stderr)

    out = {}
    for curated, dld_areas in CURATED_TO_DLD_AREA.items():
        agg = aggregate_area(curated, dld_areas, transactions, rentals)
        if agg:
            out[curated] = agg
    return out


def run_mock() -> dict:
    """Mock mode: structurally-correct fake data so frontend can be developed."""
    print("[scrape-dld] MOCK mode (no DLD_API_KEY/SECRET set)", file=sys.stderr)
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Hand-tuned fake numbers loosely consistent with curated estimates,
    # with a slight intentional drift so frontend can show "DLD live" badges
    # and we can visually distinguish them from curated.
    return {
        "palmJumeirah":      {"avgPriceSqftAed": {"apartment": 3812, "villa": 5240}, "priceSqftYoYPct": 9.4, "priceSqft5yrPct": 108.2, "avgRentalYieldPct": {"apartment": 6.2, "villa": 4.7}, "txCount12mo": 1742, "lastComputed": iso},
        "dubaiCreekHarbour": {"avgPriceSqftAed": {"apartment": 2680},                "priceSqftYoYPct": 14.6, "priceSqft5yrPct": 78.4, "avgRentalYieldPct": {"apartment": 6.8},                "txCount12mo": 3924, "lastComputed": iso},
        "zaabeel":           {"avgPriceSqftAed": {"apartment": 4350},                "priceSqftYoYPct": 11.8, "priceSqft5yrPct": 95.7, "avgRentalYieldPct": {"apartment": 5.6},                "txCount12mo": 187,  "lastComputed": iso},
        "palmJebelAli":      {"avgPriceSqftAed": {"villa": 2780},                    "priceSqftYoYPct": 32.5,                          "txCount12mo": 2105, "lastComputed": iso},
        "dubaiHills":        {"avgPriceSqftAed": {"apartment": 2095, "villa": 2480}, "priceSqftYoYPct": 11.3, "priceSqft5yrPct": 92.8, "avgRentalYieldPct": {"apartment": 6.1, "villa": 4.5}, "txCount12mo": 5320, "lastComputed": iso},
        "businessBay":       {"avgPriceSqftAed": {"apartment": 2280},                "priceSqftYoYPct": 12.4, "priceSqft5yrPct": 88.6, "avgRentalYieldPct": {"apartment": 7.2},                "txCount12mo": 5687, "lastComputed": iso},
        "downtownDubai":     {"avgPriceSqftAed": {"apartment": 3275},                "priceSqftYoYPct": 14.7, "priceSqft5yrPct": 102.3, "avgRentalYieldPct": {"apartment": 5.7},               "txCount12mo": 4108, "lastComputed": iso},
        "dubaiMarina":       {"avgPriceSqftAed": {"apartment": 2185},                "priceSqftYoYPct": 11.5, "priceSqft5yrPct": 86.2, "avgRentalYieldPct": {"apartment": 6.6},                "txCount12mo": 6320, "lastComputed": iso},
        "jvc":               {"avgPriceSqftAed": {"apartment": 1180, "villa": 1620}, "priceSqftYoYPct": 14.8, "priceSqft5yrPct": 96.8, "avgRentalYieldPct": {"apartment": 8.1, "villa": 5.9}, "txCount12mo": 11240, "lastComputed": iso},
        "jbr":               {"avgPriceSqftAed": {"apartment": 2640},                "priceSqftYoYPct": 11.9, "priceSqft5yrPct": 88.7, "avgRentalYieldPct": {"apartment": 6.0},                "txCount12mo": 1685, "lastComputed": iso},
        "damacHills":        {"avgPriceSqftAed": {"apartment": 1450, "villa": 1820}, "priceSqftYoYPct": 10.8, "priceSqft5yrPct": 81.4, "avgRentalYieldPct": {"apartment": 6.7, "villa": 5.4}, "txCount12mo": 2780, "lastComputed": iso},
        "bluewatersIsland":  {"avgPriceSqftAed": {"apartment": 4020},                "priceSqftYoYPct": 14.2, "priceSqft5yrPct": 108.5, "avgRentalYieldPct": {"apartment": 6.1},               "txCount12mo": 372,  "lastComputed": iso},
        "difc":              {"avgPriceSqftAed": {"apartment": 3580},                "priceSqftYoYPct": 12.5, "priceSqft5yrPct": 99.4, "avgRentalYieldPct": {"apartment": 5.7},                "txCount12mo": 218,  "lastComputed": iso},
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mock", action="store_true", help="Force mock mode even if credentials are set")
    p.add_argument("--output", default=str(Path(__file__).parent.parent / "live-areas.json"))
    args = p.parse_args()

    api_key = os.environ.get("DLD_API_KEY", "").strip()
    api_secret = os.environ.get("DLD_API_SECRET", "").strip()

    use_mock = args.mock or not (api_key and api_secret)
    if use_mock:
        live_areas = run_mock()
        mode = "mock"
        source = "mock data — replace with real DLD scrape once credentials are set as DLD_API_KEY / DLD_API_SECRET in repo Secrets"
    else:
        try:
            print("[scrape-dld] auth…", file=sys.stderr)
            token = get_access_token(api_key, api_secret)
            live_areas = run_real(token)
            mode = "live"
            source = "Dubai Pulse Open Data API (dld_transactions-open-api + dld_rental_contracts-open-api), trimmed-mean aggregation last 12 months"
        except Exception as e:
            print(f"[scrape-dld] real fetch failed ({e}); falling back to mock", file=sys.stderr)
            live_areas = run_mock()
            mode = "mock-fallback"
            source = f"mock fallback (real fetch failed: {e})"

    out = {
        "_meta": {
            "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": mode,
            "source": source,
            "schemaVersion": 1,
        },
        **live_areas,
    }
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"[scrape-dld] wrote {args.output} ({len(live_areas)} areas, mode={mode})", file=sys.stderr)


if __name__ == "__main__":
    main()
