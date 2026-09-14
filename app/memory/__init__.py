"""Memory 域：对话记忆、长期记忆与近期任务召回。

**应用内一等公民域**（不是可复用包）：强依赖 pgvector / Redis / 加密密钥 / LLM。

```text
app/memory/
├─ conversation.py     对话记忆：窗口构建与摘要（"这轮对话还记得什么"）
├─ trim.py             对话裁剪：超长历史的降级策略
├─ office_tasks.py     近期办公任务索引与**保守召回**（"上次那份 PPT"）
├─ long_term/          长期记忆（跨会话）
│   extraction         从对话里抽取事实（L1）
│   retrieval          长期记忆检索
│   privacy            隐私分级与加密边界
│   profile            记忆画像（memory_profile）
│   lifecycle          生命周期（写入/合并/过期）
└─ repository.py       仓储 Port（编排层可依赖，见下）
```

两条**本域特有的**架构纪律：

1. **不反向依赖编排内核**：``office_tasks`` 过去直接 import
   ``app.agents.orchestration.models`` 的 ``Job`` / ``JobStatus`` / ``TaskStatus``。
   现在它只声明一个轻量 :class:`TaskSnapshot` Protocol（"我需要任务的哪几个字段"）
   并把状态比较改成**字符串值**——方案明确说"不必新建复杂 Port 抽象"。
   于是编排改枚举、改模型都不会再牵动记忆域。
2. **``repository`` 是允许被依赖的 Port**：它已登记在门禁的 ``DOMAIN_PUBLIC["memory"]``
   里——编排层需要它来落记忆，但只能通过这个稳定的仓储接口，不能直接碰记忆实现
   （规则 3 会拦下 ``app.agents.** → app.memory.<实现>``）。
"""
