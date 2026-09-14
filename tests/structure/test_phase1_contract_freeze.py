"""阶段 1「冻结契约」清单回归：这些名字/字段一旦改动必须是**故意**的。

覆盖用户拍板的冻结项中已落地的部分：PreflightResult 对外状态、Effect Journal 状态、
插件配额字段、归档内容接口路径、灰度开关名。（ModelPlan 字段冻结留待下轮补齐。）
"""

from __future__ import annotations

from app.agents.orchestration.preflight.capability_preflight import (
    FROZEN_PREFLIGHT_STATES,
    PreflightStatus,
    STATUS_ERROR_CODES,
)
from app.api.v1 import artifacts as artifacts_api
from app.platform.runtime.feature_flags import ALL_FLAGS
from lumi_contracts.persistence.effect_recovery import (
    JOURNAL_CONFIRMED,
    JOURNAL_INTENT,
    JOURNAL_UNCERTAIN,
)
from lumi_contracts.plugins.lifecycle import PluginQuota


def test_preflight_external_states_are_frozen():
    assert FROZEN_PREFLIGHT_STATES == {
        "DEPENDENCY_MISSING",
        "CAPABILITY_UNAVAILABLE",
        "PERMISSION_DENIED",
        "APPROVAL_REQUIRED",
    }
    assert PreflightStatus.READY.value == "READY"
    # 对外四个状态都必须能映射到已登记的统一错误码
    from lumi_contracts import spec_for

    for state in FROZEN_PREFLIGHT_STATES:
        code = STATUS_ERROR_CODES[state]
        assert code and spec_for(code).code == code, state


def test_effect_journal_status_vocabulary_is_frozen():
    assert {JOURNAL_INTENT, JOURNAL_CONFIRMED, JOURNAL_UNCERTAIN} == {"intent", "confirmed", "uncertain"}
    from app.models.db_models import EffectJournal

    constraint = " ".join(str(item.sqltext) for item in EffectJournal.__table__.constraints if hasattr(item, "sqltext"))
    assert "intent" in constraint and "confirmed" in constraint and "uncertain" in constraint
    assert "uncertain_at" in EffectJournal.__table__.columns, "uncertain 时间戳是恢复判据"


def test_plugin_quota_fields_are_frozen():
    fields = set(PluginQuota.model_fields)
    assert {
        "cpu_limit", "memory_mb", "timeout_seconds", "max_output_bytes",
        "network_egress", "network_rate_limit",
    } <= fields


def test_archive_content_endpoint_is_the_only_archive_reader():
    paths = [getattr(route, "path", "") for route in artifacts_api.router.routes]
    assert "/{artifact_id}/content" in paths
    assert not any("archive" in path or "job-log" in path for path in paths)


def test_feature_flag_names_are_frozen():
    assert set(ALL_FLAGS) == {
        "CAPABILITY_PREFLIGHT_V2", "MODEL_CAPABILITY_ROUTER_V2", "EFFECT_JOURNAL_RECOVERY_V2",
        "TASK_PROFILE_CANONICAL", "PLUGIN_QUOTA_ENFORCEMENT", "ARCHIVE_CONTENT_V2",
        # 《结果存储、检查点与恢复》方案新增（同样是"默认全关、关掉回到旧路径"）。
        "RESULT_STORE_V2", "STEP_CHECKPOINT_V2", "EFFECT_JOURNAL_TYPED_V2",
        "INTEGRATION_SHADOW_MODE",
        # 《降级熔断与运行时干预》方案新增：运行时策略覆盖 + 写侧代际校验。
        "RUNTIME_POLICY_OVERRIDE", "WRITE_GATE_ENFORCEMENT",
        # 《工具发现链路》P1：统一工具注册表（静态表退化为兜底）。
        "TOOL_REGISTRY_DERIVED",
        # 《资源能力层》Phase 2：动作意图 + 资源类型 → 统一能力 → 候选 Provider 的工具窗口
        # （旧 ACTION_TOOL_WINDOW 降级为 fallback；关闭时旧路径逐字不变）。
        "RESOURCE_CAPABILITY_WINDOW",
        # 《资源能力层》Phase 3：派发前解析结构化目标（能力/资源类型/Provider Adapter），
        # 不再从工具名猜能力；关闭时旧解析逐字不变。
        "RESOURCE_CAPABILITY_DISPATCH",
        # 《资源能力层》Phase 4：Workflow Skill 按能力声明选择/校验工具；
        # 关闭时逐字走旧的 allowed_tools 白名单（底层 MCP 名降为兼容层）。
        "RESOURCE_CAPABILITY_WORKFLOW",
        # 《资源能力层》Phase 5：模型可见工具面收敛到 Read/Write/Edit/Move/Delete/Run/Search
        # （关闭时注入逐字不变；分类不了的工具有意保留原名）。
        "RESOURCE_CAPABILITY_SURFACE",
    }


def test_model_plan_fields_and_redis_key_are_frozen():
    """ModelPlan 冻结项：任务级冻结的字段与 Redis 键/TTL/版本必须稳定。"""
    import dataclasses

    from app.platform.model.model_plan import PLAN_KEY, PLAN_TTL_SECONDS, PLAN_VERSION, ModelPlan

    names = {item.name for item in dataclasses.fields(ModelPlan)}
    assert {"plan_id", "version", "created_at", "byok", "scene", "roles"} <= names
    assert PLAN_KEY == "llm_plan:{plan_id}"
    assert PLAN_TTL_SECONDS == 6 * 3600
    assert PLAN_VERSION == 1
    # 计划里只放公开配置（无密钥字段）——byok 只记录"是否用户自带"
    assert not any("key" in name for name in names if name != "plan_id")

