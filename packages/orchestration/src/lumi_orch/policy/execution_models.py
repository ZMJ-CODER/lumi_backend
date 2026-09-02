"""执行引擎默认策略的校验数据模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from lumi_orch.job_spec import ResourceClass, RetrySpec


class ExecutionDefault(BaseModel):
    timeout_seconds: int = Field(ge=1, le=86400)
    retry: RetrySpec


class ExecutionDefaultsDocument(BaseModel):
    model_config = {"extra": "forbid"}
    version: Literal[1]
    defaults: dict[ResourceClass, ExecutionDefault]
    concurrency: dict[str, int] = Field(default_factory=dict)
