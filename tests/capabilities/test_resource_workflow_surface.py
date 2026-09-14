"""Phase 5 收官：Workflow 内部工具面与 SOP 提示词的一致性。

Workflow 与 chat/ReAct 的区别：它的工具 schema 由**插件自己**用
``_tool_defs(selected)`` 构造，而 SOP 文本来自 ``plugins/workflows/prompts/*.md``。
收敛必须同时覆盖两者——否则会出现"提示词让模型用 ``workspace_write``，
schema 里只有 ``Write``"的矛盾，模型只能调一个不存在的名字。

因此这里用**运行期翻译**而不是改提示词内容：

* schema：``collapse_with_names()`` → 对外名；
* SOP 文本：``translate_prompt_names()`` → 实现名替换成对外名；
* 两者的名字集合必须一致（有测试钉住）；
* 关闭开关时**两者都逐字不变**。
"""

from __future__ import annotations

from app.agents.capabilities.views import resource_surface as sf


def _cap(name: str):
    from app.agents.skills.capability import ToolCapability

    return ToolCapability(name=name, description=f"d:{name}", parameters={"type": "object", "properties": {}})


# ── 1. SOP 提示词翻译 ───────────────────────────────────────


def test_prompt_translation_is_verbatim_when_disabled(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", False)
    text = "新建用 workspace_write，定点替换用 workspace_edit，读取用 mcp__lumi_client__workspace_navigator。"
    assert sf.translate_prompt_names(text) == text


def test_prompt_translation_replaces_implementation_names(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    text = "新建用 workspace_write，定点替换用 workspace_edit，读取用 mcp__lumi_client__workspace_navigator。"
    translated = sf.translate_prompt_names(text)
    assert "workspace_write" not in translated
    assert "workspace_edit" not in translated
    assert "mcp__lumi_client__workspace_navigator" not in translated
    assert "Write" in translated and "Edit" in translated and "Read" in translated
    # 七个动词表达不了的辅助动作**保留原名**（提交/回滚/diff/沙箱准备）
    raw = "提交用 workspace_commit，看差异用 workspace_diff，准备沙箱用 sandbox_prepare，跑测试用 sandbox_run。"
    out = sf.translate_prompt_names(raw)
    assert "workspace_commit" in out, "提交不是收敛名，必须原样保留"
    assert "workspace_diff" in out
    assert "sandbox_prepare" in out
    assert "Run" in out, "sandbox_run 收敛成 Run"


def test_tools_without_a_matching_verb_keep_their_names(monkeypatch):
    """**收敛边界**：七个动词表达不了的阶段/辅助动作不能被改名或被去重挤掉。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    pool = [
        _cap("workspace_navigator"),
        _cap("workspace_write"),
        _cap("workspace_commit"),
        _cap("workspace_diff"),
        _cap("sandbox_prepare"),
        _cap("sandbox_run"),
    ]
    pairs, alias = sf.collapse_with_names(pool)
    displays = [display for _item, display in pairs]
    assert displays == [
        "Read", "Write",
        "workspace_commit", "workspace_diff", "sandbox_prepare", "Run",
    ], displays
    # 提交路径仍然可达（不会被 workspace_write 挤掉）
    assert "workspace_commit" in displays
    assert alias == {"Read": "workspace_navigator", "Write": "workspace_write", "Run": "sandbox_run"}


def test_prompt_translation_does_not_touch_substrings(monkeypatch):
    """不能把 ``workspace_writer`` 之类的前缀匹配误替换。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    text = "字段 workspace_writer_helper 与 my_write 都不该被替换。"
    assert sf.translate_prompt_names(text) == text


def test_prompt_translation_handles_empty_and_missing(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    assert sf.translate_prompt_names("") == ""
    assert sf.translate_prompt_names(None) == ""


# ── 2. 工具 schema 与提示词一致性 ───────────────────────────


def test_collapse_with_names_returns_both_directions(monkeypatch):
    from app.core.config import settings

    pool = [_cap("workspace_navigator"), _cap("workspace_write"), _cap("AskUserQuestion")]
    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", False)
    pairs, alias = sf.collapse_with_names(pool)
    assert [display for _item, display in pairs] == [
        "workspace_navigator", "workspace_write", "AskUserQuestion",
    ]
    assert alias == {}, "关闭收敛时没有映射"

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    pairs, alias = sf.collapse_with_names(pool)
    assert [display for _item, display in pairs] == ["Read", "Write", "AskUserQuestion"]
    assert alias == {"Read": "workspace_navigator", "Write": "workspace_write"}


def test_schema_names_match_prompt_names(monkeypatch):
    """**核心一致性断言**：schema 里的名字与翻译后的 SOP 名字必须同属一套。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    pool = [_cap("workspace_navigator"), _cap("workspace_write"), _cap("workspace_edit")]
    pairs, _alias = sf.collapse_with_names(pool)
    schema_names = {display for _item, display in pairs}
    prompt = sf.translate_prompt_names(
        "读取用 workspace_navigator，新建用 workspace_write，替换用 workspace_edit。"
    )
    for name in ("Read", "Write", "Edit"):
        assert name in schema_names, name
        assert name in prompt, f"提示词应与 schema 用同一个名字：{name}"
    for impl in ("workspace_navigator", "workspace_write", "workspace_edit"):
        assert impl not in prompt, impl


# ── 3. 真实 Workflow Skill ──────────────────────────────────


def _load_skill(module_name: str):
    from importlib import import_module

    from app.agents.skills.base import WorkflowSkill

    module = import_module(f"plugins.workflows.developer.{module_name}")
    return next(
        obj() for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, WorkflowSkill) and obj is not WorkflowSkill
    )


def test_workflow_effective_prompt_is_verbatim_when_disabled(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", False)
    skill = _load_skill("workspace_code_change")
    skill.prompt_body = "新建用 workspace_write。"
    assert skill.effective_prompt() == "新建用 workspace_write。"


def test_workflow_effective_prompt_is_translated_when_enabled(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    skill = _load_skill("workspace_code_change")
    skill.prompt_body = "新建用 workspace_write，替换用 workspace_edit。"
    translated = skill.effective_prompt()
    assert "workspace_write" not in translated
    assert "Write" in translated and "Edit" in translated


def test_real_sop_prompt_has_no_converged_implementation_names(monkeypatch):
    """真实 SOP 文本（.md）在收敛打开时不应残留任何**收敛过的**实现名。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    mapping = sf._prompt_name_map()
    assert mapping, "提示词翻译表不能为空"
    for module_name in ("workspace_code_change", "workspace_operation"):
        skill = _load_skill(module_name)
        skill.prompt_body = open(  # noqa: SIM115 - 测试里读一次就关
            f"plugins/workflows/prompts/{module_name}.md", encoding="utf-8"
        ).read()
        try:
            translated = skill.effective_prompt()
        finally:
            skill.prompt_body = ""
        for impl in mapping:
            assert impl not in translated, f"{module_name} 的 SOP 仍残留 {impl}"
