"""Historical backtesting for F&O Scanner strategies.

Walks each stock's daily candle history day by day, re-running the strategy's condition set
exactly as the live scanner would (same qualification.classify() transition-detection
semantics: only a fresh transition into CE_QUALIFIED/PE_QUALIFIED opens a trade, matching how
scanner_signals.py avoids re-signaling a stock that's already been qualified for a while).
Each qualification event is simulated forward as an equity trade using the exact same
entry/SL/target rules as scanner_signals._build_equity_signal (closing price entry, day's
low/high stop with a minimum-risk floor, 2:1 reward), until price hits the stop, hits the
target, or max_hold_days elapses (closed at that day's close).

Free daily-candle history goes back years; free intraday (15-minute) history only goes back
~60 days -- nowhere near enough for a statistically useful backtest window. So the one
condition needing intraday data (INTRADAY_15M) is approximated using the day's own close vs
the previous day's close, instead of the live scanner's true intraday snapshot -- a disclosed
simplification (see _build_snapshot_at), not a bug. Every other condition (range expansion,
weekly/monthly direction, SMA20/50, volume/price floors) runs exactly as the strategy defines
it, computed only from data available as of that historical day -- no lookahead.

Backtests are equity-only. There's no reliable free historical options-pricing data, so unlike
the live scanner (which tries an ATM option contract first), a backtest can't simulate what an
option premium would have done -- it answers "would the underlying stock's move have worked,"
not "would this exact option trade have worked."
"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from app import db, qualification, strategies as strategies_mod
from app.technicals import resolve_ticker

MIN_RISK_PCT = 0.005  # same floor scanner_signals._build_equity_signal uses
DEFAULT_MAX_HOLD_DAYS = 20
MAX_WORKERS = 8
HISTORY_BUFFER_DAYS = 140  # enough calendar days to seed SMA50 + weekly/monthly rollups


def _row(df: pd.DataFrame, idx: int) -> dict:
    r = df.iloc[idx]
    return {
        "open": float(r["Open"]), "high": float(r["High"]), "low": float(r["Low"]),
        "close": float(r["Close"]), "volume": float(r["Volume"]), "timestamp": str(df.index[idx]),
    }


def _precompute(daily: pd.DataFrame) -> dict:
    """Vectorized, whole-frame versions of everything gather_snapshot would otherwise
    recompute per day -- turns what was an O(n^2) per-symbol cost (a fresh rolling-window
    mean and a fresh weekly/monthly resample on every single day of the simulation) into a
    handful of O(n) passes done once. None of this leaks future data into day i:
      - SMA20/50 are rolling means, which only look backward by construction.
      - "This week/month's opening price" is the Open of the first trading day in the same
        week/month as day i -- a label grouping, not a lookahead, since the first day of a
        period is always <= any other day in it.
      - "This week/month's close so far" is simply day i's own close: resampling a frame
        that ends exactly at day i (as the live scanner does) always yields today's close as
        the last value of whichever weekly/monthly bucket contains today, since today is
        necessarily the last row overall. So there's no separate series to compute for it.
    """
    close = daily["Close"]
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    naive_index = daily.index.tz_localize(None) if daily.index.tz is not None else daily.index
    week_period = naive_index.to_period("W")
    month_period = naive_index.to_period("M")
    week_open = daily.groupby(week_period)["Open"].transform("first")
    month_open = daily.groupby(month_period)["Open"].transform("first")
    return {"sma20": sma20, "sma50": sma50, "week_open": week_open, "month_open": month_open}


def _build_snapshot_at(daily: pd.DataFrame, i: int, pre: dict) -> dict:
    """Mirrors qualification.gather_snapshot's math (see module docstring for the one
    disclosed difference: INTRADAY_15M uses today's own close). Returns None if there isn't
    enough history yet at this index to compute every condition."""
    if i < 8 or i + 1 < qualification.MIN_DAILY_CANDLES:
        return None

    sma20 = pre["sma20"].iloc[i]
    sma50 = pre["sma50"].iloc[i]
    if pd.isna(sma20) or pd.isna(sma50):
        return None

    current_day = _row(daily, i)
    previous_day = _row(daily, i - 1)
    previous_seven_days = [_row(daily, i - k) for k in range(1, 8)]

    return {
        "current_day": current_day,
        "previous_day": previous_day,
        "previous_seven_days": previous_seven_days,
        "current_weekly_open": float(pre["week_open"].iloc[i]),
        "current_weekly_close": current_day["close"],
        "current_monthly_open": float(pre["month_open"].iloc[i]),
        "current_monthly_close": current_day["close"],
        "weekly_provisional": False,
        "monthly_provisional": False,
        "daily_sma20": round(float(sma20), 4),
        "daily_sma50": round(float(sma50), 4),
        "latest_completed_15m_close": current_day["close"],  # approximation -- see module docstring
        "latest_15m_timestamp": current_day["timestamp"],
    }


def _open_trade(symbol: str, classification: str, entry_day: dict, entry_index: int) -> dict:
    bullish = classification == "CE_QUALIFIED"
    entry = entry_day["close"]
    if bullish:
        risk = max(entry - entry_day["low"], entry * MIN_RISK_PCT)
        sl = round(entry - risk, 2)
        target = round(entry + 2 * risk, 2)
    else:
        risk = max(entry_day["high"] - entry, entry * MIN_RISK_PCT)
        sl = round(entry + risk, 2)
        target = round(entry - 2 * risk, 2)

    return {
        "symbol": symbol,
        "direction": "CE" if bullish else "PE",
        "entry_index": entry_index,
        "entry_date": entry_day["timestamp"][:10],
        "entry_price": round(entry, 2),
        "sl": sl,
        "target": target,
        "status": "open",
    }


def _check_exit(trade: dict, day: dict) -> str:
    """Returns 'target_hit', 'sl_hit', or None. If a single session's range spans both the
    stop and the target, assumes the stop was hit first -- the conservative read when only
    OHLC (not intrabar sequencing) is available."""
    bullish = trade["direction"] == "CE"
    if bullish:
        if day["low"] <= trade["sl"]:
            return "sl_hit"
        if day["high"] >= trade["target"]:
            return "target_hit"
    else:
        if day["high"] >= trade["sl"]:
            return "sl_hit"
        if day["low"] <= trade["target"]:
            return "target_hit"
    return None


def _close_trade(trade: dict, exit_date: str, exit_price: float, reason: str, hold_days: int) -> dict:
    entry = trade["entry_price"]
    bullish = trade["direction"] == "CE"
    return_pct = ((exit_price - entry) / entry * 100) if bullish else ((entry - exit_price) / entry * 100)
    trade.update(
        status="closed",
        exit_date=exit_date,
        exit_price=round(exit_price, 2),
        exit_reason=reason,
        hold_days=hold_days,
        return_pct=round(return_pct, 3),
    )
    return trade


def _simulate_symbol(symbol: str, resolved_symbol: str, start_date: str, end_date: str, max_hold_days: int) -> tuple:
    """Returns (list_of_closed_trades, error_or_None)."""
    ticker = resolve_ticker(resolved_symbol)
    fetch_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=HISTORY_BUFFER_DAYS)).date()
    fetch_end = datetime.strptime(end_date, "%Y-%m-%d").date() + timedelta(days=1)

    try:
        daily = yf.Ticker(ticker).history(
            start=fetch_start.isoformat(), end=fetch_end.isoformat(), interval="1d", auto_adjust=True
        )
    except Exception as e:
        return [], f"Fetch failed: {e}"

    if daily is None or daily.empty:
        return [], "No historical data returned."
    daily = daily.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(daily) < qualification.MIN_DAILY_CANDLES:
        return [], f"Only {len(daily)} candles available (need at least {qualification.MIN_DAILY_CANDLES})."

    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    tz = daily.index.tz
    if tz is not None:
        start_ts = start_ts.tz_localize(tz)
        end_ts = end_ts.tz_localize(tz)

    pre = _precompute(daily)
    trades = []
    open_trade = None
    prev_classification = None

    for i in range(len(daily)):
        ts = daily.index[i]
        if ts < start_ts:
            continue
        if ts > end_ts:
            break

        day = _row(daily, i)

        if open_trade:
            reason = _check_exit(open_trade, day)
            hold_days = i - open_trade["entry_index"]
            if reason:
                exit_price = open_trade["sl"] if reason == "sl_hit" else open_trade["target"]
                trades.append(_close_trade(open_trade, day["timestamp"][:10], exit_price, reason, hold_days))
                open_trade = None
            elif hold_days >= max_hold_days:
                trades.append(_close_trade(open_trade, day["timestamp"][:10], day["close"], "time_exit", hold_days))
                open_trade = None

        # Classification (and prev_classification) is tracked every day regardless of
        # whether a trade is currently open, so the transition check on the day a trade
        # closes always compares against the *actual* prior day's classification -- never a
        # stale value frozen from before the trade opened.
        snapshot = _build_snapshot_at(daily, i, pre)
        if snapshot is None:
            continue

        ce_result = qualification.evaluate_direction("CE", snapshot, futures_eligible=True)
        pe_result = qualification.evaluate_direction("PE", snapshot, futures_eligible=True)
        classification = qualification.classify(ce_result, pe_result)["classification"]

        if open_trade is None and classification in ("CE_QUALIFIED", "PE_QUALIFIED") and classification != prev_classification:
            open_trade = _open_trade(symbol, classification, day, i)
        prev_classification = classification

    if open_trade:
        last_day = _row(daily, len(daily) - 1)
        hold_days = (len(daily) - 1) - open_trade["entry_index"]
        trades.append(_close_trade(open_trade, last_day["timestamp"][:10], last_day["close"], "time_exit", hold_days))

    return trades, None


def start_backtest(
    user_id: int,
    strategy_id: str,
    symbols: list,
    start_date: str,
    end_date: str,
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
) -> int:
    """Validates inputs, creates the run row, and kicks off the actual simulation on a
    background thread -- a full-universe, multi-year backtest can take minutes even after
    the per-symbol speedups below, far longer than an HTTP request (or a proxy in front of
    one) should be held open for. The caller gets the run id back immediately and polls
    GET /api/backtest/runs/{id} for status, the same pattern the F&O Scanner already uses
    for "is this run done yet." Returns the new backtest_run_id."""
    if not strategies_mod.is_valid_strategy_id(strategy_id):
        raise ValueError(f"Unknown strategy '{strategy_id}'.")
    if not symbols:
        raise ValueError("No symbols to backtest.")

    backtest_run_id = db.create_backtest_run(user_id, strategy_id, symbols, start_date, end_date, max_hold_days)

    thread = threading.Thread(
        target=_execute_backtest,
        args=(backtest_run_id, symbols, start_date, end_date, max_hold_days),
        daemon=True,
    )
    thread.start()
    return backtest_run_id


def _execute_backtest(backtest_run_id: int, symbols: list, start_date: str, end_date: str, max_hold_days: int) -> None:
    universe_by_symbol = {s["symbol"]: s for s in db.list_fo_universe(include_indices=False)}
    all_trades = []
    errors = {}

    def _run_one(symbol):
        stock = universe_by_symbol.get(symbol)
        resolved = stock["resolved_symbol"] if stock else symbol
        return symbol, *_simulate_symbol(symbol, resolved, start_date, end_date, max_hold_days)

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(_run_one, s) for s in symbols]
            for future in as_completed(futures):
                symbol, trades, error = future.result()
                if error:
                    errors[symbol] = error
                for t in trades:
                    db.insert_backtest_trade(backtest_run_id, t)
                    all_trades.append(t)
    except Exception as e:
        db.fail_backtest_run(backtest_run_id, str(e))
        return

    stats = _aggregate(all_trades, len(symbols), errors)
    db.finish_backtest_run(backtest_run_id, stats)


def _aggregate(trades: list, symbols_scanned: int, errors: dict) -> dict:
    total = len(trades)
    wins = [t for t in trades if t["exit_reason"] == "target_hit"]
    losses = [t for t in trades if t["exit_reason"] == "sl_hit"]
    time_exits = [t for t in trades if t["exit_reason"] == "time_exit"]

    returns = [t["return_pct"] for t in trades]
    win_returns = [t["return_pct"] for t in wins]
    loss_returns = [t["return_pct"] for t in losses]

    gross_profit = sum(r for r in returns if r > 0)
    gross_loss = abs(sum(r for r in returns if r < 0))

    return {
        "symbols_scanned": symbols_scanned,
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "time_exits": len(time_exits),
        "win_rate": round(len(wins) / total * 100, 2) if total else None,
        "avg_return_pct": round(sum(returns) / total, 3) if total else None,
        "avg_win_pct": round(sum(win_returns) / len(win_returns), 3) if win_returns else None,
        "avg_loss_pct": round(sum(loss_returns) / len(loss_returns), 3) if loss_returns else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "errors": errors,
    }
