"""Auto-trade engine.

Paper mode is fully simulated: no money moves, no broker is touched. It exists so you can
validate the scoring + auto-trade logic risk-free before ever connecting a real account.

Live mode requires a connected broker account AND a broker-specific order-placement adapter
(see app/brokers/). Until one is implemented for your broker, live orders are refused rather
than silently doing nothing -- see place_live_order() below.
"""
import yfinance as yf

from app import db, options as options_mod, technicals


def get_live_price(resolved_symbol: str, instrument: str, strike: float) -> dict:
    """Best-effort current price for either an equity or an option leg."""
    if instrument in ("CE", "PE") and strike:
        snap = options_mod.fetch_option_chain_snapshot(resolved_symbol, strike, instrument)
        if snap.get("available") and snap.get("ltp"):
            return {"available": True, "price": float(snap["ltp"]), "source": "nse_option_chain"}
        return {"available": False, "reason": snap.get("reason", "Option LTP not available.")}

    try:
        ticker = technicals.resolve_ticker(resolved_symbol)
        info = yf.Ticker(ticker).fast_info
        return {"available": True, "price": float(info["last_price"]), "source": "yfinance"}
    except Exception as e:
        return {"available": False, "reason": f"Price fetch failed: {e}"}


def auto_trade_check(user_id: int, signal_id: int, signal: dict, evaluation: dict) -> dict | None:
    """Called right after every evaluation (manual or Telegram-auto). Places a paper (or,
    once wired, live) order if this user's auto-trade is enabled and the signal clears the bar."""
    settings = db.get_auto_trade_settings(user_id)
    if not settings["enabled"]:
        return None
    if evaluation["score"] < settings["min_score"]:
        return None
    if not signal.get("entry_low") and not signal.get("entry_high"):
        return None
    if not signal.get("sl"):
        return None

    mode = settings["mode"]
    if db.count_open_positions(user_id, mode=mode) >= settings["max_open_positions"]:
        return {"placed": False, "reason": "Max open positions reached."}

    if mode == "live":
        return place_live_order(user_id, signal_id, signal, evaluation, settings)
    return place_paper_order(user_id, signal_id, signal, evaluation, settings)


def place_paper_order(user_id: int, signal_id: int, signal: dict, evaluation: dict, settings: dict) -> dict:
    resolved_symbol = signal["resolved_symbol"]
    instrument = signal.get("instrument", "EQ")
    strike = signal.get("strike")
    side = "sell" if evaluation["direction"] == "bearish" else "buy"

    price_info = get_live_price(resolved_symbol, instrument, strike)
    entry_price = price_info["price"] if price_info["available"] else (signal.get("entry_high") or signal.get("entry_low"))

    targets = signal.get("targets") or []
    order_id = db.insert_order(
        user_id,
        {
            "signal_id": signal_id,
            "mode": "paper",
            "symbol": signal.get("symbol"),
            "resolved_symbol": resolved_symbol,
            "instrument": instrument,
            "strike": strike,
            "side": side,
            "quantity": settings["quantity"],
            "entry_price": entry_price,
            "sl": signal.get("sl"),
            "target": min(targets) if targets and side == "buy" else (max(targets) if targets else None),
            "broker": None,
        },
    )
    return {"placed": True, "order_id": order_id, "mode": "paper", "entry_price": entry_price}


def place_live_order(user_id: int, signal_id: int, signal: dict, evaluation: dict, settings: dict) -> dict:
    """Places a REAL market order on the user's connected Zerodha account. Deliberately
    scoped to equity signals only -- an options signal needs a contract-selection stage
    (expiry, strike, liquidity checks) that doesn't exist yet, so it's refused with a clear
    reason rather than guessing a strike.

    Only places the entry order. SL/target are recorded on the order row for the user to
    manage -- automatic bracket-style exit via Kite (GTT orders) isn't wired up yet, unlike
    paper mode where monitor_open_positions() auto-closes on SL/target hit.
    """
    if signal.get("instrument") in ("CE", "PE"):
        return {
            "placed": False,
            "reason": "Live execution is equity-only for now -- an options signal needs a "
            "separate contract-selection stage (expiry/strike/liquidity) that isn't built yet.",
        }

    try:
        from app.brokers import kite as kite_broker
    except ImportError:
        return {"placed": False, "reason": "Kite Connect adapter not available."}

    if not kite_broker.has_valid_session(user_id):
        return {
            "placed": False,
            "reason": "Kite Connect isn't connected or today's session has expired -- "
            "log in again from Broker Setup.",
        }

    if settings.get("max_daily_loss"):
        realized_today = db.daily_realized_pnl(user_id, mode="live")
        if realized_today <= -abs(settings["max_daily_loss"]):
            return {
                "placed": False,
                "reason": f"Max daily loss ({settings['max_daily_loss']}) already reached today -- live trading paused.",
            }

    resolved_symbol = signal["resolved_symbol"]
    side = "SELL" if evaluation["direction"] == "bearish" else "BUY"
    quantity = int(settings["quantity"])

    try:
        kite_order_id = kite_broker.place_order(
            user_id,
            tradingsymbol=resolved_symbol,
            exchange="NSE",
            transaction_type=side,
            quantity=quantity,
            product="MIS",
            order_type="MARKET",
        )
    except Exception as e:
        return {"placed": False, "reason": f"Kite order placement failed: {e}"}

    try:
        entry_price = kite_broker.fetch_ltp(user_id, resolved_symbol, "NSE")
    except Exception:
        entry_price = signal.get("entry_high") or signal.get("entry_low")

    targets = signal.get("targets") or []
    order_id = db.insert_order(
        user_id,
        {
            "signal_id": signal_id,
            "mode": "live",
            "symbol": signal.get("symbol"),
            "resolved_symbol": resolved_symbol,
            "instrument": "EQ",
            "strike": None,
            "side": side.lower(),
            "quantity": quantity,
            "entry_price": entry_price,
            "sl": signal.get("sl"),
            "target": min(targets) if targets and side == "BUY" else (max(targets) if targets else None),
            "broker": "zerodha",
            "broker_order_id": kite_order_id,
        },
    )
    return {"placed": True, "order_id": order_id, "mode": "live", "entry_price": entry_price, "kite_order_id": kite_order_id}


def close_order(user_id: int, order_id: int, reason: str = "manual_close") -> dict:
    """Closes an open position. For a live order this places a REAL offsetting order on
    Kite first (e.g. SELL to close a BUY) -- it never just marks the DB row closed while
    leaving the real position open on the broker, which would silently desync the two."""
    order = db.get_order(user_id, order_id)
    if not order or order["status"] != "open":
        return {"closed": False, "reason": "Order not found or already closed."}

    if order["mode"] == "live":
        return _close_live_order(user_id, order, reason)

    price_info = get_live_price(order["resolved_symbol"], order["instrument"], order["strike"])
    exit_price = price_info["price"] if price_info["available"] else order["entry_price"]
    pnl = _calc_pnl(order, exit_price)
    db.close_order(order_id, exit_price, reason, pnl)
    return {"closed": True, "exit_price": exit_price, "pnl": pnl}


def _close_live_order(user_id: int, order: dict, reason: str) -> dict:
    try:
        from app.brokers import kite as kite_broker
    except ImportError:
        return {"closed": False, "reason": "Kite Connect adapter not available."}

    if not kite_broker.has_valid_session(user_id):
        return {
            "closed": False,
            "reason": "Kite Connect session has expired -- log in again from Broker Setup "
            "before closing this live position (your real position is still open on Zerodha).",
        }

    offsetting_side = "SELL" if order["side"] == "buy" else "BUY"
    try:
        kite_broker.place_order(
            user_id,
            tradingsymbol=order["resolved_symbol"],
            exchange="NSE",
            transaction_type=offsetting_side,
            quantity=int(order["quantity"]),
            product="MIS",
            order_type="MARKET",
        )
    except Exception as e:
        return {"closed": False, "reason": f"Kite offsetting order failed: {e} -- your real position is still open."}

    try:
        exit_price = kite_broker.fetch_ltp(user_id, order["resolved_symbol"], "NSE")
    except Exception:
        exit_price = order["entry_price"]

    pnl = _calc_pnl(order, exit_price)
    db.close_order(order["id"], exit_price, reason, pnl)
    return {"closed": True, "exit_price": exit_price, "pnl": pnl}


def _calc_pnl(order: dict, exit_price: float) -> float:
    qty = order["quantity"]
    entry = order["entry_price"] or 0
    if order["side"] == "buy":
        return round((exit_price - entry) * qty, 2)
    return round((entry - exit_price) * qty, 2)


def monitor_open_positions() -> list:
    """Checks every open paper position (across all users) against its SL/target using a
    live price. Meant to be called periodically by a background task."""
    closed = []
    for order in db.list_all_open_orders(mode="paper"):
        price_info = get_live_price(order["resolved_symbol"], order["instrument"], order["strike"])
        if not price_info["available"]:
            continue
        price = price_info["price"]

        hit = None
        if order["side"] == "buy":
            if order["sl"] and price <= order["sl"]:
                hit = "sl_hit"
            elif order["target"] and price >= order["target"]:
                hit = "target_hit"
        else:
            if order["sl"] and price >= order["sl"]:
                hit = "sl_hit"
            elif order["target"] and price <= order["target"]:
                hit = "target_hit"

        if hit:
            pnl = _calc_pnl(order, price)
            db.close_order(order["id"], price, hit, pnl)
            closed.append({"order_id": order["id"], "reason": hit, "exit_price": price, "pnl": pnl})
    return closed
