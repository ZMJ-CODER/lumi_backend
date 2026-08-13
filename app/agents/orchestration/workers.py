"""执行层 WorkerAgent —— 领取 DAG 节点任务，调用技能执行并返回结构化结果.

一个 WorkerAgent = 一个专业角色 + 一组可调用的技能（复用技能插件体系）。
新增执行 agent 时继承 WorkerAgent 并注册到 WORKERS。
"""

import hashlib
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from loguru import logger

from app.core.config import settings
from app.agents.orchestration.models import TaskNode
from app.agents.skills.base import SkillContext
from app.agents.skills.registry import SkillRegistry


def _file_key(rel_path: str) -> str:
    """与客户端 fileKey 相同的算法：sha256(相对路径) 前 32 位 hex."""
    return hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:32]


@dataclass
class WorkerContext:
    """Worker 执行上下文（编排器注入）."""

    user_id: str
    job_id: str
    scene: str = "office"
    # BYOK：agent 任务提交时临时携带的 API key（内存持有，任务结束即释放，不落库）
    llm_api_key: str | None = None


class WorkerAgent(ABC):
    """执行层 agent 基类."""

    name: str = ""
    description: str = ""
    skills: list[str] = []  # 该 agent 可调用的技能名（白名单）

    @abstractmethod
    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        """执行一个任务节点，返回结构化结果."""
        ...

    async def run_skill(self, skill_name: str, params: dict, ctx: WorkerContext) -> dict:
        """调用技能并统一包装结果（带审计，复用技能体系）."""
        skill = SkillRegistry.get(skill_name)
        if skill is None:
            return {"success": False, "error": f"技能不存在: {skill_name}", "error_code": "SKILL_NOT_FOUND"}
        if self.skills and skill_name not in self.skills:
            return {"success": False, "error": f"agent '{self.name}' 无权调用技能 {skill_name}", "error_code": "FORBIDDEN"}
        result = await skill.execute(
            params,
            SkillContext(user_id=ctx.user_id, scene=ctx.scene, conversation_id=ctx.job_id),
        )
        if not result.success:
            return {"success": False, "error": result.error, "error_code": result.error_code}
        return {"success": True, "content": result.output, **result.metadata}

    def __repr__(self) -> str:
        return f"<WorkerAgent: {self.name} skills={self.skills}>"


# ── 公共工具：code 系列 agent 复用 ──────────────────────────────

CODE_SYSTEM_PROMPT = (
    "你是一名资深软件工程师，正在用户的本地代码项目里工作。"
    "根据用户指令和提供的文件内容，生成修改后的完整文件内容。"
    "只输出文件内容本身，不要解释、不要 Markdown 代码块围栏。"
)

TEST_SYSTEM_PROMPT = (
    "你是一名测试工程师。根据被测代码和用户指令，生成完整的 pytest 测试文件。"
    "只输出测试文件内容本身，不要解释、不要 Markdown 代码块围栏。"
)

REVIEW_SYSTEM_PROMPT = (
    "你是资深代码审查员。审查代码是否满足用户指令、是否存在明显 bug 或安全隐患。"
    "只输出 JSON：{\"approved\": true 或 false, \"issues\": [\"问题1\", \"问题2\"], \"feedback\": \"一句话总结\"}"
)


async def locate_project_file(
    user_id: str,
    project_id: str,
    instruction: str,
    target_file: str | None = None,
    target_key: str | None = None,
) -> dict:
    """定位相关文件：显式目标须经项目索引校验 → 语义检索（代码向量）→ 关键词（结构索引）兜底."""
    from app.core.database import async_session_factory
    from app.services import code_embedding
    from app.services import project_index

    try:
        async with async_session_factory() as session:
            # 0) file_key（语义检索命中）直接可信
            if target_key:
                return {"file_key": str(target_key)}
            # 0.5) 显式 target_file 必须先确认它确实是索引里的文件。
            #      LLM 可能给出目录路径/乱猜路径，盲目信任会报"目标不是文件"。
            if target_file:
                exact = await project_index.find_file_by_path(
                    session, user_id, project_id, str(target_file)
                )
                if exact:
                    return {"path": exact["file_path"]}
            # 1) 语义检索：命中返回 file_key（客户端按本地映射读真实文件）
            try:
                vec_hits = await code_embedding.search_code_vectors(
                    session, user_id, project_id, instruction, top_k=3
                )
                if vec_hits and vec_hits[0]["similarity"] >= 0.35:
                    h = vec_hits[0]
                    file_key = h["file_key"]
                    # 语义命中返回 file_key；同时反查真实相对路径并一起返回，
                    # 让客户端优先按 path 读取（不依赖本地 fileMap 是否完整）
                    resolved_path = None
                    if file_key:
                        for fp in await project_index.list_project_files(
                            session, user_id, project_id, limit=500
                        ):
                            if _file_key(fp) == file_key:
                                resolved_path = fp
                                break
                    if resolved_path:
                        return {
                            "path": resolved_path,
                            "file_key": file_key,
                            "function_name": h.get("function_name") or "",
                            "line_start": h.get("line_start"),
                            "line_end": h.get("line_end"),
                        }
                    return {
                        "file_key": file_key,
                        "function_name": h.get("function_name") or "",
                        "line_start": h.get("line_start"),
                        "line_end": h.get("line_end"),
                    }
            except Exception:  # noqa: BLE001
                pass  # 向量检索失败（模型未就绪等）→ 关键词兜底

            # 2) 显式文件名（order_service.py → order_service）
            for name in re.findall(r"[\w\-]+\.\w+", instruction):
                base_name = name.rsplit(".", 1)[0]
                hits = await project_index.search_project(
                    session, user_id, project_id, base_name, limit=5
                )
                if hits:
                    return {"path": hits[0]["file_path"]}
            # 3) 英文/代码标识符（calculate_total 等）
            for token in re.findall(r"[A-Za-z_]\w{2,}", instruction)[:6]:
                hits = await project_index.search_project(
                    session, user_id, project_id, token, limit=5
                )
                if hits:
                    return {"path": hits[0]["file_path"]}
            # 4) 中文关键词兜底（去掉语气词）
            query = re.sub(
                r"[帮我在项目里请把请帮我实现修改创建添加删除优化重构修复检查看看读取一下写一个设计新增]",
                "",
                instruction,
            )
            query = " ".join(query.split())[:100] or instruction[:100]
            hits = await project_index.search_project(
                session, user_id, project_id, query, limit=5
            )
            if hits:
                return {"path": hits[0]["file_path"]}
            # 5) 项目文件清单兜底：把指令与索引里的文件路径做智能匹配，
            #    保证 code agent 定位到的永远是索引里真实存在的文件。
            all_files = await project_index.list_project_files(
                session, user_id, project_id, limit=500
            )
            for fp in all_files:
                fp_l = fp.lower()
                for name in re.findall(r"[\w\-]+\.\w+", instruction):
                    if fp_l == name.lower() or fp_l.endswith("/" + name.lower()):
                        return {"path": fp}
                for token in re.findall(r"[A-Za-z_][\w\-]*", instruction)[:8]:
                    if fp_l == token.lower() or fp_l.endswith("/" + token.lower()):
                        return {"path": fp}
            # 中文：文件名主干出现在指令里（最长路径优先）
            for fp in sorted(all_files, key=len, reverse=True):
                base = fp.rsplit("/", 1)[-1]
                stem = base.rsplit(".", 1)[0] if "." in base else base
                if stem and len(stem) >= 2 and stem in instruction:
                    return {"path": fp}
            # 6) 找不到明确目标但项目有文件：优先主入口文件（"编写页面"类任务落到入口）
            if all_files:
                entry_hits = [
                    fp
                    for fp in all_files
                    if fp.lower()
                    in (
                        "index.html",
                        "src/app.vue",
                        "src/app.jsx",
                        "src/app.tsx",
                        "src/app.js",
                        "src/app.ts",
                        "src/main.js",
                        "src/main.ts",
                        "src/main.py",
                        "main.py",
                        "app.py",
                    )
                ]
                if entry_hits:
                    return {"path": entry_hits[0]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Agent] 定位文件失败: {}", exc)
    return {}


async def list_project_files(user_id: str, project_id: str, limit: int = 500) -> list[str]:
    """服务端项目索引文件清单（不依赖客户端；供 code agent 参考与匹配）."""
    from app.core.database import async_session_factory
    from app.services import project_index

    try:
        async with async_session_factory() as session:
            return await project_index.list_project_files(
                session, user_id, project_id, limit=limit
            )
    except Exception:
        return []


async def _read_with_retry(
    worker: "WorkerAgent",
    ctx: WorkerContext,
    project_id: str,
    instruction: str,
    target_path=None,
    target_key=None,
):
    """读取项目文件；首次失败（可能是目录/路径错误）时重新按索引定位换文件重试一次.

    Returns:
        (read_result, located)：located 为最终使用的定位（重试后可能变化）
    """

    def _params(path, key):
        p = {"project_id": project_id}
        if path:
            p["path"] = path
        elif key:
            p["file_key"] = key
        return p

    read = await worker.run_skill("read_project_file", _params(target_path, target_key), ctx)
    if not read.get("success"):
        located = await locate_project_file(ctx.user_id, project_id, instruction)
        if located and (located.get("path") or located.get("file_key")):
            retry = await worker.run_skill(
                "read_project_file",
                _params(located.get("path"), located.get("file_key")),
                ctx,
            )
            if retry.get("success"):
                return retry, located
    return read, {"path": target_path, "file_key": target_key}


async def generate_code_content(
    ctx: WorkerContext,
    instruction: str,
    path: str,
    original: str,
    system_prompt: str = CODE_SYSTEM_PROMPT,
    project_files: list[str] | None = None,
) -> str:
    """LLM 生成完整文件内容（云端优先，本地小模型兜底）；自动去掉代码块围栏."""
    try:
        from app.core.llm import LLMClient
        from app.services.usage import CATEGORY_CODE

        llm = LLMClient()
        content = original[-60000:] if len(original) > 60000 else original
        files_hint = ""
        if project_files:
            files_hint = (
                "\n\n项目文件清单（供参考，写入路径必须是清单里的文件，避免目录/乱猜路径）：\n"
                + "\n".join(f"- {p}" for p in project_files[:100])
            )
        reply = await llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"用户指令：{instruction}\n\n"
                        f"文件路径：{path}\n\n当前文件内容：\n{content}"
                        f"{files_hint}"
                    ),
                },
            ],
            max_tokens=8000,
            usage_user_id=ctx.user_id,
            usage_category=CATEGORY_CODE,
            api_key=ctx.llm_api_key,
        )
        reply = (reply or "").strip()
        if reply.startswith("```"):
            reply = re.sub(r"^```\w*\n?", "", reply)
            reply = re.sub(r"\n?```$", "", reply)
        return reply
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Agent] LLM 生成失败: {}", exc)
        return await _generate_local(instruction, path, original)


async def _generate_local(
    instruction: str, path: str, original: str
) -> str:
    """本地小模型兜底生成（qwen2.5:3b 等；质量低于云端，作为降级方案）."""
    if (
        not settings.RAG_QUERY_REWRITE_PROVIDER == "local"
        or not settings.RAG_QUERY_REWRITE_MODEL.strip()
    ):
        return ""
    try:
        import httpx

        base = settings.RAG_QUERY_REWRITE_BASE_URL.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        content = original[-40000:] if len(original) > 40000 else original
        prompt = (
            "你是软件工程师，在用户的本地代码项目里修改文件。"
            "根据指令和当前文件内容，输出修改后的完整文件内容。"
            "只输出文件内容本身，不要解释、不要 Markdown 代码块围栏。\n\n"
            f"用户指令：{instruction}\n文件路径：{path}\n当前文件内容：\n{content}"
        )
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{base}/api/chat",
                json={
                    "model": settings.RAG_QUERY_REWRITE_MODEL.strip(),
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.2, "num_predict": 2048},
                },
            )
            resp.raise_for_status()
            reply = ((resp.json().get("message") or {}).get("content") or "").strip()
        if reply.startswith("```"):
            reply = re.sub(r"^```\w*\n?", "", reply)
            reply = re.sub(r"\n?```$", "", reply)
        if reply:
            logger.info("[Agent] 已回退本地模型生成代码（云端不可用）")
        return reply
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Agent] 本地模型生成失败: {}", exc)
        return ""


async def review_code_content(
    ctx: WorkerContext, instruction: str, path: str, content: str
) -> dict:
    """LLM 审查代码，返回 {approved, issues, feedback}; 审查失败时放行."""
    try:
        from app.core.llm import LLMClient
        from app.services.usage import CATEGORY_REVIEW

        llm = LLMClient()
        reply = await llm.chat(
            [
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"用户指令：{instruction}\n文件路径：{path}\n文件内容：\n{content[:12000]}"
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=1024,
            usage_user_id=ctx.user_id,
            usage_category=CATEGORY_REVIEW,
            api_key=ctx.llm_api_key,
        )
        data = _extract_json(reply)
        if data:
            issues = data.get("issues") or []
            if not isinstance(issues, list):
                issues = []
            return {
                "approved": bool(data.get("approved")),
                "issues": [str(i) for i in issues[:20]],
                "feedback": str(data.get("feedback") or ""),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Agent] 代码审查失败，默认放行: {}", exc)
    return {"approved": True, "issues": [], "feedback": ""}


def _extract_json(text: str | None) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


class RetrievalAgent(WorkerAgent):
    """检索 agent：复用现成 RAG，检索知识库返回文档片段与引用."""

    name = "retrieval"
    description = "检索用户知识库，获取与问题相关的文档片段和引用"
    skills = ["query_knowledge"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        query = str(node.params.get("query") or node.params.get("request") or "").strip()
        if not query:
            return {"success": False, "error": "检索任务缺少 query 参数", "error_code": "INVALID_ARGS"}
        top_k = int(node.params.get("top_k") or 5)
        logger.debug("[Agent:retrieval] 检索 query={} top_k={}", query[:60], top_k)
        return await self.run_skill(
            "query_knowledge", {"query": query, "top_k": top_k}, ctx
        )


class CodeAgent(WorkerAgent):
    """代码 agent：方案 A —— 在用户本地项目里定位 → 读取 → LLM 生成 → 写回.

    文件读写走 client 技能（Electron 本地执行，路径 jail 到项目根）；
    定位用服务器端结构索引（不读代码正文）。
    """

    name = "code"
    description = "根据指令读写本地代码项目，生成/修改代码并运行测试"
    skills = ["list_project", "read_project_file", "write_project_file", "run_project_command"]

    _SYSTEM_PROMPT = (
        "你是一名资深软件工程师，正在用户的本地代码项目里工作。"
        "根据用户指令和提供的文件内容，生成修改后的完整文件内容。"
        "只输出文件内容本身，不要解释、不要 Markdown 代码块围栏。"
    )

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        instruction = str(node.params.get("instruction") or "").strip()
        if not project_id or not instruction:
            return {
                "success": False,
                "error": "缺少 project_id 或 instruction",
                "error_code": "INVALID_ARGS",
            }

        # 1. 定位相关代码：显式指定（真实路径/file_key）或 语义检索 + 关键词兜底
        target_path = node.params.get("target_file")
        target_key = node.params.get("file_key")
        located = None
        if not target_path and not target_key:
            located = await self._locate(project_id, instruction, ctx)
            target_key = located.get("file_key")
            target_path = located.get("path")
        if not target_path and not target_key:
            return {
                "success": False,
                "error": "未能在项目索引中定位相关文件，请明确要修改的文件",
                "error_code": "EXEC_ERROR",
            }

        # 2. 读取（client 技能，本地执行；语义命中用 file_key，客户端映射真实路径）
        read, located = await _read_with_retry(
            self, ctx, project_id, instruction, target_path, target_key
        )
        if not read.get("success"):
            return read
        if located:
            target_path = located.get("path") or target_path
            target_key = located.get("file_key") or target_key
        original = str(read.get("content") or "")

        # 3. LLM 生成修改后的完整内容（附带项目文件清单，避免乱猜路径）
        project_files = await list_project_files(ctx.user_id, project_id)
        new_content = await self._generate(
            ctx, instruction, target_path or target_key or "", original, project_files
        )
        if not new_content:
            return {
                "success": False,
                "error": "模型未能生成修改内容",
                "error_code": "EXEC_ERROR",
            }

        # 4. 写入（client 技能，确认弹窗）；结果附带可审查内容供质检层使用
        write_params = {"project_id": project_id}
        if target_path:
            write_params["path"] = str(target_path)
        else:
            write_params["file_key"] = target_key
        write_result = await self.run_skill(
            "write_project_file", {**write_params, "content": new_content}, ctx
        )
        if write_result.get("success"):
            write_result["new_content"] = new_content
            write_result["instruction"] = instruction
            write_result["path"] = target_path or target_key
        return write_result

    async def _locate(self, project_id: str, instruction: str, ctx: WorkerContext) -> dict:
        """定位相关代码：语义检索（代码向量）优先，关键词（结构索引）兜底."""
        return await locate_project_file(ctx.user_id, project_id, instruction)

    async def _generate(
        self,
        ctx: WorkerContext,
        instruction: str,
        path: str,
        original: str,
        project_files: list[str] | None = None,
    ) -> str:
        return await generate_code_content(
            ctx,
            instruction,
            path,
            original,
            self._SYSTEM_PROMPT,
            project_files=project_files,
        )


class CodeReaderAgent(WorkerAgent):
    """代码定位/阅读 agent：定位相关文件并读取内容，输出代码上下文供下游使用."""

    name = "code_reader"
    description = "在本地代码项目中定位并读取相关文件内容，梳理代码上下文"
    skills = ["list_project", "read_project_file"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        instruction = str(node.params.get("instruction") or "").strip()
        if not project_id:
            return {
                "success": False,
                "error": "缺少 project_id",
                "error_code": "INVALID_ARGS",
            }
        located = await locate_project_file(
            ctx.user_id,
            project_id,
            instruction,
            node.params.get("target_file"),
            node.params.get("file_key"),
        )
        if not located or not (located.get("path") or located.get("file_key")):
            return {
                "success": False,
                "error": "未能在项目索引中定位相关文件",
                "error_code": "EXEC_ERROR",
            }
        read, located = await _read_with_retry(
            self,
            ctx,
            project_id,
            instruction,
            located.get("path"),
            located.get("file_key"),
        )
        if not read.get("success"):
            return read
        return {
            "success": True,
            "located": located,
            "path": located.get("path") or located.get("file_key"),
            "content": read.get("content") or "",
        }


class CodeWriterAgent(WorkerAgent):
    """编码 agent：基于指令与代码上下文生成/修改本地文件并写回项目."""

    name = "code_writer"
    description = "根据指令生成或修改本地代码文件内容并写回项目"
    skills = ["read_project_file", "write_project_file"]

    _SYSTEM_PROMPT = CODE_SYSTEM_PROMPT

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        instruction = str(node.params.get("instruction") or "").strip()
        if not project_id or not instruction:
            return {
                "success": False,
                "error": "缺少 project_id 或 instruction",
                "error_code": "INVALID_ARGS",
            }

        target_path = node.params.get("target_file")
        target_key = node.params.get("file_key")
        original = node.params.get("original_content")
        path_label = str(target_path or target_key or "")

        # 上游（如 code_reader）未传内容时，自行定位并读取
        if original is None:
            located = await locate_project_file(
                ctx.user_id, project_id, instruction, target_path, target_key
            )
            if not located or not (located.get("path") or located.get("file_key")):
                return {
                    "success": False,
                    "error": "未能在项目索引中定位相关文件，请明确要修改的文件",
                    "error_code": "EXEC_ERROR",
                }
            target_path = located.get("path")
            target_key = located.get("file_key")
            path_label = str(target_path or target_key)
            read, located = await _read_with_retry(
                self, ctx, project_id, instruction, target_path, target_key
            )
            if not read.get("success"):
                return read
            if located:
                target_path = located.get("path") or target_path
                target_key = located.get("file_key") or target_key
                path_label = str(target_path or target_key)
            original = read.get("content") or ""

        new_content = await generate_code_content(
            ctx,
            instruction,
            path_label,
            original or "",
            self._SYSTEM_PROMPT,
            project_files=await list_project_files(ctx.user_id, project_id),
        )
        if not new_content:
            return {
                "success": False,
                "error": "模型未能生成修改内容",
                "error_code": "EXEC_ERROR",
            }

        write_params = {"project_id": project_id}
        if target_path:
            write_params["path"] = target_path
        else:
            write_params["file_key"] = target_key
        write_result = await self.run_skill(
            "write_project_file", {**write_params, "content": new_content}, ctx
        )
        if write_result.get("success"):
            write_result["new_content"] = new_content
            write_result["instruction"] = instruction
            write_result["path"] = path_label
        return write_result


class CodeTesterAgent(WorkerAgent):
    """测试 agent：按项目类型自动选择验证命令（构建/测试，能正常退出）并如实汇报结果.

    注意：dev 服务器不会退出，不适合自动验证；简单前端项目用 npm run build 校验即可。
    执行结果以 success=True + tests_passed 汇报，避免命令失败触发 DAG 重试导致重复确认弹窗。
    """

    name = "code_tester"
    description = "在本地项目自动选择并运行合适的测试/构建命令，如实汇报结果"
    skills = ["read_project_file", "write_project_file", "run_project_command"]

    _TEST_PROMPT = TEST_SYSTEM_PROMPT

    async def _pick_command(self, project_id: str, files: list[str], requested: str) -> str | None:
        """按项目类型选择最合适的验证命令."""
        if requested:
            return requested
        files_l = [f.lower() for f in files]
        has_pkg = "package.json" in files_l
        has_test_infra = any(
            f.endswith((".test.ts", ".test.js", ".test.tsx", ".test.jsx", "_test.py"))
            or "vitest" in f
            or "jest" in f
            or "spec." in f
            for f in files_l
        )
        if has_pkg:
            return "npm test" if has_test_infra else "npm run build"
        if any(f.endswith(".py") for f in files_l):
            return "pytest -q"
        if any(f.endswith(".go") for f in files_l):
            return "go test ./..."
        return None

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        if not project_id:
            return {
                "success": False,
                "error": "缺少 project_id",
                "error_code": "INVALID_ARGS",
            }

        files = await list_project_files(ctx.user_id, project_id)
        requested = str(node.params.get("command") or "").strip()
        command = await self._pick_command(project_id, files, requested)
        if not command:
            return {
                "success": True,
                "tests_passed": False,
                "command": None,
                "error": "无法识别项目类型，请人工指定验证命令（如 npm run build / pytest -q）",
                "project_files": files[:20],
            }

        # 执行一次（信任项目免确认；未信任则由用户确认）
        run = await self.run_skill(
            "run_project_command", {"project_id": project_id, "command": command}, ctx
        )
        ok = bool(run.get("success"))
        output = str(run.get("content") or "")
        error = str(run.get("error") or "")
        # 如实汇报执行结果（success=True 表示测试已执行；tests_passed 表示是否通过），
        # 避免 DAG 对命令失败做重试（重试会再次发起确认，造成重复弹窗）。
        return {
            "success": True,
            "tests_passed": ok,
            "command": command,
            "output": (output or error or "（无输出）")[:4000],
            "error": None if ok else (error[:500] or "命令执行失败"),
            "error_code": None if ok else (run.get("error_code") or "EXEC_ERROR"),
        }


class CodeReviewerAgent(WorkerAgent):
    """代码审查 agent：审查已有代码或改动，输出结构化问题清单（供质检/人工参考）."""

    name = "code_reviewer"
    description = "审查本地项目代码或改动，输出是否通过、问题清单与反馈"
    skills = ["read_project_file"]

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        instruction = str(node.params.get("instruction") or "").strip()
        if not project_id:
            return {
                "success": False,
                "error": "缺少 project_id",
                "error_code": "INVALID_ARGS",
            }

        content = node.params.get("content")
        path_label = str(
            node.params.get("target_file")
            or node.params.get("file_key")
            or node.params.get("path")
            or ""
        )
        if content is None:
            located = await locate_project_file(
                ctx.user_id,
                project_id,
                instruction,
                node.params.get("target_file"),
                node.params.get("file_key"),
            )
            if not located or not (located.get("path") or located.get("file_key")):
                return {
                    "success": False,
                    "error": "未能在项目索引中定位相关文件",
                    "error_code": "EXEC_ERROR",
                }
            path_label = str(located.get("path") or located.get("file_key"))
            read_params = {"project_id": project_id}
            if located.get("file_key"):
                read_params["file_key"] = located["file_key"]
            else:
                read_params["path"] = located["path"]
            read = await self.run_skill("read_project_file", read_params, ctx)
            if not read.get("success"):
                return read
            content = read.get("content") or ""

        review = await review_code_content(ctx, instruction, path_label, content or "")
        return {"success": True, "path": path_label, **review}


# 执行层 agent 注册表（按 name 路由）
WORKERS: dict[str, WorkerAgent] = {
    "retrieval": RetrievalAgent(),
    "code": CodeAgent(),
    "code_reader": CodeReaderAgent(),
    "code_writer": CodeWriterAgent(),
    "code_tester": CodeTesterAgent(),
    "code_reviewer": CodeReviewerAgent(),
}


def get_worker(name: str) -> WorkerAgent | None:
    return WORKERS.get(name)


def list_workers() -> list[WorkerAgent]:
    return list(WORKERS.values())
