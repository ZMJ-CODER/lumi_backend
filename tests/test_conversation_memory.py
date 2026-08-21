"""普通聊天分层记忆的纯逻辑回归测试。"""

import asyncio

from app.core.config import settings
from app.services.conversation_memory import ConversationRecall, needs_historical_recall
from app.services.orchestrator import Orchestrator
from app.services import conversation_trim


def test_history_recall_is_explicit_for_fast_mode():
    assert not needs_historical_recall("今天心情不错", "fast")
    assert needs_historical_recall("你还记得我们之前说的电影吗", "fast")
    assert not needs_historical_recall("他后来怎么样了", "fast")
    assert needs_historical_recall("他后来怎么样了", "think")


def test_chat_prompt_injects_only_selected_history(monkeypatch):
    orch = Orchestrator.__new__(Orchestrator)
    seen_budgets = []
    monkeypatch.setattr(
        orch,
        "_trim_history",
        lambda history, budget: seen_budgets.append(budget) or [{"role": "user", "content": "最近消息"}],
    )
    recall = ConversationRecall(
        global_summary="之前聊过毕业旅行",
        segment_summaries=("讨论过北海道行程和预算",),
        raw_messages=({"role": "user", "content": "我想去北海道"},),
    )

    messages = orch._build_messages(
        "chat",
        None,
        [],
        [{"role": "user", "content": "当前问题"}],
        "那个行程还记得吗",
        summary=recall.global_summary,
        system_prompt="system",
        thinking_mode="think",
        conversation_recall=recall,
    )

    assert seen_budgets == [min(settings.LLM_HISTORY_MAX_TOKENS, settings.CONVERSATION_RECENT_ROUNDS_THINK * 1000)]
    assert "相关此前话题摘要" in messages[0]["content"]
    assert messages[-2]["role"] == "system"
    assert "此前对话原文片段" in messages[-2]["content"]
    assert messages[-1]["content"] == "那个行程还记得吗"


def test_message_trim_is_disabled_when_long_history_is_enabled():
    # 该分支必须在查询数据库之前返回，否则“保留原文”仍可能被定时任务删除。
    original = settings.CONVERSATION_MESSAGE_HARD_CAP
    settings.CONVERSATION_MESSAGE_HARD_CAP = 0
    try:
        assert asyncio.run(conversation_trim.cleanup_all_conversations(object())) == 0
    finally:
        settings.CONVERSATION_MESSAGE_HARD_CAP = original
