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


def auto_trade_check(signal_id: int, signal: dict, evaluation: dict) -> dict | None:
    """Called right after every evaluation (manual or Telegram-auto). Places a paper (or,
    once wired, live) order if auto-trade is enabled and the signal clears the bar."""
    settings = db.get_auto_trade_settings()
    if not settings["enabled"]:
        return None
    if evaluation["score"] < settings["min_score"]:
        return None
    if not signal.get("entry_low") and not signal.get("entry_high"):
        return None
    if not signal.get("sl"):
        return None

    mode = settings["mode"]
    if db.count_open_positions(mode=mode) >= settings["max_open_positions"]:
        return {"placed": False, "reason": "Max open positions reached."}

    if mode == "live":
        return place_live_order(signal_id, signal, evaluation, settings)
    return place_paper_order(signal_id, signal, evaluation, settings)


def place_paper_order(signal_id: int, signal: dict, evaluation: dict, settings: dict) -> dict:
    resolved_symbol = signal["resolved_symbol"]
    instrument = signal.get("instrument", "EQ")
    strike = signal.get("strike")
    side = "sell" if evaluation["direction"] == "bearish" else "buy"

    price_info = get_live_price(resolved_symbol, instrument, strike)
    entry_price = price_info["price"] if price_info["available"] else (signal.get("entry_high") or signal.get("entry_low"))

    targets = signal.get("targets") or []
    order_id = db.insert_order(
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
        }
    )
    return {"placed": True, "order_id": order_id, "mode": "paper", "entry_price": entry_price}


def place_live_order(signal_id: int, signal: dict, evaluation: dict, settings: dict) -> dict:
    accounts = [a for a in db.list_broker_accounts() if a["connected"]]
    if not accounts:
        return {"placed": False, "reason": "No broker connected -- connect one in Broker Setup first."}
    # No broker adapter is wired up yet -- refuse rather than silently no-op or fake a fill.
    return {
        "placed": False,
        "reason": f"Live order execution isn't implemented for {accounts[0]['broker']} yet. "
        "Paper mode works end-to-end; tell me when you're ready to wire up real order "
        "placement for this broker.",
    }


def close_paper_order(order_id: int, reason: str = "manual_close") -> dict:
    orders = db.list_orders()
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order or order["status"] != "open":
        return {"closed": False, "reason": "Order not found or already closed."}

    price_info = get_live_price(order["resolved_symbol"], order["instrument"], order["strike"])
    exit_price = price_info["price"] if price_info["available"] else order["entry_price"]
    pnl = _calc_pnl(order, exit_price)
    db.close_order(order_id, exit_price, reason, pnl)
    return {"closed": True, "exit_price": exit_price, "pnl": pnl}


def _calc_pnl(order: dict, exit_price: float) -> float:
    qty = order["quantity"]
    entry = order["entry_price"] or 0
    if order["side"] == "buy":
        return round((exit_price - entry) * qty, 2)
    return round((entry - exit_price) * qty, 2)


def monitor_open_positions() -> list:
    """Checks every open paper position against its SL/target using a live price.
    Meant to be called periodically by a background task."""
    closed = []
    for order in db.list_orders(status="open", mode="paper"):
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
