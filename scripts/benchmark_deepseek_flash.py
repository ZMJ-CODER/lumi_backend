"""Read-only stepped concurrency benchmark for the configured DeepSeek Flash model.

It intentionally uses a two-token prompt and caps completion at four tokens.
The default 32 + 128 + 256 requests stay well below the 50,000-token budget,
even if every response consumes the full completion allowance.

Run with:
    .\\.venv\\Scripts\\python.exe scripts\\benchmark_deepseek_flash.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections import Counter
from dataclasses import dataclass

import psutil
from langchain_core.messages import HumanMessage

from app.agents.langchain.models import get_chat_model
from app.core.config import settings


STAGES = (32, 128, 256)
MAX_COMPLETION_TOKENS = 4
PROMPT = "只回复 OK"


@dataclass
class Result:
    latency_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    error: str | None = None


async def _call(model) -> Result:
    started = time.perf_counter()
    try:
        response = await model.ainvoke([HumanMessage(content=PROMPT)])
        usage = getattr(response, "usage_metadata", None) or {}
        return Result(
            latency_ms=(time.perf_counter() - started) * 1000,
            prompt_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
            completion_tokens=usage.get("output_tokens") or usage.get("completion_tokens"),
        )
    except Exception as exc:  # A failed request must not cancel its stage peers.
        return Result(
            latency_ms=(time.perf_counter() - started) * 1000,
            prompt_tokens=None,
            completion_tokens=None,
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )


async def _sample(stop: asyncio.Event) -> dict[str, float | int | None]:
    system_cpu: list[float] = []
    process_cpu: list[float] = []
    process_memory: list[int] = []
    process = psutil.Process(os.getpid())
    psutil.cpu_percent(None)
    process.cpu_percent(None)
    while not stop.is_set():
        await asyncio.sleep(0.25)
        system_cpu.append(psutil.cpu_percent(None))
        try:
            process_cpu.append(process.cpu_percent(None))
            process_memory.append(process.memory_info().rss)
        except psutil.Error:
            pass
    return {
        "system_cpu_avg_percent": round(statistics.mean(system_cpu), 2) if system_cpu else None,
        "system_cpu_max_percent": round(max(system_cpu), 2) if system_cpu else None,
        "benchmark_process_cpu_max_one_core_percent": round(max(process_cpu), 2) if process_cpu else None,
        "benchmark_process_memory_peak_mib": round(max(process_memory) / 1024 / 1024, 2) if process_memory else None,
        "sample_count": len(system_cpu),
    }


async def _stage(concurrency: int) -> dict:
    model = await get_chat_model(
        scene=None,
        user_id=None,
        base_url=settings.DEEPSEEK_BASE_URL,
        api_key=settings.DEEPSEEK_API_KEY,
        model="deepseek-v4-flash",
        temperature=0,
        max_tokens=MAX_COMPLETION_TOKENS,
        timeout=60,
    )
    stop = asyncio.Event()
    sampler = asyncio.create_task(_sample(stop))
    started = time.perf_counter()
    results = await asyncio.gather(*(_call(model) for _ in range(concurrency)))
    wall_seconds = time.perf_counter() - started
    stop.set()
    resources = await sampler

    success = [result for result in results if result.error is None]
    latencies = sorted(result.latency_ms for result in results)
    errors = Counter(result.error.split(":", 1)[0] for result in results if result.error)
    input_tokens = sum(result.prompt_tokens or 0 for result in success)
    output_tokens = sum(result.completion_tokens or 0 for result in success)
    return {
        "concurrency": concurrency,
        "requests": len(results),
        "success": len(success),
        "failed": len(results) - len(success),
        "wall_seconds": round(wall_seconds, 3),
        "throughput_rps": round(len(success) / wall_seconds, 2) if wall_seconds else None,
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1),
            "p95": round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))], 1),
            "max": round(max(latencies), 1),
        },
        "reported_tokens": {"input": input_tokens, "output": output_tokens, "total": input_tokens + output_tokens},
        "errors": dict(errors),
        "resources": resources,
    }


async def main() -> None:
    configured = bool(settings.DEEPSEEK_API_KEY and settings.DEEPSEEK_BASE_URL)
    if not configured:
        raise RuntimeError("未配置 DeepSeek API 地址或密钥")
    print("model=deepseek-v4-flash; prompt='只回复 OK'; max_completion_tokens=4")
    print(f"planned_requests={sum(STAGES)}; token_budget_cap=50000")
    stages = []
    for concurrency in STAGES:
        report = await _stage(concurrency)
        stages.append(report)
        print(report)
    total_reported = sum(stage["reported_tokens"]["total"] for stage in stages)
    print({"total_reported_tokens": total_reported, "stages": stages})


if __name__ == "__main__":
    asyncio.run(main())
