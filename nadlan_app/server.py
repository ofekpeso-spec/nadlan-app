"""Nadlan IL — MCP app for Israeli home buyers, built only on official open government data.

Run locally:   python -m nadlan_app.server            (HTTP on :8000/mcp)
Health check:  GET /health      Live data check: GET /selftest
"""
from __future__ import annotations

import os
from typing import Annotated

from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import datagov
from .datagov import DataGovClient, DataGovError

APP_NAME = "Nadlan IL"
SOURCE_NOTE = "Source: data.gov.il — Israel's official open government data portal."
DEALS_NOTE = ("Recorded sale prices for specific apartments are not available through this app. "
              "Users can look them up for free on the official site https://www.nadlan.gov.il")

INSTRUCTIONS = f"""Nadlan IL helps people buying or renting a home in Israel check an area using official open data:
urban renewal / פינוי-בינוי complexes, active construction sites (noise, future supply),
Mechir LaMishtaken / דירה בהנחה lotteries, and the housing development pipeline.
Pass the city in Hebrew when possible (e.g. 'תל אביב יפו', 'חולון') and optionally a street name.
Records come from different ministries, so field names vary: summarise them in plain language.
{DEALS_NOTE}
Answer in the user's language."""

mcp = MCPServer(name=APP_NAME, title="Nadlan IL — מידע לקוני דירות", instructions=INSTRUCTIONS,
                version="0.2.0")
_client: DataGovClient | None = None
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

City = Annotated[str, Field(description="City / town in Israel, preferably in Hebrew, e.g. 'חולון'")]
Street = Annotated[str | None, Field(description="Optional street name, e.g. 'סוקולוב'")]
Limit = Annotated[int, Field(ge=1, le=50, description="Max records to return")]


def client() -> DataGovClient:
    global _client
    if _client is None:
        _client = DataGovClient()
    return _client


def set_client(c: DataGovClient | None) -> None:  # used by tests
    global _client
    _client = c


def _run(dataset: str, city: str, street: str | None, limit: int) -> dict:
    try:
        res = datagov.lookup(client(), dataset, city.strip(), (street or "").strip() or None, limit)
        return {"ok": True, **res, "source": SOURCE_NOTE}
    except DataGovError as e:
        return {"ok": False, "error": "The government open-data service is not responding right now. "
                                      "Please try again in a minute.", "detail": str(e)}


@mcp.tool(title="Urban renewal (פינוי-בינוי) near a street", annotations=READ_ONLY)
def urban_renewal_projects(city: City, street: Street = None, limit: Limit = 20) -> dict:
    """Officially declared urban-renewal (פינוי-בינוי) complexes in a city or on a street.
    Useful to know if a building may be demolished and rebuilt, or if the area will change a lot."""
    return _run("urban_renewal", city, street, limit)


@mcp.tool(title="Active construction sites nearby", annotations=READ_ONLY)
def construction_sites(city: City, street: Street = None, limit: Limit = 20) -> dict:
    """Active construction sites in a city or on a street: signals noise/dust now and new supply later."""
    return _run("construction_sites", city, street, limit)


@mcp.tool(title="Mechir LaMishtaken / Dira BeHanacha lotteries", annotations=READ_ONLY)
def subsidized_housing_lotteries(city: City, limit: Limit = 20) -> dict:
    """Government subsidized-housing lotteries (מחיר למשתכן / דירה בהנחה) in a city,
    including prices per sqm where published. Good benchmark for eligible buyers."""
    return _run("mechir_lamishtaken", city, None, limit)


@mcp.tool(title="Housing development pipeline", annotations=READ_ONLY)
def housing_pipeline(city: City, limit: Limit = 20) -> dict:
    """Planned / in-progress housing supply in a city (how many new units are coming)."""
    return _run("housing_pipeline", city, None, limit)


@mcp.tool(title="Area check for home buyers", annotations=READ_ONLY)
def area_report(city: City, street: Street = None) -> dict:
    """One-call overview of a city/street: urban renewal, construction sites, subsidized lotteries
    and housing pipeline counts with a few example records each. Start here for general questions."""
    sections = {}
    for key, ds, use_street in [("urban_renewal", "urban_renewal", True),
                                ("construction_sites", "construction_sites", True),
                                ("subsidized_lotteries", "mechir_lamishtaken", False),
                                ("housing_pipeline", "housing_pipeline", False)]:
        r = _run(ds, city, street if use_street else None, 5)
        sections[key] = r if not r.get("ok") else {
            "matches": r["matches"], "scope": r.get("scope"), "examples": r["records"], **(
                {"note": r["note"]} if r.get("note") else {})}
    return {"ok": True, "city": city, "street": street, "sections": sections,
            "deal_prices": DEALS_NOTE, "source": SOURCE_NOTE}


@mcp.tool(title="Search Israeli open datasets", annotations=READ_ONLY)
def find_open_datasets(query: Annotated[str, Field(description="Topic in Hebrew or English, e.g. 'גני ילדים חולון'")]) -> dict:
    """Search the data.gov.il catalogue for other official datasets (schools, transport, environment...).
    Returns resource IDs that can be queried with query_open_dataset."""
    try:
        return {"ok": True, "datasets": client().find_datasets(query), "source": SOURCE_NOTE}
    except DataGovError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(title="Query an open dataset", annotations=READ_ONLY)
def query_open_dataset(
    resource_id: Annotated[str, Field(description="resource_id from find_open_datasets")],
    text: Annotated[str | None, Field(description="Free-text filter, e.g. a city or street")] = None,
    limit: Limit = 20,
) -> dict:
    """Read records from any queryable data.gov.il resource, optionally filtered by free text."""
    try:
        res = client().search(resource_id, q=text, limit=limit)
        return {"ok": True, **res, "source": SOURCE_NOTE}
    except DataGovError as e:
        return {"ok": False, "error": str(e)}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "app": APP_NAME, "version": "0.2.0"})


@mcp.custom_route("/selftest", methods=["GET"])
async def selftest(request: Request) -> JSONResponse:
    """Live check of every dataset, viewable from any browser."""
    import anyio

    city = request.query_params.get("city", "תל אביב")

    def run() -> dict:
        c = client()
        c.diagnostics.clear()
        report: dict = {"city": city, "datasets": {}}
        ok_all = True
        for name, meta in datagov.DATASETS.items():
            try:
                res = c.search(meta["resource_id"], q=city, limit=3)
                report["datasets"][name] = {"ok": True, "total_for_city": res["total"],
                                            "fields": res["fields"], "sample": res["records"][:1]}
            except Exception as e:  # noqa: BLE001
                ok_all = False
                report["datasets"][name] = {"ok": False, "error": str(e)}
        report["result"] = "OK" if ok_all else "FAIL"
        report["attempts"] = list(c.diagnostics)
        return report

    data = await anyio.to_thread.run_sync(run)
    data = {"result": data.pop("result"), **data}
    return JSONResponse(data, status_code=200 if data["result"] == "OK" else 500)


def main() -> None:
    import uvicorn
    host = os.environ.get("HOST", "0.0.0.0")
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host=host)
    uvicorn.run(app, host=host, port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
