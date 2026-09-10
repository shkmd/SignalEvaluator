"""In-app Telegram login: request a code, verify it, verify a 2FA password if needed.

This talks directly to Telegram's own servers through the same MTProto client used
elsewhere in the app (Telethon) -- your phone number, code, and password never pass
through anyone but you and Telegram. The server only runs on localhost, so this form
is only reachable from your own machine, same trust boundary as the old terminal script.
"""
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    FloodWaitError,
)
from app.telegram_client import get_client

# Single-user local app -- module-level state for the in-progress login is fine.
_pending = {"phone": None, "phone_code_hash": None}


async def send_code(phone: str) -> dict:
    client = get_client()
    if not client.is_connected():
        await client.connect()
    if await client.is_user_authorized():
        return {"already_authorized": True}

    try:
        sent = await client.send_code_request(phone)
    except FloodWaitError as e:
        raise RuntimeError(f"Telegram is rate-limiting login attempts -- wait {e.seconds}s and try again.")

    _pending["phone"] = phone
    _pending["phone_code_hash"] = sent.phone_code_hash
    return {"code_sent": True}


async def verify_code(code: str) -> dict:
    if not _pending["phone"]:
        raise RuntimeError("No login in progress -- request a code first.")

    client = get_client()
    try:
        await client.sign_in(
            phone=_pending["phone"], code=code, phone_code_hash=_pending["phone_code_hash"]
        )
    except SessionPasswordNeededError:
        return {"needs_password": True}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        raise RuntimeError("That code is invalid or expired -- request a new one.")

    _pending["phone"] = None
    _pending["phone_code_hash"] = None
    return {"logged_in": True}


async def verify_password(password: str) -> dict:
    client = get_client()
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        raise RuntimeError("Incorrect 2FA password.")

    _pending["phone"] = None
    _pending["phone_code_hash"] = None
    return {"logged_in": True}
