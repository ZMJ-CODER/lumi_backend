"""第二阶段接线回归：渲染出口经契约投影，工作区正文以可读分段进模型上下文。

这组用例锁定"业务数据与模型展示解耦"的实际效果：

* 工作区读取结果（sections 形态）渲染成 ``[文件 · 位置]`` + 正文分段，
  **不再是** JSON 字符串（JSON 会把正文埋进 ``meta``/``limits`` 噪音里）；
* 未读完时给出 cursor 继续提示；
* 凭据/绝对路径不进模型文本；
* 契约投影失败时不得丢结果（回退旧渲染）。
"""

from __future__ import annotations

from app.agents.skills.base import SkillResult
from app.services.tool_output_pipeline import normalize_skill_result, render_for_model


def _navigator_result(*, has_more: bool = False) -> SkillResult:
    return SkillResult(
        success=True,
        content_type="structured",
        data={
            "status": "partial" if has_more else "ok",
            "action": "read",
            "summary": "已读取 答辩.pptx（1/1 段）",
            "data": {
                "path": "答辩.pptx",
                "sections": [
                    {"source": "答辩.pptx", "location": "slide-1", "title": "第1页", "text": "第一页正文"},
                    {"source": "答辩.pptx", "location": "slide-2", "title": "第2页", "text": "第二页正文"},
                ],
            },
            "has_more": has_more,
            "cursor": "cursor-next" if has_more else None,
            "meta": {"workspace_id": "ws1", "workspace_version": 3},
        },
        metadata={"tool": "workspace_navigator"},
    )


def test_workspace_sections_render_as_readable_text_not_json():
    rendered = render_for_model(normalize_skill_result(_navigator_result()), max_chars=4000)
    assert "第一页正文" in rendered
    assert "第二页正文" in rendered
    assert "slide-1" in rendered and "slide-2" in rendered
    # 不应退化成 JSON 信封（正文埋在 meta/limits 里的那种形态）
    assert not rendered.lstrip().startswith("{")
    assert "workspace_version" not in rendered


def test_unfinished_read_renders_cursor_hint():
    rendered = render_for_model(normalize_skill_result(_navigator_result(has_more=True)), max_chars=4000)
    assert "cursor" in rendered
    assert "cursor-next" in rendered


def test_absolute_path_and_secrets_never_reach_model_text():
    result = SkillResult(
        success=True,
        content_type="structured",
        data={
            "status": "ok",
            "summary": "已读取 src/main.py",
            "absolute_path": "/Users/someone/private/src/main.py",
            "cwd": "C:\\Users\\someone\\project",
            "secret_token": "sk-live-abcdef",
            "sections": [{"source": "src/main.py", "location": "line-1", "text": "print('hi')"}],
        },
    )
    rendered = render_for_model(normalize_skill_result(result), max_chars=4000)
    assert "print('hi')" in rendered
    assert "/Users/someone" not in rendered
    assert "sk-live-abcdef" not in rendered


def test_projection_failure_falls_back_without_losing_result(monkeypatch):
    """契约投影抛异常时，回退旧渲染并保留交付摘要。"""
    class _Boom:
        def project(self, *_args, **_kwargs):
            raise RuntimeError("projection exploded")

        def register_for(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr("app.contracts.projection_registry", lambda **_kwargs: _Boom())
    rendered = render_for_model(normalize_skill_result(_navigator_result()), max_chars=2000)
    assert rendered.strip(), "投影失败也必须交付内容"
    assert "答辩" in rendered
