"""Agent 公共工具 —— 文件定位 / LLM 生成 / 审查 / 步骤标题 等.

供 roles/ 下各领域 agent 复用；与执行编排解耦。
"""

import asyncio
import hashlib
import re

import httpx
from loguru import logger
from pathlib import Path

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
    if base.endswith(".d.ts"):
        return True  # 类型声明文件（env.d.ts 等）：写功能代码时不应作为目标
    if any(h in base for h in _CONFIG_FILE_HINTS):
        return True
    return base.endswith(_CONFIG_FILE_SUFFIXES)


def _is_config_intent(instruction: str) -> bool:
    """指令是否明确针对配置/依赖（此时允许定位到配置文件）."""
    text = str(instruction or "").lower()
    return any(k in text for k in _CONFIG_INTENT_KEYWORDS)


def _looks_like_new_file(rel_path: str) -> bool:
    """判断显式目标路径是否像"要新建的文件"（不在索引中时允许创建）."""
    p = str(rel_path or "").strip()
    if not p or p.endswith("/") or ".." in p.split("/"):
        return False
    return bool(re.search(r"\.[A-Za-z0-9]+$", p))


# ── 提示词模板 ───────────────────────────────────────────

def _read_prompt_file(name: str) -> str:
    """读取代码 agent 的上下文契约 / 自检清单（缺文件时静默降级）."""
    try:
        p = Path(__file__).resolve().parent.parent / "roles" / "code" / name
        return p.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""


_AGENTS_MD = _read_prompt_file("AGENTS.md")
_PITFALLS_MD = _read_prompt_file("COMMON_PITFALLS.md")


CODE_SYSTEM_PROMPT = (
    "你是一名资深软件工程师，正在用户的本地代码项目里工作。"
    "你需要根据用户的编码指令，生成完整的代码文件内容。"
    "代码必须规范完整，且与用户指令保持一致。"
    "动手前先在内部完成分析：理解用户意图、结合目录树定位改动点、评估对项目其他部分的影响，"
    "分析完成后给出最合理的实现，然后输出代码。"
    "根据用户指令和提供的文件内容，生成修改后的完整文件内容。"
    "只输出文件内容本身，不要解释、不要 Markdown 代码块围栏。"
    "如果项目架构需要，你可以新建或修改目录、文件（包括代码，文本等项目支持的格式）、依赖、配置文件等。"
    + (("\n\n# 工作规范（最小上下文契约）\n" + _AGENTS_MD) if _AGENTS_MD else "")
    + (("\n\n# 写码前自检清单\n" + _PITFALLS_MD) if _PITFALLS_MD else "")
)

CODE_SYSTEM_PROMPT_PATCH = (
    "你是一名资深软件工程师，正在用户的本地代码项目里工作。文件较大，"
    "动手前先在内部完成分析：理解用户意图、结合目录树定位改动点、评估影响，"
    "分析完成后再输出补丁。"
    "请用 SEARCH/REPLACE 块输出修改（不要输出整个文件）：\n"
    "<<<<<<< SEARCH\n需要替换的原文（必须逐字符复制自上方提供的文件内容，"
    "含首尾空格，并带足够上下文保证唯一匹配）\n=======\n新内容\n>>>>>>> REPLACE\n"
    "可输出多个块（按从上到下顺序）；不要修改无关代码；除 SEARCH/REPLACE 块外不要输出任何解释。"
    + (("\n\n# 工作规范（最小上下文契约）\n" + _AGENTS_MD) if _AGENTS_MD else "")
    + (("\n\n# 写码前自检清单\n" + _PITFALLS_MD) if _PITFALLS_MD else "")
)

FIX_SYSTEM_PROMPT = (
    "你是一名代码修复工程师。下面是静态类型检查/语法检查的报错输出，以及出错文件的代码。"
    "请只修复报错相关的问题：\n"
    "1. 逐条对照报错行号定位问题（导入缺失、类型不匹配、拼写不一致、模板与脚本不一致等）；\n"
    "2. 输出 SEARCH/REPLACE 补丁修复这些错误（不要输出整个文件）；\n"
    "3. SEARCH 必须逐字符复制文件原文（含首尾空格），并带足够上下文唯一匹配；\n"
    "4. 不要修改与报错无关的代码。\n"
    "格式：\n"
    "<<<<<<< SEARCH\n需要替换的原文\n=======\n新内容\n>>>>>>> REPLACE\n"
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
                max_tokens=4096,
                usage_user_id=ctx.user_id,
                usage_category=CATEGORY_TITLE,
                reasoning_effort="low",
                disable_reasoning_effort=True,
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
                # 显式指定的路径不在索引中：若是合理文件路径则标记为新文件（供 writer 创建），
                # 否则视为无效路径直接返回空，不再落到语义检索（避免改到不相干的文件）
                if _looks_like_new_file(str(target_file)):
                    return {"path": str(target_file), "new_file": True}
                return {}
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
            # 6) 找不到明确目标但项目有文件：仅当指令明确是 UI/页面类时才落到入口文件，
            #    避免"模糊指令永远默认改 app.vue/main.js"（写半天写不对入口文件）
            _UI_INTENT_KEYWORDS = (
                "页面",
                "首页",
                "界面",
                "样式",
                "布局",
                "外观",
                "美化",
                "按钮",
                "导航",
                "组件",
                "视图",
                "渲染",
                "ui",
                "page",
            )
            if all_files and any(
                kw in instruction.lower() for kw in _UI_INTENT_KEYWORDS
            ):
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


async def locate_from_memory(worker, ctx, project_id: str, instruction: str) -> dict | None:
    """快路径：热缓存命中 → 本地正则搜索命中，避免重新向量检索."""
    try:
        res = await worker.run_skill("get_project_context", {"project_id": project_id}, ctx)
        if not res.get("success"):
            return None
        text = str(res.get("content") or "")
        for line in text.splitlines():
            m = re.match(r"\s*[-*]\s*([\w./\-\\]+\.\w+)", line)
            if not m:
                continue
            fp = m.group(1).replace("\\", "/")
            name = fp.rsplit("/", 1)[-1]
            stem = name.rsplit(".", 1)[0] if "." in name else name
            if stem and len(stem) >= 2 and (stem in instruction or name in instruction):
                return {"path": fp, "from_memory": True}
    except Exception:  # noqa: BLE001
        pass

    # 本地正则搜索（用户提议的简化方案）：提取指令里的代码标识符，直接 grep 本地文件
    tokens = re.findall(r"[A-Za-z_]\w{2,}", instruction)[:3]
    if tokens:
        try:
            pattern = "|".join(re.escape(t) for t in tokens)
            res = await worker.run_skill(
                "grep_code",
                {"project_id": project_id, "pattern": pattern, "max_results": 8},
                ctx,
            )
            if res.get("success"):
                items = res.get("results") or []
                if items:
                    from collections import Counter

                    counts = Counter(str(x.get("path")) for x in items if x.get("path"))
                    for p, _ in counts.most_common(3):
                        return {"path": p, "from_memory": True, "from_grep": True}
        except Exception:  # noqa: BLE001
            pass
    return None


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


# ── 推理强度：渐进式（Progressive Reasoning） ─────────────

_EFFORT_LEVELS = ("low", "medium", "high")


def _effort_cap() -> str:
    """代码生成允许的最高推理档（AGENT_LLM_REASONING_EFFORT）."""
    cap = (settings.AGENT_LLM_REASONING_EFFORT or "high").strip().lower()
    return cap if cap in _EFFORT_LEVELS else "high"


def effort_start_for_task(instruction: str, original_len: int, is_new: bool) -> str:
    """代码生成起始推理档：简单改动 low（快）；明显复杂的新文件/大改动 medium 起步."""
    if _effort_cap() == "low":
        return "low"
    inst_len = len(instruction or "")
    if inst_len > 600 or original_len > 8000 or (is_new and inst_len > 300):
        return "medium"
    return "low"


def effort_escalate(current: str) -> str:
    """升级一档（不超过用户允许的最高档）；到顶后返回当前档."""
    cap = _effort_cap()
    idx = _EFFORT_LEVELS.index(current)
    cap_idx = _EFFORT_LEVELS.index(cap)
    if idx >= cap_idx:
        return current
    return _EFFORT_LEVELS[idx + 1]


def _format_project_tree(project_files: list[str], max_entries: int = 120) -> str:
    """把项目文件清单渲染成缩进目录树（第一层上下文：目录结构）.

    按路径深度缩进，最多展示 max_entries 条；目录节点自动生成。
    """
    files = sorted(f for f in project_files if f)[:max_entries]
    if not files:
        return "（项目文件索引为空）"
    lines: list[str] = []
    seen_dirs: set[str] = set()
    for fp in files:
        parts = fp.replace("\\", "/").strip("/").split("/")
        # 目录节点
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            if d in seen_dirs:
                continue
            seen_dirs.add(d)
            lines.append("  " * i + "📁 " + parts[i - 1] + "/")
        lines.append("  " * len(parts) + "📄 " + parts[-1])
    return "\n".join(lines)


# ── LLM 生成 / 审查 ─────────────────────────────────────

async def generate_code_content(
    ctx,
    instruction: str,
    path: str,
    original: str,
    system_prompt: str = CODE_SYSTEM_PROMPT,
    project_files: list[str] | None = None,
    reasoning_effort: str | None = None,
    patch_mode: bool = False,
    context_override: str | None = None,
    force_full: bool = False,
    stream_begin_cb=None,
    stream_cb=None,
    no_reasoning_override: bool | None = None,
) -> str:
    """LLM 生成文件内容（渐进推理）。

    patch_mode=True：大文件本地补丁模式——只返回 SEARCH/REPLACE 原始文本，
    由调用方通过客户端 apply_patch 技能应用；context_override 传已提取的代码块上下文。
    否则：小文件全文重写；大文件走服务端提取+应用兜底。
    force_full=True：强制按"小文件"语义全文重写（发送全文、输出整文件），
    用于补丁重试耗尽后的兜底，避免反复升档硬磕补丁。
    stream_cb：可选异步回调，收到生成文本增量时调用（流式写盘用）；
    传入后改用 chat_stream 增量产出，同时仍返回完整文本。
    stream_begin_cb：可选异步回调，每次 LLM 尝试开始前调用（流式写盘时用于
    通知客户端截断重写，避免上次失败尝试的残留内容污染文件）。
    """
    try:
        from app.core.llm import LLMClient
        from app.services.usage import CATEGORY_CODE
        from app.agents.core.patch import (
            apply_search_replace,
            build_edit_context,
            parse_search_replace,
        )

        llm = LLMClient()
        lines = (original or "").splitlines()
        is_large = (not force_full) and (patch_mode or (bool(original) and len(lines) > 150))
        # 大文件：只发相关代码块 + 引用导入；小文件/强制全文：全文
        if context_override:
            shown_content = context_override
        elif is_large:
            shown_content = build_edit_context(original, path, instruction)
        else:
            shown_content = (original or "")[-60000:]
        gen_prompt = CODE_SYSTEM_PROMPT_PATCH if is_large else system_prompt
        files_hint = ""
        if project_files:
            files_hint = (
                "\n\n项目目录树（第一层上下文，供定位改动点；写入路径必须是清单里的文件）：\n"
                + _format_project_tree(project_files)
            )
        retry_hint = ""
        instruction_short = (instruction or "")[: settings.AGENT_CODE_MAX_INSTRUCTION_CHARS]

        async def _chat_once(
            effort: str,
            extra: str = "",
            no_reasoning: bool = False,
            show: str | None = None,
            prompt: str | None = None,
        ) -> str:
            messages = [
                {"role": "system", "content": prompt or gen_prompt},
                {
                    "role": "user",
                    "content": (
                        f"用户指令：{instruction_short}{extra}\n\n"
                        f"文件路径：{path}\n\n当前文件内容：\n{show or shown_content}"
                        f"{files_hint}"
                    ),
                },
            ]

            async def _invoke(max_tokens: int | None) -> str:
                kwargs = {
                    "usage_user_id": ctx.user_id,
                    "usage_category": CATEGORY_CODE,
                    "reasoning_effort": effort,
                    "disable_reasoning_effort": no_reasoning,
                    "api_key": ctx.llm_api_key,
                }
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens
                if stream_cb is None:
                    return await llm.chat(messages, **kwargs)
                # 流式：逐段回调（写盘由客户端完成），同时累积完整文本返回
                if stream_begin_cb is not None:
                    try:
                        await stream_begin_cb()
                    except Exception:  # noqa: BLE001
                        pass  # 流式开始回调失败不影响生成
                parts: list[str] = []

                # 硬超时：单次流式生成最多 180s，防止网关只推 keep-alive 导致节点卡死
                async def _consume() -> None:
                    async for delta in llm.chat_stream(messages, **kwargs):
                        parts.append(delta)
                        try:
                            if stream_cb is not None:
                                await stream_cb(delta)
                        except Exception:  # noqa: BLE001
                            pass  # 流式回调失败不影响生成

                await asyncio.wait_for(_consume(), timeout=180)
                return "".join(parts)

            try:
                return await _invoke(settings.AGENT_CODE_MAX_TOKENS)
            except httpx.HTTPStatusError as exc:
                # 部分网关不接受过大的 max_tokens（400）：去掉该参数重试一次
                if exc.response is not None and exc.response.status_code == 400:
                    logger.warning(
                        "[Agent] 网关拒绝 max_tokens={}，去掉后重试: {}",
                        settings.AGENT_CODE_MAX_TOKENS,
                        exc,
                    )
                    return await _invoke(None)
                raise

        effort = reasoning_effort or effort_start_for_task(
            instruction, len(original), not bool((original or "").strip())
        )
        reply = ""
        # 代码生成默认关闭推理（配置可关）：thinking 慢且常把输出预算烧光导致空内容
        no_reasoning = (
            no_reasoning_override
            if no_reasoning_override is not None
            else bool(settings.AGENT_CODE_NO_REASONING)
        )
        for _attempt in range(2):
            try:
                reply = await _chat_once(effort, retry_hint, no_reasoning=no_reasoning)
                if not (reply or "").strip():
                    raise RuntimeError("模型返回空内容")
            except Exception as exc:  # noqa: BLE001
                if not no_reasoning:
                    # 兜底：若开启推理，空内容多为推理把输出预算烧光 → 关推理重试
                    logger.warning("[Agent] LLM 生成失败（effort={}）: {}，关闭推理重试", effort, exc)
                    no_reasoning = True
                    continue
                nxt = effort_escalate(effort)
                if nxt != effort:
                    effort = nxt
                    continue
                logger.warning("[Agent] 推理档到顶，回退本地模型")
                reply = await _generate_local(instruction, path, original)
                break
            if not is_large:
                break  # 小文件：全文即结果
            if patch_mode:
                break  # 客户端应用：直接返回原始补丁文本
            blocks = parse_search_replace(reply)
            if not blocks:
                break  # 模型未按补丁格式输出：当作整文件内容兜底
            new_content, failures = apply_search_replace(original, blocks)
            if not failures:
                reply = new_content
                break
            # 补丁应用失败：把具体原因带回给模型，同档重试一次（不升档，避免越试越慢）
            retry_hint = (
                "\n\n【上次 SEARCH/REPLACE 应用失败】\n"
                + "\n".join(failures)
                + "\n请重新输出补丁：SEARCH 必须逐字符复制文件原文（含首尾空格），"
                "并带足够上下文唯一匹配；不要修改无关代码。"
            )
        else:
            # 服务端补丁重试耗尽：回退全文重写（不升档硬磕补丁）
            logger.warning("[Agent] 服务端补丁重试耗尽，回退全文重写")
            try:
                reply = await _chat_once(
                    effort,
                    no_reasoning=no_reasoning,
                    show=(original or "")[-60000:],
                    prompt=system_prompt,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Agent] 全文重写失败: {}", exc)
                reply = await _generate_local(instruction, path, original)
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
        from app.agents.langchain.planning import invoke_json_object

        data = await invoke_json_object(
            (
                f"{REVIEW_SYSTEM_PROMPT}\n\n"
                f"用户指令：{instruction}\n文件路径：{path}\n文件内容：\n{content[:12000]}"
            ),
            user_id=ctx.user_id,
            api_key=ctx.llm_api_key,
            max_tokens=4096,
        )
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
