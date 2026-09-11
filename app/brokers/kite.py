"""Zerodha Kite Connect adapter -- per-user broker connection for market data and (once you
choose to use it) live order placement.

Auth flow (mirrors the in-app Telegram login pattern already in this app):
1. User saves their API key + secret (from developers.kite.trade) via /api/broker/connect
   with broker="zerodha" -- this reuses the existing generic broker-credentials endpoint.
2. GET /api/broker/kite/login-url returns Zerodha's own login page URL. The user opens it in
   their browser and logs in directly on Zerodha's site -- we never see their Zerodha password.
3. Zerodha redirects to this app's registered redirect URL with a `request_token`. The user
   must set that redirect URL in their Kite Connect app's console to point at
   <this app's origin>/api/broker/kite/callback.
4. The callback exchanges request_token (+ api_secret) for an access_token, valid only for
   the current trading day -- Zerodha's own platform limitation, not something this app can
   avoid. Status reporting makes it obvious when a fresh login is needed.

IMPORTANT: this module builds real order-placement code (place_order below) but nothing in
this codebase calls it automatically, and no automated test in this project ever invokes it --
Kite Connect has no sandbox, so the first live call against a real account has to be a
deliberate action taken by the account owner, not something run during development.
"""
from datetime import datetime, date
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteConnect

from app import db

IST = ZoneInfo("Asia/Kolkata")

# In-memory instrument cache: {(user_id, exchange): (fetched_date, DataFrame)}. Kite's
# instrument dump is tens of thousands of rows -- fetch once per day per exchange, not
# per request.
_instrument_cache: dict = {}


def _today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def save_credentials(user_id: int, api_key: str, api_hash_secret: str) -> None:
    existing = db.get_broker_account(user_id, "zerodha") or {}
    creds = existing.get("credentials", {})
    creds["api_key"] = api_key
    creds["api_secret"] = api_hash_secret
    db.upsert_broker_account(user_id, "zerodha", creds, connected=bool(creds.get("access_token")))


def get_login_url(user_id: int) -> str:
    account = db.get_broker_account(user_id, "zerodha")
    if not account or not account["credentials"].get("api_key"):
        raise RuntimeError("Save your Kite API key/secret first.")
    kite = KiteConnect(api_key=account["credentials"]["api_key"])
    return kite.login_url()


def handle_callback(user_id: int, request_token: str) -> dict:
    account = db.get_broker_account(user_id, "zerodha")
    if not account or not account["credentials"].get("api_key"):
        raise RuntimeError("No Kite API key/secret on file for this account.")

    creds = account["credentials"]
    kite = KiteConnect(api_key=creds["api_key"])
    session = kite.generate_session(request_token, api_secret=creds["api_secret"])

    creds["access_token"] = session["access_token"]
    creds["access_token_date"] = _today_ist()
    creds["kite_user_id"] = session.get("user_id")
    db.upsert_broker_account(user_id, "zerodha", creds, connected=True)
    return {"kite_user_id": session.get("user_id")}


def get_status(user_id: int) -> dict:
    account = db.get_broker_account(user_id, "zerodha")
    if not account or not account["credentials"].get("api_key"):
        return {"configured": False, "logged_in_today": False}

    creds = account["credentials"]
    logged_in_today = bool(creds.get("access_token")) and creds.get("access_token_date") == _today_ist()
    return {
        "configured": True,
        "logged_in_today": logged_in_today,
        "kite_user_id": creds.get("kite_user_id"),
    }


def get_client(user_id: int) -> KiteConnect:
    account = db.get_broker_account(user_id, "zerodha")
    if not account or not account["credentials"].get("api_key"):
        raise RuntimeError("Kite Connect is not configured for this account.")

    creds = account["credentials"]
    if not creds.get("access_token") or creds.get("access_token_date") != _today_ist():
        raise RuntimeError("Kite session has expired for today -- log in again from Broker Setup.")

    kite = KiteConnect(api_key=creds["api_key"])
    kite.set_access_token(creds["access_token"])
    return kite


def has_valid_session(user_id: int) -> bool:
    try:
        get_client(user_id)
        return True
    except RuntimeError:
        return False


def _load_instruments(user_id: int, exchange: str) -> pd.DataFrame:
    cache_key = (user_id, exchange)
    cached = _instrument_cache.get(cache_key)
    if cached and cached[0] == _today_ist():
        return cached[1]

    kite = get_client(user_id)
    rows = kite.instruments(exchange)
    df = pd.DataFrame(rows)
    _instrument_cache[cache_key] = (_today_ist(), df)
    return df


def resolve_instrument_token(user_id: int, tradingsymbol: str, exchange: str = "NSE") -> int:
    df = _load_instruments(user_id, exchange)
    match = df[df["tradingsymbol"] == tradingsymbol.upper()]
    if match.empty:
        raise RuntimeError(f"{tradingsymbol} not found in Kite's {exchange} instrument list.")
    return int(match.iloc[0]["instrument_token"])


def fetch_fo_underlyings(user_id: int) -> list:
    """F&O-eligible stocks with real lot sizes and expiries, from Kite's own NFO instrument
    dump -- replaces the symbol-name-only list from the free NSE endpoint."""
    df = _load_instruments(user_id, "NFO")
    stock_futures = df[df["segment"] == "NFO-FUT"]
    if stock_futures.empty:
        return []

    grouped = stock_futures.groupby("name").agg(lot_size=("lot_size", "max"))
    expiries_by_name = stock_futures.groupby("name")["expiry"].apply(
        lambda s: sorted({e.isoformat() if hasattr(e, "isoformat") else str(e) for e in s})
    )

    result = []
    for name, row in grouped.iterrows():
        result.append(
            {
                "symbol": name,
                "resolved_symbol": name,
                "company_name": name,
                "lot_size": int(row["lot_size"]),
                "expiries": expiries_by_name.get(name, []),
                "futures_eligible": True,
                "is_index": False,
            }
        )
    return result


def fetch_daily_candles(user_id: int, tradingsymbol: str, exchange: str = "NSE", days: int = 400) -> pd.DataFrame:
    """Returns a DataFrame shaped exactly like yfinance's .history() output (Open/High/Low/
    Close/Volume columns, DatetimeIndex) so it's a drop-in replacement everywhere technicals.py
    and qualification.py consume daily candles."""
    kite = get_client(user_id)
    token = resolve_instrument_token(user_id, tradingsymbol, exchange)
    to_date = datetime.now(IST).date()
    from_date = to_date - pd.Timedelta(days=days)
    rows = kite.historical_data(token, from_date, to_date, "day")
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.rename(columns={"date": "Date", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_intraday_candles(
    user_id: int, tradingsymbol: str, exchange: str = "NSE", interval: str = "15minute", days: int = 5
) -> pd.DataFrame:
    kite = get_client(user_id)
    token = resolve_instrument_token(user_id, tradingsymbol, exchange)
    to_date = datetime.now(IST)
    from_date = to_date - pd.Timedelta(days=days)
    rows = kite.historical_data(token, from_date, to_date, interval)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.rename(columns={"date": "Date", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_ltp(user_id: int, tradingsymbol: str, exchange: str = "NSE") -> float:
    kite = get_client(user_id)
    key = f"{exchange}:{tradingsymbol.upper()}"
    data = kite.ltp([key])
    return float(data[key]["last_price"])


def place_order(
    user_id: int,
    tradingsymbol: str,
    exchange: str,
    transaction_type: str,  # "BUY" or "SELL"
    quantity: int,
    product: str = "MIS",
    order_type: str = "MARKET",
    price: float = None,
    trigger_price: float = None,
) -> str:
    """Places a REAL order on the connected Zerodha account. Never call this from an
    automated test or from Claude's own tool use -- only from a code path the account owner
    deliberately triggers (e.g. clicking "Place live order" in the UI). Returns the Kite
    order_id."""
    kite = get_client(user_id)
    order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        transaction_type=transaction_type,
        quantity=quantity,
        product=product,
        order_type=order_type,
        price=price,
        trigger_price=trigger_price,
    )
    return order_id
