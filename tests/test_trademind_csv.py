"""Tests parse_csv() against a realistic Zerodha Tradebook export, including a row that should
land in the rejected/review queue rather than silently being dropped or crashing the import."""
from app.trademind.parser_zerodha import parse_csv

SAMPLE_CSV = (
    "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,"
    "trade_id,order_id,order_execution_time\n"
    "RELIANCE,INE002A01018,2026-01-01,NSE,EQ,EQ,buy,false,100,2450.5,T1,O1,2026-01-01 09:15:03\n"
    "RELIANCE,INE002A01018,2026-01-01,NSE,EQ,EQ,sell,false,100,2465.0,T2,O2,2026-01-01 10:30:12\n"
    "TCS,INE467B01029,2026-01-01,NSE,EQ,EQ,buy,false,-5,3500,T3,O3,2026-01-01 09:20:00\n"  # bad qty
)


def test_parse_csv_happy_path_and_rejected_row():
    result = parse_csv(SAMPLE_CSV.encode(), broker_account_id=1)
    assert len(result["executions"]) == 2
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["row_number"] == 4

    first = result["executions"][0]
    assert first["symbol"] == "RELIANCE"
    assert first["side"] == "buy"
    assert first["quantity"] == 100
    assert first["price"] == 2450.5
    assert first["executed_at"] == "2026-01-01T09:15:03"
    assert first["fingerprint"]


def test_parse_csv_no_header_raises():
    import pytest
    from app.trademind.parser_zerodha import ParseError

    with pytest.raises(ParseError):
        parse_csv(b"", broker_account_id=1)
