"""The model x role arm grid: ``<role>:<model>`` for 3 roles x 4 models (12 arms).

Generator arms return a ``HypothesisGenerator`` (``make_generator``) whose plan feeds the built-in
EIG selector (``make_arm_selector`` returns it under the grid arm id). Reviewer and agent arms are
``Selector``s over the provider's client. Cells that cannot work are listed in ``models.json``
``grid.skipped`` with the reason and are never faked.
"""

from __future__ import annotations

from typing import Any, Final

from research.baselines import frontier, meta, selectors
from research.baselines.config import load_models
from research.schemas.interfaces import HypothesisGenerator, Selector

ROLES: Final = ("generator", "reviewer", "agent")


def models() -> dict[str, str]:
    return dict(load_models()["grid"]["models"])


def skipped() -> dict[str, str]:
    return dict(load_models()["grid"]["skipped"])


def grid_arms() -> tuple[str, ...]:
    return tuple(f"{r}:{m}" for r in ROLES for m in models() if f"{r}:{m}" not in skipped())


def canonical(arm_id: str) -> str:
    return str(load_models()["grid"]["aliases"].get(arm_id, arm_id))


def parse_arm(arm_id: str) -> tuple[str, str]:
    arm = canonical(arm_id)
    role, _, model = arm.partition(":")
    if role not in ROLES or model not in models():
        raise ValueError(f"unknown grid arm: {arm_id}")
    if arm in skipped():
        raise ValueError(f"grid arm {arm} is skipped: {skipped()[arm]}")
    return role, model


def make_generator(arm_id: str, **kwargs: Any) -> HypothesisGenerator:
    """Generator-role arm. ``kwargs``: ``api_key``, ``ledger``, ``transport`` and, for OpenAI,
    the ``frontier.make_client`` options. Meta models use the production adapter."""
    role, model = parse_arm(arm_id)
    if role != "generator":
        raise ValueError(f"{arm_id} is not a generator arm")
    if frontier.provider_of(model) == "meta":
        gen = meta.meta_generator(api_key=kwargs.pop("api_key", None),
                                  ledger=kwargs.pop("ledger", None),
                                  transport=kwargs.pop("transport", None), model_id=model)
        assert isinstance(gen, meta.MetaGenerator)
        for name in ("phase", "mode", "estimated_job_usd", "case_id"):
            if name in kwargs:
                setattr(gen, name, kwargs.pop(name))
        gen.generator_id = f"generator:{model}"
        return gen
    client = frontier.make_client(model, max_attempts=2, **kwargs)
    assert isinstance(client, frontier.ResponsesClient)
    return frontier.ResponsesGenerator(client)


def make_arm_selector(arm_id: str, *, seed: int, **kwargs: Any) -> Selector:
    """Selector for a grid arm (generator role: built-in EIG under the grid arm id)."""
    role, model = parse_arm(arm_id)
    name = f"{role}:{model}"
    if role == "generator":
        return selectors.make_selector(name, seed=seed)
    if role == "reviewer":
        return frontier.FrontierReviewer(frontier.make_client(model, **kwargs), name)
    return frontier.FrontierAgent(frontier.make_client(model, **kwargs), name)
