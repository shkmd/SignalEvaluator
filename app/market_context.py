"""Nightly market context over the F&O universe: relative-strength rank, Weinstein stage, sector
strength and market breadth -- plus daily futures open-interest snapshots.

Why nightly and shared: none of this depends on any one signal or user. A stock's RS rank only
means something relative to its peers, so it needs the whole universe's price history at once
(one batched yfinance download, ~200 symbols), and signal scoring then just does a DB lookup.

Methodology (standard, public concepts -- Weinstein stage analysis, IBD-style RS percentile):
- RS: percentile (1-99) of a blended 3/6/9/12-month return across the universe.
- Stage: price vs its 30-week (150-day) SMA and that SMA's 20-day slope.
    Stage 2 = above a rising line (advancing), 3 = above a flat/falling line (topping),
    4 = below a falling line (declining), 1 = below a flat/rising line (basing).
- Breadth: % of the universe trading above its 30-week line; regime bullish >= 60%, bearish < 40%.
"""
import statistics
from datetime import datetime, timedelta

from app import db

SMA_DAYS = 150  # 30 weeks
SLOPE_LOOKBACK = 20
SLOPE_FLAT_BAND = 0.005  # a 30-week SMA moving < 0.5% over 20 days counts as flat
MIN_BARS = 190  # need ~9 months for the 150-day SMA + its slope + a 6-month return
RS_WEIGHTS = ((63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2))  # trading days -> weight
BULL_BREADTH = 60.0
BEAR_BREADTH = 40.0
STALE_AFTER_DAYS = 7  # ignore a context snapshot older than this (job has been failing)

JOB_CONTEXT = "market_context"
JOB_OI = "fut_oi_snapshot"


# ---------------------------------------------------------------- pure computation

def blended_return(closes: list):
    """Weighted return over the horizons the history actually covers; None if under 6 months."""
    n = len(closes)
    if n < 127 or not closes[-1]:
        return None
    total, weight_sum = 0.0, 0.0
    for days, w in RS_WEIGHTS:
        if n > days and closes[-1 - days] > 0:
            total += w * (closes[-1] / closes[-1 - days] - 1)
            weight_sum += w
    return total / weight_sum if weight_sum else None


def percentile_ranks(values: dict) -> dict:
    """{symbol: value} -> {symbol: 1..99 percentile}, ties share their average position."""
    items = [(k, v) for k, v in values.items() if v is not None]
    if not items:
        return {}
    if len(items) == 1:
        return {items[0][0]: 50}
    vals = sorted(v for _, v in items)
    out = {}
    for k, v in items:
        below = sum(1 for x in vals if x < v)
        equal = sum(1 for x in vals if x == v)
        pct = (below + (equal - 1) / 2) / (len(vals) - 1)
        out[k] = max(1, min(99, round(1 + 98 * pct)))
    return out


def classify_stage(closes: list):
    """(stage 1-4, ext_pct = % above/below the 30-week SMA) or (None, None) if too little data."""
    if len(closes) < SMA_DAYS + SLOPE_LOOKBACK:
        return None, None
    sma_now = sum(closes[-SMA_DAYS:]) / SMA_DAYS
    sma_prev = sum(closes[-SMA_DAYS - SLOPE_LOOKBACK:-SLOPE_LOOKBACK]) / SMA_DAYS
    if sma_now <= 0 or sma_prev <= 0:
        return None, None
    slope = sma_now / sma_prev - 1
    price = closes[-1]
    above = price > sma_now
    if above:
        stage = 2 if slope > SLOPE_FLAT_BAND else 3
    else:
        stage = 4 if slope < -SLOPE_FLAT_BAND else 1
    return stage, round((price / sma_now - 1) * 100, 2)


def regime_for(breadth_pct: float) -> str:
    if breadth_pct >= BULL_BREADTH:
        return "bullish"
    if breadth_pct < BEAR_BREADTH:
        return "bearish"
    return "neutral"


def compute_context(closes_by_symbol: dict, sector_by_symbol: dict = None):
    """closes_by_symbol: {symbol: [daily closes, oldest first]} -> (stock_rows, summary).
    Symbols with too little history are skipped rather than ranked on partial data."""
    sector_by_symbol = sector_by_symbol or {}
    returns, stages, rets = {}, {}, {}
    for sym, closes in closes_by_symbol.items():
        if len(closes) < MIN_BARS:
            continue
        r = blended_return(closes)
        stage, ext = classify_stage(closes)
        if r is None or stage is None:
            continue
        returns[sym] = r
        stages[sym] = (stage, ext)
        rets[sym] = (
            round(closes[-1] / closes[-64] - 1, 4) if len(closes) > 63 else None,
            round(closes[-1] / closes[-127] - 1, 4) if len(closes) > 126 else None,
        )
    ranks = percentile_ranks(returns)

    rows = []
    for sym in returns:
        rows.append({
            "symbol": sym, "sector": sector_by_symbol.get(sym), "rs": ranks[sym],
            "stage": stages[sym][0], "ext_pct": stages[sym][1],
            "ret_3m": rets[sym][0], "ret_6m": rets[sym][1],
        })

    n = len(rows)
    above = sum(1 for r in rows if r["ext_pct"] is not None and r["ext_pct"] > 0)
    breadth = round(100 * above / n, 1) if n else 0.0
    by_sector = {}
    for r in rows:
        if r["sector"]:
            by_sector.setdefault(r["sector"], []).append(r["rs"])
    summary = {
        "breadth_pct": breadth,
        "regime": regime_for(breadth) if n else "neutral",
        "stocks_count": n,
        "stage_counts": {str(s): sum(1 for r in rows if r["stage"] == s) for s in (1, 2, 3, 4)},
        # a sector's median RS needs a few members to mean anything
        "sector_rs": {k: round(statistics.median(v)) for k, v in by_sector.items() if len(v) >= 3},
    }
    return rows, summary


# ---------------------------------------------------------------- lookup for scoring

def context_for(symbol: str) -> dict:
    """The latest stored context for one symbol + the market summary, in the shape
    scoring.evaluate_signal expects. {"available": False, ...} for anything not in the F&O
    universe (indices, non-F&O stocks) or if the nightly job has never run / is stale."""
    if not symbol:
        return {"available": False, "reason": "No symbol."}
    try:
        row = db.get_latest_stock_context(symbol)
        summary = db.get_latest_market_summary()
    except Exception as e:  # scoring must never fail because of an optional enrichment
        return {"available": False, "reason": f"Market context lookup failed: {e}"}
    if not row or not summary:
        return {"available": False, "reason": "No market-context data for this symbol (not in the F&O universe, or the nightly job hasn't run yet)."}
    try:
        age = (datetime.now().date() - datetime.fromisoformat(row["as_of"]).date()).days
    except ValueError:
        age = 0
    if age > STALE_AFTER_DAYS:
        return {"available": False, "reason": f"Market context is stale (last computed {row['as_of']})."}
    return {
        "available": True,
        "as_of": row["as_of"],
        "rs": row["rs"],
        "stage": row["stage"],
        "ext_pct": row["ext_pct"],
        "sector": row["sector"],
        "sector_rs": (summary["sector_rs"] or {}).get(row["sector"]) if row["sector"] else None,
        "breadth_pct": summary["breadth_pct"],
        "regime": summary["regime"],
    }


# ---------------------------------------------------------------- nightly jobs

def _today_ist() -> str:
    return datetime.now(db.IST).date().isoformat()


def _download_closes(tickers: list) -> dict:
    """{ticker: [closes]} for ~14 months of daily bars, batched so one bad symbol can't sink all."""
    import yfinance as yf

    out = {}
    for i in range(0, len(tickers), 60):
        chunk = tickers[i:i + 60]
        try:
            df = yf.download(chunk, period="14mo", interval="1d", auto_adjust=True,
                             group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print(f"[market_context] download failed for batch starting {chunk[0]}: {e}")
            continue
        for t in chunk:
            # The column layout depends on the yfinance version and on whether the batch held
            # one ticker or many, so try the per-ticker frame first, then the flat one.
            for getter in (lambda: df[t]["Close"], lambda: df["Close"]):
                try:
                    series = getter().dropna()
                except Exception:
                    continue
                if len(series):
                    out[t] = [float(x) for x in series.tolist()]
                    break
    return out


def run_context_job() -> dict:
    universe = db.list_fo_universe(include_indices=False)
    if not universe:
        return {"ok": False, "reason": "F&O universe is empty -- refresh it from the Scanner tab first."}
    ticker_to_symbol = {f"{u['resolved_symbol']}.NS": u["symbol"] for u in universe}
    sectors = {u["symbol"]: u.get("sector") for u in universe}

    closes_by_ticker = _download_closes(list(ticker_to_symbol))
    closes = {ticker_to_symbol[t]: c for t, c in closes_by_ticker.items()}
    rows, summary = compute_context(closes, sectors)
    if len(rows) < 30:
        return {"ok": False, "reason": f"Only {len(rows)} symbols had enough history -- not saving a partial ranking."}
    as_of = _today_ist()
    db.save_market_context(as_of, rows, summary)
    db.set_job_done(JOB_CONTEXT, as_of, f"{len(rows)} stocks, breadth {summary['breadth_pct']}% ({summary['regime']})")
    print(f"[market_context] {as_of}: ranked {len(rows)} stocks, breadth {summary['breadth_pct']}% -> {summary['regime']}")
    return {"ok": True, "as_of": as_of, "stocks": len(rows), **summary}


def run_oi_snapshot_job() -> dict:
    """Snapshots every F&O futures contract's OI + LTP via the first connected Kite session.
    Kite is the only adapter with a futures-OI fetch so far; needs someone to have logged in
    that day (Kite tokens expire daily), otherwise the job is skipped and retried later."""
    from app.brokers import kite

    universe = db.list_fo_universe(include_indices=True)
    names = [u["symbol"] for u in universe]
    if not names:
        return {"ok": False, "reason": "F&O universe is empty."}
    for user_id in db.list_user_ids_with_broker("zerodha"):
        if not kite.has_valid_session(user_id):
            continue
        try:
            rows = kite.fetch_futures_oi(user_id, names)
        except Exception as e:
            print(f"[market_context] OI snapshot via user {user_id} failed: {e}")
            continue
        if not rows:
            continue
        as_of = _today_ist()
        n = db.save_fut_oi_snapshot(as_of, rows)
        db.set_job_done(JOB_OI, as_of, f"{n} contracts")
        print(f"[market_context] {as_of}: saved {n} futures OI rows")
        return {"ok": True, "as_of": as_of, "contracts": n}
    return {"ok": False, "reason": "No connected Kite session today -- log in to Kite (Broker Setup) so the snapshot can run."}


# Run windows (IST): OI needs the final settled numbers, so wait until after the close; daily
# bars from Yahoo are complete a bit later.
OI_AFTER = (15, 45)
CONTEXT_AFTER = (16, 15)


def due_jobs(now: datetime = None) -> list:
    """Which nightly jobs should run right now. Pure w.r.t. the clock so it can be tested."""
    now = now or datetime.now(db.IST)
    today = now.date().isoformat()
    due = []
    if now.weekday() >= 5:
        return due
    hm = (now.hour, now.minute)
    if hm >= CONTEXT_AFTER and db.get_job_last_date(JOB_CONTEXT) != today:
        due.append(JOB_CONTEXT)
    if hm >= OI_AFTER and db.get_job_last_date(JOB_OI) != today:
        due.append(JOB_OI)
    return due


def bootstrap_needed() -> bool:
    """First deploy: no context yet -> compute once now instead of waiting for tonight."""
    return db.get_latest_market_summary() is None
