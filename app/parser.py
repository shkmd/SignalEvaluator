"""Parses raw signal text pasted from a Telegram/trading channel into structured fields."""
import re

# Common channel-abbreviation -> actual NSE trading symbol overrides.
# Extend this as you run into channels that use nicknames instead of the real NSE symbol.
SYMBOL_MAP = {
    "KALYAN": "KALYANKJIL",
    "M&M": "M&M",
    "MM": "M&M",
    "BAJFIN": "BAJFINANCE",
    "BAJAJFIN": "BAJFINANCE",
    "ICICI": "ICICIBANK",
    "HDFC": "HDFCBANK",
    "SBI": "SBIN",
    "TATASTEEL": "TATASTEEL",
    "TATAMOTORS": "TATAMOTORS",
    "RIL": "RELIANCE",
    "INFY": "INFY",
    "ADANIENT": "ADANIENT",
    "ADANIPORTS": "ADANIPORTS",
}

SIGNAL_TYPE_KEYWORDS = ["POSITIONAL", "INTRADAY", "BTST", "SWING", "SCALP"]


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_signal(raw_text: str) -> dict:
    text = raw_text.strip()
    upper = text.upper()

    result = {
        "raw_text": text,
        "signal_type": None,
        "action": None,
        "symbol": None,
        "resolved_symbol": None,
        "instrument": "EQ",
        "strike": None,
        "expiry_hint": None,
        "entry_low": None,
        "entry_high": None,
        "add_more_at": None,
        "sl": None,
        "targets": [],
        "targets_open_ended": False,
        "parse_warnings": [],
    }

    for kw in SIGNAL_TYPE_KEYWORDS:
        if kw in upper:
            result["signal_type"] = kw.lower()
            break
    if not result["signal_type"]:
        result["signal_type"] = "positional"
        result["parse_warnings"].append("No signal type (POSITIONAL/INTRADAY/...) found, defaulted to positional.")

    # Buy/Sell + SYMBOL [+ STRIKE] [+ CE/PE]
    m = re.search(
        r"\b(BUY|SELL)\s+([A-Z&\-]{2,20})\s*(\d+(?:\.\d+)?)?\s*(CE|PE)?\b",
        upper,
    )
    if m:
        result["action"] = m.group(1).lower()
        raw_symbol = m.group(2).strip("-")
        result["symbol"] = raw_symbol
        result["resolved_symbol"] = SYMBOL_MAP.get(raw_symbol, raw_symbol)
        if m.group(3):
            result["strike"] = _to_float(m.group(3))
        if m.group(4):
            result["instrument"] = m.group(4)
    else:
        result["parse_warnings"].append("Could not find a 'BUY/SELL SYMBOL [STRIKE] [CE/PE]' pattern.")

    # Entry range: "Around 13-14", "Range 13-14", "13-14 Range"
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(?:RANGE)?", upper)
    if m:
        lo, hi = _to_float(m.group(1)), _to_float(m.group(2))
        if lo is not None and hi is not None and lo <= hi:
            result["entry_low"], result["entry_high"] = lo, hi

    # "Add more at 11"
    m = re.search(r"ADD\s+MORE\s+AT\s+(\d+(?:\.\d+)?)", upper)
    if m:
        result["add_more_at"] = _to_float(m.group(1))

    # SL 08 / SL: 8 / STOPLOSS 8
    m = re.search(r"\bSL\b\s*[:\-]?\s*(\d+(?:\.\d+)?)", upper) or re.search(
        r"STOP\s*LOSS\s*[:\-]?\s*(\d+(?:\.\d+)?)", upper
    )
    if m:
        result["sl"] = _to_float(m.group(1))
    else:
        result["parse_warnings"].append("No stop-loss (SL) found.")

    # Target(s) 16/18/20/24/30+
    m = re.search(r"TARGET[S]?\s*[:\-]?\s*([\d/\.\+\s]+)", upper)
    if m:
        chunk = m.group(1).strip()
        if chunk.endswith("+"):
            result["targets_open_ended"] = True
            chunk = chunk.rstrip("+ ").strip()
        parts = [p for p in re.split(r"[/,]", chunk) if p.strip()]
        targets = []
        for p in parts:
            v = _to_float(p.strip().rstrip("+"))
            if v is not None:
                targets.append(v)
        result["targets"] = targets
        if not targets:
            result["parse_warnings"].append("Target line found but no numeric targets parsed.")
    else:
        result["parse_warnings"].append("No target(s) found.")

    return result
