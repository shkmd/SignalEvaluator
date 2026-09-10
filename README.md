# Signal Evaluator

A multi-user app: each person creates their own account, connects their own Telegram (their
own API credentials, their own phone login, their own channels), and gets their own signal
history, paper-trading, and broker setup -- fully isolated from every other user.

Paste a raw call from a trading channel (like a "BUY KALYAN 630 CE" positional signal),
confirm the parsed fields, and get an evaluation combining:

- **Technicals** (via Yahoo Finance / `yfinance`): trend alignment (EMA20/50/200), breakout
  vs. the 20-day range with volume confirmation, RSI, ATR, relative strength vs Nifty.
- **Options OI/IV** (best-effort via NSE's unofficial option-chain endpoint): fresh OI
  buildup vs. unwinding/short-covering at the given strike.
- **News sentiment**: recent headlines via Google News RSS, scored with a keyword lexicon.
- **Risk/Reward**: computed from entry, stop-loss, and targets.

Everything is combined into a 0-100 score, a verdict, and a list of red flags. Every
evaluated signal is logged to a per-user SQLite table so you can mark the outcome
(target hit / SL hit / partial) later and build a **per-channel track record** — i.e.
whether a given channel's calls are actually worth following.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

Then open http://localhost:8000 and sign up with an email + password.

**Note on email verification**: accounts work immediately after signup -- no email provider
is wired up yet to actually send a verification link, so the `email_verified` flag exists in
the schema but nothing currently blocks on it. Add a transactional email step (Resend,
SendGrid, etc.) before relying on verification for anything security-sensitive.

## Auto-ingesting signals from your Telegram channels

Every user connects their own Telegram account -- entirely inside the app, no separate scripts.

1. **Get API credentials** (one-time per user): go to https://my.telegram.org -> API
   Development Tools -> create an app -> copy the `api_id` and `api_hash`.
2. Open the **Telegram** tab, paste those two values into the **API credentials** panel, and
   click **Save credentials**. They're stored in your own row in the database, never shared
   with other accounts on this app.
3. A **Log in to Telegram** panel appears: enter your phone number (with country code) and
   click **Send login code**, enter the code Telegram sends to your app/SMS and click
   **Verify code**, and if you have 2FA enabled, enter that password too. This talks
   directly to Telegram's servers over the same connection Telegram's own apps use.
4. Once logged in, the status badge flips to "listening" and your session persists under
   `data/` (keyed to your account) so you won't need to log in again.
5. Click **Sync my channels from Telegram** -- it lists every channel/group you're a member
   of. Tick the ones you want monitored.
6. From then on, any new message in a ticked channel is parsed automatically for you. If it
   looks like a real call (has a symbol plus a stop-loss or target), it's evaluated the same
   way as the manual "Evaluate" tab and logged to your **History** with a 📡 marker, tagged
   with the channel's name -- so it also feeds into that channel's track record on your
   **Channel Stats**. Messages that don't look like a signal (chit-chat, images, etc.) are
   ignored. Other users' channels, signals, and settings are never visible to you, and yours
   are never visible to them.

## Notes / limitations

- **NSE option-chain data is best-effort.** NSE's endpoint is unofficial, rate-limited,
  and often blocks non-browser traffic. If it fails, the app just shows "not available" —
  it won't crash. For reliable OI/IV, wire up a broker API (Kite Connect, Dhan, Upstox,
  Angel One, etc.) in `app/options.py`.
- **Symbol resolution**: channels often use nicknames (e.g. "KALYAN" for Kalyan Jewellers,
  NSE symbol `KALYANKJIL`). A small lookup table lives in `app/parser.py`
  (`SYMBOL_MAP`) — add to it as you hit more channel abbreviations. The evaluate form also
  lets you manually correct the symbol before fetching data.
- **News sentiment is a keyword-based heuristic**, not NLP — treat it as a rough directional
  read, not a substitute for reading the actual articles.
- **Paper trading is fully simulated** (no broker touched); **live trading** requires
  connecting a broker in Broker Setup, but no broker's order-placement adapter is wired up
  yet -- live orders are refused with a clear reason rather than silently no-op'd.
- This tool does not place real trades or give financial advice — it is a signal-quality
  filter to help you decide whether a call is worth investigating further.
