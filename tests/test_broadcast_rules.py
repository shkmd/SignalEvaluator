"""maybe_broadcast() fans a signal out to every one of the user's broadcast rules that matches
it -- on score, on the fo_only (options/CE-PE) filter, and on which SOURCE generated the signal
(manual/telegram/scanner/chartink). A signal can go to more than one channel if more than one
rule matches, and a rule with no sources configured matches every source (today's default)."""
from unittest.mock import patch

from app import telegram_broadcast


def _rule(**overrides):
    base = {"id": 1, "enabled": True, "target_chat_id": 111, "target_chat_title": "Chan",
            "min_score": 50, "fo_only": False, "sources": None}
    base.update(overrides)
    return base


def _signal(instrument="EQ", **overrides):
    base = {"resolved_symbol": "RELIANCE", "symbol": "RELIANCE", "instrument": instrument,
            "entry_low": 100.0, "entry_high": 100.0, "sl": 95.0, "targets": [110.0], "action": "buy"}
    base.update(overrides)
    return base


def _broadcast(rules, signal, score=80, source="manual", signal_id=1):
    # run_coroutine_threadsafe is mocked out (no real event loop in a sync test), which leaves
    # the real _send(...) coroutine it was handed never awaited -- close() it in the mock so
    # Python doesn't warn about that on GC; it's a test artifact, not a production concern.
    def _close_and_record(coro, loop):
        coro.close()

    with patch.object(telegram_broadcast.db, "list_broadcast_rules", return_value=rules), \
         patch.object(telegram_broadcast, "get_client") as mock_get_client, \
         patch.object(telegram_broadcast, "get_active_loop", return_value="fake-loop") as mock_loop, \
         patch.object(telegram_broadcast.asyncio, "run_coroutine_threadsafe", side_effect=_close_and_record) as mock_schedule:
        telegram_broadcast.maybe_broadcast(1, signal, {"score": score}, signal_id, source=source)
    return mock_get_client, mock_loop, mock_schedule


# ---- basic gates (unchanged from the single-rule version) ----

def test_disabled_rule_is_skipped():
    _, _, mock_schedule = _broadcast([_rule(enabled=False)], _signal())
    mock_schedule.assert_not_called()


def test_rule_with_no_target_channel_is_skipped():
    _, _, mock_schedule = _broadcast([_rule(target_chat_id=None)], _signal())
    mock_schedule.assert_not_called()


def test_score_below_rule_minimum_is_skipped():
    _, _, mock_schedule = _broadcast([_rule(min_score=90)], _signal(), score=80)
    mock_schedule.assert_not_called()


def test_no_rules_at_all_never_touches_the_telegram_client():
    mock_get_client, _, mock_schedule = _broadcast([], _signal())
    mock_get_client.assert_not_called()
    mock_schedule.assert_not_called()


# ---- fo_only filter ----

def test_fo_only_rule_skips_equity_signals():
    _, _, mock_schedule = _broadcast([_rule(fo_only=True)], _signal("EQ"), score=95)
    mock_schedule.assert_not_called()


def test_fo_only_rule_forwards_ce_and_pe():
    _, _, mock_schedule = _broadcast([_rule(fo_only=True)], _signal("CE", strike=2900), score=80)
    mock_schedule.assert_called_once()


def test_fo_only_is_case_insensitive_and_treats_missing_instrument_as_equity():
    _, _, mock_schedule = _broadcast([_rule(fo_only=True)], _signal(instrument="ce", strike=2900), score=80)
    mock_schedule.assert_called_once()
    _, _, mock_schedule = _broadcast([_rule(fo_only=True)], _signal(instrument=None), score=80)
    mock_schedule.assert_not_called()


# ---- source filter -- the actual new behavior ----

def test_rule_with_no_sources_set_matches_every_source():
    for source in ("manual", "telegram", "scanner", "chartink"):
        _, _, mock_schedule = _broadcast([_rule(sources=None)], _signal(), source=source)
        mock_schedule.assert_called_once()


def test_rule_restricted_to_scanner_and_chartink_only_matches_those():
    rule = _rule(sources=["scanner", "chartink"])
    for source in ("scanner", "chartink"):
        _, _, mock_schedule = _broadcast([rule], _signal(), source=source)
        mock_schedule.assert_called_once()
    for source in ("manual", "telegram"):
        _, _, mock_schedule = _broadcast([rule], _signal(), source=source)
        mock_schedule.assert_not_called()


# ---- fan-out to multiple matching rules ----

def test_signal_matching_two_rules_is_scheduled_to_send_to_both():
    rules = [
        _rule(id=1, target_chat_id=111, sources=None),
        _rule(id=2, target_chat_id=222, sources=["scanner", "chartink"]),
    ]
    _, _, mock_schedule = _broadcast(rules, _signal(), source="chartink")
    assert mock_schedule.call_count == 2


def test_only_the_matching_rule_out_of_several_fires():
    rules = [
        _rule(id=1, target_chat_id=111, sources=["manual", "telegram"]),
        _rule(id=2, target_chat_id=222, sources=["scanner", "chartink"]),
    ]
    _, _, mock_schedule = _broadcast(rules, _signal(), source="chartink")
    assert mock_schedule.call_count == 1


def test_no_matching_rules_never_fetches_a_telegram_client():
    rules = [_rule(sources=["scanner"])]
    mock_get_client, _, mock_schedule = _broadcast(rules, _signal(), source="manual")
    mock_get_client.assert_not_called()
    mock_schedule.assert_not_called()


# ---- db.list_broadcast_rules / create / update / delete round-trip against a temp DB ----

def _fresh_db(tmp_path):
    import importlib
    from app import db
    db.DB_PATH = tmp_path / "t.db"
    db.init_db()
    return db


def test_broadcast_rule_crud_round_trips(tmp_path):
    db = _fresh_db(tmp_path)
    uid = db.create_user("t@t.com", "x", "tok")

    assert db.list_broadcast_rules(uid) == []

    created = db.create_broadcast_rule(uid, True, 111, "Chan A", 70, False, ["scanner", "chartink"])
    assert created["target_chat_id"] == 111
    assert created["sources"] == ["scanner", "chartink"]
    assert created["fo_only"] is False or created["fo_only"] == 0

    rules = db.list_broadcast_rules(uid)
    assert len(rules) == 1

    updated = db.update_broadcast_rule(uid, created["id"], True, 222, "Chan B", 80, True, None)
    assert updated["target_chat_id"] == 222
    assert updated["sources"] is None  # None/empty means unrestricted

    assert db.delete_broadcast_rule(uid, created["id"]) is True
    assert db.list_broadcast_rules(uid) == []
    assert db.delete_broadcast_rule(uid, created["id"]) is False  # already gone


def test_existing_single_row_setting_is_migrated_into_one_rule(tmp_path):
    # Simulates an install that had the old single-row telegram_broadcast_settings populated
    # before this feature existed -- init_db() must turn it into an equivalent, unrestricted rule
    # exactly once, so nobody's current broadcast config silently changes.
    db = _fresh_db(tmp_path)
    uid = db.create_user("t@t.com", "x", "tok")
    conn = db._conn()
    conn.execute(
        "INSERT INTO telegram_broadcast_settings (user_id, enabled, target_chat_id, target_chat_title, min_score, fo_only) "
        "VALUES (?, 1, 999, 'Old Chan', 65, 0)",
        (uid,),
    )
    conn.commit()
    conn.close()

    db.init_db()  # re-run the migration path

    rules = db.list_broadcast_rules(uid)
    assert len(rules) == 1
    assert rules[0]["target_chat_id"] == 999
    assert rules[0]["sources"] is None

    db.init_db()  # idempotent -- must not duplicate the migrated rule
    assert len(db.list_broadcast_rules(uid)) == 1
