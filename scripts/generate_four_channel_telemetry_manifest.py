"""Generate safe, ready-to-submit four-channel routing telemetry manifests.

The generated files contain explicit numbered checklists.  Submitting a file
creates one route-decision event per item when the manifest is compiled.  The
items are intentionally synthetic and should be cancelled immediately after
submission; they are not a substitute for production traffic measurement.
"""

from __future__ import annotations

from pathlib import Path


OUT_DIR = Path("tests/fixtures")


DIRECT = [
    "把这句话改写得更简洁：项目将在下周发布。",
    "解释一下什么是幂等性，用三句话说明。",
    "为新员工写一句友好的欢迎语。",
    "把下面标题改成正式语气：版本更新啦。",
    "用表格列出计划、执行、复盘三个阶段的区别。",
    "将这段说明压缩成不超过五十字的摘要。",
    "给‘稳定性优先’写三个宣传口号。",
    "把这句话翻译成英文：系统已准备就绪。",
    "解释平均响应时间和 p99 的区别。",
    "为技术周报拟一个清晰的标题。",
    "列出计划、执行、复盘三个阶段的要点。",
    "写一段不超过三句的项目简介。",
    "比较同步调用和异步调用的概念差异。",
    "把‘请尽快处理’改成礼貌且明确的表达。",
    "给数据库连接池写一个通俗比喻。",
    "生成三个关于缓存的学习问题。",
    "把下面句子改成被动语态：服务完成了部署。",
    "用一句话定义回归测试。",
    "为压测报告写一个中性的结论句。",
    "把这段话改写成适合 README 的说明。",
]

SCRIPT = [
    "将 example.txt 转换为 example.csv（仅演练，不执行写入）。",
    "把 sample.csv 导出为 sample.xlsx（仅演练，不执行写入）。",
    "将 notes.md 转换为 notes.pdf（仅演练，不执行写入）。",
    "批量处理示例目录中的 .txt 并统计行数（仅演练）。",
    "把 table.xlsx 另存为 table.csv（仅演练，不执行写入）。",
    "将 data.json 转换为 data.csv（仅演练）。",
    "导出 report.xlsx 中的表格为 report.csv（仅演练，不执行写入）。",
    "批量处理三份 .txt 并统一扩展名（仅演练）。",
    "把 source.xlsx 转换为 source.tsv（仅演练，不执行写入）。",
    "将 summary.pdf 转成 summary.txt（仅演练，不执行写入）。",
    "把 meeting.docx 导出为 meeting.txt（仅演练）。",
    "批量处理示例 .csv 并检查列数一致性（仅演练）。",
    "将 slides.pptx 转换为 slides.pdf（仅演练，不执行写入）。",
    "把 access.log 导出为按日期分组的 access.csv（仅演练）。",
    "将 source.tsv 转为 source.xlsx（仅演练，不执行写入）。",
    "批量处理示例 .txt 并去除空行（仅演练）。",
    "把 archive.csv 另存为 archive.tsv（仅演练，不执行写入）。",
    "将 metrics.csv 导出为 metrics.json（仅演练）。",
    "转换 report.docx 并保留原始名称（仅演练）。",
    "运行示例 .csv 转换脚本并返回计划摘要（仅演练，不执行）。",
]

RAG = [
    "从知识库资料中查询项目的上线日期。",
    "根据知识库资料回答当前版本包含哪些主要功能。",
    "在资料库中查找数据库连接池的配置说明。",
    "根据知识库资料回答发布流程中的审批条件。",
    "查询知识库中关于缓存失效策略的说明。",
    "根据知识库资料总结故障复盘的根因。",
    "在资料库中找出负责 API 监控的角色。",
    "从知识库资料查询默认的压测并发数。",
    "查找已授权资料中关于 Redis 的容量建议。",
    "根据知识库内容回答备份保留周期是多少。",
    "根据知识库资料提取数据库迁移步骤。",
    "在知识库中检索 p99 长尾问题的处理建议。",
    "查询知识库资料中健康检查接口的路径。",
    "根据知识库资料列出部署前检查项。",
    "从资料库找出生产环境的日志保留要求。",
    "根据知识库资料查找四级路由的定义。",
    "查询知识库资料中 worker 数量的配置方式。",
    "根据知识库资料提取 API 限流的默认阈值。",
    "根据知识库资料总结冷启动加热流程。",
    "在资料库中定位 RRF 参数 k 的说明。",
]

AGENT = [
    "先读取系统状态，再核对服务是否健康（仅演练，不执行修改）。",
    "先检查配置，再生成一份变更前检查结果（仅演练）。",
    "核对当前运行状态并给出需要人工确认的事项（仅演练）。",
    "先分析日志，再判断是否需要重启服务（仅演练，不执行重启）。",
    "检查数据库连接状态，然后生成诊断摘要（仅演练）。",
    "先读取任务状态，再规划后续处理步骤（仅演练）。",
    "先核验服务配置与要求，再输出差异（仅演练）。",
    "先检查缓存状态，再给出是否需要预热的建议（仅演练）。",
    "分析一组运行指标并提出排查顺序（仅演练，不执行操作）。",
    "先确认当前环境，再编排一次只读诊断流程（仅演练）。",
    "核对部署清单与运行状态，最后生成检查报告（仅演练）。",
    "先读取压测摘要，再判断是否存在资源瓶颈（仅演练）。",
    "先检查 API 和数据库状态，再总结异常关联（仅演练）。",
    "验证配置项、日志和连接数，输出综合诊断（仅演练）。",
    "先查看任务依赖，再给出安全的执行顺序（仅演练）。",
    "审查当前路由配置并列出需要人工审批的变更（仅演练）。",
    "读取监控指标后核对是否达到发布条件（仅演练）。",
    "先检查服务状态，再模拟一次故障处置计划（仅演练）。",
    "先分析多项测试结果，再生成下一步行动清单（仅演练）。",
    "核对系统状态、配置和日志后给出结论（仅演练）。",
]


def write_manifest(path: Path, items: list[str]) -> None:
    lines = [
        "执行下面的任务清单（仅用于四级路由遥测；提交后请立即取消，不要执行具体操作）：",
    ]
    lines.extend(f"{index}. {item}" for index, item in enumerate(items, start=1))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    groups = [DIRECT, SCRIPT, RAG, AGENT]
    # Five requests of 20 items each keeps every API request well below the
    # current 2,000-character request limit while yielding 100 events total.
    manifests = list(groups)
    mixed = []
    for i in range(5):
        mixed.extend([DIRECT[i], SCRIPT[i], RAG[i], AGENT[i]])
    manifests.append(mixed)
    for index, items in enumerate(manifests, start=1):
        write_manifest(OUT_DIR / f"four_channel_telemetry_manifest_{index:02d}.txt", items)
    print(f"已生成 {len(manifests)} 份清单，共 {sum(map(len, manifests))} 条任务：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
