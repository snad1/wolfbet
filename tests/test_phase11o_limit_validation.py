"""Limits reject nan and inf in the CLI and the Telegram commands.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11o_limit_validation.py -q
"""

import asyncio
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

import wolfbet as wb  # noqa: E402
from tg import handlers as tg_handlers  # noqa: E402


class TestCliMutation:
    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_non_finite_limits_rejected(self, value):
        with pytest.raises(ValueError, match="finite"):
            wb._apply_mutation({"max_loss": value})

    @pytest.mark.parametrize("field", ["max_profit", "stop_on_balance", "profit_threshold"])
    def test_every_limit_field_is_guarded(self, field):
        with pytest.raises(ValueError, match="finite"):
            wb._apply_mutation({field: "nan"})

    def test_normal_values_still_apply(self):
        wb._apply_mutation({"max_loss": "5"})
        assert wb.state.max_loss == 5.0


class TestTelegramSet:
    @pytest.mark.parametrize("value", ["nan", "inf"])
    def test_set_rejects_non_finite(self, value):
        sent = []

        async def reply_text(text, **kw):
            sent.append(text)

        upd = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(reply_text=reply_text),
        )
        store = {}
        original_load, original_save = tg_handlers.load_user_config, tg_handlers.save_user_config
        tg_handlers.load_user_config = lambda uid: dict(store)
        tg_handlers.save_user_config = lambda uid, cfg: store.update(cfg)
        try:
            asyncio.run(tg_handlers.cmd_set(upd, types.SimpleNamespace(args=["maxloss", value])))
        finally:
            tg_handlers.load_user_config, tg_handlers.save_user_config = original_load, original_save
        assert "max_loss" not in store
        assert sent and "Invalid value" in sent[-1]

    def test_set_accepts_a_number(self):
        assert tg_handlers._finite("12.5") == 12.5
        with pytest.raises(ValueError):
            tg_handlers._finite("nan")
