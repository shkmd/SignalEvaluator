"""Per-user Telethon client registry. Each user has their own API ID/Hash and their own
session file, so each user's Telegram connection is fully independent of every other user's."""
import asyncio
from pathlib import Path
from telethon import TelegramClient

from app import db

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_clients: dict[int, TelegramClient] = {}
# Telethon's own `.loop` property always calls asyncio.get_running_loop() fresh (it does not
# remember the loop a client was built/connected on -- see telethon's telegrambaseclient.py),
# so reading it from any thread other than the one currently running that loop raises
# "no running event loop". We track the real loop ourselves, recorded once a client actually
# connects (see register_active_loop / telegram_ingest.start_listener). Anything that needs
# to schedule work onto a user's client from a background thread -- e.g. telegram_broadcast,
# called from the scanner's worker threads -- must use get_active_loop(), never client.loop.
_active_loops: dict[int, asyncio.AbstractEventLoop] = {}


def get_client(user_id: int) -> TelegramClient:
    if user_id not in _clients:
        creds = db.get_telegram_credentials(user_id)
        if not creds or not creds.get("api_id") or not creds.get("api_hash"):
            raise RuntimeError(
                "Telegram API credentials not set for this account -- add your API ID/Hash "
                "in the Telegram tab first."
            )
        session_name = creds.get("session_name") or f"user_{user_id}"
        session_path = str(DATA_DIR / session_name)
        # Telethon's constructor calls asyncio.get_event_loop() internally if no loop= is
        # given, which raises on any thread that never had one set (e.g. a scanner worker
        # thread, or FastAPI's own request threadpool for a sync endpoint) -- so make sure
        # one exists on whichever thread happens to build this client first. This is only
        # enough for construction to succeed; see _active_loops above for the loop actually
        # used once the client connects.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        _clients[user_id] = TelegramClient(session_path, int(creds["api_id"]), creds["api_hash"], loop=loop)
    return _clients[user_id]


def register_active_loop(user_id: int, loop: asyncio.AbstractEventLoop) -> None:
    """Called from the coroutine that actually connected this user's client (so `loop` is
    genuinely the one it's running on) -- see telegram_ingest.start_listener."""
    _active_loops[user_id] = loop


def get_active_loop(user_id: int) -> asyncio.AbstractEventLoop:
    return _active_loops.get(user_id)


def reset_client(user_id: int):
    """Drop the cached client so the next get_client() rebuilds it with fresh credentials."""
    _clients.pop(user_id, None)
    _active_loops.pop(user_id, None)


def all_client_user_ids() -> list:
    return list(_clients.keys())
