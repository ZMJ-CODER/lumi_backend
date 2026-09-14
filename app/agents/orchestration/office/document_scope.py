"""Document authorization and artifact-delivery helpers.

These helpers do not classify business intent. They only resolve explicitly
named attachments inside the already-authorized scope and derive a small,
verifiable output contract for execution.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

_OUTPUT_FILENAME_RE = re.compile(
    r"(?iu)(?:保存为|另存为|输出为|导出为|存成|存为|生成(?:文件)?(?:名为)?|命名为|文件名为|文件名叫|文件叫|"
    r"save(?:\s+it)?\s+as|export(?:\s+it)?\s+as|guardar(?:lo)?\s+como|guárdalo\s+como|enregistrer\s+sous)\s*"
    r"(?:文件\s*)?([a-z0-9_\-\u4e00-\u9fff][a-z0-9_.\-\u4e00-\u9fff]*\.[a-z0-9]{1,10})\b"
)
_OUTPUT_FILENAME_SUFFIX_RE = re.compile(
    r"(?iu)([a-z0-9_\-\u4e00-\u9fff][a-z0-9_.\-\u4e00-\u9fff]*\.[a-z0-9]{1,10})\s*"
    r"(?:として保存|として出力|に保存(?:して)?ください?|として保存してください)"
)
_FILENAME_RE = re.compile(
    r"(?iu)([a-z0-9_\-\u4e00-\u9fff][a-z0-9_.\-\u4e00-\u9fff]*?\.[a-z][a-z0-9]{0,9})"
    r"(?=$|[\s《“\"'：:，,。；;、和与及并或])"
)

def _normalise_filename(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", Path(value or "").name.casefold())

def _explicit_output_filename(request: str) -> str | None:
    candidates = [match for pattern in (_OUTPUT_FILENAME_RE, _OUTPUT_FILENAME_SUFFIX_RE)
                  if (match := pattern.search(request or ""))]
    match = min(candidates, key=lambda item: item.start()) if candidates else None
    return Path(match.group(1)).name if match else None

def extract_output_contract(request: str, conversion: dict[str, Any] | None = None) -> dict[str, Any]:
    output_filename = ""
    if isinstance(conversion, dict):
        output_filename = Path(str(conversion.get("output_filename") or "")).name
    output_filename = output_filename or (_explicit_output_filename(request) or "")
    target_extension = Path(output_filename).suffix.casefold() if output_filename else ""
    if isinstance(conversion, dict):
        target_extension = str(conversion.get("target_extension") or target_extension).casefold()
    result: dict[str, Any] = {
        "version": 1,
        "requires_artifact": bool(output_filename),
        "expected_output_names": [output_filename] if output_filename else [],
    }
    if target_extension:
        result["target_extension"] = target_extension
    return result

def _filename_match_score(requested: str, uploaded: str) -> float:
    requested_name = _normalise_filename(requested)
    uploaded_name = _normalise_filename(uploaded)
    if not requested_name or not uploaded_name:
        return 0.0
    if requested_name == uploaded_name:
        return 1.0
    if Path(requested_name).suffix != Path(uploaded_name).suffix:
        return 0.0
    return SequenceMatcher(None, requested_name, uploaded_name).ratio()

def select_named_office_documents(request: str, office_docs: list[dict] | None) -> tuple[list[dict], list[str], bool]:
    """Resolve explicit filenames without widening document authorization."""
    output_name = _normalise_filename(_explicit_output_filename(request) or "")
    requested_names: list[str] = []
    for raw in _FILENAME_RE.findall(request or ""):
        name = re.sub(
            r"^(?:生成(?:文件)?(?:名为)?|导出为|保存为|另存为|命名为|将|把|"
            r"对比|比较|总结|分析|提取|读取|查看|阅读|和|与|及|并)",
            "", Path(raw.strip()).name, flags=re.IGNORECASE,
        )
        if name and _normalise_filename(name) != output_name and name not in requested_names:
            requested_names.append(name)
    if not requested_names:
        return list(office_docs or []), [], False
    selected: list[dict] = []
    unresolved: list[str] = []
    seen_ids: set[str] = set()
    for requested_name in requested_names:
        candidates = [
            (_filename_match_score(requested_name, str(document.get("filename") or "")), document)
            for document in office_docs or []
            if document.get("doc_id") and document.get("filename")
        ]
        candidates = sorted((item for item in candidates if item[0] >= 0.86), reverse=True, key=lambda item: item[0])
        if not candidates or (len(candidates) > 1 and candidates[1][0] >= candidates[0][0] - 0.04):
            unresolved.append(requested_name)
            continue
        doc_id = str(candidates[0][1]["doc_id"])
        if doc_id not in seen_ids:
            selected.append(candidates[0][1])
            seen_ids.add(doc_id)
    return selected, unresolved, True
