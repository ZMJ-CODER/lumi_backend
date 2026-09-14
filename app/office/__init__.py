"""Office 域：办公文档的结构化编辑、渲染与可信上下文。

**应用内一等公民域**（不是可复用包）：强依赖 LLM / 文件系统 / 客户端 MCP / DB。

```text
app/office/
├─ docs.py         ← services/office_docs.py（1524 行：结构化编辑引擎 + 产物管理）
├─ context.py      ← services/office_context.py（可信办公资料上下文加载）
├─ skill_utils.py  ← services/office_skill_utils.py（LLM 调用封装 + 合规敏感词表）
├─ stream.py       ← services/office_stream.py（短生命周期文本流）
└─ render.py       ← services/document_renderer.py（新建文档的确定性渲染器）
```

**刻意留在 ``app/services/`` 的三个模块**（方案把它们列在 Office，但它们是跨角色/跨场景的）：

| 模块 | 为什么不进 office |
| --- | --- |
| ``prompts`` | 角色提示词服务：``chat_agent``、``react_runner``、``api/v1/prompts`` 都用，不属于办公 |
| ``scene_manager`` | 场景模板与知识范围映射：聊天/办公/代码场景共用 |
| ``response_format`` | 面向聊天气泡的排版约定：``orchestration/temporal`` 也在用 |

把它们塞进 office 只会**制造**新的跨域依赖（agents → office、orchestration → office），
与方案自己的规则 2/3 冲突。它们的归属在 P5 判断（更像 ``app/platform`` 的展示/提示词设施），
这段时间里它们在门禁里仍按"office"分类，因此既有的跨域违规**继续可见**、不会被悄悄藏起来。

``app/agents/roles/office/agents.py`` **留在 agents**：它是 Worker 角色（协议），不是领域逻辑。
"""
