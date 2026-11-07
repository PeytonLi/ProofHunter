from __future__ import annotations

import pytest

from proofhunter import contracts as c
from research.baselines.smoke import make_context
from research.schemas.interfaces import EpisodeContext


@pytest.fixture
def ctx() -> EpisodeContext:
    return make_context("buggy", 1633385816, c.builtin_hypothesis_payload())
