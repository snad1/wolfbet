"""Re-export from core.strategy — single source of truth.

Note: STRATEGIES is now {key: (name, desc)} tuples.
Use STRATEGY_NAMES for name-only access.
"""

from core.strategy import (  # noqa: F401
    STRATEGIES,
    STRATEGY_NAMES,
    STRATEGY_BY_NAME,
    RULE_ACTIONS,
    RULE_ACTION_LABELS,
    SEQ_MODES,
    StrategyRule,
    describe_rule,
    cmp,
    load_rules_from_text,
)
