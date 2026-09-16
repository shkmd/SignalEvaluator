"""Auto-graduates a channel from paper to REAL live trading once it proves reliable enough,
with no manual click required per channel -- the user explicitly opted into this fully
automatic behavior via the master auto_graduate_enabled toggle in Broker Setup (off by
default). Also auto-demotes a graduated channel back to paper if its win rate later drops,
so the automation protects in both directions, not just one.

Deliberately conservative: a channel only graduates once it has enough closed paper trades
AND a high enough win rate (both configurable, both stricter defaults than the position-sizing
minimums) AND the user has an actually-connected broker right now. Every graduation and
demotion sends a Telegram alert -- this is real money moving without a per-trade click, so it
is never silent.
"""
from app import alerts, db
from app.brokers import get_connected_adapter

DEMOTE_WIN_RATE_FLOOR = 50.0


def check_and_graduate(user_id: int, channel: str) -> None:
    """Call after a channel's paper-trade reliability numbers change (i.e. right after one of
    its paper orders closes). Cheap no-op when auto-graduate is off or the channel doesn't yet
    qualify either way."""
    settings = db.get_auto_trade_settings(user_id)
    if not settings.get("auto_graduate_enabled"):
        return

    reliability = db.channel_reliability(user_id, channel)
    trades = reliability["trades_closed"]
    win_rate = reliability["win_rate"]
    if win_rate is None:
        return

    current = db.get_channel_graduation(user_id, channel)
    min_trades = settings.get("auto_graduate_min_trades", 15)
    min_win_rate = settings.get("auto_graduate_min_win_rate", 75)

    if current["status"] == "paper":
        if trades < min_trades or win_rate < min_win_rate:
            return
        if not get_connected_adapter(user_id):
            print(f"[graduation] user {user_id}: '{channel}' qualifies ({win_rate}% over {trades} trades) "
                  f"but no broker is connected -- staying on paper until reconnected.")
            return
        db.set_channel_graduation(user_id, channel, "live", win_rate, trades)
        alerts.alert_graduation(user_id, channel, win_rate, trades)
        print(f"[graduation] user {user_id}: '{channel}' graduated to LIVE ({win_rate}% over {trades} trades).")

    elif current["status"] == "live":
        if trades < min_trades or win_rate >= DEMOTE_WIN_RATE_FLOOR:
            return
        db.set_channel_graduation(user_id, channel, "paper", win_rate, trades)
        alerts.alert_demotion(user_id, channel, win_rate, trades)
        print(f"[graduation] user {user_id}: '{channel}' demoted back to paper ({win_rate}% over {trades} trades).")


def is_graduated(user_id: int, channel: str) -> bool:
    return db.get_channel_graduation(user_id, channel)["status"] == "live"
