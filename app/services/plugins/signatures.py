"""阶段 4：插件签名校验（在既有准入模型上做，不发明新的信任根）。

现状（已核对仓库）：**没有任何插件签名验证设施**；既有插件准入是"命名空间白名单 +
semver"（``app/contracts/tools.py`` 的 ``validate_plugin_declaration`` /
``DEFAULT_TRUSTED_NAMESPACES``）。因此本模块的立场是：

* **不假装有 PKI**：没有配置公钥时，任何插件都是"未验签"（``verified=False``），
  而不是"默认信任"；
* **验签成功才允许 official 信任级别**：``trust_level=official/builtin`` 必须带
  有效签名，否则安装期降级为 ``third_party`` 并要求更强隔离；
* **支持两种真实可用的机制**（都由配置提供密钥，不引入新依赖）：
  ``hmac-sha256``（与 ``app/api/v1/uploads.py`` 的签名 URL 同一模式）与
  ``ed25519``（``cryptography`` 已在依赖里，可用时才启用）；
* 摘要覆盖 **Manifest 规范化摘要 + 制品摘要**，防止"Manifest 验过、代码被换掉"。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from loguru import logger

from lumi_contracts.plugins import PluginManifest, TrustLevel


@dataclass(frozen=True, slots=True)
class SignaturePolicy:
    """验签配置（密钥只从服务端配置来；插件自述的 signature 不参与信任判定）。"""

    algorithm: str = ""
    secret: str = ""
    public_key: str = ""
    key_id: str = ""
    #: 未配置密钥时是否把 official 降级为 third_party（生产应为 True）。
    require_signature_for_official: bool = True

    @property
    def configured(self) -> bool:
        algo = self.algorithm.strip().casefold()
        if algo == "hmac-sha256":
            return bool(self.secret)
        if algo == "ed25519":
            return bool(self.public_key)
        return False


@dataclass(frozen=True, slots=True)
class SignatureOutcome:
    """一次验签结论（可审计：算法、key_id、是否验过、为什么没验）。"""

    verified: bool
    algorithm: str = ""
    key_id: str = ""
    reason: str = ""

    def apply(self, manifest: PluginManifest) -> PluginManifest:
        """把结论写回 Manifest（``verified`` 只能由这里写入）。"""
        signature = manifest.signature.model_copy(
            update={
                "algorithm": self.algorithm or manifest.signature.algorithm,
                "key_id": self.key_id or manifest.signature.key_id,
                "verified": bool(self.verified),
            }
        )
        trust = manifest.trust_level
        if self.verified:
            # 验签通过才允许回到自述的信任级别。
            trust = manifest.trust_level
        elif trust in {TrustLevel.BUILTIN, TrustLevel.OFFICIAL}:
            # 自称官方但没验过 → 降级为第三方（更强隔离），绝不保留 official。
            trust = TrustLevel.THIRD_PARTY
        return manifest.model_copy(update={"signature": signature, "trust_level": trust})


def artifact_digest(files: dict[str, bytes] | None = None) -> str:
    """制品摘要：对文件名排序后逐个哈希（内容变了摘要就变）。"""
    digest = hashlib.sha256()
    for name in sorted((files or {}).keys()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((files or {})[name])
        digest.update(b"\0")
    return digest.hexdigest()


def signed_payload(manifest: PluginManifest, files: dict[str, bytes] | None = None) -> bytes:
    """签名覆盖的内容：Manifest 规范化摘要 + 制品摘要。"""
    return f"{manifest.digest()}:{artifact_digest(files)}".encode("utf-8")


def verify_signature(
    manifest: PluginManifest,
    *,
    policy: SignaturePolicy,
    files: dict[str, bytes] | None = None,
) -> SignatureOutcome:
    """校验插件签名；任何"没配密钥/算法不支持"都返回**未验签**而不是通过。"""
    declared = manifest.signature
    algorithm = (policy.algorithm or declared.algorithm or "").strip().casefold()
    key_id = (policy.key_id or declared.key_id or "").strip()
    if not policy.configured:
        return SignatureOutcome(
            verified=False,
            algorithm=algorithm,
            key_id=key_id,
            reason="服务端未配置插件验签密钥（视为未验签）",
        )
    if not declared.value:
        return SignatureOutcome(
            verified=False, algorithm=algorithm, key_id=key_id, reason="Manifest 未提供签名值"
        )
    payload = signed_payload(manifest, files)
    if algorithm == "hmac-sha256":
        expected = hmac.new(policy.secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(expected, declared.value.strip())
        return SignatureOutcome(
            verified=ok,
            algorithm=algorithm,
            key_id=key_id,
            reason="" if ok else "HMAC 签名不匹配",
        )
    if algorithm == "ed25519":
        try:
            import base64

            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(policy.public_key.encode("utf-8"))
            )
            key.verify(base64.b64decode(declared.value.encode("utf-8")), payload)
            return SignatureOutcome(verified=True, algorithm=algorithm, key_id=key_id)
        except Exception as exc:  # noqa: BLE001 - 验签失败一律"未验签"
            return SignatureOutcome(
                verified=False,
                algorithm=algorithm,
                key_id=key_id,
                reason=f"Ed25519 验签失败：{str(exc)[:120]}",
            )
    return SignatureOutcome(
        verified=False, algorithm=algorithm, key_id=key_id, reason=f"不支持的签名算法：{algorithm}"
    )


def policy_from_settings() -> SignaturePolicy:
    """从配置读取验签策略（缺省＝未配置＝未验签）。"""
    try:
        from app.core.config import settings
    except Exception:  # noqa: BLE001
        return SignaturePolicy()
    return SignaturePolicy(
        algorithm=str(getattr(settings, "PLUGIN_SIGNATURE_ALGORITHM", "") or ""),
        secret=str(getattr(settings, "PLUGIN_SIGNATURE_SECRET", "") or ""),
        public_key=str(getattr(settings, "PLUGIN_SIGNATURE_PUBLIC_KEY", "") or ""),
        key_id=str(getattr(settings, "PLUGIN_SIGNATURE_KEY_ID", "") or ""),
    )


def log_outcome(plugin_id: str, outcome: SignatureOutcome) -> None:
    """验签结论进日志（安装审计要能回答"为什么这个插件没被信任"）。"""
    if outcome.verified:
        logger.info("[plugin] {} 验签通过（算法={}）", plugin_id, outcome.algorithm)
    else:
        logger.warning(
            "[plugin] {} 未验签（算法={}）：{}", plugin_id, outcome.algorithm or "-",
            outcome.reason or "未知原因",
        )


__all__ = [
    "SignatureOutcome",
    "SignaturePolicy",
    "artifact_digest",
    "log_outcome",
    "policy_from_settings",
    "signed_payload",
    "verify_signature",
]
