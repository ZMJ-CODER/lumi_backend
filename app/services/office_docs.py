"""办公文档结构化编辑引擎.

能力：
  - docx / xlsx / pptx：结构化读取与编辑（python-docx / openpyxl / python-pptx），
    不触碰二进制流，按"结构→编辑指令→应用"工作；
  - md / txt / json / csv / yaml 等纯文本：全量重写或 SEARCH/REPLACE 补丁；
  - 缓冲会话：编辑只作用于 buffered 副本，用户审核通过后取回落盘，可随时丢弃。

会话布局：data/office/{user_id}/{doc_id}/original.ext + buffered.ext + meta.json
"""

import json
import shutil
import uuid
from pathlib import Path

from app.core.config import settings

OFFICE_DIR = Path(settings.UPLOAD_DIR).parent / "office"
TEXT_EXTS = {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".toml", ".ini", ".log", ".xml"}
STRUCTURED_EXTS = {".docx", ".xlsx", ".pptx"}
MAX_OFFICE_SIZE = 30 * 1024 * 1024

# 各格式允许的编辑操作（LLM 规划指令时用）
OP_SCHEMAS = {
    "docx": [
        '{"op":"replace_paragraph","index":0,"text":"新内容"}',
        '{"op":"add_paragraph","text":"新段落","index":null}',
        '{"op":"delete_paragraph","index":0}',
        '{"op":"replace_all","find":"旧词","replace":"新词"}',
        '{"op":"replace_table_cell","table_index":0,"row":0,"col":0,"text":"新内容"}',
        '{"op":"add_table_row","table_index":0,"values":["a","b"]}',
    ],
    "xlsx": [
        '{"op":"set_cell","sheet":"Sheet1","cell":"A1","value":"x"}',
        '{"op":"replace_all","sheet":"Sheet1","find":"旧值","replace":"新值"}',
        '{"op":"add_row","sheet":"Sheet1","values":["a","b"]}',
    ],
    "pptx": [
        '{"op":"replace_text","slide_index":0,"find":"旧词","replace":"新词"}',
        '{"op":"add_text","slide_index":0,"text":"新文本"}',
    ],
    "text": [
        '{"op":"rewrite","content":"完整新内容"}',
        '{"op":"search_replace","old":"原文片段","new":"新片段","first_only":false}',
    ],
}


async def plan_edits(
    instruction: str,
    structure: str,
    kind: str,
    user_id: str,
    api_key: str | None = None,
) -> list[dict]:
    """让 LLM 根据文档结构与用户指令，输出结构化的编辑指令 JSON 列表."""
    from app.core.llm import LLMClient
    from app.services.usage import CATEGORY_SKILL

    llm = LLMClient()
    ops = OP_SCHEMAS.get(kind, OP_SCHEMAS["text"])
    system = (
        "你是办公文档编辑规划器。根据文档结构和用户指令，输出要执行的编辑操作列表。\n"
        f"本文件类型 {kind} 允许的操作（字段按示例）：\n"
        + "\n".join(f"- {o}" for o in ops)
        + "\n约束：索引从 0 开始；只输出 JSON 数组（不要 Markdown 围栏、不要解释）；"
        "无法满足的指令输出空数组 []。"
    )
    reply = await llm.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": f"用户指令：{instruction}\n\n文档结构：\n{structure[:60000]}"},
        ],
        scene="office",
        max_tokens=4000,
        temperature=0.1,
        usage_user_id=user_id,
        usage_category=CATEGORY_SKILL,
        disable_reasoning_effort=True,
        api_key=api_key,
    )
    text = (reply or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _session_dir(user_id: str, doc_id: str) -> Path:
    safe_u = "".join(c for c in str(user_id) if c.isalnum() or c in "-_") or "anon"
    safe_d = "".join(c for c in str(doc_id) if c.isalnum() or c in "-_")
    return OFFICE_DIR / safe_u / safe_d


def _original_path(session: Path, ext: str) -> Path:
    return session / f"original{ext}"


def _buffered_path(session: Path, ext: str) -> Path:
    return session / f"buffered{ext}"


def _meta_path(session: Path) -> Path:
    return session / "meta.json"


def detect_kind(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in STRUCTURED_EXTS:
        return ext.lstrip(".")
    return "text"


# ── 会话管理 ─────────────────────────────────────────────

def create_session(user_id: str, filename: str, content: bytes) -> dict:
    """保存上传文件并建立编辑会话；返回会话元数据."""
    if len(content) > MAX_OFFICE_SIZE:
        raise ValueError("文件超过 30MB 限制")
    ext = Path(filename).suffix.lower() or ".txt"
    doc_id = uuid.uuid4().hex[:12]
    session = _session_dir(user_id, doc_id)
    session.mkdir(parents=True, exist_ok=True)
    (session / f"original{ext}").write_bytes(content)
    meta = {
        "doc_id": doc_id,
        "filename": filename,
        "ext": ext,
        "kind": detect_kind(filename),
        "created_at": None,
        "committed": False,
    }
    _meta_path(session).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def load_session(user_id: str, doc_id: str) -> dict:
    session = _session_dir(user_id, doc_id)
    if not session.exists():
        raise LookupError("办公文档会话不存在")
    meta = json.loads(_meta_path(session).read_text(encoding="utf-8"))
    meta["_session"] = str(session)
    return meta


async def discard_session(user_id: str, doc_id: str) -> None:
    """丢弃会话：删除 RAG 空间数据 + 会话目录."""
    session = _session_dir(user_id, doc_id)
    if session.exists():
        try:
            meta = json.loads(_meta_path(session).read_text(encoding="utf-8"))
            if meta.get("space_id"):
                await _delete_space_rows(user_id, meta["space_id"])
        except Exception:  # noqa: BLE001
            pass
    if session.exists():
        shutil.rmtree(session, ignore_errors=True)


def _content_path(meta: dict) -> Path:
    session = Path(meta["_session"])
    buffered = _buffered_path(session, meta["ext"])
    if buffered.exists():
        return buffered
    return _original_path(session, meta["ext"])


def extract_full_text(user_id: str, doc_id: str) -> str:
    """抽取文档全文（缓冲优先），供 RAG 索引."""
    meta = load_session(user_id, doc_id)
    path = _content_path(meta)
    kind = meta["kind"]
    if kind == "docx":
        return _full_docx(path)
    if kind == "xlsx":
        return _full_xlsx(path)
    if kind == "pptx":
        return _full_pptx(path)
    return path.read_text(encoding="utf-8", errors="replace")


def _full_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for ti, table in enumerate(doc.tables):
        parts.append(f"[表格{ti}]")
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def _full_xlsx(path: Path) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"[Sheet: {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            vals = ["" if v is None else str(v) for v in row]
            if any(vals):
                parts.append(" | ".join(vals))
    return "\n".join(parts)


def _full_pptx(path: Path) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    parts = []
    for si, slide in enumerate(prs.slides):
        parts.append(f"[第{si}页]")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text.strip())
            elif shape.has_table:
                for row in shape.table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


# ── 结构读取 ─────────────────────────────────────────────

def read_structure(user_id: str, doc_id: str) -> dict:
    """读取文档结构（不是二进制流），供 LLM 决策编辑指令."""
    meta = load_session(user_id, doc_id)
    path = _content_path(meta)
    kind = meta["kind"]
    if kind == "docx":
        structure = _read_docx(path)
    elif kind == "xlsx":
        structure = _read_xlsx(path)
    elif kind == "pptx":
        structure = _read_pptx(path)
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        structure = text[:120000]
    return {"doc_id": doc_id, "kind": kind, "filename": meta["filename"], "structure": structure}


def _read_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    lines = [f"# 文档段落（共 {len(doc.paragraphs)} 段）"]
    for i, p in enumerate(doc.paragraphs):
        if p.text.strip():
            lines.append(f"[段{i}] {p.text}")
    for ti, table in enumerate(doc.tables):
        lines.append(f"# 表格{ti}（{len(table.rows)}行 x {len(table.columns)}列）")
        for ri, row in enumerate(table.rows[:30]):
            cells = [c.text.strip() for c in row.cells]
            lines.append(f"  行{ri}: {' | '.join(cells)}")
    return "\n".join(lines)[:120000]


def _read_xlsx(path: Path) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=True)
    lines = [f"# 工作簿（共 {len(wb.sheetnames)} 个表）: {', '.join(wb.sheetnames)}"]
    for ws in wb.worksheets:
        lines.append(f"## Sheet: {ws.title}（{ws.max_row}行 x {ws.max_column}列）")
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 60), values_only=True):
            vals = ["" if v is None else str(v) for v in row]
            if any(vals):
                lines.append("  " + " | ".join(vals))
    return "\n".join(lines)[:120000]


def _read_pptx(path: Path) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    lines = [f"# 演示文稿（共 {len(prs.slides)} 页）"]
    for si, slide in enumerate(prs.slides):
        lines.append(f"## 第{si}页")
        for shape in slide.shapes:
            if shape.has_text_frame:
                txt = shape.text_frame.text.strip()
                if txt:
                    lines.append(f"  [文本] {txt}")
            elif shape.has_table:
                tbl = shape.table
                lines.append(f"  [表格] {len(tbl.rows)}行x{len(tbl.columns)}列")
    return "\n".join(lines)[:120000]


# ── 编辑指令应用 ─────────────────────────────────────────

def apply_edits(user_id: str, doc_id: str, ops: list[dict]) -> list[str]:
    """把编辑指令应用到缓冲副本；返回修改记录列表."""
    meta = load_session(user_id, doc_id)
    session = Path(meta["_session"])
    src = _original_path(session, meta["ext"])
    buffered = _buffered_path(session, meta["ext"])
    if not buffered.exists():
        shutil.copy2(src, buffered)
    records: list[str] = []
    kind = meta["kind"]
    for op in ops or []:
        try:
            if kind == "docx":
                r = _apply_docx_op(buffered, op)
            elif kind == "xlsx":
                r = _apply_xlsx_op(buffered, op)
            elif kind == "pptx":
                r = _apply_pptx_op(buffered, op)
            else:
                r = _apply_text_op(buffered, op)
            records.append(r)
        except Exception as exc:  # noqa: BLE001
            records.append(f"❌ 操作 {op.get('op')} 失败：{exc}")
    return records


def _apply_text_op(path: Path, op: dict) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    op_name = op.get("op")
    if op_name == "rewrite":
        new_text = str(op.get("content") or "")
        path.write_text(new_text, encoding="utf-8")
        return f"✅ 已全文重写（{len(text)}→{len(new_text)} 字符）"
    if op_name == "search_replace":
        old = str(op.get("old") or "")
        new = str(op.get("new") or "")
        if not old:
            raise ValueError("search_replace 缺少 old")
        count = text.count(old)
        if count == 0:
            raise ValueError("未找到要替换的原文")
        path.write_text(text.replace(old, new, 1 if op.get("first_only") else count), encoding="utf-8")
        return f"✅ 已替换 {count} 处：{old[:30]} → {new[:30]}"
    raise ValueError(f"不支持的纯文本操作: {op_name}")


def _apply_docx_op(path: Path, op: dict) -> str:
    from docx import Document

    doc = Document(str(path))
    op_name = op.get("op")
    saved = False

    def _save():
        nonlocal saved
        if not saved:
            doc.save(str(path))
            saved = True

    if op_name == "replace_paragraph":
        idx = int(op.get("index") or 0)
        if idx >= len(doc.paragraphs):
            raise ValueError(f"段落索引越界: {idx}")
        p = doc.paragraphs[idx]
        for run in p.runs:
            run.text = ""
        if p.runs:
            p.runs[0].text = str(op.get("text") or "")
        else:
            p.add_run(str(op.get("text") or ""))
        _save()
        return f"✅ 已替换第{idx}段"
    if op_name == "add_paragraph":
        idx = int(op.get("index") or len(doc.paragraphs))
        p = doc.paragraphs[idx].insert_paragraph_before() if idx < len(doc.paragraphs) else doc.add_paragraph()
        p.add_run(str(op.get("text") or ""))
        _save()
        return "✅ 已新增段落"
    if op_name == "delete_paragraph":
        idx = int(op.get("index") or 0)
        p = doc.paragraphs[idx]._element
        p.getparent().remove(p)
        _save()
        return f"✅ 已删除第{idx}段"
    if op_name == "replace_all":
        find = str(op.get("find") or "")
        replace = str(op.get("replace") or "")
        count = 0
        for p in doc.paragraphs:
            for run in p.runs:
                if find in run.text:
                    run.text = run.text.replace(find, replace)
                    count += 1
        _save()
        return f"✅ 文档正文替换 {find} → {replace}（{count} 处）"
    if op_name == "replace_table_cell":
        ti = int(op.get("table_index") or 0)
        row = int(op.get("row") or 0)
        col = int(op.get("col") or 0)
        cell = doc.tables[ti].cell(row, col)
        cell.text = str(op.get("text") or "")
        _save()
        return f"✅ 表格{ti} 单元格({row},{col}) 已替换"
    if op_name == "add_table_row":
        ti = int(op.get("table_index") or 0)
        values = op.get("values") or []
        row = doc.tables[ti].add_row()
        for ci, v in enumerate(values):
            if ci < len(row.cells):
                row.cells[ci].text = str(v)
        _save()
        return f"✅ 表格{ti} 已新增一行"
    raise ValueError(f"不支持的 docx 操作: {op_name}")


def _apply_xlsx_op(path: Path, op: dict) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(str(path))
    op_name = op.get("op")
    ws = wb[op.get("sheet") or wb.sheetnames[0]] if op.get("sheet") else wb.active
    if op_name == "set_cell":
        ws[op.get("cell")] = op.get("value")
        wb.save(str(path))
        return f"✅ 单元格 {op.get('cell')} = {op.get('value')}"
    if op_name == "replace_all":
        find = str(op.get("find") or "")
        replace = str(op.get("replace") or "")
        count = 0
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None and find in str(c.value):
                    c.value = str(c.value).replace(find, replace)
                    count += 1
        wb.save(str(path))
        return f"✅ Sheet[{ws.title}] 替换 {find} → {replace}（{count} 处）"
    if op_name == "add_row":
        values = op.get("values") or []
        ws.append(values)
        wb.save(str(path))
        return f"✅ Sheet[{ws.title}] 已新增一行"
    raise ValueError(f"不支持的 xlsx 操作: {op_name}")


def _apply_pptx_op(path: Path, op: dict) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    op_name = op.get("op")
    if op_name == "replace_text":
        si = int(op.get("slide_index") or 0)
        find = str(op.get("find") or "")
        replace = str(op.get("replace") or "")
        count = 0
        for shape in prs.slides[si].shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        if find in run.text:
                            run.text = run.text.replace(find, replace)
                            count += 1
        prs.save(str(path))
        return f"✅ 第{si}页替换 {find} → {replace}（{count} 处）"
    if op_name == "add_text":
        si = int(op.get("slide_index") or 0)
        slide = prs.slides[si]
        from pptx.util import Inches

        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
        box.text_frame.text = str(op.get("text") or "")
        prs.save(str(path))
        return f"✅ 第{si}页已新增文本框"
    raise ValueError(f"不支持的 pptx 操作: {op_name}")


# ── 会话级 RAG（分析/问答/总结用，复用现有知识库管线） ──────────

def _space_tag(doc_id: str) -> str:
    return f"officedoc_{doc_id}"


async def _delete_space_rows(user_id: str, space_id: str) -> None:
    from sqlalchemy import text

    from app.core.database import async_session_factory

    async with async_session_factory() as session:
        await session.execute(text("DELETE FROM document_chunks WHERE space_id = :i"), {"i": space_id})
        await session.execute(text("DELETE FROM documents WHERE space_id = :i"), {"i": space_id})
        await session.execute(text("DELETE FROM knowledge_spaces WHERE id = :i"), {"i": space_id})
        await session.commit()


async def ensure_rag_index(user_id: str, doc_id: str) -> dict:
    """把文档全文索引进会话专属私有 RAG 空间（幂等，内容变化自动重建）."""
    import hashlib

    from app.core.database import async_session_factory
    from app.services.rag.knowledge import create_space, process_document_pipeline, upload_document_file

    meta = load_session(user_id, doc_id)
    text = extract_full_text(user_id, doc_id)
    if not text.strip():
        raise ValueError("文档无可索引文本")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if meta.get("content_hash") == content_hash and meta.get("space_id"):
        return meta
    if meta.get("space_id"):
        await _delete_space_rows(user_id, meta["space_id"])
    async with async_session_factory() as session:
        space = await create_space(
            session,
            user_id,
            f"办公文档会话-{doc_id}",
            "办公文档分析索引（随会话丢弃）",
            _space_tag(doc_id),
        )
        doc, path, _ = await upload_document_file(
            session,
            user_id,
            str(space.id),
            f"{meta['filename']}.txt",
            text.encode("utf-8"),
        )
        await session.commit()
        await process_document_pipeline(session, str(doc.id), path)
    meta["space_id"] = str(space.id)
    meta["rag_doc_id"] = str(doc.id)
    meta["content_hash"] = content_hash
    _meta_path(Path(meta["_session"])).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


async def analyze_doc(
    user_id: str,
    doc_id: str,
    instruction: str,
    mode: str = "qa",
    api_key: str | None = None,
) -> dict:
    """基于会话 RAG 检索 + LLM 作答（总结 / 问答）."""
    from app.agents.skills.base import SkillContext
    from app.core.database import async_session_factory
    from app.services.office_skill_utils import office_llm
    from app.services.rag.knowledge import search_user_knowledge

    await ensure_rag_index(user_id, doc_id)
    async with async_session_factory() as session:
        rag_text, citations = await search_user_knowledge(
            session,
            user_id,
            instruction,
            [_space_tag(doc_id)],
            top_k=6,
            exclude_categories=["code"],
        )
    if not rag_text:
        return {
            "answer": "文档中未检索到与该问题相关的内容",
            "citations": [],
            "records": [],
        }
    system = (
        "你是文档分析助手。仅依据给定文档片段作答；片段不足以支撑时明确说明，不要编造。"
        if mode == "qa"
        else "你是文档总结助手。基于给定文档片段输出结构化摘要（要点列表，含关键数据）。不要编造。"
    )
    answer = await office_llm(
        SkillContext(user_id=user_id, scene="office", llm_api_key=api_key),
        system,
        f"用户问题/指令：{instruction}\n\n文档片段：\n{rag_text[:60000]}",
        max_tokens=6000,
    )
    return {
        "answer": answer,
        "citations": citations,
        "records": [f"已从文档检索到 {len(citations)} 个相关片段"],
    }
