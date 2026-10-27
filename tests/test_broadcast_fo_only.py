"""maybe_broadcast()'s "F&O only" filter -- when a user turns it on, plain equity signals must
not reach the broadcast channel at all, regardless of score; options (CE/PE) signals still do."""
from unittest.mock import patch

from app import telegram_broadcast


def _settings(**overrides):
    base = {"enabled": True, "target_chat_id": 123, "target_chat_title": "My Channel", "min_score": 50, "fo_only": False}
    base.update(overrides)
    return base


def _signal(instrument="EQ", **overrides):
    base = {"resolved_symbol": "RELIANCE", "symbol": "RELIANCE", "instrument": instrument,
            "entry_low": 100.0, "entry_high": 100.0, "sl": 95.0, "targets": [110.0], "action": "buy"}
    base.update(overrides)
    return base


def test_fo_only_off_forwards_equity_signals_as_before():
    with patch.object(telegram_broadcast.db, "get_telegram_broadcast_settings", return_value=_settings(fo_only=False)), \
         patch.object(telegram_broadcast, "get_client") as mock_get_client:
        telegram_broadcast.maybe_broadcast(1, _signal("EQ"), {"score": 80}, signal_id=1)
    mock_get_client.assert_called_once()


def test_fo_only_on_skips_equity_signals():
    with patch.object(telegram_broadcast.db, "get_telegram_broadcast_settings", return_value=_settings(fo_only=True)), \
         patch.object(telegram_broadcast, "get_client") as mock_get_client:
        telegram_broadcast.maybe_broadcast(1, _signal("EQ"), {"score": 95}, signal_id=1)
    mock_get_client.assert_not_called()


def test_fo_only_on_still_forwards_ce_and_pe_signals():
    with patch.object(telegram_broadcast.db, "get_telegram_broadcast_settings", return_value=_settings(fo_only=True)), \
         patch.object(telegram_broadcast, "get_client") as mock_get_client:
        telegram_broadcast.maybe_broadcast(1, _signal("CE", strike=2900), {"score": 80}, signal_id=1)
        telegram_broadcast.maybe_broadcast(1, _signal("PE", strike=2900), {"score": 80}, signal_id=2)
    assert mock_get_client.call_count == 2


def test_fo_only_is_case_insensitive_and_treats_missing_instrument_as_equity():
    with patch.object(telegram_broadcast.db, "get_telegram_broadcast_settings", return_value=_settings(fo_only=True)), \
         patch.object(telegram_broadcast, "get_client") as mock_get_client:
        telegram_broadcast.maybe_broadcast(1, _signal(instrument="ce", strike=2900), {"score": 80}, signal_id=1)
        telegram_broadcast.maybe_broadcast(1, _signal(instrument=None), {"score": 80}, signal_id=2)
    mock_get_client.assert_called_once()  # only the "ce" (options) one went through


def test_fo_only_filter_runs_before_the_score_gate_would_matter_either_way():
    # a low-score equity signal is already stopped by min_score -- fo_only must stop it too,
    # independent of score, so a high-score equity signal is the real test of this filter.
    with patch.object(telegram_broadcast.db, "get_telegram_broadcast_settings", return_value=_settings(fo_only=True, min_score=0)), \
         patch.object(telegram_broadcast, "get_client") as mock_get_client:
        telegram_broadcast.maybe_broadcast(1, _signal("EQ"), {"score": 100}, signal_id=1)
    mock_get_client.assert_not_called()
