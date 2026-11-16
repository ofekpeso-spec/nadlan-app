"""Thin client for Govmap's public real-estate API (Israel Tax Authority deal data).

Flow (documented by the open-source nadlan-skill / nadlan-mcp projects):
  1. POST /search-service/autocomplete          -> address -> point (x, y)
  2. GET  /real-estate/deals/{x},{y}/{radius}    -> nearby building polygons (polygon_id), NOT deals
  3. GET  /real-estate/street-deals/{polygon_id} -> actual recorded deals
     GET  /real-estate/neighborhood-deals/{id}   -> wider-area fallback

Govmap publishes no formal spec, so the client tries a few request variants
(header sets, coordinate rounding) when it gets HTTP 403 and remembers what worked.
`GovmapClient.diagnostics` records every attempt for the /selftest page.
"""
from __future__ import annotations

import math
import os
import time
from datetime import date
from typing import Any

import requests

BASE_URL = os.environ.get("GOVMAP_BASE_URL", "https://www.govmap.gov.il/api").rstrip("/")
TIMEOUT = float(os.environ.get("GOVMAP_TIMEOUT", "25"))
CACHE_TTL = int(os.environ.get("GOVMAP_CACHE_TTL", "21600"))  # 6 hours

HEADER_SETS = [
    {"User-Agent": "NadlanMCP/1.0.0", "Accept": "application/json"},
    {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
     "Accept": "application/json, text/plain, */*", "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
     "Referer": "https://www.govmap.gov.il/", "Origin": "https://www.govmap.gov.il"},
    {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
     "Accept": "application/json, text/plain, */*", "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
     "Referer": "https://www.govmap.gov.il/?c=0,0&z=0"},
]


class GovmapError(RuntimeError):
    pass


class GovmapClient:
    def __init__(self, base_url: str = BASE_URL, session: requests.Session | None = None):
        self.base_url = base_url
        self.s = session or requests.Session()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._good_headers = 0
        self.diagnostics: list[dict] = []

    # ---- low level -------------------------------------------------------
    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: Any = None) -> Any:
        key = f"{method} {path} {params} {json}"
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        url = f"{self.base_url}{path}"
        order = [self._good_headers] + [i for i in range(len(HEADER_SETS)) if i != self._good_headers]
        last = "no attempt"
        for hi in order:
            for attempt in range(2):
                try:
                    r = self.s.request(method, url, params=params, json=json,
                                       headers=HEADER_SETS[hi], timeout=TIMEOUT)
                except requests.RequestException as e:
                    last = f"network error: {e.__class__.__name__}"
                    self._log(method, path, hi, None, last)
                    time.sleep(1 + attempt)
                    continue
                self._log(method, path, hi, r.status_code)
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except ValueError:
                        last = "200 but response is not JSON"
                        break
                    self._good_headers = hi
                    self._cache[key] = (time.time(), data)
                    return data
                last = f"HTTP {r.status_code}"
                if r.status_code in (403, 401, 404, 400):
                    break  # try next header set
                time.sleep(1 + attempt)  # 429 / 5xx: back off and retry
        raise GovmapError(f"{method} {path} failed ({last})")

    def _log(self, method: str, path: str, header_set: int, status: int | None, note: str = "") -> None:
        self.diagnostics.append({"call": f"{method} {path}", "headers": header_set,
                                 "status": status, **({"note": note} if note else {})})
        del self.diagnostics[:-30]

    @staticmethod
    def _as_list(data: Any) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "results", "items", "deals", "value"):
                v = data.get(k)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):
                    inner = GovmapClient._as_list(v)
                    if inner:
                        return inner
        return []

    # ---- public ----------------------------------------------------------
    def autocomplete(self, text: str, max_results: int = 5) -> list[dict]:
        """Address search. Returns [{'name': str, 'point': [x, y], 'type': str}...]."""
        data = self._request("POST", "/search-service/autocomplete",
                             json={"searchText": text, "language": "he",
                                   "isAccurate": False, "maxResults": max(max_results, 5)})
        out = []
        for item in self._as_list(data):
            name = item.get("text") or item.get("displayName") or item.get("name")
            xy = (_parse_point(item.get("shape")) or _parse_point(item.get("point"))
                  or _parse_point([item.get("x"), item.get("y")]))
            if name and xy:
                out.append({"name": name, "point": xy, "type": item.get("type") or item.get("resultType")})
        return out[:max_results]

    def polygons_near(self, point: list[float], radius_m: int = 50) -> list[dict]:
        """Building polygons near a point (step 2). Tries raw then rounded coordinates."""
        x, y = point
        last_err: Exception | None = None
        for fx, fy in ((x, y), (round(x, 2), round(y, 2)), (int(round(x)), int(round(y)))):
            try:
                return self._as_list(self._request("GET", f"/real-estate/deals/{fx},{fy}/{int(radius_m)}"))
            except GovmapError as e:
                last_err = e
        raise last_err or GovmapError("polygon lookup failed")

    def street_deals(self, polygon_id: str, *, limit: int = 100, deal_type: int | None = 2,
                     start: str | None = None, end: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if deal_type:
            params["dealType"] = deal_type
        if start:
            params["startDate"] = start
        if end:
            params["endDate"] = end
        return self._as_list(self._request("GET", f"/real-estate/street-deals/{polygon_id}", params=params))

    def neighborhood_deals(self, polygon_id: str, *, limit: int = 100, deal_type: int | None = 2,
                           start: str | None = None, end: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if deal_type:
            params["dealType"] = deal_type
        if start:
            params["startDate"] = start
        if end:
            params["endDate"] = end
        return self._as_list(self._request("GET", f"/real-estate/neighborhood-deals/{polygon_id}", params=params))

    def deals_by_radius(self, point: list[float], radius_m: int = 300, *, months_back: int = 36,
                        max_polygons: int = 8, include_new: bool = True) -> list[dict]:
        """Address point -> raw deal records (steps 2+3), deduplicated."""
        polys: list[dict] = []
        for r in sorted({min(radius_m, 50), min(radius_m, 200), radius_m}):
            polys = self.polygons_near(point, r)
            if polys:
                break
        if polys and any(k in polys[0] for k in ("dealAmount", "deal_amount")):
            return polys  # API returned deals directly
        x0, y0 = point

        def dist(p: dict) -> float:
            px, py = _num(p.get("longitude") or p.get("x")), _num(p.get("latitude") or p.get("y"))
            return math.hypot(px - x0, py - y0) if px is not None and py is not None else 1e9

        ids, seen_ids = [], set()
        for p in sorted(polys, key=dist):
            pid = p.get("polygon_id") or p.get("polygonId") or p.get("id")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                ids.append(str(pid))
        ids = ids[:max_polygons]
        today = date.today()
        start = f"{today.year - (months_back // 12) - 1}-{today.month:02d}"
        end = f"{today.year}-{today.month:02d}"
        types = [2, 1] if include_new else [2]
        out, seen = [], set()
        for pid in ids:
            for t in types:
                try:
                    batch = self.street_deals(pid, limit=200, deal_type=t, start=start, end=end)
                except GovmapError:
                    continue
                for d in batch:
                    k = (d.get("objectid"), d.get("dealDate"), d.get("dealAmount"))
                    if k not in seen:
                        seen.add(k)
                        out.append(d)
        if len(out) < 10 and ids:
            try:
                for d in self.neighborhood_deals(ids[0], limit=200, deal_type=2, start=start, end=end):
                    k = (d.get("objectid"), d.get("dealDate"), d.get("dealAmount"))
                    if k not in seen:
                        seen.add(k)
                        out.append(d)
            except GovmapError:
                pass
        return out


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_point(pt: Any) -> list[float] | None:
    """Accept [x,y], {'x':..,'y':..}, or WKT 'POINT(x y)'."""
    try:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2 and pt[0] is not None:
            return [float(pt[0]), float(pt[1])]
        if isinstance(pt, dict):
            if "x" in pt and "y" in pt:
                return [float(pt["x"]), float(pt["y"])]
            if "coordinates" in pt:
                return _parse_point(pt["coordinates"])
        if isinstance(pt, str) and "POINT" in pt.upper():
            inner = pt[pt.index("(") + 1: pt.index(")")].split()
            return [float(inner[0]), float(inner[1])]
    except (ValueError, TypeError, IndexError):
        return None
    return None
