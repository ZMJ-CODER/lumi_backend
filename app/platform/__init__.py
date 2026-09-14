"""``app/platform``：横切技术设施（P5 从 ``app/core`` 迁出）。

**只有"纯技术设施"能进这里**。方案 §P5 要求迁移前对每个文件做四分类标注：

+--------------------------------+------------------------------------------+
| 分类                            | 例子与去向                                |
+================================+==========================================+
| **纯技术设施**（进 platform）    | LLM 客户端封装、安全/加密/限流、截止时间、  |
|                                | 执行器、灰度开关、韧性、读缓存、网络        |
+--------------------------------+------------------------------------------+
| **业务策略**（先迁移、不合并）    | ``model_capability_router`` / ``model_roles``：|
|                                | 它们做"模型选路"，与能力选路同族，长期应与  |
|                                | ``lumi_capability`` 收进同一包（另行立项）  |
+--------------------------------+------------------------------------------+
| **领域服务**（不该进这里）        | 已经在 P4 搬进 ``app/{knowledge,workspace,  |
|                                | memory,office}``                          |
+--------------------------------+------------------------------------------+
| **API 适配**（留在 app/api）     | 路由与请求/响应形状                        |
+--------------------------------+------------------------------------------+

目录按**关注点**切分，而不是把 core 原样搬过来::

    model/      模型调用与选路（llm / llm_config / model_catalog / model_plan /
                model_response / model_capability_router / model_roles）
    security/   安全与限流（security / security_hardening / crypto / throttling /
                resource_policy / agent_security）
    runtime/    运行时设施（deadline / executors / feature_flags / resilience /
                read_view_cache）
    network/    受控网络客户端

``app/core`` 保留的只有"每个模块都可能要用、且不带业务语义"的那几个：
``config`` / ``database`` / ``redis`` / ``exceptions`` / ``exception_handlers`` /
``error_mapping`` / ``deps``。

**可观测性不放这里**：``observability`` 与 ``logging`` 迁到了 ``app/observability/``
（方案 §二 把 observability 列为独立的 ★P2-A 目录；审计/日志语义统一是另一个立项）。
"""
