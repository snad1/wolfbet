"""Strategy definitions, rule engine, and rule parsing.

Single source of truth for strategies and rules.
Both the CLI bot and Telegram bot import from here.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shared.core.strategy import (  # noqa: E402
    make_lookups,
    RULE_ACTIONS, RULE_ACTION_LABELS,
    SEQ_MODES,
    StrategyRule,
    describe_rule, cmp, load_rules_from_text,
)

STRATEGIES = {
    "1": ("Flat Bet",          "Same amount every single bet — low risk, low variance"),
    "2": ("Martingale",        "Double bet on loss, reset to base on win — classic high-risk"),
    "3": ("Anti-Martingale",   "Double bet on win, reset to base on loss — ride hot streaks"),
    "4": ("D'Alembert",        "+1 unit on loss, -1 on win — gentler progression"),
    "5": ("Paroli (3-step)",   "Double on win up to 3x then reset — safer streak play"),
    "6": ("WolfRider",         "Multiply on loss + switch over/under on bet 2/6/9"),
    "7": ("Delay Martingale",  "Flat for N losses then multiply — delays escalation"),
    "8": ("Rule-Based",        "Build custom conditions & actions — full control"),
}

STRATEGY_NAMES, STRATEGY_BY_NAME = make_lookups(STRATEGIES)
