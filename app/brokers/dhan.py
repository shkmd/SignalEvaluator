"""Dhan API v2 adapter -- same per-user broker connection pattern as Kite/Upstox (market data
for technicals/options, and never-self-tested order placement), but a different auth model:
Dhan has no OAuth redirect for individual retail users. Instead:

1. User logs into web.dhan.co -> My Profile -> "Access DhanHQ APIs" -> generates a
   Client ID + a 24-hour access token directly on Dhan's own site.
2. User pastes both into this app via /api/broker/dhan/credentials. There's no "Log in to
   Dhan" button/redirect -- the token itself IS the login, generated entirely on Dhan's side.
3. The token expires 24 hours after generation (Dhan's own platform limitation) -- the user
   re-generates and re-pastes it daily, same rhythm as Kite/Upstox's daily re-login.

IMPORTANT: place_order below builds real order-placement code but nothing in this codebase
calls it automatically -- see kite.py's module docstring for the same safety note, which
applies here identically.

Field names below (EXCH_ID, SEGMENT, INSTRUMENT, UNDERLYING_SYMBOL, SECURITY_ID, LOT_SIZE,
SM_EXPIRY_DATE, STRIKE_PRICE, OPTION_TYPE) were verified against a real download of Dhan's
scrip master CSV, not just documentation.
"""
import io
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from app import db
from app.options import pick_strike

BASE_URL = "https://api.dhan.co/v2"
INSTRUMENTS_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
TOKEN_VALIDITY_HOURS = 24

# Dhan's scrip master is the same for every user -- cached once per day, not per user.
_instrument_cache: dict = {}


def save_credentials(user_id: int, client_id: str, access_token: str) -> None:
    creds = {
        "client_id": client_id,
        "access_token": access_token,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    db.upsert_broker_account(user_id, "dhan", creds, connected=True)


def get_status(user_id: int) -> dict:
    account = db.get_broker_account(user_id, "dhan")
    if not account or not account["credentials"].get("access_token"):
        return {"configured": False, "logged_in_today": False}
    creds = account["credentials"]
    valid = _token_valid(creds)
    return {"configured": True, "logged_in_today": valid, "dhan_client_id": creds.get("client_id")}


def _token_valid(creds: dict) -> bool:
    generated_at = creds.get("generated_at")
    if not generated_at:
        return False
    try:
        generated = datetime.fromisoformat(generated_at)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - generated < timedelta(hours=TOKEN_VALIDITY_HOURS)


def _creds(user_id: int) -> dict:
    account = db.get_broker_account(user_id, "dhan")
    if not account or not account["credentials"].get("access_token"):
        raise RuntimeError("Dhan is not configured for this account.")
    creds = account["credentials"]
    if not _token_valid(creds):
        raise RuntimeError("Dhan access token has expired (24-hour validity) -- paste a fresh one from Broker Setup.")
    return creds


def has_valid_session(user_id: int) -> bool:
    try:
        _creds(user_id)
        return True
    except RuntimeError:
        return False


def _headers(user_id: int, extra: dict = None) -> dict:
    creds = _creds(user_id)
    headers = {
        "access-token": creds["access_token"],
        "client-id": creds["client_id"],
        "Accept": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def _today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _load_instruments() -> pd.DataFrame:
    cached = _instrument_cache.get(_today_key())
    if cached is not None:
        return cached
    resp = requests.get(INSTRUMENTS_URL, timeout=45)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    _instrument_cache.clear()
    _instrument_cache[_today_key()] = df
    return df


def resolve_security_id(tradingsymbol: str, exchange: str = "NSE") -> tuple:
    """Returns (security_id, exchange_segment), e.g. ('2885', 'NSE_EQ')."""
    df = _load_instruments()
    symbol = tradingsymbol.upper()
    match = df[(df["EXCH_ID"] == exchange.upper()) & (df["INSTRUMENT"] == "EQUITY") & (df["UNDERLYING_SYMBOL"].astype(str) == symbol)]
    if not match.empty:
        return str(int(match.iloc[0]["SECURITY_ID"])), f"{exchange.upper()}_EQ"

    # Indices (e.g. "NIFTY 50") aren't keyed by UNDERLYING_SYMBOL the same way equities are
    # (Dhan's own UNDERLYING_SYMBOL for the Nifty 50 index is just "NIFTY") -- match by
    # display name instead. Verified against a real download: {DISPLAY_NAME: "Nifty 50",
    # UNDERLYING_SYMBOL: "NIFTY", INSTRUMENT: "INDEX"}. "IDX_I" as the index segment code is
    # Dhan's documented convention, not independently verified against a live account here.
    idx_match = df[(df["EXCH_ID"] == exchange.upper()) & (df["INSTRUMENT"] == "INDEX") & (df["DISPLAY_NAME"].astype(str).str.upper() == symbol)]
    if not idx_match.empty:
        return str(int(idx_match.iloc[0]["SECURITY_ID"])), "IDX_I"

    raise RuntimeError(f"{tradingsymbol} not found in Dhan's instrument list.")


def fetch_fo_underlyings() -> list:
    """F&O-eligible stocks with real lot sizes/expiries, from Dhan's own scrip master -- no
    per-user auth needed since the CSV itself doesn't require a session."""
    df = _load_instruments()
    stock_futures = df[(df["EXCH_ID"] == "NSE") & (df["INSTRUMENT"] == "FUTSTK")]
    if stock_futures.empty:
        return []

    grouped = stock_futures.groupby("UNDERLYING_SYMBOL").agg(lot_size=("LOT_SIZE", "max"))
    expiries_by_name = stock_futures.groupby("UNDERLYING_SYMBOL")["SM_EXPIRY_DATE"].apply(
        lambda s: sorted({str(e) for e in s if pd.notna(e)})
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


def _historical(user_id: int, security_id: str, exchange_segment: str, instrument: str, days: int) -> pd.DataFrame:
    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=days)
    resp = requests.post(
        f"{BASE_URL}/charts/historical",
        headers=_headers(user_id, {"Content-Type": "application/json"}),
        json={
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "expiryCode": 0,
            "oi": False,
            "fromDate": from_date.isoformat(),
            "toDate": to_date.isoformat(),
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("open"):
        return pd.DataFrame()
    df = pd.DataFrame(
        {
            "Date": pd.to_datetime(data["timestamp"], unit="s"),
            "Open": data["open"],
            "High": data["high"],
            "Low": data["low"],
            "Close": data["close"],
            "Volume": data["volume"],
        }
    )
    return df.set_index("Date").sort_index()


def fetch_daily_candles(user_id: int, tradingsymbol: str, exchange: str = "NSE", days: int = 400) -> pd.DataFrame:
    """Returns a DataFrame shaped exactly like yfinance's .history() output -- a drop-in
    replacement everywhere technicals.py and qualification.py consume daily candles."""
    security_id, segment = resolve_security_id(tradingsymbol, exchange)
    instrument = "INDEX" if segment == "IDX_I" else "EQUITY"
    return _historical(user_id, security_id, segment, instrument, days)


def fetch_intraday_candles(
    user_id: int, tradingsymbol: str, exchange: str = "NSE", interval: str = "15minute", days: int = 5
) -> pd.DataFrame:
    # Dhan's /charts/historical only returns daily candles; intraday needs a separate
    # /charts/intraday endpoint this adapter doesn't implement yet -- degrade gracefully
    # (empty frame) rather than guess at an unverified endpoint shape.
    return pd.DataFrame()


def fetch_ltp(user_id: int, tradingsymbol: str, exchange: str = "NSE") -> float:
    security_id, segment = resolve_security_id(tradingsymbol, exchange)
    resp = requests.post(
        f"{BASE_URL}/marketfeed/ltp",
        headers=_headers(user_id, {"Content-Type": "application/json"}),
        json={segment: [int(security_id)]},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {}).get(segment, {})
    row = data.get(security_id) or next(iter(data.values()), None)
    if not row:
        raise RuntimeError(f"No quote returned for {tradingsymbol}.")
    return float(row["last_price"])


def find_atm_option(user_id: int, name: str, current_price: float, instrument: str, moneyness: str = "ATM") -> dict:
    """Strike at the requested moneyness (ITM/ATM/OTM) for the nearest upcoming expiry, using
    Dhan's own scrip master plus a live quote for premium/OI."""
    df = _load_instruments()
    opts = df[
        (df["EXCH_ID"] == "NSE")
        & (df["INSTRUMENT"] == "OPTSTK")
        & (df["UNDERLYING_SYMBOL"].astype(str) == name.upper())
        & (df["OPTION_TYPE"] == instrument.upper())
    ]
    if opts.empty:
        return {"available": False, "reason": f"No {instrument} contracts found for {name} on Dhan's instrument list."}

    opts = opts.copy()
    opts["expiry_date"] = pd.to_datetime(opts["SM_EXPIRY_DATE"]).dt.date
    today = datetime.now(timezone.utc).date()
    upcoming = opts[opts["expiry_date"] >= today]
    if upcoming.empty:
        return {"available": False, "reason": f"No upcoming {instrument} expiries found for {name}."}

    nearest_expiry = upcoming["expiry_date"].min()
    at_expiry = upcoming[upcoming["expiry_date"] == nearest_expiry]
    strike = pick_strike(at_expiry["STRIKE_PRICE"].tolist(), current_price, instrument, moneyness)
    closest = at_expiry[at_expiry["STRIKE_PRICE"] == strike].iloc[0]

    security_id = str(int(closest["SECURITY_ID"]))
    try:
        resp = requests.post(
            f"{BASE_URL}/marketfeed/quote",
            headers=_headers(user_id, {"Content-Type": "application/json"}),
            json={"NSE_FNO": [int(security_id)]},
            timeout=10,
        )
        resp.raise_for_status()
        quote_data = resp.json().get("data", {}).get("NSE_FNO", {})
        quote = quote_data.get(security_id) or next(iter(quote_data.values()), {})
    except Exception as e:
        return {"available": False, "reason": f"Quote fetch failed for {closest.get('SYMBOL_NAME')}: {e}"}

    return {
        "available": True,
        "source": "dhan",
        "tradingsymbol": closest.get("SYMBOL_NAME"),
        "expiry": nearest_expiry.isoformat(),
        "strike": float(closest["STRIKE_PRICE"]),
        "ltp": quote.get("last_price"),
        "oi": quote.get("oi"),
        "volume": quote.get("volume"),
        "lot_size": int(closest["LOT_SIZE"]),
        "security_id": security_id,
    }


def find_option_by_strike(user_id: int, name: str, strike: float, instrument: str) -> dict:
    """Live LTP for a SPECIFIC strike (an already-open position's strike, not a moneyness pick)
    at the nearest upcoming expiry -- see kite.find_option_by_strike() for why this exists."""
    df = _load_instruments()
    opts = df[
        (df["EXCH_ID"] == "NSE")
        & (df["INSTRUMENT"] == "OPTSTK")
        & (df["UNDERLYING_SYMBOL"].astype(str) == name.upper())
        & (df["OPTION_TYPE"] == instrument.upper())
    ]
    if opts.empty:
        return {"available": False, "reason": f"No {instrument} contracts found for {name} on Dhan's instrument list."}

    opts = opts.copy()
    opts["expiry_date"] = pd.to_datetime(opts["SM_EXPIRY_DATE"]).dt.date
    today = datetime.now(timezone.utc).date()
    upcoming = opts[opts["expiry_date"] >= today]
    if upcoming.empty:
        return {"available": False, "reason": f"No upcoming {instrument} expiries found for {name}."}

    nearest_expiry = upcoming["expiry_date"].min()
    at_expiry = upcoming[upcoming["expiry_date"] == nearest_expiry]
    match = at_expiry[(at_expiry["STRIKE_PRICE"] - strike).abs() < 0.5]
    if match.empty:
        return {"available": False, "reason": f"Strike {strike} not found for {name} {instrument} at the nearest expiry."}
    closest = match.iloc[0]

    security_id = str(int(closest["SECURITY_ID"]))
    try:
        resp = requests.post(
            f"{BASE_URL}/marketfeed/quote",
            headers=_headers(user_id, {"Content-Type": "application/json"}),
            json={"NSE_FNO": [int(security_id)]},
            timeout=10,
        )
        resp.raise_for_status()
        quote_data = resp.json().get("data", {}).get("NSE_FNO", {})
        quote = quote_data.get(security_id) or next(iter(quote_data.values()), {})
    except Exception as e:
        return {"available": False, "reason": f"Quote fetch failed for {closest.get('SYMBOL_NAME')}: {e}"}

    return {
        "available": True,
        "source": "dhan",
        "tradingsymbol": closest.get("SYMBOL_NAME"),
        "expiry": nearest_expiry.isoformat(),
        "strike": float(closest["STRIKE_PRICE"]),
        "ltp": quote.get("last_price"),
    }


def place_order(
    user_id: int,
    security_id: str,
    exchange_segment: str,
    transaction_type: str,  # "BUY" or "SELL"
    quantity: int,
    product_type: str = "INTRADAY",
    order_type: str = "MARKET",
    price: float = 0,
    trigger_price: float = 0,
) -> str:
    """Places a REAL order on the connected Dhan account. Never call this from an automated
    test or from Claude's own tool use -- only from a code path the account owner deliberately
    triggers. See kite.py's place_order docstring for the same safety note. Returns Dhan's
    orderId. Untested against a live account -- Dhan has no sandbox for this endpoint."""
    creds = _creds(user_id)
    resp = requests.post(
        f"{BASE_URL}/orders",
        headers=_headers(user_id, {"Content-Type": "application/json"}),
        json={
            "dhanClientId": creds["client_id"],
            "transactionType": transaction_type,
            "exchangeSegment": exchange_segment,
            "productType": product_type,
            "orderType": order_type,
            "validity": "DAY",
            "securityId": security_id,
            "quantity": quantity,
            "price": price,
            "triggerPrice": trigger_price,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["orderId"]
