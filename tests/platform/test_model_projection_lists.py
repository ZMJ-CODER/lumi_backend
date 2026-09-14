"""模型投影的列表型渲染：检索命中 / 目录列举也必须走 SDK 投影。

为什么要有这组回归：search 与 list 的结果以前会绕过契约投影（正文走投影、
命中/目录走 ``json.dumps``），同一份数据出现两套模型侧形状。现在三类列表型
payload（sections / matches / entries）统一由 ``ModelProjection`` 渲染。
"""

from __future__ import annotations

from lumi_contracts.projections.model import ModelProjection
from lumi_contracts import ExecutionResult

from app.agents.skills.output_contract import ToolOutput
from app.services.tool_output_pipeline import render_for_model


def _search_envelope(**overrides) -> dict:
    envelope = {
        "status": "success",
        "action": "search",
        "summary": "「README」命中 2 处",
        "has_more": True,
        "cursor": "nav1:abc",
        "data": {
            "matches": [
                {
                    "path": "docs/README.md",
                    "location": "page-2",
                    "context": "这里是包含 README 的上下文片段",
                    "format": "md",
                },
                {
                    "path": "src/main.py",
                    "location": "line-42",
                    "context": "README 相关注释",
                    "format": "py",
                },
            ]
        },
        "meta": {"workspace_id": "ws-1"},
    }
    envelope.update(overrides)
    return envelope


def _list_envelope() -> dict:
    return {
        "status": "success",
        "action": "list",
        "summary": "根目录 3 项",
        "data": {
            "entries": [
                {"path": "docs", "kind": "directory"},
                {"path": "README.md", "kind": "file", "size": 12},
            ]
        },
        "meta": {"workspace_id": "ws-1"},
    }


def test_search_hits_are_rendered_as_location_lines_not_json():
    rendered = render_for_model(
        ToolOutput(data=_search_envelope(), content_type="structured"), max_chars=2000
    )
    assert "[docs/README.md · page-2]" in rendered
    assert "[src/main.py · line-42]" in rendered
    assert "这里是包含 README 的上下文片段" in rendered
    assert "README 相关注释" in rendered
    # 不再退化成 JSON
    assert '"matches"' not in rendered and "{" not in rendered


def test_search_summary_and_cursor_hint_survive_the_projection():
    rendered = render_for_model(
        ToolOutput(data=_search_envelope(), content_type="structured"), max_chars=2000
    )
    assert "「README」命中 2 处" in rendered
    # 未读完时必须给出继续读取的游标
    assert "nav1:abc" in rendered


def test_listing_entries_are_rendered_one_path_per_line():
    rendered = render_for_model(
        ToolOutput(data=_list_envelope(), content_type="structured"), max_chars=2000
    )
    assert "[docs/]" in rendered  # 目录补斜杠，便于直接拼 read 的 path
    assert "[README.md]" in rendered
    assert "根目录 3 项" in rendered
    assert '"entries"' not in rendered


def test_rendering_respects_the_budget_and_marks_truncation():
    many = _search_envelope(
        data={
            "matches": [
                {"path": f"docs/f{index}.md", "location": f"line-{index}", "context": "x" * 200}
                for index in range(50)
            ]
        }
    )
    rendered = render_for_model(ToolOutput(data=many, content_type="structured"), max_chars=600)
    # 预算 + 1：末尾截断标记 "…" 允许超出 1 个字符（既有渲染契约）。
    assert len(rendered) <= 601
    assert "docs/f0.md" in rendered
    # 超预算时必须显式提示，而不是静默丢弃
    assert rendered.endswith("…") or "结果不完整" in rendered


def test_non_list_payloads_still_use_json_rendering():
    """只有列表型 payload 走行式渲染，其它结构化结果行为不变。"""
    view = ModelProjection(budget=2000).project(
        ExecutionResult[dict](payload={"answer": "42", "meta": {"k": 1}})
    )
    assert '"answer"' in view["text"]


def test_matches_render_does_not_leak_host_paths():
    envelope = _search_envelope(
        data={
            "matches": [
                {
                    "path": "docs/README.md",
                    "location": "line-1",
                    "context": "ok",
                    "internal_locator": "/Users/someone/secret/dir",
                }
            ]
        }
    )
    rendered = render_for_model(ToolOutput(data=envelope, content_type="structured"), max_chars=2000)
    assert "docs/README.md" in rendered
    assert "/Users/someone" not in rendered
