"""Broker adapter registry. Each adapter module (kite, upstox, dhan) exposes the same
interface: has_valid_session(user_id), fetch_daily_candles(user_id, symbol, exchange, days),
fetch_intraday_candles(...), fetch_ltp(...), find_atm_option(...), place_order(...).

get_connected_adapter() picks the first connected broker for a user in a fixed priority
order, so technicals/options/trading code can stay broker-agnostic instead of special-casing
Kite. Priority is Kite > Upstox > Dhan -- arbitrary but stable, since a user with more than
one connected broker still only wants one source of truth per evaluation.
"""

BROKER_PRIORITY = ("kite", "upstox", "dhan")


def get_connected_adapter(user_id: int):
    from app.brokers import kite, upstox, dhan

    modules = {"kite": kite, "upstox": upstox, "dhan": dhan}
    for name in BROKER_PRIORITY:
        mod = modules[name]
        try:
            if mod.has_valid_session(user_id):
                return mod
        except Exception:
            continue
    return None
