"""Auto-trade engine.

Paper mode is fully simulated: no money moves, no broker is touched. It exists so you can
validate the scoring + auto-trade logic risk-free before ever connecting a real account.

Live mode requires a connected broker account AND a broker-specific order-placement adapter
(see app/brokers/). Until one is implemented for your broker, live orders are refused rather
than silently doing nothing -- see place_live_order() below.
"""
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf

from app import alerts, db, graduation, options as options_mod, technicals

MAX_WORKERS_LIVE_PRICE = 8


def get_live_price(resolved_symbol: str, instrument: str, strike: float, user_id: int = None) -> dict:
    """Best-effort current price for either an equity or an option leg.

    For an option leg, tries the user's own connected broker FIRST when a user_id is given --
    NSE's public option-chain endpoint is blocked outright (HTTP 403 on their own homepage, via
    Akamai bot protection) from most cloud/datacenter IPs, so on a server deployment like this
    one the NSE path is a fallback of last resort, not the primary source, whenever a broker
    session is actually available."""
    if instrument in ("CE", "PE") and strike:
        if user_id is not None:
            broker_result = _broker_option_ltp(user_id, resolved_symbol, strike, instrument)
            if broker_result is not None:
                return broker_result

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


def _broker_option_ltp(user_id: int, resolved_symbol: str, strike: float, instrument: str) -> dict | None:
    """Returns None (not a failure dict) when no broker is connected or the broker lookup
    itself errors, so the caller falls through to the NSE path -- only an explicit, successful
    broker answer short-circuits it."""
    try:
        from app.brokers import get_connected_adapter

        broker = get_connected_adapter(user_id)
        if not broker or not hasattr(broker, "find_option_by_strike"):
            return None
        name = resolved_symbol.split(".")[0]
        result = broker.find_option_by_strike(user_id, name, strike, instrument)
        if result.get("available") and result.get("ltp"):
            return {"available": True, "price": float(result["ltp"]), "source": result.get("source", "broker")}
        return None
    except Exception:
        return None


def enrich_open_positions(orders: list) -> list:
    """Adds the current live price and unrealized P&L to each open order -- without this, the
    Positions table can only show what a trade was entered at, not what it's worth right now or
    how close it's sitting to its own SL/target. Fetched concurrently since this runs inline in
    a GET request and a handful of sequential price lookups would make the page noticeably slow
    to load."""
    def _enrich_one(order):
        o = dict(order)
        price_info = get_live_price(o["resolved_symbol"], o["instrument"], o["strike"], user_id=o["user_id"])
        if price_info["available"]:
            price = price_info["price"]
            o["current_price"] = round(price, 2)
            o["unrealized_pnl"] = _calc_pnl(o, price)
        else:
            o["current_price"] = None
            o["unrealized_pnl"] = None
            o["price_unavailable_reason"] = price_info.get("reason")
        return o

    if not orders:
        return []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS_LIVE_PRICE) as pool:
        return list(pool.map(_enrich_one, orders))


MIN_TRADES_FOR_SIZING = 5
MIN_SIZE_MULTIPLIER = 0.5
MAX_SIZE_MULTIPLIER = 2.0


def _reliability_multiplier(win_rate: float | None, trades_closed: int) -> float:
    """50% historical win rate on this channel keeps the base quantity unchanged (1.0x);
    every point above/below scales it, clamped to 0.5x-2.0x so one hot or cold streak can't
    swing size too far. Needs MIN_TRADES_FOR_SIZING closed paper trades on the channel first --
    before that there isn't enough history to size by, so it stays at the base quantity."""
    if win_rate is None or trades_closed < MIN_TRADES_FOR_SIZING:
        return 1.0
    return max(MIN_SIZE_MULTIPLIER, min(MAX_SIZE_MULTIPLIER, win_rate / 50.0))


def size_for_reliability(user_id: int, signal_id: int, base_quantity: float) -> float:
    """Scales base_quantity by the linked signal's channel's historical paper win-rate, when
    the user has risk-based position sizing turned on (auto_trade_settings.position_sizing_enabled
    -- one shared per-user toggle, since this applies uniformly regardless of which auto-trade
    path placed the order)."""
    settings = db.get_auto_trade_settings(user_id)
    if not settings.get("position_sizing_enabled"):
        return base_quantity
    signal = db.get_signal(user_id, signal_id)
    if not signal:
        return base_quantity
    reliability = db.channel_reliability(user_id, signal["channel"])
    multiplier = _reliability_multiplier(reliability["win_rate"], reliability["trades_closed"])
    return max(1, round(base_quantity * multiplier))


def _order_side(signal: dict, evaluation: dict) -> str:
    """The actual buy/sell action for this order. Prefers signal["action"] -- set correctly
    upstream (scanner_signals.py's _build_atm_signal always sets "buy" for a CE or PE, since
    buying the option matching your directional view is the actual strategy this app trades;
    _build_equity_signal and the Telegram/manual parser set "buy"/"sell" explicitly too, for a
    genuine equity long/short or an explicitly-stated "SELL ... PE" call).

    evaluation["direction"] is a DIFFERENT concept -- the market thesis (bullish/bearish) used
    for scoring/technicals alignment -- and must never be used to infer the order's side: for
    an option, scoring.py sets direction="bearish" for every single PE by definition of the
    instrument, which is not the same question as "did we buy or sell that PE." Using it here
    silently flipped every option order to the wrong side and inverted its P&L sign. Only falls
    back to direction when a signal genuinely has no action of its own (a manually/Telegram
    parsed EQUITY signal with no BUY/SELL keyword in the text)."""
    action = signal.get("action")
    if action in ("buy", "sell"):
        return action
    return "sell" if evaluation["direction"] == "bearish" else "buy"


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
    # One signal, at most one order -- without this, a signal that qualifies for more than one
    # independently-enabled auto-trade path (general Broker-Setup auto-trade, F&O Scanner's own
    # dedicated paper-trade, a Telegram channel's dedicated paper-trade) gets traded once per
    # path instead of once, which looks like duplicate positions for the same contract.
    if signal_id and db.order_exists_for_signal(user_id, signal_id):
        return {"placed": False, "reason": "An order already exists for this signal."}

    resolved_symbol = signal["resolved_symbol"]
    instrument = signal.get("instrument", "EQ")
    strike = signal.get("strike")
    side = _order_side(signal, evaluation)

    price_info = get_live_price(resolved_symbol, instrument, strike, user_id=user_id)
    entry_price = price_info["price"] if price_info["available"] else (signal.get("entry_high") or signal.get("entry_low"))
    quantity = size_for_reliability(user_id, signal_id, settings["quantity"])

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
            "quantity": quantity,
            "entry_price": entry_price,
            "sl": signal.get("sl"),
            "target": min(targets) if targets and side == "buy" else (max(targets) if targets else None),
            "broker": None,
        },
    )
    return {"placed": True, "order_id": order_id, "mode": "paper", "entry_price": entry_price, "quantity": quantity}


def place_order_for_channel(
    user_id: int, signal_id: int, signal: dict, evaluation: dict, channel: str, base_quantity: float
) -> dict:
    """The single entry point dedicated per-source auto-trade paths (F&O Scanner, Telegram
    channels) call instead of place_paper_order() directly -- routes to a REAL place_live_order()
    if this channel has been auto-graduated to live (app/graduation.py), else stays on
    place_paper_order(), so graduation applies uniformly regardless of which source fired."""
    if graduation.is_graduated(user_id, channel):
        settings = dict(db.get_auto_trade_settings(user_id))
        settings["quantity"] = base_quantity
        return place_live_order(user_id, signal_id, signal, evaluation, settings)
    return place_paper_order(user_id, signal_id, signal, evaluation, {"quantity": base_quantity})


def place_live_order(user_id: int, signal_id: int, signal: dict, evaluation: dict, settings: dict) -> dict:
    """Places a REAL market order on the user's connected Zerodha account. Deliberately
    scoped to equity signals only -- an options signal needs a contract-selection stage
    (expiry, strike, liquidity checks) that doesn't exist yet, so it's refused with a clear
    reason rather than guessing a strike.

    Only places the entry order. SL/target are recorded on the order row for the user to
    manage -- automatic bracket-style exit via Kite (GTT orders) isn't wired up yet, unlike
    paper mode where monitor_open_positions() auto-closes on SL/target hit.
    """
    if signal_id and db.order_exists_for_signal(user_id, signal_id):
        return {"placed": False, "reason": "An order already exists for this signal."}

    if signal.get("instrument") in ("CE", "PE"):
        return {
            "placed": False,
            "reason": "Live execution is equity-only for now -- an options signal needs a "
            "separate contract-selection stage (expiry/strike/liquidity) that isn't built yet.",
        }

    from app.brokers import get_connected_adapter

    broker = get_connected_adapter(user_id)
    if not broker:
        return {
            "placed": False,
            "reason": "No broker is connected or today's session has expired -- "
            "log in again from Broker Setup (Kite, Upstox, or Dhan).",
        }

    if settings.get("max_daily_loss"):
        realized_today = db.daily_realized_pnl(user_id, mode="live")
        if realized_today <= -abs(settings["max_daily_loss"]):
            return {
                "placed": False,
                "reason": f"Max daily loss ({settings['max_daily_loss']}) already reached today -- live trading paused.",
            }

    resolved_symbol = signal["resolved_symbol"]
    side = _order_side(signal, evaluation).upper()
    quantity = int(size_for_reliability(user_id, signal_id, settings["quantity"]))

    try:
        broker_order_id, broker_name = _place_equity_order(broker, user_id, resolved_symbol, side, quantity)
    except Exception as e:
        return {"placed": False, "reason": f"{_broker_name(broker)} order placement failed: {e}"}

    try:
        entry_price = broker.fetch_ltp(user_id, resolved_symbol, "NSE")
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
            "broker": broker_name,
            "broker_order_id": broker_order_id,
        },
    )
    return {"placed": True, "order_id": order_id, "mode": "live", "entry_price": entry_price, "broker_order_id": broker_order_id}


def _broker_name(mod) -> str:
    return mod.__name__.rsplit(".", 1)[-1]


def _place_equity_order(broker, user_id: int, resolved_symbol: str, side: str, quantity: int):
    """Translates this app's generic (symbol, exchange, side, qty) shape into whichever
    broker-specific order call the connected adapter needs -- each broker's place_order()
    takes different identifiers (Kite: tradingsymbol; Upstox: instrument_key; Dhan:
    security_id), so this is the one place that bridges them. Returns (broker_order_id,
    broker name used for the orders table)."""
    name = _broker_name(broker)
    if name == "kite":
        order_id = broker.place_order(
            user_id, tradingsymbol=resolved_symbol, exchange="NSE",
            transaction_type=side, quantity=quantity, product="MIS", order_type="MARKET",
        )
        return order_id, "zerodha"
    if name == "upstox":
        instrument_key = broker.resolve_instrument_key(resolved_symbol, "NSE")
        order_id = broker.place_order(
            user_id, instrument_key, transaction_type=side, quantity=quantity, product="I", order_type="MARKET",
        )
        return order_id, "upstox"
    if name == "dhan":
        security_id, segment = broker.resolve_security_id(resolved_symbol, "NSE")
        order_id = broker.place_order(
            user_id, security_id, segment, transaction_type=side, quantity=quantity,
            product_type="INTRADAY", order_type="MARKET",
        )
        return order_id, "dhan"
    raise RuntimeError(f"No order-placement wiring for broker '{name}'.")


def close_order(user_id: int, order_id: int, reason: str = "manual_close") -> dict:
    """Closes an open position. For a live order this places a REAL offsetting order on
    Kite first (e.g. SELL to close a BUY) -- it never just marks the DB row closed while
    leaving the real position open on the broker, which would silently desync the two."""
    order = db.get_order(user_id, order_id)
    if not order or order["status"] != "open":
        return {"closed": False, "reason": "Order not found or already closed."}

    if order["mode"] == "live":
        return _close_live_order(user_id, order, reason)

    price_info = get_live_price(order["resolved_symbol"], order["instrument"], order["strike"], user_id=user_id)
    exit_price = price_info["price"] if price_info["available"] else order["entry_price"]
    pnl = _calc_pnl(order, exit_price)
    db.close_order(order_id, exit_price, reason, pnl)
    return {"closed": True, "exit_price": exit_price, "pnl": pnl}


def _close_live_order(user_id: int, order: dict, reason: str) -> dict:
    from app.brokers import get_connected_adapter

    broker = get_connected_adapter(user_id)
    if not broker:
        return {
            "closed": False,
            "reason": "No broker session is active -- log in again from Broker Setup before "
            "closing this live position (your real position is still open with your broker).",
        }

    offsetting_side = "SELL" if order["side"] == "buy" else "BUY"
    try:
        _place_equity_order(broker, user_id, order["resolved_symbol"], offsetting_side, int(order["quantity"]))
    except Exception as e:
        return {"closed": False, "reason": f"{_broker_name(broker)} offsetting order failed: {e} -- your real position is still open."}

    try:
        exit_price = broker.fetch_ltp(user_id, order["resolved_symbol"], "NSE")
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


def _check_hit(order: dict, price: float) -> str | None:
    if order["side"] == "buy":
        if order["sl"] and price <= order["sl"]:
            return "sl_hit"
        if order["target"] and price >= order["target"]:
            return "target_hit"
    else:
        if order["sl"] and price >= order["sl"]:
            return "sl_hit"
        if order["target"] and price <= order["target"]:
            return "target_hit"
    return None


def monitor_open_positions() -> list:
    """Checks every open paper position (across all users) against its SL/target using a
    live price, auto-closing on a hit. Also checks open LIVE positions for SL proximity only
    (no auto-close -- live has no bracket-exit order wired up yet, see place_live_order()) so
    the SL-approaching alert can fire there too. Meant to be called periodically by a
    background task."""
    closed = []
    for order in db.list_all_open_orders(mode="paper"):
        price_info = get_live_price(order["resolved_symbol"], order["instrument"], order["strike"], user_id=order["user_id"])
        if not price_info["available"]:
            continue
        price = price_info["price"]

        hit = _check_hit(order, price)

        if hit:
            pnl = _calc_pnl(order, price)
            db.close_order(order["id"], price, hit, pnl)
            # An automatic SL/target exit is exactly what "outcome" means for the linked
            # signal too, so sync it here -- this is what turns Channel Stats' hit-rate for
            # a source like "F&O Scanner" into a real, automatically-computed number instead
            # of something the user has to set by hand on every row. A *manual* close (see
            # close_order() above) intentionally does not touch this -- that's the user's own
            # call, not an automatic outcome.
            if order.get("signal_id"):
                db.update_outcome(order["user_id"], order["signal_id"], hit)
                # A closed paper trade is exactly what changes a channel's reliability numbers,
                # so this is the right moment to re-check whether it now qualifies for (or has
                # fallen out of) auto-graduation to live -- see app/graduation.py.
                signal = db.get_signal(order["user_id"], order["signal_id"])
                if signal:
                    graduation.check_and_graduate(order["user_id"], signal["channel"])
            closed.append({"order_id": order["id"], "reason": hit, "exit_price": price, "pnl": pnl})
        elif not order.get("sl_alert_sent"):
            if alerts.maybe_alert_sl_proximity(order["user_id"], order, price):
                db.mark_sl_alert_sent(order["id"])

    for order in db.list_all_open_orders(mode="live"):
        if order.get("sl_alert_sent"):
            continue
        price_info = get_live_price(order["resolved_symbol"], order["instrument"], order["strike"], user_id=order["user_id"])
        if not price_info["available"]:
            continue
        if alerts.maybe_alert_sl_proximity(order["user_id"], order, price_info["price"]):
            db.mark_sl_alert_sent(order["id"])

    return closed
