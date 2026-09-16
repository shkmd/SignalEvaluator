"""Per-user background Telegram listeners: each user with an authorized Telegram session
gets their own watcher, tied to their own enabled channels and their own signal/order data.
One user's listener failing or being unconfigured never affects another user's."""
import asyncio

from telethon import events

from app import db, scoring, trading, screener, stock_score, telegram_broadcast, alerts
from app import parser as signal_parser
from app import technicals, options as options_mod, news as news_mod
from app.telegram_client import get_client, register_active_loop

_handler_registered_users: set[int] = set()


async def start_listener(user_id: int) -> bool:
    client = get_client(user_id)
    if not client.is_connected():
        await client.connect()
    register_active_loop(user_id, asyncio.get_running_loop())

    if not await client.is_user_authorized():
        print(f"[telegram] user {user_id}: not logged in yet.")
        return False

    if user_id not in _handler_registered_users:
        # No incoming=True filter: channels you post/test in yourself would otherwise be
        # marked "outgoing" and silently skipped, which is exactly the case that trips
        # people up when testing. We want every new message in a watched channel, period.
        client.add_event_handler(
            lambda event, uid=user_id: _on_new_message(event, uid), events.NewMessage()
        )
        _handler_registered_users.add(user_id)

    print(f"[telegram] user {user_id}: listener active.")
    return True


async def stop_listener(user_id: int):
    client = get_client(user_id)
    if client.is_connected():
        await client.disconnect()
    _handler_registered_users.discard(user_id)


async def start_all_known_listeners():
    """Called once at app startup: reconnects every user who has Telegram credentials on
    file and is already authorized (has a saved session), so listeners survive a restart."""
    for user_id in db.list_users_with_telegram_credentials():
        try:
            await start_listener(user_id)
        except Exception as e:
            print(f"[telegram] user {user_id}: startup reconnect skipped ({e})")


async def get_status(user_id: int) -> dict:
    creds = db.get_telegram_credentials(user_id)
    if not creds or not creds.get("api_id") or not creds.get("api_hash"):
        return {"configured": False, "authorized": False, "listening": False}

    client = get_client(user_id)
    if not client.is_connected():
        try:
            await client.connect()
        except Exception:
            return {"configured": True, "authorized": False, "listening": False}

    authorized = await client.is_user_authorized()
    return {"configured": True, "authorized": authorized, "listening": authorized and user_id in _handler_registered_users}


async def fetch_dialogs(user_id: int) -> list:
    client = get_client(user_id)
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Not logged in -- log in from the Telegram tab first.")

    dialogs = await client.get_dialogs()
    result = []
    for d in dialogs:
        if d.is_channel or d.is_group:
            result.append(
                {
                    "telegram_chat_id": d.id,
                    "title": d.title,
                    "username": getattr(d.entity, "username", None),
                }
            )
    return result


def looks_like_signal(parsed: dict) -> bool:
    return bool(parsed.get("symbol")) and (parsed.get("sl") is not None or bool(parsed.get("targets")))


async def _on_new_message(event, user_id: int):
    try:
        chat_id = event.chat_id
        enabled_ids = await asyncio.to_thread(db.get_enabled_chat_ids, user_id)
        if chat_id not in enabled_ids:
            return

        text = event.raw_text or ""
        if not text.strip():
            return

        parsed = signal_parser.parse_signal(text)
        if not looks_like_signal(parsed):
            return

        await asyncio.to_thread(_evaluate_and_store, user_id, parsed, chat_id, event.id)
    except Exception as e:
        print(f"[telegram] user {user_id}: error handling message: {e}")


def _evaluate_and_store(user_id: int, parsed: dict, chat_id: int, message_id: int):
    symbol = parsed["symbol"]
    resolved_symbol = (parsed.get("resolved_symbol") or symbol).upper()
    instrument = (parsed.get("instrument") or "EQ").upper()
    direction = "bearish" if (instrument == "PE" or parsed.get("action") == "sell") else "bullish"

    signal = {
        "raw_text": parsed.get("raw_text"),
        "signal_type": parsed.get("signal_type"),
        "action": parsed.get("action"),
        "symbol": symbol,
        "resolved_symbol": resolved_symbol,
        "instrument": instrument,
        "strike": parsed.get("strike"),
        "entry_low": parsed.get("entry_low"),
        "entry_high": parsed.get("entry_high"),
        "sl": parsed.get("sl"),
        "targets": parsed.get("targets") or [],
    }

    tech = technicals.fetch_technicals(resolved_symbol, direction=direction, user_id=user_id)
    opts = options_mod.fetch_option_chain_snapshot(resolved_symbol, parsed.get("strike"), instrument)
    headlines = news_mod.fetch_news(resolved_symbol)
    scr = screener.evaluate_screener(resolved_symbol, direction)
    stock_ctx = stock_score.evaluate_stock_context(resolved_symbol)

    evaluation = scoring.evaluate_signal(signal, tech, opts, headlines, scr, stock_ctx)
    evaluation["technicals"] = tech
    evaluation["options"] = opts
    evaluation["news"] = headlines
    evaluation["screener"] = scr
    evaluation["stock_context"] = stock_ctx

    channel_title = db.get_channel_title(user_id, chat_id) or str(chat_id)
    signal_id = db.insert_signal(
        user_id,
        signal,
        evaluation,
        channel_title,
        source="telegram",
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
    )
    if signal_id:
        print(
            f"[telegram] user {user_id}: auto-evaluated signal #{signal_id} from {channel_title}: "
            f"{symbol} -> score {evaluation['score']} ({evaluation['verdict']})"
        )
        trade_result = trading.auto_trade_check(user_id, signal_id, signal, evaluation)
        if trade_result and trade_result.get("placed"):
            print(f"[trading] user {user_id}: paper order #{trade_result['order_id']} placed for signal #{signal_id}")
        _maybe_paper_trade(user_id, signal_id, signal, evaluation, channel_title)
        telegram_broadcast.maybe_broadcast(user_id, signal, evaluation, signal_id)
        alerts.maybe_alert_signal(user_id, signal, evaluation, signal_id)


def _maybe_paper_trade(user_id: int, signal_id: int, signal: dict, evaluation: dict, channel: str) -> None:
    """Dedicated auto-paper-trade for every signal auto-evaluated from this user's monitored
    Telegram channels -- separate from both the general Broker-Setup auto-trade (auto_trade_check,
    above) and the F&O-Scanner-specific one (scanner_signals._maybe_paper_trade). Starts on paper,
    scoped to this channel only, so Channel Stats' hit-rate becomes a real, hands-off reliability
    measurement per channel -- and, if the user has turned on auto-graduate (Broker Setup), a
    channel that proves itself here can start placing REAL orders automatically; see
    trading.place_order_for_channel() and app/graduation.py."""
    settings = db.get_telegram_paper_trade_settings(user_id)
    if not settings.get("enabled"):
        return
    if (evaluation.get("score") or 0) < settings.get("min_score", 70):
        return
    if not signal.get("entry_low") and not signal.get("entry_high"):
        return
    if not signal.get("sl"):
        return
    try:
        trading.place_order_for_channel(user_id, signal_id, signal, evaluation, channel, 1)
    except Exception as e:
        print(f"[telegram] Paper trade failed for signal {signal_id}: {e}")
