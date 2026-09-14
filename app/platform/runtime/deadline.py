"""统一绝对截止时间：用 ``contextvars`` 传播，不逐层传参。

## 问题

"整条链路共享一个绝对截止时间"最怕实现成 ``func(deadline=deadline)`` 的层层透传：
每加一层调用就要改一次签名，漏一处就等于那条路径没有截止时间。改起来累人，
而且**漏掉的那条路径恰恰是线上最慢的那条**。

## 做法

``contextvars.ContextVar`` 在 Python 3.7+ 就是为此而生的：在入口
（Orchestrator / 请求处理）``set_deadline(...)`` 一次，之后任意深度的
``get_remaining_budget()`` 都能拿到同一份绝对截止时间，**函数签名一个都不用改**。

关键性质：

* 存的是**绝对时刻**，不是"剩余秒数"——链路上每一层各自减一次的做法会在并发/重试下漂移；
* **内部统一用 ``time.monotonic()``**（单调时钟）。能力 Broker 的
  ``CapabilityInvocation.deadline`` 契约就是 monotonic 秒，两边必须同轴才能比较；
  用 ``time.time()`` 还会被 NTP 校时/夏令时往前跳一下，把"还剩 30 秒"变成"已超时"或
  "还有一小时"。需要跨进程传输时才用 :func:`wall_clock_deadline` 换算；
* 每个 asyncio 任务拿到的是**自己的 context 副本**，因此同一进程内并发请求互不干扰；
  ``asyncio.create_task`` 会自动继承当前 context（这也是"不用传参"的机制本身）；
* 没有设置时返回 ``float("inf")``：**默认不限制**。让"忘记设截止时间"退化为旧行为，
  而不是把所有老路径一次性掐死（渐进接入，符合项目既有灰度风格）；
* :func:`with_deadline` 提供"缩窄"语义：子路径要更短的预算时取
  ``min(现有截止时间, 新的)``，绝不把上游给的更紧的预算放宽。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Iterator

#: 绝对截止时刻，基于 ``time.monotonic()``。``None`` / 未设置 = 不限制。
deadline_var: ContextVar[float | None] = ContextVar("lumi_request_deadline", default=None)
#: 预算来源标签（排障用：这次预算是入口给的还是某个子路径缩窄的）。
deadline_source_var: ContextVar[str] = ContextVar("lumi_request_deadline_source", default="")


def _clock() -> float:
    """单调时钟（唯一内部时间轴）。"""
    return time.monotonic()


@dataclass(frozen=True, slots=True)
class DeadlineToken:
    """一次 ``set_deadline`` 的还原凭证（**两个** ContextVar 都要还原）。

    只 reset ``deadline_var`` 会让 ``deadline_source_var`` 的标签泄漏到后续请求——
    排障时看到的原因会指向错误的来源，比没有标签更容易误导。
    """

    deadline_token: Token
    source_token: Token

    def reset(self) -> None:
        deadline_var.reset(self.deadline_token)
        deadline_source_var.reset(self.source_token)


class DeadlineExceeded(TimeoutError):
    """剩余预算已耗尽（或低于最小可发起阈值）。

    继承 ``TimeoutError``：调用方原本捕获超时的代码不用改就能接住它，
    因此接入截止时间不会把"超时"变成"未处理异常"。
    """

    def __init__(self, message: str = "请求预算已耗尽", *, remaining: float = 0.0, source: str = "") -> None:
        super().__init__(message)
        self.remaining = float(remaining)
        self.source = str(source or "")


def default_budget_seconds() -> float:
    """入口默认预算（``REQUEST_DEADLINE_SECONDS``；配置不可用时 300s）。"""
    try:
        from app.core.config import settings

        return max(0.1, float(getattr(settings, "REQUEST_DEADLINE_SECONDS", 300.0) or 300.0))
    except Exception:  # noqa: BLE001
        return 300.0


def min_budget_seconds() -> float:
    """"还剩这么点就别再发起外部调用"的阈值（留给收尾与落盘）。"""
    try:
        from app.core.config import settings

        return max(0.0, float(getattr(settings, "REQUEST_DEADLINE_MIN_BUDGET_SECONDS", 1.0) or 0.0))
    except Exception:  # noqa: BLE001
        return 1.0


def job_budget_seconds() -> float:
    """**任务级**预算（``JOB_DEADLINE_SECONDS``）；``<= 0`` = 关闭（不限制）。"""
    try:
        from app.core.config import settings

        return float(getattr(settings, "JOB_DEADLINE_SECONDS", 1800.0) or 0.0)
    except Exception:  # noqa: BLE001
        return 1800.0


def job_min_budget_seconds() -> float:
    """任务侧的收尾余量（``JOB_DEADLINE_MIN_BUDGET_SECONDS``）。"""
    try:
        from app.core.config import settings

        return max(0.0, float(getattr(settings, "JOB_DEADLINE_MIN_BUDGET_SECONDS", 1.0) or 0.0))
    except Exception:  # noqa: BLE001
        return 1.0


def install_job_deadline(
    *,
    budget: float | None = None,
    source: str = "job",
) -> DeadlineToken | None:
    """给**后台 Job 的一段执行**装上自己的预算（返回还原凭证；关闭时返回 ``None``）。

    为什么是**替换**而不是 :func:`with_deadline` 的"取更紧"：

    * 后台任务不是请求的一部分。HTTP 请求（尤其 SSE）结束时剩余预算可能只剩几秒，
      而任务还要继续跑几分钟——"取更紧"会让长任务在执行到一半时被请求预算掐死；
    * 反过来，后台任务此前**完全没有上限**（没有请求上下文时 ``get_remaining_budget()``
      是 ``inf``），一个卡住的 Provider 调用能把 worker 永久占住。

    因此：装上任务自己的绝对截止时间，同时**显式脱离**请求作用域。配置为 ``0`` 时
    仍然调用 :func:`clear_deadline` —— "不限制"必须是明确的，而不是继承来的。

    "一段执行"是刻意的口径：暂停/等待审批期间**不消耗**任务预算（每次
    :meth:`ExecutionLoopService.run` 重新锚定一次），否则审批慢一点就把任务判死。
    """
    limit = job_budget_seconds() if budget is None else float(budget)
    if limit <= 0:
        clear_deadline()
        return None
    return _set_absolute(_clock() + max(0.0, limit), source=source)


def set_deadline(
    timeout_seconds: float | None = None,
    *,
    source: str = "entry",
) -> DeadlineToken:
    """在入口设置绝对截止时间（默认取 ``REQUEST_DEADLINE_SECONDS``）。

    返回 :class:`DeadlineToken`，调用方用 ``token.reset()`` 还原——流式请求在同一进程里
    串行处理多个请求时必须还原，否则会把上一个请求的预算**和来源标签**泄漏给下一个。
    """
    budget = default_budget_seconds() if timeout_seconds is None else max(0.0, float(timeout_seconds))
    return _set_absolute(_clock() + budget, source=source)


def set_absolute_deadline(deadline: float | None, *, source: str = "entry") -> DeadlineToken:
    """直接设置绝对时刻（**必须是 monotonic 时间轴**；跨进程传入请先转 monotonic）。"""
    return _set_absolute(deadline, source=source)


def _set_absolute(deadline: float | None, *, source: str) -> DeadlineToken:
    deadline_token = deadline_var.set(None if deadline is None else float(deadline))
    source_token = deadline_source_var.set(str(source or ""))
    return DeadlineToken(deadline_token=deadline_token, source_token=source_token)


def clear_deadline() -> None:
    deadline_var.set(None)
    deadline_source_var.set("")


def get_deadline() -> float | None:
    """当前绝对截止时刻（monotonic 秒；未设置返回 ``None``）。

    可直接喂给 ``CapabilityInvocation.deadline`` —— 那个契约字段就是 monotonic 秒，
    因此不需要任何换算（这正是统一时间轴的价值）。
    """
    return deadline_var.get()


def get_remaining_budget() -> float:
    """剩余预算（秒）。未设置截止时间时返回 ``float("inf")``（= 不限制）。"""
    deadline = deadline_var.get()
    if deadline is None:
        return float("inf")
    return max(0.0, float(deadline) - _clock())


def wall_clock_deadline() -> float | None:
    """换算成 wall-clock 绝对时刻（``time.time()`` 轴），**仅供跨进程传输**。

    子进程/远端 Worker 收到后要么按自己的启动时间折算成相对预算，要么调用
    :func:`set_absolute_deadline` 前再换算回 monotonic——两套时钟不能直接比较
    （评审指出的 P0：``time.time()`` 与 ``time.monotonic()`` 混用）。
    """
    remaining = get_remaining_budget()
    if remaining == float("inf"):
        return None
    return time.time() + remaining


def request_budget_seconds(*, cap: float | None = None) -> float:
    """把剩余预算换算成"这次外部调用最多能用多久"。

    ``cap`` 是调用点自己的上限（例如某接口的 ``DEFAULT_DEADLINE_SECONDS``）：
    取 ``min(剩余预算, cap)``。剩余预算无限时返回 ``cap``；``cap`` 为 ``None`` 时
    返回剩余预算（可能仍是 ``inf``，调用方按"不限制"处理）。
    """
    remaining = get_remaining_budget()
    if cap is None:
        return remaining
    limit = max(0.001, float(cap))
    if remaining == float("inf"):
        return limit
    return max(0.0, min(remaining, limit))


def effective_min_budget_seconds() -> float:
    """"别再发起外部调用"的阈值，按**当前预算的作用域**取。

    任务级预算的收尾余量与请求级不同（任务要落库/发事件/写结果），因此不能用同一个
    阈值。作用域由 ``deadline_source_var`` 判定（``install_job_deadline`` 写 "job…"）。
    """
    if is_job_scope():
        return job_min_budget_seconds()
    return min_budget_seconds()


def is_job_scope() -> bool:
    """当前预算是不是**任务级**（后台 Job 的一段执行）。"""
    return str(deadline_source_var.get() or "").startswith("job")


def ensure_budget(*, minimum: float | None = None, what: str = "") -> float:
    """发起外部调用前的统一闸门：预算不足直接抛 :class:`DeadlineExceeded`。

    返回**本次可用的秒数**（调用方直接拿它当 timeout），因此常见写法是::

        timeout = ensure_budget(what="llm.chat")

    这样"检查预算"和"取超时值"是同一个动作，不会出现"检查了但没用上"。
    """
    floor = effective_min_budget_seconds() if minimum is None else max(0.0, float(minimum))
    remaining = get_remaining_budget()
    if remaining < floor:
        raise DeadlineExceeded(
            f"请求预算不足，已停止{'：' + what if what else ''}",
            remaining=remaining,
            source=deadline_source_var.get(),
        )
    return remaining


def has_budget(*, minimum: float | None = None) -> bool:
    """软检查（不抛错）：还有没有预算继续做下一步。"""
    floor = effective_min_budget_seconds() if minimum is None else max(0.0, float(minimum))
    return get_remaining_budget() >= floor


@contextmanager
def with_deadline(
    timeout_seconds: float | None = None,
    *,
    source: str = "scoped",
    cap_only: bool = True,
) -> Iterator[float]:
    """在子路径上**缩窄**预算，退出时自动还原（含来源标签）。

    ``cap_only=True``（默认）取 ``min(现有截止时间, now + timeout)``：子路径永远不能
    把上游给的更紧预算放宽——放宽就等于绕过入口设定的总预算。
    """
    budget = default_budget_seconds() if timeout_seconds is None else max(0.0, float(timeout_seconds))
    proposed = _clock() + budget
    current = deadline_var.get()
    target = proposed if current is None else (min(float(current), proposed) if cap_only else proposed)
    token = _set_absolute(target, source=source)
    try:
        yield get_remaining_budget()
    finally:
        token.reset()


def budget_snapshot() -> dict[str, Any]:
    """当前预算的可观测快照（日志/排障用）。"""
    deadline = deadline_var.get()
    remaining = get_remaining_budget()
    return {
        "deadline_monotonic": deadline,
        # 换算成 wall-clock 只用于**人看**（日志可读），不参与任何判定。
        "deadline_wall_clock": wall_clock_deadline(),
        "remaining_seconds": None if remaining == float("inf") else round(remaining, 3),
        "unbounded": remaining == float("inf"),
        "source": deadline_source_var.get(),
        # 作用域：后台任务与 HTTP 请求的收尾余量不同，排障时要能一眼区分。
        "scope": "job" if is_job_scope() else ("request" if deadline is not None else "unbounded"),
        "min_budget_seconds": effective_min_budget_seconds(),
    }


__all__ = [
    "DeadlineExceeded",
    "DeadlineToken",
    "budget_snapshot",
    "clear_deadline",
    "deadline_source_var",
    "deadline_var",
    "default_budget_seconds",
    "effective_min_budget_seconds",
    "ensure_budget",
    "get_deadline",
    "get_remaining_budget",
    "has_budget",
    "install_job_deadline",
    "is_job_scope",
    "job_budget_seconds",
    "job_min_budget_seconds",
    "min_budget_seconds",
    "request_budget_seconds",
    "set_absolute_deadline",
    "set_deadline",
    "wall_clock_deadline",
    "with_deadline",
]
