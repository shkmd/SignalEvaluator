"""Registry of F&O Scanner strategies -- named, independently-toggleable condition sets.

A "strategy" is a set of CE/PE qualification rules (an evaluate_direction function with the
same shape as qualification.evaluate_direction: (direction, snapshot, futures_eligible) ->
{qualified, conditions, failed_conditions}). Every strategy is evaluated against the same
market-data snapshot (qualification.gather_snapshot) -- only the condition logic differs.

Today there is exactly one strategy, the original CE/PE condition set (range expansion vs the
last 7 sessions, daily/weekly/monthly direction, SMA20-vs-SMA50, 15m confirmation, volume/price
floors). New strategies get added here as they're defined; each one then shows up automatically
as its own card on the Strategy tab.
"""
from app import qualification

STRATEGIES = {
    "range_expansion_v1": {
        "id": "range_expansion_v1",
        "name": "Range Expansion CE/PE",
        "description": (
            "The original screener: current day's range beats each of the last 7 sessions, "
            "daily/weekly/monthly candle direction agrees, SMA20 vs SMA50 confirms the trend, "
            "the latest completed 15-minute candle confirms, plus volume (>20,000) and price "
            "(>100) floors and a futures-eligibility check. 16 conditions total, split evenly "
            "between the CE and PE side."
        ),
        "evaluate_direction": qualification.evaluate_direction,
    },
}

DEFAULT_STRATEGY_ID = "range_expansion_v1"


def list_strategies() -> list:
    return [
        {"id": s["id"], "name": s["name"], "description": s["description"]}
        for s in STRATEGIES.values()
    ]


def get_strategy(strategy_id: str) -> dict:
    strategy = STRATEGIES.get(strategy_id)
    if not strategy:
        raise ValueError(f"Unknown strategy '{strategy_id}'.")
    return strategy


def is_valid_strategy_id(strategy_id: str) -> bool:
    return strategy_id in STRATEGIES
