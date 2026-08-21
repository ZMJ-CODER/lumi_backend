"""Benchmark the locally installed Ollama chat model through Lumi's ChatModel adapter.

This is deliberately read-only: it does not submit office jobs or change a
user's saved model configuration.  It measures the local inference portion of
an office task so API quotas and persistent application state are untouched.

Run with:
    uv --cache-dir .uv-cache run python scripts/benchmark_local_ollama.py
"""

from __future__ import annotations

import asyncio
import argparse
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass

import psutil
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.langchain.models import get_chat_model


OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
OLLAMA_MODEL = "qwen2.5vl:7b"
CONCURRENCY = 2
SAMPLE_INTERVAL_SECONDS = 0.5

SYSTEM_PROMPT = """你是办公任务规划助手。只输出简洁的中文 Markdown。
根据用户请求，给出不超过三条按顺序执行的原子步骤和一条验收标准。
不要臆造已经调用过工具或创建过文件。"""

REQUESTS = [
    "将 scores.csv 转成 UTF-8 的制表符分隔 scores.txt，并验证行数与源文件一致。",
    "从三份周报提取阻塞项，按负责人分组，输出一份待办清单并标出高风险项目。",
    "核对采购清单与发票明细，标记金额或数量不一致的项目，并给出复核顺序。",
    "根据会议纪要生成项目跟进表，包含负责人、截止日期、依赖项和风险等级。",
]


@dataclass
class RequestMetric:
    request_index: int
    first_token_ms: float | None
    total_ms: float
    output_characters: int
    error: str | None = None


async def _run_request(index: int, prompt: str) -> RequestMetric:
    model = await get_chat_model(
        scene=None,
        user_id=None,
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",
        model=OLLAMA_MODEL,
        temperature=0,
        max_tokens=180,
        timeout=180,
    )
    started = time.perf_counter()
    first_token_ms: float | None = None
    chunks: list[str] = []
    try:
        async for chunk in model.astream(
            [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
        ):
            text = str(chunk.content or "")
            if text and first_token_ms is None:
                first_token_ms = (time.perf_counter() - started) * 1000
            chunks.append(text)
    except Exception as exc:  # Keep the other concurrent requests measurable.
        return RequestMetric(
            request_index=index,
            first_token_ms=first_token_ms,
            total_ms=(time.perf_counter() - started) * 1000,
            output_characters=len("".join(chunks)),
            error=f"{type(exc).__name__}: {exc}",
        )
    return RequestMetric(
        request_index=index,
        first_token_ms=first_token_ms,
        total_ms=(time.perf_counter() - started) * 1000,
        output_characters=len("".join(chunks)),
    )


def _ollama_processes() -> list[psutil.Process]:
    processes: list[psutil.Process] = []
    for process in psutil.process_iter(["name"]):
        name = (process.info.get("name") or "").lower()
        if "ollama" in name or "llama" in name:
            processes.append(process)
    return processes


async def _sample_resources(stop: asyncio.Event) -> dict:
    samples: list[float] = []
    process_cpu: list[float] = []
    process_memory: list[int] = []
    gpu_utilization: list[float] = []
    gpu_memory: list[float] = []
    processes = _ollama_processes()
    psutil.cpu_percent(None)
    for process in processes:
        try:
            process.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    while not stop.is_set():
        await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
        samples.append(psutil.cpu_percent(None))
        cpu_sum = 0.0
        memory_sum = 0
        for process in processes:
            try:
                cpu_sum += process.cpu_percent(None)
                memory_sum += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        process_cpu.append(cpu_sum)
        process_memory.append(memory_sum)
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
            for line in result.stdout.splitlines():
                values = [value.strip() for value in line.split(",")]
                if len(values) == 2:
                    gpu_utilization.append(float(values[0]))
                    gpu_memory.append(float(values[1]))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return {
        "system_cpu_avg_percent": round(sum(samples) / len(samples), 2) if samples else None,
        "system_cpu_max_percent": round(max(samples), 2) if samples else None,
        "ollama_cpu_max_one_core_percent": round(max(process_cpu), 2) if process_cpu else None,
        "ollama_memory_peak_mib": round(max(process_memory) / 1024 / 1024, 2) if process_memory else None,
        "sample_count": len(samples),
        "logical_cpus": os.cpu_count(),
        "gpu_utilization_max_percent": round(max(gpu_utilization), 2) if gpu_utilization else None,
        "gpu_memory_peak_mib": round(max(gpu_memory), 2) if gpu_memory else None,
    }


async def main(concurrency: int) -> None:
    # Warm model weights and the OpenAI-compatible endpoint before measuring.
    warmup = await _run_request(-1, "回复“已就绪”。")
    if warmup.error:
        raise RuntimeError(f"Ollama 预热失败: {warmup.error}")

    stop = asyncio.Event()
    sampler = asyncio.create_task(_sample_resources(stop))
    started = time.perf_counter()
    prompts = [REQUESTS[index % len(REQUESTS)] for index in range(concurrency)]
    metrics = await asyncio.gather(*(_run_request(index, prompt) for index, prompt in enumerate(prompts)))
    total_seconds = time.perf_counter() - started
    stop.set()
    resources = await sampler

    successful = [metric for metric in metrics if not metric.error]
    report = {
        "model": OLLAMA_MODEL,
        "base_url": OLLAMA_BASE_URL,
        "concurrency": concurrency,
        "request_count": len(metrics),
        "successful_requests": len(successful),
        "total_wall_seconds": round(total_seconds, 3),
        "throughput_rps": round(len(successful) / total_seconds, 3) if total_seconds else None,
        "latency_ms": {
            "min": round(min(metric.total_ms for metric in metrics), 1),
            "max": round(max(metric.total_ms for metric in metrics), 1),
            "average": round(sum(metric.total_ms for metric in metrics) / len(metrics), 1),
        },
        "first_token_ms": {
            "min": round(min(metric.first_token_ms for metric in successful if metric.first_token_ms is not None), 1)
            if any(metric.first_token_ms is not None for metric in successful)
            else None,
            "max": round(max(metric.first_token_ms for metric in successful if metric.first_token_ms is not None), 1)
            if any(metric.first_token_ms is not None for metric in successful)
            else None,
        },
        "resources": resources,
        "requests": [asdict(metric) for metric in metrics],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    asyncio.run(main(args.concurrency))
