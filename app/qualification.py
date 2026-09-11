"""Shared CE/PE qualification engine for the F&O Directional Stock Scanner.

Implements the mandatory-conditions spec exactly: range expansion vs the last 7 completed
sessions, daily/weekly/monthly candle direction, a volume floor, SMA20-vs-SMA50, and a
latest-COMPLETED-15-minute-candle confirmation. CE and PE are evaluated independently from
the same market-data snapshot; a stock should never qualify for both (if it does, that is a
data-integrity error, not a real state -- see classify()).

Every condition is reported individually (ConditionResult-shaped dicts) so the UI can show
exactly why a stock did or didn't qualify, never just a final true/false.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from app.technicals import resolve_ticker

IST = ZoneInfo("Asia/Kolkata")
MIN_VOLUME = 20_000
MIN_PRICE = 100
MIN_DAILY_CANDLES = 50
TARGET_DAILY_CANDLES = 60
STALE_AFTER_DAYS = 5
RULE_VERSION = "1.0"


class DataUnavailable(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _cond(code, label, actual, comparison, required, passed, provisional=False, error=None):
    return {
        "code": code,
        "label": label,
        "actual_value": actual,
        "comparison": comparison,
        "required_value": required,
        "passed": passed,
        "provisional": provisional,
        "error": error,
    }


def _is_nan(x):
    try:
        return x is None or pd.isna(x)
    except (TypeError, ValueError):
        return x is None


def gather_snapshot(symbol: str) -> dict:
    """Fetches everything the qualification engine needs for one stock. Raises
    DataUnavailable with a specific, displayable reason rather than ever returning
    zeros/nulls in place of missing data."""
    ticker = resolve_ticker(symbol)

    try:
        daily = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=True)
    except Exception as e:
        raise DataUnavailable(f"Daily data fetch failed: {e}")

    if daily is None or daily.empty:
        raise DataUnavailable("No daily candle data returned.")

    daily = daily.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(daily) < MIN_DAILY_CANDLES:
        raise DataUnavailable(
            f"Only {len(daily)} completed daily candles available (need at least {MIN_DAILY_CANDLES})."
        )
    if len(daily) < 8:
        raise DataUnavailable("Fewer than 7 previous completed daily sessions available.")

    last_date = daily.index[-1]
    last_date_naive = last_date.tz_localize(None) if last_date.tzinfo else last_date
    now_ist = datetime.now(IST).replace(tzinfo=None)
    if (now_ist - last_date_naive) > timedelta(days=STALE_AFTER_DAYS):
        raise DataUnavailable(f"Daily data is stale -- latest candle is from {last_date_naive.date()}.")

    current_day = {
        "open": float(daily["Open"].iloc[-1]),
        "high": float(daily["High"].iloc[-1]),
        "low": float(daily["Low"].iloc[-1]),
        "close": float(daily["Close"].iloc[-1]),
        "volume": float(daily["Volume"].iloc[-1]),
        "timestamp": str(daily.index[-1]),
    }
    previous_day = {
        "open": float(daily["Open"].iloc[-2]),
        "high": float(daily["High"].iloc[-2]),
        "low": float(daily["Low"].iloc[-2]),
        "close": float(daily["Close"].iloc[-2]),
        "volume": float(daily["Volume"].iloc[-2]),
        "timestamp": str(daily.index[-2]),
    }
    previous_seven = []
    for i in range(2, 9):
        row_idx = -i
        previous_seven.append(
            {
                "open": float(daily["Open"].iloc[row_idx]),
                "high": float(daily["High"].iloc[row_idx]),
                "low": float(daily["Low"].iloc[row_idx]),
                "close": float(daily["Close"].iloc[row_idx]),
                "volume": float(daily["Volume"].iloc[row_idx]),
                "timestamp": str(daily.index[row_idx]),
            }
        )

    for c in [current_day, previous_day] + previous_seven:
        if any(_is_nan(c[k]) for k in ("open", "high", "low", "close", "volume")):
            raise DataUnavailable("One or more required candle values are null/NaN.")

    sma20 = daily["Close"].rolling(20).mean().iloc[-1]
    sma50 = daily["Close"].rolling(50).mean().iloc[-1]
    if _is_nan(sma20) or _is_nan(sma50):
        raise DataUnavailable("SMA20/SMA50 could not be computed (insufficient history).")

    weekly = daily.resample("W").agg({"Open": "first", "Close": "last"}).dropna()
    monthly = daily.resample("ME").agg({"Open": "first", "Close": "last"}).dropna()
    if weekly.empty or monthly.empty:
        raise DataUnavailable("Weekly/monthly candle could not be derived from daily data.")
    current_weekly_open = float(weekly["Open"].iloc[-1])
    current_weekly_close = float(weekly["Close"].iloc[-1])
    current_monthly_open = float(monthly["Open"].iloc[-1])
    current_monthly_close = float(monthly["Close"].iloc[-1])

    now = datetime.now(IST)
    weekly_provisional = True  # the current week is, by definition, never complete intraday
    monthly_provisional = True  # same for the current month

    try:
        intraday = yf.Ticker(ticker).history(period="5d", interval="15m", auto_adjust=True)
        intraday = intraday.dropna(subset=["Close"]) if intraday is not None else None
    except Exception:
        intraday = None
    if intraday is None or intraday.empty:
        raise DataUnavailable("15-minute candle data unavailable.")

    last_bar_time = intraday.index[-1]
    last_bar_time_ist = last_bar_time.tz_convert(IST) if last_bar_time.tzinfo else last_bar_time.tz_localize(IST)
    if (now - last_bar_time_ist) < timedelta(minutes=15) and len(intraday) >= 2:
        # Most recent bar is still forming (fetched mid-candle) -- use the prior, closed one.
        latest_completed_15m_close = float(intraday["Close"].iloc[-2])
        latest_15m_timestamp = str(intraday.index[-2])
    else:
        latest_completed_15m_close = float(intraday["Close"].iloc[-1])
        latest_15m_timestamp = str(intraday.index[-1])

    if _is_nan(latest_completed_15m_close):
        raise DataUnavailable("Latest completed 15-minute close is null/NaN.")

    return {
        "current_day": current_day,
        "previous_day": previous_day,
        "previous_seven_days": previous_seven,
        "current_weekly_open": current_weekly_open,
        "current_weekly_close": current_weekly_close,
        "current_monthly_open": current_monthly_open,
        "current_monthly_close": current_monthly_close,
        "weekly_provisional": weekly_provisional,
        "monthly_provisional": monthly_provisional,
        "daily_sma20": round(float(sma20), 4),
        "daily_sma50": round(float(sma50), 4),
        "latest_completed_15m_close": latest_completed_15m_close,
        "latest_15m_timestamp": latest_15m_timestamp,
        "data_timestamp": datetime.now(IST).isoformat(),
    }


def _range(candle: dict) -> float:
    return candle["high"] - candle["low"]


def evaluate_direction(direction: str, snapshot: dict, futures_eligible: bool) -> dict:
    """Returns {qualified, conditions: [...], failed_conditions: [...]} for one direction."""
    bullish = direction == "CE"
    conditions = []

    current_range = _range(snapshot["current_day"])
    for i, day in enumerate(snapshot["previous_seven_days"], start=1):
        day_range = _range(day)
        passed = current_range > day_range
        conditions.append(
            _cond(
                f"RANGE_EXPANSION_DAY_{i}",
                f"Current day range > Day {i} ago range",
                round(current_range, 4),
                ">",
                round(day_range, 4),
                passed,
            )
        )

    cd = snapshot["current_day"]
    pd_ = snapshot["previous_day"]

    if bullish:
        conditions.append(_cond("CLOSE_VS_OPEN", "Daily close vs open", cd["close"], ">", cd["open"], cd["close"] > cd["open"]))
        conditions.append(_cond("CLOSE_VS_PREV_CLOSE", "Daily close vs 1 day ago close", cd["close"], ">", pd_["close"], cd["close"] > pd_["close"]))
        conditions.append(
            _cond(
                "WEEKLY_DIRECTION", "Weekly close vs weekly open", snapshot["current_weekly_close"], ">",
                snapshot["current_weekly_open"], snapshot["current_weekly_close"] > snapshot["current_weekly_open"],
                provisional=snapshot["weekly_provisional"],
            )
        )
        conditions.append(
            _cond(
                "MONTHLY_DIRECTION", "Monthly close vs monthly open", snapshot["current_monthly_close"], ">",
                snapshot["current_monthly_open"], snapshot["current_monthly_close"] > snapshot["current_monthly_open"],
                provisional=snapshot["monthly_provisional"],
            )
        )
        conditions.append(_cond("SMA_TREND", "SMA20 vs SMA50", snapshot["daily_sma20"], ">", snapshot["daily_sma50"], snapshot["daily_sma20"] > snapshot["daily_sma50"]))
        conditions.append(
            _cond(
                "INTRADAY_15M", "Latest completed 15m close vs 1 day ago close", snapshot["latest_completed_15m_close"], ">",
                pd_["close"], snapshot["latest_completed_15m_close"] > pd_["close"],
            )
        )
    else:
        conditions.append(_cond("CLOSE_VS_OPEN", "Daily close vs open", cd["close"], "<", cd["open"], cd["close"] < cd["open"]))
        conditions.append(_cond("CLOSE_VS_PREV_CLOSE", "Daily close vs 1 day ago close", cd["close"], "<", pd_["close"], cd["close"] < pd_["close"]))
        conditions.append(
            _cond(
                "WEEKLY_DIRECTION", "Weekly close vs weekly open", snapshot["current_weekly_close"], "<",
                snapshot["current_weekly_open"], snapshot["current_weekly_close"] < snapshot["current_weekly_open"],
                provisional=snapshot["weekly_provisional"],
            )
        )
        conditions.append(
            _cond(
                "MONTHLY_DIRECTION", "Monthly close vs monthly open", snapshot["current_monthly_close"], "<",
                snapshot["current_monthly_open"], snapshot["current_monthly_close"] < snapshot["current_monthly_open"],
                provisional=snapshot["monthly_provisional"],
            )
        )
        conditions.append(_cond("SMA_TREND", "SMA20 vs SMA50", snapshot["daily_sma20"], "<", snapshot["daily_sma50"], snapshot["daily_sma20"] < snapshot["daily_sma50"]))
        conditions.append(
            _cond(
                "INTRADAY_15M", "Latest completed 15m close vs 1 day ago close", snapshot["latest_completed_15m_close"], "<",
                pd_["close"], snapshot["latest_completed_15m_close"] < pd_["close"],
            )
        )

    # Shared mandatory filters (section 7) -- identical for CE and PE.
    conditions.append(_cond("VOLUME_FLOOR", f"1 day ago volume > {MIN_VOLUME:,}", pd_["volume"], ">", MIN_VOLUME, pd_["volume"] > MIN_VOLUME))
    conditions.append(_cond("PRICE_FLOOR", f"Daily close > {MIN_PRICE}", cd["close"], ">", MIN_PRICE, cd["close"] > MIN_PRICE))
    conditions.append(_cond("FUTURES_ELIGIBLE", "Futures eligible", futures_eligible, "==", True, bool(futures_eligible)))

    failed = [c["code"] for c in conditions if not c["passed"]]
    qualified = len(failed) == 0

    return {"direction": direction, "qualified": qualified, "conditions": conditions, "failed_conditions": failed}


def classify(ce_result: dict, pe_result: dict, near_qualified_max_failures: int = 2) -> dict:
    """Combines independent CE/PE results into one of:
    CE_QUALIFIED, PE_QUALIFIED, NEAR_CE, NEAR_PE, NOT_QUALIFIED, DATA_CONFLICT."""
    if ce_result["qualified"] and pe_result["qualified"]:
        return {
            "classification": "DATA_CONFLICT",
            "warning": "Stock qualified for both CE and PE in the same scan -- inspect underlying candle sources.",
        }
    if ce_result["qualified"]:
        return {"classification": "CE_QUALIFIED"}
    if pe_result["qualified"]:
        return {"classification": "PE_QUALIFIED"}

    ce_failures = len(ce_result["failed_conditions"])
    pe_failures = len(pe_result["failed_conditions"])

    if ce_failures <= near_qualified_max_failures and ce_failures <= pe_failures:
        return {"classification": "NEAR_CE"}
    if pe_failures <= near_qualified_max_failures:
        return {"classification": "NEAR_PE"}
    return {"classification": "NOT_QUALIFIED"}


def evaluate_stock(symbol: str, futures_eligible: bool = True) -> dict:
    """Full pipeline for one stock: fetch snapshot, evaluate CE + PE, classify.
    Never raises for a data problem -- returns a DATA_UNAVAILABLE result instead."""
    if not futures_eligible:
        return {
            "symbol": symbol,
            "classification": "DATA_UNAVAILABLE",
            "error_reason": "Stock is not currently eligible for the futures segment.",
        }

    try:
        snapshot = gather_snapshot(symbol)
    except DataUnavailable as e:
        return {"symbol": symbol, "classification": "DATA_UNAVAILABLE", "error_reason": e.reason}

    ce_result = evaluate_direction("CE", snapshot, futures_eligible)
    pe_result = evaluate_direction("PE", snapshot, futures_eligible)
    classification = classify(ce_result, pe_result)

    return {
        "symbol": symbol,
        "classification": classification["classification"],
        "warning": classification.get("warning"),
        "ce_conditions": ce_result["conditions"],
        "pe_conditions": pe_result["conditions"],
        "ce_passed_count": len(ce_result["conditions"]) - len(ce_result["failed_conditions"]),
        "ce_total_count": len(ce_result["conditions"]),
        "pe_passed_count": len(pe_result["conditions"]) - len(pe_result["failed_conditions"]),
        "pe_total_count": len(pe_result["conditions"]),
        "snapshot": snapshot,
        "data_timestamp": snapshot["data_timestamp"],
    }
