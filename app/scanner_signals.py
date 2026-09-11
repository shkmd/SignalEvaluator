"""Turns a freshly CE/PE-Qualified scanner result into an actual signal in a user's own
History, using the exact same evaluation pipeline as a manual/Telegram signal (technicals,
options, news, screener, stock context, scoring) plus the existing paper auto-trade engine.

Deliberately stays at the underlying-stock level (instrument=EQ) rather than picking an
option contract -- per the scanner spec, a directional stock signal is not the same thing
as an option-contract recommendation; that's a separate stage (Phase 4, not built yet).

Only fires for a genuine transition into CE_QUALIFIED/PE_QUALIFIED (the stock wasn't already
qualified for that direction in the previous scan), so one continuous qualification period
produces one signal, not one every scan.
"""
from app import db, scoring, trading
from app import technicals, options as options_mod, news as news_mod, screener, stock_score

MIN_RISK_PCT = 0.005  # floor so a same-day close==low (or close==high) never yields zero risk


def generate_signals_for_scan(scan_run_id: int) -> list:
    users = db.list_users_with_scanner_signals_enabled()
    if not users:
        return []

    generated = []
    for classification in ("CE_QUALIFIED", "PE_QUALIFIED"):
        results = db.list_scanner_results(scan_run_id, classification=classification)
        for result in results:
            prev = db.get_previous_classification(scan_run_id, result["symbol"])
            if prev == classification:
                continue  # already signaled during this same qualification streak

            for user_id in users:
                signal_id = _generate_one(user_id, result, classification)
                if signal_id:
                    generated.append({"user_id": user_id, "symbol": result["symbol"], "signal_id": signal_id})
    return generated


def _generate_one(user_id: int, result: dict, classification: str) -> int:
    snapshot = result.get("snapshot") or {}
    cd = snapshot.get("current_day")
    if not cd:
        return None

    bullish = classification == "CE_QUALIFIED"
    entry = cd["close"]
    if bullish:
        risk = max(entry - cd["low"], entry * MIN_RISK_PCT)
        sl = round(entry - risk, 2)
        target = round(entry + 2 * risk, 2)
    else:
        risk = max(cd["high"] - entry, entry * MIN_RISK_PCT)
        sl = round(entry + risk, 2)
        target = round(entry - 2 * risk, 2)

    symbol = result["symbol"]
    resolved_symbol = result.get("resolved_symbol") or symbol
    direction = "bullish" if bullish else "bearish"

    signal = {
        "raw_text": None,
        "signal_type": "positional",
        "action": "buy" if bullish else "sell",
        "symbol": symbol,
        "resolved_symbol": resolved_symbol,
        "instrument": "EQ",
        "strike": None,
        "entry_low": round(entry, 2),
        "entry_high": round(entry, 2),
        "sl": sl,
        "targets": [target],
    }

    tech = technicals.fetch_technicals(resolved_symbol, direction=direction, user_id=user_id)
    opts = {"available": False, "reason": "No strike/instrument to look up (equity signal)."}
    headlines = news_mod.fetch_news(resolved_symbol)
    scr = screener.evaluate_screener(resolved_symbol, direction)
    stock_ctx = stock_score.evaluate_stock_context(resolved_symbol)

    evaluation = scoring.evaluate_signal(signal, tech, opts, headlines, scr, stock_ctx)
    evaluation["technicals"] = tech
    evaluation["options"] = opts
    evaluation["news"] = headlines
    evaluation["screener"] = scr
    evaluation["stock_context"] = stock_ctx

    signal_id = db.insert_signal(user_id, signal, evaluation, "F&O Scanner", source="scanner")
    if signal_id:
        trading.auto_trade_check(user_id, signal_id, signal, evaluation)
    return signal_id
