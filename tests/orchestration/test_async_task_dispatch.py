"""Async dispatch contract tests that do not need Redis/Postgres."""

from celery_app import celery_app


def test_celery_uses_isolated_named_queues_and_at_least_once_delivery():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.broker_transport_options["visibility_timeout"] > celery_app.conf.task_time_limit
    assert {queue.name for queue in celery_app.conf.task_queues} == {
        "durable", "best_effort", "maintenance"
    }


def test_celery_routes_document_and_memory_work_to_different_queues():
    routes = celery_app.conf.task_routes
    assert routes["celery_app.tasks.process_document"]["queue"] == "durable"
    assert routes["celery_app.tasks.extract_memories"]["queue"] == "best_effort"
    assert routes["celery_app.tasks.recover_stale_documents"]["queue"] == "maintenance"
