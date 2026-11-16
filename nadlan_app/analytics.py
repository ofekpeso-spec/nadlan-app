"""Pure analytics over Israeli real-estate deals (no network).

Raw deals from Govmap may use different key names, so `normalize_deal`
accepts several variants and returns a clean, predictable dict.
"""
from __future__ import annotations

from datetime import date, datetime
from statistics import median
from typing import Any, Iterable

_KEYS = {
    "price": ["dealAmount", "deal_amount", "DEALAMOUNT", "price", "amount"],
    "date": ["dealDate", "deal_date", "DEALDATETIME", "date"],
    "sqm": ["assetArea", "asset_area", "DEALNATURE_AREA", "area", "sqm"],
    "rooms": ["assetRoomNum", "rooms", "ASSETROOMNUM", "roomNum"],
    "floor": ["floorNumber", "floorNo", "FLOORNO", "floor"],
    "address": ["address_description", "addressDescription", "fullAddress", "address"],
    "street": ["streetNameHeb", "streetName"],
    "house": ["houseNum", "houseNumber"],
    "city": ["settlementNameHeb", "settlementName", "city", "cityName"],
    "neighborhood": ["neighborhood", "neighborhoodName"],
    "property_type": ["propertyTypeDescription", "dealNature", "dealNatureDescription", "assetType", "propertyType"],
    "id": ["objectid", "objectId", "id"],
    "year_built": ["yearBuilt", "buildingYear", "BUILDINGYEAR"],
}


def _pick(raw: dict, field: str) -> Any:
    for k in _KEYS[field]:
        v = raw.get(k)
        if v not in (None, "", "null"):
            return v
    return None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("₪", "").strip())
    except ValueError:
        return None


def _date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v if not isinstance(v, datetime) else v.date()
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%Y-%m", "%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def normalize_deal(raw: dict) -> dict | None:
    """Return a clean deal dict, or None if price/date are missing."""
    price = _num(_pick(raw, "price"))
    d = _date(_pick(raw, "date"))
    if not price or price <= 0 or d is None:
        return None
    sqm = _num(_pick(raw, "sqm"))
    rooms = _num(_pick(raw, "rooms"))
    floor = _pick(raw, "floor")
    year = _num(_pick(raw, "year_built"))
    address = _pick(raw, "address")
    if not address:
        street, house = _pick(raw, "street"), _pick(raw, "house")
        address = " ".join(str(p) for p in (street, house) if p) or None
    return {
        "id": _pick(raw, "id"),
        "date": d.isoformat(),
        "price": int(round(price)),
        "sqm": round(sqm, 1) if sqm and sqm > 0 else None,
        "price_per_sqm": int(round(price / sqm)) if sqm and sqm > 0 else None,
        "rooms": rooms if rooms and rooms > 0 else None,
        "floor": str(floor) if floor is not None else None,
        "address": address,
        "city": _pick(raw, "city"),
        "neighborhood": _pick(raw, "neighborhood"),
        "property_type": _pick(raw, "property_type"),
        "year_built": int(year) if year else None,
    }


def normalize_deals(raws: Iterable[dict]) -> list[dict]:
    out = [d for d in (normalize_deal(r) for r in raws if isinstance(r, dict)) if d]
    # de-duplicate identical records, newest first
    seen, uniq = set(), []
    for d in sorted(out, key=lambda x: x["date"], reverse=True):
        key = (d["id"], d["date"], d["price"]) if d.get("id") is not None else (
            d["date"], d["price"], d["address"], d["sqm"], d["floor"])
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def _months_ago(today: date, months: int) -> date:
    y, m = today.year, today.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = min(today.day, 28)
    return date(y, m, day)


def filter_deals(deals: list[dict], *, rooms: float | None = None, min_rooms: float | None = None,
                 max_rooms: float | None = None, months_back: int | None = None,
                 today: date | None = None) -> list[dict]:
    today = today or date.today()
    out = []
    since = _months_ago(today, months_back).isoformat() if months_back else None
    for d in deals:
        r = d.get("rooms")
        if rooms is not None and (r is None or abs(r - rooms) > 0.25):
            continue
        if min_rooms is not None and (r is None or r < min_rooms):
            continue
        if max_rooms is not None and (r is None or r > max_rooms):
            continue
        if since and d["date"] < since:
            continue
        out.append(d)
    return out


def _percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    if not s:
        raise ValueError("empty")
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(deals: list[dict], *, today: date | None = None) -> dict:
    """Median price, price/sqm range and 12-month trend vs the 12 months before."""
    today = today or date.today()
    if not deals:
        return {"deal_count": 0}
    prices = [d["price"] for d in deals]
    ppsqm = [d["price_per_sqm"] for d in deals if d.get("price_per_sqm")]
    res: dict[str, Any] = {
        "deal_count": len(deals),
        "date_range": {"from": min(d["date"] for d in deals), "to": max(d["date"] for d in deals)},
        "median_price": int(median(prices)),
        "min_price": min(prices),
        "max_price": max(prices),
    }
    if ppsqm:
        res["price_per_sqm"] = {
            "median": int(median(ppsqm)),
            "p25": int(_percentile(ppsqm, 0.25)),
            "p75": int(_percentile(ppsqm, 0.75)),
            "sample_size": len(ppsqm),
        }
    y1 = _months_ago(today, 12).isoformat()
    y2 = _months_ago(today, 24).isoformat()
    last = [d["price_per_sqm"] for d in deals if d.get("price_per_sqm") and d["date"] >= y1]
    prev = [d["price_per_sqm"] for d in deals if d.get("price_per_sqm") and y2 <= d["date"] < y1]
    if len(last) >= 3 and len(prev) >= 3:
        a, b = median(last), median(prev)
        res["trend_12m"] = {
            "median_ppsqm_last_12m": int(a),
            "median_ppsqm_prev_12m": int(b),
            "change_pct": round((a - b) / b * 100, 1),
        }
    else:
        res["trend_12m"] = None
        res["trend_note"] = "Not enough deals in both periods (need 3+ each) to compute a trend."
    return res


def estimate_value(deals: list[dict], *, sqm: float, rooms: float | None = None,
                   today: date | None = None) -> dict:
    """Comps-based estimate: price/sqm of similar recent deals × sqm.

    Prefers deals within ±0.5 rooms and ±25% size from the last 24 months,
    and widens the filter if there are too few comparables.
    """
    today = today or date.today()
    since = _months_ago(today, 24).isoformat()
    base = [d for d in deals if d.get("price_per_sqm") and d["date"] >= since]

    def similar(room_tol: float | None, size_tol: float | None) -> list[dict]:
        out = []
        for d in base:
            if room_tol is not None and rooms is not None and (d.get("rooms") is None or abs(d["rooms"] - rooms) > room_tol):
                continue
            if size_tol is not None and (d.get("sqm") is None or abs(d["sqm"] - sqm) / sqm > size_tol):
                continue
            out.append(d)
        return out

    comps, level = [], ""
    for room_tol, size_tol, lvl in [(0.5, 0.25, "high"), (1.0, 0.4, "medium"), (None, None, "low")]:
        comps = similar(room_tol, size_tol)
        if len(comps) >= 5:
            level = lvl
            break
    if len(comps) < 3:
        return {"ok": False, "reason": "Not enough comparable deals in the last 24 months.",
                "comparables_found": len(comps)}
    level = level or "low"
    pp = [d["price_per_sqm"] for d in comps]
    return {
        "ok": True,
        "estimate": {
            "low": int(round(_percentile(pp, 0.25) * sqm, -3)),
            "mid": int(round(median(pp) * sqm, -3)),
            "high": int(round(_percentile(pp, 0.75) * sqm, -3)),
        },
        "confidence": level,
        "comparables_used": len(comps),
        "sample_comparables": comps[:5],
        "disclaimer": "Statistical estimate from public deal records only, not a professional appraisal (שומה).",
    }
