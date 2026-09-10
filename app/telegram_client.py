"""Shared Telethon client singleton, reused by the login script and the FastAPI app."""
from telethon import TelegramClient
from app import config

DATA_DIR = config.DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

_client = None


def get_client() -> TelegramClient:
    global _client
    if _client is None:
        if not config.telegram_configured():
            raise RuntimeError(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH are not set. Copy .env.example to .env "
                "and fill them in from https://my.telegram.org first."
            )
        _client = TelegramClient(
            config.TELEGRAM_SESSION_PATH,
            int(config.TELEGRAM_API_ID),
            config.TELEGRAM_API_HASH,
        )
    return _client


def reset_client():
    """Drop the cached client so the next get_client() rebuilds it with fresh
    credentials -- used after saving new API ID/Hash from the UI."""
    global _client
    _client = None
