"""Fetches OHLCV data and computes technical indicators used to evaluate a signal."""
import numpy as np
import pandas as pd
import yfinance as yf

NIFTY_TICKER = "^NSEI"


def _fetch_daily_frames(symbol: str, ticker: str, user_id: int = None):
    """Returns (stock_df, nifty_df, source). Tries the given user's connected broker session
    first (real data, priority Kite > Upstox > Dhan); falls back to yfinance on any error or
    if no broker is connected -- a broken/expired broker session should degrade evaluation
    quality, never break it."""
    if user_id is not None:
        try:
            from app.brokers import get_connected_adapter

            adapter = get_connected_adapter(user_id)
            if adapter:
                stock_df = adapter.fetch_daily_candles(user_id, symbol.upper(), "NSE", days=280)
                idx_df = adapter.fetch_daily_candles(user_id, "NIFTY 50", "NSE", days=280)
                if stock_df is not None and not stock_df.empty:
                    return stock_df, idx_df, adapter.__name__.rsplit(".", 1)[-1]
        except Exception:
            pass  # fall through to yfinance

    stock_df = yf.Ticker(ticker).history(period="9mo", interval="1d", auto_adjust=True)
    idx_df = yf.Ticker(NIFTY_TICKER).history(period="9mo", interval="1d", auto_adjust=True)
    return stock_df, idx_df, "yfinance"


def resolve_ticker(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if symbol.startswith("^") or symbol.endswith(".NS") or symbol.endswith(".BO"):
        return symbol
    return f"{symbol}.NS"


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def fetch_technicals(symbol: str, direction: str = "bullish", user_id: int = None) -> dict:
    ticker = resolve_ticker(symbol)
    try:
        df, idx, source = _fetch_daily_frames(symbol, ticker, user_id)
        if df is None or df.empty or len(df) < 30:
            return {"available": False, "reason": f"No/insufficient price history for {ticker}."}

        # Both sources can carry an incomplete trailing/current-session row (NaN OHLC) --
        # drop it so indicators never see it.
        df = df.dropna(subset=["Close", "High", "Low", "Volume"])
        if len(df) < 30:
            return {"available": False, "reason": f"No/insufficient price history for {ticker}."}

        idx = idx.dropna(subset=["Close"]) if idx is not None and not idx.empty else idx

        close = df["Close"]
        volume = df["Volume"]

        ema20 = _ema(close, 20)
        ema50 = _ema(close, 50)
        ema200 = _ema(close, 200) if len(close) >= 200 else pd.Series([np.nan] * len(close))
        rsi14 = _rsi(close, 14)
        atr14 = _atr(df, 14)

        last_close = float(close.iloc[-1])
        last_ema20 = float(ema20.iloc[-1])
        last_ema50 = float(ema50.iloc[-1])
        last_ema200 = float(ema200.iloc[-1]) if not np.isnan(ema200.iloc[-1]) else None
        last_rsi = float(rsi14.iloc[-1])
        last_atr = float(atr14.iloc[-1])

        # 20-day high/low excluding today, for breakout detection
        lookback = 20
        prior_high = float(df["High"].iloc[-(lookback + 1):-1].max())
        prior_low = float(df["Low"].iloc[-(lookback + 1):-1].min())
        breakout_up = last_close > prior_high
        breakdown = last_close < prior_low

        avg_vol_20 = float(volume.iloc[-21:-1].mean())
        last_vol = float(volume.iloc[-1])
        vol_ratio = (last_vol / avg_vol_20) if avg_vol_20 > 0 else None

        trend_up = last_close > last_ema20 > last_ema50 and (
            last_ema200 is None or last_ema50 > last_ema200
        )
        trend_down = last_close < last_ema20 < last_ema50 and (
            last_ema200 is None or last_ema50 < last_ema200
        )

        rel_strength = None
        if idx is not None and not idx.empty and len(idx) >= 11:
            stock_ret = last_close / float(close.iloc[-11]) - 1
            idx_ret = float(idx["Close"].iloc[-1]) / float(idx["Close"].iloc[-11]) - 1
            rel_strength = stock_ret - idx_ret

        aligned_with_direction = (direction == "bullish" and trend_up) or (
            direction == "bearish" and trend_down
        )
        against_direction = (direction == "bullish" and trend_down) or (
            direction == "bearish" and trend_up
        )

        return {
            "available": True,
            "ticker": ticker,
            "source": source,
            "last_close": round(last_close, 2),
            "ema20": round(last_ema20, 2),
            "ema50": round(last_ema50, 2),
            "ema200": round(last_ema200, 2) if last_ema200 is not None else None,
            "rsi14": round(last_rsi, 1),
            "atr14": round(last_atr, 2),
            "prior_20d_high": round(prior_high, 2),
            "prior_20d_low": round(prior_low, 2),
            "breakout_up": breakout_up,
            "breakdown": breakdown,
            "avg_vol_20": round(avg_vol_20, 0),
            "last_vol": round(last_vol, 0),
            "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "trend_up": trend_up,
            "trend_down": trend_down,
            "aligned_with_direction": aligned_with_direction,
            "against_direction": against_direction,
            "rel_strength_10d": round(rel_strength, 4) if rel_strength is not None else None,
        }
    except Exception as e:  # network hiccups, delisted symbol, rate limiting, etc.
        return {"available": False, "reason": f"Technical data fetch failed: {e}"}
