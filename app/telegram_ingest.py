"""Background Telegram listener: watches your selected channels, parses each message as a
potential signal, and -- if it looks like a real call (has a symbol plus an SL or targets) --
runs it through the same evaluation pipeline as the manual "Evaluate" tab and logs it to
history automatically."""
import asyncio

from telethon import events

from app import db, scoring, trading
from app import parser as signal_parser
from app import technicals, options as options_mod, news as news_mod
from app.telegram_client import get_client

_handler_registered = False


async def start_listener():
    client = get_client()
    if not client.is_connected():
        await client.connect()

    if not await client.is_user_authorized():
        print("[telegram] Not logged in yet -- run `scripts/telegram_login.py` once, then restart the app.")
        return False

    global _handler_registered
    if not _handler_registered:
        client.add_event_handler(_on_new_message, events.NewMessage(incoming=True))
        _handler_registered = True

    print("[telegram] Listener active -- watching enabled channels for signals.")
    return True


async def stop_listener():
    client = get_client()
    if client.is_connected():
        await client.disconnect()


async def get_status() -> dict:
    from app import config

    if not config.telegram_configured():
        return {"configured": False, "authorized": False, "listening": False}

    client = get_client()
    if not client.is_connected():
        try:
            await client.connect()
        except Exception:
            return {"configured": True, "authorized": False, "listening": False}

    authorized = await client.is_user_authorized()
    return {"configured": True, "authorized": authorized, "listening": authorized and _handler_registered}


async def fetch_dialogs() -> list:
    client = get_client()
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Not logged in -- run scripts/telegram_login.py first.")

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


async def _on_new_message(event):
    try:
        chat_id = event.chat_id
        enabled_ids = await asyncio.to_thread(db.get_enabled_chat_ids)
        if chat_id not in enabled_ids:
            return

        text = event.raw_text or ""
        if not text.strip():
            return

        parsed = signal_parser.parse_signal(text)
        if not looks_like_signal(parsed):
            return

        await asyncio.to_thread(_evaluate_and_store, parsed, chat_id, event.id)
    except Exception as e:
        print(f"[telegram] Error handling message: {e}")


def _evaluate_and_store(parsed: dict, chat_id: int, message_id: int):
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

    evaluation = scoring.evaluate_signal(signal, tech, opts, headlines)
    evaluation["technicals"] = tech
    evaluation["options"] = opts
    evaluation["news"] = headlines

    channel_title = db.get_channel_title(chat_id) or str(chat_id)
    signal_id = db.insert_signal(
        signal,
        evaluation,
        channel_title,
        source="telegram",
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
    )
    if signal_id:
        print(
            f"[telegram] Auto-evaluated signal #{signal_id} from {channel_title}: "
            f"{symbol} -> score {evaluation['score']} ({evaluation['verdict']})"
        )
        trade_result = trading.auto_trade_check(signal_id, signal, evaluation)
        if trade_result and trade_result.get("placed"):
            print(f"[trading] Paper order #{trade_result['order_id']} placed for signal #{signal_id}")
