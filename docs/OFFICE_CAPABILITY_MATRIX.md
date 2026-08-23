# 办公任务能力矩阵

> 维护日期：2026-08-23
> 说明：覆盖度分三级 —— ✅ 完整可用 / 🟡 部分可用（有明确边界） / ⬜ 未覆盖

## 一、业务场景 × 覆盖状态

| 场景 | 状态 | 实现链路 | 备注 |
| --- | --- | --- | --- |
| 公文/邮件撰写 | ✅ | `office_text` → compose_official_doc / compose_email | 邮件为"起草"，实际发送需用户确认 |
| 多风格改写 | ✅ | `office_text` → rewrite_text | |
| 会议纪要整理 | ✅ | `office_text` → meeting_minutes | |
| 长文摘要 | ✅ | `office_text` → summarize_text / document_analysis_flow | |
| 文档问答 | ✅ | `office_doc` analyze + RAG | 上传即挂载，随每条消息携带 |
| 信息抽取 | ✅ | `office_text` → extract_info | 文本级，输出 JSON |
| 竞品分析 | 🟡 | `office_research` → competitor_analysis（联网检索） | 无数据源集成，依赖搜索质量 |
| 本地文件检索 | 🟡 | 知识库 RAG + 项目索引 | 非全盘桌面搜索 |
| 个人日程/日历 | ✅ | `office_calendar` → calendar_manager（事件库 + ICS） | 导出 ICS 可导入 Outlook/Google/Thunderbird/苹果日历；系统日历打开导入 |
| 个人待办 | 🟡 | `office_todo` → todo_manager（本地 JSON） | 未接任务平台（滴答/微软待办） |
| 敏感词/合规审查 | ✅ | `office_text` → compliance_check | 规则词表 + LLM 复核 |
| 早晚报推送 | 🟡 | `office_research` → daily_report | 只生成内容，**无定时触发** |
| 发票/报销处理 | 🟡 | invoice_filter_flow + invoice_parse | 文本/LLM 抽取 + 图片扫描件 OCR 提取；非标准版式识别率取决于 OCR 质量 |
| 客服自动回复 | ✅ | `office_research` → customer_service | 文本生成，无工单系统对接 |
| 语音转文字+总结 | ✅ | speech_to_text（Whisper） | |
| 脚本批量处理 | ✅ | `office_script` → python_exec | 伪代码→代码两阶段，产物落输出目录 |
| 打开软件/文件/网页 | ✅ | `office_system` / desktop MCP | 客户端主进程定位并启动 |
| 发邮件（指定客户端） | ✅ | send_email + client 偏好 | 默认客户端 / 指定 Outlook/Thunderbird/Foxmail 等，选择自动保存 |
| 日程/日历文件解析 | 🟡 | `office_doc` analyze（ics） | 只读分析，不写回日历应用 |
| 扫描件/图片 OCR | ✅ | Docling + RapidOCR（png/jpg/jpeg/webp/bmp） | 图片上传走办公链路，OCR 后进结构/分析/发票抽取 |

## 二、文件类型 × 处理能力

| 类型 | 读/分析 | 结构化编辑 | 脚本处理 | 编辑操作集（示例） |
| --- | --- | --- | --- | --- |
| docx / docm | ✅ | ✅ | ✅ | 替换/新增/删除段落、替换全部、表格单元格、加行 |
| xlsx / xlsm | ✅ | ✅ | ✅ | 改单元格、批量替换、加行 |
| pptx / pptm | ✅ | ✅ | ✅ | 替换文本、加文本、删页、加页 |
| doc / xls / ppt / rtf | ✅（Windows + 本机 Office） | ✅（同上） | ✅ | 同对应新格式 |
| pdf / odt | ✅（Docling 解析） | ⬜ 只读 | 🟡（读内容后脚本加工） | 无 |
| md / txt / json / csv / yaml / toml / ini / log / xml | ✅ | ✅（文本重写/补丁） | ✅ | rewrite 全量 / search_replace |
| eml / ics | ✅ 分析 | ⬜ | 🟡 | 无 |
| 图片 / 扫描件（png/jpg/jpeg/webp/bmp） | ✅ OCR 提取文字 | ⬜（只读） | 🟡 | 仅读取/分析，不支持结构化编辑 |
| 音频 | ✅ 转写+总结 | — | — | speech_to_text |

> 编辑采用"缓冲 + 审核后落盘"：前端预览（Office 同款样式），用户保留/撤销后决定是否写回原文件。

## 三、编排能力

| 层 | 现状 |
| --- | --- |
| 意图分类 | 规则粗分类：模板 / 半结构 / 脚本 / 自由 |
| 模板库 | 6 个：文档分析、发票筛选、早晚报、文档对比、文档合并、文档翻译 |
| 模式库 | 2 个：ETL（读→转→写）、Router（读→条件→通知/审批） |
| 自由规划 | LLM Plan-then-Execute，Few-Shot 历史案例，失败回退规则版 |
| 文档兜底 | 规划结果未覆盖已上传文档时，强制补 office_doc 分析节点 |
| 记忆 | 任务级摘要（Redis，按会话最多 8 条）传给下一次规划 |
| 审批门控 | 高风险写节点（如发票高额邮件）可挂 approval，等待用户确认 |
| 执行引擎 | 持久化 asyncio DAG（默认）；Temporal 仅灰度静态只读任务 |

## 四、当前缺口（按优先级）

1. **定时/推送缺失**：早晚报、待办提醒只能当场生成，无定时任务触发。
2. **邮件只"起草"**：send_email 打开客户端草稿，真正发送需用户在邮箱确认（符合隐私与安全预期）。
3. **PDF/ODT 只读**：无法结构化编辑；复杂版式编辑（插图、样式、删除表格）不在 OP_SCHEMAS 内。
4. **OCR 依赖模型**：RapidOCR 首次使用需下载模型，离线环境图片 OCR 会失败（PDF 文本层优先走 pypdf）。
5. **真实系统未打通**：日历已通过 ICS 互通；OA、网盘、工单仍为文件级/文本级能力。
6. **全盘检索缺失**：仅知识库 + 项目索引，无桌面全文检索。

## 五、路线图建议

1. 图片 OCR 接入办公链路（✅ 已完成：png/jpg/jpeg/webp/bmp 走 Docling + RapidOCR，发票扫描件可抽取）
2. 真实日历（✅ 已完成：calendar_manager 事件库 + ICS 导出/导入 + 系统日历打开导入）
3. 定时任务框架（Celery beat 已有，补"定时触发办公模板 DAG"）
4. 邮件客户端选择偏好（✅ 已完成：对话内切换 + 设置页 + 服务端多端同步）
5. 扩展 OP_SCHEMAS（删除表格/样式/插图）与 PDF 编辑（Docling 结构回写）
6. 日历/邮箱双向同步适配器（按需：Outlook/Google 凭据授权后直接读写）
