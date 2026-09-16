"""Sends personal Telegram alerts to the user's own Saved Messages ('me'), via their already-
connected Telethon session -- the same account used for reading channels and broadcasting, not
a bot, and with no target chat to configure since it's always the user's own account.

Two triggers: a new signal clearing a score threshold, and an open position's price moving
within a configurable percentage of its stop-loss. Purely a notification -- never places,
closes, or modifies any order.
"""
import asyncio

from app import db
from app.telegram_broadcast import format_signal_message
from app.telegram_client import get_client, get_active_loop


def _send(user_id: int, text: str, context: str) -> None:
    """Fire-and-forget: schedules the send on the Telethon client's own event loop and
    returns immediately. Safe to call from either an async or a background-thread context --
    mirrors telegram_broadcast.maybe_broadcast()."""
    try:
        client = get_client(user_id)
    except Exception as e:
        print(f"[alerts] user {user_id}: no Telegram client available: {e}")
        return

    loop = get_active_loop(user_id)
    if loop is None:
        print(f"[alerts] user {user_id}: Telegram client has no active loop -- not connected yet.")
        return

    async def _do_send():
        try:
            if not client.is_connected():
                await client.connect()
            await client.send_message("me", text)
            print(f"[alerts] user {user_id}: {context} sent.")
        except Exception as e:
            print(f"[alerts] user {user_id}: send failed for {context}: {e}")

    asyncio.run_coroutine_threadsafe(_do_send(), loop)


def maybe_alert_signal(user_id: int, signal: dict, evaluation: dict, signal_id: int) -> None:
    settings = db.get_alert_settings(user_id)
    if not settings.get("enabled"):
        return
    if (evaluation.get("score") or 0) < (settings.get("min_score") or 0):
        return
    text = "\U0001F514 New signal\n\n" + format_signal_message(signal, evaluation)
    _send(user_id, text, f"signal #{signal_id} alert")


def maybe_alert_sl_proximity(user_id: int, order: dict, current_price: float) -> bool:
    """Returns True if an alert was actually sent, so the caller can mark sl_alert_sent and
    avoid re-alerting on the next 60s poll while price keeps hovering near SL."""
    settings = db.get_alert_settings(user_id)
    if not settings.get("enabled"):
        return False
    sl = order.get("sl")
    if not sl:
        return False
    distance_pct = abs(current_price - sl) / sl * 100
    if distance_pct > settings.get("sl_proximity_pct", 2.0):
        return False

    symbol = order.get("resolved_symbol") or order.get("symbol")
    text = (
        f"⚠️ SL approaching\n\n"
        f"{symbol} ({order.get('mode')}) -- {(order.get('side') or '').upper()}\n"
        f"Current: {current_price}\n"
        f"SL: {sl}\n"
        f"Distance: {distance_pct:.1f}%"
    )
    _send(user_id, text, f"SL-proximity alert for order #{order.get('id')}")
    return True
