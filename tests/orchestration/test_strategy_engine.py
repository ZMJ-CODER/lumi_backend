"""策略引擎仅裁决已验证画像和候选，不能退化为关键词路由。"""

from __future__ import annotations

import asyncio

from app.agents.orchestration.planning.strategy_engine import StrategyCandidate, StrategyEngine
from app.agents.orchestration.planning.task_profiles import TaskProfile


def _profile(**overrides):
    return TaskProfile.model_validate({
        "goal": "RETRIEVE",
        "required_sources": ["PUBLIC_WEB"],
        "complexity": "ATOMIC",
        "safety_level": "READ_ONLY",
        "confidence": 0.9,
        **overrides,
    })


def _write(directory, name: str, content: str) -> None:
    (directory / name).write_text(content, encoding="utf-8")


def test_selects_among_capability_candidates_without_request_text(tmp_path):
    _write(tmp_path, "policy.yaml", """
version: 1
id: economy-retrieval
priority: 100
applies_to: {goals: [RETRIEVE]}
selection:
  mode: economy
  weights: {capability_specificity: 0, declared_capability_bonus: 0, reliability: 0, cost: 1}
""")
    engine = StrategyEngine(tmp_path)
    snapshot = asyncio.run(engine.snapshot())
    selection = engine.select_implementation(
        [
            StrategyCandidate(value="premium", name="premium", capability_specificity=99, declared_capability=True, cost=9),
            StrategyCandidate(value="economy", name="economy", capability_specificity=1, declared_capability=False, cost=1),
        ],
        profile=_profile(),
        snapshot=snapshot,
    )

    assert selection is not None
    assert selection.candidate.value == "economy"
    assert selection.policy_id == "economy-retrieval"


def test_unload_activates_safe_fallback_and_load_restores_policy(tmp_path):
    _write(tmp_path, "policy.yaml", """
version: 1
id: temporary
priority: 10
selection: {mode: quality}
""")
    engine = StrategyEngine(tmp_path)
    assert asyncio.run(engine.snapshot()).fallback_active is False
    unloaded = asyncio.run(engine.unload("temporary"))
    assert unloaded["fallback_active"] is True
    restored = asyncio.run(engine.load("temporary"))
    assert restored["fallback_active"] is False


def test_invalid_policy_is_rejected_and_never_prevents_safe_fallback(tmp_path):
    _write(tmp_path, "invalid.yaml", """
version: 1
id: invalid
selection:
  mode: balanced
  executable: "__import__('os').system('bad')"
""")
    engine = StrategyEngine(tmp_path)
    inspection = asyncio.run(engine.inspect())

    assert inspection["fallback_active"] is True
    assert inspection["errors"]
    assert "executable" in inspection["errors"][0]["error"]


def test_failure_and_interaction_are_profile_scoped(tmp_path):
    _write(tmp_path, "write.yaml", """
version: 1
id: safe-write
priority: 100
applies_to:
  safety_levels: [SAFE_WRITE]
interaction:
  confidence_below: 0.8
  safety_at_or_above: SAFE_WRITE
failure:
  actions: {transient: abort}
""")
    engine = StrategyEngine(tmp_path)
    snapshot = asyncio.run(engine.snapshot())
    write_profile = _profile(goal="EXECUTE", safety_level="SAFE_WRITE", confidence=0.5)
    read_profile = _profile()

    assert engine.should_clarify(profile=write_profile, has_prior_context=False, snapshot=snapshot) == (True, "safe-write")
    assert engine.failure_action(category="transient", profile=write_profile, snapshot=snapshot) == "abort"
    assert engine.failure_action(category="transient", profile=read_profile, snapshot=snapshot) == "retry"


def test_persisted_snapshot_survives_later_unload(tmp_path):
    _write(tmp_path, "policy.yaml", """
version: 1
id: quality
priority: 10
selection: {mode: quality}
""")
    engine = StrategyEngine(tmp_path)
    original = asyncio.run(engine.snapshot())
    payload = engine.snapshot_payload(original)
    asyncio.run(engine.unload("quality"))

    restored = engine.snapshot_from_payload(payload)
    assert restored is not None
    assert restored.version == original.version
    assert restored.policies[0].id == "quality"
