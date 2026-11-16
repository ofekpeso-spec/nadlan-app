"""Thin client for Govmap's public real-estate API (Israel Tax Authority deal data).

Endpoint paths follow the open-source nitzpo/nadlan-mcp project. Govmap does not
publish a formal spec, so request/response shapes are handled defensively and the
base URL is configurable. Run `python -m nadlan_app.smoke_test` once on a machine
with internet access to confirm the live API still matches.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

BASE_URL = os.environ.get("GOVMAP_BASE_URL", "https://www.govmap.gov.il/api").rstrip("/")
TIMEOUT = float(os.environ.get("GOVMAP_TIMEOUT", "20"))
USER_AGENT = os.environ.get("GOVMAP_USER_AGENT", "Mozilla/5.0 (compatible; nadlan-app/0.1)")
CACHE_TTL = int(os.environ.get("GOVMAP_CACHE_TTL", "21600"))  # 6 hours


class GovmapError(RuntimeError):
    pass


class GovmapClient:
    def __init__(self, base_url: str = BASE_URL, session: requests.Session | None = None):
        self.base_url = base_url
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json",
                               "Origin": "https://www.govmap.gov.il",
                               "Referer": "https://www.govmap.gov.il/"})
        self._cache: dict[str, tuple[float, Any]] = {}

    # ---- low level -------------------------------------------------------
    def _request(self, method: str, path: str, **kw) -> Any:
        key = f"{method} {path} {kw.get('json')}"
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        url = f"{self.base_url}{path}"
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                r = self.s.request(method, url, timeout=TIMEOUT, **kw)
                if r.status_code in (429, 502, 503, 504):
                    raise GovmapError(f"Govmap busy (HTTP {r.status_code})")
                r.raise_for_status()
                data = r.json()
                self._cache[key] = (time.time(), data)
                return data
            except (requests.RequestException, ValueError, GovmapError) as e:
                last_err = e
                time.sleep(0.5 * (2 ** attempt))
        raise GovmapError(f"Govmap request failed: {last_err}")

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
        """Address search. Returns [{'name': str, 'point': [x, y]}...]."""
        data = self._request("POST", "/search-service/autocomplete",
                             json={"searchText": text, "language": "he",
                                   "isAccurate": False, "maxResults": max_results})
        out = []
        for item in self._as_list(data):
            name = item.get("displayName") or item.get("text") or item.get("name")
            pt = item.get("point") or item.get("shape") or item.get("coordinates")
            xy = _parse_point(pt) or _parse_point([item.get("x"), item.get("y")])
            if name and xy:
                out.append({"name": name, "point": xy, "type": item.get("type") or item.get("resultType")})
        return out[:max_results]

    def deals_by_radius(self, point: list[float], radius_m: int = 300) -> list[dict]:
        x, y = point
        data = self._request("GET", f"/real-estate/deals/{x},{y}/{int(radius_m)}")
        return self._as_list(data)

    def street_deals(self, polygon_id: str) -> list[dict]:
        return self._as_list(self._request("GET", f"/real-estate/street-deals/{polygon_id}"))

    def neighborhood_deals(self, polygon_id: str) -> list[dict]:
        return self._as_list(self._request("GET", f"/real-estate/neighborhood-deals/{polygon_id}"))


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
