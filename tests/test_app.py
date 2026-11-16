import asyncio
import json
import random
from datetime import date, timedelta

import pytest

from nadlan_app import analytics, server

TODAY = date.today()


def make_raw(n=60, seed=1):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        days = rnd.randint(0, 900)
        rooms = rnd.choice([3, 3.5, 4, 4.5, 5])
        sqm = rooms * 22 + rnd.randint(-8, 8)
        # ~5%/yr appreciation, base 40k/sqm
        ppsqm = 40000 * (1 - 0.05 * days / 365) * rnd.uniform(0.9, 1.1)
        out.append({
            "dealDate": (TODAY - timedelta(days=days)).isoformat() + "T00:00:00.000Z",
            "dealAmount": f"{int(ppsqm * sqm):,}",
            "assetArea": sqm, "assetRoomNum": rooms, "floorNo": str(rnd.randint(0, 9)),
            "address_description": f"הרצל {rnd.randint(1, 40)}", "settlementNameHeb": "תל אביב-יפו",
        })
    out.append({"dealDate": None, "dealAmount": 100})          # junk -> dropped
    out.append({"dealDate": "2024-01-01", "dealAmount": "0"})  # zero -> dropped
    out.append(dict(out[0]))                                   # duplicate -> dropped
    return out


class FakeGovmap:
    def __init__(self, raw):
        self.raw = raw

    def autocomplete(self, text, max_results=5):
        return [] if "nowhere" in text else [{"name": "הרצל 10, תל אביב-יפו", "point": [180000.0, 663000.0]}]

    def polygons_near(self, point, radius_m=50):
        return [{"polygon_id": "p1"}]

    def deals_by_radius(self, point, radius_m=300):
        return self.raw


def test_normalize_drops_junk_and_dupes():
    deals = analytics.normalize_deals(make_raw())
    assert len(deals) == 60
    d = deals[0]
    assert d["price"] > 0 and d["price_per_sqm"] and d["date"] <= TODAY.isoformat()
    assert deals == sorted(deals, key=lambda x: x["date"], reverse=True)


def test_alternate_keys_and_formats():
    d = analytics.normalize_deal({"deal_date": "15/03/2025", "price": "₪2,500,000", "area": "100", "rooms": "4"})
    assert d["date"] == "2025-03-15" and d["price"] == 2500000 and d["price_per_sqm"] == 25000


def test_summary_and_trend_positive():
    s = analytics.summarize(analytics.normalize_deals(make_raw(200)))
    assert s["deal_count"] == 200
    assert s["price_per_sqm"]["p25"] <= s["price_per_sqm"]["median"] <= s["price_per_sqm"]["p75"]
    assert s["trend_12m"]["change_pct"] > 0  # synthetic data appreciates


def test_filter_rooms_and_months():
    deals = analytics.normalize_deals(make_raw(200))
    f = analytics.filter_deals(deals, rooms=4, months_back=12)
    assert f and all(d["rooms"] == 4 for d in f)
    cutoff = analytics._months_ago(TODAY, 12).isoformat()
    assert all(d["date"] >= cutoff for d in f)


def test_estimate_value_reasonable():
    est = analytics.estimate_value(analytics.normalize_deals(make_raw(200)), sqm=90, rooms=4)
    assert est["ok"]
    e = est["estimate"]
    assert e["low"] <= e["mid"] <= e["high"]
    assert 2_800_000 < e["mid"] < 3_900_000  # ~38-40k/sqm × 90


def test_estimate_too_few():
    assert analytics.estimate_value([], sqm=80)["ok"] is False


@pytest.fixture
def fake():
    server.set_client(FakeGovmap(make_raw(200)))
    yield
    server.set_client(None)


def _call(name, args):
    res = asyncio.run(server.mcp.call_tool(name, args))
    # MCPServer may return (content, structured) or a result object; extract the dict
    if isinstance(res, tuple):
        structured = res[1]
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    if hasattr(res, "structured_content") and res.structured_content:
        sc = res.structured_content
        return sc.get("result", sc)
    if hasattr(res, "structuredContent") and res.structuredContent:
        sc = res.structuredContent
        return sc.get("result", sc)
    content = res.content if hasattr(res, "content") else res
    return json.loads(content[0].text)


def test_tools_listed():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"search_address", "recent_deals", "area_price_summary", "estimate_apartment_value"}


def test_tool_calls(fake):
    r = _call("recent_deals", {"address": "הרצל 10 תל אביב", "rooms": 4, "limit": 5})
    assert r["ok"] and len(r["deals"]) == 5 and "Tax Authority" in r["source"]
    s = _call("area_price_summary", {"address": "הרצל 10 תל אביב"})
    assert s["ok"] and s["summary"]["deal_count"] > 0
    e = _call("estimate_apartment_value", {"address": "הרצל 10 תל אביב", "sqm": 90, "rooms": 4})
    assert e["ok"] and e["estimate"]["mid"] > 0
    n = _call("recent_deals", {"address": "nowhere"})
    assert n["ok"] is False and "not found" in n["error"]


# ---- GovmapClient against a fake HTTP session (no network) -------------------
class _Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeSession:
    """Blocks header set 0 on real-estate paths with 403, like a WAF might."""
    def __init__(self):
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, headers.get("User-Agent")))
        if "autocomplete" in url:
            return _Resp(200, {"resultsCount": 1, "results": [
                {"text": "הרצל 10, תל אביב-יפו", "type": "address", "shape": "POINT(3870574.19 3771516.47)"}]})
        if "NadlanMCP" in headers.get("User-Agent", ""):
            return _Resp(403)
        if "/real-estate/deals/" in url:
            return _Resp(200, [{"polygon_id": "far", "longitude": 3871574, "latitude": 3771516},
                               {"polygon_id": "near", "longitude": 3870580, "latitude": 3771510}])
        if "/street-deals/" in url:
            pid = url.rsplit("/", 1)[1]
            return _Resp(200, {"data": [
                {"objectid": hash((pid, i, params.get("dealType"))) % 10**6, "dealAmount": 2_000_000 + i * 10_000,
                 "dealDate": f"2026-0{1 + i % 9}-15T00:00:00", "assetArea": 80 + i, "assetRoomNum": 3.5,
                 "floorNumber": i % 5, "streetNameHeb": "הרצל", "houseNum": str(10 + i),
                 "settlementNameHeb": "תל אביב-יפו", "propertyTypeDescription": "דירה"} for i in range(8)],
                "totalCount": 8})
        if "/neighborhood-deals/" in url:
            return _Resp(200, {"data": []})
        return _Resp(404)


def test_client_flow_with_403_fallback(monkeypatch):
    from nadlan_app import govmap
    monkeypatch.setattr(govmap.time, "sleep", lambda s: None)
    sess = FakeSession()
    c = govmap.GovmapClient(session=sess)
    hits = c.autocomplete("הרצל 10 תל אביב")
    assert hits[0]["point"] == [3870574.19, 3771516.47]
    raw = c.deals_by_radius(hits[0]["point"], 300)
    deals = analytics.normalize_deals(raw)
    assert len(deals) == 32  # 2 polygons x 2 deal types x 8
    assert deals[0]["address"].startswith("הרצל") and deals[0]["property_type"] == "דירה"
    # nearest polygon queried first
    street_calls = [u for _, u, _, _ in sess.calls if "street-deals" in u]
    assert street_calls[0].endswith("/near")
    # after the first 403 the client remembers the working header set
    assert c._good_headers != 0
    assert any(d["status"] == 403 for d in c.diagnostics)
