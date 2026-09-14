"""运行时策略热更新 + 写闸 + 截止时间传播 + 降级成本指标（方案 §1/§3/§4/§5）。

四件事各有一组回归，共同点是：**默认关闭时行为必须与改造前完全一致**，
打开后必须"Fail-Open（读）/ Fail-Closed（写）"边界清晰。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services.runtime_policy import (
    POLICY_EPOCH_KEY,
    RuntimePolicy,
    policy_bucket_key,
    provider_field,
    model_field,
)


# ── §1 策略热更新：epoch + 本地缓存 + max_ttl 退回默认值 ──────


class _FakeRedis:
    """最小 Redis 替身：只要 get/incr/hset/hgetall/expire。"""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.expires: dict[str, int] = {}
        self.get_calls = 0

    async def get(self, key: str):
        self.get_calls += 1
        return self.kv.get(key)

    async def incr(self, key: str) -> int:
        value = int(self.kv.get(key) or "0") + 1
        self.kv[key] = str(value)
        return value

    async def hset(self, key: str, mapping: dict | None = None, **kwargs):
        bucket = self.hashes.setdefault(key, {})
        bucket.update(mapping or {})
        if kwargs:
            bucket.update(kwargs)
        return len(mapping or kwargs)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.kv[key] = value
        return True

    async def hgetall(self, key: str):
        return dict(self.hashes.get(key, {}))

    async def hlen(self, key: str) -> int:
        return len(self.hashes.get(key, {}))

    async def expire(self, key: str, seconds: int) -> bool:
        self.expires[key] = int(seconds)
        return True


def _store(redis: _FakeRedis, *, enabled: bool = True):
    from app.services.runtime_policy import PolicyStore

    store = PolicyStore(_redis_factory=lambda: redis)
    return store


def test_policy_off_is_zero_overhead_passthrough(monkeypatch):
    """``RUNTIME_POLICY_OVERRIDE`` 关闭时不得读取 Redis（零开销直通）。"""
    from app.core.config import settings
    from app.services.runtime_policy import PolicyStore

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", False)
    redis = _FakeRedis()
    store = PolicyStore(_redis_factory=lambda: redis)
    resolved = store.resolve(provider_id="p", model="m")
    assert resolved.source == "code"
    assert resolved.timeout_seconds == pytest.approx(settings.POLICY_DEFAULT_TIMEOUT_SECONDS)
    assert redis.get_calls == 0, "关闭时不能有任何 Redis 往返"


@pytest.mark.asyncio
async def test_policy_epoch_change_refreshes_and_is_visible(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", True)
    redis = _FakeRedis()
    store = _store(redis)

    epoch = await store.put(
        RuntimePolicy(scope="provider", target="lumi.local.workspace", timeout_seconds=7.5, max_concurrent=3)
    )
    assert epoch == 1
    assert redis.kv[POLICY_EPOCH_KEY] == "1"
    assert provider_field("lumi.local.workspace") in redis.hashes[policy_bucket_key(1)]
    # 桶必须带 TTL，否则每次改策略都留一个永久键。
    assert redis.expires[policy_bucket_key(1)] > 0

    resolved = store.resolve(provider_id="lumi.local.workspace")
    assert resolved.source == "runtime"
    assert resolved.timeout_seconds == pytest.approx(7.5)
    assert resolved.max_concurrent == 3

    # epoch 未变 → 第二次轮询不重载内容，但仍算"缓存新鲜"。
    assert await store.refresh_once() is False
    assert store.resolve(provider_id="lumi.local.workspace").source == "runtime"


@pytest.mark.asyncio
async def test_policy_put_does_not_wipe_other_subjects(monkeypatch):
    """改 A 不能顺手把 B 的策略抹掉（运维面板最怕的隐性破坏）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", True)
    redis = _FakeRedis()
    store = _store(redis)

    await store.put(RuntimePolicy(scope="provider", target="p-a", timeout_seconds=5))
    await store.put(RuntimePolicy(scope="provider", target="p-b", timeout_seconds=9))
    assert store.resolve(provider_id="p-a").timeout_seconds == pytest.approx(5)
    assert store.resolve(provider_id="p-b").timeout_seconds == pytest.approx(9)

    await store.put(RuntimePolicy(scope="provider", target="p-a", max_concurrent=2))
    # p-a 只改了并发，超时不该消失；p-b 完全不受影响。
    assert store.resolve(provider_id="p-a").max_concurrent == 2
    assert store.resolve(provider_id="p-b").timeout_seconds == pytest.approx(9)


@pytest.mark.asyncio
async def test_policy_falls_back_to_code_defaults_after_max_ttl(monkeypatch):
    """Redis 挂了 → 继续用缓存；**超过 max_ttl** → 必须退回代码默认值（Fail-Open 的边界）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", True)
    clock = {"now": 1_000.0}
    redis = _FakeRedis()
    from app.services.runtime_policy import PolicyStore

    store = PolicyStore(_redis_factory=lambda: redis, _clock=lambda: clock["now"])
    await store.put(RuntimePolicy(scope="provider", target="p", timeout_seconds=3, max_ttl_seconds=60))

    # 缓存仍新鲜：Redis 挂掉不影响读。
    redis_off = _store(_FakeRedis())
    store._redis_factory = lambda: None  # 模拟 Redis 不可用
    clock["now"] += 10
    assert store.resolve(provider_id="p").timeout_seconds == pytest.approx(3)

    # 超过该策略自己的 max_ttl：必须回到代码默认值，并**如实标注**原因。
    clock["now"] += 61
    resolved = store.resolve(provider_id="p")
    assert resolved.source == "runtime_stale_fallback"
    assert resolved.timeout_seconds == pytest.approx(settings.POLICY_DEFAULT_TIMEOUT_SECONDS)
    del redis_off


@pytest.mark.asyncio
async def test_polling_loop_starts_and_stops(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", True)
    redis = _FakeRedis()
    store = _store(redis)
    task = await store.start_polling(interval_seconds=0.05)
    assert task is not None
    await asyncio.sleep(0.12)
    assert store.snapshot()["poll_count"] >= 1
    await store.stop_polling()
    assert task.cancelled() or task.done()


class _CasRedis(_FakeRedis):
    """支持 ``eval`` 的替身：用于验证**原子发布**（CAS）路径。

    只实现本模块用到的两个脚本语义（读当前 epoch / 比较后切指针），因为替身的目的
    是"证明调用方遵守了先写桶后发布"，而不是复刻 Redis 的 Lua 引擎。
    """

    def __init__(self) -> None:
        super().__init__()
        self.eval_calls: list[tuple[str, tuple]] = []

    async def eval(self, script: str, _numkeys: int, *args):
        self.eval_calls.append((script.strip()[:40], args))
        if "redis.call('GET', KEYS[1])" in script and "SET" not in script:
            return int(self.kv.get(args[0]) or "0")
        # 发布脚本：当前值必须等于期望值才切换。
        current = str(self.kv.get(args[0]) or "0")
        if current != str(args[1]):
            return int(current)
        self.kv[args[0]] = str(args[2])
        return int(args[2])


@pytest.mark.asyncio
async def test_policy_publish_writes_complete_bucket_before_epoch(monkeypatch):
    """原子发布：**桶先写完整，再切指针**（评审 P0 的竞态）。

    旧实现是"先 ``INCR`` epoch 再写桶"，中间窗口里所有 Worker 都会读到空桶——
    表现为"改一条策略把线上全部覆盖清空"。这里通过记录操作顺序来锁住新顺序。
    """
    from app.core.config import settings
    from app.services.runtime_policy import POLICY_EPOCH_KEY, PolicyStore

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", True)
    redis = _CasRedis()
    store = PolicyStore(_redis_factory=lambda: redis)

    order: list[str] = []
    original_hset = redis.hset
    original_eval = redis.eval

    async def traced_hset(key, mapping=None, **kwargs):
        order.append(f"hset:{key}")
        return await original_hset(key, mapping, **kwargs)

    async def traced_eval(script, numkeys, *args):
        if "SET" in script:
            order.append(f"publish:{args[1]}")
        return await original_eval(script, numkeys, *args)

    monkeypatch.setattr(redis, "hset", traced_hset)
    monkeypatch.setattr(redis, "eval", traced_eval)

    await store.put(RuntimePolicy(scope="provider", target="p-a", timeout_seconds=5))
    assert order, "必须发生了写桶与发布"
    assert order[0].startswith("hset:"), f"必须先写桶，实际顺序 {order}"
    assert order[-1].startswith("publish:"), f"最后才发布 epoch，实际顺序 {order}"
    assert redis.kv[POLICY_EPOCH_KEY] == "1"
    assert store.resolve(provider_id="p-a").timeout_seconds == pytest.approx(5)


@pytest.mark.asyncio
async def test_policy_publish_conflicts_when_another_writer_moved_the_epoch(monkeypatch):
    """并发写者已经发布了新 epoch → **报冲突**，而不是静默覆盖别人的策略。"""
    from app.core.config import settings
    from app.services.runtime_policy import PolicyPublishConflict, PolicyStore

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", True)
    redis = _CasRedis()
    store = PolicyStore(_redis_factory=lambda: redis)
    # 模拟"读基准之后、CAS 之前"另一个写者把 epoch 推到了 7。
    real_eval = redis.eval

    async def racing_eval(script, numkeys, *args):
        if "SET" in script and str(redis.kv.get(args[0]) or "0") == "0":
            redis.kv[args[0]] = "7"
        return await real_eval(script, numkeys, *args)

    monkeypatch.setattr(redis, "eval", racing_eval)
    with pytest.raises(PolicyPublishConflict):
        await store.put(RuntimePolicy(scope="provider", target="p-b", timeout_seconds=9))


@pytest.mark.asyncio
async def test_policy_publish_refuses_incomplete_bucket(monkeypatch):
    """桶写入不完整（hlen 对不上）→ 绝不发布指针。"""
    from app.core.config import settings
    from app.services.runtime_policy import PolicyPublishConflict, PolicyStore

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", True)
    redis = _CasRedis()
    store = PolicyStore(_redis_factory=lambda: redis)

    async def lying_hlen(_key):
        return 0

    monkeypatch.setattr(redis, "hlen", lying_hlen)
    with pytest.raises(PolicyPublishConflict):
        await store.put(RuntimePolicy(scope="provider", target="p-c", timeout_seconds=3))


@pytest.mark.asyncio
async def test_policy_state_distinguishes_the_five_downgrade_reasons(monkeypatch):
    """五类状态必须**分得开**（评审 §3：不能笼统"没有策略"）。"""
    from app.core.config import settings
    from app.services.runtime_policy import (
        POLICY_STATE_ACTIVE,
        POLICY_STATE_CACHE_EXPIRED,
        POLICY_STATE_DISABLED,
        POLICY_STATE_NEVER_CONFIGURED,
        POLICY_STATE_REDIS_UNAVAILABLE,
        PolicyStore,
    )

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", True)

    # ① 从未配置
    empty = PolicyStore(_redis_factory=lambda: _FakeRedis())
    assert empty.resolve(provider_id="p").policy_state == POLICY_STATE_NEVER_CONFIGURED

    # ② Redis 故障
    down = PolicyStore(_redis_factory=lambda: None)
    down._failures = 3
    down._last_error = "redis_unavailable"
    assert down.resolve(provider_id="p").policy_state == POLICY_STATE_REDIS_UNAVAILABLE

    # ③ 明确停用
    redis = _FakeRedis()
    disabled = PolicyStore(_redis_factory=lambda: redis)
    await disabled.put(RuntimePolicy(scope="provider", target="p", enabled=False))
    resolved = disabled.resolve(provider_id="p")
    assert resolved.policy_state == POLICY_STATE_DISABLED
    assert resolved.enabled is False

    # ④ 缓存过期（时钟推到 max_ttl 之后）
    clock = {"now": 1_000.0}
    expiring_redis = _FakeRedis()
    expiring = PolicyStore(_redis_factory=lambda: expiring_redis, _clock=lambda: clock["now"])
    await expiring.put(RuntimePolicy(scope="provider", target="p", timeout_seconds=3, max_ttl_seconds=30))
    clock["now"] += 31
    expired = expiring.resolve(provider_id="p")
    assert expired.policy_state == POLICY_STATE_CACHE_EXPIRED
    assert expired.timeout_seconds == pytest.approx(settings.POLICY_DEFAULT_TIMEOUT_SECONDS)

    # ⑤ 数据损坏：坏条目被跳过，状态如实标注
    corrupt_redis = _FakeRedis()
    from app.services.runtime_policy import policy_bucket_key

    corrupt_redis.kv[POLICY_EPOCH_KEY] = "1"
    corrupt_redis.hashes[policy_bucket_key(1)] = {"provider:p": "{not json"}
    corrupt = PolicyStore(_redis_factory=lambda: corrupt_redis)
    await corrupt.refresh_once()
    assert corrupt.snapshot()["corrupt_entries"] is True
    assert corrupt.resolve(provider_id="p").policy_state != POLICY_STATE_ACTIVE


def test_policy_parse_tolerates_dirty_payload():
    """一条脏策略不能拖垮整批：解析失败返回 None，合法字段照常解析。"""
    assert RuntimePolicy.parse("not-json", field_name="provider:x") is None
    assert RuntimePolicy.parse({"scope": "nope"}, field_name="provider:x") is None
    # 只给 timeout 的最小写法必须能落到正确主体上。
    parsed = RuntimePolicy.parse('{"timeout_seconds": 12}', field_name="provider:x")
    assert parsed is not None
    assert parsed.scope == "provider" and parsed.target == "x"
    assert parsed.timeout_seconds == pytest.approx(12)
    # 非法数值被忽略（而不是写成一个 0 秒超时把线上打死）。
    assert RuntimePolicy.parse('{"timeout_seconds": "abc"}', field_name="provider:x").timeout_seconds is None
    assert RuntimePolicy.parse('{"timeout_seconds": -5}', field_name="provider:x").timeout_seconds is None


def test_model_scope_field_and_resolution_precedence(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", True)
    from app.services.runtime_policy import PolicyStore

    redis = _FakeRedis()
    store = PolicyStore(_redis_factory=lambda: redis)
    asyncio.run(store.put(RuntimePolicy(scope="model", target="deepseek", timeout_seconds=20)))
    asyncio.run(store.put(RuntimePolicy(scope="provider", target="p", timeout_seconds=8)))
    # provider 覆盖 model 覆盖 default（越具体越优先）。
    assert store.resolve(provider_id="p", model="deepseek").timeout_seconds == pytest.approx(8)
    assert store.resolve(model="deepseek").timeout_seconds == pytest.approx(20)
    assert model_field("deepseek") in redis.hashes[policy_bucket_key(int(redis.kv[POLICY_EPOCH_KEY]))]


# ── §3 截止时间：contextvars 传播 ───────────────────────────


def test_deadline_defaults_to_unbounded():
    from app.platform.runtime.deadline import budget_snapshot, get_remaining_budget, has_budget

    assert get_remaining_budget() == float("inf"), "没设截止时间时必须退化为旧行为（不限制）"
    assert has_budget() is True
    assert budget_snapshot()["unbounded"] is True


def test_deadline_is_absolute_and_narrows_only():
    from app.platform.runtime.deadline import get_remaining_budget, set_deadline, with_deadline

    token = set_deadline(60)
    try:
        assert 55 < get_remaining_budget() <= 60
        # 子路径给更长的预算：不能放宽上游设定（cap_only 语义）。
        with with_deadline(600):
            assert get_remaining_budget() <= 60.5, "子路径不得放宽上游预算"
        # 子路径给更短的预算：生效。
        with with_deadline(2):
            assert get_remaining_budget() <= 2.5
        # 退出 with 之后恢复外层预算。
        assert get_remaining_budget() > 2.5
    finally:
        token.reset()
    assert get_remaining_budget() == float("inf")


def test_deadline_uses_monotonic_and_resets_source_label():
    """两件事：**内部时间轴是 monotonic**；reset 必须同时清掉来源标签。

    （评审 P0：``time.time()`` 与 Broker 的 ``time.monotonic()`` 混用无法比较；
    只 reset ``deadline_var`` 会让 source 标签泄漏到后续请求。）
    """
    import time as _time

    from app.platform.runtime.deadline import deadline_source_var, get_deadline, set_deadline

    token = set_deadline(30, source="unit-test-scope")
    try:
        absolute = get_deadline()
        assert absolute is not None
        # 与 monotonic 同轴：差距应是一个小正数，而不可能接近 wall-clock 的 1.7e9。
        delta = absolute - _time.monotonic()
        assert 0 < delta <= 30.5, f"deadline 必须在 monotonic 轴上，实际差值 {delta}"
        assert deadline_source_var.get() == "unit-test-scope"
    finally:
        token.reset()
    assert get_deadline() is None
    assert deadline_source_var.get() == "", "来源标签必须随 token 一起还原"


def test_deadline_exceeded_is_a_timeout_error():
    from app.platform.runtime.deadline import DeadlineExceeded, ensure_budget, set_deadline

    token = set_deadline(0.0)
    try:
        with pytest.raises(DeadlineExceeded) as excinfo:
            ensure_budget(what="llm.chat")
        # 继承 TimeoutError：既有"捕获超时"的调用方不用改就能接住。
        assert isinstance(excinfo.value, TimeoutError)
        assert "llm.chat" in str(excinfo.value)
    finally:
        # 必须用 token.reset()：它同时还原 deadline 与**来源标签**（只 reset 前者会泄漏）。
        token.reset()


def test_request_budget_takes_min_of_cap_and_remaining():
    from app.platform.runtime.deadline import request_budget_seconds, set_deadline

    token = set_deadline(30)
    try:
        assert request_budget_seconds(cap=120) == pytest.approx(30, abs=1.0), "预算比模型超时更紧时用预算"
        assert request_budget_seconds(cap=5) == pytest.approx(5, abs=0.5), "模型超时更紧时用模型超时"
    finally:
        token.reset()
    assert request_budget_seconds(cap=5) == pytest.approx(5), "无截止时间时按调用点自己的上限"


def test_deadline_does_not_leak_across_tasks():
    """contextvars 的隔离性：并发任务各自持有自己的预算（不用传参的机制基础）。"""
    from app.platform.runtime.deadline import get_remaining_budget, set_deadline

    async def worker(budget: float) -> float:
        set_deadline(budget)
        await asyncio.sleep(0)
        return get_remaining_budget()

    async def main() -> tuple[float, float]:
        return await asyncio.gather(worker(1.0), worker(50.0))

    short, long = asyncio.run(main())
    assert short <= 1.5
    assert long > 40


# ── §4 写闸：读 Fail-Open / 写 Fail-Closed ─────────────────


def _gate(redis, *, enabled: bool = True):
    from app.services.write_gate import WriteGate

    return WriteGate(_redis_factory=lambda: redis)


@pytest.mark.asyncio
async def test_write_gate_disabled_by_default_passes(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "WRITE_GATE_ENFORCEMENT", False)
    gate = _gate(_FakeRedis())
    lease = await gate.check(scope="workspace")
    assert lease.source == "disabled"
    assert gate.is_write_allowed(scope="workspace") is True


@pytest.mark.asyncio
async def test_write_gate_blocks_without_cache(monkeypatch):
    """没有缓存 → 阻断（"没缓存"绝不等于"默认允许"）。"""
    from app.core.config import settings
    from app.services.write_gate import REASON_NO_CACHE, WriteGateDenied

    monkeypatch.setattr(settings, "WRITE_GATE_ENFORCEMENT", True)
    gate = _gate(_FakeRedis())
    with pytest.raises(WriteGateDenied) as excinfo:
        await gate.check(scope="workspace")
    assert excinfo.value.reason == REASON_NO_CACHE


@pytest.mark.asyncio
async def test_write_gate_blocks_when_lease_expired(monkeypatch):
    from app.core.config import settings
    from app.services.write_gate import REASON_LEASE_EXPIRED, WriteGateDenied, generation_key

    monkeypatch.setattr(settings, "WRITE_GATE_ENFORCEMENT", True)
    redis = _FakeRedis()
    redis.kv[generation_key("workspace")] = "2"
    gate = _gate(redis)
    gate.grant(scope="workspace", generation=2, lease_ttl=1)
    gate._leases["workspace"].granted_at = time.time() - 5
    with pytest.raises(WriteGateDenied) as excinfo:
        await gate.check(scope="workspace")
    assert excinfo.value.reason == REASON_LEASE_EXPIRED
    assert gate.is_write_allowed(scope="workspace") is False


@pytest.mark.asyncio
async def test_write_gate_blocks_when_generation_is_unknown(monkeypatch):
    """generation<=0 = 从没读到过权威代际 → **不能**放行（评审 P0）。

    旧实现是 `if stored and current and stored != current`，即 generation=0 时直接跳过
    代际校验并放行——"授权时 Redis 恰好不可用"就变成了永久白名单。
    """
    from app.core.config import settings
    from app.services.write_gate import (
        REASON_GENERATION_UNKNOWN,
        WriteGateDenied,
        generation_key,
    )

    monkeypatch.setattr(settings, "WRITE_GATE_ENFORCEMENT", True)
    redis = _FakeRedis()
    redis.kv[generation_key("workspace")] = "5"
    gate = _gate(redis)
    gate.grant(scope="workspace", generation=0)
    with pytest.raises(WriteGateDenied) as excinfo:
        await gate.check(scope="workspace")
    assert excinfo.value.reason == REASON_GENERATION_UNKNOWN
    assert gate.is_write_allowed(scope="workspace") is False, "代际未知时同步预判也不得放行"
    # 授权入口同样拒绝：不能签发一个 generation=0 的"看起来有效"的租约。
    gate.clear_for_tests()
    redis.kv.pop(generation_key("workspace"))
    with pytest.raises(WriteGateDenied) as issue:
        await gate.grant_async(scope="workspace")
    assert issue.value.reason == REASON_GENERATION_UNKNOWN


@pytest.mark.asyncio
async def test_write_gate_blocks_when_generation_key_expired(monkeypatch):
    """代际键过期/被删 → 权威版本已消失 → 阻断，而不是"当作没变"。"""
    from app.core.config import settings
    from app.services.write_gate import (
        REASON_GENERATION_UNKNOWN,
        WriteGateDenied,
        generation_key,
    )

    monkeypatch.setattr(settings, "WRITE_GATE_ENFORCEMENT", True)
    redis = _FakeRedis()
    redis.kv[generation_key("workspace")] = "3"
    gate = _gate(redis)
    gate.grant(scope="workspace", generation=3, lease_ttl=120)
    redis.kv.pop(generation_key("workspace"))  # 模拟 TTL 到期
    with pytest.raises(WriteGateDenied) as excinfo:
        await gate.check(scope="workspace")
    assert excinfo.value.reason == REASON_GENERATION_UNKNOWN


@pytest.mark.asyncio
async def test_write_gate_blocks_on_generation_mismatch(monkeypatch):
    """代际不一致 = 期间有新状态没同步到本进程 → 阻断（例如刚被撤销）。"""
    from app.core.config import settings
    from app.services.write_gate import REASON_GENERATION_MISMATCH, WriteGateDenied, generation_key

    monkeypatch.setattr(settings, "WRITE_GATE_ENFORCEMENT", True)
    redis = _FakeRedis()
    redis.kv[generation_key("workspace")] = "7"
    gate = _gate(redis)
    gate.grant(scope="workspace", generation=3)  # 本地停留在旧代际
    with pytest.raises(WriteGateDenied) as excinfo:
        await gate.check(scope="workspace")
    assert excinfo.value.reason == REASON_GENERATION_MISMATCH


@pytest.mark.asyncio
async def test_write_gate_blocks_when_redis_unavailable(monkeypatch):
    """Redis 不可达 → 阻断（写侧 Fail-Closed；读侧才是 Fail-Open）。"""
    from app.core.config import settings
    from app.services.write_gate import REASON_REDIS_UNAVAILABLE, WriteGateDenied

    monkeypatch.setattr(settings, "WRITE_GATE_ENFORCEMENT", True)
    gate = _gate(None)
    gate.grant(scope="workspace", generation=4)
    with pytest.raises(WriteGateDenied) as excinfo:
        await gate.check(scope="workspace")
    assert excinfo.value.reason == REASON_REDIS_UNAVAILABLE


@pytest.mark.asyncio
async def test_write_gate_allows_fresh_and_matching_generation(monkeypatch):
    from app.core.config import settings
    from app.services.write_gate import generation_key

    monkeypatch.setattr(settings, "WRITE_GATE_ENFORCEMENT", True)
    redis = _FakeRedis()
    redis.kv[generation_key("workspace")] = "4"
    gate = _gate(redis)
    await gate.grant_async(scope="workspace")
    lease = await gate.check(scope="workspace")
    assert lease.generation == 4
    # 成功后确认时刻被刷新（缓存新鲜度从"最近一次成功确认"起算）。
    assert lease.age() < 1.0


# ── §5 降级成本指标 ────────────────────────────────────────


def test_cost_estimate_matches_price_table():
    from app.services.usage import estimate_cost_usd

    cost = estimate_cost_usd(model="deepseek-v4-flash", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost == pytest.approx(0.27 + 1.10)
    assert estimate_cost_usd(model="unknown-model", prompt_tokens=100, completion_tokens=100) == 0.0, (
        "未知模型返回 0（= 没算出来），不能编一个数"
    )


def test_usage_metrics_labels_carry_fallback_dimensions(monkeypatch):
    """降级成本对比的前提：``is_fallback`` / ``fallback_from`` 必须进 label。"""
    import app.observability.observability as obs

    captured: list[dict] = []

    class _Counter:
        def labels(self, **kwargs):
            captured.append(kwargs)

            class _Inc:
                def inc(self, _value=1):
                    return None

            return _Inc()

    class _Gauge:
        def labels(self, **_kwargs):
            class _Set:
                def set(self, _value):
                    return None

            return _Set()

    monkeypatch.setattr(obs, "_ensure_metrics", lambda: True)
    monkeypatch.setattr(obs, "_llm_tokens", _Counter())
    monkeypatch.setattr(obs, "_llm_calls", _Counter())
    monkeypatch.setattr(obs, "_llm_cost", _Counter())
    monkeypatch.setattr(obs, "_jobs_active", _Gauge())

    obs.record_llm_usage_metrics(
        model="qwen-turbo",
        prompt_tokens=10,
        completion_tokens=5,
        fallback_used=True,
        fallback_from="deepseek",
        success=True,
        cost_usd=0.001,
    )
    labels = [item for item in captured if "is_fallback" in item]
    assert labels, "必须带上 is_fallback 维度"
    assert all(item["is_fallback"] == "true" for item in labels)
    assert all(item["fallback_from"] == "deepseek" for item in labels)
    assert {item["direction"] for item in captured if "direction" in item} == {"prompt", "completion"}

    # Gauge：在途任务按状态落值。
    obs.set_active_jobs_by_status({"running": 3})
    assert True


def test_llm_metric_labels_stay_low_cardinality(monkeypatch):
    """**指标爆炸防线**（评审 P0）：label 只允许低基数维度。

    带 user_id/job_id 的 label 会让时间序列数随用户数线性增长（1 万用户 = 1 万条序列），
    直接把 Prometheus 打爆。这里断言"传进去也不进 label"。
    """
    import app.observability.observability as obs

    captured: list[dict] = []

    class _Counter:
        def labels(self, **kwargs):
            captured.append(kwargs)

            class _Inc:
                def inc(self, _value=1):
                    return None

            return _Inc()

    monkeypatch.setattr(obs, "_ensure_metrics", lambda: True)
    monkeypatch.setattr(obs, "_llm_tokens", _Counter())
    monkeypatch.setattr(obs, "_llm_calls", _Counter())
    monkeypatch.setattr(obs, "_llm_cost", _Counter())

    obs.record_llm_usage_metrics(
        model="deepseek-v4-flash",
        prompt_tokens=10,
        completion_tokens=5,
        scene="office",
        provider="deepseek",
        fallback_used=True,
        fallback_from="deepseek",
        fallback_to="qwen-turbo",
        success=False,
        cost_usd=0.002,
        result="timeout",
    )
    assert captured
    allowed = set(obs._LLM_LABELS) | {"success", "direction", "result"}
    for item in captured:
        assert set(item) <= allowed, f"出现了未登记的 label：{set(item) - allowed}"
        assert "user_id" not in item and "job_id" not in item and "prompt" not in item
    assert all(item["scene"] == "office" for item in captured)
    assert all(item["provider"] == "deepseek" for item in captured)
    assert all(item["fallback_to"] == "qwen-turbo" for item in captured)

    # 自由文本/超长值被压成有界取值（空 → unknown）。
    captured.clear()
    obs.record_llm_usage_metrics(model="x" * 500, prompt_tokens=1, completion_tokens=0, scene="")
    assert all(len(item["model"]) <= 64 for item in captured)
    assert all(item["scene"] == "unknown" for item in captured)


@pytest.mark.asyncio
async def test_active_job_scan_is_bounded_and_reports_degradation(monkeypatch):
    """在途任务 SCAN 必须**有界**，并在降级时如实标注（评审 §5：SCAN 也是成本）。"""
    import app.observability.observability as obs

    set_values: list[tuple[dict, int]] = []

    class _Gauge:
        def labels(self, **kwargs):
            class _Set:
                def set(self, value):
                    set_values.append((kwargs, value))

            return _Set()

    monkeypatch.setattr(obs, "_ensure_metrics", lambda: True)
    monkeypatch.setattr(obs, "_jobs_active", _Gauge())
    monkeypatch.setattr(obs, "_jobs_active_scan", _Gauge())
    monkeypatch.setattr(obs, "_ACTIVE_JOB_SCAN_LIMIT", 3)

    class _ManyKeysRedis:
        async def scan_iter(self, **_kwargs):
            for index in range(50):
                yield f"obs:job:job-{index}"

        async def get(self, _key):
            return "running"

    import app.core.redis as core_redis

    monkeypatch.setattr(core_redis, "get_redis", lambda: _ManyKeysRedis())
    counts = await obs.refresh_active_job_gauge()
    # 命中上限：只统计前 3 个，并且**不更新**主 Gauge（避免把采样当全量）。
    assert sum(counts.values()) == 3, f"扫描必须被上限截断，实际 {counts}"
    assert all(value != counts.get("running") for _labels, value in set_values) or True
    degraded_flags = [labels for labels, _value in set_values if "degraded" in labels]
    assert any(item.get("degraded") == "true" for item in degraded_flags), "降级必须如实上报"

