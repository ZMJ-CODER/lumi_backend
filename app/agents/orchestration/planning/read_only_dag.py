"""显式声明的纯只读并行 DAG 编译。"""

from __future__ import annotations

import re
import time
import uuid

from app.agents.orchestration.models import TaskNode


_STAGE_RE = re.compile(r"(?ms)(?:^|[。；;\n])\s*(?:步骤|阶段|任务)?\s*([ABCD])\s*[：:]\s*(.*?)(?=(?:[。；;\n]\s*(?:步骤|阶段|任务)?\s*[ABCD]\s*[：:])|\Z)")
_EXTERNAL_OPERATION_RE = re.compile(r"(?:发送|发邮件|上传|下载|保存(?:为|到)|写入|创建|删除|修改|编辑|打开|启动|运行|关闭|调用工具|联网|网络搜索|检索知识库|读取(?:文件|文档)|处理附件|导出|转换|安装|部署)")
_SAFETY_SUFFIX_RE = re.compile(r"(?is)(?:\n|[。；;])\s*(?:只输出文本|不要调用工具|请勿调用工具).*$")


def _stage_id(label: str) -> str:
    return f"ro-{label.lower()}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _contains_dependency(text: str, labels: tuple[str, ...]) -> bool:
    normalized = text.upper().replace("、", " ").replace("，", " ")
    return any(marker in normalized for marker in ("依赖", "基于", "根据", "引用")) and all(re.search(rf"(?<![A-Z]){label}(?![A-Z])", normalized) for label in labels)


def _instruction(label: str, content: str, dependencies: tuple[str, ...] = ()) -> str:
    body = content.strip().rstrip("。；;")
    if dependencies:
        return f"只完成阶段 {label} 的纯文本分析：{body}\n只能使用前置阶段 {'、'.join(dependencies)} 提供的结果作为事实素材；资料不足时明确说明，不要补造事实。不得调用工具、访问网络、读取文件或改变外部状态。"
    return f"只完成阶段 {label} 的独立纯文本分析：{body}\n不得调用工具、访问网络、读取文件或改变外部状态。"


def build_explicit_read_only_dag(request: str) -> list[TaskNode] | None:
    """仅接受显式 A/B 并行、C 汇总、D 交付的无副作用契约。"""
    text = str(request or "").strip()
    if not text or "并行" not in text:
        return None
    stages = {label: _SAFETY_SUFFIX_RE.sub("", content).strip() for label, content in _STAGE_RE.findall(text) if content.strip()}
    if set(stages) != {"A", "B", "C", "D"} or any(_EXTERNAL_OPERATION_RE.search(content) for content in stages.values()):
        return None
    if not _contains_dependency(stages["C"], ("A", "B")) or not _contains_dependency(stages["D"], ("C",)):
        return None
    ids = {label: _stage_id(label) for label in "ABCD"}

    def node(label: str, dependencies: tuple[str, ...] = ()) -> TaskNode:
        return TaskNode(
            id=ids[label], name=f"阶段 {label}：只读分析", agent="direct_llm",
            params={"instruction": _instruction(label, stages[label], dependencies), "max_tokens": 600},
            depends_on=[ids[item] for item in dependencies],
            metadata={"route_channel": "direct_llm", "preserve_dependencies": True, "routing": {"reason": "explicit_read_only_parallel_dag", "stage": label, "side_effect_free": True}},
        )

    return [node("A"), node("B"), node("C", ("A", "B")), node("D", ("C",))]
