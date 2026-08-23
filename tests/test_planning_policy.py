from pathlib import Path

from app.agents.orchestration.intent import classify
from app.agents.orchestration.policy import planning


def test_deployment_planning_policy_is_loadable_and_bounded():
    planning.load_planning_policy.cache_clear()
    policy = planning.load_planning_policy()

    assert policy.version == 1
    assert "document_compare_flow" in policy.document_required_templates
    assert any(entry.name == "daily_brief_flow" for entry in policy.template_markers)


def test_planning_policy_preserves_template_and_script_classifier_paths():
    assert classify("请对比这两份附件", [{"doc_id": "a"}, {"doc_id": "b"}]) == {
        "task_type": "template", "template": "document_compare_flow",
    }
    assert classify("请把这个附件导出", [{"doc_id": "a"}]) == {
        "task_type": "script", "template": None,
    }
    assert classify("请生成今日早报") == {
        "task_type": "template", "template": "daily_brief_flow",
    }


def test_planning_policy_path_is_a_checked_in_deployment_asset():
    path = Path(__file__).resolve().parents[1] / "config" / "agent_policies" / "planning_rules.yaml"

    assert path.is_file()
