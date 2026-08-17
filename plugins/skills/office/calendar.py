"""办公技能（office/日程）：calendar_manager —— 个人日历（本地事件库 + 真实 ICS 互通）.

设计：
  - 事件库按用户隔离，持久化为 data/calendar/{user_id}.json；
  - 任何变更同步生成标准 ICS（data/calendar/{user_id}.ics），
    Outlook / Google Calendar / Thunderbird / 苹果日历均可直接导入；
  - export 把 ICS 写入"通用产物目录"，由前端 save_generated_output
    投递到用户下载目录并调用系统日历打开（触发导入弹窗）。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from app.agents.skills.base import Skill, SkillContext, SkillResult

CAL_DIR = Path(__file__).resolve().parents[3] / "data" / "calendar"


def _cal_file(user_id: str) -> Path:
    safe = "".join(c for c in str(user_id or "anon") if c.isalnum() or c in "-_") or "anon"
    return CAL_DIR / f"{safe}.json"


def _ics_file(user_id: str) -> Path:
    safe = "".join(c for c in str(user_id or "anon") if c.isalnum() or c in "-_") or "anon"
    return CAL_DIR / f"{safe}.ics"


def _load(user_id: str) -> list[dict]:
    try:
        p = _cal_file(user_id)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:  # noqa: BLE001
        pass
    return []


def _save(user_id: str, events: list[dict]) -> None:
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    _cal_file(user_id).write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _ics_file(user_id).write_text(_to_ics(events), encoding="utf-8")


def _fmt_ics_dt(value: str, all_day: bool) -> str:
    """'2026-08-20 09:00' → '20260820T090000'；全天事件 → '20260820'."""
    if not value:
        return ""
    value = value.strip()
    if all_day:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", value)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}T{int(m.group(4)):02d}{m.group(5)}00"
    # 只给日期 → 按全天处理
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else value.replace(":", "").replace("-", "").replace(" ", "T")


def _to_ics(events: list[dict]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Lumi//Calendar//CN",
        "CALSCALE:GREGORIAN",
    ]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    for ev in sorted(events, key=lambda e: e.get("start") or ""):
        uid = str(ev.get("uid") or uuid.uuid4().hex)
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}")
        lines.append(f"DTSTAMP:{stamp}")
        all_day = bool(ev.get("all_day"))
        start = _fmt_ics_dt(str(ev.get("start") or ""), all_day)
        end = _fmt_ics_dt(str(ev.get("end") or ""), all_day)
        date_only = all_day or "T" not in start
        if date_only:
            lines.append(f"DTSTART;VALUE=DATE:{start}")
            lines.append(f"DTEND;VALUE=DATE:{end or start}")
        else:
            lines.append(f"DTSTART:{start}")
            lines.append(f"DTEND:{end or start}")
        title = str(ev.get("title") or "无标题").replace("\n", "\\n")
        lines.append(f"SUMMARY:{title}")
        if ev.get("location"):
            lines.append(f"LOCATION:{str(ev['location']).replace(chr(10), '\\n')}")
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{str(ev['description']).replace(chr(10), '\\n')}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _parse_ics_text(text: str) -> list[dict]:
    """解析 ICS 文本 → 事件 dict 列表（展开折叠行，兼容常见日历导出）."""
    flat: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith((" ", "\t")) and flat:
            flat[-1] += line[1:]
        else:
            flat.append(line)
    events: list[dict] = []
    cur: dict = {}
    in_event = False
    for line in flat:
        key, _, value = line.partition(":")
        key = key.upper().strip()
        if key == "BEGIN" and value.strip().upper() == "VEVENT":
            cur = {}
            in_event = True
            continue
        if key == "END" and value.strip().upper() == "VEVENT":
            if cur.get("title") or cur.get("start"):
                events.append(cur)
            cur = {}
            in_event = False
            continue
        if not in_event:
            continue
        if key == "UID":
            cur["uid"] = value.strip()
        elif key == "SUMMARY":
            cur["title"] = value.strip().replace("\\n", "\n")
        elif key == "DTSTART" or key.startswith("DTSTART;"):
            cur["start"] = _ics_dt_to_readable(value.strip())
            cur["all_day"] = "VALUE=DATE" in key
        elif key == "DTEND" or key.startswith("DTEND;"):
            cur["end"] = _ics_dt_to_readable(value.strip())
        elif key == "LOCATION":
            cur["location"] = value.strip().replace("\\n", "\n")
        elif key == "DESCRIPTION":
            cur["description"] = value.strip().replace("\\n", "\n")
    return events


def _ics_dt_to_readable(value: str) -> str:
    v = value.replace("Z", "")
    if "T" in v:
        m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?", v)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"
        return v
    m = re.match(r"(\d{4})(\d{2})(\d{2})", v)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else v


def _fmt(events: list[dict]) -> str:
    if not events:
        return "（暂无日历事件）"
    lines = []
    for i, ev in enumerate(events, 1):
        start = str(ev.get("start") or "未设时间")
        end = f" → {ev.get('end')}" if ev.get("end") else ""
        loc = f" @ {ev['location']}" if ev.get("location") else ""
        lines.append(f"{i}. {ev.get('title') or '无标题'}｜{start}{end}{loc}")
        if ev.get("description"):
            lines.append(f"   说明：{ev['description'][:80]}")
    return "\n".join(lines)


class CalendarManagerSkill(Skill):
    name = "calendar_manager"
    description = (
        "个人日历管理：新增、查看、修改、删除日历事件；导出为 ICS 文件（可导入 "
        "Outlook / Google Calendar / Thunderbird / 苹果日历）；导入 ICS 日历文件内容。"
        "动作 action=add/list/update/delete/export/import"
    )
    category = "office"
    environment = "server"
    write_op = True
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "add/list/update/delete/export/import"},
            "title": {"type": "string", "description": "事件标题（add/update）"},
            "start": {"type": "string", "description": "开始时间，如 2026-08-20 09:00 或 2026-08-20（全天）"},
            "end": {"type": "string", "description": "结束时间（可选）"},
            "all_day": {"type": "boolean", "description": "是否全天事件（默认 false）"},
            "location": {"type": "string", "description": "地点（可选）"},
            "description": {"type": "string", "description": "说明（可选）"},
            "item_id": {"type": "string", "description": "事件 id（update/delete 必填）"},
            "window": {
                "type": "string",
                "description": "list 范围：today=今天 / upcoming=未来 N 天（配 days）/ all=全部",
            },
            "days": {"type": "integer", "description": "upcoming 的 N（默认 7）"},
            "content": {"type": "string", "description": "import 时传入的 ICS 文件内容"},
        },
        "required": ["action"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(
                success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False
            )
        action = str(params.get("action") or "").strip().lower()
        events = _load(context.user_id)

        if action == "add":
            title = str(params.get("title") or "").strip()
            start = str(params.get("start") or "").strip()
            if not title or not start:
                return SkillResult(
                    success=False,
                    error="add 需要 title 和 start（如 2026-08-20 09:00）",
                    error_code="INVALID_ARGS",
                    retryable=False,
                )
            ev = {
                "uid": uuid.uuid4().hex,
                "title": title,
                "start": start,
                "end": str(params.get("end") or "").strip() or None,
                "all_day": bool(params.get("all_day")),
                "location": str(params.get("location") or "").strip() or None,
                "description": str(params.get("description") or "").strip() or None,
                "created_at": time.time(),
            }
            events.append(ev)
            _save(context.user_id, events)
            return SkillResult(
                success=True,
                output=f"已添加日历事件：{title}（{start}）\n\n最近事件：\n{_fmt(events[-10:])}",
                metadata={"item_id": ev["uid"]},
            )

        if action == "list":
            window = str(params.get("window") or "upcoming").strip().lower()
            days = max(1, int(params.get("days") or 7))
            today = datetime.now().strftime("%Y-%m-%d")
            if window == "today":
                picked = [e for e in events if str(e.get("start") or "").startswith(today)]
            elif window == "all":
                picked = events
            else:
                limit = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
                picked = [
                    e
                    for e in events
                    if (str(e.get("start") or "")[:10] >= today and str(e.get("start") or "")[:10] <= limit)
                    or (str(e.get("end") or "")[:10] >= today)
                ]
            return SkillResult(
                success=True,
                output=f"日历事件（{window}）：\n{_fmt(sorted(picked, key=lambda e: e.get('start') or ''))}",
            )

        if action in ("update", "delete"):
            item_id = str(params.get("item_id") or "").strip()
            target = next((x for x in events if x.get("uid") == item_id), None)
            if not target:
                return SkillResult(
                    success=False, error="未找到该事件 id", error_code="INVALID_ARGS", retryable=False
                )
            if action == "delete":
                events.remove(target)
                _save(context.user_id, events)
                return SkillResult(success=True, output=f"已删除事件：{target.get('title')}")
            for field in ("title", "start", "end", "location", "description"):
                if params.get(field) is not None:
                    target[field] = str(params.get(field) or "").strip() or None
            if params.get("all_day") is not None:
                target["all_day"] = bool(params.get("all_day"))
            _save(context.user_id, events)
            return SkillResult(success=True, output=f"已更新事件：{target.get('title')}\n\n{_fmt([target])}")

        if action == "export":
            try:
                from app.services.office_docs import generic_outputs_dir

                conv_id = context.conversation_id or "default"
                out_dir = generic_outputs_dir(context.user_id, conv_id)
                out_dir.mkdir(parents=True, exist_ok=True)
                name = f"calendar-{datetime.now().strftime('%Y%m%d-%H%M%S')}.ics"
                (out_dir / name).write_text(_to_ics(events), encoding="utf-8")
                # 桌面端在线时自动投递到用户下载目录并调用系统日历打开（触发导入弹窗）
                try:
                    from app.agents.skills.executor import run_client_skill_request

                    await run_client_skill_request(
                        context.user_id,
                        "save_generated_output",
                        {"job_id": conv_id, "name": name},
                        False,
                    )
                except Exception:  # noqa: BLE001 桌面端离线时仅保留产物目录副本
                    pass
                return SkillResult(
                    success=True,
                    output=(
                        f"已导出日历 {len(events)} 个事件为 ICS：{name}\n"
                        "已保存到你的下载目录并用系统日历打开，可导入 Outlook / Google Calendar / "
                        "Thunderbird / 苹果日历。"
                    ),
                    metadata={"output_name": name, "count": len(events)},
                )
            except Exception as exc:  # noqa: BLE001
                return SkillResult(
                    success=False,
                    error=f"导出 ICS 失败：{exc}",
                    error_code="EXEC_ERROR",
                )

        if action == "import":
            content = str(params.get("content") or "").strip()
            if not content:
                return SkillResult(
                    success=False, error="import 需要 content（ICS 文件内容）", error_code="INVALID_ARGS", retryable=False
                )
            incoming = _parse_ics_text(content)
            if not incoming:
                return SkillResult(
                    success=False, error="未从 ICS 内容中解析到事件", error_code="INVALID_ARGS", retryable=False
                )
            known = {e.get("uid") for e in events if e.get("uid")}
            added = 0
            for ev in incoming:
                if ev.get("uid") and ev["uid"] in known:
                    continue
                if ev.get("uid"):
                    known.add(ev["uid"])
                ev.setdefault("uid", uuid.uuid4().hex)
                ev.setdefault("created_at", time.time())
                events.append(ev)
                added += 1
            _save(context.user_id, events)
            return SkillResult(
                success=True,
                output=f"已导入 {added} 个日历事件（重复 UID 自动跳过）\n\n最近事件：\n{_fmt(events[-10:])}",
            )

        return SkillResult(
            success=False,
            error="action 仅支持 add/list/update/delete/export/import",
            error_code="INVALID_ARGS",
            retryable=False,
        )
