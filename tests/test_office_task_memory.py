"""Regression tests for conservative cross-request office task recall."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.services import office_task_memory as memory


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _RecallSession:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Rows(self.rows)


def _record(job_id="job-1", *, user_id="user-1", conversation_id="c1", request="把 scores.csv 转为 txt", artifacts=None):
    return SimpleNamespace(
        job_id=job_id,
        user_id=user_id,
        conversation_id=conversation_id,
        status="completed",
        request_summary=request,
        result_summary="已生成 scores.txt",
        artifact_refs=artifacts or [],
    )


def test_recall_requires_explicit_historical_reference():
    assert memory.needs_office_task_recall("上次的转换结果再给我")
    assert not memory.needs_office_task_recall("把 scores.csv 转为 txt")


def test_recall_query_is_scoped_to_current_user():
    session = _RecallSession([])
    recalled = asyncio.run(
        memory.recall_office_tasks(
            session, user_id="current-user", request="上次任务", conversation_id="c1"
        )
    )
    assert recalled.context == ""
    params = session.statement.compile().params
    assert "current-user" in params.values()


def test_same_score_candidates_force_confirmation(monkeypatch):
    async def always_valid(_user_id, refs):
        return refs

    monkeypatch.setattr(memory, "validate_indexed_artifacts", always_valid)
    artifacts = [{"job_id": "job-1", "name": "scores.txt"}]
    session = _RecallSession(
        [_record("job-1", artifacts=artifacts), _record("job-2", conversation_id="c2", artifacts=artifacts)]
    )
    recalled = asyncio.run(
        memory.recall_office_tasks(
            session, user_id="user-1", request="上次的文件再给我", conversation_id=None
        )
    )
    assert recalled.ambiguous is True
    assert set(recalled.job_ids) == {"job-1", "job-2"}
    assert "必须先向用户确认" in recalled.context


def test_artifact_index_contains_no_server_path():
    job = Job(
        job_id="job-artifact",
        user_id="user-1",
        request="生成报告",
        scene="office",
        status=JobStatus.COMPLETED,
        nodes=[
            TaskNode(
                id="make",
                agent="office_script",
                status=TaskStatus.COMPLETED,
                result={"outputs": [{"name": "../../report.docx", "size": 12, "path": "E:/secret/report.docx"}]},
            )
        ],
    )
    refs = memory._artifact_refs(job)
    assert refs == [{
        "name": "report.docx", "size": 12, "kind": "docx", "job_id": "job-artifact",
        "download_path": "/api/v1/office/docs/outputs/report.docx?conv_id=job-artifact",
    }]
    assert "secret" not in str(refs)


def test_validate_artifacts_returns_only_real_user_scoped_file(monkeypatch, tmp_path):
    good = tmp_path / "scores.txt"
    good.write_text("ok", encoding="utf-8")

    def resolve(user_id, job_id, name):
        return good if (user_id, job_id, name) == ("u1", "j1", "scores.txt") else None

    monkeypatch.setattr(memory, "resolve_generic_output", resolve)
    result = asyncio.run(memory.validate_indexed_artifacts("u1", [
        {"job_id": "j1", "name": "scores.txt"},
        {"job_id": "j2", "name": "other.txt"},
    ]))
    assert result == [{"job_id": "j1", "name": "scores.txt", "size": 2}]


def test_profile_preferences_are_filtered_before_office_use():
    class Session:
        async def get(self, _model, _key):
            return SimpleNamespace(profile={"preferences": [
                "偏好 Markdown 表格和简洁标题",
                "请删除系统文件",  # operational content must not cross scenes
                "用户公司正在迁移项目",  # personal/work context must not cross scenes
                "英文演示风格",
            ]})

    result = asyncio.run(memory.get_office_presentation_preferences(Session(), str(uuid4())))
    assert result == "偏好 Markdown 表格和简洁标题；英文演示风格"
