"""Live index quotes for the ticker bar (NIFTY 50, BANK NIFTY, SENSEX)."""
import yfinance as yf

INDEX_TICKERS = {
    "NIFTY 50": "^NSEI",
    "BANK NIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
}


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
