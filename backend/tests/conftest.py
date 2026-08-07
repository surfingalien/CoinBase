import os
import sys

import pytest

# Make `app` importable regardless of where pytest is invoked from.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _reset_metabolism_state():
    """app.metabolism keeps module-level state (the pending-cost buffer and
    the cached survival tier). Without a reset between tests, a test that
    parks the tier at 'critical' leaks entry damping/halts into every later
    test file — a flaky suite that hides real regressions."""
    from app import metabolism

    metabolism.reset_state()
    yield
    metabolism.reset_state()
