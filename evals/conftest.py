"""Real-model evals. They call Claude, so they are not in `testpaths`: `uv run pytest` skips
this directory, and `uv run pytest evals -s` runs it.

Without ANTHROPIC_API_KEY every test here is skipped.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _needs_anthropic_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY is not set; the evals call Claude")
