"""Upstox API v2 adapter -- same per-user broker connection pattern as Kite
(app/brokers/kite.py): OAuth login, market data for technicals/options, and (never
self-tested) order placement. See kite.py's module docstring for the shared safety notes;
they apply here identically.

Auth flow:
1. User saves their Upstox app's API key + API secret (from
   https://upstox.com/developer/apps) via /api/broker/upstox/credentials.
2. GET /api/broker/upstox/login-url returns Upstox's OAuth authorization dialog URL. The
   user opens it and logs in directly on Upstox's site -- we never see their password.
3. Upstox redirects to this app's registered redirect URI with a `code`. The user must set
   that redirect URI in their Upstox app console to point at
   <this app's origin>/api/broker/upstox/callback.
4. The callback exchanges the code (+ client secret) for an access_token, valid until
   3:30 AM the next day regardless of when it was issued -- Upstox's own platform limitation,
   same daily-relogin UX as Kite.

Field names below (segment, name, underlying_symbol, instrument_key, trading_symbol,
strike_price, lot_size) were verified against a real download of Upstox's instrument dump,
not just documentation -- see the "EQ sample" / "OPT sample" shapes this was built from.
"""
import gzip
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from app import db
from app.options import pick_strike

IST = ZoneInfo("Asia/Kolkata")
BASE_URL = "https://api.upstox.com/v2"
ORDER_URL = "https://api-hft.upstox.com/v2/order/place"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

# Upstox's instrument dump is the same for every user (no auth needed to download) -- cached
# once per day, not per user, unlike Kite's per-user token-scoped instrument list.
_instrument_cache: dict = {}


def _today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def save_credentials(user_id: int, api_key: str, api_secret: str) -> None:
    existing = db.get_broker_account(user_id, "upstox") or {}
    creds = existing.get("credentials", {})
    creds["api_key"] = api_key
    creds["api_secret"] = api_secret
    db.upsert_broker_account(user_id, "upstox", creds, connected=bool(creds.get("access_token")))


def get_login_url(user_id: int, redirect_uri: str) -> str:
    account = db.get_broker_account(user_id, "upstox")
    if not account or not account["credentials"].get("api_key"):
        raise RuntimeError("Save your Upstox API key/secret first.")
    api_key = account["credentials"]["api_key"]
    return f"{BASE_URL}/login/authorization/dialog?response_type=code&client_id={api_key}&redirect_uri={redirect_uri}"


def handle_callback(user_id: int, code: str, redirect_uri: str) -> dict:
    account = db.get_broker_account(user_id, "upstox")
    if not account or not account["credentials"].get("api_key"):
        raise RuntimeError("No Upstox API key/secret on file for this account.")

    creds = account["credentials"]
    resp = requests.post(
        f"{BASE_URL}/login/authorization/token",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data={
            "code": code,
            "client_id": creds["api_key"],
            "client_secret": creds["api_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    creds["access_token"] = data["access_token"]
    creds["access_token_date"] = _today_ist()
    creds["upstox_user_id"] = data.get("user_id")
    db.upsert_broker_account(user_id, "upstox", creds, connected=True)
    return {"upstox_user_id": data.get("user_id")}


def get_status(user_id: int) -> dict:
    account = db.get_broker_account(user_id, "upstox")
    if not account or not account["credentials"].get("api_key"):
        return {"configured": False, "logged_in_today": False}
    creds = account["credentials"]
    logged_in_today = bool(creds.get("access_token")) and creds.get("access_token_date") == _today_ist()
    return {"configured": True, "logged_in_today": logged_in_today, "upstox_user_id": creds.get("upstox_user_id")}


def _access_token(user_id: int) -> str:
    account = db.get_broker_account(user_id, "upstox")
    if not account or not account["credentials"].get("api_key"):
        raise RuntimeError("Upstox is not configured for this account.")
    creds = account["credentials"]
    if not creds.get("access_token") or creds.get("access_token_date") != _today_ist():
        raise RuntimeError("Upstox session has expired for today -- log in again from Broker Setup.")
    return creds["access_token"]


def has_valid_session(user_id: int) -> bool:
    try:
        _access_token(user_id)
        return True
    except RuntimeError:
        return False


def _headers(user_id: int, extra: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {_access_token(user_id)}", "Accept": "application/json"}
    if extra:
        headers.update(extra)
    return headers


def _load_instruments() -> pd.DataFrame:
    cached = _instrument_cache.get(_today_ist())
    if cached is not None:
        return cached
    resp = requests.get(INSTRUMENTS_URL, timeout=30)
    resp.raise_for_status()
    rows = json.loads(gzip.decompress(resp.content))
    df = pd.DataFrame(rows)
    _instrument_cache.clear()
    _instrument_cache[_today_ist()] = df
    return df


def resolve_instrument_key(tradingsymbol: str, exchange: str = "NSE") -> str:
    df = _load_instruments()
    symbol = tradingsymbol.upper()
    match = df[(df["exchange"] == exchange.upper()) & (df["instrument_type"] == "EQ") & (df["trading_symbol"] == symbol)]
    if match.empty:
        # Indices (e.g. "NIFTY 50") aren't keyed by trading_symbol the same way equities are
        # (Upstox's own trading_symbol for the Nifty 50 index is just "NIFTY") -- match by
        # display name instead. Verified against a real download: {"segment": "NSE_INDEX",
        # "name": "Nifty 50", "trading_symbol": "NIFTY"}.
        match = df[(df["exchange"] == exchange.upper()) & (df["segment"] == "NSE_INDEX") & (df["name"].str.upper() == symbol)]
    if match.empty:
        raise RuntimeError(f"{tradingsymbol} not found in Upstox's instrument list.")
    return match.iloc[0]["instrument_key"]


def fetch_fo_underlyings() -> list:
    """F&O-eligible stocks with real lot sizes/expiries, from Upstox's own instrument dump --
    no per-user auth needed since the dump itself doesn't require a session."""
    df = _load_instruments()
    stock_futures = df[(df["segment"] == "NSE_FO") & (df["instrument_type"] == "FUT") & (df["underlying_type"] == "EQUITY")]
    if stock_futures.empty:
        return []

    grouped = stock_futures.groupby("underlying_symbol").agg(lot_size=("lot_size", "max"))
    expiries_by_name = stock_futures.groupby("underlying_symbol")["expiry"].apply(
        lambda s: sorted({pd.Timestamp(e, unit="ms").date().isoformat() for e in s})
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
    instrument_key = resolve_instrument_key(tradingsymbol, exchange)
    to_date = datetime.now(IST).date()
    from_date = to_date - pd.Timedelta(days=days)
    url = f"{BASE_URL}/historical-candle/{instrument_key}/day/{to_date.isoformat()}/{from_date.isoformat()}"
    resp = requests.get(url, headers=_headers(user_id), timeout=15)
    resp.raise_for_status()
    candles = resp.json().get("data", {}).get("candles", [])
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=["Date", "Open", "High", "Low", "Close", "Volume", "OI"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_intraday_candles(
    user_id: int, tradingsymbol: str, exchange: str = "NSE", interval: str = "15minute", days: int = 5
) -> pd.DataFrame:
    instrument_key = resolve_instrument_key(tradingsymbol, exchange)
    to_date = datetime.now(IST).date()
    from_date = to_date - pd.Timedelta(days=days)
    # Upstox's intraday intervals are "1minute"/"30minute" only (no native 15minute) --
    # fetch 1minute and resample, same trick qualification.py already needs to be robust to.
    resample_needed = interval not in ("1minute", "30minute", "day")
    fetch_interval = "1minute" if resample_needed else interval
    url = f"{BASE_URL}/historical-candle/{instrument_key}/{fetch_interval}/{to_date.isoformat()}/{from_date.isoformat()}"
    resp = requests.get(url, headers=_headers(user_id), timeout=15)
    resp.raise_for_status()
    candles = resp.json().get("data", {}).get("candles", [])
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=["Date", "Open", "High", "Low", "Close", "Volume", "OI"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()[["Open", "High", "Low", "Close", "Volume"]]

    if resample_needed:
        minutes = int("".join(c for c in interval if c.isdigit()) or 15)
        df = df.resample(f"{minutes}min").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna()
    return df


def fetch_ltp(user_id: int, tradingsymbol: str, exchange: str = "NSE") -> float:
    instrument_key = resolve_instrument_key(tradingsymbol, exchange)
    resp = requests.get(
        f"{BASE_URL}/market-quote/quotes", params={"instrument_key": instrument_key}, headers=_headers(user_id), timeout=10
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    row = next(iter(data.values()), None)
    if not row:
        raise RuntimeError(f"No quote returned for {tradingsymbol}.")
    return float(row["last_price"])


def find_atm_option(user_id: int, name: str, current_price: float, instrument: str, moneyness: str = "ATM") -> dict:
    """Strike at the requested moneyness (ITM/ATM/OTM) for the nearest upcoming expiry, using
    Upstox's own instrument dump plus a live quote for premium/OI."""
    df = _load_instruments()
    opts = df[
        (df["segment"] == "NSE_FO") & (df["underlying_symbol"] == name.upper()) & (df["instrument_type"] == instrument.upper())
    ]
    if opts.empty:
        return {"available": False, "reason": f"No {instrument} contracts found for {name} on Upstox's instrument list."}

    today_ms = datetime.now(IST).timestamp() * 1000
    upcoming = opts[opts["expiry"] >= today_ms]
    if upcoming.empty:
        return {"available": False, "reason": f"No upcoming {instrument} expiries found for {name}."}

    nearest_expiry = upcoming["expiry"].min()
    at_expiry = upcoming[upcoming["expiry"] == nearest_expiry]
    strike = pick_strike(at_expiry["strike_price"].tolist(), current_price, instrument, moneyness)
    closest = at_expiry[at_expiry["strike_price"] == strike].iloc[0]

    instrument_key = closest["instrument_key"]
    try:
        resp = requests.get(
            f"{BASE_URL}/market-quote/quotes", params={"instrument_key": instrument_key}, headers=_headers(user_id), timeout=10
        )
        resp.raise_for_status()
        quote_data = resp.json().get("data", {})
        quote = next(iter(quote_data.values()), {})
    except Exception as e:
        return {"available": False, "reason": f"Quote fetch failed for {closest['trading_symbol']}: {e}"}

    return {
        "available": True,
        "source": "upstox",
        "tradingsymbol": closest["trading_symbol"],
        "expiry": pd.Timestamp(nearest_expiry, unit="ms").date().isoformat(),
        "strike": float(closest["strike_price"]),
        "ltp": quote.get("last_price"),
        "oi": (quote.get("ohlc") or {}).get("oi") if isinstance(quote.get("ohlc"), dict) else quote.get("oi"),
        "volume": quote.get("volume"),
        "lot_size": int(closest["lot_size"]),
        "instrument_key": instrument_key,
    }


def find_option_by_strike(user_id: int, name: str, strike: float, instrument: str) -> dict:
    """Live LTP for a SPECIFIC strike (an already-open position's strike, not a moneyness pick)
    at the nearest upcoming expiry -- see kite.find_option_by_strike() for why this exists."""
    df = _load_instruments()
    opts = df[
        (df["segment"] == "NSE_FO") & (df["underlying_symbol"] == name.upper()) & (df["instrument_type"] == instrument.upper())
    ]
    if opts.empty:
        return {"available": False, "reason": f"No {instrument} contracts found for {name} on Upstox's instrument list."}

    today_ms = datetime.now(IST).timestamp() * 1000
    upcoming = opts[opts["expiry"] >= today_ms]
    if upcoming.empty:
        return {"available": False, "reason": f"No upcoming {instrument} expiries found for {name}."}

    nearest_expiry = upcoming["expiry"].min()
    at_expiry = upcoming[upcoming["expiry"] == nearest_expiry]
    match = at_expiry[(at_expiry["strike_price"] - strike).abs() < 0.5]
    if match.empty:
        return {"available": False, "reason": f"Strike {strike} not found for {name} {instrument} at the nearest expiry."}
    closest = match.iloc[0]

    instrument_key = closest["instrument_key"]
    try:
        resp = requests.get(
            f"{BASE_URL}/market-quote/quotes", params={"instrument_key": instrument_key}, headers=_headers(user_id), timeout=10
        )
        resp.raise_for_status()
        quote_data = resp.json().get("data", {})
        quote = next(iter(quote_data.values()), {})
    except Exception as e:
        return {"available": False, "reason": f"Quote fetch failed for {closest['trading_symbol']}: {e}"}

    return {
        "available": True,
        "source": "upstox",
        "tradingsymbol": closest["trading_symbol"],
        "expiry": pd.Timestamp(nearest_expiry, unit="ms").date().isoformat(),
        "strike": float(closest["strike_price"]),
        "ltp": quote.get("last_price"),
    }


def place_order(
    user_id: int,
    instrument_key: str,
    transaction_type: str,  # "BUY" or "SELL"
    quantity: int,
    product: str = "I",  # "I" (Intraday), "D" (Delivery), "MTF"
    order_type: str = "MARKET",
    price: float = 0,
    trigger_price: float = 0,
) -> str:
    """Places a REAL order on the connected Upstox account. Never call this from an automated
    test or from Claude's own tool use -- only from a code path the account owner deliberately
    triggers. See kite.py's place_order docstring for the same safety note. Returns Upstox's
    order_id. Untested against a live account -- Upstox has no sandbox for this endpoint."""
    resp = requests.post(
        ORDER_URL,
        headers=_headers(user_id, {"Content-Type": "application/json"}),
        json={
            "instrument_token": instrument_key,
            "quantity": quantity,
            "product": product,
            "validity": "DAY",
            "price": price,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": trigger_price,
            "is_amo": False,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]["order_id"]
