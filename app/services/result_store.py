"""统一结果存储（ResultStore）的生产实现：分层存储 + 完整性校验 + 版本化解析。

方案《结果存储、检查点与恢复（整合版）》第一节的落点。**唯一收口**：

* 结果脱敏（委托 ``sanitize_dependency_result``，与既有依赖上下文同一套规则）；
* 计算 sha256（完整性校验，hash 不符即拒绝）；
* 判断存储位置（按序列化字节数分层：Redis / 本地 / Blob）；
* 保存并返回统一引用 :class:`~lumi_contracts.persistence.result_store.ResultRef`；
* 按用户 / 任务 / 权限读取（``owner_id`` 不匹配一律拒绝）；
* 过期返回 ``RESULT_REF_EXPIRED``——**不静默当空结果**；
* 按存储时的 ``schema_version`` 解析历史数据，解析失败降级为原始内容展示。

后端端口（`ResultKvPort` / `ResultBlobPort`）由本模块提供生产实现：

* :class:`RedisKvPort`：Redis，不可用时回退进程内存（本地开发/单测，与既有行为一致）；
* :class:`LocalBlobPort`：本地受控目录（``data/uploads/agent_results/...``）；
* :class:`S3BlobPort`：S3 兼容对象存储（MinIO / OSS），``boto3`` 惰性导入——没有该
  依赖时保持"不可用"，由调用方按 ``RESULT_STORE_BLOB_FALLBACK_LOCAL`` 决定回退或失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from lumi_contracts.persistence.result_store import (
    RESULT_REF_EXPIRED,
    RESULT_REF_FORBIDDEN,
    RESULT_REF_INTEGRITY_FAILED,
    RESULT_REF_SCHEMA_MISMATCH,
    RESULT_REF_UNAVAILABLE,
    BoundedLoad,
    LoadBudget,
    ResultDegradation,
    ResultRef,
    ResultRefError,
    ResultStorageKind,
    TieringPolicy,
    bound_body,
    canonical_json,
    digest_bytes,
    ref_from_mapping,
    resolve_expiry,
)

#: 结果存储根目录名（相对 ``settings.UPLOAD_DIR``）。
RESULT_STORE_DIRNAME = "agent_results"
#: 引用键前缀（``{prefix}:{owner_key}:{result_id}``）；业务层不得直接使用。
RESULT_KEY_PREFIX = "agent:execution:result"


# ── 后端端口实现 ─────────────────────────────────────────────


class _MemoryKv:
    """进程内存兜底（Redis 不可用）：只保证同一进程内的读回，不伪装成持久化。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        item = self._items.get(key)
        if item is None:
            return None
        payload, expires_at = item
        if expires_at and time.time() >= expires_at:
            self._items.pop(key, None)
            return None
        return payload

    def put(self, key: str, payload: str, ttl_seconds: int) -> None:
        expires_at = (time.time() + int(ttl_seconds)) if int(ttl_seconds) > 0 else 0.0
        self._items[key] = (payload, expires_at)

    def delete(self, key: str) -> None:
        self._items.pop(key, None)

    def find_by_result_id(self, result_id: str) -> str | None:
        """按 result_id 反查（仅供权限区分：结果到底是不存在还是属于别人）。"""
        suffix = f":{result_id}"
        for key, (payload, expires_at) in list(self._items.items()):
            if not str(key).endswith(suffix):
                continue
            if expires_at and time.time() >= expires_at:
                self._items.pop(key, None)
                continue
            return payload
        return None


class RedisKvPort:
    """小结果键值端口：优先 Redis，客户端不可用时回退进程内存。"""

    def __init__(self, *, redis: Any = None) -> None:
        self._redis = redis
        self._memory = _MemoryKv()

    def _client(self) -> Any:
        if self._redis is not None:
            return self._redis
        from app.core.redis import get_redis

        return get_redis()

    async def put(self, key: str, payload: str, *, ttl_seconds: int) -> None:
        try:
            client = self._client()
            if int(ttl_seconds) > 0:
                await client.set(key, payload, ex=int(ttl_seconds))
            else:
                await client.set(key, payload)
            return
        except Exception as exc:  # noqa: BLE001 - Redis 不可用时必须仍能读回本次结果
            logger.debug("[result-store] Redis 写入不可用，回退内存: {}", str(exc)[:120])
        self._memory.put(key, payload, ttl_seconds)

    async def get(self, key: str) -> str | None:
        try:
            client = self._client()
            raw = await client.get(key)
            if raw:
                return str(raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[result-store] Redis 读取不可用，尝试内存: {}", str(exc)[:120])
        return self._memory.get(key)

    async def delete(self, key: str) -> None:
        try:
            client = self._client()
            await client.delete(key)
        except Exception:  # noqa: BLE001 - 删除失败不影响引用语义
            pass
        self._memory.delete(key)

    async def find_by_result_id(self, result_id: str) -> str | None:
        """按 result_id 在键空间里反查（仅用于区分"不存在"与"属于别人"）。

        正常读取路径永远按 ``所有者键 + result_id`` 直取；这个扫描只服务于**权限判定**，
        且只会在"直取未命中"时发生。
        """
        suffix = f":{result_id}"
        try:
            client = self._client()
            async for key in client.scan_iter(match=f"{RESULT_KEY_PREFIX}:*:{result_id}"):
                raw = await client.get(key)
                if raw:
                    return str(raw)
        except Exception as exc:  # noqa: BLE001 - 扫描不可用按"未找到"处理
            logger.debug("[result-store] 反查不可用: {}", str(exc)[:120])
        return self._memory.find_by_result_id(suffix.lstrip(":"))


class LocalBlobPort:
    """本地受控目录（中等结果 / 无对象存储时的 Blob 后端）。

    键 → 路径的映射由 :func:`local_blob_path` 统一实现，并做目录越权校验：
    越权键直接抛错，绝不落到根目录之外。
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else default_blob_root()

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        return local_blob_path(self._root, key)

    async def put(self, key: str, payload: bytes) -> None:
        path = self._path(key)
        await asyncio.to_thread(_write_bytes, path, payload)

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return await asyncio.to_thread(_read_bytes, path)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(_unlink, path)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3BlobPort:
    """S3 兼容对象存储（MinIO / OSS / S3）。

    依赖 ``boto3``（可选）。缺失时 :meth:`available` 为假，由调用方决定回退本地还是
    直接失败——**绝不静默把大结果当成"已存"**。
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_url: str = "",
        region: str = "",
        access_key: str = "",
        secret_key: str = "",
        use_ssl: bool = True,
    ) -> None:
        self._bucket = str(bucket or "")
        self._prefix = str(prefix or "").strip("/")
        self._endpoint = str(endpoint_url or "")
        self._region = str(region or "")
        self._access_key = str(access_key or "")
        self._secret_key = str(secret_key or "")
        self._use_ssl = bool(use_ssl)
        self._client: Any = None
        self._import_failed = False

    @property
    def bucket(self) -> str:
        return self._bucket

    def available(self) -> bool:
        """后端是否真的可用（bucket 已配置且 ``boto3`` 可导入）。"""
        if not self._bucket:
            return False
        if self._import_failed:
            return False
        try:
            import boto3  # noqa: F401
        except Exception:  # noqa: BLE001 - 没有 boto3 就是"不可用"，不是异常
            self._import_failed = True
            return False
        return True

    def _object_key(self, key: str) -> str:
        clean = str(key or "").strip("/")
        return f"{self._prefix}/{clean}" if self._prefix else clean

    def _s3(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint or None,
                region_name=self._region or None,
                aws_access_key_id=self._access_key or None,
                aws_secret_access_key=self._secret_key or None,
                use_ssl=self._use_ssl,
            )
        return self._client

    async def put(self, key: str, payload: bytes) -> None:
        await asyncio.to_thread(
            self._s3().put_object,
            Bucket=self._bucket,
            Key=self._object_key(key),
            Body=payload,
        )

    async def get(self, key: str) -> bytes | None:
        def _read() -> bytes | None:
            try:
                response = self._s3().get_object(Bucket=self._bucket, Key=self._object_key(key))
                return response["Body"].read()
            except Exception as exc:  # noqa: BLE001 - 不存在按 None，其余也按不可读
                logger.debug("[result-store] S3 读取失败 {}: {}", key[:24], str(exc)[:120])
                return None

        return await asyncio.to_thread(_read)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(
            self._s3().delete_object, Bucket=self._bucket, Key=self._object_key(key)
        )

    async def exists(self, key: str) -> bool:
        def _head() -> bool:
            try:
                self._s3().head_object(Bucket=self._bucket, Key=self._object_key(key))
                return True
            except Exception:  # noqa: BLE001
                return False

        return await asyncio.to_thread(_head)


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def default_blob_root() -> Path:
    """本地 Blob 根目录：``{UPLOAD_DIR}/agent_results``（受控目录，不暴露给业务层）。"""
    try:
        from app.core.config import settings

        return Path(settings.UPLOAD_DIR) / RESULT_STORE_DIRNAME
    except Exception:  # noqa: BLE001 - 配置不可用时退回仓库内 data 目录
        return Path("data") / RESULT_STORE_DIRNAME


def local_blob_path(root: Path, key: str) -> Path:
    """键 → 本地路径（含越权校验：解析后必须仍在根目录内）。"""
    safe = str(key or "").replace("\\", "/").strip("/")
    if not safe or ".." in safe.split("/"):
        raise ValueError(f"非法结果存储键：{key!r}")
    base = Path(root).resolve()
    target = (base / safe).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"结果存储键越权：{key!r}")
    return target


# ── 统一引用体系 ─────────────────────────────────────────────


def owner_key(user_id: str) -> str:
    """所有者键（sha256 前 24 位）：Redis 键与本地目录都按它隔离。"""
    return hashlib.sha256(str(user_id or "").encode("utf-8")).hexdigest()[:24]


def result_storage_key(user_id: str, result_id: str) -> str:
    """Redis 键（业务层不得直接构造；需要时走本函数，便于将来换 key 空间）。"""
    return f"{RESULT_KEY_PREFIX}:{owner_key(user_id)}:{result_id}"


def blob_object_key(user_id: str, result_id: str) -> str:
    """Blob 对象键（相对根/桶）：``{owner_key}/{result_id}.json``。"""
    return f"{owner_key(user_id)}/{result_id}.json"


@dataclass(frozen=True, slots=True)
class SaveResultRequest:
    """一次结果保存请求（业务层只提供结果与归属，不选存储后端）。"""

    result: Any = None
    user_id: str = ""
    job_id: str = ""
    step_id: str = ""
    tool_name: str = ""
    schema_name: str = "execution_result"
    schema_version: int = 1
    content_type: str = "application/json"
    ttl_seconds: int = 0
    #: 服务端已知的产物引用（正文已在产物存储里）。
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    #: 结果类型提示（``execution_result`` / ``text`` / ``binary``）。
    kind: str = ""


@dataclass(frozen=True, slots=True)
class SaveReceipt:
    """一次保存的回执：引用 + 实际落点 + 是否脱敏 + 体积。"""

    ref: ResultRef
    storage_kind: str = ResultStorageKind.REDIS.value
    size_bytes: int = 0
    sanitized: bool = False

    @property
    def minimal_ref(self) -> dict[str, str]:
        return self.ref.as_ref_dict()


@dataclass(frozen=True, slots=True)
class RefResolution:
    """一次引用解析结果（完整态：正文 + 降级事实）。"""

    ref: ResultRef
    body: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    degraded: bool = False
    degradation: str = ResultDegradation.NONE.value
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.degraded


class ResultStore:
    """统一结果存储：唯一保存 / 读取 / 校验 / 过期入口。"""

    def __init__(
        self,
        *,
        kv: Any = None,
        local_blob: Any = None,
        blob: Any = None,
        tiering: TieringPolicy | None = None,
        default_ttl_seconds: int = 0,
        clock: Any = None,
        sanitizer: Any = None,
    ) -> None:
        self._kv = kv if kv is not None else RedisKvPort()
        self._local = local_blob if local_blob is not None else LocalBlobPort()
        self._blob = blob
        self._tiering = tiering or tiering_from_settings()
        self._ttl = int(default_ttl_seconds or default_result_ttl_seconds())
        self._clock = clock or time.time
        self._sanitizer = sanitizer

    # ── 保存 ──────────────────────────────────────────────

    async def save(self, request: SaveResultRequest) -> SaveReceipt | None:
        """脱敏 → 算摘要 → 分层 → 保存 → 返回统一引用（空结果返回 ``None``）。"""
        if request.result is None:
            return None
        body, sanitized = self._sanitize(request.result)
        raw = canonical_json(body)
        if not raw or raw in {"{}", "null"}:
            return None
        payload = raw.encode("utf-8")
        digest = digest_bytes(payload)
        result_id = uuid.uuid4().hex
        created_at = float(self._clock())
        expires_at = resolve_expiry(created_at=created_at, ttl_seconds=request.ttl_seconds or self._ttl)
        blob_available = self._blob_available()
        kind = self._tiering.decide(len(payload), blob_available=blob_available)
        ref = ResultRef(
            id=result_id,
            sha256=digest,
            storage_kind=kind.value,
            content_type=str(request.content_type or "application/json"),
            size=len(payload),
            # 引用里存的是**所有者键**（user id 的 sha256 前 24 位），不是明文 user id：
            # 结果引用可能被写进快照/事件，不该把用户标识带出去。
            owner_id=owner_key(request.user_id),
            job_id=str(request.job_id or ""),
            step_id=str(request.step_id or ""),
            schema_name=str(request.schema_name or "execution_result"),
            schema_version=int(request.schema_version or 1),
            expires_at=expires_at,
            created_at=created_at,
        )
        record = {
            "sha256": digest,
            "body": body,
            "ref": ref.as_dict(include_artifacts=True),
            # 所有者键一并落记录：按 result_id 直查（/jobs/{id}/results/{rid}）时
            # 不必扫描键空间，权限也仍然只认这一份记录里的 owner。
            "owner_key": owner_key(request.user_id),
            "artifact_refs": [dict(item) for item in (request.artifact_refs or [])],
            "created_at": created_at,
            # 到期时间同时记在记录里：最小引用（旧快照只有 id+sha256）也能判过期。
            "expires_at": expires_at,
            "schema_name": ref.schema_name,
            "schema_version": ref.schema_version,
            "content_type": ref.content_type,
            "size": ref.size,
        }
        await self._write(kind, ref, record, payload)
        logger.debug(
            "[result-store] 保存 result={} kind={} size={}B schema={}@{}",
            result_id[:12],
            kind.value,
            len(payload),
            ref.schema_name,
            ref.schema_version,
        )
        return SaveReceipt(ref=ref, storage_kind=kind.value, size_bytes=len(payload), sanitized=sanitized)

    async def save_ref(self, request: SaveResultRequest) -> dict[str, str] | None:
        """保存并只返回**最小引用**（``{"id", "sha256"}``，写进 Job/事件/快照）。"""
        receipt = await self.save(request)
        return receipt.minimal_ref if receipt is not None else None

    # ── 读取 ──────────────────────────────────────────────

    async def load(
        self,
        result_ref: Any,
        *,
        user_id: str = "",
        budget: LoadBudget | None = None,
        strict_owner: bool = True,
    ) -> RefResolution:
        """按引用读取完整结果（含过期 / 权限 / 完整性 / 版本四项校验）。"""
        ref = self._require_ref(result_ref)
        self._check_expiry(ref)
        if strict_owner:
            self._check_owner(ref, user_id=user_id, strict=True)
        record = await self._read_for_requester(ref, user_id=user_id, strict=strict_owner)
        return self._resolve_record(ref, record)

    async def load_for_owner(
        self,
        ref: ResultRef,
        *,
        max_chars: int = 0,
        page_size: int = 0,
        page_offset: int = 0,
        fields: tuple[str, ...] = (),
        summary_only: bool = False,
    ) -> RefResolution:
        """按 ``ref.owner_id``（所有者键）**直查**（``GET /jobs/{id}/results/{rid}``）。

        与 :meth:`load` 的差别：调用方已经知道归属（由任务所有者推导），因此不需要按
        请求方身份推导所有者键，也不做键空间反查；其余校验（过期 / 完整性 / 版本化解析）
        完全一致——**直查不等于放宽校验**。
        """
        owner = str(ref.owner_id or "").strip()
        if not owner or not ref.id:
            raise ResultRefError(
                RESULT_REF_UNAVAILABLE, details={"result_id": str(ref.id or "")}
            )
        record = await self._read_record(ref, user_id="")
        resolution = self._resolve_record(ref, record)
        if max_chars or page_size or fields or summary_only:
            budget = LoadBudget(
                fields=fields,
                summary_only=bool(summary_only),
                page_size=int(page_size or 0),
                page_offset=int(page_offset or 0),
                max_chars=int(max_chars or 0),
            )
            if not resolution.degraded:
                bounded = bound_body(resolution.body, budget)
                resolution = RefResolution(
                    ref=resolution.ref,
                    body=bounded.body,
                    raw=resolution.raw,
                    degraded=resolution.degraded,
                    degradation=resolution.degradation,
                    note=resolution.note,
                )
        return resolution

    def _resolve_record(
        self, ref: ResultRef, record: dict[str, Any] | None
    ) -> RefResolution:
        """记录 → 解析结论（过期 / 完整性 / 版本化解析的**唯一**实现）。"""
        if record is None:
            raise ResultRefError(
                RESULT_REF_UNAVAILABLE,
                details={"result_id": ref.id, "storage_kind": ref.storage_kind},
            )
        # 记录里才有真实到期时间：最小引用（旧快照 id+sha256）在这里补做过期判定。
        self._check_expiry(ref, record=record)
        # 记录里的引用是权威元数据（storage_kind / schema / 到期都在这里）。
        stored_ref = ref_from_mapping(record.get("ref"))
        if stored_ref is not None:
            ref = stored_ref
        body = record.get("body")
        text = canonical_json(body) if isinstance(body, (dict, list)) else str(body or "")
        digest = str(record.get("sha256") or ref.sha256 or "")
        if digest and text and digest_bytes(text) != digest:
            # 完整性失败：拒绝交付正文，只允许降级展示引用本身。
            raise ResultRefError(
                RESULT_REF_INTEGRITY_FAILED,
                details={"result_id": ref.id, "expected": digest, "schema": ref.schema_name},
            )
        stored_version = int(record.get("schema_version") or ref.schema_version or 1)
        stored_name = str(record.get("schema_name") or ref.schema_name or "")
        if isinstance(body, str):
            return RefResolution(
                ref=ref,
                body={"content": body},
                raw=body,
                degraded=True,
                degradation=ResultDegradation.BODY_UNAVAILABLE.value,
                note="历史记录只有文本正文，未保留结构化载荷。",
            )
        if not isinstance(body, dict):
            return RefResolution(
                ref=ref,
                body={"content": text},
                raw=text,
                degraded=True,
                degradation=ResultDegradation.BODY_UNAVAILABLE.value,
                note="结果载荷不是结构化对象，按原始内容展示。",
            )
        if not self._schema_compatible(stored_name, stored_version):
            return RefResolution(
                ref=ref,
                body=body,
                raw=text,
                degraded=True,
                degradation=ResultDegradation.SCHEMA_MISMATCH.value,
                note=(
                    f"结果按存储时契约 {stored_name}@{stored_version} 解析失败，"
                    "已降级为原始内容展示（不会用当前版本误解析历史数据）。"
                ),
            )
        return RefResolution(ref=ref, body=body, raw=text)

    async def load_body(
        self,
        result_ref: Any,
        *,
        user_id: str = "",
        budget: LoadBudget | None = None,
        strict_owner: bool = True,
    ) -> dict[str, Any] | None:
        """兼容既有调用方：只拿正文（``None`` 表示不可用，不抛错）。

        需要区分"过期 / 权限 / 完整性"时请用 :meth:`load`——它会给出稳定错误码。
        """
        try:
            resolution = await self.load(
                result_ref, user_id=user_id, budget=budget, strict_owner=strict_owner
            )
        except ResultRefError as exc:
            logger.debug("[result-store] 引用解析失败 {}: {}", exc.code, exc.message[:120])
            return None
        return resolution.body

    async def load_bounded(
        self,
        result_ref: Any,
        *,
        user_id: str = "",
        budget: LoadBudget | None = None,
        strict_owner: bool = True,
    ) -> tuple[RefResolution, BoundedLoad]:
        """按预算加载（方案 §1.4：只读摘要 / 字段 / 分页 / 字符预算）。"""
        effective = budget or LoadBudget(max_chars=default_load_max_chars())
        resolution = await self.load(
            result_ref, user_id=user_id, budget=effective, strict_owner=strict_owner
        )
        if resolution.degraded:
            return resolution, BoundedLoad(
                body=resolution.body, truncated=False, total_chars=len(resolution.raw)
            )
        bounded = bound_body(resolution.body, effective)
        return resolution, bounded

    async def head(self, result_ref: Any, *, user_id: str = "") -> ResultRef | None:
        """只读引用元数据（不加载正文）：用于恢复前的可恢复性判断。"""
        try:
            ref = self._require_ref(result_ref)
        except ResultRefError:
            return None
        try:
            record = await self._read_for_requester(ref, user_id=user_id, strict=bool(user_id))
        except ResultRefError:
            return None
        if record is None:
            return None
        try:
            self._check_expiry(ref, record=record)
        except ResultRefError:
            return None
        stored = record.get("ref")
        merged = ref_from_mapping(stored) if isinstance(stored, dict) else None
        return merged or ref

    async def exists(self, result_ref: Any, *, user_id: str = "") -> bool:
        return await self.head(result_ref, user_id=user_id) is not None

    async def release(self, result_ref: Any) -> None:
        """显式释放（过期清理 / 撤销）：按分层后端删除，失败只记日志。"""
        ref = self._require_ref(result_ref)
        try:
            if ref.storage is ResultStorageKind.REDIS:
                await self._kv.delete(result_storage_key(ref.owner_id, ref.id))
                return
            backend = self._backend_for(ref.storage)
            await backend.delete(blob_object_key(ref.owner_id, ref.id))
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响主流程
            logger.debug("[result-store] 释放引用失败 {}: {}", ref.id[:12], str(exc)[:120])

    # ── 分层后端 ──────────────────────────────────────────

    def _blob_available(self) -> bool:
        if self._blob is None:
            return False
        checker = getattr(self._blob, "available", None)
        return bool(checker()) if callable(checker) else True

    def _backend_for(self, kind: ResultStorageKind) -> Any:
        if kind is ResultStorageKind.BLOB:
            return self._blob if self._blob is not None else self._local
        return self._local

    async def _write(
        self,
        kind: ResultStorageKind,
        ref: ResultRef,
        record: dict[str, Any],
        payload: bytes,
    ) -> None:
        text = json.dumps(record, ensure_ascii=False, default=str)
        if kind is ResultStorageKind.REDIS:
            await self._kv.put(
                result_storage_key(ref.owner_id, ref.id), text, ttl_seconds=self._ttl
            )
            return
        backend = self._backend_for(kind)
        try:
            await backend.put(blob_object_key(ref.owner_id, ref.id), payload)
        except Exception as exc:  # noqa: BLE001 - Blob 写失败绝不伪装成功
            raise ResultRefError(
                RESULT_REF_UNAVAILABLE,
                "结果正文写入存储后端失败。",
                retryable=True,
                details={"result_id": ref.id, "storage_kind": kind.value, "error": str(exc)[:200]},
            ) from exc
        # 引用元数据放小 KV（TTL 与正文一致），正文在大存储：读取时先取元数据再取正文。
        await self._kv.put(
            result_storage_key(ref.owner_id, ref.id),
            json.dumps(
                {**record, "body": None, "body_in_blob": True},
                ensure_ascii=False,
                default=str,
            ),
            ttl_seconds=self._ttl,
        )

    async def _read_for_requester(
        self,
        ref: ResultRef,
        *,
        user_id: str,
        strict: bool,
    ) -> dict[str, Any] | None:
        """按请求方身份读取记录，并做**准确**的权限判定。

        判定顺序很重要：最小引用（旧快照只有 ``{id, sha256}``）里没有 ``owner_id``，
        若直接按"请求方的所有者键"查找，跨用户读取会退化成 ``RESULT_REF_UNAVAILABLE``，
        让人误以为"结果丢了"。这里先查请求方的键；查不到且要求严格归属时，再**只按
        result_id** 定位记录以区分两类情况：

        * 记录存在但所有者不同 → ``RESULT_REF_FORBIDDEN``（越权，明确拒绝）；
        * 记录确实不存在 → 返回 ``None``，由调用方报 ``RESULT_REF_UNAVAILABLE``。
        """
        if not strict or not user_id:
            return await self._read_record(ref, user_id=user_id)
        owned = await self._read_record(ref, user_id=user_id)
        if owned is not None:
            return owned
        located = await self._find_record_without_owner(ref)
        if located is None:
            return None
        stored_owner = str(located.get("owner_key") or "")
        if stored_owner and stored_owner != owner_key(user_id):
            raise ResultRefError(RESULT_REF_FORBIDDEN, details={"result_id": ref.id})
        return located

    async def _find_record_without_owner(self, ref: ResultRef) -> dict[str, Any] | None:
        """只按 ``result_id`` 定位记录（仅供权限区分；不返回给越权调用方）。"""
        owner = str(ref.owner_id or "").strip()
        if owner:
            record = await self._read_record(ref, user_id="")
            if isinstance(record, dict):
                return {**record, "owner_key": owner}
            return None
        finder = getattr(self._kv, "find_by_result_id", None)
        if not callable(finder):
            return None
        try:
            raw = await finder(ref.id)
        except Exception:  # noqa: BLE001 - 定位失败按"不存在"处理
            return None
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        stored_owner = ""
        stored_ref = record.get("ref")
        if isinstance(stored_ref, dict):
            stored_owner = str(stored_ref.get("owner_id") or "")
        return {**record, "owner_key": stored_owner}

    async def _read_record(self, ref: ResultRef, *, user_id: str = "") -> dict[str, Any] | None:
        """读取记录：小结果在 KV，大结果的正文在 Blob（先取元数据再取正文）。

        ``owner_id`` 为空（旧快照只留 ``{id, sha256}`` 的最小引用）时用调用方身份推回
        所有者键——权限校验已在 :meth:`_read_for_requester` 做过，这里只定位存储位置。
        """
        owner = str(ref.owner_id or "") or owner_key(user_id)
        raw = await self._kv.get(result_storage_key(owner, ref.id))
        record: dict[str, Any] | None = None
        if raw:
            try:
                record = json.loads(raw)
            except ValueError:
                record = None
        if not isinstance(record, dict):
            return None
        if record.get("body_in_blob"):
            backend = self._backend_for(ref.storage)
            payload = await backend.get(blob_object_key(owner, ref.id))
            if payload is None:
                return None
            try:
                record = {**record, "body": json.loads(payload.decode("utf-8"))}
            except (ValueError, UnicodeDecodeError):
                return None
        return {**record, "owner_key": owner}

    # ── 校验 ──────────────────────────────────────────────

    def _require_ref(self, value: Any) -> ResultRef:
        ref = ref_from_mapping(value)
        if ref is None or not ref.id:
            raise ResultRefError(
                RESULT_REF_UNAVAILABLE,
                "结果引用缺失或格式非法。",
                details={"ref": str(value)[:200]},
            )
        return ref

    def _check_expiry(self, ref: ResultRef, *, record: dict[str, Any] | None = None) -> None:
        """过期判定：引用自带 ``expires_at`` 优先；最小引用（旧快照只有 id+sha256）
        回退到记录里的到期时间——否则"过期"会被误判成"引用不可用"。
        """
        moment = float(self._clock())
        expires_at = float(ref.expires_at or 0.0)
        if not expires_at and isinstance(record, dict):
            try:
                expires_at = float(record.get("expires_at") or 0.0)
            except (TypeError, ValueError):
                expires_at = 0.0
        if expires_at and moment >= expires_at:
            raise ResultRefError(
                RESULT_REF_EXPIRED,
                details={"result_id": ref.id, "expires_at": expires_at},
            )

    def _check_owner(self, ref: ResultRef, *, user_id: str, strict: bool) -> None:
        if not strict or not user_id or not ref.owner_id:
            return
        if str(ref.owner_id) != str(user_id):
            raise ResultRefError(
                RESULT_REF_FORBIDDEN,
                details={"result_id": ref.id},
            )

    def _schema_compatible(self, name: str, version: int) -> bool:
        """存储时版本是否可被当前解析器读取。

        规则：同名且**不高于**当前登记版本 → 可解析（旧数据按旧结构读，允许缺字段）；
        更高于当前版本 → 不兼容，降级展示（绝不硬解析未知结构）。
        只对"结构会漂移"的结果类型生效；文本 / 任意 payload 永远可读。
        """
        from lumi_contracts.common.version import KNOWN_CONTRACTS

        text = str(name or "").strip()
        if not text or text in {"text", "application/json", "any"}:
            return True
        registered = {item.rsplit("@", 1)[0]: int(item.rsplit("@", 1)[1]) for item in KNOWN_CONTRACTS}
        key = text if text.startswith("lumi.") else f"lumi.{text}"
        known = registered.get(key)
        if known is None:
            # 未登记的 schema：按"可读"处理（历史结果可能来自已卸载插件）。
            return True
        return int(version or 1) <= int(known)

    def _sanitize(self, value: Any) -> tuple[Any, bool]:
        if self._sanitizer is not None:
            cleaned = self._sanitizer(value)
            return cleaned, cleaned is not value
        if isinstance(value, dict):
            # 落库走**存储侧脱敏**：只剥敏感键，不按 Prompt 预算裁剪结构
            # （裁剪由读取侧的 LoadBudget 负责，否则按引用取回的是残件）。
            from app.agents.orchestration.context import sanitize_stored_result

            cleaned = sanitize_stored_result(value)
            return cleaned, cleaned != value
        return value, False


# ── 配置读取与单例 ───────────────────────────────────────────


def tiering_from_settings() -> TieringPolicy:
    """分层策略来自 settings（代码里不写死阈值）。"""
    try:
        from app.core.config import settings

        return TieringPolicy(
            redis_max_bytes=int(getattr(settings, "RESULT_STORE_REDIS_MAX_BYTES", 65536)),
            local_max_bytes=int(getattr(settings, "RESULT_STORE_LOCAL_MAX_BYTES", 4194304)),
            blob_fallback_local=bool(
                getattr(settings, "RESULT_STORE_BLOB_FALLBACK_LOCAL", True)
            ),
        )
    except Exception:  # noqa: BLE001 - 配置不可用时用契约默认值
        return TieringPolicy()


def default_result_ttl_seconds() -> int:
    """结果引用保留期：显式配置优先，否则与既有的 ``AGENT_RESULT_REF_TTL_SECONDS`` 对齐。"""
    try:
        from app.core.config import settings

        explicit = int(getattr(settings, "RESULT_STORE_TTL_SECONDS", 0) or 0)
        if explicit > 0:
            return explicit
        return max(3600, int(settings.AGENT_RESULT_REF_TTL_SECONDS))
    except Exception:  # noqa: BLE001
        return 604800


def default_load_max_chars() -> int:
    try:
        from app.core.config import settings

        return max(500, int(getattr(settings, "RESULT_STORE_LOAD_MAX_CHARS", 24000)))
    except Exception:  # noqa: BLE001
        return 24000


def build_blob_backend() -> Any:
    """按 ``RESULT_STORE_BLOB_BACKEND`` 构造 Blob 后端（不可用时返回 ``None``）。"""
    try:
        from app.core.config import settings
    except Exception:  # noqa: BLE001
        return None
    backend = str(getattr(settings, "RESULT_STORE_BLOB_BACKEND", "local") or "local").strip().lower()
    if backend in {"s3", "minio", "oss"}:
        port = S3BlobPort(
            bucket=str(getattr(settings, "RESULT_STORE_BLOB_BUCKET", "") or ""),
            prefix=str(getattr(settings, "RESULT_STORE_BLOB_PREFIX", "") or ""),
            endpoint_url=str(getattr(settings, "RESULT_STORE_BLOB_ENDPOINT", "") or ""),
            region=str(getattr(settings, "RESULT_STORE_BLOB_REGION", "") or ""),
            access_key=str(getattr(settings, "RESULT_STORE_BLOB_ACCESS_KEY", "") or ""),
            secret_key=str(getattr(settings, "RESULT_STORE_BLOB_SECRET_KEY", "") or ""),
            use_ssl=bool(getattr(settings, "RESULT_STORE_BLOB_USE_SSL", True)),
        )
        if port.available():
            return port
        logger.warning(
            "[result-store] Blob 后端配置为 {} 但不可用（缺 boto3 或 bucket 未配置）",
            backend,
        )
        return None
    return None


_store: ResultStore | None = None
_store_lock = asyncio.Lock()


def get_result_store() -> ResultStore:
    """进程级单例（Redis/Blob 客户端只建一次）。"""
    global _store
    if _store is None:
        _store = ResultStore(blob=build_blob_backend())
    return _store


def set_result_store_for_tests(store: ResultStore | None) -> None:
    """显式测试替身；生产代码不调用。"""
    global _store
    _store = store


async def save_result(
    result: Any,
    *,
    user_id: str,
    job_id: str = "",
    step_id: str = "",
    tool_name: str = "",
    schema_name: str = "execution_result",
    schema_version: int = 1,
    content_type: str = "application/json",
    ttl_seconds: int = 0,
    artifact_refs: list[dict[str, Any]] | None = None,
    store: ResultStore | None = None,
) -> dict[str, str] | None:
    """模块级便捷入口：保存结果并返回**最小引用**（``{"id", "sha256"}``）。"""
    active = store or get_result_store()
    return await active.save_ref(
        SaveResultRequest(
            result=result,
            user_id=user_id,
            job_id=job_id,
            step_id=step_id,
            tool_name=tool_name,
            schema_name=schema_name,
            schema_version=schema_version,
            content_type=content_type,
            ttl_seconds=ttl_seconds,
            artifact_refs=list(artifact_refs or []),
        )
    )


async def resolve_result(
    user_id: str,
    result_ref: Any,
    *,
    budget: LoadBudget | None = None,
    store: ResultStore | None = None,
) -> dict[str, Any] | None:
    """模块级便捷入口：按引用解析正文（不可用返回 ``None``，不抛错）。"""
    active = store or get_result_store()
    return await active.load_body(result_ref, user_id=user_id, budget=budget)


async def resolve_result_storage(
    user_id: str,
    result_ref: Any,
    *,
    store: ResultStore | None = None,
) -> dict[str, Any] | None:
    """只取引用元数据（恢复/fork 前置判断用，不加载正文）。"""
    active = store or get_result_store()
    ref = await active.head(result_ref, user_id=user_id)
    return ref.as_dict() if ref is not None else None


async def resolve_result_for_owner(
    *,
    owner_key_value: str,
    result_id: str,
    max_chars: int = 0,
    page_size: int = 0,
    page_offset: int = 0,
    fields: tuple[str, ...] = (),
    summary_only: bool = False,
    store: ResultStore | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """按 ``(所有者键, result_id)`` 直查结果（``GET /jobs/{id}/results/{rid}`` 用）。

    与 :func:`resolve_result` 的区别：调用方已经知道结果属于哪个所有者（由**任务**归属
    推导出来，见 ``/jobs/{id}/results/{rid}``），因此这里不需要扫描键空间；但权限仍然
    只认记录里的 ``owner_key``，越权一律按"不存在"处理。

    返回 ``(payload, error_code)``；``error_code`` 为空表示成功。**过期是明确错误**：
    返回 ``RESULT_REF_EXPIRED``，绝不返回空成功。分页/预算参数由调用方传入、这里执行。
    """
    active = store or get_result_store()
    ref = ResultRef(id=str(result_id), owner_id=str(owner_key_value or ""))
    try:
        full = await active.load_for_owner(ref, summary_only=summary_only)
        resolution = await active.load_for_owner(
            ref,
            max_chars=int(max_chars or 0),
            page_size=int(page_size or 0),
            page_offset=int(page_offset or 0),
            fields=tuple(fields or ()),
            summary_only=summary_only,
        )
    except ResultRefError as exc:
        return None, str(exc.code)
    except Exception as exc:  # noqa: BLE001 - 存储异常按不可用处理（明确错误码）
        logger.debug("[result-store] 直查失败 result={}: {}", str(result_id)[:12], str(exc)[:120])
        return None, RESULT_REF_UNAVAILABLE
    bounded = bool(max_chars or page_size or fields or summary_only)
    payload = dict(resolution.body)
    payload.update(
        {
            "result_id": resolution.ref.id,
            "sha256": resolution.ref.sha256,
            "content_type": resolution.ref.content_type,
            "size": int(resolution.ref.size or 0),
            "storage_kind": str(resolution.ref.storage_kind),
            "schema_name": resolution.ref.schema_name,
            "schema_version": int(resolution.ref.schema_version or 1),
            "expires_at": float(resolution.ref.expires_at or 0.0),
            # 降级事实显式回传：前端不必自己从 version 反推"要不要降级展示"。
            "degraded": bool(resolution.degraded),
            "degradation": str(resolution.degradation),
            "note": str(resolution.note or ""),
            "truncated": bool(resolution.degraded or bounded),
            # 分页元数据（方案 §1.4：分页与预算由后端执行，前端只透传）。
            "offset": int(page_offset or 0),
            "limit": int(page_size or 0),
            "total": _list_total(full.body),
        }
    )
    return payload, ""


def _list_total(body: Mapping[str, Any] | None) -> int:
    """分页总数：取正文里最长列表字段的长度（没有列表时为 0，不猜）。"""
    best = 0
    for value in dict(body or {}).values():
        if isinstance(value, list):
            best = max(best, len(value))
    return int(best)


__all__ = [
    "RESULT_KEY_PREFIX",
    "RESULT_REF_SCHEMA_MISMATCH",
    "RESULT_STORE_DIRNAME",
    "LocalBlobPort",
    "RedisKvPort",
    "RefResolution",
    "ResultStore",
    "S3BlobPort",
    "SaveReceipt",
    "SaveResultRequest",
    "blob_object_key",
    "build_blob_backend",
    "default_blob_root",
    "default_load_max_chars",
    "default_result_ttl_seconds",
    "get_result_store",
    "local_blob_path",
    "owner_key",
    "resolve_result",
    "resolve_result_for_owner",
    "resolve_result_storage",
    "result_storage_key",
    "save_result",
    "set_result_store_for_tests",
    "tiering_from_settings",
]
