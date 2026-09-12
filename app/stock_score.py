"""Stock-level context score: market-cap/liquidity tier and sector strength.

This is independent of any specific signal's entry/SL/targets -- it asks "is this
generally a stock worth trading right now" (size/liquidity risk, and whether its sector
is leading or lagging), not "is this specific call well-timed" (that's the other factors).
"""
import yfinance as yf

from app.technicals import resolve_ticker

# INR crore thresholds (1 crore = 1e7). Approximate, commonly used tiers -- not an official
# SEBI ranking (that's rank-based, not a fixed cutoff), but a reasonable proxy.
LARGE_CAP_THRESHOLD = 20_000 * 1e7
MID_CAP_THRESHOLD = 5_000 * 1e7

# Yahoo Finance's generic sector taxonomy -> nearest NSE sectoral index available on yfinance.
SECTOR_INDEX_MAP = {
    "Technology": "^CNXIT",
    "Financial Services": "^NSEBANK",
    "Healthcare": "^CNXPHARMA",
    "Consumer Cyclical": "^CNXAUTO",
    "Consumer Defensive": "^CNXFMCG",
    "Basic Materials": "^CNXMETAL",
    "Energy": "^CNXENERGY",
    "Real Estate": "^CNXREALTY",
    "Industrials": "^CNXINFRA",
    "Communication Services": "^CNXMEDIA",
    "Utilities": "^CNXENERGY",
}


def market_cap_tier(market_cap: float) -> str:
    if not market_cap:
        return "unknown"
    if market_cap >= LARGE_CAP_THRESHOLD:
        return "large-cap"
    if market_cap >= MID_CAP_THRESHOLD:
        return "mid-cap"
    return "small-cap"


def fetch_sector(resolved_symbol: str) -> str:
    """Just the sector label -- lighter than evaluate_stock_context (no market-cap or
    sector-strength history fetch), for bulk enrichment like tagging the whole F&O universe
    where only the label is needed."""
    ticker = resolve_ticker(resolved_symbol)
    try:
        return yf.Ticker(ticker).get_info().get("sector")
    except Exception:
        return None


def evaluate_stock_context(resolved_symbol: str) -> dict:
    ticker = resolve_ticker(resolved_symbol)
    try:
        info = yf.Ticker(ticker).get_info()
    except Exception as e:
        return {"available": False, "reason": f"Stock context fetch failed: {e}"}

    market_cap = info.get("marketCap")
    sector = info.get("sector")
    tier = market_cap_tier(market_cap)

    result = {
        "available": True,
        "market_cap": market_cap,
        "market_cap_cr": round(market_cap / 1e7, 0) if market_cap else None,
        "tier": tier,
        "sector": sector,
        "industry": info.get("industry"),
    }

    sector_index = SECTOR_INDEX_MAP.get(sector)
    if sector_index:
        try:
            # NSE sectoral index tickers (^CNXxxx) return only the latest row for short
            # `period` values on yfinance -- 3mo is the shortest period that reliably
            # returns enough daily bars for the 10-day lookback below.
            stock_df = yf.Ticker(ticker).history(period="3mo", interval="1d", auto_adjust=True)
            idx_df = yf.Ticker(sector_index).history(period="3mo", interval="1d", auto_adjust=True)
            stock_df = stock_df.dropna(subset=["Close"])
            idx_df = idx_df.dropna(subset=["Close"])
            if len(stock_df) >= 11 and len(idx_df) >= 11:
                stock_ret = float(stock_df["Close"].iloc[-1]) / float(stock_df["Close"].iloc[-11]) - 1
                idx_ret = float(idx_df["Close"].iloc[-1]) / float(idx_df["Close"].iloc[-11]) - 1
                result["sector_index"] = sector_index
                result["sector_rel_strength_10d"] = round(stock_ret - idx_ret, 4)
        except Exception:
            pass  # sector strength is a bonus signal -- degrade quietly if it fails

    return result
