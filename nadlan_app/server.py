"""Nadlan IL — MCP server exposing official Israeli property-sale records.

Run locally:   python -m nadlan_app.server            (HTTP on :8000/mcp)
Health check:  GET /health
"""
from __future__ import annotations

import os
from typing import Annotated

from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import analytics
from .govmap import GovmapClient, GovmapError

APP_NAME = "Nadlan IL"
SOURCE_NOTE = ("Source: official sold-property records reported to the Israel Tax Authority "
               "(public data via govmap.gov.il / nadlan.gov.il). Not a professional appraisal.")

INSTRUCTIONS = """Use these tools for questions about Israeli apartment / house SALE prices:
what similar homes sold for, price per square meter (מחיר למ"ר) in a street or neighborhood,
price trends, and rough value estimates. Addresses may be in Hebrew or English.
Always mention that figures come from official recorded deals and that an estimate is not an appraisal (שומה).
Answer in the user's language. Prices are in NIS (₪)."""

mcp = MCPServer(name=APP_NAME, title="Nadlan IL — מחירי דירות בישראל", instructions=INSTRUCTIONS,
                version="0.1.0")
_client: GovmapClient | None = None
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


def client() -> GovmapClient:
    global _client
    if _client is None:
        _client = GovmapClient()
    return _client


def set_client(c: GovmapClient) -> None:  # used by tests
    global _client
    _client = c


def _resolve(address: str) -> dict:
    hits = client().autocomplete(address, max_results=5)
    if not hits:
        raise ValueError(f"Address not found: {address!r}. Try 'street number, city' in Hebrew.")
    return hits[0]


def _deals_near(address: str, radius_m: int) -> tuple[dict, list[dict]]:
    place = _resolve(address)
    raw = client().deals_by_radius(place["point"], radius_m)
    return place, analytics.normalize_deals(raw)


def _err(e: Exception) -> dict:
    msg = str(e)
    if isinstance(e, GovmapError):
        msg = "The government data service is not responding right now. Please try again in a minute."
    return {"ok": False, "error": msg}


@mcp.tool(title="Search an Israeli address", annotations=READ_ONLY)
def search_address(
    query: Annotated[str, Field(description="Address or place, e.g. 'הרצל 10 תל אביב' or 'Herzl 10 Tel Aviv'")],
) -> dict:
    """Find matching Israeli addresses / streets / neighborhoods. Use to disambiguate before other tools."""
    try:
        return {"ok": True, "matches": client().autocomplete(query, max_results=5)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(title="Recent sold deals near an address", annotations=READ_ONLY)
def recent_deals(
    address: Annotated[str, Field(description="Israeli address, Hebrew or English")],
    radius_m: Annotated[int, Field(ge=50, le=1000, description="Search radius in meters")] = 300,
    rooms: Annotated[float | None, Field(description="Only deals with this many rooms (e.g. 3, 3.5, 4)")] = None,
    months_back: Annotated[int, Field(ge=1, le=120, description="How far back to look")] = 24,
    limit: Annotated[int, Field(ge=1, le=50)] = 15,
) -> dict:
    """List official recorded property sales around an address: date, price, size, rooms, floor, price per sqm."""
    try:
        place, deals = _deals_near(address, radius_m)
        deals = analytics.filter_deals(deals, rooms=rooms, months_back=months_back)
        return {"ok": True, "resolved_address": place["name"], "radius_m": radius_m,
                "total_matching": len(deals), "deals": deals[:limit], "source": SOURCE_NOTE}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(title="Area price summary & trend", annotations=READ_ONLY)
def area_price_summary(
    address: Annotated[str, Field(description="Address, street or neighborhood in Israel")],
    radius_m: Annotated[int, Field(ge=100, le=1000)] = 500,
    rooms: Annotated[float | None, Field(description="Optional: restrict to this room count")] = None,
) -> dict:
    """Median sale price, price per sqm (median / 25th / 75th percentile) and 12-month trend around an address."""
    try:
        place, deals = _deals_near(address, radius_m)
        deals = analytics.filter_deals(deals, rooms=rooms, months_back=36)
        return {"ok": True, "resolved_address": place["name"], "radius_m": radius_m,
                "rooms_filter": rooms, "summary": analytics.summarize(deals), "source": SOURCE_NOTE}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(title="Estimate an apartment's value", annotations=READ_ONLY)
def estimate_apartment_value(
    address: Annotated[str, Field(description="Apartment address in Israel")],
    sqm: Annotated[float, Field(gt=10, lt=1000, description="Apartment size in square meters")],
    rooms: Annotated[float | None, Field(description="Number of rooms")] = None,
) -> dict:
    """Rough low/mid/high value in NIS from comparable recorded sales nearby. Not an official appraisal."""
    try:
        place, deals = _deals_near(address, 500)
        est = analytics.estimate_value(deals, sqm=sqm, rooms=rooms)
        return {"resolved_address": place["name"], **est, "source": SOURCE_NOTE}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "app": APP_NAME})


@mcp.custom_route("/selftest", methods=["GET"])
async def selftest(request: Request) -> JSONResponse:
    """Live check against Govmap, viewable from any browser (e.g. a phone)."""
    import anyio

    addr = request.query_params.get("address", "הרצל 10 תל אביב")

    def run() -> dict:
        steps: dict = {"address": addr}
        try:
            hits = client().autocomplete(addr)
            steps["autocomplete_matches"] = hits[:3]
            if not hits:
                return {"result": "FAIL", "where": "autocomplete", **steps}
            raw = client().deals_by_radius(hits[0]["point"], 300)
            steps["raw_deals"] = len(raw)
            steps["raw_keys"] = sorted(raw[0].keys()) if raw and isinstance(raw[0], dict) else None
            deals = analytics.normalize_deals(raw)
            steps["normalized_deals"] = len(deals)
            if not deals:
                return {"result": "FAIL", "where": "deals", **steps}
            steps["sample"] = deals[0]
            steps["summary"] = analytics.summarize(deals)
            return {"result": "OK", **steps}
        except Exception as e:  # noqa: BLE001
            return {"result": "FAIL", "where": "exception", "error": str(e), **steps}

    data = await anyio.to_thread.run_sync(run)
    return JSONResponse(data, status_code=200 if data["result"] == "OK" else 500)


def main() -> None:
    import uvicorn
    app = mcp.streamable_http_app(stateless_http=True, json_response=True,
                                  host=os.environ.get("HOST", "0.0.0.0"))
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
