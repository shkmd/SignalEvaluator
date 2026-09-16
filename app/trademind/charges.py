"""Deterministic, versioned charge calculation -- Zerodha equity rates only for this first
slice (delivery and intraday), since that's the only broker CSV format being imported yet.
Versioned by CHARGE_RULES_VERSION so a rate change later doesn't silently reprice historical
trades that were already charged and shown to the user under the old rule.

Rates are the commonly published Zerodha equity structure as of early 2026 and are a
simplification (e.g. they don't model per-order brokerage caps across multiple orders within
one trade, or DP charges on delivery sells) -- good enough for a first estimate, not a
substitute for the broker's own contract note.
"""

CHARGE_RULES_VERSION = "zerodha_equity_2026_01"

_BROKERAGE_RATE = 0.0003  # 0.03%
_BROKERAGE_CAP = 20.0
_EXCHANGE_TXN_RATE = 0.0000297  # NSE
_GST_RATE = 0.18
_SEBI_RATE = 0.000001
_STAMP_DUTY_BUY_RATE_INTRADAY = 0.00003
_STAMP_DUTY_BUY_RATE_DELIVERY = 0.00015
_STT_RATE_DELIVERY = 0.001  # both legs
_STT_RATE_INTRADAY_SELL_ONLY = 0.00025


def calculate_charges(segment: str, product: str, buy_value: float, sell_value: float) -> dict:
    """segment: 'EQ' (only supported segment in this slice). product: 'intraday' or
    'delivery'. buy_value/sell_value: total executed turnover on each side (quantity * price,
    summed across all entry/exit executions in the trade). Returns a breakdown, not just a
    total, so the UI can show where the money actually went (spec section 24)."""
    is_delivery = product == "delivery"

    brokerage = 0.0 if is_delivery else min(_BROKERAGE_RATE * (buy_value + sell_value), _BROKERAGE_CAP * 2)
    stt = (
        (buy_value + sell_value) * _STT_RATE_DELIVERY
        if is_delivery
        else sell_value * _STT_RATE_INTRADAY_SELL_ONLY
    )
    exchange_txn_charge = (buy_value + sell_value) * _EXCHANGE_TXN_RATE
    sebi_charges = (buy_value + sell_value) * _SEBI_RATE
    stamp_duty = buy_value * (_STAMP_DUTY_BUY_RATE_DELIVERY if is_delivery else _STAMP_DUTY_BUY_RATE_INTRADAY)
    gst = _GST_RATE * (brokerage + exchange_txn_charge)

    total = brokerage + stt + exchange_txn_charge + sebi_charges + stamp_duty + gst
    return {
        "version": CHARGE_RULES_VERSION,
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn_charge": round(exchange_txn_charge, 2),
        "sebi_charges": round(sebi_charges, 2),
        "stamp_duty": round(stamp_duty, 2),
        "gst": round(gst, 2),
        "total": round(total, 2),
    }
