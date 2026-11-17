"""Test environment for the wolfbet suite.

web.config refuses the shipped secret and password, so any test that imports
the web package needs valid values in place before collection.
"""

import os

import pytest

os.environ.setdefault("WOLFBET_WEB_SECRET_KEY", "test-secret-key-for-the-suite-12345678")
os.environ.setdefault("WOLFBET_WEB_ADMIN_PASS", "test-admin-pass-1234")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "bench: slow performance measurement, runs only with -m bench")


def pytest_collection_modifyitems(config, items):
    """Benchmarks take minutes, so they run only when `-m` asks for them."""
    if "bench" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="benchmark: run with -m bench")
    for item in items:
        if "bench" in item.keywords:
            item.add_marker(skip)
