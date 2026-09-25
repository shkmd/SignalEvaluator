"""Turns a Chartink webhook alert into signals in the triggering user's History, using the
same evaluation pipeline as a manual/Telegram/Scanner signal.

Chartink's webhook payload only says which stocks matched a scan and at what price -- it
never says whether the scan itself is bullish or bearish (a "breakout" scan and a "breakdown"
scan look identical on the wire). So each distinct scan is auto-registered on its first hit
(see db.get_or_create_chartink_scan) and the user assigns it a direction once, from the
Chartink tab -- every later hit from that same scan reuses it.

No option-chain lookup here (Chartink screens equities, not specific option contracts), so
every Chartink-sourced signal is an equity ("EQ") signal: entry = the price Chartink reports
the stock triggered at, SL/target sized off ATR (a scan doesn't carry today's high/low the way
an F&O Scanner snapshot does, but 14-day ATR is fetched anyway as part of scoring).
"""
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

from app import db, scoring, trading, telegram_broadcast, alerts, confluence
from app import technicals, news as news_mod, screener, stock_score

MIN_RISK_PCT = 0.005  # floor so a near-zero ATR (illiquid/newly-listed stock) never yields ~zero risk
ATR_RISK_MULT = 1.5


def parse_body(raw: bytes) -> dict:
    """Chartink's documented payload is JSON, but nothing here should depend on the sender's
    Content-Type being what we expect -- a strict Pydantic body model would reject anything else
    with a 422 and the alert would silently vanish from the sender's side. Tries JSON first, then
    falls back to a form-encoded body; every value is coerced to a string."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items() if v is not None}
    except ValueError:
        pass
    return {k: v[0] for k, v in parse_qs(text).items() if v}


def _parse_stocks(payload: dict) -> list:
    stocks = [s.strip().upper() for s in (payload.get("stocks") or "").split(",") if s.strip()]
    prices_raw = [p.strip() for p in (payload.get("trigger_prices") or "").split(",") if p.strip()]
    prices = []
    for p in prices_raw:
        try:
            prices.append(float(p))
        except ValueError:
            prices.append(None)
    # Chartink sends stocks/trigger_prices as parallel comma lists -- if they're mismatched
    # (malformed alert config, truncated payload) fall back to no trigger price per stock
    # rather than mis-pairing a stock with the wrong price.
    if len(prices) != len(stocks):
        prices = [None] * len(stocks)
    return list(zip(stocks, prices))


def _build_signal(symbol: str, resolved_symbol: str, bullish: bool, entry: float, atr: float = None) -> dict:
    risk = max((atr or 0) * ATR_RISK_MULT, entry * MIN_RISK_PCT)
    if bullish:
        sl = round(entry - risk, 2)
        target = round(entry + 2 * risk, 2)
    else:
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


def process_webhook(token: str, payload: dict) -> dict:
    user_id = db.get_user_id_by_chartink_token(token)
    if not user_id:
        raise ValueError("Unknown or revoked Chartink webhook token")

    scan_url = (payload.get("scan_url") or payload.get("scan_name") or "chartink").strip()
    scan_name = (payload.get("scan_name") or scan_url).strip()
    triggered_at = payload.get("triggered_at") or datetime.now(timezone.utc).isoformat()

    scan = db.get_or_create_chartink_scan(user_id, scan_url, scan_name)
    if not scan["enabled"]:
        return {"processed": 0, "reason": "scan disabled"}

    bullish = scan["direction"] != "bearish"
    generated = []
    for symbol, trigger_price in _parse_stocks(payload):
        try:
            signal_id = _generate_one(user_id, scan, symbol, trigger_price, bullish, scan_url, triggered_at)
            if signal_id:
                generated.append({"symbol": symbol, "signal_id": signal_id})
        except Exception as e:
            print(f"[chartink] user {user_id}: failed on {symbol}: {e}")
    return {"processed": len(generated), "signals": generated}


def _generate_one(user_id: int, scan: dict, symbol: str, trigger_price: float, bullish: bool, scan_url: str, triggered_at: str) -> int:
    resolved_symbol = symbol
    direction = "bullish" if bullish else "bearish"

    tech = technicals.fetch_technicals(resolved_symbol, direction=direction, user_id=user_id)
    entry = trigger_price if trigger_price is not None else (tech.get("last_close") if tech.get("available") else None)
    if entry is None:
        return None  # no trigger price from Chartink and no fallback price available -- nothing to build a signal on

    atr = tech.get("atr14") if tech.get("available") else None
    signal = _build_signal(symbol, resolved_symbol, bullish, entry, atr)

    opts = {"available": False, "reason": "Chartink signals are equity-level (no option contract)."}
    headlines = news_mod.fetch_news(resolved_symbol)
    scr = screener.evaluate_screener(resolved_symbol, direction)
    stock_ctx = stock_score.evaluate_stock_context(resolved_symbol)

    evaluation = scoring.evaluate_signal(signal, tech, opts, headlines, scr, stock_ctx)
    evaluation["technicals"] = tech
    evaluation["options"] = opts
    evaluation["news"] = headlines
    evaluation["screener"] = scr
    evaluation["stock_context"] = stock_ctx

    external_ref = f"{scan_url}|{symbol}|{triggered_at}"
    signal_id = db.insert_signal(
        user_id, signal, evaluation, scan["scan_name"] or scan_url, source="chartink", external_ref=external_ref
    )
    if signal_id:
        trading.auto_trade_check(user_id, signal_id, signal, evaluation)
        _maybe_paper_trade(user_id, signal_id, signal, evaluation, scan)
        telegram_broadcast.maybe_broadcast(user_id, signal, evaluation, signal_id)
        alerts.maybe_alert_signal(user_id, signal, evaluation, signal_id)
        confluence.check_and_trade(user_id, signal_id)
    return signal_id


def _maybe_paper_trade(user_id: int, signal_id: int, signal: dict, evaluation: dict, scan: dict) -> None:
    """Dedicated per-scan paper-trading, same pattern as scanner_signals._maybe_paper_trade and
    telegram_ingest._maybe_paper_trade -- separate from the general Broker Setup auto-trade so a
    scan's own hit-rate (Channel Stats, grouped by this scan's name) is measurable hands-off."""
    if not scan.get("paper_trade_enabled"):
        return
    if (evaluation.get("score") or 0) < (scan.get("paper_trade_min_score") or 70):
        return
    if not signal.get("entry_low") or not signal.get("sl"):
        return
    try:
        trading.place_order_for_channel(user_id, signal_id, signal, evaluation, scan["scan_name"] or scan["scan_url"], 1)
    except Exception as e:
        print(f"[chartink] Paper trade failed for signal {signal_id}: {e}")
