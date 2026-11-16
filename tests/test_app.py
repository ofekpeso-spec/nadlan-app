import asyncio
import json

import pytest

from nadlan_app import datagov, server

RENEWAL = [
    {"_id": 1, "Yeshuv": "תל אביב - יפו", "ShemMitcham": "סוקולוב-הרצל", "Ktovet": "רח' הרצל 10-20",
     "YachadKayam": 48, "YachadTosafti": 120, "Status": "בביצוע", "_full_text": "x"},
    {"_id": 2, "Yeshuv": "תל אביב - יפו", "ShemMitcham": "יד אליהו", "Ktovet": "שד' ההגנה 5",
     "YachadKayam": 60, "YachadTosafti": 200, "Status": "הוכרז"},
    {"_id": 3, "Yeshuv": "חולון", "ShemMitcham": "קוגל", "Ktovet": "קוגל 1", "YachadKayam": 30},
]
SITES = [{"_id": 1, "city": "תל אביב-יפו", "street": "הרצל", "builder": "א.ב. בנייה", "status": "פעיל"}]
LOTTERY = [{"_id": 1, "LamasName": "חולון", "PricePerMeter": 15500, "Units": 120}]
PIPELINE = [{"_id": 1, "Yishuv": "תל אביב-יפו", "Units": 3400}]

BY_RID = {
    datagov.DATASETS["urban_renewal"]["resource_id"]: RENEWAL,
    datagov.DATASETS["construction_sites"]["resource_id"]: SITES,
    datagov.DATASETS["mechir_lamishtaken"]["resource_id"]: LOTTERY,
    datagov.DATASETS["housing_pipeline"]["resource_id"]: PIPELINE,
}


class _Resp:
    def __init__(self, status, data=None, text_only=False):
        self.status_code, self._data, self._text_only = status, data, text_only

    def json(self):
        if self._text_only:
            raise ValueError("html")
        return self._data


class FakeSession:
    """Mimics CKAN. Rejects the first header set (like a bot filter) to exercise fallback."""

    def __init__(self, block_first=True):
        self.block_first, self.calls = block_first, []

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), headers.get("User-Agent")))
        if self.block_first and headers.get("User-Agent", "").startswith("NadlanIL"):
            return _Resp(200, text_only=True)  # HTML challenge page
        if url.endswith("/datastore_search"):
            rows = BY_RID.get(params["resource_id"])
            if rows is None:
                return _Resp(404)
            q = params.get("q")
            # crude full-text: every word must appear
            if q:
                rows = [r for r in rows if all(w in json.dumps(r, ensure_ascii=False) for w in q.split())]
            rows = rows[: int(params.get("limit", 100))]
            return _Resp(200, {"success": True, "result": {
                "records": rows, "total": len(rows),
                "fields": [{"id": "_id"}] + [{"id": k} for k in (rows[0] if rows else {}) if k != "_id"]}})
        if url.endswith("/package_search"):
            return _Resp(200, {"success": True, "result": {"results": [{
                "name": "kindergartens", "title": "גני ילדים", "organization": {"title": "משרד החינוך"},
                "metadata_modified": "2026-10-01T00:00", "notes": "רשימת גנים",
                "resources": [{"id": "rid-1", "name": "גנים", "format": "CSV", "datastore_active": True},
                              {"id": "rid-2", "name": "pdf", "format": "PDF", "datastore_active": False}]}]}})
        return _Resp(404)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(datagov.time, "sleep", lambda s: None)
    c = datagov.DataGovClient(session=FakeSession())
    server.set_client(c)
    yield c
    server.set_client(None)


def _call(name, args):
    res = asyncio.run(server.mcp.call_tool(name, args))
    if isinstance(res, tuple):
        sc = res[1]
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    for attr in ("structured_content", "structuredContent"):
        sc = getattr(res, attr, None)
        if sc:
            return sc.get("result", sc)
    return json.loads(res.content[0].text)


def test_matches_normalization():
    rec = {"a": "תל אביב - יפו", "b": "רח' הרצל 10"}
    assert datagov.matches(rec, "תל אביב", "הרצל")
    assert datagov.matches(rec, "תל אביב-יפו")
    assert not datagov.matches(rec, "חולון")
    assert datagov.matches({"c": "קרית אונו"}, "קריית אונו")


def test_tools_listed():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"urban_renewal_projects", "construction_sites", "subsidized_housing_lotteries",
                     "housing_pipeline", "area_report", "find_open_datasets", "query_open_dataset"}


def test_urban_renewal_street_and_city(fake):
    r = _call("urban_renewal_projects", {"city": "תל אביב", "street": "הרצל"})
    assert r["ok"] and r["scope"] == "street" and r["matches"] == 1
    assert "_id" not in r["records"][0] and "_full_text" not in r["records"][0]
    r2 = _call("urban_renewal_projects", {"city": "תל אביב"})
    assert r2["matches"] == 2


def test_street_not_found_falls_back_to_city(fake):
    r = _call("urban_renewal_projects", {"city": "תל אביב", "street": "דיזנגוף"})
    assert r["ok"] and r["scope"] == "city" and r["matches"] == 2 and "note" in r


def test_header_fallback_remembered(fake):
    _call("construction_sites", {"city": "תל אביב"})
    assert fake._good_headers == 1
    assert any("not JSON" in str(d) or d["headers"] == 0 for d in fake.diagnostics)


def test_area_report(fake):
    r = _call("area_report", {"city": "חולון"})
    s = r["sections"]
    assert r["ok"] and s["urban_renewal"]["matches"] == 1 and s["subsidized_lotteries"]["matches"] == 1
    assert "nadlan.gov.il" in r["deal_prices"]


def test_find_and_query(fake):
    f = _call("find_open_datasets", {"query": "גני ילדים"})
    assert f["ok"] and f["datasets"][0]["queryable_resources"] == [
        {"resource_id": "rid-1", "name": "גנים", "format": "CSV"}]
    q = _call("query_open_dataset", {"resource_id": datagov.DATASETS["housing_pipeline"]["resource_id"]})
    assert q["ok"] and q["total"] == 1


def test_service_down_gives_friendly_error(monkeypatch):
    monkeypatch.setattr(datagov.time, "sleep", lambda s: None)

    class Down:
        def request(self, *a, **k):
            return _Resp(503)

    server.set_client(datagov.DataGovClient(session=Down()))
    try:
        r = _call("construction_sites", {"city": "חולון"})
        assert r["ok"] is False and "not responding" in r["error"]
    finally:
        server.set_client(None)


def test_slim_keeps_key_fields_and_survives_schema_change():
    rec = {"ProjectName": "מגרשים", "LamasName": "חולון", "PriceForMeter": "15,500.00", "WinnersSeriesA": 1}
    assert datagov.slim("mechir_lamishtaken", rec) == {"ProjectName": "מגרשים", "LamasName": "חולון",
                                                        "PriceForMeter": "15,500.00"}
    odd = {"x": 1, "y": 2}
    assert datagov.slim("mechir_lamishtaken", odd) == odd
    assert datagov.slim("housing_pipeline", odd) == odd


def test_usage_stats_tracked(fake):
    before = server.STATS["calls"]
    _call("area_report", {"city": "חולון", "street": "סוקולוב"})
    assert server.STATS["calls"] == before + 1
    assert server.STATS["by_city"]["חולון"] >= 1
    assert server.STATS["recent"][0]["street"] == "סוקולוב"
