"""Auto-trade engine.

Paper mode is fully simulated: no money moves, no broker is touched. It exists so you can
validate the scoring + auto-trade logic risk-free before ever connecting a real account.

Live mode requires a connected broker account AND a broker-specific order-placement adapter
(see app/brokers/). Until one is implemented for your broker, live orders are refused rather
than silently doing nothing -- see place_live_order() below.
"""
import math
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

    if instrument in ("CE", "PE"):
        # Same reasoning as _place_option_order()'s live-order rounding: NSE options only
        # trade in whole lots, so a paper position sized in raw "shares" (e.g. the general
        # Auto-Trade toggle's "Quantity per trade" taken literally) doesn't reflect a real,
        # tradable position and makes its P&L meaningless for judging this channel's
        # reliability. Round up to at least one full lot using the contract's real lot size.
        lot_size = signal.get("lot_size") or db.get_lot_size(resolved_symbol) or 1
        quantity = max(1, math.ceil(quantity / lot_size)) * lot_size

    targets = signal.get("targets") or []
    # Risk Manager defaults: independent of which auto-trade path opened this order (general
    # Broker-Setup auto-trade, F&O Scanner, a Telegram channel) -- trailing/profit-lock is a
    # global per-user preference, same pattern as size_for_reliability()'s own settings lookup.
    risk_defaults = db.get_auto_trade_settings(user_id)
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
            "trailing_enabled": risk_defaults.get("default_trailing_enabled"),
            "trail_pct": risk_defaults.get("default_trail_pct"),
            "lock_trigger_pct": risk_defaults.get("default_lock_trigger_pct") if risk_defaults.get("default_lock_enabled") else None,
            "lock_pct": risk_defaults.get("default_lock_pct") if risk_defaults.get("default_lock_enabled") else None,
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
    """Places a REAL market order on the user's connected broker account -- equity directly,
    or (see _place_option_order) an options contract resolved fresh at order time, since a
    signal's strike/expiry can go stale between when it was scored and when this actually
    fires.

    Only places the entry order. SL/target are recorded on the order row for the user to
    manage -- automatic bracket-style exit isn't wired up yet, unlike paper mode where
    monitor_open_positions() auto-closes on SL/target hit. For a live position,
    monitor_open_positions() only sends an SL-proximity alert; closing it (manually, from
    Positions) is what places the real offsetting order -- see close_order().
    """
    if signal_id and db.order_exists_for_signal(user_id, signal_id):
        return {"placed": False, "reason": "An order already exists for this signal."}

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
    base_quantity = size_for_reliability(user_id, signal_id, settings["quantity"])
    instrument = signal.get("instrument", "EQ")
    targets = signal.get("targets") or []

    if instrument in ("CE", "PE"):
        strike = signal.get("strike")
        if not strike:
            return {"placed": False, "reason": "Signal has no strike recorded -- can't place a live options order."}
        try:
            broker_order_id, broker_name, contract, quantity = _place_option_order(
                broker, user_id, resolved_symbol, strike, instrument, side, int(base_quantity)
            )
        except Exception as e:
            return {"placed": False, "reason": f"{_broker_name(broker)} options order placement failed: {e}"}
        entry_price = contract.get("ltp") or signal.get("entry_high") or signal.get("entry_low")
        order_strike = contract.get("strike", strike)
    else:
        quantity = int(base_quantity)
        try:
            broker_order_id, broker_name = _place_equity_order(broker, user_id, resolved_symbol, side, quantity)
        except Exception as e:
            return {"placed": False, "reason": f"{_broker_name(broker)} order placement failed: {e}"}
        try:
            entry_price = broker.fetch_ltp(user_id, resolved_symbol, "NSE")
        except Exception:
            entry_price = signal.get("entry_high") or signal.get("entry_low")
        order_strike = None

    order_id = db.insert_order(
        user_id,
        {
            "signal_id": signal_id,
            "mode": "live",
            "symbol": signal.get("symbol"),
            "resolved_symbol": resolved_symbol,
            "instrument": instrument,
            "strike": order_strike,
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


def _place_option_order(broker, user_id: int, name: str, strike: float, instrument: str, side: str, quantity: int):
    """Resolves the exact option contract fresh via the broker's own instrument list -- never
    trusts a signal's strike/expiry as still the right contract by the time this actually
    fires -- then places a REAL order on it. `quantity` is rounded UP to at least one full lot
    using the broker's own lot size, since NSE options can only trade in exact lot multiples
    and a real order for a non-lot quantity would be rejected (or worse, misinterpreted) by the
    broker. Returns (broker_order_id, broker_name, resolved_contract, final_quantity)."""
    name_only = name.split(".")[0]  # tolerate a ".NS"-style suffix if the caller passed one
    contract = broker.find_option_by_strike(user_id, name_only, strike, instrument)
    if not contract.get("available"):
        raise RuntimeError(contract.get("reason", "Could not resolve the option contract."))
    if not contract.get("ltp"):
        raise RuntimeError(
            f"No live quote for {contract.get('tradingsymbol', name_only)} -- refusing to place a live order blind."
        )

    # `quantity` here is a share-count target, same meaning it has for equity -- round UP to
    # the nearest whole lot rather than to the nearest, so a small configured quantity (e.g. 1)
    # never gets rounded away to less exposure than requested, and never gets silently rejected
    # by the broker for not being a lot multiple.
    lot_size = contract.get("lot_size") or 1
    lots = max(1, math.ceil(quantity / lot_size))
    final_quantity = lots * lot_size

    broker_name = _broker_name(broker)
    if broker_name == "kite":
        order_id = broker.place_order(
            user_id, tradingsymbol=contract["tradingsymbol"], exchange="NFO",
            transaction_type=side, quantity=final_quantity, product="MIS", order_type="MARKET",
        )
    elif broker_name == "upstox":
        order_id = broker.place_order(
            user_id, contract["instrument_key"], transaction_type=side, quantity=final_quantity,
            product="I", order_type="MARKET",
        )
    elif broker_name == "dhan":
        order_id = broker.place_order(
            user_id, contract["security_id"], "NSE_FNO", transaction_type=side,
            quantity=final_quantity, product_type="INTRADAY", order_type="MARKET",
        )
    else:
        raise RuntimeError(f"No options order-placement wiring for broker '{broker_name}'.")

    return order_id, broker_name, contract, final_quantity


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
        if order["instrument"] in ("CE", "PE"):
            # Same strike the position was actually opened at, at the nearest upcoming expiry
            # -- never re-pick ATM, that could resolve to a completely different contract.
            _, _, contract, _ = _place_option_order(
                broker, user_id, order["resolved_symbol"], order["strike"], order["instrument"],
                offsetting_side, int(order["quantity"]),
            )
            exit_price = contract.get("ltp") or order["entry_price"]
        else:
            _place_equity_order(broker, user_id, order["resolved_symbol"], offsetting_side, int(order["quantity"]))
            try:
                exit_price = broker.fetch_ltp(user_id, order["resolved_symbol"], "NSE")
            except Exception:
                exit_price = order["entry_price"]
    except Exception as e:
        return {"closed": False, "reason": f"{_broker_name(broker)} offsetting order failed: {e} -- your real position is still open."}

    pnl = _calc_pnl(order, exit_price)
    db.close_order(order["id"], exit_price, reason, pnl)
    return {"closed": True, "exit_price": exit_price, "pnl": pnl}


def _calc_pnl(order: dict, exit_price: float) -> float:
    qty = order["quantity"]
    entry = order["entry_price"] or 0
    if order["side"] == "buy":
        return round((exit_price - entry) * qty, 2)
    return round((entry - exit_price) * qty, 2)


def apply_risk_management(order: dict, current_price: float) -> dict:
    """Risk Manager: trailing stop-loss and one-time profit-lock, evaluated fresh every monitor
    tick. Always returns the order's up-to-date sl/peak_price/profit_locked plus a "changed"
    flag -- the caller applies these to the order dict for THIS tick's hit-check even when
    changed is False, so callers don't need their own separate fallback logic.

    - Profit lock (one-time ratchet): once unrealized profit crosses lock_trigger_pct, SL jumps
      to the level that locks in lock_pct profit -- fires once (profit_locked), not repeatedly.
    - Trailing stop (continuous): once trailing_enabled, SL follows the best price seen since
      entry (peak_price) by trail_pct, re-evaluated every tick.
    - Combined, and either alone: SL only ever tightens (moves toward locking in more profit),
      never loosens, regardless of which rule proposed the move -- the better of the two wins.
    """
    is_long = order["side"] == "buy"
    entry = order.get("entry_price")
    sl = order.get("sl")
    peak_price = order.get("peak_price") or entry
    profit_locked = bool(order.get("profit_locked"))
    changed = False

    if entry:
        new_peak = max(peak_price, current_price) if is_long else min(peak_price, current_price)
        if new_peak != peak_price:
            peak_price = new_peak
            changed = True

        def _better(candidate):
            return candidate is not None and (sl is None or (candidate > sl if is_long else candidate < sl))

        if not profit_locked and order.get("lock_trigger_pct") and order.get("lock_pct") is not None:
            profit_pct = ((current_price - entry) / entry * 100) if is_long else ((entry - current_price) / entry * 100)
            if profit_pct >= order["lock_trigger_pct"]:
                lock_sl = entry * (1 + order["lock_pct"] / 100) if is_long else entry * (1 - order["lock_pct"] / 100)
                profit_locked = True
                changed = True
                if _better(lock_sl):
                    sl = round(lock_sl, 2)

        if order.get("trailing_enabled") and order.get("trail_pct"):
            trail_sl = peak_price * (1 - order["trail_pct"] / 100) if is_long else peak_price * (1 + order["trail_pct"] / 100)
            if _better(trail_sl):
                sl = round(trail_sl, 2)
                changed = True

    return {"sl": sl, "peak_price": peak_price, "profit_locked": profit_locked, "changed": changed}


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
    live price, auto-closing on a hit. Also checks open LIVE positions: if the user has opted
    into live_auto_exit_enabled (off by default -- see auto_trade_settings), a hit places a
    REAL offsetting order via _close_live_order(); otherwise (or if that real order fails) it
    falls back to the existing SL-proximity Telegram alert, same as before this existed.
    Meant to be called periodically by a background task."""
    closed = []
    for order in db.list_all_open_orders(mode="paper"):
        price_info = get_live_price(order["resolved_symbol"], order["instrument"], order["strike"], user_id=order["user_id"])
        if not price_info["available"]:
            continue
        price = price_info["price"]

        risk = apply_risk_management(order, price)
        if risk["changed"]:
            db.update_order_risk_state(order["id"], risk["sl"], risk["peak_price"], risk["profit_locked"])
        order = {**order, "sl": risk["sl"], "peak_price": risk["peak_price"], "profit_locked": risk["profit_locked"]}

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
        price_info = get_live_price(order["resolved_symbol"], order["instrument"], order["strike"], user_id=order["user_id"])
        if not price_info["available"]:
            continue
        price = price_info["price"]

        hit = _check_hit(order, price)
        if hit:
            settings = db.get_auto_trade_settings(order["user_id"])
            if settings.get("live_auto_exit_enabled"):
                try:
                    result = _close_live_order(order["user_id"], order, hit)
                except Exception as e:
                    result = {"closed": False, "reason": str(e)}

                if result.get("closed"):
                    if order.get("signal_id"):
                        db.update_outcome(order["user_id"], order["signal_id"], hit)
                        signal = db.get_signal(order["user_id"], order["signal_id"])
                        if signal:
                            graduation.check_and_graduate(order["user_id"], signal["channel"])
                    alerts.alert_live_auto_exit(order["user_id"], order, hit, result["exit_price"], result["pnl"])
                    closed.append(
                        {"order_id": order["id"], "reason": hit, "exit_price": result["exit_price"], "pnl": result["pnl"], "mode": "live"}
                    )
                    continue  # exited for real -- nothing left to alert on below
                alerts.alert_live_auto_exit_failed(order["user_id"], order, hit, result.get("reason", "unknown error"))

        if not order.get("sl_alert_sent"):
            if alerts.maybe_alert_sl_proximity(order["user_id"], order, price):
                db.mark_sl_alert_sent(order["id"])

    return closed
