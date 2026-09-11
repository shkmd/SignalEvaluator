"""Per-user background Telegram listeners: each user with an authorized Telegram session
gets their own watcher, tied to their own enabled channels and their own signal/order data.
One user's listener failing or being unconfigured never affects another user's."""
import asyncio

from telethon import events

from app import db, scoring, trading, screener
from app import parser as signal_parser
from app import technicals, options as options_mod, news as news_mod
from app.telegram_client import get_client

_handler_registered_users: set[int] = set()


async def start_listener(user_id: int) -> bool:
    client = get_client(user_id)
    if not client.is_connected():
        await client.connect()

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

    tech = technicals.fetch_technicals(resolved_symbol, direction=direction)
    opts = options_mod.fetch_option_chain_snapshot(resolved_symbol, parsed.get("strike"), instrument)
    headlines = news_mod.fetch_news(resolved_symbol)
    scr = screener.evaluate_screener(resolved_symbol, direction)

    evaluation = scoring.evaluate_signal(signal, tech, opts, headlines, scr)
    evaluation["technicals"] = tech
    evaluation["options"] = opts
    evaluation["news"] = headlines
    evaluation["screener"] = scr

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
