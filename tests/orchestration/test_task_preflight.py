from app.agents.orchestration.preflight.task_preflight import preflight_external_effect


def test_pure_generation_is_not_blocked():
    result = preflight_external_effect("写一份项目延期说明")
    assert result.needs_clarification is False


def test_missing_target_is_clarified_before_execution():
    result = preflight_external_effect("帮我修改一下")
    assert result.needs_clarification is True
    assert result.reason == "effect_without_target"


def test_local_target_without_workspace_is_clarified():
    result = preflight_external_effect("把本地文件改一下")
    assert result.needs_clarification is True
    assert result.reason in {"effect_without_target", "local_effect_without_scope"}


def test_authorized_workspace_allows_targeted_operation():
    result = preflight_external_effect(
        "修改 src/main.py", workspace_id="ws-1"
    )
    assert result.needs_clarification is False


def test_path_target_without_scope_is_clarified():
    result = preflight_external_effect("修改 src/main.py")
    assert result.needs_clarification is True
    assert result.reason == "local_effect_without_scope"
