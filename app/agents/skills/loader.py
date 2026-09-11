"""工具与开发者工作流插件加载器。

``plugins/tools`` 只能放原子 Tool；``plugins/workflows`` 只能放开发者
维护的公共 WorkflowSkill。用户自建 Skill 存数据库，不加载任意 Python。
"""

import importlib.util
import inspect
import sys
from pathlib import Path

from loguru import logger

from app.agents.skills.base import Tool, WorkflowSkill
from app.agents.skills.contract_lint import lint_skill_contracts
from app.agents.skills.registry import SkillRegistry, ToolRegistry
from app.core.config import settings

# 已加载模块与注册名称（reload 时据此卸载）
_loaded_modules: list[str] = []
_loaded_skill_names: list[str] = []
_loaded_tool_names: list[str] = []
# 被插件覆盖的内置技能（卸载插件时恢复）
_builtin_backup: dict[str, Tool | WorkflowSkill] = {}

def tool_plugins_dir() -> Path:
    return Path(settings.TOOL_PLUGINS_DIR)


def workflow_plugins_dir() -> Path:
    return Path(settings.WORKFLOW_SKILLS_DIR)


def load_skill_plugins() -> int:
    """扫描工具/工作流插件并注册；返回两类新增实例总数。"""
    count = 0
    for kind, directory in (("tool", tool_plugins_dir()), ("workflow", workflow_plugins_dir())):
        if not directory.is_dir():
            logger.warning("{} 插件目录不存在，跳过: {}", "工具" if kind == "tool" else "工作流 Skill", directory)
            continue
        for path in sorted(directory.rglob("*.py")):
            if path.name.startswith("_") or "__pycache__" in path.parts or path.name == "__init__.py":
                continue
            rel_parts = path.relative_to(directory).with_suffix("").parts
            module_name = _safe_module_name(f"{kind}_" + "_".join(rel_parts))
            if not module_name:
                logger.warning("跳过非法插件文件名（需字母/数字/下划线）: {}", path.name)
                continue
            count += _load_module(f"lumi_{kind}_plugin_{module_name}", path, expected_kind=kind)
    errors = lint_skill_contracts(SkillRegistry.list())
    if errors:
        raise RuntimeError("Skill 契约静态检查失败：" + " | ".join(errors[:8]))
    logger.info(
        "插件加载完成: {} 个基础工具，{} 个内部工具已禁用，{} 个工作流 Skill（插件实例 {}）",
        len(ToolRegistry.list()),
        len(ToolRegistry.internal_list()),
        len(SkillRegistry.list()),
        count,
    )
    return count


def unload_skill_plugins() -> int:
    """卸载所有插件注册的技能（恢复被覆盖的内置技能）；返回移除数量."""
    removed = 0
    for name in _loaded_skill_names:
        SkillRegistry.unregister(name)
        if name in _builtin_backup:
            previous = _builtin_backup.pop(name)
            if isinstance(previous, WorkflowSkill):
                SkillRegistry.register(previous, source="builtin")
        removed += 1
    for name in _loaded_tool_names:
        ToolRegistry.unregister(name)
        if name in _builtin_backup:
            ToolRegistry.register(_builtin_backup.pop(name), source="builtin")
        removed += 1
    _loaded_skill_names.clear()
    _loaded_tool_names.clear()
    for mod_name in _loaded_modules:
        sys.modules.pop(mod_name, None)
    _loaded_modules.clear()
    return removed


def reload_skill_plugins() -> dict:
    """热更新：卸载旧插件 → 全量重新扫描注册."""
    unloaded = unload_skill_plugins()
    registered = load_skill_plugins()
    return {
        "unloaded": unloaded,
        "registered": registered,
        "workflow_skills": [
            {"name": s.name, "source": SkillRegistry.get_source(s.name), "kind": "workflow_skill"}
            for s in SkillRegistry.list()
        ],
        "tools": [
            {"name": tool.name, "source": ToolRegistry.get_source(tool.name), "kind": "tool"}
            for tool in ToolRegistry.list()
        ],
    }


async def rebuild_skill_semantic_index() -> bool:
    """Rebuild routing vectors immediately after a plugin reload.

    This function is intentionally async so the API endpoint can await a
    single coherent registry+index generation rather than leaving an opaque
    invalidation window behind.
    """
    from app.agents.skills.routing import warm_registered_skill_semantic_index

    return await warm_registered_skill_semantic_index()


def _safe_module_name(stem: str) -> str:
    """插件文件名 → 合法模块名（仅保留字母/数字/下划线）."""
    cleaned = "".join(ch for ch in stem if ch.isalnum() or ch == "_")
    return cleaned if cleaned else ""


def _load_module(module_name: str, path: Path, *, expected_kind: str) -> int:
    """导入单个插件文件；目录类型和类类型必须一致。"""
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            logger.warning("无法创建插件加载器: {}", path.name)
            return 0
        # 热更新关键：删除陈旧字节码缓存。
        # SourceFileLoader 按 mtime+size 判断缓存有效性，插件修改前后若等长且
        # 在同一秒内写入，会复用旧 pyc 导致"改了代码不生效"。
        cache_path = Path(importlib.util.cache_from_source(str(path)))
        if cache_path.exists():
            cache_path.unlink()
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        old_dont_write = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = old_dont_write
    except Exception as exc:  # noqa: BLE001
        logger.error("插件加载失败 {}: {}", path.name, exc)
        sys.modules.pop(module_name, None)
        return 0

    count = 0
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj in (Tool, WorkflowSkill) or not issubclass(obj, (Tool, WorkflowSkill)):
            continue
        # 以“_”开头的类是插件内部抽象基类，不是可注册能力。
        if obj.__name__.startswith("_"):
            continue
        # 只注册本模块定义的子类（跳过导入的基类/其他模块的类）
        if getattr(obj, "__module__", None) != module_name:
            continue
        try:
            instance = obj()
        except TypeError:
            logger.warning("插件 {} 中 {} 无法实例化，跳过", path.name, obj.__name__)
            continue
        actual_kind = "workflow" if isinstance(instance, WorkflowSkill) else "tool"
        if actual_kind != expected_kind:
            logger.error(
                "插件类型错误: {} 位于 {} 目录，却声明为 {}；已拒绝加载",
                path.name, expected_kind, actual_kind,
            )
            continue
        _register_plugin(instance)
        count += 1
    _loaded_modules.append(module_name)
    return count


def _register_plugin(instance: Tool | WorkflowSkill) -> None:
    """按类型注册：Tool 进原子工具表，WorkflowSkill 进工作流表。"""
    name = instance.name
    if isinstance(instance, WorkflowSkill):
        _load_workflow_prompt(instance)
        existing = SkillRegistry.get_workflow(name)
        if existing is not None and SkillRegistry.get_source(name) == "builtin" and name not in _builtin_backup:
            _builtin_backup[name] = existing
        # 文件系统插件属于开发者维护的公共资产；用户私有 Skill 只从数据库加载。
        instance.source = "developer"
        instance.visibility = "public"
        instance.owner_user_id = None
        SkillRegistry.register(instance, source="developer")
        _loaded_skill_names.append(name)
    else:
        existing = ToolRegistry.get(name)
        if existing is not None and ToolRegistry.get_source(name) == "builtin" and name not in _builtin_backup:
            _builtin_backup[name] = existing
        # 只有 base_tools.yaml 中明确列出的规范工具进入模型公共候选池。
        # 其余插件仍保留为执行器内部能力，供已编译 Workflow/Worker 使用，
        # 但不会被 Function Calling 或自由路由暴露。
        from app.agents.skills.discovery import base_tool_names

        public_names = base_tool_names()
        ToolRegistry.register(instance, source="plugin", public=(not public_names or name in public_names))
        _loaded_tool_names.append(name)
        # 插件白名单/版本校验（F 项）：不受信的命名空间或非法版本号只记录不阻断，
        # 与 ToolSpec 影子注册共用同一份报告（tool_spec_report()）。
        try:
            from app.contracts.tools import (
                plugin_namespace_for,
                record_plugin_report,
                validate_plugin_declaration,
            )

            namespace = plugin_namespace_for(
                str(getattr(instance, "__module__", "") or ""),
                declared=str(getattr(instance, "namespace", "") or ""),
            )
            record_plugin_report(
                name,
                validate_plugin_declaration(instance, namespace=namespace),
            )
        except Exception as exc:  # noqa: BLE001 - 准入校验不得影响插件加载
            logger.debug("插件契约准入校验异常: {} ({})", name, str(exc)[:120])


def _load_workflow_prompt(instance: WorkflowSkill) -> None:
    """Load an optional Prompt-as-Code body without changing runtime logic.

    A developer workflow may provide ``plugins/workflows/prompts/<skill>.md``
    (or set ``prompt_file`` to a relative path).  The prompt is data only: the
    Skill's allowed tools, permissions and confirmation policy remain enforced
    by the existing runner and cannot be overridden by Markdown.
    """
    try:
        root = workflow_plugins_dir()
        candidates = []
        if getattr(instance, "prompt_file", ""):
            candidates.append(root / str(instance.prompt_file))
        candidates.extend((root / "prompts" / f"{instance.name}.md", root / "prompts" / f"{instance.name}.prompt.md"))
        for path in candidates:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    instance.prompt_body = text
                    instance.prompt_version = str(int(path.stat().st_mtime_ns))
                return
    except (OSError, ValueError):
        return
