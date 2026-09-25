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

# Ordinary words that sit right after BUY/SELL or at the start of a line in channel messages but
# are never a stock symbol. Without this, "BUY ABOVE 130" parsed "ABOVE" as the symbol.
NOT_A_SYMBOL = {
    "ABOVE", "ABV", "BELOW", "NEAR", "AROUND", "AT", "ON", "RANGE", "WITH", "ONLY", "TODAY", "NOW",
    "CMP", "LEVEL", "LEVELS", "PRICE", "ZONE", "STOCK", "CALL", "PUT", "OPTION", "OPTIONS", "EQUITY",
    "CASH", "FUT", "FUTURE", "FUTURES", "LOT", "LOTS", "QTY", "SL", "TGT", "TARGET", "TARGETS",
    "STOPLOSS", "BUY", "SELL", "ENTRY", "EXIT", "SCORE", "TRADE", "SETUP",
    "POSITIONAL", "INTRADAY", "BTST", "SWING", "SCALP",
}

# "NIFTY 23200 CE" at the start of a line -- the common channel layout, symbol first.
_HEADER_RE = re.compile(r"^[ \t]*([A-Z&\-]{2,20})[ \t]+(\d+(?:\.\d+)?)[ \t]*(CE|PE)\b", re.MULTILINE)
# "BUY RELIANCE 2900 CE" -- action first.
_ACTION_SYMBOL_RE = re.compile(r"\b(BUY|SELL)\s+([A-Z&\-]{2,20})\s*(\d+(?:\.\d+)?)?\s*(CE|PE)?\b")


# Currency markers between a keyword and its number ("ABOVE ₹170", "SL Rs. 160", "TGT INR 230").
# Rs/INR are only stripped when a digit follows, so a symbol that merely starts with those
# letters is left alone.
_CURRENCY_RE = re.compile(r"₹|\bRS\.?\s*(?=\d)|\bINR\s*(?=\d)")

# A line that starts with SL / stop loss / target -- levels, not the entry.
_LEVEL_LINE_RE = re.compile(r"\s*(?:TGTS?|TRGTS?|TARGETS?|SL|STOP\s*LOSS)\b")


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_signal(raw_text: str) -> dict:
    text = raw_text.strip()
    upper = _CURRENCY_RE.sub("", text.upper())

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

    action_m = re.search(r"\b(BUY|SELL)\b", upper)
    if action_m:
        result["action"] = action_m.group(1).lower()

    symbol_m = strike = instrument = None
    for m in _HEADER_RE.finditer(upper):
        if m.group(1).strip("-") not in NOT_A_SYMBOL:
            symbol_m, strike, instrument = m.group(1), m.group(2), m.group(3)
            break
    if symbol_m is None:
        for m in _ACTION_SYMBOL_RE.finditer(upper):
            if m.group(2).strip("-") not in NOT_A_SYMBOL:
                symbol_m, strike, instrument = m.group(2), m.group(3), m.group(4)
                break

    if symbol_m:
        raw_symbol = symbol_m.strip("-")
        result["symbol"] = raw_symbol
        result["resolved_symbol"] = SYMBOL_MAP.get(raw_symbol, raw_symbol)
        if strike:
            result["strike"] = _to_float(strike)
        if instrument:
            result["instrument"] = instrument
    else:
        result["parse_warnings"].append("Could not find a 'SYMBOL [STRIKE] [CE/PE]' or 'BUY/SELL SYMBOL' pattern.")

    # The entry is searched for only on lines that aren't the SL/target lines: a dash-separated
    # target line ("TGT 182-200-230") otherwise looks exactly like an entry range "182-200".
    entry_text = "\n".join(l for l in upper.split("\n") if not _LEVEL_LINE_RE.match(l))

    # Entry range: "Around 13-14", "Range 13-14", "13-14 Range"
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(?:RANGE)?", entry_text)
    if m:
        lo, hi = _to_float(m.group(1)), _to_float(m.group(2))
        if lo is not None and hi is not None and lo <= hi:
            result["entry_low"], result["entry_high"] = lo, hi

    # "BUY ABOVE 130" / "BUY NEAR 130" / "SELL BELOW 90" / "BUY AT 130" -- a single entry price.
    if result["entry_low"] is None:
        m = re.search(r"\b(?:ABOVE|ABV|NEAR|AROUND|BELOW)\s*[:\-]?\s*(\d+(?:\.\d+)?)", entry_text) or re.search(
            r"\b(?:BUY|SELL)\s+(?:AT|@|ON)\s*(\d+(?:\.\d+)?)", entry_text
        )
        if m:
            result["entry_low"] = result["entry_high"] = _to_float(m.group(1))

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
    m = re.search(r"\b(?:TARGETS?|TGTS?|TRGTS?)\b\s*[:\-]?\s*([\d/\.\+\s,\-]+)", upper)
    if m:
        chunk = m.group(1).strip()
        if chunk.endswith("+"):
            result["targets_open_ended"] = True
            chunk = chunk.rstrip("+ ").strip()
        parts = [p for p in re.split(r"[/,\-]", chunk) if p.strip()]
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
