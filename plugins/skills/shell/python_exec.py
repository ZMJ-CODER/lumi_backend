"""技能插件（shell/终端执行）：python_exec —— 在受限环境执行 Python 代码."""

from pathlib import Path

from app.agents.sandbox.registry import get_sandbox
from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.core.config import settings
from loguru import logger


def _generic_output_dir(user_id: str, conv_id: str) -> Path:
    """新建文件的通用输出目录（按用户 × 会话/任务隔离）."""
    safe_conv = "".join(ch for ch in str(conv_id or "default") if ch.isalnum() or ch in "-_")[:64] or "default"
    return Path(settings.UPLOAD_DIR) / "office_outputs" / str(user_id) / safe_conv


class PythonExecSkill(Skill):
    name = "python_exec"
    description = (
        "执行一段 Python 代码完成任务。适用于批量/重复任务：格式转换（如 xlsx→csv）、"
        "批量处理、数据导出、新建文件（docx/xlsx/csv/json 等）等，不需要逐步查看文件内容。"
        "可传 doc_ids 指定上传的办公文档：脚本通过环境变量 LUMI_DOC_PATHS（JSON：文件名→路径）读取文档，"
        "把产物写入 LUMI_DOC_OUTPUT_DIRS（JSON：文件名→输出目录）；"
        "新建文件统一写入 LUMI_OUTPUT_DIR 环境变量指向的目录。"
        "代码在受限环境运行（超时/输出截断），不能访问网络。"
    )
    category = "shell"
    environment = "sandbox"
    scenes = ["office"]
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
        doc_ids = list(params.get("doc_ids") or [])
        timeout = min(int(params.get("timeout") or 20), 60)
        env_extra: dict = {}
        doc_meta: dict[str, dict] = {}
        # 通用输出目录：无论是否有 doc_ids，都提供给脚本（新建文件必须）
        generic_dir = _generic_output_dir(
            str(context.user_id) if context and context.user_id else "guest",
            str(context.conversation_id) if context and context.conversation_id else "default",
        )
        generic_dir.mkdir(parents=True, exist_ok=True)
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
                    doc_paths[fname] = path
                    doc_out_dirs[fname] = str(out_dir)
                    doc_meta[str(doc_id)] = {
                        "filename": fname,
                        "path": path,
                        "output_dir": str(out_dir),
                    }
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
        sandbox = get_sandbox()
        result = await sandbox.run_script(
            code, language="python", timeout=timeout, env_extra=env_extra or None
        )
        # 收集产物（文档输出 + 通用新建文件）
        produced: list[dict] = []
        if doc_ids and context and context.user_id:
            from app.services import office_docs

            seen = set()
            for doc_id in doc_ids:
                for f in office_docs.list_doc_outputs(context.user_id, str(doc_id)):
                    key = f["name"]
                    if key not in seen:
                        seen.add(key)
                        produced.append({**f, "doc_id": str(doc_id)})
        for f in sorted(generic_dir.iterdir()):
            if f.is_file():
                produced.append({"name": f.name, "size": f.stat().st_size, "generic": True})
        # 自动投递：把通用产物送到用户端下载目录（默认下载目录/设置中配置的目录），
        # 客户端保存成功后删除后端副本；投递失败不影响脚本结果（气泡仍可手动下载）。
        if context and context.user_id and produced:
            from app.services import client_tools

            for p in produced:
                if not p.get("generic"):
                    continue
                try:
                    await client_tools.create_client_tool_request(
                        context.user_id,
                        "save_generated_output",
                        {
                            "job_id": str(context.conversation_id or ""),
                            "name": p["name"],
                        },
                        requires_confirmation=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("产物自动投递入队失败 {}: {}", p.get("name"), exc)
        if result.status == "success":
            return SkillResult(
                success=True,
                output=result.stdout or "(无输出)",
                metadata={"outputs": produced, "doc_paths": doc_meta},
            )
        if result.status == "timeout":
            return SkillResult(
                success=False,
                error=f"代码执行超时（>{timeout}s）",
                error_code="TIMEOUT",
                retryable=False,
                metadata={"stderr": result.stderr, "outputs": produced},
            )
        return SkillResult(
            success=False,
            error=result.error or result.stderr or "代码执行失败",
            error_code="EXEC_ERROR",
            retryable=False,
            metadata={"stderr": result.stderr, "outputs": produced},
        )
