"""技能插件加载器测试：扫描/注册/热更新/覆盖恢复."""

import asyncio

import pytest

from app.agents.skills import loader
from app.agents.skills.registry import SkillRegistry
from app.core.config import settings


@pytest.fixture(autouse=True)
def _skills():
    """加载真实插件目录，测试结束后清理."""
    SkillRegistry.clear()
    loader.unload_skill_plugins()
    loader.load_skill_plugins()
    yield
    loader.unload_skill_plugins()
    SkillRegistry.clear()


def _write_plugin(plugin_dir, name, output):
    (plugin_dir / f"{name}.py").write_text(
        f"""
from app.agents.skills.base import Skill, SkillResult

class {name.title()}Skill(Skill):
    name = "{name}"
    description = "test plugin {name}"
    category = "computation"
    scenes = ["chat"]

    async def execute(self, params, context=None):
        return SkillResult(success=True, output="{output}")
""",
        encoding="utf-8",
    )


def _plugin_skill_output(skill):
    return asyncio.run(skill.execute({})).output


def test_plugin_load_reload_unload(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "skills"
    plugin_dir.mkdir()
    monkeypatch.setattr(settings, "SKILL_PLUGINS_DIR", str(plugin_dir))
    loader.unload_skill_plugins()

    _write_plugin(plugin_dir, "hello", "v1")
    assert loader.load_skill_plugins() == 1
    skill = SkillRegistry.get("hello")
    assert skill is not None
    assert SkillRegistry.get_source("hello") == "plugin"
    assert _plugin_skill_output(skill) == "v1"

    # 热更新：改文件 → reload → 新逻辑生效（不重启）
    _write_plugin(plugin_dir, "hello", "v2")
    result = loader.reload_skill_plugins()
    assert result["registered"] == 1
    assert _plugin_skill_output(SkillRegistry.get("hello")) == "v2"

    # 卸载后移除
    loader.unload_skill_plugins()
    assert SkillRegistry.get("hello") is None

