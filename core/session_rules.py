"""Session rules: phase choice, pre-bet room and config checks.

Pure functions with explicit parameters. No state, no I/O. The CLI calls
them from its bet loop, its backtest loop and its start-up check.
"""

import math
from typing import Optional

PHASE_BASE = "base"
PHASE_RECOVER = "recover"
PHASE_PRESS = "press"

HALF_SAT = 5e-9

_OFF_VALUES = ("", "off", "none", "0", "disable", "false", "no", "n")
_ON_VALUES = ("on", "true", "yes", "y", "1")
PHASE_STRATEGY_KEYS = ("1", "2", "3", "4", "5", "6", "7")
_RAISE_ON_LOSS = ("2", "6", "7")
_RAISE_ON_WIN = ("3", "5")


def _is_off(raw) -> bool:
    return raw is None or (isinstance(raw, str) and raw.strip().lower() in _OFF_VALUES)


def _finite(name: str, raw) -> float:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {raw!r}")
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    return number


def parse_amount(name: str, raw) -> Optional[float]:
    """Parse a money amount. Off values and zero return None; nan, inf and negatives are refused."""
    if _is_off(raw):
        return None
    number = _finite(name, raw)
    if number < 0:
        raise ValueError(f"{name} must not be negative, got {raw!r}")
    return number or None


def parse_flag(name: str, raw) -> bool:
    """Parse an on/off switch."""
    if isinstance(raw, bool):
        return raw
    if _is_off(raw):
        return False
    if str(raw).strip().lower() in _ON_VALUES:
        return True
    raise ValueError(f"{name} must be on or off, got {raw!r}")


def parse_multiple(name: str, raw) -> float:
    """Parse a phase's first bet as a multiple of base_bet (1 or more)."""
    number = _finite(name, raw)
    if number < 1:
        raise ValueError(f"{name} must be 1 or more (a multiple of base_bet), got {raw!r}")
    return number


def parse_strategy_key(name: str, raw) -> str:
    """Parse the strategy a phase bets with. Rule-Based is not available to a phase."""
    key = str(raw).strip()
    if key not in PHASE_STRATEGY_KEYS:
        raise ValueError(f"{name} must be a strategy key 1-7, got {raw!r}")
    return key


def next_phase(phase: str, profit: float, recover_at: Optional[float],
               press_at: Optional[float], press_floor: float = 0.0) -> str:
    """Phase for the next bet, from the session profit so far.

    Recover holds until the session is back to break-even. Press holds while
    profit stays above press_floor. Otherwise the thresholds decide.
    """
    if phase == PHASE_RECOVER and recover_at is not None and profit < -HALF_SAT:
        return PHASE_RECOVER
    if phase == PHASE_PRESS and press_at is not None and profit > (press_floor or 0.0) + HALF_SAT:
        return PHASE_PRESS
    if recover_at is not None and profit <= -recover_at + HALF_SAT:
        return PHASE_RECOVER
    if press_at is not None and profit >= press_at - HALF_SAT:
        return PHASE_PRESS
    return PHASE_BASE


def loss_room(profit: float, max_loss: Optional[float]) -> float:
    """Largest bet that keeps the session loss within max_loss. No usable cap means no room."""
    if max_loss is None or not math.isfinite(max_loss):
        return 0.0
    return max(profit + abs(max_loss), 0.0)


def floor_room(balance: float, floor: Optional[float]) -> float:
    """Largest bet that keeps the balance at or above the floor. No floor means no limit."""
    if floor is None:
        return math.inf
    if not math.isfinite(floor):
        return 0.0
    return max(balance - floor, 0.0)


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _usable(value) -> bool:
    number = _number(value)
    return number is not None and number > 0


def _progression_errors(name: str, key: str, loss_mult, win_mult) -> list:
    """Error when a phase strategy needs a multiplier above 1 to raise its bet and has none."""
    if key in _RAISE_ON_LOSS and not (_usable(loss_mult) and loss_mult > 1):
        return [f"{name} {key} raises the bet after a loss and needs loss_mult above 1 (now {loss_mult})"]
    if key in _RAISE_ON_WIN and not (_usable(win_mult) and win_mult > 1):
        return [f"{name} {key} raises the bet after a win and needs win_mult above 1 (now {win_mult})"]
    return []


def config_errors(cfg: dict) -> list:
    """Reasons the session rules in cfg cannot work, each naming the setting to fix.

    cfg holds base_bet, loss_mult, win_mult, max_profit, max_loss, stop_on_balance,
    recurring, hard_cap, restart_after_loss, balance_goal and the hybrid_* values.
    An empty list means the rules are usable, or that none of them is switched on.
    """
    errors = []
    base_bet = _number(cfg.get("base_bet")) or 0.0
    max_loss = abs(_number(cfg.get("max_loss")) or 0.0)
    max_profit = cfg.get("max_profit")
    floor = cfg.get("stop_on_balance")
    goal = cfg.get("balance_goal")
    recover_at = cfg.get("hybrid_recover_at")
    press_at = cfg.get("hybrid_press_at")
    phases_on = recover_at is not None or press_at is not None
    cap_on = bool(cfg.get("hard_cap")) or phases_on
    cap_ok = max_loss > 0

    if cap_on and not cap_ok:
        owner = "hybrid phases need" if phases_on else "hard_cap needs"
        errors.append(f"{owner} max_loss: set max_loss to the most one session may lose")
    if cap_on and cap_ok and base_bet > max_loss + HALF_SAT:
        errors.append(f"base_bet {base_bet:.8f} is larger than max_loss {max_loss:.8f}: "
                      "the first bet does not fit")

    if cfg.get("restart_after_loss"):
        if not cfg.get("recurring"):
            errors.append("restart_after_loss needs recurring=on")
        if not cap_on:
            errors.append("restart_after_loss needs hard_cap=on")
        if not _usable(floor):
            errors.append("restart_after_loss needs stop_on_balance: "
                          "set the balance at which the bot must stop")

    if goal is not None:
        if not _usable(goal):
            errors.append(f"balance_goal must be a positive number (now {goal})")
        elif _usable(floor) and goal <= floor:
            errors.append(f"balance_goal {goal:.8f} must be above stop_on_balance {floor:.8f}")

    if recover_at is not None:
        key = str(cfg.get("hybrid_recover_strategy"))
        multiple = cfg.get("hybrid_recover_bet")
        if not _usable(recover_at):
            errors.append(f"hybrid_recover_at must be a positive number (now {recover_at})")
        elif cap_ok and recover_at >= max_loss:
            errors.append(f"hybrid_recover_at {recover_at:.8f} must be below max_loss {max_loss:.8f}")
        elif cap_ok and _usable(multiple) and base_bet * multiple > max_loss - recover_at + HALF_SAT:
            errors.append(f"hybrid_recover_bet: the first recovery bet {base_bet * multiple:.8f} "
                          f"does not fit in the {max_loss - recover_at:.8f} left before max_loss")
        if key not in PHASE_STRATEGY_KEYS:
            errors.append(f"hybrid_recover_strategy must be a strategy key 1-7 (now {key})")
        else:
            errors += _progression_errors("hybrid_recover_strategy", key,
                                          cfg.get("loss_mult"), cfg.get("win_mult"))
        if not (_usable(multiple) and multiple >= 1):
            errors.append(f"hybrid_recover_bet must be 1 or more (now {multiple})")

    if press_at is not None:
        key = str(cfg.get("hybrid_press_strategy"))
        multiple = cfg.get("hybrid_press_bet")
        raw_floor = cfg.get("hybrid_press_floor")
        press_floor = 0.0 if raw_floor is None else _number(raw_floor)
        if press_floor is None or press_floor < 0:
            errors.append(f"hybrid_press_floor must be zero or a positive number (now {raw_floor})")
        if not _usable(press_at):
            errors.append(f"hybrid_press_at must be a positive number (now {press_at})")
        else:
            if press_floor is not None and press_floor >= press_at:
                errors.append(f"hybrid_press_floor {press_floor:.8f} must be below "
                              f"hybrid_press_at {press_at:.8f}")
            if _usable(max_profit) and press_at >= max_profit:
                errors.append(f"hybrid_press_at {press_at:.8f} must be below max_profit {max_profit:.8f}")
            if cap_ok and _usable(multiple) and base_bet * multiple > max_loss + press_at + HALF_SAT:
                errors.append(f"hybrid_press_bet: the first press bet {base_bet * multiple:.8f} "
                              f"does not fit in the {max_loss + press_at:.8f} left before max_loss")
        if key not in PHASE_STRATEGY_KEYS:
            errors.append(f"hybrid_press_strategy must be a strategy key 1-7 (now {key})")
        else:
            errors += _progression_errors("hybrid_press_strategy", key,
                                          cfg.get("loss_mult"), cfg.get("win_mult"))
        if not (_usable(multiple) and multiple >= 1):
            errors.append(f"hybrid_press_bet must be 1 or more (now {multiple})")

    return errors
