"""Best-effort NSE option-chain lookup for OI / IV context on a specific strike.

NSE's public option-chain endpoint is unofficial, rate-limited, and frequently blocks
non-browser traffic. This module degrades gracefully: if the fetch fails for any reason,
callers get {"available": False, "reason": ...} instead of a crash. Treat this data as a
nice-to-have signal, not a dependable feed -- for reliable OI/IV, wire up a broker API
(Kite Connect, Dhan, Upstox, etc.) in place of this module.
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


def fetch_option_chain_snapshot(symbol: str, strike: float, instrument: str) -> dict:
    if not strike or instrument not in ("CE", "PE"):
        return {"available": False, "reason": "No strike/instrument to look up (equity signal)."}

    symbol = symbol.strip().upper()
    url = f"https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"

    session = requests.Session()
    try:
        # NSE requires a warm-up hit to the site first to set cookies before the API call.
        session.get("https://www.nseindia.com", headers=BASE_HEADERS, timeout=6)
        resp = session.get(url, headers=BASE_HEADERS, timeout=6)
        resp.raise_for_status()
        data = resp.json()

        records = data.get("records", {})
        nearest_expiry = records.get("expiryDates", [None])[0]
        rows = records.get("data", [])

        match = None
        for row in rows:
            if row.get("expiryDate") == nearest_expiry and abs(row.get("strikePrice", -1) - strike) < 0.5:
                match = row
                break

        if not match:
            return {
                "available": False,
                "reason": f"No option-chain row found for strike {strike} at nearest expiry.",
            }

        leg = match.get(instrument.upper()) or match.get(instrument.lower())
        if not leg:
            return {"available": False, "reason": f"Strike found but no {instrument} leg data."}

        return {
            "available": True,
            "expiry": nearest_expiry,
            "strike": strike,
            "instrument": instrument,
            "oi": leg.get("openInterest"),
            "change_in_oi": leg.get("changeinOpenInterest"),
            "iv": leg.get("impliedVolatility"),
            "ltp": leg.get("lastPrice"),
            "volume": leg.get("totalTradedVolume"),
            "bid_qty": leg.get("bidQty"),
            "ask_qty": leg.get("askQty"),
        }
    except Exception as e:
        return {
            "available": False,
            "reason": f"NSE option-chain fetch failed ({e}). NSE often blocks server-side "
            "scraping -- for reliable OI/IV, connect a broker API instead.",
        }
    finally:
        session.close()
