import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List

from app import db, parser as signal_parser, technicals, options as options_mod, news as news_mod, scoring
from app import telegram_ingest, telegram_auth, config, market, trading
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
        await telegram_ingest.start_listener()
    except Exception as e:
        print(f"[telegram] Startup skipped: {e}")
    _monitor_task = asyncio.create_task(_position_monitor_loop())


@app.on_event("shutdown")
async def _shutdown():
    if _monitor_task:
        _monitor_task.cancel()
    try:
        await telegram_ingest.stop_listener()
    except Exception:
        pass


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


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/parse")
def parse_signal(req: ParseRequest):
    return signal_parser.parse_signal(req.raw_text)


@app.post("/api/evaluate")
def evaluate(req: EvaluateRequest):
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

    tech = technicals.fetch_technicals(resolved_symbol, direction=direction)
    opts = options_mod.fetch_option_chain_snapshot(resolved_symbol, req.strike, instrument)
    headlines = news_mod.fetch_news(resolved_symbol)

    evaluation = scoring.evaluate_signal(signal, tech, opts, headlines)
    evaluation["technicals"] = tech
    evaluation["options"] = opts
    evaluation["news"] = headlines

    signal_id = db.insert_signal(signal, evaluation, req.channel or "unknown")
    evaluation["signal_id"] = signal_id
    evaluation["auto_trade"] = trading.auto_trade_check(signal_id, signal, evaluation)

    return evaluation


@app.get("/api/signals")
def get_signals(channel: Optional[str] = None, outcome: Optional[str] = None):
    return db.list_signals(channel=channel, outcome=outcome)


@app.get("/api/signals/{signal_id}")
def get_signal(signal_id: int):
    s = db.get_signal(signal_id)
    if not s:
        raise HTTPException(status_code=404, detail="Signal not found")
    return s


@app.post("/api/signals/{signal_id}/outcome")
def set_outcome(signal_id: int, req: OutcomeRequest):
    if req.outcome not in ("pending", "target_hit", "sl_hit", "partial"):
        raise HTTPException(status_code=400, detail="Invalid outcome value")
    ok = db.update_outcome(signal_id, req.outcome, req.note)
    if not ok:
        raise HTTPException(status_code=404, detail="Signal not found")
    return {"ok": True}


@app.get("/api/channels/stats")
def get_channel_stats():
    return db.channel_stats()


@app.get("/api/market/ticker")
def get_market_ticker():
    return market.fetch_index_quotes()


class ChannelToggleRequest(BaseModel):
    enabled: bool


class TelegramConfigRequest(BaseModel):
    api_id: str
    api_hash: str


@app.get("/api/telegram/status")
async def telegram_status():
    return await telegram_ingest.get_status()


@app.post("/api/telegram/config")
def save_telegram_config(req: TelegramConfigRequest):
    api_id = req.api_id.strip()
    api_hash = req.api_hash.strip()
    if not api_id.isdigit():
        raise HTTPException(status_code=400, detail="API ID should be numeric -- check my.telegram.org.")
    if not api_hash:
        raise HTTPException(status_code=400, detail="API Hash is required.")
    config.save_telegram_credentials(api_id, api_hash)
    reset_client()
    return {"ok": True}


@app.post("/api/telegram/reconnect")
async def telegram_reconnect():
    try:
        await telegram_ingest.start_listener()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await telegram_ingest.get_status()


class PhoneRequest(BaseModel):
    phone: str


class CodeRequest(BaseModel):
    code: str


class PasswordRequest(BaseModel):
    password: str


@app.post("/api/telegram/login/send-code")
async def telegram_send_code(req: PhoneRequest):
    try:
        return await telegram_auth.send_code(req.phone.strip())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/telegram/login/verify-code")
async def telegram_verify_code(req: CodeRequest):
    try:
        result = await telegram_auth.verify_code(req.code.strip())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result.get("logged_in"):
        await telegram_ingest.start_listener()
    return result


@app.post("/api/telegram/login/verify-password")
async def telegram_verify_password(req: PasswordRequest):
    try:
        result = await telegram_auth.verify_password(req.password)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result.get("logged_in"):
        await telegram_ingest.start_listener()
    return result


@app.post("/api/telegram/sync-channels")
async def telegram_sync_channels():
    try:
        dialogs = await telegram_ingest.fetch_dialogs()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.upsert_dialogs(dialogs)
    return db.list_monitored_channels()


@app.get("/api/telegram/channels")
def telegram_list_channels():
    return db.list_monitored_channels()


@app.post("/api/telegram/channels/{channel_id}/toggle")
def telegram_toggle_channel(channel_id: int, req: ChannelToggleRequest):
    ok = db.set_channel_enabled(channel_id, req.enabled)
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
def get_trading_settings():
    return db.get_auto_trade_settings()


@app.post("/api/trading/settings")
def save_trading_settings(req: AutoTradeSettingsRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if "mode" in updates and updates["mode"] not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    return db.save_auto_trade_settings(updates)


@app.get("/api/trading/positions")
def get_positions(mode: Optional[str] = None):
    return db.list_orders(status="open", mode=mode)


@app.get("/api/trading/orders")
def get_orders(mode: Optional[str] = None, status: Optional[str] = None):
    return db.list_orders(status=status, mode=mode)


@app.post("/api/trading/orders/{order_id}/close")
def close_order(order_id: int):
    result = trading.close_paper_order(order_id)
    if not result.get("closed"):
        raise HTTPException(status_code=400, detail=result.get("reason", "Could not close order"))
    return result


@app.get("/api/trading/pnl-summary")
def get_pnl_summary(mode: str = "paper"):
    return {"realized_today": db.daily_realized_pnl(mode=mode), "open_positions": db.count_open_positions(mode=mode)}


class BrokerConnectRequest(BaseModel):
    broker: str
    credentials: dict


@app.get("/api/broker/accounts")
def get_broker_accounts():
    return db.list_broker_accounts()


@app.post("/api/broker/connect")
def connect_broker(req: BrokerConnectRequest):
    # No order-placement adapter exists for any broker yet -- this only stores credentials
    # so the account shows as "connected" for setup purposes. Live orders are still refused
    # in trading.place_live_order() until a real adapter is wired up for this broker.
    db.upsert_broker_account(req.broker, req.credentials, connected=True)
    return {"ok": True}


@app.post("/api/broker/{broker}/disconnect")
def disconnect_broker(broker: str):
    ok = db.disconnect_broker_account(broker)
    if not ok:
        raise HTTPException(status_code=404, detail="Broker account not found")
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
