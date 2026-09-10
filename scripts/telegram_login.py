"""
Run this ONCE yourself, in your own terminal, to log the app into your Telegram account:

    .venv\\Scripts\\python scripts\\telegram_login.py      (Windows)
    .venv/bin/python scripts/telegram_login.py             (macOS/Linux)

It will prompt you directly (in this terminal) for your phone number, then the login
code Telegram sends you, and your 2FA password if you have one set. Nobody and nothing
else sees these -- Telethon reads them straight from your terminal input.

Once you see "Logged in as ...", a local session file is saved under data/ and the web
app can use it to read your channels without asking you to log in again.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.telegram_client import get_client  # noqa: E402


def main():
    client = get_client()
    with client:
        me = client.get_me()
        print(f"Logged in as {me.first_name} (@{me.username or me.id})")
        print("Session saved. You can now close this and start the web app normally.")


if __name__ == "__main__":
    main()
