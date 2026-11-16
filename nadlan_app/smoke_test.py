"""Live check against the real Govmap API. Run on a machine with internet:

    python -m nadlan_app.smoke_test "הרצל 10 תל אביב"
"""
import json
import sys

from . import analytics
from .govmap import GovmapClient


def main() -> None:
    addr = sys.argv[1] if len(sys.argv) > 1 else "הרצל 10 תל אביב"
    c = GovmapClient()
    hits = c.autocomplete(addr)
    print("autocomplete:", json.dumps(hits[:3], ensure_ascii=False))
    if not hits:
        sys.exit("FAIL: no address results — autocomplete payload/response may have changed")
    raw = c.deals_by_radius(hits[0]["point"], 300)
    print(f"raw deals: {len(raw)}")
    if raw:
        print("first raw deal keys:", sorted(raw[0].keys()))
    deals = analytics.normalize_deals(raw)
    print(f"normalized deals: {len(deals)}")
    if not deals:
        sys.exit("FAIL: no deals normalized — check field names in analytics._KEYS")
    print("sample:", json.dumps(deals[0], ensure_ascii=False))
    print("summary:", json.dumps(analytics.summarize(deals), ensure_ascii=False))
    print("OK")


if __name__ == "__main__":
    main()
