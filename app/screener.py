"""CE/PE confirmation screener -- a volatility-expansion + multi-timeframe trend-alignment
checklist (the kind of condition set you'd run in a stock scanner), used here as an extra
confirmation layer on top of the core technical score rather than a standalone scanner.

For a bullish (CE) call:
  - today's daily range (High-Low) exceeds each of the last 7 days' ranges (fresh volatility
    expansion, not a quiet range-bound day)
  - daily candle is green, and closes above the prior day's close
  - the weekly and monthly candles are also green (multi-timeframe agreement)
  - yesterday's volume clears a basic liquidity floor
  - SMA(20) is above SMA(50) (short-term trend above long-term trend)
  - the latest 15-minute close is above the prior day's close (intraday follow-through)
  - price clears a basic floor (avoids illiquid/penny names)

For a bearish (PE) call, every directional condition is mirrored (red candles, SMA20<SMA50,
etc.) while the volatility-expansion, volume, and price-floor conditions stay the same.
"""
import pandas as pd
import yfinance as yf

from app.technicals import resolve_ticker

MIN_VOLUME = 20000
MIN_PRICE = 100


def evaluate_screener(resolved_symbol: str, direction: str) -> dict:
    ticker = resolve_ticker(resolved_symbol)
    bullish = direction == "bullish"

    try:
        df = yf.Ticker(ticker).history(period="4mo", interval="1d", auto_adjust=True)
        df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        if len(df) < 12:
            return {"available": False, "reason": "Not enough daily history for screener checks."}

        checks = []

        # Volatility expansion: today's range bigger than each of the last 7 days'.
        ranges = (df["High"] - df["Low"]).iloc[-8:]
        today_range = ranges.iloc[-1]
        prior_ranges = ranges.iloc[:-1]
        expansion = bool((today_range > prior_ranges).all()) if len(prior_ranges) == 7 else False
        checks.append({"name": "Range expansion vs last 7 days", "passed": expansion})

        o, c = float(df["Open"].iloc[-1]), float(df["Close"].iloc[-1])
        prev_c = float(df["Close"].iloc[-2])

        candle_ok = (c > o) if bullish else (c < o)
        checks.append({"name": "Daily close vs open", "passed": bool(candle_ok)})

        follow_ok = (c > prev_c) if bullish else (c < prev_c)
        checks.append({"name": "Daily close vs 1 day ago close", "passed": bool(follow_ok)})

        weekly = df.resample("W").agg({"Open": "first", "Close": "last"}).dropna()
        w_ok = False
        if len(weekly) >= 1:
            wo, wc = float(weekly["Open"].iloc[-1]), float(weekly["Close"].iloc[-1])
            w_ok = (wc > wo) if bullish else (wc < wo)
        checks.append({"name": "Weekly close vs weekly open", "passed": bool(w_ok)})

        monthly = df.resample("ME").agg({"Open": "first", "Close": "last"}).dropna()
        m_ok = False
        if len(monthly) >= 1:
            mo, mc = float(monthly["Open"].iloc[-1]), float(monthly["Close"].iloc[-1])
            m_ok = (mc > mo) if bullish else (mc < mo)
        checks.append({"name": "Monthly close vs monthly open", "passed": bool(m_ok)})

        vol_ok = float(df["Volume"].iloc[-2]) > MIN_VOLUME
        checks.append({"name": f"1 day ago volume > {MIN_VOLUME:,}", "passed": bool(vol_ok)})

        sma20 = df["Close"].rolling(20).mean().iloc[-1]
        sma_ok = False
        if len(df) >= 50:
            sma50 = df["Close"].rolling(50).mean().iloc[-1]
            if not pd.isna(sma50):
                sma_ok = (sma20 > sma50) if bullish else (sma20 < sma50)
        checks.append({"name": "SMA(20) vs SMA(50)", "passed": bool(sma_ok)})

        intraday_ok = False
        try:
            df15 = yf.Ticker(ticker).history(period="5d", interval="15m", auto_adjust=True)
            df15 = df15.dropna(subset=["Close"])
            if not df15.empty:
                last15 = float(df15["Close"].iloc[-1])
                intraday_ok = (last15 > prev_c) if bullish else (last15 < prev_c)
        except Exception:
            pass
        checks.append({"name": "Latest 15m close vs 1 day ago close", "passed": bool(intraday_ok)})

        price_ok = c > MIN_PRICE
        checks.append({"name": f"Daily close > {MIN_PRICE}", "passed": bool(price_ok)})

        passed = sum(1 for chk in checks if chk["passed"])
        return {"available": True, "checks": checks, "passed": passed, "total": len(checks)}
    except Exception as e:
        return {"available": False, "reason": f"Screener check failed: {e}"}
