"""阶段 4：插件安装状态（启用/停用/升级/回滚/版本锁定）。

存储放在**服务端文件系统**（``<state_dir>/plugins/state.json``）而不是数据库：插件
状态是一种低频、小体积、需要"人工可读可修"的控制面数据，落 JSON 能在没有数据库连接
时也完成安装与回滚（也与既有 ``plugins/`` 目录约定一致）。

三条约束：

* **未验签不落库**：安装记录里保存验签结论（``signature.verified``），"自称官方"无效；
* **版本锁定**：同时保存 ``version`` 与 ``previous_version``，回滚只走已安装过的版本；
* **原子写**：先写临时文件再 ``replace``，避免半截 JSON 让插件全部不可用。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

from lumi_contracts.plugins import (
    PluginManifest,
    parse_plugin_kind,
)
from lumi_contracts.plugins.vocabulary import (
    ACTIVATABLE_PLUGIN_KINDS,
    DEVELOPER_ONLY_PLUGIN_KINDS,
)

#: 安装状态文件（相对 state_dir；直接放在该目录下，不额外建子目录）。
STATE_FILE_NAME = "state.json"


def default_state_dir() -> Path:
    """状态目录：仓库根的 ``.lumi_state``（可用环境变量覆盖，见 app.core.config）。"""
    try:
        from app.core.config import settings

        configured = str(getattr(settings, "PLUGIN_STATE_DIR", "") or "").strip()
        if configured:
            return Path(configured)
    except Exception:  # noqa: BLE001 - 配置不可用时用仓库默认值
        pass
    return Path(__file__).resolve().parents[3] / ".lumi_state"


class PluginStateStore:
    """插件的已安装记录（JSON 文件；读失败降级为空而不是崩掉）。"""

    def __init__(self, *, state_dir: Path | None = None) -> None:
        self._state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self._path = self._state_dir / STATE_FILE_NAME
        self._cache: dict[str, dict[str, Any]] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._cache is not None:
            return self._cache
        rows: dict[str, dict[str, Any]] = {}
        try:
            raw = self._path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            for item in payload.get("plugins") or []:
                if isinstance(item, dict) and item.get("plugin_id"):
                    rows[str(item["plugin_id"])] = dict(item)
        except FileNotFoundError:
            rows = {}
        except (OSError, ValueError) as exc:  # noqa: BLE001 - 坏状态文件不该让插件全挂
            logger.warning("[plugin] 读取插件状态失败（按空处理）: {}", str(exc)[:160])
            rows = {}
        self._cache = rows
        return rows

    def _flush(self, rows: dict[str, dict[str, Any]]) -> None:
        payload = {
            "schema_version": 1,
            "updated_at": time.time(),
            "plugins": sorted(rows.values(), key=lambda item: str(item.get("plugin_id"))),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except OSError as exc:  # noqa: BLE001 - 落盘失败时保持内存态并告警
            logger.warning("[plugin] 写入插件状态失败（仅内存生效）: {}", str(exc)[:160])
        self._cache = rows

    def all(self) -> list[dict[str, Any]]:
        return sorted(self._load().values(), key=lambda item: str(item.get("plugin_id")))

    def get(self, plugin_id: str) -> dict[str, Any] | None:
        row = self._load().get(str(plugin_id))
        return dict(row) if row is not None else None

    def put(self, record: dict[str, Any]) -> dict[str, Any]:
        plugin_id = str(record.get("plugin_id") or "")
        if not plugin_id:
            raise ValueError("安装记录缺少 plugin_id")
        rows = self._load()
        rows[plugin_id] = dict(record)
        self._flush(rows)
        return dict(rows[plugin_id])

    def drop(self, plugin_id: str) -> bool:
        rows = self._load()
        if str(plugin_id) not in rows:
            return False
        rows.pop(str(plugin_id), None)
        self._flush(rows)
        return True


def installation_record(
    manifest: PluginManifest,
    *,
    enabled: bool,
    verified: bool,
    now: float | None = None,
    previous_version: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一条安装记录（供状态文件与 API 共用同一形状）。"""
    stamp = time.time() if now is None else float(now)
    record: dict[str, Any] = {
        "plugin_id": manifest.id,
        "version": manifest.version,
        "kind": str(manifest.kind),
        "deployment": str(manifest.deployment),
        # 安装记录只记**声明**：实际执行位置/方式在租约与结果里（见 docs/EXECUTION_PLANE_CONTRACT.md）。
        "execution_plane": str(manifest.declared_plane()),
        "runtime_kind": str(manifest.declared_runtime()),
        "declared_execution_plane": str(manifest.declared_plane()),
        "declared_runtime_kind": str(manifest.declared_runtime()),
        "trust_level": str(manifest.trust_level),
        "data_locality": str(manifest.data_locality),
        "enabled": bool(enabled),
        "verified": bool(verified),
        "digest": manifest.digest(),
        "provides": manifest.provides.model_dump(mode="json"),
        "requires": manifest.requires.model_dump(mode="json"),
        # "数据是否离开本机"是安装界面必须显示的事实，直接由本地性推导。
        "data_leaves_device": str(manifest.data_locality) != "local_only",
        "needs_local_confirmation": bool(manifest.needs_approval),
        "needs_workspace_binding": any(
            str(item).startswith("workspace.") for item in manifest.provides.capabilities
        ),
        "previous_version": str(previous_version or ""),
        "installed_at": stamp,
        "updated_at": stamp,
    }
    record.update(dict(extra or {}))
    return record


def kind_requires_developer_mode(kind: Any) -> bool:
    """该插件类型是否只能在开发者模式激活（未知 kind 一律 True）。"""
    parsed = parse_plugin_kind(kind)
    if parsed is None:
        return True
    if parsed.value in ACTIVATABLE_PLUGIN_KINDS:
        return False
    return parsed.value in DEVELOPER_ONLY_PLUGIN_KINDS


__all__ = [
    "STATE_FILE_NAME",
    "PluginStateStore",
    "default_state_dir",
    "installation_record",
    "kind_requires_developer_mode",
]
