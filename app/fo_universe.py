"""Fetches the current F&O-eligible stock universe from NSE's public underlying-information
endpoint. Refreshable at any time (POST /api/scanner/universe/refresh) without touching code --
satisfies "must be refreshable without modifying application code."

Best-effort like every other NSE integration in this app: NSE blocks non-browser traffic
unpredictably, so failures are reported clearly rather than silently falling back to stale
or fabricated data.
"""
import requests

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json",
}

UNDERLYING_URL = "https://www.nseindia.com/api/underlying-information"


def fetch_fo_universe() -> dict:
    """Returns {"available": True, "stocks": [...], "indices": [...]} or
    {"available": False, "reason": ...}. Each stock/index dict has symbol + company_name."""
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=BASE_HEADERS, timeout=8)
        resp = session.get(UNDERLYING_URL, headers=BASE_HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()["data"]

        stocks = [
            {
                "symbol": row["symbol"],
                "resolved_symbol": row["symbol"],
                "company_name": row.get("underlying"),
                "is_index": False,
                "futures_eligible": True,
            }
            for row in data.get("UnderlyingList", [])
        ]
        indices = [
            {
                "symbol": row["symbol"],
                "resolved_symbol": row["symbol"],
                "company_name": row.get("underlying"),
                "is_index": True,
                "futures_eligible": True,
            }
            for row in data.get("IndexList", [])
        ]
        return {"available": True, "stocks": stocks, "indices": indices}
    except Exception as e:
        return {
            "available": False,
            "reason": f"NSE underlying-information fetch failed ({e}). NSE often blocks "
            "server-side scraping -- for a reliable universe feed, wire up a broker/licensed "
            "market-data API in place of this module.",
        }
    finally:
        session.close()
