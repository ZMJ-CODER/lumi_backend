"""技能插件加载器 —— 递归扫描 plugins/skills 分类目录，自动发现并注册 Skill 子类.

设计：
  - 每个插件 = 分类子目录下一个 Python 文件，定义一个（或多个）Skill 子类
  - 八大分类：filesystem / shell / process / system / network / devtools / desktop / mcp
  - 启动时与 POST /admin/skills/reload 时扫描注册；同名插件覆盖内置技能
  - 热更新不重启进程：重新扫描 → 卸载旧插件技能（恢复被覆盖的内置技能）→ 重新注册
  - Docker 部署时将 ./plugins 挂载为 volume，改插件文件无需重建镜像

安全边界：插件在服务端进程内执行 Python 代码，属于受信代码（管理员放置），
不能作为用户上传入口；用户级插件市场需走沙箱隔离（后续）。
"""

import importlib.util
import inspect
import sys
from pathlib import Path

from loguru import logger

from app.agents.skills.base import Skill
from app.agents.skills.contract_lint import lint_skill_contracts
from app.agents.skills.registry import SkillRegistry
from app.core.config import settings

# 已加载的插件模块名 / 插件注册的技能名（reload 时据此卸载）
_loaded_modules: list[str] = []
_loaded_skill_names: list[str] = []
# 被插件覆盖的内置技能（卸载插件时恢复）
_builtin_backup: dict[str, Skill] = {}


def plugins_dir() -> Path:
    return Path(settings.SKILL_PLUGINS_DIR)


def load_skill_plugins() -> int:
    """扫描插件目录并注册；返回新注册的技能数."""
    directory = plugins_dir()
    if not directory.is_dir():
        logger.warning("技能插件目录不存在，跳过: {}", directory)
        return 0
    count = 0
    for path in sorted(directory.rglob("*.py")):
        # 跳过隐藏/私有文件、__init__.py 与 __pycache__ 字节码缓存
        if (
            path.name.startswith("_")
            or "__pycache__" in path.parts
            or path.name == "__init__.py"
        ):
            continue
        # 模块名 = 分类_文件名（相对插件根），避免不同目录同名文件冲突
        rel_parts = path.relative_to(directory).with_suffix("").parts
        module_name = _safe_module_name("_".join(rel_parts))
        if not module_name:
            logger.warning("跳过非法插件文件名（需字母/数字/下划线）: {}", path.name)
            continue
        count += _load_module(f"lumi_skill_plugin_{module_name}", path)
    errors = lint_skill_contracts(SkillRegistry.list())
    if errors:
        raise RuntimeError("Skill 契约静态检查失败：" + " | ".join(errors[:8]))
    logger.info("技能插件加载完成: {} 个新技能", count)
    return count


def unload_skill_plugins() -> int:
    """卸载所有插件注册的技能（恢复被覆盖的内置技能）；返回移除数量."""
    removed = 0
    for name in _loaded_skill_names:
        SkillRegistry.unregister(name)
        if name in _builtin_backup:
            SkillRegistry.register(_builtin_backup.pop(name), source="builtin")
        removed += 1
    _loaded_skill_names.clear()
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
        "skills": [
            {"name": s.name, "source": SkillRegistry.get_source(s.name)}
            for s in SkillRegistry.list()
        ],
    }


def _safe_module_name(stem: str) -> str:
    """插件文件名 → 合法模块名（仅保留字母/数字/下划线）."""
    cleaned = "".join(ch for ch in stem if ch.isalnum() or ch == "_")
    return cleaned if cleaned else ""


def _load_module(module_name: str, path: Path) -> int:
    """导入单个插件文件并注册其中定义的 Skill 子类."""
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
        if obj is Skill or not issubclass(obj, Skill):
            continue
        # 只注册本模块定义的子类（跳过导入的基类/其他模块的类）
        if getattr(obj, "__module__", None) != module_name:
            continue
        try:
            instance = obj()
        except TypeError:
            logger.warning("插件 {} 中 {} 无法实例化，跳过", path.name, obj.__name__)
            continue
        _register_plugin(instance)
        count += 1
    _loaded_modules.append(module_name)
    return count


def _register_plugin(instance: Skill) -> None:
    """注册插件技能；若覆盖内置技能则先备份."""
    name = instance.name
    existing = SkillRegistry.get(name)
    if existing is not None and SkillRegistry.get_source(name) == "builtin" and name not in _builtin_backup:
        _builtin_backup[name] = existing
        logger.warning("插件技能 '{}' 覆盖内置技能", name)
    SkillRegistry.register(instance, source="plugin")
    _loaded_skill_names.append(name)
