"""Temporal 进程内 Worker 注册契约。"""

from __future__ import annotations


def test_inprocess_runtime_registers_all_temporal_queues(monkeypatch):
    """API 进程模式不能遗漏逻辑计划队列。"""
    from app.agents.orchestration.temporal import runtime
    from app.agents.orchestration.temporal import worker as worker_module

    expected = (object(), object(), object())
    factories = (
        "build_worker",
        "build_logical_read_worker",
        "build_logical_effects_worker",
    )
    for factory, value in zip(factories, expected, strict=True):
        monkeypatch.setattr(worker_module, factory, lambda _client, value=value: value)

    assert runtime.build_inprocess_workers(object()) == expected
