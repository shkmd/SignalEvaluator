"""Nightly market-context computation (RS rank, Weinstein stage, breadth) and its scoring factor."""
from datetime import datetime
from unittest.mock import patch

from app import market_context as mc
from app import scoring
from app.market_context import blended_return, classify_stage, compute_context, percentile_ranks, regime_for


def _trend(start, daily_pct, n=260):
    out, p = [], start
    for _ in range(n):
        out.append(p)
        p *= 1 + daily_pct
    return out


def test_percentile_ranks_orders_and_bounds():
    r = percentile_ranks({"A": 0.5, "B": 0.1, "C": -0.2, "D": 0.3})
    assert r["A"] == 99 and r["C"] == 1
    assert r["A"] > r["D"] > r["B"] > r["C"]


def test_percentile_ties_share_a_rank_and_none_is_ignored():
    r = percentile_ranks({"A": 0.1, "B": 0.1, "C": None})
    assert r["A"] == r["B"] and "C" not in r


def test_stage_2_for_steady_uptrend_and_4_for_steady_downtrend():
    stage, ext = classify_stage(_trend(100, 0.002))
    assert stage == 2 and ext > 0
    stage, ext = classify_stage(_trend(100, -0.002))
    assert stage == 4 and ext < 0


def test_stage_3_topping_and_stage_1_basing():
    # rally, then a long plateau: still above a 30-week line that has stopped rising
    rally = _trend(100, 0.004, 100)
    topping = rally + [rally[-1] * (1 + 0.0001 * i) for i in range(160)]
    assert classify_stage(topping)[0] == 3
    # long decline, then a flat base below the (now flattening) line
    decline = _trend(200, -0.004, 100)
    basing = decline + [decline[-1] * (1 - 0.0001 * i) for i in range(160)]
    assert classify_stage(basing)[0] == 1


def test_stage_needs_enough_history():
    assert classify_stage([100.0] * 100) == (None, None)


def test_blended_return_needs_six_months():
    assert blended_return([100.0] * 100) is None
    assert blended_return(_trend(100, 0.001)) > 0


def test_breadth_regime_thresholds():
    assert regime_for(72) == "bullish"
    assert regime_for(50) == "neutral"
    assert regime_for(39.9) == "bearish"


def test_compute_context_ranks_universe_and_summarises_breadth():
    closes = {f"UP{i}": _trend(100, 0.001 + i * 0.0003) for i in range(4)}
    closes.update({f"DN{i}": _trend(100, -0.001 - i * 0.0003) for i in range(4)})
    closes["SHORT"] = [100.0] * 50  # too little history -> skipped, not ranked
    sectors = {s: "Banks" for s in closes if s.startswith("UP")}
    sectors.update({s: "Metals" for s in closes if s.startswith("DN")})
    rows, summary = compute_context(closes, sectors)
    by = {r["symbol"]: r for r in rows}
    assert "SHORT" not in by and len(rows) == 8
    assert by["UP3"]["rs"] > by["UP0"]["rs"] > by["DN0"]["rs"] > by["DN3"]["rs"]
    assert by["UP0"]["stage"] == 2 and by["DN0"]["stage"] == 4
    assert summary["breadth_pct"] == 50.0 and summary["regime"] == "neutral"
    assert summary["sector_rs"]["Banks"] > summary["sector_rs"]["Metals"]


# ---- scoring factor ----
STRONG = {"available": True, "stage": 2, "rs": 90, "sector": "Banks", "sector_rs": 70,
          "breadth_pct": 65, "regime": "bullish", "ext_pct": 12, "as_of": "2026-09-24"}
WEAK = {"available": True, "stage": 4, "rs": 10, "sector": "Metals", "sector_rs": 20,
        "breadth_pct": 30, "regime": "bearish", "ext_pct": -15, "as_of": "2026-09-24"}


def test_bullish_call_on_a_stage2_leader_scores_full_marks():
    pts, note, flags = scoring.market_context_factor(STRONG, "bullish")
    assert pts == 15 and flags == []
    assert "Stage 2" in note and "RS 90/99" in note


def test_bullish_call_on_a_stage4_laggard_scores_zero_and_raises_flags():
    pts, _, flags = scoring.market_context_factor(WEAK, "bullish")
    assert pts == 0
    assert len(flags) == 3  # stage 4, low RS, breadth against


def test_bearish_call_mirrors_it():
    assert scoring.market_context_factor(WEAK, "bearish")[0] == 15
    pts, _, flags = scoring.market_context_factor(STRONG, "bearish")
    assert pts <= 1 and any("Stage 2" in f for f in flags)


def _base_eval(market_ctx):
    signal = {"instrument": "EQ", "action": "buy", "entry_low": 100, "entry_high": 100, "sl": 95, "targets": [110]}
    return scoring.evaluate_signal(signal, {"available": False}, {"available": False}, {"available": False},
                                   None, None, market_ctx)


def test_factor_only_appears_when_data_is_available():
    without = _base_eval(None)
    assert not any(row[0] == "Market context" for row in without["breakdown"])
    with_ctx = _base_eval(STRONG)
    row = [r for r in with_ctx["breakdown"] if r[0] == "Market context"]
    assert row and row[0][1] == 15 and row[0][2] == 15
    assert with_ctx["score"] > without["score"]


def test_context_for_unknown_symbol_or_missing_data_is_unavailable():
    with patch.object(mc.db, "get_latest_stock_context", return_value=None), \
         patch.object(mc.db, "get_latest_market_summary", return_value=None):
        assert mc.context_for("NIFTY")["available"] is False
        assert mc.context_for("")["available"] is False


def test_context_for_ignores_a_stale_snapshot_and_survives_db_errors():
    stale = {"as_of": "2020-01-01", "rs": 80, "stage": 2, "ext_pct": 5, "sector": "Banks"}
    summ = {"breadth_pct": 60, "regime": "bullish", "sector_rs": {"Banks": 70}}
    with patch.object(mc.db, "get_latest_stock_context", return_value=stale), \
         patch.object(mc.db, "get_latest_market_summary", return_value=summ):
        assert mc.context_for("HDFCBANK")["available"] is False
    with patch.object(mc.db, "get_latest_stock_context", side_effect=RuntimeError("no table")):
        assert mc.context_for("HDFCBANK")["available"] is False


def test_context_for_returns_joined_fields_when_fresh():
    today = datetime.now().date().isoformat()
    row = {"as_of": today, "rs": 80, "stage": 2, "ext_pct": 5.0, "sector": "Banks"}
    summ = {"breadth_pct": 60.0, "regime": "bullish", "sector_rs": {"Banks": 70}}
    with patch.object(mc.db, "get_latest_stock_context", return_value=row), \
         patch.object(mc.db, "get_latest_market_summary", return_value=summ):
        ctx = mc.context_for("HDFCBANK")
    assert ctx["available"] and ctx["sector_rs"] == 70 and ctx["regime"] == "bullish"


# ---- scheduling ----
def _at(h, m, day=24):  # 2026-09-24 is a Thursday
    from zoneinfo import ZoneInfo
    return datetime(2026, 9, day, h, m, tzinfo=ZoneInfo("Asia/Kolkata"))


def test_due_jobs_respect_time_windows_and_last_run():
    with patch.object(mc.db, "get_job_last_date", return_value=None):
        assert mc.due_jobs(_at(15, 0)) == []
        assert mc.due_jobs(_at(15, 50)) == [mc.JOB_OI]
        assert mc.due_jobs(_at(16, 20)) == [mc.JOB_CONTEXT, mc.JOB_OI]
        assert mc.due_jobs(_at(16, 20, day=26)) == []  # Saturday
    with patch.object(mc.db, "get_job_last_date", return_value="2026-09-24"):
        assert mc.due_jobs(_at(16, 20)) == []
