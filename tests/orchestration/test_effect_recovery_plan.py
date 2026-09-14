"""恢复计划契约回归（方案 §5.4：副作用以 Effect Journal 为准）。"""

from __future__ import annotations

from lumi_contracts.persistence.effect_recovery import (
    RecoveryAction,
    plan_recovery,
)

KEYS = {"s1": "k1", "s2": "k2", "s3": "k3", "s4": "k4"}


def test_confirmed_effect_is_skipped_not_rerun():
    plan = plan_recovery(["s1"], {"k1": "confirmed"}, idempotency_keys=KEYS)
    assert plan.skip_step_ids == ("s1",)
    assert not plan.requires_human and plan.resume_allowed
    assert plan.steps[0].reason_code == "EFFECT_ALREADY_COMMITTED"


def test_uncertain_effect_pauses_for_human():
    plan = plan_recovery(["s2"], {"k2": "uncertain"}, idempotency_keys=KEYS)
    assert plan.paused_step_ids == ("s2",)
    assert plan.requires_human is True
    assert plan.resume_allowed is False, "有不确定副作用时整体不得继续自动恢复"
    assert plan.steps[0].action == RecoveryAction.PAUSE_FOR_HUMAN.value


def test_in_flight_intent_is_treated_as_uncertain():
    plan = plan_recovery(["s3"], {"k3": "intent"}, idempotency_keys=KEYS)
    assert plan.steps[0].action == RecoveryAction.PAUSE_FOR_HUMAN.value
    assert plan.steps[0].reason_code == "EFFECT_IN_FLIGHT"


def test_step_without_journal_record_is_rescheduled():
    plan = plan_recovery(["s4"], {}, idempotency_keys=KEYS)
    assert plan.reschedule_step_ids == ("s4",)
    assert plan.steps[0].reason_code == "EFFECT_NOT_STARTED"
    assert plan.resume_allowed


def test_mixed_plan_keeps_order_and_blocks_resume():
    plan = plan_recovery(
        ["s1", "s2", "s3", "s4"],
        {"k1": "confirmed", "k2": "uncertain", "k3": "intent"},
        idempotency_keys=KEYS,
    )
    assert [step.step_id for step in plan.steps] == ["s1", "s2", "s3", "s4"]
    assert plan.skip_step_ids == ("s1",)
    assert plan.paused_step_ids == ("s2", "s3")
    assert plan.reschedule_step_ids == ("s4",)
    assert plan.resume_allowed is False
    assert set(plan.reason_codes) == {"EFFECT_ALREADY_COMMITTED", "EFFECT_UNCERTAIN", "EFFECT_IN_FLIGHT", "EFFECT_NOT_STARTED"}


def test_missing_idempotency_key_falls_back_to_resume():
    """没有幂等键（非副作用步骤）→ 视为未执行，可重排。"""
    plan = plan_recovery(["s9"], {"k9": "confirmed"}, idempotency_keys={"s9": ""})
    assert plan.reschedule_step_ids == ("s9",)


def test_empty_and_blank_inputs_are_safe():
    assert plan_recovery([], {}, idempotency_keys={}).steps == ()
    plan = plan_recovery(["", "s1"], {}, idempotency_keys={"s1": "k1"})
    assert [step.step_id for step in plan.steps] == ["s1"]


def test_plan_is_serialisable_for_snapshots():
    payload = plan_recovery(["s1", "s2"], {"k1": "confirmed", "k2": "uncertain"}, idempotency_keys=KEYS).as_dict()
    assert payload["resume_allowed"] is False
    assert payload["skip_step_ids"] == ["s1"] and payload["paused_step_ids"] == ["s2"]
    assert isinstance(payload["steps"], list) and len(payload["steps"]) == 2
