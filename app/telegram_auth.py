"""In-app Telegram login: request a code, verify it, verify a 2FA password if needed.
Scoped per-user -- each user logs into their own Telegram account against their own
API credentials, independent of every other user's session.
"""
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    FloodWaitError,
)
from app.telegram_client import get_client

# user_id -> {"phone": ..., "phone_code_hash": ...} for the in-progress login, if any.
_pending: dict[int, dict] = {}


async def send_code(user_id: int, phone: str) -> dict:
    client = get_client(user_id)
    if not client.is_connected():
        await client.connect()
    if await client.is_user_authorized():
        return {"already_authorized": True}

    try:
        sent = await client.send_code_request(phone)
    except FloodWaitError as e:
        raise RuntimeError(f"Telegram is rate-limiting login attempts -- wait {e.seconds}s and try again.")

    _pending[user_id] = {"phone": phone, "phone_code_hash": sent.phone_code_hash}
    return {"code_sent": True}


async def verify_code(user_id: int, code: str) -> dict:
    pending = _pending.get(user_id)
    if not pending:
        raise RuntimeError("No login in progress -- request a code first.")

    client = get_client(user_id)
    try:
        await client.sign_in(phone=pending["phone"], code=code, phone_code_hash=pending["phone_code_hash"])
    except SessionPasswordNeededError:
        return {"needs_password": True}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        raise RuntimeError("That code is invalid or expired -- request a new one.")

    _pending.pop(user_id, None)
    return {"logged_in": True}


async def verify_password(user_id: int, password: str) -> dict:
    client = get_client(user_id)
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        raise RuntimeError("Incorrect 2FA password.")

    _pending.pop(user_id, None)
    return {"logged_in": True}
