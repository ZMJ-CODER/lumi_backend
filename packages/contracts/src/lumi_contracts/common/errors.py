"""统一错误信封与错误码。

约束（与方案一致）：

* 错误码是**跨模块契约**的一部分，新增必须登记，不能随手拼字符串；
* 无法完成契约转换时返回 ``UNSUPPORTED_CONTRACT_VERSION``，禁止静默丢字段；
* 错误信封只描述"失败了什么、能不能重试、下一步怎么办"，不含业务正文。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from lumi_contracts.common.version import ContractVersion


class ContractErrorCode(StrEnum):
    """跨边界稳定错误码（框架级 + 校验级）。"""

    # ── 契约/校验 ──
    INVALID_CONTRACT = "INVALID_CONTRACT"
    UNSUPPORTED_CONTRACT_VERSION = "UNSUPPORTED_CONTRACT_VERSION"
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    OUTPUT_SCHEMA_MISSING = "OUTPUT_SCHEMA_MISSING"
    # ── 编排/运行 ──
    ROUTE_REQUIRED = "ROUTE_REQUIRED"
    EXECUTION_REQUIRED = "EXECUTION_REQUIRED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    # ── 权限/安全 ──
    PERMISSION_DENIED = "PERMISSION_DENIED"
    SIDE_EFFECT_FORBIDDEN = "SIDE_EFFECT_FORBIDDEN"


class ErrorEnvelope(BaseModel):
    """统一错误信封：稳定错误码 + 人类可读信息 + 重试与自我修正提示。"""

    code: str
    message: str = ""
    retryable: bool = False
    # 给模型/调用方的"下一步"提示（例如"先 list 再 read"）。
    suggested_action: str = ""
    # 附加结构化细节（字段名、校验错误等）；不得放业务正文或凭据。
    details: dict = Field(default_factory=dict)

    def with_contract(self, version: ContractVersion | str) -> "ErrorEnvelope":
        """把契约版本登记进 details，便于跨版本排障。"""
        return self.model_copy(
            update={"details": {**self.details, "contract": str(version)}}
        )


class ContractError(Exception):
    """契约级异常：可转换为 ``ErrorEnvelope``，也可向上抛出由边界捕获。"""

    def __init__(
        self,
        code: ContractErrorCode | str,
        message: str = "",
        *,
        retryable: bool = False,
        suggested_action: str = "",
        details: dict | None = None,
    ) -> None:
        self.code = str(code)
        self.message = str(message or self.code)
        self.retryable = bool(retryable)
        self.suggested_action = str(suggested_action or "")
        self.details = dict(details or {})
        super().__init__(self.message)

    def to_envelope(self) -> ErrorEnvelope:
        return ErrorEnvelope(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            suggested_action=self.suggested_action,
            details=dict(self.details),
        )


__all__ = ["ContractError", "ContractErrorCode", "ErrorEnvelope"]
