"""Client for data.gov.il (CKAN API) — Israel's official open-data portal.

All datasets used here are published as open government data. Field names differ
per dataset and are not documented consistently, so records are returned mostly
as-is (minus CKAN internals) and filtered with tolerant text matching.
"""
from __future__ import annotations

import json as _json
import os
import time
from typing import Any

import requests

BASE_URL = os.environ.get("DATAGOV_BASE_URL", "https://data.gov.il/api/3/action").rstrip("/")
TIMEOUT = float(os.environ.get("DATAGOV_TIMEOUT", "25"))
CACHE_TTL = int(os.environ.get("DATAGOV_CACHE_TTL", "43200"))  # 12 hours

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HEADER_SETS = [
    {"User-Agent": "NadlanIL/0.2 (+https://github.com/ofekpeso-spec/nadlan-app)", "Accept": "application/json"},
    {"User-Agent": _BROWSER_UA, "Accept": "application/json, text/plain, */*",
     "Accept-Language": "he-IL,he;q=0.9,en;q=0.8", "Referer": "https://data.gov.il/"},
]

# Open datasets on data.gov.il (resource IDs from the public catalogue).
DATASETS: dict[str, dict[str, str]] = {
    "urban_renewal": {"resource_id": "f65a0daf-f737-49c5-9424-d378d52104f5",
                      "title": "Urban renewal (פינוי-בינוי) declared complexes"},
    "construction_sites": {"resource_id": "b072e36c-a53b-49e1-be08-4a608fcf4638",
                           "title": "Active construction sites"},
    "mechir_lamishtaken": {"resource_id": "7c8255d0-49ef-49db-8904-4cf917586031",
                           "title": "Mechir LaMishtaken / Dira BeHanacha lotteries"},
    "housing_pipeline": {"resource_id": "99aad98f-2b54-4eea-834d-650b56389bf3",
                         "title": "Housing supply / development pipeline"},
}

_INTERNAL = {"_id", "_full_text", "rank", "_rank"}

# Fields worth showing per dataset (verified against live data.gov.il, Nov 2026).
# Unknown datasets / renamed fields fall back to returning the full record.
KEY_FIELDS: dict[str, list[str]] = {
    "urban_renewal": ["Yeshuv", "ShemMitcham", "YachadKayam", "YachadTosafti", "YachadMutza",
                      "TaarichHachraza", "Maslul", "Status", "Bebitzua", "ShnatMatanTokef",
                      "MisparTochnit", "KishurLatar", "KishurLaMapa"],
    "construction_sites": ["site_name", "city_name", "executor_name", "build_types", "has_cranes",
                           "safety_warrents", "sanctions", "sanctions_sum"],
    "mechir_lamishtaken": ["ProjectName", "LamasName", "Neighborhood", "PriceForMeter",
                           "LotteryHousingUnits", "Subscribers", "Winners", "LotteryExecutionDate",
                           "LotteryEndSignupDate", "ProjectStatus", "LotteryStatusValue",
                           "ProviderName", "Eligibility", "MarketingMethodDesc"],
}


def _lottery_date(rec: dict) -> str:
    """'5/19/2019 10:57:39 AM' -> '2019-05-19' for sorting (unknown formats sort last)."""
    from datetime import datetime
    v = str(rec.get("LotteryExecutionDate") or "").strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


SORTERS = {"mechir_lamishtaken": _lottery_date}


def slim(dataset: str, rec: dict) -> dict:
    keys = KEY_FIELDS.get(dataset)
    if not keys:
        return rec
    out = {k: rec[k] for k in keys if k in rec}
    return out if len(out) >= 2 else rec  # schema changed -> don't hide data


class DataGovError(RuntimeError):
    pass


class DataGovClient:
    def __init__(self, base_url: str = BASE_URL, session: requests.Session | None = None):
        self.base_url = base_url
        self.s = session or requests.Session()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._good_headers = 0
        self.diagnostics: list[dict] = []

    def _get(self, action: str, params: dict) -> Any:
        key = f"{action} {sorted(params.items())}"
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        url = f"{self.base_url}/{action}"
        order = [self._good_headers] + [i for i in range(len(HEADER_SETS)) if i != self._good_headers]
        last = "no attempt"
        for hi in order:
            for attempt in range(2):
                try:
                    r = self.s.request("GET", url, params=params, headers=HEADER_SETS[hi], timeout=TIMEOUT)
                except requests.RequestException as e:
                    last = f"network error: {e.__class__.__name__}"
                    self._log(action, hi, None, last)
                    time.sleep(1 + attempt)
                    continue
                self._log(action, hi, r.status_code)
                if r.status_code == 200:
                    try:
                        body = r.json()
                    except ValueError:
                        last = "200 but not JSON (possibly a bot-protection page)"
                        break
                    if isinstance(body, dict) and body.get("success") is False:
                        raise DataGovError(f"data.gov.il error: {body.get('error')}")
                    result = body.get("result", body) if isinstance(body, dict) else body
                    self._good_headers = hi
                    self._cache[key] = (time.time(), result)
                    return result
                last = f"HTTP {r.status_code}"
                if r.status_code in (400, 401, 403, 404):
                    break
                time.sleep(1 + attempt)
        raise DataGovError(f"{action} failed ({last})")

    def _log(self, action: str, header_set: int, status: int | None, note: str = "") -> None:
        self.diagnostics.append({"call": action, "headers": header_set, "status": status,
                                 **({"note": note} if note else {})})
        del self.diagnostics[:-30]

    # ---- public ----------------------------------------------------------
    def search(self, resource_id: str, q: str | None = None, limit: int = 100,
               filters: dict | None = None) -> dict:
        params: dict[str, Any] = {"resource_id": resource_id, "limit": limit, "include_total": "true"}
        if q:
            params["q"] = q
        if filters:
            params["filters"] = _json.dumps(filters, ensure_ascii=False)
        res = self._get("datastore_search", params)
        records = [clean(r) for r in res.get("records", [])]
        return {"records": records, "total": res.get("total", len(records)),
                "fields": [f.get("id") for f in res.get("fields", []) if f.get("id") not in _INTERNAL]}

    def find_datasets(self, query: str, rows: int = 8) -> list[dict]:
        res = self._get("package_search", {"q": query, "rows": rows})
        out = []
        for p in res.get("results", []):
            out.append({
                "dataset": p.get("name"), "title": p.get("title"),
                "organization": (p.get("organization") or {}).get("title"),
                "updated": (p.get("metadata_modified") or "")[:10],
                "description": (p.get("notes") or "")[:300],
                "queryable_resources": [
                    {"resource_id": r.get("id"), "name": r.get("name"), "format": r.get("format")}
                    for r in p.get("resources", []) if r.get("datastore_active")
                ],
            })
        return out


def clean(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in _INTERNAL and v not in (None, "", " ")}


def _norm(s: str) -> str:
    s = str(s)
    for ch in "\"'`׳״-–_,.()":
        s = s.replace(ch, " ")
    for prefix in ("רחוב ", "רח ", "שדרות ", "שד "):
        s = s.replace(prefix, " ")
    return " ".join(s.split()).lower()


def matches(rec: dict, *terms: str | None) -> bool:
    """True if every non-empty term appears (normalized) somewhere in the record's values."""
    blob = _norm(" ".join(str(v) for v in rec.values()))
    for t in terms:
        if t and _norm(t) not in blob:
            # tolerate "תל אביב" vs "תל אביב יפו", "קריית" vs "קרית"
            alt = _norm(t).replace("קריית", "קרית").replace("יי", "י")
            if alt not in blob.replace("קריית", "קרית").replace("יי", "י"):
                return False
    return True


def lookup(client: DataGovClient, dataset: str, city: str, street: str | None = None,
           limit: int = 20) -> dict:
    """City (+optional street) lookup with server-side full-text search and a client-side fallback."""
    rid = DATASETS[dataset]["resource_id"]
    title = DATASETS[dataset]["title"]
    city_recs: list[dict] = []
    for q in ([f"{city} {street}"] if street else []) + [city]:
        res = client.search(rid, q=q, limit=500)
        recs = [r for r in res["records"] if matches(r, city, street)]
        if dataset in SORTERS:
            recs.sort(key=SORTERS[dataset], reverse=True)  # newest first
        if recs:
            return {"dataset": title, "query": {"city": city, "street": street}, "scope": "street" if street else "city",
                    "matches": len(recs), "records": [slim(dataset, r) for r in recs[:limit]]}
        if q == city:
            city_recs = [r for r in res["records"] if matches(r, city)]
    if street and city_recs:
        return {"dataset": title, "query": {"city": city, "street": street}, "scope": "city",
                "note": f"No records mention '{street}'. Showing records for {city} instead.",
                "matches": len(city_recs), "records": [slim(dataset, r) for r in city_recs[:limit]]}
    return {"dataset": title, "query": {"city": city, "street": street}, "scope": None,
            "matches": 0, "records": []}
