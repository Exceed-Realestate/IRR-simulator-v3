#!/usr/bin/env python3
"""
Daily DLD scraper for IRR Simulator v3 — data.dubai DDA iPaaS edition.

Auth flow (per HowToUseAPI.pdf):
  1. POST {base}/secure/ssis/dubaiai/gatewaytoken/1.0.0/getAccessToken
     - Headers: Content-Type: application/json, x-DDA-SecurityApplicationIdentifier
     - Body:    {grant_type:"client_credentials", client_id, client_secret}
     - Returns: { access_token, token_type:"Bearer", expires_in:3600, scope }
  2. GET {base}/secure/ddads/openapi/1.0.0/{entity}/{dataset}
     - Header:  Authorization: Bearer <access_token>
     - Query:   page, pageSize (max 1000), filter, order_by, order_dir
     - 60 req/min rate limit, 30s timeout, UAE-only IP access

Two run modes:
  - DIRECT: call data.dubai directly (only works from a UAE IP; not GH Actions)
  - PROXY:  call our Cloudflare Worker, which holds the credentials and is
            geo-pinned to UAE via Smart Placement. Default for GH Actions.

Environment:
  DLD_PROXY_URL          — Cloudflare Worker URL (e.g. https://exceed-irr-ai.<sub>.workers.dev)
  DLD_PROXY_SECRET       — shared secret for Worker /dld-fetch endpoint

  (Direct-mode only — for local UAE testing:)
  DLD_BASE_URL           — defaults to https://stg-apis.data.dubai
  DLD_APP_ID             — x-DDA-SecurityApplicationIdentifier
  DLD_CLIENT_ID
  DLD_CLIENT_SECRET

Outputs live-areas.json at the repo root with the aggregated per-area stats.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.parse
import urllib.error

# ----- Curated → DLD area-name mapping (lookups verified manually once
# the dld_lkp_areas-open dataset is available) -----
CURATED_TO_DLD_AREA = {
    "palmJumeirah":      ["Nakhlat Jumeira", "Palm Jumeirah"],
    "dubaiCreekHarbour": ["Al Khairan First", "Dubai Creek Harbour"],
    "zaabeel":           ["Za'abeel First", "Za'abeel Second", "Trade Center First"],
    "palmJebelAli":      ["Palm Jebel Ali", "Mina Jebel Ali"],
    "dubaiHills":        ["Hadaeq Sheikh Mohammed Bin Rashid", "Dubai Hills"],
    "businessBay":       ["Business Bay"],
    "downtownDubai":     ["Burj Khalifa", "Downtown Dubai"],
    "dubaiMarina":       ["Marsa Dubai", "Dubai Marina"],
    "jvc":               ["Al Barsha South Fourth", "Jumeirah Village Circle"],
    "jbr":               ["Marsa Dubai"],
    "damacHills":        ["Hadaeq Sheikh Mohammed Bin Rashid", "DAMAC Hills"],
    "bluewatersIsland":  ["Marsa Dubai"],
    "difc":              ["DIFC", "Trade Center 2"],
    "sobhaHartland":     ["Hadaeq Sheikh Mohammed Bin Rashid", "Sobha Hartland"],
    "mbrCity1":          ["Hadaeq Sheikh Mohammed Bin Rashid", "MBR City"],
    "dubaiHarbour":      ["Marsa Dubai"],
    "minaRashid":        ["Mina Rashid", "Mena Rashid"],
    "tilalAlGhaf":       ["Hessyan Second", "Tilal Al Ghaf"]
}

# DLD entity + dataset names on data.dubai. These need to be confirmed once
# the user navigates to the DLD page on data.dubai → check the "Additional
# Information" / API path. Placeholders below — override with env vars.
DLD_ENTITY = os.environ.get("DLD_ENTITY", "dld")
DLD_TX_DATASET = os.environ.get("DLD_TX_DATASET", "dld_transactions-open")
DLD_RENT_DATASET = os.environ.get("DLD_RENT_DATASET", "dld_rental_contracts-open")


# ============ TRANSPORT ============

def http_post(url: str, body: dict, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def http_get_json(url: str, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ============ DIRECT (data.dubai) ============

class DirectClient:
    def __init__(self, base: str, app_id: str, client_id: str, client_secret: str):
        self.base = base.rstrip("/")
        self.app_id = app_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._token_expires = 0

    def _ensure_token(self):
        if self._token and time.time() < self._token_expires - 30:
            return
        url = f"{self.base}/secure/ssis/dubaiai/gatewaytoken/1.0.0/getAccessToken"
        body = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        headers = {"x-DDA-SecurityApplicationIdentifier": self.app_id}
        data = http_post(url, body, headers, timeout=30)
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 3600))

    def health(self) -> dict:
        self._ensure_token()
        return http_get_json(
            f"{self.base}/secure/ddads/healthcheck/1.0.0/health",
            {"Authorization": f"Bearer {self._token}"},
            timeout=30,
        )

    def fetch_page(self, entity: str, dataset: str, page: int = 1, page_size: int = 1000, **params) -> dict:
        self._ensure_token()
        q = {"page": page, "pageSize": page_size, **params}
        qs = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
        url = f"{self.base}/secure/ddads/openapi/1.0.0/{entity}/{dataset}?{qs}"
        return http_get_json(url, {"Authorization": f"Bearer {self._token}"}, timeout=30)


# ============ PROXY (Cloudflare Worker) ============

class ProxyClient:
    def __init__(self, proxy_url: str, secret: str):
        self.proxy_url = proxy_url.rstrip("/")
        self.secret = secret

    def _post(self, path: str, body: dict) -> dict:
        return http_post(
            f"{self.proxy_url}{path}",
            body,
            {"x-Proxy-Secret": self.secret},
            timeout=60,
        )

    def health(self) -> dict:
        return self._post("/dld-health", {})

    def fetch_page(self, entity: str, dataset: str, page: int = 1, page_size: int = 1000, **params) -> dict:
        body = {"entity": entity, "dataset": dataset, "page": page, "pageSize": page_size}
        body.update({k: v for k, v in params.items() if v is not None})
        return self._post("/dld-fetch", body)


def build_client():
    proxy_url = os.environ.get("DLD_PROXY_URL", "").strip()
    proxy_secret = os.environ.get("DLD_PROXY_SECRET", "").strip()
    if proxy_url and proxy_secret:
        print(f"[scrape-dld] PROXY mode → {proxy_url}", file=sys.stderr)
        return ProxyClient(proxy_url, proxy_secret), "proxy"

    base = os.environ.get("DLD_BASE_URL", "https://stg-apis.data.dubai").strip()
    app_id = os.environ.get("DLD_APP_ID", "").strip()
    cid = os.environ.get("DLD_CLIENT_ID", "").strip()
    csec = os.environ.get("DLD_CLIENT_SECRET", "").strip()
    if app_id and cid and csec:
        print(f"[scrape-dld] DIRECT mode → {base} (UAE IP required)", file=sys.stderr)
        return DirectClient(base, app_id, cid, csec), "direct"
    return None, None


# ============ AGGREGATION ============

def fetch_all_pages(client, entity: str, dataset: str, max_rows: int = 200_000):
    rows = []
    page = 1
    while True:
        data = client.fetch_page(entity, dataset, page=page, page_size=1000)
        chunk = data.get("results") or data.get("data") or []
        if not isinstance(chunk, list):
            break
        rows.extend(chunk)
        if len(chunk) < 1000 or len(rows) >= max_rows:
            break
        page += 1
        time.sleep(1.05)  # ≈60 req/min limit
    return rows


def parse_date_ts(s):
    if not s:
        return 0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        try:
            return datetime.strptime(str(s), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return 0


def aggregate_area(dld_areas: list, txs: list, rents: list) -> dict:
    area_set = set(dld_areas)
    area_tx = [t for t in txs if t.get("area_name_en") in area_set or t.get("area_name") in area_set]
    if not area_tx:
        return {}

    now = datetime.now(timezone.utc)
    cutoff_12 = now.timestamp() - 365 * 86400
    cutoff_24 = now.timestamp() - 730 * 86400
    cutoff_5 = now.timestamp() - 365 * 86400 * 5

    def avg_psf(rows):
        psfs = []
        for r in rows:
            try:
                amt = float(r.get("trans_value") or r.get("actual_worth") or r.get("amount") or 0)
                sqft = float(r.get("procedure_area") or r.get("actual_area") or r.get("area_size") or 0)
                if amt > 0 and sqft > 100:
                    psfs.append(amt / sqft)
            except (TypeError, ValueError):
                continue
        if not psfs:
            return None
        psfs.sort()
        trim = max(1, len(psfs) // 10)
        psfs = psfs[trim:-trim] if len(psfs) > 2 * trim else psfs
        return sum(psfs) / len(psfs)

    def date_of(r):
        return parse_date_ts(r.get("instance_date") or r.get("transaction_date") or r.get("date"))

    last12 = [t for t in area_tx if date_of(t) > cutoff_12]
    prev12 = [t for t in area_tx if cutoff_24 < date_of(t) <= cutoff_12]
    near5  = [t for t in area_tx if cutoff_5 - 86400*90 < date_of(t) < cutoff_5 + 86400*90]

    def type_filter(rows, kind):
        return [r for r in rows if (r.get("property_sub_type_en") or r.get("property_type") or "").lower().startswith(kind)]

    apt_now   = avg_psf(type_filter(last12, "flat"))
    villa_now = avg_psf(type_filter(last12, "villa"))
    apt_prev  = avg_psf(type_filter(prev12, "flat"))
    apt_5yr   = avg_psf(type_filter(near5,  "flat"))

    def pct(now, then):
        if not now or not then:
            return None
        return round((now - then) / then * 100, 1)

    # Rentals
    area_rent = [r for r in rents if r.get("area_name_en") in area_set or r.get("area_name") in area_set]
    last12_rent = [r for r in area_rent if parse_date_ts(r.get("registration_date")) > cutoff_12]
    apt_rents = [float(r.get("annual_amount") or 0) for r in last12_rent if (r.get("property_sub_type_en") or "").lower().startswith("flat")]
    apt_rents = [v for v in apt_rents if v > 0]

    apt_yield = None
    if apt_rents and apt_now:
        apt_avg_rent = sum(apt_rents) / len(apt_rents)
        apt_flat_rows = type_filter(last12, "flat")
        sqfts = [float(t.get("procedure_area") or 0) for t in apt_flat_rows if t.get("procedure_area")]
        if sqfts:
            apt_avg_sqft = sum(sqfts) / len(sqfts)
            apt_avg_price = apt_avg_sqft * apt_now
            if apt_avg_price > 0:
                apt_yield = round(apt_avg_rent / apt_avg_price * 100, 1)

    out = {}
    if apt_now:   out.setdefault("avgPriceSqftAed", {})["apartment"] = round(apt_now)
    if villa_now: out.setdefault("avgPriceSqftAed", {})["villa"] = round(villa_now)
    yoy = pct(apt_now, apt_prev)
    yr5 = pct(apt_now, apt_5yr)
    if yoy is not None: out["priceSqftYoYPct"] = yoy
    if yr5 is not None: out["priceSqft5yrPct"] = yr5
    if apt_yield:       out.setdefault("avgRentalYieldPct", {})["apartment"] = apt_yield
    if last12:          out["txCount12mo"] = len(last12)
    out["lastComputed"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


# ============ MOCK ============

def run_mock() -> dict:
    print("[scrape-dld] MOCK mode — set DLD_PROXY_URL+DLD_PROXY_SECRET to go live", file=sys.stderr)
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base = {
        "palmJumeirah":      {"avgPriceSqftAed": {"apartment": 3812, "villa": 5240}, "priceSqftYoYPct": 9.4, "priceSqft5yrPct": 108.2, "avgRentalYieldPct": {"apartment": 6.2, "villa": 4.7}, "txCount12mo": 1742},
        "dubaiCreekHarbour": {"avgPriceSqftAed": {"apartment": 2680}, "priceSqftYoYPct": 14.6, "priceSqft5yrPct": 78.4, "avgRentalYieldPct": {"apartment": 6.8}, "txCount12mo": 3924},
        "zaabeel":           {"avgPriceSqftAed": {"apartment": 4350}, "priceSqftYoYPct": 11.8, "priceSqft5yrPct": 95.7, "avgRentalYieldPct": {"apartment": 5.6}, "txCount12mo": 187},
        "palmJebelAli":      {"avgPriceSqftAed": {"villa": 2780},     "priceSqftYoYPct": 32.5,                          "txCount12mo": 2105},
        "dubaiHills":        {"avgPriceSqftAed": {"apartment": 2095, "villa": 2480}, "priceSqftYoYPct": 11.3, "priceSqft5yrPct": 92.8, "avgRentalYieldPct": {"apartment": 6.1, "villa": 4.5}, "txCount12mo": 5320},
        "businessBay":       {"avgPriceSqftAed": {"apartment": 2280}, "priceSqftYoYPct": 12.4, "priceSqft5yrPct": 88.6, "avgRentalYieldPct": {"apartment": 7.2}, "txCount12mo": 5687},
        "downtownDubai":     {"avgPriceSqftAed": {"apartment": 3275}, "priceSqftYoYPct": 14.7, "priceSqft5yrPct": 102.3, "avgRentalYieldPct": {"apartment": 5.7}, "txCount12mo": 4108},
        "dubaiMarina":       {"avgPriceSqftAed": {"apartment": 2185}, "priceSqftYoYPct": 11.5, "priceSqft5yrPct": 86.2, "avgRentalYieldPct": {"apartment": 6.6}, "txCount12mo": 6320},
        "jvc":               {"avgPriceSqftAed": {"apartment": 1180, "villa": 1620}, "priceSqftYoYPct": 14.8, "priceSqft5yrPct": 96.8, "avgRentalYieldPct": {"apartment": 8.1, "villa": 5.9}, "txCount12mo": 11240},
        "jbr":               {"avgPriceSqftAed": {"apartment": 2640}, "priceSqftYoYPct": 11.9, "priceSqft5yrPct": 88.7, "avgRentalYieldPct": {"apartment": 6.0}, "txCount12mo": 1685},
        "damacHills":        {"avgPriceSqftAed": {"apartment": 1450, "villa": 1820}, "priceSqftYoYPct": 10.8, "priceSqft5yrPct": 81.4, "avgRentalYieldPct": {"apartment": 6.7, "villa": 5.4}, "txCount12mo": 2780},
        "bluewatersIsland":  {"avgPriceSqftAed": {"apartment": 4020}, "priceSqftYoYPct": 14.2, "priceSqft5yrPct": 108.5, "avgRentalYieldPct": {"apartment": 6.1}, "txCount12mo": 372},
        "difc":              {"avgPriceSqftAed": {"apartment": 3580}, "priceSqftYoYPct": 12.5, "priceSqft5yrPct": 99.4, "avgRentalYieldPct": {"apartment": 5.7}, "txCount12mo": 218},
        "sobhaHartland":     {"avgPriceSqftAed": {"apartment": 2150, "villa": 2780}, "priceSqftYoYPct": 11.4, "priceSqft5yrPct": 82.5, "avgRentalYieldPct": {"apartment": 6.0, "villa": 4.8}, "txCount12mo": 3450},
        "mbrCity1":          {"avgPriceSqftAed": {"apartment": 2080, "villa": 3120}, "priceSqftYoYPct": 11.7, "priceSqft5yrPct": 96.8, "avgRentalYieldPct": {"apartment": 5.9, "villa": 4.5}, "txCount12mo": 4180},
        "dubaiHarbour":      {"avgPriceSqftAed": {"apartment": 3420}, "priceSqftYoYPct": 16.2, "priceSqft5yrPct": 99.5, "avgRentalYieldPct": {"apartment": 6.3}, "txCount12mo": 1220},
        "minaRashid":        {"avgPriceSqftAed": {"apartment": 2580}, "priceSqftYoYPct": 14.8, "priceSqft5yrPct": 68.2, "avgRentalYieldPct": {"apartment": 6.7}, "txCount12mo": 1790},
        "tilalAlGhaf":       {"avgPriceSqftAed": {"apartment": 1620, "villa": 2280}, "priceSqftYoYPct": 12.2, "priceSqft5yrPct": 89.4, "avgRentalYieldPct": {"apartment": 6.0, "villa": 5.0}, "txCount12mo": 2480}
    }
    for v in base.values():
        v["lastComputed"] = iso
    return base


def run_live(client) -> dict:
    print(f"[scrape-dld] fetching {DLD_ENTITY}/{DLD_TX_DATASET} …", file=sys.stderr)
    txs = fetch_all_pages(client, DLD_ENTITY, DLD_TX_DATASET)
    print(f"[scrape-dld] {len(txs)} transactions", file=sys.stderr)
    print(f"[scrape-dld] fetching {DLD_ENTITY}/{DLD_RENT_DATASET} …", file=sys.stderr)
    try:
        rents = fetch_all_pages(client, DLD_ENTITY, DLD_RENT_DATASET)
        print(f"[scrape-dld] {len(rents)} rental contracts", file=sys.stderr)
    except urllib.error.HTTPError as e:
        print(f"[scrape-dld] rental dataset unavailable ({e.code}); yields will be missing", file=sys.stderr)
        rents = []

    out = {}
    for k, dld_areas in CURATED_TO_DLD_AREA.items():
        agg = aggregate_area(dld_areas, txs, rents)
        if agg:
            out[k] = agg
    return out


# ============ MAIN ============

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mock", action="store_true", help="Force mock mode")
    p.add_argument("--health", action="store_true", help="Just run the health-check and exit")
    p.add_argument("--output", default=str(Path(__file__).parent.parent / "live-areas.json"))
    args = p.parse_args()

    client, mode = (None, None) if args.mock else build_client()

    if args.health:
        if not client:
            print("No credentials available for health check.", file=sys.stderr)
            sys.exit(2)
        result = client.health()
        print(json.dumps(result, indent=2))
        return

    try:
        if client and not args.mock:
            live_areas = run_live(client)
            note = f"{mode} fetch from data.dubai DDA iPaaS"
        else:
            live_areas = run_mock()
            note = "mock (set DLD_PROXY_URL+SECRET to go live)"
    except Exception as e:
        print(f"[scrape-dld] live fetch failed ({e}); falling back to mock", file=sys.stderr)
        live_areas = run_mock()
        note = f"mock-fallback (live error: {e})"
        mode = "mock-fallback"

    out = {
        "_meta": {
            "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": mode or "mock",
            "source": note,
            "schemaVersion": 2,
            "entity": DLD_ENTITY,
            "transactionsDataset": DLD_TX_DATASET,
            "rentalsDataset": DLD_RENT_DATASET
        },
        **live_areas,
    }
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"[scrape-dld] wrote {args.output} ({len(live_areas)} areas, mode={mode or 'mock'})", file=sys.stderr)


if __name__ == "__main__":
    main()
