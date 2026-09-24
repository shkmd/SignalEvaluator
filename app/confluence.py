"""Confluence auto-trade: only takes a position when the F&O Scanner and a Chartink scan have
independently flagged the same symbol in the same direction within a short window of each
other. Two unrelated signal sources agreeing is a stronger signal than either alone.

A match creates its own dedicated signal record (channel="Confluence (F&O + Chartink)",
source="confluence") copied from the Scanner's signal -- never Chartink's, since the Scanner
signal already carries a real ATM option contract (or a properly-sized equity fallback), while
Chartink's signal here is equity-only and exists purely as the confirming filter. A dedicated
record (rather than reusing the Scanner signal's own id) keeps this strategy's own hit rate
visible on Channel Stats, and keeps it from colliding with the Scanner's own dedicated
paper-trade, which may independently claim that same Scanner signal_id.

Dedup is keyed on the Scanner signal alone (external_ref="scanner:<id>"): Chartink can keep
re-firing the same scan every few minutes for as long as the window stays open, but a given
Scanner signal should produce at most one confluence trade no matter how many times it
re-matches.
"""
from datetime import datetime, timedelta, timezone

from app import db, trading

CHANNEL_LABEL = "Confluence (F&O + Chartink)"


def check_and_trade(user_id: int, signal_id: int) -> None:
    settings = db.get_confluence_settings(user_id)
    if not settings.get("enabled"):
        return

    signal_row = db.get_signal(user_id, signal_id)
    if not signal_row or signal_row["source"] not in ("scanner", "chartink"):
        return

    other_source = "chartink" if signal_row["source"] == "scanner" else "scanner"
    window_start = (datetime.now(timezone.utc) - timedelta(minutes=settings["window_minutes"])).isoformat()
    match = db.find_recent_signal(
        user_id, signal_row["resolved_symbol"], signal_row["direction"], other_source, window_start
    )
    if not match:
        return

    scanner_row = signal_row if signal_row["source"] == "scanner" else match
    try:
        _record_and_trade(user_id, scanner_row, settings["quantity"])
    except Exception as e:
        print(f"[confluence] user {user_id}: failed on {scanner_row.get('resolved_symbol')}: {e}")


def _record_and_trade(user_id: int, scanner_row: dict, quantity: float) -> None:
    instrument = scanner_row["instrument"]
    # Same action-derivation as scanner_signals._build_atm_signal / _build_equity_signal at the
    # point this signal was originally created -- CE/PE scanner signals are always *buying* the
    # option contract regardless of direction (see the order_side fix in trading.py for why
    # this can't be inferred from evaluation["direction"] alone: PE is always "bearish" there).
    action = "buy" if instrument in ("CE", "PE") else ("buy" if scanner_row["direction"] == "bullish" else "sell")
    signal = {
        "raw_text": None,
        "signal_type": scanner_row.get("signal_type"),
        "action": action,
        "symbol": scanner_row["symbol"],
        "resolved_symbol": scanner_row["resolved_symbol"],
        "instrument": instrument,
        "strike": scanner_row["strike"],
        "entry_low": scanner_row["entry_low"],
        "entry_high": scanner_row["entry_high"],
        "sl": scanner_row["sl"],
        "targets": scanner_row["targets"],
        "lot_size": scanner_row["lot_size"],
    }
    evaluation = scanner_row["evaluation"]

    signal_id = db.insert_signal(
        user_id, signal, evaluation, CHANNEL_LABEL, source="confluence", external_ref=f"scanner:{scanner_row['id']}"
    )
    if not signal_id:
        return  # this Scanner signal already produced a confluence trade

    trading.place_order_for_channel(user_id, signal_id, signal, evaluation, CHANNEL_LABEL, quantity)
