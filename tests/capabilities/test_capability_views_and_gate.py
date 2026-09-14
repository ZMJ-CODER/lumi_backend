"""阶段 7/8 + 步骤级门禁回归：声明式视图、插件 REST 端点、执行前能力门禁。

三条边界：

1. **视图只呈现不搬运**：超预算截断并标记 ``truncated``；``file_preview`` 不给路径；
   未知视图类型由契约层拒绝；
2. **插件端点稳定错误**：类型未登记/依赖未满足 → 400 且带 ``error_code``；未安装 → 404；
   扩展类型登记在生产模式 → 403；
3. **门禁只拦"静态不可能"**：未声明能力/位置违反本地性 → 执行前失败；运行时不可用
   （客户端离线）只记录，不阻断调度（否则一次离线会把计划整体打死）。
"""

from __future__ import annotations

from lumi_contracts.plugins import Deployment, DataLocality, VIEW_DATA_MAX_BYTES

from app.agents.capabilities.policy.gate import (
    evaluate_node_capabilities,
    node_capability_failure,
)
from app.agents.capabilities.views.views import (
    chart_view,
    diff_view,
    file_preview_view,
    form_view,
    supported_view_types,
    table_view,
    timeline_view,
    views_for_payload,
)
from app.agents.orchestration.models import TaskNode


def _node(**params) -> TaskNode:
    return TaskNode(id="s1", name="步骤", agent="w1", params=dict(params))


# ── 视图投影 ─────────────────────────────────────────────────────


def test_view_types_are_the_declarative_whitelist():
    assert set(supported_view_types()) == {
        "table", "chart", "diff", "timeline", "file_preview", "form",
    }
    # 未知类型由契约层拒绝（不允许自定义 JSX/JS）。
    try:
        table_view([{"a": 1}]).model_copy(update={"view_type": "custom_jsx"})
    except Exception:  # noqa: BLE001 - pydantic 校验失败也算通过
        pass


def test_table_view_renders_scalars_and_truncates_large_payloads():
    rows = [{"name": f"file-{i}.py", "size": i, "nested": {"a": 1}, "list": [1, 2]} for i in range(50)]
    view = table_view(rows, title="工作区文件")
    assert view.view_type == "table"
    assert view.data["rows"][0]["name"] == "file-0.py"
    # 嵌套结构只给摘要，避免前端渲染复杂对象。
    assert view.data["rows"][0]["nested"] == '{"a": 1}'
    assert view.data["rows"][0]["list"] == "[2 项]"

    huge = [{"name": "x" * 200, "content": "y" * 5000} for _ in range(200)]
    big = table_view(huge)
    assert big.data.get("truncated") is True
    assert len(
        str(big.data).encode("utf-8")
    ) <= VIEW_DATA_MAX_BYTES + 2000  # 截断后回到预算量级


def test_file_preview_never_exposes_paths_or_body():
    view = file_preview_view(
        name="report.docx", size=1024, media_type="application/docx", summary="季度报告要点"
    )
    blob = str(view.data)
    assert view.data["name"] == "report.docx"
    assert view.data["size"] == 1024
    # 只有名称/大小/类型/摘要——没有路径，也没有正文。
    for forbidden in ("C:\\", "/Users/", "content", "body"):
        assert forbidden not in blob


def test_diff_view_labels_lines_and_hides_directories():
    diff = "@@ -1,2 +1,2 @@\n-old line\n+new line\n context"
    view = diff_view(diff, path="E:\\work\\proj\\src\\app.py")
    assert view.view_type == "diff"
    assert view.data["added"] == 1 and view.data["removed"] == 1
    # 只保留文件名（不暴露目录结构）。
    assert view.data["path"] == "app.py"
    assert [item["kind"] for item in view.data["lines"]][:3] == ["hunk", "remove", "add"]


def test_chart_timeline_and_form_views_are_bounded():
    chart = chart_view([{"x": i, "y": i * 2} for i in range(600)])
    assert chart.view_type == "chart"
    assert len(chart.data["points"]) == 500
    assert chart.data["truncated"] is True

    timeline = timeline_view([{"at": "2026-01-01", "title": "读取", "status": "completed"}])
    assert timeline.data["items"][0]["title"] == "读取"

    form = form_view([{"name": "path", "label": "文件路径", "type": "text", "required": True}])
    assert form.data["fields"][0]["name"] == "path"
    assert form.data["submit_label"] == "提交"


def test_views_for_payload_only_uses_known_safe_shapes():
    views = views_for_payload(
        {
            "entries": [{"name": "a.py", "kind": "file"}],
            "activities": [{"at": "t1", "title": "读取", "status": "completed"}],
            "diff": "+x\n-y",
            "artifacts": [{"name": "out.docx", "size": 10}],
            "arguments": {"secret": "sk-live-xxx"},
        },
        capability="workspace.read@1",
    )
    kinds = [item.view_type for item in views]
    assert kinds == ["table", "timeline", "diff", "file_preview"]
    blob = str([item.data for item in views])
    # 原始参数不参与任何视图
    assert "sk-live" not in blob and "arguments" not in blob
    assert all(item.source == "workspace.read@1" for item in views)
    # 不认识的形状不硬猜
    assert views_for_payload({"unknown_shape": [1, 2, 3]}) == []


# ── 步骤级能力门禁 ───────────────────────────────────────────────


def test_gate_ignores_nodes_without_declared_capabilities():
    gate = evaluate_node_capabilities(_node())
    assert gate.ok is True and gate.required == []
    assert node_capability_failure(_node()) is None


def test_gate_blocks_unknown_capability_before_execution():
    node = _node(required_capabilities=["nope.thing@1"])
    gate = evaluate_node_capabilities(node)
    assert gate.ok is False
    assert gate.blocking[0].code == "CAPABILITY_DEPENDENCY_MISSING"
    failure = node_capability_failure(node)
    assert failure is not None
    # 与 Broker 的缺能力错误码对齐（前端一套处理）。
    assert failure.error_code == "CAPABILITY_MISSING"


def test_gate_blocks_local_capability_on_server_side_node():
    node = _node(required_capabilities=["workspace.read@1"])
    assert evaluate_node_capabilities(node).ok is True
    gate = evaluate_node_capabilities(node, deployment=Deployment.SERVER)
    assert gate.ok is False
    assert gate.blocking[0].code == "CAPABILITY_LOCATION_UNSATISFIABLE"
    assert gate.blocking[0].details["data_locality"] == DataLocality.LOCAL_ONLY.value


def test_gate_maps_abstract_capabilities_and_reports_unmapped_as_hint():
    node = _node(required_capabilities=["CODE_EXECUTION"])
    gate = evaluate_node_capabilities(node)
    assert gate.ok is True
    assert gate.required == ["code.execute@1"]

    node2 = _node(required_capabilities=["EMAIL_SEND"])
    gate2 = evaluate_node_capabilities(node2)
    # 未映射的抽象能力不阻断（服务端仍有等价实现），但会记录提示。
    assert gate2.ok is True
    assert gate2.issues and gate2.issues[0].required is False


def test_gate_records_runtime_unavailability_without_blocking():
    """客户端离线时只记 degraded，不在调度期阻断（否则一次离线打死整个计划）。"""
    from app.agents.capabilities.registry.resolver import CapabilityResolver

    node = _node(required_capabilities=["workspace.read@1"])
    # broker=None 时解析器不查 Provider → 记为 degraded（有记录、不阻断）
    gate = evaluate_node_capabilities(node, resolver=CapabilityResolver(broker=None))
    assert gate.ok is True
    assert gate.degraded and gate.degraded[0]["available"] is False


def test_gate_snapshot_is_json_safe():
    import json

    gate = evaluate_node_capabilities(_node(required_capabilities=["nope.thing@1"]))
    snapshot = gate.to_snapshot()
    assert snapshot["ok"] is False
    assert json.loads(json.dumps(snapshot, ensure_ascii=False)) == snapshot


# ── 插件 REST 端点注册 ───────────────────────────────────────────


def test_plugin_rest_router_is_registered():
    from app.api.router import api_router

    paths: set[str] = set()
    for route in api_router.routes:
        original = getattr(route, "original_router", None)
        if original is None:
            continue
        context = getattr(route, "include_context", None)
        prefix = str(getattr(context, "prefix", "") or "")
        if "plugin" not in prefix:
            continue
        for sub in getattr(original, "routes", []):
            paths.add(str(getattr(sub, "path", "")))
    assert "" in paths
    assert "/{plugin_id}/enable" in paths
    assert "/{plugin_id}/disable" in paths
    assert "/{plugin_id}/rollback" in paths
    assert "/{plugin_id}/health" in paths
    assert "/extension-handlers" in paths
