"""Read-only HTTP RPS benchmark for Lumi's deployed health endpoint.

No authentication, model, job, database write, or MCP operation is involved.
The health route does read PostgreSQL and Redis, so it is more representative
than a static ping while remaining safe to run against the local deployment.

Run with:
    .\\.venv\\Scripts\\python.exe scripts\\benchmark_api_rps.py
"""

from __future__ import annotations

import asyncio
import argparse
import os
import statistics
import time
from collections import Counter

import httpx
import psutil


URL = "http://127.0.0.1:8000/api/v1/health"
CONCURRENCY_LEVELS = (32, 128, 256)
DURATION_SECONDS = 15


async def _sample(stop: asyncio.Event) -> dict:
    system_cpu: list[float] = []
    process_cpu: list[float] = []
    memory: list[int] = []
    process = psutil.Process(os.getpid())
    psutil.cpu_percent(None)
    process.cpu_percent(None)
    while not stop.is_set():
        await asyncio.sleep(0.25)
        system_cpu.append(psutil.cpu_percent(None))
        process_cpu.append(process.cpu_percent(None))
        memory.append(process.memory_info().rss)
    return {
        "system_cpu_avg_percent": round(statistics.mean(system_cpu), 2) if system_cpu else None,
        "system_cpu_max_percent": round(max(system_cpu), 2) if system_cpu else None,
        "benchmark_process_cpu_max_one_core_percent": round(max(process_cpu), 2) if process_cpu else None,
        "benchmark_process_memory_peak_mib": round(max(memory) / 1024 / 1024, 2) if memory else None,
        "sample_count": len(system_cpu),
    }


async def _worker(client: httpx.AsyncClient, deadline: float, results: list[tuple[float, int | str]]) -> None:
    while time.perf_counter() < deadline:
        started = time.perf_counter()
        try:
            response = await client.get(URL)
            results.append(((time.perf_counter() - started) * 1000, response.status_code))
        except httpx.HTTPError as exc:
            results.append(((time.perf_counter() - started) * 1000, type(exc).__name__))


async def _run_level(concurrency: int, duration_seconds: float) -> dict:
    results: list[tuple[float, int | str]] = []
    stop = asyncio.Event()
    sampler = asyncio.create_task(_sample(stop))
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(10.0)
    started = time.perf_counter()
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        deadline = started + duration_seconds
        await asyncio.gather(*(_worker(client, deadline, results) for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    stop.set()
    resources = await sampler
    latencies = sorted(value for value, _ in results)
    statuses = Counter(str(status) for _, status in results)
    successful = sum(count for status, count in statuses.items() if status == "200")
    return {
        "concurrency": concurrency,
        "duration_seconds": round(elapsed, 3),
        "requests": len(results),
        "success": successful,
        "failed": len(results) - successful,
        "rps": round(successful / elapsed, 2),
        "latency_ms": {
            "p50": round(statistics.median(latencies), 2),
            "p95": round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))], 2),
            "max": round(max(latencies), 2),
        },
        "status_counts": dict(statuses),
        "resources": resources,
    }


async def main(duration_seconds: float) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(URL)
        response.raise_for_status()
    print(f"url={URL}; duration={duration_seconds}s; levels={CONCURRENCY_LEVELS}", flush=True)
    for level in CONCURRENCY_LEVELS:
        print(await _run_level(level, duration_seconds), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=DURATION_SECONDS)
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be positive")
    asyncio.run(main(args.duration))
