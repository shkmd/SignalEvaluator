"""Scan orchestrator: runs the qualification engine across the whole F&O universe and
persists results. Manual trigger only in Phase 1 -- scheduled 15-minute runs are Phase 2.
"""
import io
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db, qualification, scanner_signals

MAX_WORKERS = 8


def run_scan(triggered_by_user_id: int = None) -> dict:
    universe = db.list_fo_universe(include_indices=False)
    if not universe:
        raise RuntimeError("F&O universe is empty -- refresh it first (Scanner > Refresh universe).")

    scan_run_id = db.create_scan_run(stocks_total=len(universe), triggered_by_user_id=triggered_by_user_id)

    counts = {
        "stocks_scanned": 0,
        "ce_qualified": 0,
        "pe_qualified": 0,
        "near_ce": 0,
        "near_pe": 0,
        "not_qualified": 0,
        "unavailable": 0,
        "conflict": 0,
    }

    def _scan_one(stock):
        result = qualification.evaluate_stock(stock["resolved_symbol"], futures_eligible=bool(stock["futures_eligible"]))
        return stock, result

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_scan_one, stock) for stock in universe]
        for future in as_completed(futures):
            try:
                stock, result = future.result()
            except Exception as e:
                continue

            classification = result["classification"]
            counts["stocks_scanned"] += 1
            key = {
                "CE_QUALIFIED": "ce_qualified",
                "PE_QUALIFIED": "pe_qualified",
                "NEAR_CE": "near_ce",
                "NEAR_PE": "near_pe",
                "NOT_QUALIFIED": "not_qualified",
                "DATA_UNAVAILABLE": "unavailable",
                "DATA_CONFLICT": "conflict",
            }.get(classification)
            if key:
                counts[key] += 1

            db.insert_scanner_result(
                scan_run_id,
                {
                    "symbol": stock["symbol"],
                    "resolved_symbol": stock["resolved_symbol"],
                    "company_name": stock.get("company_name"),
                    "sector": stock.get("sector"),
                    "classification": classification,
                    "ce_conditions": result.get("ce_conditions"),
                    "pe_conditions": result.get("pe_conditions"),
                    "ce_passed_count": result.get("ce_passed_count"),
                    "ce_total_count": result.get("ce_total_count"),
                    "pe_passed_count": result.get("pe_passed_count"),
                    "pe_total_count": result.get("pe_total_count"),
                    "snapshot": result.get("snapshot"),
                    "data_timestamp": result.get("data_timestamp"),
                    "error_reason": result.get("error_reason") or result.get("warning"),
                },
            )

    db.finish_scan_run(scan_run_id, counts)

    signals_generated = []
    try:
        signals_generated = scanner_signals.generate_signals_for_scan(scan_run_id)
    except Exception as e:
        print(f"[scanner] Signal generation failed for run {scan_run_id}: {e}")

    return {
        "scan_run_id": scan_run_id,
        **counts,
        "stocks_total": len(universe),
        "signals_generated": len(signals_generated),
    }


def export_results_csv(scan_run_id: int, classification: str = None) -> str:
    rows = db.list_scanner_results(scan_run_id, classification=classification, limit=5000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "symbol", "company_name", "sector", "classification",
            "ce_passed", "ce_total", "pe_passed", "pe_total", "data_timestamp", "error_reason",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["symbol"], r.get("company_name"), r.get("sector"), r["classification"],
                r.get("ce_passed_count"), r.get("ce_total_count"),
                r.get("pe_passed_count"), r.get("pe_total_count"),
                r.get("data_timestamp"), r.get("error_reason"),
            ]
        )
    return buf.getvalue()
