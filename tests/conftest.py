"""Test environment for the wolfbet suite: the bench marker and its opt-in."""

import pytest


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
