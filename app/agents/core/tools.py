"""Agent 公共工具 —— 文件定位 / LLM 生成 / 审查 / 步骤标题 等.

供 roles/ 下各领域 agent 复用；与执行编排解耦。
"""

import asyncio
import hashlib
import json
import re

from loguru import logger

from app.core.config import settings


def _file_key(rel_path: str) -> str:
    """与客户端 fileKey 相同的算法：sha256(相对路径) 前 32 位 hex."""
    return hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:32]


_CONFIG_FILE_HINTS = (
    "tsconfig",
    "package.json",
    "vite.config",
    "webpack.config",
    "eslint",
    "prettier",
    ".gitignore",
    "readme",
    "license",
    "dockerfile",
)
_CONFIG_FILE_SUFFIXES = (".json", ".lock", ".md", ".yaml", ".yml", ".toml", ".ini", ".cfg")
_CONFIG_INTENT_KEYWORDS = (
    "配置",
    "依赖",
    "tsconfig",
    "package",
    "vite",
    "构建",
    "脚手架",
    "依赖包",
)


def _is_config_file(rel_path: str) -> bool:
    """判断是否为配置/元数据类文件（写 UI/功能代码时应避开这些目标）."""
    base = str(rel_path or "").lower()
    if any(h in base for h in _CONFIG_FILE_HINTS):
        return True
    return base.endswith(_CONFIG_FILE_SUFFIXES)


def _is_config_intent(instruction: str) -> bool:
    """指令是否明确针对配置/依赖（此时允许定位到配置文件）."""
    text = str(instruction or "").lower()
    return any(k in text for k in _CONFIG_INTENT_KEYWORDS)


# ── 提示词模板 ───────────────────────────────────────────

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

_STEP_TITLE_PROMPT = (
    "根据用户的编码指令和文件路径，用一句话概括这次代码改动做了什么（20 字以内）。"
    "不要引号、不要 Markdown、不要多余解释，只输出标题本身。"
)


# ── 步骤标题 ────────────────────────────────────────────

async def _llm_step_title(ctx, instruction: str, path: str) -> str | None:
    """为代码编写节点生成一句话标题（LLM 概括；失败返回 None 走兜底）."""
    try:
        from app.core.llm import LLMClient
        from app.services.usage import CATEGORY_TITLE

        llm = LLMClient()
        reply = await asyncio.wait_for(
            llm.chat(
                [
                    {
                        "role": "user",
                        "content": (
                            f"{_STEP_TITLE_PROMPT}\n用户指令：{(instruction or '')[:200]}\n文件：{path}"
                        ),
                    }
                ],
                temperature=0.2,
                max_tokens=60,
                usage_user_id=ctx.user_id,
                usage_category=CATEGORY_TITLE,
                api_key=ctx.llm_api_key,
            ),
            timeout=10,
        )
        title = (reply or "").strip().strip('"').strip("'")
        return title[:50] or None
    except Exception:  # noqa: BLE001
        return None


async def _step_title(ctx, node, result: dict) -> str | None:
    """为已完成节点生成一句话标题（code 类用 LLM 概括，其余直接拼接）."""
    try:
        agent = node.agent
        path = str(
            result.get("path")
            or node.params.get("target_file")
            or node.params.get("file_key")
            or ""
        )
        if agent in ("code_writer", "code"):
            instruction = str(
                result.get("instruction") or node.params.get("instruction") or ""
            )
            title = await _llm_step_title(ctx, instruction, path)
            return title or (f"编写 {path}" if path else "编写代码")
        if agent == "code_reader":
            return f"阅读 {path}" if path else "阅读代码"
        if agent == "code_tester":
            cmd = str(result.get("command") or node.params.get("command") or "")
            passed = result.get("tests_passed")
            state = "通过" if passed is True else ("未通过" if passed is False else "")
            return f"测试 {cmd}{'：' + state if state else ''}".strip()
        if agent == "retrieval":
            return "检索知识库"
        if agent == "code_reviewer":
            return "代码审查"
    except Exception:  # noqa: BLE001
        pass
    return None


# ── 项目文件定位 / 读取 ─────────────────────────────────

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
                    # 写 UI/功能代码时优先避开配置文件（tsconfig.json/package.json 等），
                    # 避免"改了个配置就完事"；指令明确涉及配置时仍允许命中配置文件
                    all_files = await project_index.list_project_files(
                        session, user_id, project_id, limit=500
                    )
                    config_intent = _is_config_intent(instruction)
                    picked = None
                    for h in vec_hits:
                        if h["similarity"] < 0.35:
                            continue
                        resolved = None
                        if h.get("file_key"):
                            resolved = next(
                                (fp for fp in all_files if _file_key(fp) == h["file_key"]),
                                None,
                            )
                        check_path = resolved or h.get("file_path") or ""
                        if config_intent or not _is_config_file(check_path):
                            picked = h
                            break
                    if picked is None:
                        picked = vec_hits[0]  # 全部命中都是配置文件时退回最相似
                    h = picked
                    file_key = h["file_key"]
                    # 语义命中返回 file_key；同时反查真实相对路径并一起返回，
                    # 让客户端优先按 path 读取（不依赖本地 fileMap 是否完整）
                    resolved_path = None
                    if file_key:
                        resolved_path = next(
                            (fp for fp in all_files if _file_key(fp) == file_key), None
                        )
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
    worker,
    ctx,
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


# ── LLM 生成 / 审查 ─────────────────────────────────────

async def generate_code_content(
    ctx,
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


async def _generate_local(instruction: str, path: str, original: str) -> str:
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


async def review_code_content(ctx, instruction: str, path: str, content: str) -> dict:
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
