# 路由置信度与校准

## 当前含义

L1 规则置信度来自确定性特征，不是概率模型。L2 的 `top_score`、
`second_score` 与 `score_margin` 是合法工具池中的混合排序分数，也不是概率。
因此 L2 只能使用经标注集验证后的相对阈值，不能直接套用概率阈值。

L3 的 `confidence_hint` 是模型自报提示，不是经校准的正确率。它只会写入
路由元数据用于分析，不能提高 L1 的 `RoutingConfidence`，不能绕过澄清、
权限、审批或执行编译门禁。

## L2 歧义门

候选池先按场景、角色、权限、写开关和运行时可用性过滤。之后才计算首名、
次名和 margin。

- margin 小于 `SKILL_CANDIDATE_MARGIN_THRESHOLD` 时标记 `ambiguous`；
- 明确工具意图或写入/外部候选歧义时，不向模型提供工具，改为澄清；
- 普通只读歧义保留有限候选，由模型结合工具适用/禁用契约选择，同时记录遥测；
- 冲突、替代和不可用能力不参与可执行候选竞争。

默认值 `3.0` 只是保守起点，必须用标注集的 margin 分桶正确率调整。

## L3 离线校准

`app.agents.orchestration.confidence_calibration` 提供不依赖供应商的：

- binary temperature scaling；
- ECE；
- Brier score。

可用 `scripts/evaluate_routing_confidence.py` 对标注 JSONL 生成报告：

```powershell
.venv\Scripts\python.exe scripts\evaluate_routing_confidence.py `
  artifacts\routing-eval\labelled.jsonl `
  --output artifacts\routing-eval\confidence-report.json
```

每行至少提供 `correct: true/false`；可选提供顶层或嵌套在 `selection`、
`metadata.routing` 中的 `top_score`、`second_score`、`score_margin`、
`confidence_hint`。报告包含样本数、margin 分桶准确率、ECE、Brier、温度参数
及校准后的指标。没有真实标签的遥测只能做分布统计，不能用于拟合阈值。

评测样本至少记录：路由标签、L3 主意图是否正确、`confidence_hint`、L2 首次名
分数和 margin、是否澄清、是否发生误调用。按训练/留出集划分：只在训练集拟合
temperature，在留出集报告 ECE、Brier、coverage 与 selective accuracy。校准结果
未达到可接受标准前，`confidence_hint` 始终不得作为自动放行依据。

## Logprobs

供应商若稳定支持标签 token 的 logprobs，可将其加入离线特征并重新校准；不能
将供应商私有字段作为路由正确性的基础依赖。不同 OpenAI-compatible 实现的
logprobs 语义、结构和可用性并不一致。
