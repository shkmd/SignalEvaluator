"""TradeMind's API surface, mounted under /api/trademind by app/main.py. Kept as its own
APIRouter (rather than more endpoints piled into the already-large main.py) since this is a
genuinely separate subsystem -- see app/trademind/schema.py."""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from typing import Optional

from app import auth
from app.trademind import db as tm_db, importer
from app.trademind.scoring import compute_process_score, evaluate_trade_rules

router = APIRouter(prefix="/api/trademind", tags=["trademind"])


def current_user_id(user: dict = Depends(auth.require_user)) -> int:
    return user["id"]


# ---- Broker accounts ----

class BrokerAccountRequest(BaseModel):
    broker: str
    nickname: str
    masked_client_id: Optional[str] = None
    account_purpose: Optional[str] = None
    starting_capital: float = 0
    classification: str = "manual"


@router.get("/broker-accounts")
def list_broker_accounts(user_id: int = Depends(current_user_id)):
    return tm_db.list_broker_accounts(user_id)


@router.post("/broker-accounts")
def create_broker_account(req: BrokerAccountRequest, user_id: int = Depends(current_user_id)):
    account_id = tm_db.create_broker_account(
        user_id, req.broker, req.nickname, req.masked_client_id, req.account_purpose,
        req.starting_capital, req.classification,
    )
    return tm_db.get_broker_account(user_id, account_id)


# ---- Imports ----

@router.post("/broker-accounts/{account_id}/import")
async def import_tradebook(account_id: int, file: UploadFile = File(...), user_id: int = Depends(current_user_id)):
    account = tm_db.get_broker_account(user_id, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Broker account not found.")
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported in this release.")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    result = importer.run_import(user_id, account_id, file.filename, file_bytes)
    return result


@router.get("/imports")
def list_imports(user_id: int = Depends(current_user_id)):
    return tm_db.list_import_jobs(user_id)


@router.post("/imports/{job_id}/rollback")
def rollback_import(job_id: int, user_id: int = Depends(current_user_id)):
    result = tm_db.rollback_import_job(user_id, job_id)
    if not result.get("rolled_back"):
        raise HTTPException(status_code=404, detail=result.get("reason", "Rollback failed."))
    return result


# ---- Dashboard ----

@router.get("/dashboard")
def dashboard(user_id: int = Depends(current_user_id)):
    summary = tm_db.dashboard_summary(user_id)
    summary["compliance"] = tm_db.compliance_summary(user_id)
    return summary


# ---- Trades ----

@router.get("/trades")
def list_trades(broker_account_id: Optional[int] = None, user_id: int = Depends(current_user_id)):
    trades = tm_db.list_trades(user_id, broker_account_id=broker_account_id)
    for t in trades:
        journal = tm_db.get_journal(user_id, t["id"])
        t["process_score"] = compute_process_score(journal)
        t["journaled"] = journal is not None
    return trades


@router.get("/trades/{trade_id}")
def get_trade(trade_id: int, user_id: int = Depends(current_user_id)):
    trade = tm_db.get_trade(user_id, trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found.")
    journal = tm_db.get_journal(user_id, trade_id)
    trade["journal"] = journal
    trade["process_score"] = compute_process_score(journal)
    trade["rule_evaluations"] = tm_db.list_rule_evaluations_for_trade(user_id, trade_id)
    return trade


class TradePlanRequest(BaseModel):
    planned_sl: Optional[float] = None
    planned_target: Optional[float] = None
    planned_risk: Optional[float] = None
    setup: Optional[str] = None
    strategy: Optional[str] = None


@router.post("/trades/{trade_id}/plan")
def update_trade_plan(trade_id: int, req: TradePlanRequest, user_id: int = Depends(current_user_id)):
    trade = tm_db.get_trade(user_id, trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found.")
    updated = tm_db.update_trade_plan(
        user_id, trade_id, req.planned_sl, req.planned_target, req.planned_risk, req.setup, req.strategy
    )
    # Trade-level rules (mandatory_stop_loss, min_risk_reward) need this plan data, so
    # re-evaluate them now that it's been entered -- see importer.py's docstring for why
    # these aren't evaluated at import time.
    rules = [r for r in tm_db.list_rules(user_id) if r["rule_type"] in ("mandatory_stop_loss", "min_risk_reward")]
    evaluations = evaluate_trade_rules(updated, rules)
    tm_db.save_rule_evaluations(user_id, updated["exit_time"][:10], evaluations)
    return updated


class JournalRequest(BaseModel):
    setup_followed: bool = False
    sl_followed: bool = False
    exit_plan_followed: bool = False
    emotional_state: Optional[str] = None
    mistakes: Optional[str] = None
    what_went_well: Optional[str] = None
    what_went_wrong: Optional[str] = None
    lesson_learned: Optional[str] = None
    notes: Optional[str] = None


@router.post("/trades/{trade_id}/journal")
def save_journal(trade_id: int, req: JournalRequest, user_id: int = Depends(current_user_id)):
    trade = tm_db.get_trade(user_id, trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found.")
    return tm_db.upsert_journal(user_id, trade_id, req.model_dump())


# ---- Rules ----

RULE_TYPES = ("mandatory_stop_loss", "min_risk_reward", "max_daily_loss", "max_trades_per_day")


class RuleRequest(BaseModel):
    rule_type: str
    threshold_value: float = 0


@router.get("/rules")
def list_rules(user_id: int = Depends(current_user_id)):
    return tm_db.list_rules(user_id)


@router.post("/rules")
def create_rule(req: RuleRequest, user_id: int = Depends(current_user_id)):
    if req.rule_type not in RULE_TYPES:
        raise HTTPException(status_code=400, detail=f"rule_type must be one of {RULE_TYPES}")
    rule_id = tm_db.create_rule(user_id, req.rule_type, req.threshold_value)
    return {"id": rule_id}


@router.post("/rules/{rule_id}/toggle")
def toggle_rule(rule_id: int, enabled: bool, user_id: int = Depends(current_user_id)):
    tm_db.set_rule_enabled(user_id, rule_id, enabled)
    return {"ok": True}


@router.delete("/rules/{rule_id}")
def remove_rule(rule_id: int, user_id: int = Depends(current_user_id)):
    tm_db.delete_rule(user_id, rule_id)
    return {"ok": True}


@router.get("/compliance-summary")
def compliance_summary(user_id: int = Depends(current_user_id)):
    return tm_db.compliance_summary(user_id)
