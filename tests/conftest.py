"""Test environment for the wolfbet suite.

web.config refuses the shipped secret and password, so any test that imports
the web package needs valid values in place before collection.
"""

import os

os.environ.setdefault("WOLFBET_WEB_SECRET_KEY", "test-secret-key-for-the-suite-12345678")
os.environ.setdefault("WOLFBET_WEB_ADMIN_PASS", "test-admin-pass-1234")
