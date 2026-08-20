"""通用文件操作脚本执行器：只在受限沙箱内执行 Python/系统命令."""

import csv
import io
from pathlib import Path, PurePosixPath

from app.agents.sandbox.registry import get_sandbox
from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.core.config import settings
from loguru import logger


def _generic_output_dir(user_id: str, conv_id: str) -> Path:
    """新建文件的通用输出目录（按用户 × 会话/任务隔离）."""
    safe_conv = "".join(ch for ch in str(conv_id or "default") if ch.isalnum() or ch in "-_")[:64] or "default"
    return Path(settings.UPLOAD_DIR) / "office_outputs" / str(user_id) / safe_conv


def _container_path(*parts: str) -> str:
    """构造容器工作区下的路径；禁止把宿主路径传给脚本。"""
    return str(PurePosixPath("/workspace", *parts))


_TEXT_OUTPUT_EXTENSIONS = {".txt", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log"}
_OUTPUT_ENCODINGS = {"utf-8", "utf-8-sig", "gb18030"}


def _safe_output_name(value: object) -> str:
    """Return a basename only; contracts may never select an arbitrary path."""
    text = str(value or "").strip().replace("\\", "/")
    if not text or "/" in text:
        return ""
    return Path(text).name


def _normalise_output_contract(value: object) -> dict:
    """Accept only small, host-verifiable contract fields from the planner."""
    raw = value if isinstance(value, dict) else {}
    names: list[str] = []
    for item in raw.get("expected_output_names") or []:
        name = _safe_output_name(item)
        if name and name not in names:
            names.append(name)
    result = {"expected_output_names": names}
    extension = str(raw.get("target_extension") or "").casefold()
    if extension.startswith(".") and len(extension) <= 12 and extension[1:].isalnum():
        result["target_extension"] = extension
    delimiter = raw.get("text_delimiter")
    if delimiter in ("\t", ","):
        result["text_delimiter"] = delimiter
    encoding = str(raw.get("encoding") or "").casefold()
    if encoding in _OUTPUT_ENCODINGS:
        result["encoding"] = encoding
    return result


def _validate_output_contract(contract: dict, output_paths: dict[str, list[Path]]) -> str | None:
    """Verify an artifact rather than trusting the script stdout or model claim."""
    names = contract.get("expected_output_names") or []
    target_extension = contract.get("target_extension")
    encoding = contract.get("encoding")
    delimiter = contract.get("text_delimiter")
    for name in names:
        candidates = output_paths.get(name.casefold()) or []
        if not candidates:
            return f"未找到需要校验的产物：{name}"
        path = candidates[0]
        if target_extension and path.suffix.casefold() != target_extension:
            return f"产物 {name} 的扩展名不符合要求"
        if not (encoding or delimiter):
            continue
        if path.suffix.casefold() not in _TEXT_OUTPUT_EXTENSIONS:
            return f"产物 {name} 不是可校验的文本格式"
        try:
            raw = path.read_bytes()
            if encoding == "utf-8-sig" and not raw.startswith(b"\xef\xbb\xbf"):
                return f"产物 {name} 未使用 UTF-8 BOM 编码"
            decode_encoding = encoding or "utf-8"
            text = raw.decode(decode_encoding)
        except (OSError, UnicodeError):
            return f"产物 {name} 的编码不符合要求"
        if delimiter:
            # A delimiter contract is meaningful only when the rendered text
            # actually contains the requested separator. csv.reader also
            # catches malformed quoting rather than silently accepting it.
            if delimiter not in text:
                return f"产物 {name} 未使用要求的文本分隔符"
            try:
                list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
            except csv.Error:
                return f"产物 {name} 的分隔文本格式无效"
    return None


class PythonExecSkill(Skill):
    name = "python_exec"
    description = (
        "通用文件操作脚本执行器。在一次隔离运行中执行任意 Python 文件处理逻辑，"
        "脚本内部也可调用沙箱镜像已有的系统命令。优先用于格式转换、数据清洗、"
        "批量处理、合并拆分、数据导出和生成真实文件，避免把任务拆成逐行读取/写入。"
        "可传 doc_ids 指定上传的办公文档：脚本通过环境变量 LUMI_DOC_PATHS（JSON：文件名→路径）读取文档，"
        "把产物写入 LUMI_DOC_OUTPUT_DIRS（JSON：文件名→输出目录）；"
        "新建文件统一写入 LUMI_OUTPUT_DIR 环境变量指向的目录。"
        "只能访问本次授权文档和输出目录；容器禁网、只读根目录、非 root，并限制时间与资源。"
    )
    category = "shell"
    environment = "sandbox"
    scenes = ["office"]
    cost_estimate = 2.0
    success_rate = 0.9
    requires = ["authorized_documents", "docker_sandbox"]
    produces = ["reviewable_file_artifacts", "structured_stdout"]
    fallback_group = "office_file_processing"
    parameters_schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码"},
            "doc_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选的办公文档会话 id 列表：脚本通过 LUMI_DOC_PATHS / LUMI_DOC_OUTPUT_DIRS 环境变量访问",
            },
            "timeout": {"type": "integer", "description": "超时秒数（默认 20，最大 60）", "minimum": 1, "maximum": 60},
            "expected_output_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "本次执行必须实际生成的文件名；缺失时即使脚本退出码为 0 也视为失败",
            },
            "output_contract": {
                "type": "object",
                "description": "由规划器编译的可验证交付契约；文件名、扩展名、编码和文本分隔符将在宿主侧复核",
            },
        },
        "required": ["code"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        code = str(params.get("code") or "").strip()
        if not code:
            return SkillResult(
                success=False,
                error="缺少要执行的代码 code",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        sandbox = get_sandbox()
        # LocalSandbox 只是子进程，不具备文件系统隔离能力。默认拒绝，避免模型生成的
        # Python 从后端读取 .env、其他用户文档或数据库凭据；开发者须显式 opt-in。
        if sandbox.name == "local" and not settings.AGENT_ALLOW_UNSAFE_LOCAL_SANDBOX:
            return SkillResult(
                success=False,
                error="脚本执行需要隔离沙箱。当前服务器未启用容器/WASM 沙箱，已为保护数据拒绝执行。",
                error_code="SANDBOX_REQUIRED",
                retryable=False,
            )
        doc_ids = list(params.get("doc_ids") or [])
        output_contract = _normalise_output_contract(params.get("output_contract"))
        expected_output_names = {
            _safe_output_name(name).casefold()
            for name in (params.get("expected_output_names") or [])
            if _safe_output_name(name)
        }
        expected_output_names.update(
            name.casefold() for name in output_contract.get("expected_output_names") or []
        )
        timeout = min(int(params.get("timeout") or 20), 60)
        env_extra: dict[str, str] = {}
        mounts: list[dict[str, str]] = []
        # 通用输出目录：无论是否有 doc_ids，都提供给脚本（新建文件必须）
        generic_dir = _generic_output_dir(
            str(context.user_id) if context and context.user_id else "guest",
            str(context.conversation_id) if context and context.conversation_id else "default",
        )
        generic_dir.mkdir(parents=True, exist_ok=True)
        if sandbox.name == "docker":
            # 仅输出目录可写；其余目录和根文件系统都只读。
            env_extra["LUMI_OUTPUT_DIR"] = _container_path("output")
            mounts.append({"source": str(generic_dir), "target": _container_path("output"), "mode": "rw"})
        else:
            env_extra["LUMI_OUTPUT_DIR"] = str(generic_dir)
        if doc_ids and context and context.user_id:
            from app.services import office_docs

            doc_paths: dict[str, str] = {}
            doc_out_dirs: dict[str, str] = {}
            for doc_id in doc_ids:
                try:
                    await office_docs.ensure_session(context.user_id, str(doc_id))
                    meta = office_docs.load_session(context.user_id, str(doc_id))
                    path = office_docs.resolve_doc_path(context.user_id, str(doc_id))
                    out_dir = office_docs.doc_output_dir(context.user_id, str(doc_id))

                    # key 用"用户上传时的文件名"（脚本按文件名查找），而非磁盘上的 original.ext
                    fname = meta.get("filename") or f"doc_{str(doc_id)[:8]}"
                    if sandbox.name == "docker":
                        doc_key = "".join(ch for ch in str(doc_id) if ch.isalnum())[:40]
                        # 目标直接位于 /workspace 下，避免依赖 Docker 为嵌套
                        # bind mount 自动创建父目录的实现差异。
                        input_target = _container_path(f"input_{doc_key}")
                        output_target = _container_path(f"doc_output_{doc_key}")
                        doc_paths[fname] = input_target
                        doc_out_dirs[fname] = output_target
                        mounts.extend(
                            [
                                {"source": str(Path(path).resolve()), "target": input_target, "mode": "ro"},
                                {"source": str(Path(out_dir).resolve()), "target": output_target, "mode": "rw"},
                            ]
                        )
                    else:
                        doc_paths[fname] = path
                        doc_out_dirs[fname] = str(out_dir)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("脚本技能解析文档 {} 失败: {}", doc_id, exc)
            if doc_paths:
                import json as _json

                env_extra.update(
                    {
                        "LUMI_DOC_PATHS": _json.dumps(doc_paths, ensure_ascii=False),
                        "LUMI_DOC_OUTPUT_DIRS": _json.dumps(doc_out_dirs, ensure_ascii=False),
                    }
                )
        result = await sandbox.run_script(
            code,
            language="python",
            timeout=timeout,
            env_extra=env_extra or None,
            mounts=mounts or None,
        )
        # 收集产物（文档输出 + 通用新建文件）
        produced: list[dict] = []
        output_paths: dict[str, list[Path]] = {}
        if doc_ids and context and context.user_id:
            from app.services import office_docs

            seen = set()
            for doc_id in doc_ids:
                for f in office_docs.list_doc_outputs(context.user_id, str(doc_id)):
                    key = f["name"]
                    safe_name = _safe_output_name(key)
                    if safe_name:
                        output_paths.setdefault(safe_name.casefold(), []).append(
                            office_docs.doc_output_dir(context.user_id, str(doc_id)) / safe_name
                        )
                    if key not in seen:
                        seen.add(key)
                        produced.append({**f, "doc_id": str(doc_id)})
        for f in sorted(generic_dir.iterdir()):
            if f.is_file():
                produced.append({"name": f.name, "size": f.stat().st_size, "generic": True})
                output_paths.setdefault(f.name.casefold(), []).append(f)
        # 产物只保存在用户隔离的后端临时区，前端必须先预览、再由用户主动下载。
        # 禁止自动投递/自动写入用户电脑，避免生成内容未经审阅落盘。
        if result.status == "success":
            output_names = {
                str(item.get("name") or "").casefold()
                for item in produced
                if isinstance(item, dict)
            }
            missing = sorted(expected_output_names - output_names)
            if missing:
                # stdout 属于脚本自报，不能作为交付成功的依据。必须由宿主侧
                # 实际扫描到可审阅的隔离产物，才能把文件展示给前端。
                return SkillResult(
                    success=False,
                    error=f"脚本已结束，但未生成预期文件：{'、'.join(missing)}",
                    error_code="OUTPUT_MISSING",
                    retryable=False,
                    metadata={"outputs": produced, "stdout": result.stdout, "stderr": result.stderr},
                )
            contract_error = _validate_output_contract(output_contract, output_paths)
            if contract_error:
                return SkillResult(
                    success=False,
                    error=f"生成的文件未满足交付要求：{contract_error}",
                    error_code="OUTPUT_CONTRACT_VIOLATION",
                    retryable=False,
                    metadata={"outputs": produced, "stdout": result.stdout, "stderr": result.stderr},
                )
            return SkillResult(
                success=True,
                output=result.stdout or "(无输出)",
                metadata={"outputs": produced},
            )
        if result.status == "timeout":
            return SkillResult(
                success=False,
                error=f"代码执行超时（>{timeout}s）",
                error_code="TIMEOUT",
                retryable=False,
                metadata={"stderr": result.stderr, "outputs": produced},
            )
        error = result.error or result.stderr or "代码执行失败"
        # 退出码本身不能区分脚本逻辑失败和产物交付失败。后者必须保留专用
        # 语义，让 M0 在原文件契约内终止，而不是升级为会遍历附件的开放规划。
        error_code = (
            "SANDBOX_OUTPUT_TRANSFER_FAILED"
            if "沙箱产物回传失败" in error
            else "EXEC_ERROR"
        )
        return SkillResult(
            success=False,
            error=error,
            error_code=error_code,
            retryable=False,
            metadata={"stderr": result.stderr, "outputs": produced},
        )
