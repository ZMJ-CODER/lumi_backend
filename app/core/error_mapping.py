"""跨边界错误到统一业务结果的映射。"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.orchestration.state_machine.errors import classify_error


@dataclass(frozen=True)
class PublicTaskError:
    code: str
    message: str
    retryable: bool


def map_task_error(error: BaseException | str) -> PublicTaskError:
    """把内部异常映射为稳定的用户可见错误，不泄露堆栈或供应商细节。"""
    exc = error if isinstance(error, BaseException) else RuntimeError(str(error))
    info = classify_error(exc)
    code = str(getattr(info, "code", "TASK_EXECUTION_ERROR") or "TASK_EXECUTION_ERROR")
    category = str(getattr(info, "category", "") or "").lower()
    text = str(error)
    # 编排/业务代码异常不能伪装成模型不可用，返回明确的内部错误码。
    if isinstance(exc, (NameError, UnboundLocalError, AttributeError, TypeError, KeyError)):
        return PublicTaskError(
            "INTERNAL_ERROR",
            "任务执行器内部发生错误，请稍后重试；若持续出现请联系管理员。",
            False,
        )
    if "balance" in text.lower() or "insufficient" in text.lower():
        return PublicTaskError("MODEL_INSUFFICIENT_BALANCE", "模型账户余额不足，任务未完成。", False)
    if "json" in text.lower() or "dsml" in text.lower() or "tool_call" in text.lower():
        return PublicTaskError("MODEL_TOOL_CALL_INVALID", "模型工具调用格式无效，任务未完成。", True)
    if "timeout" in category or "timeout" in code.lower():
        return PublicTaskError(code, "任务执行超时，可稍后重试。", True)
    if "connection" in category or "provider" in code.lower() or "unavailable" in code.lower():
        return PublicTaskError(code, "模型或外部服务暂时不可用，任务未完成。", True)
    return PublicTaskError(code, "任务执行失败，未产生完整结果。", False)


__all__ = ["PublicTaskError", "map_task_error"]
