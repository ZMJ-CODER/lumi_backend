"""P6（拆 ``models/db_models``）落地回归：聚合入口必须**覆盖全部模型**。

这一阶段的风险很单一：**拆完忘了在聚合入口里导出**。后果是"某张表的模型在
Alembic autogenerate 里消失"——静默、且要等到线上加字段才发现。所以这里用
SQLAlchemy 的 mapper registry 做**闭环校验**：注册表里有多少个映射类，
聚合入口就必须导出多少个。
"""

from __future__ import annotations

import importlib
import sys

import pytest

from _paths import REPO_ROOT
REPO_ROOT = REPO_ROOT
DB_DIR = REPO_ROOT / "app" / "models" / "db"

#: 域模块 → 它负责的表（拆分时的分组，测试把它钉住）
EXPECTED_TABLES: dict[str, set[str]] = {
    "user": {"users", "refresh_tokens", "user_prompts", "user_preferences", "user_presets"},
    "conversation": {"conversations", "messages", "conversation_memory_states", "conversation_segments", "attachments"},
    "knowledge": {"knowledge_spaces", "documents", "document_chunks", "projects", "project_index", "code_embeddings"},
    "memory": {"memories", "memory_profile"},
    "office": {"office_sessions", "office_task_indices"},
    "job": {"effect_journal", "job_runs", "job_steps"},
    "plugin": {"user_mcp_tool_bindings", "skill_telemetry_daily", "user_workflow_skills"},
    "audit": {"control_logs"},
    "usage": {"llm_usage", "daily_token_stats"},
}


def test_every_mapped_class_is_exported_by_the_aggregator():
    """拆分后最容易犯的错：新模型只加进域模块、忘了聚合入口导出。"""
    import app.models.db_models as aggregator
    from app.models.db_base import Base

    mapped = {mapper.class_.__name__ for mapper in Base.registry.mappers}
    exported = {name for name in aggregator.__all__}
    missing = mapped - exported
    assert not missing, f"这些映射类没有从 app.models.db_models 导出：{sorted(missing)}"
    for name in sorted(exported):
        assert hasattr(aggregator, name), name
    # 反向：导出的名字必须真的是映射类（防止导出到常量/工具函数）
    assert exported <= mapped, f"聚合入口导出了非映射类：{sorted(exported - mapped)}"


def test_domain_modules_own_their_tables():
    """每个域模块只定义自己那几张表（防复制粘贴时把表搬错地方）。"""
    import app.models.db_models  # noqa: F401  确保 registry 完整
    from app.models.db_base import Base

    assert set(EXPECTED_TABLES) == {path.stem for path in DB_DIR.glob("*.py") if path.stem != "__init__"}
    declared = {table for tables in EXPECTED_TABLES.values() for table in tables}
    assert declared == set(Base.metadata.tables), "有表没被分组，或分组里写了不存在的表"


@pytest.mark.parametrize("module", sorted(EXPECTED_TABLES))
def test_tables_live_in_the_expected_module(module):
    loaded = importlib.import_module(f"app.models.db.{module}")
    names = {name for name in dir(loaded) if not name.startswith("_")}
    from app.models.db_base import Base

    tables = {
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if mapper.class_.__module__ == loaded.__name__ and hasattr(mapper.class_, "__tablename__")
    }
    assert tables == EXPECTED_TABLES[module], f"{module} 负责的表不符：{sorted(tables)}"
    assert names, module


def test_package_import_loads_every_sibling_module():
    """只 import 一个子模块时，兄弟模块也必须已加载——否则 mapper 解析不出关系目标。"""
    for name in EXPECTED_TABLES:
        assert f"app.models.db.{name}" in sys.modules, name


def test_legacy_import_path_still_works():
    """``from app.models.db_models import X`` 是 Alembic 与 50 处调用的入口，必须照旧。"""
    from app.models.db_models import Memory, MemoryProfile, User  # noqa: F401

    assert User.__tablename__ == "users"
    assert Memory.__tablename__ == "memories"
    assert MemoryProfile.__tablename__ == "memory_profile"


def test_alembic_metadata_entry_sees_all_tables():
    """Alembic 的 ``from app.models import db_models`` 必须能看到 29 张表。"""
    import app.models.db_models  # noqa: F401
    from app.models.db_base import Base

    assert len(Base.metadata.tables) == sum(len(tables) for tables in EXPECTED_TABLES.values())
