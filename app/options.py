"""Best-effort NSE option-chain lookup for OI / IV context on a specific strike, and ATM
(at-the-money) strike discovery for auto-generated option signals.

NSE's public option-chain endpoint is unofficial, rate-limited, and frequently blocks
non-browser traffic. This module degrades gracefully: if the fetch fails for any reason,
callers get {"available": False, "reason": ...} instead of a crash. When a user has a
connected Kite Connect session, ATM lookup prefers that (real instrument data, no scraping).
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


def _fetch_chain(symbol: str) -> dict:
    """Internal: one NSE fetch shared by fetch_option_chain_snapshot and find_atm_strike."""
    symbol = symbol.strip().upper()
    url = f"https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"

    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=BASE_HEADERS, timeout=6)
        resp = session.get(url, headers=BASE_HEADERS, timeout=6)
        resp.raise_for_status()
        data = resp.json()

        records = data.get("records", {})
        nearest_expiry = records.get("expiryDates", [None])[0]
        rows = [r for r in records.get("data", []) if r.get("expiryDate") == nearest_expiry]
        if not rows:
            return {"available": False, "reason": "No option-chain rows at the nearest expiry."}
        return {"available": True, "expiry": nearest_expiry, "rows": rows}
    except Exception as e:
        return {
            "available": False,
            "reason": f"NSE option-chain fetch failed ({e}). NSE often blocks server-side "
            "scraping -- for reliable OI/IV, connect Kite Connect instead.",
        }
    finally:
        session.close()


def fetch_option_chain_snapshot(symbol: str, strike: float, instrument: str) -> dict:
    if not strike or instrument not in ("CE", "PE"):
        return {"available": False, "reason": "No strike/instrument to look up (equity signal)."}

    chain = _fetch_chain(symbol)
    if not chain["available"]:
        return chain

    match = next((r for r in chain["rows"] if abs(r.get("strikePrice", -1) - strike) < 0.5), None)
    if not match:
        return {"available": False, "reason": f"No option-chain row found for strike {strike} at nearest expiry."}

    leg = match.get(instrument.upper()) or match.get(instrument.lower())
    if not leg:
        return {"available": False, "reason": f"Strike found but no {instrument} leg data."}

    return {
        "available": True,
        "expiry": chain["expiry"],
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


def find_atm_strike_nse(symbol: str, current_price: float, instrument: str) -> dict:
    """Nearest-to-money strike at the nearest expiry, plus that leg's live premium/OI, via
    the same best-effort NSE endpoint. Returns {available, strike, expiry, ltp, oi, ...}."""
    chain = _fetch_chain(symbol)
    if not chain["available"]:
        return chain

    rows_with_leg = [r for r in chain["rows"] if r.get(instrument.upper()) or r.get(instrument.lower())]
    if not rows_with_leg:
        return {"available": False, "reason": f"No {instrument} legs found at the nearest expiry."}

    closest = min(rows_with_leg, key=lambda r: abs(r["strikePrice"] - current_price))
    leg = closest.get(instrument.upper()) or closest.get(instrument.lower())

    return {
        "available": True,
        "source": "nse",
        "expiry": chain["expiry"],
        "strike": closest["strikePrice"],
        "ltp": leg.get("lastPrice"),
        "oi": leg.get("openInterest"),
        "change_in_oi": leg.get("changeinOpenInterest"),
        "iv": leg.get("impliedVolatility"),
        "volume": leg.get("totalTradedVolume"),
    }


def find_atm_option(symbol: str, current_price: float, instrument: str, user_id: int = None) -> dict:
    """Tries the given user's Kite Connect session first (real NFO instrument data); falls
    back to the NSE scraper on any error or if not connected."""
    if user_id is not None:
        try:
            from app.brokers import kite as kite_broker

            result = kite_broker.find_atm_option(user_id, symbol, current_price, instrument)
            if result.get("available"):
                return result
        except Exception:
            pass
    return find_atm_strike_nse(symbol, current_price, instrument)
