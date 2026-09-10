import os
from pathlib import Path
from dotenv import load_dotenv, set_key

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"

load_dotenv(ENV_PATH)

TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "signal_evaluator")
TELEGRAM_SESSION_PATH = str(DATA_DIR / TELEGRAM_SESSION_NAME)


def telegram_configured() -> bool:
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)


def reload():
    """Re-read .env into the module globals -- used after saving new credentials
    from the UI so the running process picks them up without a restart."""
    global TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_NAME, TELEGRAM_SESSION_PATH
    load_dotenv(ENV_PATH, override=True)
    TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
    TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "signal_evaluator")
    TELEGRAM_SESSION_PATH = str(DATA_DIR / TELEGRAM_SESSION_NAME)


def save_telegram_credentials(api_id: str, api_hash: str):
    if not ENV_PATH.exists():
        ENV_PATH.write_text("", encoding="utf-8")
    set_key(str(ENV_PATH), "TELEGRAM_API_ID", api_id)
    set_key(str(ENV_PATH), "TELEGRAM_API_HASH", api_hash)
    if not os.getenv("TELEGRAM_SESSION_NAME"):
        set_key(str(ENV_PATH), "TELEGRAM_SESSION_NAME", TELEGRAM_SESSION_NAME)
    reload()
