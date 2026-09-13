"""运行时策略热更新：Redis Key + TTL + 本地内存轮询（不引入配置中心）。

## 为什么是这个形状

三层覆盖（**代码默认 < ``.env`` < 运行时覆盖**）里，前两层在启动时就定了，第三层要能在
**不重启 Worker** 的前提下改。为此不需要 ConfigServer：Redis 存策略、Worker 每 10 秒
拿一次 epoch、变了才全量拉取，就够运维用了。

## 键空间

* ``policy:epoch`` —— 单调递增字符串。写策略时 ``INCR``；Worker 只比较它，不比较内容，
  因此"没变"时零传输。
* ``policy:<epoch>`` —— Hash，field 为策略主体，value 为一份策略 JSON。
  用 `epoch` 分桶而不是原地改同一个 Hash，是为了让**读到一半的 Worker 不会被写坏**：
  旧 epoch 的桶带 TTL 自然过期，读到的永远是自洽的一份。

主体命名（`field` 的写法）：

* ``provider:<provider_id>`` —— 某个能力 Provider（如 ``lumi.local.workspace``）；
* ``model:<model_name>`` —— 某个模型（如 ``deepseek-v4-flash``）。

## Fail-Open 的边界（本模块的核心约束）

**Redis 挂了不等于策略可以无限期沿用**。每条策略带 ``max_ttl``（默认
``POLICY_CACHE_MAX_TTL_SECONDS``，600s）：本地缓存超过它就必须**退回代码默认值**，
而不是继续拿旧策略去限制（或放宽）线上行为。

* Redis **可用**：epoch 没变 → 继续用本地缓存（缓存时刻刷新，不过期）；
* Redis **不可用**：继续用本地缓存，但 ``max_ttl`` 一到即失效 → 退回代码默认值；
* Redis **恢复**：下一次轮询自动拉回最新策略。

写操作另有一套更严的规则（见 :mod:`app.services.write_gate`）：读路径 Fail-Open，
**写路径 Fail-Closed**。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from loguru import logger

#: epoch 键：Worker 只比较它。
POLICY_EPOCH_KEY = "policy:epoch"
#: 策略桶键前缀：``policy:<epoch>``。
POLICY_BUCKET_PREFIX = "policy:"

#: **原子发布**：只有"当前 epoch 仍等于我读到的基准"时才把指针切到新桶。
#:
#: 为什么必须原子：Worker 读的是 ``policy:epoch`` 指向的那个桶。如果先 ``INCR`` epoch
#: 再写桶，间隙里所有 Worker 都会读到**空桶** —— 结果是"改一条策略把线上全部覆盖清空"，
#: 而且它只在竞态窗口出现，平时看不出来。CAS 保证指针永远指向一个**已完整写入并校验过**
#: 的桶。
_PUBLISH_EPOCH_SCRIPT = """
local current = redis.call('GET', KEYS[1])
local expected = ARGV[1]
if not current then current = '0' end
if current ~= expected then
  return tonumber(current)
end
redis.call('SET', KEYS[1], ARGV[2])
return tonumber(ARGV[2])
"""

#: 读"权威当前 epoch"（发布前的基准）——必须是 Redis 的值，**不是**本地缓存 epoch：
#: 用本地 epoch 当基准会让两个 Worker 各自基于旧状态生成新桶，互相覆盖。
_CURRENT_EPOCH_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
return tonumber(current)
"""

#: 策略主体类型（field 前缀）。
SCOPE_PROVIDER = "provider"
SCOPE_MODEL = "model"
#: 兜底主体：所有 Provider / 模型都适用的默认值。
SCOPE_DEFAULT = "default"

_VALID_SCOPES: frozenset[str] = frozenset({SCOPE_PROVIDER, SCOPE_MODEL, SCOPE_DEFAULT})


def policy_epoch_key() -> str:
    return POLICY_EPOCH_KEY


def policy_bucket_key(epoch: int) -> str:
    return f"{POLICY_BUCKET_PREFIX}{int(epoch)}"


def provider_field(provider_id: str) -> str:
    return f"{SCOPE_PROVIDER}:{str(provider_id or '').strip()}"


def model_field(model_name: str) -> str:
    return f"{SCOPE_MODEL}:{str(model_name or '').strip()}"


def default_field() -> str:
    return SCOPE_DEFAULT


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """一条运行时策略（缺省字段 = 不覆盖，继续沿用下一层）。

    只有**显式给了**的字段才覆盖：``None`` 表示"这一层不管"，因此不会出现
    "运维只想改超时，却把并发度顺手清零"这种事。
    """

    scope: str = SCOPE_PROVIDER
    target: str = ""
    #: 单次调用超时（秒）。
    timeout_seconds: float | None = None
    #: 并发上限（>0 生效）。
    max_concurrent: int | None = None
    #: 启用/停用该 Provider 或模型。
    enabled: bool | None = None
    #: 该策略自身的可信上限（秒）：本地缓存超过它必须退回代码默认值。
    max_ttl_seconds: float | None = None
    #: 自由备注（运维面板用；不参与判定）。
    note: str = ""
    #: 写入时刻（运维侧给的 ISO8601 或 unix 秒；仅审计用途）。
    updated_at: str = ""

    @property
    def field_name(self) -> str:
        if self.scope == SCOPE_DEFAULT:
            return default_field()
        if self.scope == SCOPE_MODEL:
            return model_field(self.target)
        return provider_field(self.target)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"scope": self.scope, "target": self.target}
        for name in ("timeout_seconds", "max_concurrent", "enabled", "max_ttl_seconds"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        if self.note:
            payload["note"] = self.note
        if self.updated_at:
            payload["updated_at"] = self.updated_at
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def parse(cls, raw: Any, *, field_name: str = "") -> "RuntimePolicy | None":
        """宽容解析（坏数据返回 ``None``，绝不让一条脏策略拖垮整批）。

        ``field_name`` 是 Hash field；payload 里的 ``scope``/``target`` 缺失时由它推导，
        这样运维面板只写 ``{"timeout_seconds": 30}`` 也能落到正确主体上。
        """
        scope = SCOPE_DEFAULT
        target = ""
        if field_name:
            head, _, tail = str(field_name).partition(":")
            if head in _VALID_SCOPES:
                scope = head
                target = tail
        if isinstance(raw, (str, bytes)):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                return None
        if not isinstance(raw, Mapping):
            return None
        scope = str(raw.get("scope") or scope or SCOPE_DEFAULT).strip().lower() or SCOPE_DEFAULT
        if scope not in _VALID_SCOPES:
            return None
        target = str(raw.get("target") or target or "").strip()
        if scope != SCOPE_DEFAULT and not target:
            return None

        def _number(name: str, *, minimum: float = 0.0) -> float | None:
            value = raw.get(name)
            if value is None or value == "":
                return None
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            if number < minimum:
                return None
            return number

        def _flag(name: str) -> bool | None:
            value = raw.get(name)
            if value is None or value == "":
                return None
            if isinstance(value, bool):
                return value
            text = str(value).strip().casefold()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
            return None

        max_concurrent_raw = _number("max_concurrent")
        return cls(
            scope=scope,
            target=target,
            timeout_seconds=_number("timeout_seconds", minimum=0.001),
            max_concurrent=int(max_concurrent_raw) if max_concurrent_raw else None,
            enabled=_flag("enabled"),
            max_ttl_seconds=_number("max_ttl_seconds", minimum=1.0),
            note=str(raw.get("note") or "")[:200],
            updated_at=str(raw.get("updated_at") or "")[:40],
        )


#: 运行时策略的**可信度状态**（方案 §3 要求区分五类，而不是笼统"没有策略"）。
#: 前端/运维面板据此决定"能不能信这份结论"，也决定下一步动作。
POLICY_STATE_NEVER_CONFIGURED = "never_configured"
POLICY_STATE_ACTIVE = "active"
POLICY_STATE_CACHE_EXPIRED = "cache_expired"
POLICY_STATE_REDIS_UNAVAILABLE = "redis_unavailable"
POLICY_STATE_DISABLED = "explicitly_disabled"
POLICY_STATE_CORRUPT = "policy_data_corrupt"


#: 解析一层覆盖后的有效值（含来源，便于回答"这个 30s 是谁定的"）。
@dataclass(frozen=True, slots=True)
class ResolvedPolicy:
    timeout_seconds: float
    max_concurrent: int
    enabled: bool
    #: ``code`` / ``env`` / ``runtime`` / ``runtime_stale_fallback``。
    source: str
    #: 命中的策略主体（``provider:x`` / ``model:y`` / ``default`` / ``""``）。
    scope: str = ""
    #: 本地缓存年龄（秒）；无运行时策略时为 0。
    cache_age_seconds: float = 0.0
    #: 可信度状态，取值见 ``POLICY_STATE_*``。
    policy_state: str = POLICY_STATE_NEVER_CONFIGURED

    def as_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_concurrent": self.max_concurrent,
            "enabled": self.enabled,
            "source": self.source,
            "scope": self.scope,
            "cache_age_seconds": self.cache_age_seconds,
            "policy_state": self.policy_state,
        }


def _code_defaults() -> tuple[float, int, bool]:
    """代码默认值（第三层兜底的终点）。

    只有这里读 ``settings``：``.env`` 属于"第二层"，它给的值就是代码默认值本身
    （没有单独的常量文件，避免两处默认值漂移）。
    """
    try:
        from app.core.config import settings

        timeout = float(getattr(settings, "POLICY_DEFAULT_TIMEOUT_SECONDS", 30.0) or 30.0)
        concurrency = int(getattr(settings, "POLICY_DEFAULT_MAX_CONCURRENT", 4) or 4)
    except Exception:  # noqa: BLE001 - 配置不可用时用硬编码兜底
        timeout, concurrency = 30.0, 4
    return max(0.001, timeout), max(1, concurrency), True


def default_max_ttl_seconds() -> float:
    try:
        from app.core.config import settings

        return max(1.0, float(getattr(settings, "POLICY_CACHE_MAX_TTL_SECONDS", 600) or 600))
    except Exception:  # noqa: BLE001
        return 600.0


def poll_interval_seconds() -> float:
    try:
        from app.core.config import settings

        return max(1.0, float(getattr(settings, "POLICY_POLL_INTERVAL_SECONDS", 10) or 10))
    except Exception:  # noqa: BLE001
        return 10.0


def policy_bucket_ttl_seconds() -> int:
    """epoch 桶的 TTL：默认远大于轮询间隔（旧桶只是没人读，不必马上删）。

    没有 TTL 的话，每次改策略都会在 Redis 留一个永久 Hash，运维面板用久了就把
    Redis 写满——这类"看起来正常、几周后爆炸"的键必须一开始就带上过期时间。
    """
    try:
        from app.core.config import settings

        return max(300, int(getattr(settings, "POLICY_BUCKET_TTL_SECONDS", 86400) or 86400))
    except Exception:  # noqa: BLE001
        return 86400


def runtime_policy_enabled() -> bool:
    """``RUNTIME_POLICY_OVERRIDE``（默认关闭时本模块是零开销直通）。"""
    try:
        from app.core.feature_flags import feature_enabled

        return feature_enabled("RUNTIME_POLICY_OVERRIDE")
    except Exception:  # noqa: BLE001
        return False


class PolicyPublishConflict(RuntimeError):
    """策略发布失败（桶写入不完整，或期间有别的写者发布了新 epoch）。

    这是**有意的** fail-loud：宁可让运维看到"有人同时在改，请刷新"，也不能让一次
    静默覆盖把别人的策略抹掉——策略覆盖的是线上超时/并发/启停。
    """


@dataclass(slots=True)
class _CacheEntry:
    policy: RuntimePolicy
    #: 本地缓存**装载/确认**时刻：Redis 可用且 epoch 未变时不断刷新，因此只有
    #: Redis 不可用期间它才会变旧。
    loaded_at: float


@dataclass(slots=True)
class PolicyStore:
    """策略本地缓存 + 轮询器（进程内单例，见 :data:`policy_store`）。

    ``_redis_factory`` 可注入（测试）；生产用 ``app.core.redis.get_redis``。
    """

    _redis_factory: Any = None
    _clock: Any = None
    _entries: dict[str, _CacheEntry] = field(default_factory=dict)
    _epoch: int = 0
    _loaded_epoch: int = -1
    _last_poll_at: float = 0.0
    #: 最后一次**成功**与 Redis 确认的时刻（与 ``loaded_at`` 分开：前者是"策略从哪来"，
    #: 后者是"这份缓存多久没被确认过了"）。
    _last_success_at: float = 0.0
    _last_error: str = ""
    #: 连续 Redis 失败次数（运维面板据此判断"缓存还新不新"）。
    _failures: int = 0
    #: 桶里存在**无法解析**的条目（策略数据损坏）：宁可如实报告，也不假装没这回事。
    _corrupt: bool = False
    _lock: Any = None
    _task: Any = None
    _poll_count: int = 0

    def __post_init__(self) -> None:
        if self._clock is None:
            self._clock = time.time
        if self._lock is None:
            self._lock = asyncio.Lock()

    # ── Redis 句柄（不可用时返回 None，绝不抛给调用方）──────────

    def _redis(self) -> Any | None:
        try:
            if self._redis_factory is not None:
                return self._redis_factory()
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001 - 未初始化/不可用都按降级处理
            return None

    # ── 读：解析有效策略 ─────────────────────────────────────

    def resolve(
        self,
        *,
        provider_id: str = "",
        model: str = "",
    ) -> ResolvedPolicy:
        """按 ``default`` → ``model`` → ``provider`` 顺序合并运行时覆盖（**后者覆盖前者**）。

        为什么 provider 比 model 更"优先"：一次调用同时命中"模型名"和"提供方"是很常见的
        （``deepseek-v4-flash`` 由本机 Provider 或云端提供），而运维要干预的通常是**具体
        执行方**（某台设备太慢 → 只调它的超时）。因此更具体的 provider 覆盖更宽的 model，
        model 覆盖 default——顺序即优先级，且是**单层内逐字段合并**：
        ``provider`` 只改超时、``model`` 只改并发，两者都会生效。

        任何一条命中记录超过其 ``max_ttl_seconds``（或全局上限）即视为不可信，整条跳过；
        全部过期时如实返回 ``runtime_stale_fallback``，而不是假装"没有策略"。
        """
        timeout, concurrency, enabled = _code_defaults()
        if not runtime_policy_enabled():
            return ResolvedPolicy(
                timeout, concurrency, enabled, "code",
                policy_state=POLICY_STATE_NEVER_CONFIGURED,
            )
        now = float(self._clock())
        best_age = 0.0
        scope = ""
        source = "code"
        # 从宽到窄：default（最宽）→ model → provider（最具体）。
        for field_name in (
            default_field(),
            model_field(model) if model else "",
            provider_field(provider_id) if provider_id else "",
        ):
            if not field_name:
                continue
            entry = self._entries.get(field_name)
            if entry is None or not self._entry_usable(entry, now):
                continue
            policy = entry.policy
            if policy.timeout_seconds is not None:
                timeout = float(policy.timeout_seconds)
            if policy.max_concurrent is not None:
                concurrency = int(policy.max_concurrent)
            if policy.enabled is not None:
                enabled = bool(policy.enabled)
            best_age = max(best_age, max(0.0, now - entry.loaded_at))
            scope = field_name
            source = "runtime"
        if source == "runtime":
            state = (
                POLICY_STATE_DISABLED
                if (scope and self._entries.get(scope) is not None and enabled is False)
                else POLICY_STATE_ACTIVE
            )
            return ResolvedPolicy(
                timeout, concurrency, enabled, source, scope=scope,
                cache_age_seconds=best_age, policy_state=state,
            )
        # 没有任何**可信**命中：把原因说准（五类里的一种），不要笼统说"没有策略"。
        # 注意 ``source`` 的语义：真的连一条策略都没配过 = ``code``（旧行为）；
        # 配过但这次不可信 = ``runtime_stale_fallback``（调用方应据此更保守）。
        state = self._degraded_state(now)
        return ResolvedPolicy(
            timeout, concurrency, enabled,
            "code" if state == POLICY_STATE_NEVER_CONFIGURED else "runtime_stale_fallback",
            policy_state=state,
        )

    def _degraded_state(self, now: float) -> str:
        """没有可信运行时策略时，到底是哪一种（方案 §3 要求的五类区分）。"""
        if self._last_error == "redis_unavailable" or (self._failures and not self._entries):
            return POLICY_STATE_REDIS_UNAVAILABLE
        if self._corrupt:
            return POLICY_STATE_CORRUPT
        if not self._entries:
            return POLICY_STATE_NEVER_CONFIGURED
        # 有策略但全部超过 max_ttl：情况①"缓存过期"（Redis 故障与它可能同时成立，
        # 但过期是**本地可判定**的那个，所以优先如实报它）。
        return POLICY_STATE_CACHE_EXPIRED

    def timeout_seconds(self, *, provider_id: str = "", model: str = "", fallback: float = 0.0) -> float:
        """便捷入口：只要超时。``fallback`` 给了就作为"代码默认"用（调用点自己的默认值）。"""
        resolved = self.resolve(provider_id=provider_id, model=model)
        if resolved.source in {"code", "runtime_stale_fallback"} and fallback:
            return max(0.001, float(fallback))
        return resolved.timeout_seconds

    def max_concurrent(self, *, provider_id: str = "", model: str = "", fallback: int = 0) -> int:
        resolved = self.resolve(provider_id=provider_id, model=model)
        if resolved.source in {"code", "runtime_stale_fallback"} and fallback:
            return max(1, int(fallback))
        return resolved.max_concurrent

    def enabled(self, *, provider_id: str = "", model: str = "", fallback: bool = True) -> bool:
        resolved = self.resolve(provider_id=provider_id, model=model)
        if resolved.source in {"code", "runtime_stale_fallback"}:
            return bool(fallback)
        return resolved.enabled

    def _entry_usable(self, entry: _CacheEntry, now: float) -> bool:
        ttl = entry.policy.max_ttl_seconds or default_max_ttl_seconds()
        return (now - entry.loaded_at) <= max(1.0, float(ttl))

    # ── 轮询：epoch 变了才全量拉取 ───────────────────────────

    async def refresh_once(self) -> bool:
        """轮询一次：返回 ``True`` 表示这次真的刷新了本地缓存。

        失败（Redis 不可用）**不抛错**：调用方继续用本地缓存，直到 ``max_ttl`` 到期。
        """
        client = self._redis()
        self._last_poll_at = float(self._clock())
        self._poll_count += 1
        if client is None:
            self._failures += 1
            self._last_error = "redis_unavailable"
            return False
        try:
            raw_epoch = await client.get(POLICY_EPOCH_KEY)
            epoch = int(str(raw_epoch or "0") or "0")
        except Exception as exc:  # noqa: BLE001 - 读失败沿用本地缓存
            self._failures += 1
            self._last_error = str(exc)[:160]
            logger.debug("[policy] epoch 读取失败（沿用本地缓存）: {}", str(exc)[:120])
            return False
        self._epoch = epoch
        if epoch == self._loaded_epoch and self._entries:
            # epoch 未变：刷新缓存时刻（Redis 就在手边，说明这份缓存仍然可信）。
            now = float(self._clock())
            for entry in self._entries.values():
                entry.loaded_at = now
            self._failures = 0
            self._last_error = ""
            self._last_success_at = now
            return False
        return await self._load_bucket(epoch)

    async def _load_bucket(self, epoch: int) -> bool:
        client = self._redis()
        if client is None:
            return False
        try:
            raw = await client.hgetall(policy_bucket_key(epoch))
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            self._last_error = str(exc)[:160]
            logger.debug("[policy] 策略桶读取失败: {}", str(exc)[:120])
            return False
        entries: dict[str, _CacheEntry] = {}
        corrupt = 0
        now = float(self._clock())
        for field_name, value in (raw or {}).items():
            name = field_name.decode() if isinstance(field_name, bytes) else str(field_name)
            policy = RuntimePolicy.parse(value, field_name=name)
            if policy is None:
                corrupt += 1
                logger.warning("[policy] 跳过无法解析的策略 field={}", str(name)[:80])
                continue
            entries[name] = _CacheEntry(policy=policy, loaded_at=now)
        # 全量替换：epoch 是新的，旧桶内容一律作废（不做增量合并，避免脏残留）。
        self._entries = entries
        self._loaded_epoch = epoch
        self._corrupt = corrupt > 0
        # ``_epoch`` 是"本地当前生效的 epoch"，必须与刚装载的桶一致：
        # 只写 ``_loaded_epoch`` 会让 snapshot() 报 0，运维面板就会显示"策略没生效"
        # 而实际上已经在用了（观测与事实不一致比没有观测更糟）。
        self._epoch = epoch
        self._failures = 0
        self._last_error = ""
        logger.info("[policy] 运行时策略已刷新 epoch={} 条数={}", epoch, len(entries))
        return True

    async def start_polling(self, *, interval_seconds: float | None = None) -> asyncio.Task | None:
        """启动后台轮询协程（每 ``interval`` 秒一次；已在跑则不重复启动）。

        最坏情况下策略生效延迟 = 一个轮询间隔（默认 10s）。这就是"用轮询代替配置中心"
        的代价，换来的是"没有额外依赖、Worker 挂了也不影响策略读取"。
        """
        if self._task is not None and not self._task.done():
            return self._task
        interval = float(interval_seconds if interval_seconds is not None else poll_interval_seconds())
        if not runtime_policy_enabled():
            logger.debug("[policy] RUNTIME_POLICY_OVERRIDE 关闭，不启动轮询")
            return None
        # 先拉一次：不让"启动后第一个 10 秒"成为策略真空期。
        try:
            await self.refresh_once()
        except Exception as exc:  # noqa: BLE001 - 启动拉取失败沿用默认值
            logger.debug("[policy] 启动刷新失败（沿用代码默认值）: {}", str(exc)[:120])
        self._task = asyncio.create_task(self._poll_loop(interval))
        return self._task

    async def _poll_loop(self, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(max(1.0, interval))
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 轮询绝不能把进程打挂
                self._failures += 1
                self._last_error = str(exc)[:160]
                logger.debug("[policy] 轮询异常（继续下一轮）: {}", str(exc)[:120])

    async def stop_polling(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def aclose(self) -> None:
        await self.stop_polling()

    # ── 写：Admin API 用 ────────────────────────────────────

    async def put(
        self,
        policy: RuntimePolicy,
        *,
        epoch: int | None = None,
        bucket_ttl_seconds: int = 0,
    ) -> int:
        """写入一条策略并**原子发布**（返回新的 epoch）。

        发布顺序是硬要求（方案 §1 的竞态修复）：

            以 **Redis 当前 epoch** 为基准读全桶 → 应用本次改动 → 写**新**桶并校验
            → CAS 发布（只有指针仍指向基准时才切换）

        三个都不能省：

        * **不能先推进 epoch 再写桶**：中间窗口里所有 Worker 都会读到**空桶**，表现为
          "改一条策略把线上全部覆盖清空"，而且只在竞态窗口出现；
        * **基准必须是 Redis 的当前 epoch，不是本地缓存 epoch**：用本地 epoch 做基准，
          两个 Worker 会各自基于旧状态生成新桶并互相覆盖（丢改动）；
        * **必须校验桶内容**：写失败/半写时宁可发布失败（返回 409 语义的错误），
          也不能让指针指向一个不完整的桶。

        CAS 失败说明有并发写者刚刚改了同一份策略。此时**不重放本次改动**：运维的
        改动可能已经语义过时（例如他基于旧列表做的修改），静默覆盖比报错更危险。
        调用方拿到 :class:`PolicyPublishConflict` 后应刷新面板再决定。
        """
        client = self._redis()
        if client is None:
            raise RuntimeError("Redis 不可用，无法写入运行时策略")
        return await self._mutate(
            client,
            lambda rows: {**rows, policy.field_name: policy.to_json()},
            bucket_ttl_seconds=bucket_ttl_seconds,
            explicit_epoch=epoch,
        )

    async def delete(self, field_name: str, *, bucket_ttl_seconds: int = 0) -> int:
        """删掉某个主体的覆盖（重新回落 ``.env`` / 代码默认值），同样原子发布。"""
        client = self._redis()
        if client is None:
            raise RuntimeError("Redis 不可用，无法删除运行时策略")
        target = str(field_name or "")
        return await self._mutate(
            client,
            lambda rows: {name: value for name, value in rows.items() if name != target},
            bucket_ttl_seconds=bucket_ttl_seconds,
        )

    async def _mutate(
        self,
        client: Any,
        mutate: Any,
        *,
        bucket_ttl_seconds: int = 0,
        explicit_epoch: int | None = None,
    ) -> int:
        """读基准 → 改 → 写新桶 → 校验 → CAS 发布（唯一写入路径）。"""
        baseline_epoch = await self._authoritative_epoch(client)
        rows = dict(await self._read_bucket(client, baseline_epoch))
        rows = dict(mutate(rows))
        ttl = max(1, int(bucket_ttl_seconds or policy_bucket_ttl_seconds()))
        candidate = int(explicit_epoch) if explicit_epoch is not None else baseline_epoch + 1
        # 候选 epoch 必须**大于**基准：否则会覆盖一个可能仍在被读的桶。
        if candidate <= baseline_epoch:
            candidate = baseline_epoch + 1
        key = policy_bucket_key(candidate)
        if rows:
            await client.hset(key, mapping=rows)
            await client.expire(key, ttl)
            # 校验：读回条数一致才算"完整写入"（半写/失败时绝不发布指针）。
            written = await client.hlen(key)
            if int(written or 0) != len(rows):
                raise PolicyPublishConflict(
                    f"策略桶写入不完整（期望 {len(rows)} 条，实际 {written} 条），已放弃发布"
                )
        published = await self._publish_epoch(client, expected=baseline_epoch, target=candidate)
        if published != candidate:
            # 期间有别的写者发布了新策略：不静默重放（见 put 的 docstring）。
            raise PolicyPublishConflict(
                f"策略已被其它写者更新（期望基准 epoch={baseline_epoch}，当前 {published}），请刷新后重试"
            )
        await self._load_bucket(candidate)
        return candidate

    @staticmethod
    async def _authoritative_epoch(client: Any) -> int:
        """读 Redis 的**当前** epoch（发布基准；本地缓存 epoch 不能当基准）。"""
        try:
            value = await client.eval(_CURRENT_EPOCH_SCRIPT, 1, POLICY_EPOCH_KEY)
            return int(value or 0)
        except Exception:  # noqa: BLE001 - 不支持 eval 的替身/旧客户端退回普通 GET
            try:
                return int(str(await client.get(POLICY_EPOCH_KEY) or "0") or "0")
            except Exception:  # noqa: BLE001
                return 0

    @staticmethod
    async def _publish_epoch(client: Any, *, expected: int, target: int) -> int:
        """CAS 发布 epoch：只有当前值仍等于 ``expected`` 时才切到 ``target``。"""
        try:
            result = await client.eval(
                _PUBLISH_EPOCH_SCRIPT, 1, POLICY_EPOCH_KEY, str(int(expected)), str(int(target))
            )
            return int(result or 0)
        except Exception:  # noqa: BLE001 - 不支持 eval 时退回非原子的 SET
            current = await PolicyStore._authoritative_epoch(client)
            if current != int(expected):
                return current
            await client.set(POLICY_EPOCH_KEY, str(int(target)))
            return int(target)

    @staticmethod
    async def _read_bucket(client: Any, epoch: int) -> dict[str, str]:
        """读一个桶成普通 dict（读不到返回空，不抛）。"""
        if int(epoch) < 0:
            return {}
        try:
            raw = await client.hgetall(policy_bucket_key(int(epoch)))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[policy] 桶读取失败 epoch={}: {}", int(epoch), str(exc)[:120])
            return {}
        out: dict[str, str] = {}
        for key, value in (raw or {}).items():
            name = key.decode() if isinstance(key, bytes) else str(key)
            out[name] = value.decode() if isinstance(value, bytes) else str(value)
        return out

    @staticmethod
    async def _write_bucket(client: Any, epoch: int, rows: dict[str, str], ttl_seconds: int) -> None:
        key = policy_bucket_key(int(epoch))
        await client.hset(key, mapping=dict(rows))
        # 桶必须带 TTL：否则每次改策略都会留一个永久键，Redis 迟早被运维面板写满。
        await client.expire(key, max(1, int(ttl_seconds or policy_bucket_ttl_seconds())))

    @staticmethod
    async def _next_epoch(client: Any) -> int:
        """**仅用于测试/兼容**：现在的发布路径一律走 CAS（见 ``_publish_epoch``）。"""
        try:
            return int(await client.incr(POLICY_EPOCH_KEY))
        except Exception:  # noqa: BLE001 - 计数不可用时退回"当前+1"
            current = await client.get(POLICY_EPOCH_KEY)
            return int(str(current or "0") or "0") + 1

    # ── 观测 ────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """运维面板/排障用的只读快照（不含任何用户数据）。"""
        now = float(self._clock())
        code_timeout, code_concurrency, _ = _code_defaults()
        rows = []
        for name, entry in sorted(self._entries.items()):
            ttl = entry.policy.max_ttl_seconds or default_max_ttl_seconds()
            age = max(0.0, now - entry.loaded_at)
            rows.append(
                {
                    **entry.policy.to_payload(),
                    "field": name,
                    "cache_age_seconds": round(age, 3),
                    "max_ttl_seconds": float(ttl),
                    "usable": self._entry_usable(entry, now),
                }
            )
        return {
            "enabled": runtime_policy_enabled(),
            "epoch": int(self._epoch),
            "loaded_epoch": int(self._loaded_epoch),
            # 权威 epoch（Redis 侧）与本地生效 epoch 分开报：两者不一致就是"还没轮询到"，
            # 运维面板据此判断"改完是不是所有 Worker 都换过来了"。
            "authoritative_epoch": int(self._epoch),
            "state": self._degraded_state(now) if not self._entries else POLICY_STATE_ACTIVE,
            "corrupt_entries": bool(self._corrupt),
            "entries": rows,
            "count": len(rows),
            "last_poll_at": float(self._last_poll_at),
            "last_success_at": float(self._last_success_at),
            "poll_count": int(self._poll_count),
            "poll_interval_seconds": float(poll_interval_seconds()),
            "consecutive_failures": int(self._failures),
            "last_error": self._last_error,
            "code_defaults": {
                "timeout_seconds": code_timeout,
                "max_concurrent": code_concurrency,
            },
            "max_ttl_seconds": float(default_max_ttl_seconds()),
        }

    def expire_all_for_tests(self) -> None:
        """把本地缓存时刻往前推，用于测试"缓存超过 max_ttl → 退回默认值"。"""
        self._entries = {}
        self._loaded_epoch = -1
        self._corrupt = False


#: 进程内单例（Admin API 与调用点共用同一份缓存）。
policy_store = PolicyStore()


__all__ = [
    "POLICY_BUCKET_PREFIX",
    "POLICY_EPOCH_KEY",
    "POLICY_STATE_ACTIVE",
    "POLICY_STATE_CACHE_EXPIRED",
    "POLICY_STATE_CORRUPT",
    "POLICY_STATE_DISABLED",
    "POLICY_STATE_NEVER_CONFIGURED",
    "POLICY_STATE_REDIS_UNAVAILABLE",
    "SCOPE_DEFAULT",
    "SCOPE_MODEL",
    "SCOPE_PROVIDER",
    "PolicyPublishConflict",
    "PolicyStore",
    "ResolvedPolicy",
    "RuntimePolicy",
    "default_field",
    "default_max_ttl_seconds",
    "model_field",
    "policy_bucket_key",
    "policy_bucket_ttl_seconds",
    "policy_epoch_key",
    "policy_store",
    "poll_interval_seconds",
    "provider_field",
    "runtime_policy_enabled",
]
