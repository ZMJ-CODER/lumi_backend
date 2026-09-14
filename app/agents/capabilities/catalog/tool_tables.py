"""工具注册表的**静态词表**（四分类中的"配置加载"，纯数据、零依赖）。

这一整段是"人写死的配置"——意图副作用、
意图能力、档位例外、遗留工具、能力档位、副作用档位、执行环境词汇。它没有任何运行时依赖，
也不做任何判定，因此可以先于其它段落安全拆出。

**改这里的门槛**：这些表是**行为的一部分**（档位、窗口、审批都从它们派生）。
新增工具时优先让它**自述**（``Tool.capability`` / ``resource_type`` / manifest 的
副作用声明），只有在"必须覆盖既有静态行为"时才动本文件。

注意取值必须覆盖契约的 ``SideEffectKind``（``none/read/write/delete/execute/network/external``）：
漏一个就会掉进"默认 routine"，把 ``external``（对外发布，不可逆）误判成普通写入。
"""

from __future__ import annotations

# ── [配置加载] 静态词表（本模块整体属于这一类：纯数据，不做任何判定）

#: 动作意图 → 触发的副作用集合（与 ``CapabilityDescriptor.side_effects`` 同词表）。
#: 这是"动作窗口派生"的桥梁：注册表不知道"MODIFY 该给哪个工具"，但知道
#: "哪些工具的副作用属于写"，再把两者对齐。
INTENT_SIDE_EFFECTS: dict[str, frozenset[str]] = {
    "READ": frozenset({"read"}),
    "SEARCH": frozenset({"read"}),
    "CREATE": frozenset({"write"}),
    "MODIFY": frozenset({"write"}),
    "DELETE": frozenset({"delete"}),
    "MOVE": frozenset({"write", "delete"}),
    "EXECUTE": frozenset({"execute"}),
    # SEND / PUBLISH 属于"对外发布"，本地能力目录里没有对应副作用：
    # 派生结果为空，静态表里的 send_email 由兜底提供（见 ``action_window``）。
    "SEND": frozenset({"publish"}),
    "PUBLISH": frozenset({"publish"}),
}

#: 动作意图 → **候选能力序列**（顺序即"最相关在前"）。
#:
#: 为什么需要它而不是"副作用匹配 + 全收"：同一能力下挂着十几个工具别名
#: （``workspace_list``/``stat``/``search``/``read``…），按副作用收会把 12 个同义工具
#: 一起塞进窗口，把名额占满并让模型无从选择。窗口的价值在于**收窄**，因此每个意图只取
#: 该意图对应的那几个规范入口；具体工具名由 ``CAPABILITY_TOOL_MAP`` 反查（单一事实源），
#: 因此插件换实现时窗口会自动跟着变。
INTENT_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "READ": ("workspace.read",),
    "SEARCH": ("workspace.read",),
    "CREATE": ("workspace.write",),
    # 改内容：读入口 + 编辑入口（静态表同构）。
    "MODIFY": ("workspace.read", "workspace.edit"),
    "MOVE": ("workspace.read", "workspace.move"),
    "DELETE": ("workspace.read", "workspace.delete"),
    "EXECUTE": ("code.execute",),
}

#: 只读意图：派生的动作窗口里额外带上"读取入口"，因为改文件之前必须先读到内容。
#: 这不是猜——``ACTION_TOOL_WINDOW`` 的 MODIFY/DELETE/MOVE 也都是
#: ``workspace_navigator`` + 具体操作工具。
READ_INTENT_PREFIXES: frozenset[str] = frozenset({"MODIFY", "DELETE", "MOVE"})

#: 允许"派生补位"的能力族：只收**工作区读/写**族（静态表里恰好 1–2 个工具）。
#:
#: ``code.execute`` 刻意**不在**这里：执行窗口按子动作分叉
#: （prepare/run/output_read/reset），客户端原始名本身就对应显式子动作，
#: 硬塞一个"规范入口"反而让模型不知道用哪个——这类族以静态表（那份子动作清单）为准。
SUPPLEMENTAL_CAPABILITIES: frozenset[str] = frozenset(
    {"workspace.read", "workspace.write", "workspace.edit", "workspace.move", "workspace.delete"}
)

#: 契约副作用名 → **意图词表**的副作用名。
#:
#: 两套词汇不同：契约 ``SideEffectKind`` 用 ``external``（"对外发送/发布等不可逆外部
#: 效果"），而意图表（与静态 ``ACTION_TOOL_WINDOW`` 同构）用 ``publish``。没有这层桥接，
#: ``SEND``/``PUBLISH`` 的派生补位永远匹配不上"对外发布"能力——那不是保守，是漏。
#: ``network`` 刻意**不映射**：访问网络不等于对外发布，静态窗口里也没有它。
_EFFECT_TO_INTENT: dict[str, str] = {
    "external": "publish",
}

#: 审批档位（与 ``skills/approval_policy.py`` 的三档语义一致）。
TIER_AUTO = "auto"
TIER_ROUTINE = "routine"
TIER_CRITICAL = "critical"

#: **工具级例外**（覆盖能力级档位）。每一条都是"同一个能力下不同工具的档位不同"的真实事实：
#:
#: * ``workspace_stage_write`` / ``workspace_stage_delete`` / ``sandbox_prepare`` /
#:   ``sandbox_reset`` 只动**暂存区/沙箱**，不碰真实工作区 → A 档；
#: * ``workspace_commit`` 才把暂存写入真实工作区 → B 档；
#: * ``workspace_rollback`` **不可逆** → C 档（即使它是 workspace.write 能力）；
#: * ``workspace_diff`` 只读 → A 档（虽然属于 git.operations）。
#:
#: 这些例外以前写在 ``approval_policy.py`` 的三个私有 frozenset 里（代码表）；
#: 现在它们是注册表数据的一部分，新增工具只需声明 ``risk_tier`` 或加一条例外。
TOOL_TIER_OVERRIDES: dict[str, str] = {
    "workspace_stage_write": TIER_AUTO,
    "workspace_stage_delete": TIER_AUTO,
    "workspace_commit": TIER_ROUTINE,
    "workspace_rollback": TIER_CRITICAL,
    "workspace_diff": TIER_AUTO,
    "sandbox_prepare": TIER_AUTO,
    "sandbox_reset": TIER_AUTO,
    "workspace_navigator": TIER_AUTO,
    "workspace_read": TIER_AUTO,
    "workspace_code_scan": TIER_AUTO,
    "workspace_catalog": TIER_AUTO,
    "workspace_list": TIER_AUTO,
    "workspace_stat": TIER_AUTO,
    "workspace_search": TIER_AUTO,
    "workspace_content_extract": TIER_AUTO,
    "workspace_write": TIER_ROUTINE,
    "workspace_edit": TIER_ROUTINE,
    "workspace_move": TIER_ROUTINE,
    "workspace_delete": TIER_ROUTINE,
    "python_exec": TIER_ROUTINE,
    "run_in_sandbox": TIER_ROUTINE,
    "sandbox_run": TIER_ROUTINE,
    "create_office_document": TIER_ROUTINE,
}

#: **遗留/辅助工具**：不在工作区/沙箱能力域里，注册表**不派生**它们的档位，
#: 一律由既有审批引擎的三张静态词表决定（`Read` / `Bash` / `office_doc_read` 等）。
#:
#: 为什么显式列出而不是"派生不出来就回落"：这些工具的档位是**安全边界**，
#: "偶然派生对"和"明确不派生"是两件事。列出来之后，`risk_tier_of` 的返回 None
#: 就是有意的契约，而不是兜底副产物。
LEGACY_TIER_TOOLS: frozenset[str] = frozenset(
    {
        "read",
        "write",
        "edit",
        "glob",
        "grep",
        "bash",
        "shell",
        "filestat",
        "notebookedit",
        "openfile",
        "read_document",
        "office_doc_read",
        "office_doc_edit",
        "office_doc_analyze",
        "inspect_document_set",
        "get_project_context",
        "todo_manager",
        "send_email",
        "git_reset_hard",
        "git_clean",
        "git_force_push",
        "delete_workspace_root",
        # 服务端**实现名**（不是模型可见的工具名）：静态侧对它们走"workspace_ 前缀 →
        # 例行确认 / 都不认识 → 始终确认"的保守兜底，派生侧则会按能力给出更细的档位。
        # 两侧都说得通，但既然要保证"切开关不改行为"，就明确让静态词表继续负责它们。
        # 待实现名从审批链路里退场后，这两条可以删掉。
        "workspace_reader",
        "code_edit",
    }
)

#: 能力 → 审批档位（**唯一**的能力级映射；工具可直接声明 ``risk_tier`` 覆盖它）。
#:
#: 判据是"这一步会不会立刻破坏真实工作区"：
#:
#: * 只读/暂存/沙箱准备 → ``auto``（不碰真实文件）；
#: * 真实写入/提交/执行 → ``routine``（"帮我确认"关闭时在写入前确认一次）；
#: * 不可逆或对外发布 → ``critical``（即使开启"帮我确认"也必须确认）。
CAPABILITY_TIER: dict[str, str] = {
    "workspace.read": TIER_AUTO,
    "code.scan": TIER_AUTO,
    "workspace.write": TIER_ROUTINE,
    "workspace.edit": TIER_ROUTINE,
    "workspace.move": TIER_ROUTINE,
    "workspace.delete": TIER_ROUTINE,
    "code.execute": TIER_ROUTINE,
    # git 操作含提交/推送面，按"始终确认"处理。
    "git.operations": TIER_CRITICAL,
    "artifact.create": TIER_ROUTINE,
}

#: 副作用 → 档位（能力不在上面的表里时用；插件新能力走这条路）。
#:
#: 取值必须覆盖**契约的 ``SideEffectKind``**（``none/read/write/delete/execute/network/external``）：
#: 漏一个就会掉进"默认 routine"，把 ``external``（对外发送/发布，不可逆）误判成普通写入。
SIDE_EFFECT_TIER: dict[str, str] = {
    "read": TIER_AUTO,
    "write": TIER_ROUTINE,
    "execute": TIER_ROUTINE,
    "delete": TIER_ROUTINE,
    # 网络访问本身不可逆性有限，但会把数据带出本机 → 至少例行确认。
    "network": TIER_ROUTINE,
    # 对外发布/发送：收不回来，按始终确认。
    "external": TIER_CRITICAL,
    # 兼容``INTENT_SIDE_EFFECTS`` 里的合成副作用名（SEND/PUBLISH 意图）。
    "publish": TIER_CRITICAL,
}

#: 执行环境词汇（与 ``ToolCapability.environment`` 一致）。
ENV_SERVER = "server"
ENV_CLIENT = "client"
ENV_SANDBOX = "sandbox"
