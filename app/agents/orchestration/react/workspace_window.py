"""ReAct 的**工作区工具窗口**：按阶段把工作区能力并入本轮候选窗。

这一层是"模型这一轮能看见哪些工作区工具"
的唯一入口，规则刻意写在这里而不是散在循环里：

* 读取阶段：只并入聚合入口 ``workspace_navigator``（list/search/read/scan）；
* 写阶段：出现修改/创建/删除/移动等意图时，注入四个原子操作工具
  （``workspace_write/edit/move/delete``：版本校验 + 审批 + 回收站 + 读回校验）；
  客户端还没广告这些工具时退回旧的暂存对（stage_write/stage_delete）；
* 沙箱/提交阶段：分别按执行与提交意图注入。

**意图来源优先画像**（``action_intents``），没有画像时才退回 ``route_text`` 关键词——
"用户要求创建文件，模型只拿到读取工具"就是靠这条修掉的。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.agents.skills.mandatory_tools import apply_tool_window


class WorkspaceWindowMixin:
    """工作区阶段窗口（混入 ``OfficeReactRunner``；不定义 ``__init__``）。"""

    #: 会被视为"要动手"的动作意图（其余是只读）。
    WRITE_ACTIONS = frozenset({"CREATE", "MODIFY", "DELETE", "MOVE", "SEND", "PUBLISH"})
    #: 会被视为"要执行/验证"的动作意图（沙箱域）。
    EXECUTE_ACTIONS = frozenset({"EXECUTE"})

    @staticmethod
    def _uses_workspace_vocab(text: str) -> bool:
        """保守判断当前步骤是否可能面向本地工作区内容。

        仅作为“是否给工作区读取域留窗口”的提示信号；真正能否调用仍由
        workspace 授权门（execute_tool_call WORKSPACE_SCOPE_*）决定。
        """
        value = (text or "").casefold()
        markers = (
            "工作区", "目录", "文件夹", "文件", "项目代码", "本地项目",
            "workspace", "project file", "readme", "src/", ".py", ".md",
            ".txt", ".json", ".toml", ".cfg", "读取", "查看", "查找", "搜索文件",
        )
        return any(marker in value for marker in markers)

    async def _workspace_caps_for_group(self, group: frozenset[str]) -> list[Any]:
        from app.agents.skills.executor import get_workspace_action_capabilities

        return await get_workspace_action_capabilities(
            self.user_id, "office", self.user_role, self.workspace_id, group
        )

    def _intent_scope(self, route_text: str) -> tuple[bool, bool, bool]:
        """本次要注入哪些工作区域：(要写, 要执行, 要提交)。

        **画像优先**（方案 4 §4.1）：给了 ``action_intents`` 就直接映射，
        ``route_text`` 关键词只作极端兜底（没有画像时的兼容路径）。
        """
        intents = set(self.action_intents)
        if intents:
            write = bool(intents & self.WRITE_ACTIONS)
            execute = bool(intents & self.EXECUTE_ACTIONS)
            # 提交/回滚不是独立动作意图：只有用户显式要求提交时才注入（关键词兜底）。
            commit = False
            return write, execute, commit
        value = str(route_text or "").casefold()
        write = any(token in value for token in (
            "修改", "写入", "创建", "新建", "删除", "移动", "重命名", "复制", "暂存", "覆盖",
            "write", "create", "delete", "rename", "move", "copy", "stage",
        ))
        execute = any(token in value for token in (
            "测试", "运行", "执行", "构建", "验证", "沙箱", "test", "run", "build", "check",
        ))
        commit = any(token in value for token in ("提交", "回滚", "commit", "rollback"))
        return write, execute, commit

    async def _maybe_inject_workspace_stage_window(self, capabilities: list[Any], route_text: str) -> list[Any]:
        """按阶段把工作区能力并入候选窗，不因 write_op 永久隐藏写工具。

        读取阶段：只并入聚合入口 workspace_navigator（list/search/read/scan），内部原子
        读取名不再进窗；
        写阶段：出现修改/创建/删除/移动等意图时注入**四个原子操作工具**
        （workspace_write/edit/move/delete：版本校验 + 审批 + 回收站 + 读回校验）；
        只有客户端还没广告这些工具时，才退回旧的暂存对（stage_write/stage_delete）；
        沙箱：出现测试/运行/构建/验证意图时注入；
        提交域：出现提交/回滚意图时注入（实际提交仍由 ApprovalPolicyEngine
        决定是否需要确认，回滚始终确认）。

        **意图来源**：优先用画像的 ``action_intents``（方案 4 §4.1），没有画像时才退回
        ``route_text`` 关键词——"用户要求创建文件，模型只拿到读取工具"就是靠这条修掉的。
        """
        if not self.workspace_id or not self._uses_workspace_vocab(route_text):
            return capabilities
        from app.workspace.context import (
            WORKSPACE_COMMIT_CAPABILITIES,
            WORKSPACE_NAVIGATOR,
            WORKSPACE_OPERATION_CAPABILITIES,
            WORKSPACE_SANDBOX_CAPABILITIES,
            WORKSPACE_STAGE_WRITE_CAPABILITIES,
        )

        want_write, want_execute, want_commit = self._intent_scope(route_text)

        desired: list[Any] = list(
            await self._workspace_caps_for_group(frozenset({WORKSPACE_NAVIGATOR}))
        )
        if want_write:
            operations = await self._workspace_caps_for_group(WORKSPACE_OPERATION_CAPABILITIES)
            if operations:
                desired.extend(operations)
            else:
                # 兼容老客户端：没有原子操作工具时退回暂存写。
                desired.extend(
                    await self._workspace_caps_for_group(WORKSPACE_STAGE_WRITE_CAPABILITIES)
                )
        if want_execute:
            desired.extend(await self._workspace_caps_for_group(WORKSPACE_SANDBOX_CAPABILITIES))
        if want_commit:
            desired.extend(await self._workspace_caps_for_group(WORKSPACE_COMMIT_CAPABILITIES))

        injected = [item for item in desired if item.name not in {c.name for c in capabilities}]
        if not injected:
            return capabilities
        # 注入的**阶段工具**（读/写/执行/提交）本轮是"当前阶段明确要求"的工具，
        # 因此与核心工具一样属于强制项：它们不能把核心工具挤掉，也不该被 8 个名额
        # 反过来裁掉（旧写法 `keep = [...][: 8 - len(injected)]` 会在注入较多时把
        # 旧工具清空，包括 workspace_navigator）。
        injected_names = {str(item.name) for item in injected}
        keep = [item for item in capabilities if item.name not in injected_names]
        mandatory_hint = list(injected_names)
        window, _snapshot = apply_tool_window(
            [*injected, *keep],
            # 上限 = 8 个可选位 + 强制项数量（阶段工具 + 核心工具）。
            limit=8 + len(mandatory_hint),
            scene="office",
            catalog=[*capabilities, *injected],
            eligible=[*capabilities, *injected],
            extra_mandatory=mandatory_hint,
            mandatory_reason="stage_window",
            layer="react.workspace_stage",
        )
        # 诊断（用户排查"模型这次到底拿到了哪些工具"）：只记工具名，不记参数。
        logger.info(
            "[react] 工作区工具窗口注入: intents={} write={} execute={} injected={} window={}",
            list(self.action_intents),
            want_write,
            want_execute,
            [item.name for item in injected],
            [item.name for item in window],
        )
        return window

    #: 由 runner 的 ``__init__`` 赋值（这里只做类型提示）。
    user_id: str
    user_role: str
    workspace_id: str
    action_intents: tuple[str, ...]


__all__ = ["WorkspaceWindowMixin"]
