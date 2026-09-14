from app.agents.skills.base import SkillResult
from app.services.tool_output_projection import project_citations, project_tool_output


def test_web_results_are_projected_as_short_summaries():
    result = SkillResult(
        success=True,
        output="x" * 10000,
        metadata={"citations": [{"title": "来源", "source": "https://example.com", "content": "长内容 " * 500}]},
    )
    projected = project_tool_output(result)
    assert len(projected) < 2600
    assert "不要逐字复制原文" not in projected
    assert "检索到" in projected
    assert len(projected.split("摘要：", 1)[-1]) <= 260


def test_citations_are_bounded_for_client_display():
    citations = project_citations([{"title": "a", "content": "z" * 1000} for _ in range(20)])
    assert len(citations) == 10
    assert len(citations[0]["content"]) <= 240
