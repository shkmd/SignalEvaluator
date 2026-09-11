"""Formats a signal into a compact channel-style message and posts it to a Telegram chat
the user has write access to, using their own already-connected Telethon session -- the
same account/session used for reading channels, not a bot.

Fires automatically after every signal (manual, Telegram-sourced, or F&O Scanner) that
clears the user's configured min_score, immediately, with no per-message review step --
that's a deliberate choice the user made; the min_score gate exists specifically to keep
weak signals from spamming the target channel.
"""
import asyncio
from datetime import datetime

from app import db
from app.telegram_client import get_client

MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _fmt_num(n):
    if n is None:
        return "-"
    return str(int(n)) if float(n).is_integer() else str(n)


def format_signal_message(signal: dict, evaluation: dict) -> str:
    symbol = signal.get("resolved_symbol") or signal.get("symbol")
    instrument = (signal.get("instrument") or "EQ").upper()
    entry_low = signal.get("entry_low")
    entry_high = signal.get("entry_high")
    sl = signal.get("sl")
    targets = signal.get("targets") or []
    signal_type = (signal.get("signal_type") or "positional").capitalize()

    lines = []
    if instrument in ("CE", "PE") and signal.get("strike"):
        expiry_label = ""
        expiry_iso = (evaluation.get("options") or {}).get("expiry")
        if expiry_iso:
            try:
                dt = datetime.fromisoformat(str(expiry_iso)[:10])
                expiry_label = f" {MONTH_ABBR[dt.month - 1]}"
            except Exception:
                pass
        lines.append(f"{symbol} {_fmt_num(signal['strike'])}{instrument}{expiry_label} Series")
    else:
        lines.append(f"{symbol} Equity")

    lines.append("")
    if entry_low == entry_high:
        lines.append(f"Buy Range - {_fmt_num(entry_high)}")
    else:
        lines.append(f"Buy Range - {_fmt_num(entry_high)}/{_fmt_num(entry_low)}")
    lines.append("")
    lines.append(f"SL {_fmt_num(sl)}")
    lines.append("")
    lines.append(f"Targets - {'/'.join(_fmt_num(t) for t in targets)}")
    lines.append("")
    lines.append(signal_type)
    lines.append("")
    lines.append(f"Score: {evaluation.get('score')} · {evaluation.get('verdict', '')}")

    return "\n".join(lines)


def maybe_broadcast(user_id: int, signal: dict, evaluation: dict, signal_id: int) -> None:
    """Fire-and-forget: schedules the send on the Telethon client's own event loop and
    returns immediately. Safe to call from either an async or a background-thread context."""
    settings = db.get_telegram_broadcast_settings(user_id)
    if not settings.get("enabled") or not settings.get("target_chat_id"):
        return
    if (evaluation.get("score") or 0) < (settings.get("min_score") or 0):
        return

    text = format_signal_message(signal, evaluation)

    try:
        client = get_client(user_id)
    except Exception as e:
        print(f"[broadcast] user {user_id}: no Telegram client available: {e}")
        return

    if client.loop is None:
        print(f"[broadcast] user {user_id}: Telegram client has no event loop -- not connected yet.")
        return

    async def _send():
        try:
            if not client.is_connected():
                await client.connect()
            await client.send_message(settings["target_chat_id"], text)
            print(f"[broadcast] user {user_id}: signal #{signal_id} sent to {settings.get('target_chat_title')}")
        except Exception as e:
            print(f"[broadcast] user {user_id}: send failed for signal #{signal_id}: {e}")

    asyncio.run_coroutine_threadsafe(_send(), client.loop)
