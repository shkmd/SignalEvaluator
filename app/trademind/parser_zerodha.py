"""Parses a Zerodha Tradebook CSV export into canonical execution records, then groups those
executions (FIFO) into round-trip trades -- a Tradebook is one row per fill, not per trade, so
a position built from three scale-in buys and closed with two partial sells is five rows that
must collapse into exactly one trade record spanning the first entry to the last exit.

Deterministic and side-effect free: parse_csv() and group_into_trades() take/return plain
dicts, no DB access, so both are directly unit-testable (see tests/test_trademind_parser.py).
"""
import csv
import hashlib
import io
from datetime import datetime

PARSER_VERSION = "zerodha_tradebook_v1"

# Zerodha's actual Tradebook export header, case-insensitive; a few historical variants are
# aliased to the canonical name on the right.
_COLUMN_ALIASES = {
    "symbol": "symbol",
    "tradingsymbol": "symbol",
    "isin": "isin",
    "trade_date": "trade_date",
    "exchange": "exchange",
    "segment": "segment",
    "series": "series",
    "trade_type": "side",
    "auction": "auction",
    "quantity": "quantity",
    "price": "price",
    "trade_id": "broker_trade_id",
    "order_id": "broker_order_id",
    "order_execution_time": "executed_at",
}

REQUIRED_FIELDS = ["symbol", "exchange", "segment", "side", "quantity", "price", "executed_at"]


class ParseError(Exception):
    pass


def _normalize_row(raw_row: dict) -> dict:
    row = {}
    for key, value in raw_row.items():
        canonical = _COLUMN_ALIASES.get((key or "").strip().lower())
        if canonical:
            row[canonical] = (value or "").strip()
    return row


def _parse_timestamp(value: str) -> str:
    """Zerodha's order_execution_time is typically 'YYYY-MM-DD HH:MM:SS'; fall back to
    date-only if only trade_date is available."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            continue
    raise ParseError(f"Unrecognized timestamp format: {value!r}")


def fingerprint(broker_account_id: int, execution: dict) -> str:
    """Duplicate-detection key: same broker account + exchange + broker's own trade/order id +
    timestamp + symbol + side + quantity + price -- if a user re-uploads the same Tradebook
    (or an overlapping date range), every row hashes identically and insert_executions()'s
    unique index silently no-ops the re-import instead of doubling the trades."""
    parts = [
        str(broker_account_id),
        execution["exchange"],
        execution.get("broker_trade_id") or "",
        execution.get("broker_order_id") or "",
        execution["executed_at"],
        execution["symbol"],
        execution["side"],
        f"{execution['quantity']:.4f}",
        f"{execution['price']:.4f}",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def parse_csv(file_bytes: bytes, broker_account_id: int) -> dict:
    """Returns {"executions": [...], "rejected": [...]}. Never raises on a single bad row --
    a malformed row goes into "rejected" with a reason so the import can still complete and
    show the user exactly what didn't parse, per the import wizard's review-queue design."""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ParseError("File has no header row.")

    executions = []
    rejected = []
    for i, raw_row in enumerate(reader, start=2):  # header is row 1
        row = _normalize_row(raw_row)
        try:
            missing = [f for f in REQUIRED_FIELDS if not row.get(f)]
            if missing:
                raise ParseError(f"Missing required field(s): {', '.join(missing)}")

            side = row["side"].strip().lower()
            if side not in ("buy", "sell"):
                raise ParseError(f"Unrecognized trade_type: {row['side']!r}")

            quantity = float(row["quantity"])
            price = float(row["price"])
            if quantity <= 0 or price <= 0:
                raise ParseError("Quantity and price must both be positive.")

            executed_at = _parse_timestamp(row["executed_at"])

            execution = {
                "symbol": row["symbol"].upper(),
                "isin": row.get("isin") or None,
                "exchange": row["exchange"].upper(),
                "segment": (row.get("segment") or "EQ").upper(),
                "side": side,
                "quantity": quantity,
                "price": price,
                "broker_trade_id": row.get("broker_trade_id") or None,
                "broker_order_id": row.get("broker_order_id") or None,
                "executed_at": executed_at,
            }
            execution["fingerprint"] = fingerprint(broker_account_id, execution)
            executions.append(execution)
        except (ParseError, ValueError) as e:
            rejected.append({"row_number": i, "raw": raw_row, "reason": str(e)})

    return {"executions": executions, "rejected": rejected}


def group_into_trades(executions: list) -> list:
    """FIFO round-trip grouping, independently per (symbol, exchange, segment). Handles
    scale-ins (multiple entries before any exit), scale-outs (multiple partial exits), and a
    position flip (an exit larger than the remaining open quantity, which closes the current
    trade and immediately opens a new one in the opposite direction) -- see
    tests/test_trademind_parser.py for the exact scenarios this is verified against.

    Each returned trade dict carries "entry_executions"/"exit_executions" (lists of
    {execution, matched_quantity}) so the caller can link tm_executions.trade_id after
    inserting the trade."""
    by_key = {}
    for ex in sorted(executions, key=lambda e: e["executed_at"]):
        key = (ex["symbol"], ex["exchange"], ex["segment"])
        by_key.setdefault(key, []).append(ex)

    all_trades = []
    for (symbol, exchange, segment), execs in by_key.items():
        all_trades.extend(_group_one_symbol(symbol, exchange, segment, execs))
    return all_trades


def _group_one_symbol(symbol: str, exchange: str, segment: str, execs: list) -> list:
    trades = []
    position_qty = 0.0  # signed: positive = long, negative = short
    lots = []  # FIFO queue of [quantity, price] for the currently open side
    current = None  # in-progress trade accumulator

    def start_trade(side, first_ex):
        return {
            "symbol": symbol, "exchange": exchange, "segment": segment, "side": side,
            "entry_executions": [], "exit_executions": [], "realized_pnl": 0.0,
        }

    def finalize(trade):
        entry_qty = sum(m["matched_quantity"] for m in trade["entry_executions"])
        exit_qty = sum(m["matched_quantity"] for m in trade["exit_executions"])
        entry_value = sum(m["matched_quantity"] * m["execution"]["price"] for m in trade["entry_executions"])
        exit_value = sum(m["matched_quantity"] * m["execution"]["price"] for m in trade["exit_executions"])
        trade["quantity"] = entry_qty
        trade["avg_entry_price"] = entry_value / entry_qty if entry_qty else 0.0
        trade["avg_exit_price"] = exit_value / exit_qty if exit_qty else 0.0
        trade["entry_time"] = trade["entry_executions"][0]["execution"]["executed_at"]
        trade["exit_time"] = trade["exit_executions"][-1]["execution"]["executed_at"]
        trade["gross_pnl"] = round(trade["realized_pnl"], 2)
        trades.append(trade)

    for ex in execs:
        if position_qty == 0:
            current = start_trade("long" if ex["side"] == "buy" else "short", ex)

        is_entry = (position_qty >= 0 and ex["side"] == "buy") or (position_qty <= 0 and ex["side"] == "sell")

        if is_entry:
            current["entry_executions"].append({"execution": ex, "matched_quantity": ex["quantity"]})
            lots.append([ex["quantity"], ex["price"]])
        else:
            remaining = ex["quantity"]
            exit_side_sign = 1 if current["side"] == "long" else -1
            while remaining > 1e-9 and lots:
                lot_qty, lot_price = lots[0]
                matched = min(remaining, lot_qty)
                current["realized_pnl"] += matched * (ex["price"] - lot_price) * exit_side_sign
                current["exit_executions"].append({"execution": ex, "matched_quantity": matched})
                remaining -= matched
                lot_qty -= matched
                if lot_qty <= 1e-9:
                    lots.pop(0)
                else:
                    lots[0][0] = lot_qty

            if remaining > 1e-9:
                # Exit exceeded the open position -- this execution both closes the current
                # trade AND opens a new one (in the opposite direction) with the leftover.
                finalize(current)
                current = start_trade("short" if ex["side"] == "sell" else "long", ex)
                current["entry_executions"].append({"execution": ex, "matched_quantity": remaining})
                lots = [[remaining, ex["price"]]]

        # lots is the single source of truth for the open quantity; re-derive the signed
        # position from it rather than tracking a separate running delta, which is what
        # produced the double-bookkeeping bug this replaced.
        open_qty = sum(l[0] for l in lots)
        position_qty = open_qty if current["side"] == "long" else -open_qty

        if abs(position_qty) <= 1e-9 and current is not None and current["exit_executions"]:
            finalize(current)
            current = None
            position_qty = 0.0
            lots = []

    # Any executions left with an open (unclosed) position are simply not emitted as a trade
    # yet -- they represent a genuinely open position, out of scope for this closed-trades slice.
    return trades
