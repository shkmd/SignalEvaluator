"""Turns a freshly CE/PE-Qualified scanner result into an actual signal in a user's own
History, using the exact same evaluation pipeline as a manual/Telegram signal (technicals,
options, news, screener, stock context, scoring) plus the existing paper auto-trade engine.

Picks the ATM (at-the-money) option contract for the qualified direction -- the nearest
strike to the current price at the nearest expiry, via the user's Kite session if connected,
else NSE's best-effort option chain. ATM is a deliberately simple, conservative choice: best
liquidity of any strike band, ~0.5 delta, no ITM/OTM risk-preference judgment call baked in.
Falls back to an equity-level signal (no strike) if no ATM contract data is available at all,
so a scanner qualification never silently produces nothing.

Only fires for a genuine transition into CE_QUALIFIED/PE_QUALIFIED (the stock wasn't already
qualified for that direction in the previous scan), so one continuous qualification period
produces one signal, not one every scan.
"""
from app import db, scoring, trading, telegram_broadcast, alerts
from app import technicals, options as options_mod, news as news_mod, screener, stock_score

MIN_RISK_PCT = 0.005  # floor so a same-day close==low (or close==high) never yields zero risk
PREMIUM_SL_PCT = 0.30  # options move fast -- stop-loss as a % of premium, not underlying range


def generate_signals_for_scan(scan_run_id: int) -> list:
    scan_run = db.get_scan_run(scan_run_id)
    strategy_id = scan_run["strategy_id"] if scan_run else None

    candidate_users = db.list_users_with_scanner_signals_enabled()
    # A user only gets signals from a strategy they've actually left enabled (default-on,
    # see get_user_strategy_settings) -- "generate signals" is necessary but not sufficient.
    users = [u for u in candidate_users if db.is_strategy_enabled_for_user(u, strategy_id)] if strategy_id else candidate_users
    if not users:
        return []

    generated = []
    for classification in ("CE_QUALIFIED", "PE_QUALIFIED"):
        results = db.list_scanner_results(scan_run_id, classification=classification)
        for result in results:
            prev = db.get_previous_classification(scan_run_id, result["symbol"], strategy_id=strategy_id)
            if prev == classification:
                continue  # already signaled during this same qualification streak

            for user_id in users:
                signal_id = _generate_one(user_id, result, classification)
                if signal_id:
                    generated.append({"user_id": user_id, "symbol": result["symbol"], "signal_id": signal_id})
    return generated


def _build_atm_signal(
    symbol: str, resolved_symbol: str, bullish: bool, current_price: float, user_id: int, moneyness: str = "ATM"
) -> dict:
    """Returns a signal dict for the option at the user's chosen moneyness (ITM/ATM/OTM), or
    None if no contract data is available from either Kite or NSE."""
    instrument = "CE" if bullish else "PE"
    atm = options_mod.find_atm_option(resolved_symbol, current_price, instrument, user_id=user_id, moneyness=moneyness)
    if not atm.get("available") or not atm.get("ltp"):
        return None

    premium = float(atm["ltp"])
    sl = round(premium * (1 - PREMIUM_SL_PCT), 2)
    risk = premium - sl
    target = round(premium + 2 * risk, 2)

    return {
        "raw_text": None,
        "signal_type": "positional",
        "action": "buy",  # both CE and PE signals here are *buying* the option contract
        "symbol": symbol,
        "resolved_symbol": resolved_symbol,
        "instrument": instrument,
        "strike": atm["strike"],
        "entry_low": premium,
        "entry_high": premium,
        "sl": sl,
        "targets": [target],
        "lot_size": atm.get("lot_size"),
    }, atm


def _build_equity_signal(symbol: str, resolved_symbol: str, bullish: bool, cd: dict) -> dict:
    entry = cd["close"]
    if bullish:
        risk = max(entry - cd["low"], entry * MIN_RISK_PCT)
        sl = round(entry - risk, 2)
        target = round(entry + 2 * risk, 2)
    else:
        risk = max(cd["high"] - entry, entry * MIN_RISK_PCT)
        sl = round(entry + risk, 2)
        target = round(entry - 2 * risk, 2)

    return {
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


def _generate_one(user_id: int, result: dict, classification: str) -> int:
    snapshot = result.get("snapshot") or {}
    cd = snapshot.get("current_day")
    if not cd:
        return None

    bullish = classification == "CE_QUALIFIED"
    symbol = result["symbol"]
    resolved_symbol = result.get("resolved_symbol") or symbol
    direction = "bullish" if bullish else "bearish"

    strike_preference = db.get_scanner_signal_settings(user_id).get("strike_preference", "ATM")
    atm_build = _build_atm_signal(symbol, resolved_symbol, bullish, cd["close"], user_id, moneyness=strike_preference)
    if atm_build:
        signal, atm = atm_build
    else:
        signal = _build_equity_signal(symbol, resolved_symbol, bullish, cd)
        atm = None

    tech = technicals.fetch_technicals(resolved_symbol, direction=direction, user_id=user_id)
    if signal["instrument"] in ("CE", "PE"):
        opts = options_mod.fetch_option_chain_snapshot(resolved_symbol, signal["strike"], signal["instrument"])
        # find_atm_option already fetched this leg -- reuse it instead of a second network call
        # if the dedicated snapshot fetch comes back empty (e.g. NSE rate-limited the 2nd hit).
        if not opts.get("available") and atm:
            opts = {
                "available": True,
                "expiry": atm.get("expiry"),
                "strike": atm.get("strike"),
                "instrument": signal["instrument"],
                "oi": atm.get("oi"),
                "change_in_oi": atm.get("change_in_oi"),
                "iv": atm.get("iv"),
                "ltp": atm.get("ltp"),
                "volume": atm.get("volume"),
            }
    else:
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
        _maybe_paper_trade(user_id, signal_id, signal, evaluation)
        telegram_broadcast.maybe_broadcast(user_id, signal, evaluation, signal_id)
        alerts.maybe_alert_signal(user_id, signal, evaluation, signal_id)
    return signal_id


def _maybe_paper_trade(user_id: int, signal_id: int, signal: dict, evaluation: dict) -> None:
    """Dedicated paper-trading for F&O Scanner signals -- separate from the general Broker
    Setup auto-trade toggle (which spans every signal source and can be set to live mode).
    This is specifically about building a real forward-tested track record for the scanner +
    strategy combo, gated on its own score threshold (default 70, i.e. "Strong setup"), so a
    user doesn't have to turn on the general auto-trade toggle (with its live-mode option)
    just to see how reliable the scanner actually is. Positions close automatically on SL/
    target hit via trading.monitor_open_positions(), which also syncs the outcome back onto
    this signal -- so Channel Stats' hit-rate for "F&O Scanner" becomes a real, hands-off
    number instead of something the user has to mark by hand. If the user has turned on
    auto-graduate (Broker Setup) and "F&O Scanner" proves itself, trading.place_order_for_channel()
    below starts placing REAL orders automatically -- see app/graduation.py."""
    settings = db.get_scanner_signal_settings(user_id)
    if not settings.get("paper_trade_enabled"):
        return
    if (evaluation.get("score") or 0) < settings.get("paper_trade_min_score", 70):
        return
    if not signal.get("entry_low") and not signal.get("entry_high"):
        return
    if not signal.get("sl"):
        return
    try:
        trading.place_order_for_channel(user_id, signal_id, signal, evaluation, "F&O Scanner", signal.get("lot_size") or 1)
    except Exception as e:
        print(f"[scanner] Paper trade failed for signal {signal_id}: {e}")
