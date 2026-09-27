"""Shared test fixtures: keep the suite fast and hermetic."""
import os

import pytest


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """LLM pacing must never slow down unit tests."""
    monkeypatch.setenv("VLAB_LLM_MIN_INTERVAL", "0")
    # reset throttle clock so ordering between tests can't inject waits
    import vlab.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_last_call_ts", 0.0)
