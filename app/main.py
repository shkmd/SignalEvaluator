import asyncio
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr
from typing import Optional, List

from app import db, parser as signal_parser, technicals, options as options_mod, news as news_mod, scoring
from app import telegram_ingest, telegram_auth, market, trading, auth, screener, stock_score
from app import fo_universe, scanner, telegram_broadcast
from app.brokers import kite as kite_broker
from fastapi.responses import PlainTextResponse, RedirectResponse
from app.telegram_client import reset_client

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Signal Evaluator")

_monitor_task = None


async def _position_monitor_loop():
    while True:
        try:
            await asyncio.to_thread(trading.monitor_open_positions)
        except Exception as e:
            print(f"[trading] Position monitor error: {e}")
        await asyncio.sleep(60)


@app.on_event("startup")
async def _startup():
    global _monitor_task
    db.init_db()
    try:
        await telegram_ingest.start_all_known_listeners()
    except Exception as e:
        print(f"[telegram] Startup skipped: {e}")
    _monitor_task = asyncio.create_task(_position_monitor_loop())


@app.on_event("shutdown")
async def _shutdown():
    if _monitor_task:
        _monitor_task.cancel()


def current_user_id(user: dict = Depends(auth.require_user)) -> int:
    return user["id"]


# ---- Auth ----

class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        auth.SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=auth.SESSION_DAYS * 86400
    )


@app.post("/api/auth/signup")
def signup(req: SignupRequest, response: Response):
    if db.get_user_by_email(req.email):
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    password_hash = auth.hash_password(req.password)
    verification_token = secrets.token_urlsafe(24)
    user_id = db.create_user(req.email, password_hash, verification_token)

    token, _ = auth.create_session_for_user(user_id)
    _set_session_cookie(response, token)

    return {
        "user": {"id": user_id, "email": req.email, "email_verified": False},
        "note": "Email verification isn't wired up to a mail provider yet -- your account works immediately.",
    }


@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response):
    user = db.get_user_by_email(req.email)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token, _ = auth.create_session_for_user(user["id"])
    _set_session_cookie(response, token)
    return {"user": {"id": user["id"], "email": user["email"], "email_verified": bool(user["email_verified"])}}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        db.delete_session(token)
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(auth.require_user)):
    return {"id": user["id"], "email": user["email"], "email_verified": bool(user["email_verified"])}


class ParseRequest(BaseModel):
    raw_text: str


class EvaluateRequest(BaseModel):
    raw_text: Optional[str] = None
    channel: Optional[str] = "unknown"
    signal_type: Optional[str] = "positional"
    action: Optional[str] = "buy"
    symbol: str
    resolved_symbol: Optional[str] = None
    instrument: Optional[str] = "EQ"  # EQ, CE, PE
    strike: Optional[float] = None
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    sl: Optional[float] = None
    targets: List[float] = []


class OutcomeRequest(BaseModel):
    outcome: str  # pending | target_hit | sl_hit | partial
    note: Optional[str] = None


def _asset_version() -> str:
    """Cache-busting token derived from the static files' own mtimes. A browser that has
    a long-lived cached copy of app.js/style.css from before a change will still fetch the
    new one, because the URL it's referenced by has actually changed -- no reliance on the
    browser correctly honoring cache-control headers."""
    js_mtime = (STATIC_DIR / "app.js").stat().st_mtime
    css_mtime = (STATIC_DIR / "style.css").stat().st_mtime
    return str(int(max(js_mtime, css_mtime)))


@app.get("/")
def root():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    v = _asset_version()
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={v}"')
    return Response(content=html, media_type="text/html")


@app.post("/api/parse")
def parse_signal(req: ParseRequest, user_id: int = Depends(current_user_id)):
    return signal_parser.parse_signal(req.raw_text)


@app.post("/api/evaluate")
def evaluate(req: EvaluateRequest, user_id: int = Depends(current_user_id)):
    symbol = req.symbol.strip().upper()
    resolved_symbol = (req.resolved_symbol or symbol).strip().upper()
    instrument = (req.instrument or "EQ").strip().upper()

    signal = {
        "raw_text": req.raw_text,
        "signal_type": req.signal_type,
        "action": req.action,
        "symbol": symbol,
        "resolved_symbol": resolved_symbol,
        "instrument": instrument,
        "strike": req.strike,
        "entry_low": req.entry_low,
        "entry_high": req.entry_high,
        "sl": req.sl,
        "targets": req.targets or [],
    }

    direction = "bearish" if (instrument == "PE" or req.action == "sell") else "bullish"

    tech = technicals.fetch_technicals(resolved_symbol, direction=direction, user_id=user_id)
    opts = options_mod.fetch_option_chain_snapshot(resolved_symbol, req.strike, instrument)
    headlines = news_mod.fetch_news(resolved_symbol)
    scr = screener.evaluate_screener(resolved_symbol, direction)
    stock_ctx = stock_score.evaluate_stock_context(resolved_symbol)

    evaluation = scoring.evaluate_signal(signal, tech, opts, headlines, scr, stock_ctx)
    evaluation["technicals"] = tech
    evaluation["options"] = opts
    evaluation["news"] = headlines
    evaluation["screener"] = scr
    evaluation["stock_context"] = stock_ctx

    signal_id = db.insert_signal(user_id, signal, evaluation, req.channel or "unknown")
    evaluation["signal_id"] = signal_id
    evaluation["auto_trade"] = trading.auto_trade_check(user_id, signal_id, signal, evaluation)
    if signal_id:
        telegram_broadcast.maybe_broadcast(user_id, signal, evaluation, signal_id)

    return evaluation


@app.get("/api/signals")
def get_signals(channel: Optional[str] = None, outcome: Optional[str] = None, user_id: int = Depends(current_user_id)):
    return db.list_signals(user_id, channel=channel, outcome=outcome)


@app.get("/api/signals/{signal_id}")
def get_signal(signal_id: int, user_id: int = Depends(current_user_id)):
    s = db.get_signal(user_id, signal_id)
    if not s:
        raise HTTPException(status_code=404, detail="Signal not found")
    return s


@app.post("/api/signals/{signal_id}/outcome")
def set_outcome(signal_id: int, req: OutcomeRequest, user_id: int = Depends(current_user_id)):
    if req.outcome not in ("pending", "target_hit", "sl_hit", "partial"):
        raise HTTPException(status_code=400, detail="Invalid outcome value")
    ok = db.update_outcome(user_id, signal_id, req.outcome, req.note)
    if not ok:
        raise HTTPException(status_code=404, detail="Signal not found")
    return {"ok": True}


@app.get("/api/channels/stats")
def get_channel_stats(user_id: int = Depends(current_user_id)):
    return db.channel_stats(user_id)


@app.get("/api/market/ticker")
def get_market_ticker():
    return market.fetch_index_quotes()


class ChannelToggleRequest(BaseModel):
    enabled: bool


class TelegramConfigRequest(BaseModel):
    api_id: str
    api_hash: str


@app.get("/api/telegram/status")
async def telegram_status(user_id: int = Depends(current_user_id)):
    return await telegram_ingest.get_status(user_id)


@app.post("/api/telegram/config")
def save_telegram_config(req: TelegramConfigRequest, user_id: int = Depends(current_user_id)):
    api_id = req.api_id.strip()
    api_hash = req.api_hash.strip()
    if not api_id.isdigit():
        raise HTTPException(status_code=400, detail="API ID should be numeric -- check my.telegram.org.")
    if not api_hash:
        raise HTTPException(status_code=400, detail="API Hash is required.")
    db.save_telegram_credentials(user_id, api_id, api_hash)
    reset_client(user_id)
    return {"ok": True}


@app.post("/api/telegram/reconnect")
async def telegram_reconnect(user_id: int = Depends(current_user_id)):
    try:
        await telegram_ingest.start_listener(user_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await telegram_ingest.get_status(user_id)


class PhoneRequest(BaseModel):
    phone: str


class CodeRequest(BaseModel):
    code: str


class PasswordRequest(BaseModel):
    password: str


@app.post("/api/telegram/login/send-code")
async def telegram_send_code(req: PhoneRequest, user_id: int = Depends(current_user_id)):
    try:
        return await telegram_auth.send_code(user_id, req.phone.strip())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/telegram/login/verify-code")
async def telegram_verify_code(req: CodeRequest, user_id: int = Depends(current_user_id)):
    try:
        result = await telegram_auth.verify_code(user_id, req.code.strip())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result.get("logged_in"):
        await telegram_ingest.start_listener(user_id)
    return result


@app.post("/api/telegram/login/verify-password")
async def telegram_verify_password(req: PasswordRequest, user_id: int = Depends(current_user_id)):
    try:
        result = await telegram_auth.verify_password(user_id, req.password)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result.get("logged_in"):
        await telegram_ingest.start_listener(user_id)
    return result


@app.post("/api/telegram/sync-channels")
async def telegram_sync_channels(user_id: int = Depends(current_user_id)):
    try:
        dialogs = await telegram_ingest.fetch_dialogs(user_id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.upsert_dialogs(user_id, dialogs)
    return db.list_monitored_channels(user_id)


@app.get("/api/telegram/channels")
def telegram_list_channels(user_id: int = Depends(current_user_id)):
    return db.list_monitored_channels(user_id)


@app.post("/api/telegram/channels/{channel_id}/toggle")
def telegram_toggle_channel(channel_id: int, req: ChannelToggleRequest, user_id: int = Depends(current_user_id)):
    ok = db.set_channel_enabled(user_id, channel_id, req.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"ok": True}


class AutoTradeSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    mode: Optional[str] = None
    min_score: Optional[float] = None
    quantity: Optional[float] = None
    max_open_positions: Optional[int] = None
    max_daily_loss: Optional[float] = None


@app.get("/api/trading/settings")
def get_trading_settings(user_id: int = Depends(current_user_id)):
    return db.get_auto_trade_settings(user_id)


@app.post("/api/trading/settings")
def save_trading_settings(req: AutoTradeSettingsRequest, user_id: int = Depends(current_user_id)):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if "mode" in updates and updates["mode"] not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    return db.save_auto_trade_settings(user_id, updates)


@app.get("/api/trading/positions")
def get_positions(mode: Optional[str] = None, user_id: int = Depends(current_user_id)):
    return db.list_orders(user_id, status="open", mode=mode)


@app.get("/api/trading/orders")
def get_orders(mode: Optional[str] = None, status: Optional[str] = None, user_id: int = Depends(current_user_id)):
    return db.list_orders(user_id, status=status, mode=mode)


@app.post("/api/trading/orders/{order_id}/close")
def close_order(order_id: int, user_id: int = Depends(current_user_id)):
    result = trading.close_order(user_id, order_id)
    if not result.get("closed"):
        raise HTTPException(status_code=400, detail=result.get("reason", "Could not close order"))
    return result


@app.get("/api/trading/pnl-summary")
def get_pnl_summary(mode: str = "paper", user_id: int = Depends(current_user_id)):
    return {
        "realized_today": db.daily_realized_pnl(user_id, mode=mode),
        "open_positions": db.count_open_positions(user_id, mode=mode),
    }


class BrokerConnectRequest(BaseModel):
    broker: str
    credentials: dict


@app.get("/api/broker/accounts")
def get_broker_accounts(user_id: int = Depends(current_user_id)):
    return db.list_broker_accounts(user_id)


@app.post("/api/broker/connect")
def connect_broker(req: BrokerConnectRequest, user_id: int = Depends(current_user_id)):
    # No order-placement adapter exists for any broker yet -- this only stores credentials
    # so the account shows as "connected" for setup purposes. Live orders are still refused
    # in trading.place_live_order() until a real adapter is wired up for this broker.
    db.upsert_broker_account(user_id, req.broker, req.credentials, connected=True)
    return {"ok": True}


@app.post("/api/broker/{broker}/disconnect")
def disconnect_broker(broker: str, user_id: int = Depends(current_user_id)):
    ok = db.disconnect_broker_account(user_id, broker)
    if not ok:
        raise HTTPException(status_code=404, detail="Broker account not found")
    return {"ok": True}


class NoCacheStaticFiles(StaticFiles):
    """Forces browsers to revalidate static assets on every load instead of trusting
    heuristic freshness -- without this, a browser can silently keep serving a stale
    app.js/style.css for a long time after a deploy, with no visible error."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@app.get("/api/scanner/universe")
def get_fo_universe(include_indices: bool = False, user_id: int = Depends(current_user_id)):
    return db.list_fo_universe(include_indices=include_indices)


@app.post("/api/scanner/universe/refresh")
def refresh_fo_universe(user_id: int = Depends(current_user_id)):
    result = fo_universe.fetch_fo_universe()
    if not result["available"]:
        raise HTTPException(status_code=502, detail=result["reason"])

    stocks = result["stocks"]
    lot_sizes_added = 0
    try:
        if kite_broker.has_valid_session(user_id):
            kite_rows = {r["symbol"]: r for r in kite_broker.fetch_fo_underlyings(user_id)}
            for s in stocks:
                match = kite_rows.get(s["symbol"])
                if match:
                    s["lot_size"] = match["lot_size"]
                    s["expiries"] = match["expiries"]
                    lot_sizes_added += 1
    except Exception:
        pass  # NSE-only universe still works fine without Kite's lot sizes/expiries

    count = db.replace_fo_universe(stocks + result["indices"])
    return {
        "ok": True,
        "stocks": len(stocks),
        "indices": len(result["indices"]),
        "total_rows": count,
        "lot_sizes_from_kite": lot_sizes_added,
    }


@app.post("/api/scanner/run")
def run_scanner(user_id: int = Depends(current_user_id)):
    try:
        return scanner.run_scan(triggered_by_user_id=user_id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/scanner/runs")
def list_scan_runs(limit: int = 20, user_id: int = Depends(current_user_id)):
    return db.list_scan_runs(limit=limit)


@app.get("/api/scanner/runs/latest")
def get_latest_run(user_id: int = Depends(current_user_id)):
    run = db.get_latest_scan_run()
    if not run:
        raise HTTPException(status_code=404, detail="No scan runs yet -- run a scan first.")
    return run


@app.get("/api/scanner/results")
def get_scanner_results(
    run_id: Optional[int] = None, classification: Optional[str] = None, user_id: int = Depends(current_user_id)
):
    if run_id is None:
        latest = db.get_latest_scan_run()
        if not latest:
            return []
        run_id = latest["id"]
    return db.list_scanner_results(run_id, classification=classification)


@app.get("/api/scanner/results/{symbol}")
def get_scanner_result_detail(symbol: str, run_id: Optional[int] = None, user_id: int = Depends(current_user_id)):
    if run_id is None:
        latest = db.get_latest_scan_run()
        if not latest:
            raise HTTPException(status_code=404, detail="No scan runs yet.")
        run_id = latest["id"]
    result = db.get_scanner_result(run_id, symbol.upper())
    if not result:
        raise HTTPException(status_code=404, detail="No result for this symbol in the given run.")
    return result


@app.get("/api/scanner/export.csv")
def export_scanner_csv(
    run_id: Optional[int] = None, classification: Optional[str] = None, user_id: int = Depends(current_user_id)
):
    if run_id is None:
        latest = db.get_latest_scan_run()
        if not latest:
            raise HTTPException(status_code=404, detail="No scan runs yet.")
        run_id = latest["id"]
    csv_text = scanner.export_results_csv(run_id, classification=classification)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=scan_{run_id}_results.csv"},
    )


class ScannerSignalSettingsRequest(BaseModel):
    enabled: bool


@app.get("/api/scanner/signal-settings")
def get_scanner_signal_settings(user_id: int = Depends(current_user_id)):
    return db.get_scanner_signal_settings(user_id)


@app.post("/api/scanner/signal-settings")
def save_scanner_signal_settings(req: ScannerSignalSettingsRequest, user_id: int = Depends(current_user_id)):
    return db.save_scanner_signal_settings(user_id, req.enabled)


class KiteCredentialsRequest(BaseModel):
    api_key: str
    api_secret: str


@app.post("/api/broker/kite/credentials")
def save_kite_credentials(req: KiteCredentialsRequest, user_id: int = Depends(current_user_id)):
    kite_broker.save_credentials(user_id, req.api_key.strip(), req.api_secret.strip())
    return {"ok": True}


@app.get("/api/broker/kite/login-url")
def kite_login_url(user_id: int = Depends(current_user_id)):
    try:
        return {"url": kite_broker.get_login_url(user_id)}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/broker/kite/callback")
def kite_callback(request_token: str, user_id: int = Depends(current_user_id)):
    try:
        kite_broker.handle_callback(user_id, request_token)
    except RuntimeError as e:
        return RedirectResponse(url=f"/?kite_error={e}")
    return RedirectResponse(url="/?kite_connected=1")


@app.get("/api/broker/kite/status")
def kite_status(user_id: int = Depends(current_user_id)):
    return kite_broker.get_status(user_id)


class TelegramBroadcastSettingsRequest(BaseModel):
    enabled: bool
    target_chat_id: Optional[int] = None
    target_chat_title: Optional[str] = None
    min_score: float = 60


@app.get("/api/telegram/broadcast-settings")
def get_telegram_broadcast_settings(user_id: int = Depends(current_user_id)):
    return db.get_telegram_broadcast_settings(user_id)


@app.post("/api/telegram/broadcast-settings")
def save_telegram_broadcast_settings(req: TelegramBroadcastSettingsRequest, user_id: int = Depends(current_user_id)):
    return db.save_telegram_broadcast_settings(
        user_id, req.enabled, req.target_chat_id, req.target_chat_title, req.min_score
    )


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
