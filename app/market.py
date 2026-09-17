"""Live index quotes for the ticker bar (NIFTY 50, BANK NIFTY, SENSEX)."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

import yfinance as yf

INDEX_TICKERS = {
    "NIFTY 50": "^NSEI",
    "BANK NIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
}

IST = ZoneInfo("Asia/Kolkata")
NSE_OPEN = time(9, 15)
NSE_CLOSE = time(15, 30)


def is_market_hours_now() -> bool:
    """NSE regular session, Monday-Friday 09:15-15:30 IST. Deliberately doesn't know about
    exchange holidays (no holiday calendar wired up) -- on a holiday this returns True and a
    scan runs anyway, just against a closed market; the underlying data lookups degrade
    gracefully (qualification.evaluate_stock already treats an unavailable quote as
    "unavailable", not a crash), so the cost is a handful of wasted API calls, not a bug."""
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday, Sunday
        return False
    return NSE_OPEN <= now.time() <= NSE_CLOSE


def fetch_index_quotes() -> list:
    result = []
    for label, ticker in INDEX_TICKERS.items():
        try:
            info = yf.Ticker(ticker).fast_info
            last = float(info["last_price"])
            prev = float(info["previous_close"])
            change = last - prev
            pct = (change / prev) * 100 if prev else 0
            result.append(
                {
                    "label": label,
                    "available": True,
                    "last": round(last, 2),
                    "change": round(change, 2),
                    "pct_change": round(pct, 2),
                }
            )
        except Exception as e:
            result.append({"label": label, "available": False, "reason": str(e)})
    return result
