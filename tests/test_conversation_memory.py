"""普通聊天分层记忆的纯逻辑回归测试。"""

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

    assert seen_budgets == [settings.LLM_HISTORY_MAX_TOKENS]
    assert "相关此前话题摘要" in messages[0]["content"]
    assert messages[-2]["role"] == "system"
    assert "此前对话原文片段" in messages[-2]["content"]
    assert messages[-1]["content"] == "那个行程还记得吗"


def test_token_window_evicts_oldest_prefix_only_after_trigger(monkeypatch):
    class _Message:
        def __init__(self, content):
            self.content = content

    monkeypatch.setattr(settings, "CONVERSATION_SUMMARY_TRIGGER_TOKENS", 8)
    monkeypatch.setattr(settings, "CONVERSATION_SUMMARY_KEEP_TOKENS", 4)
    messages = [_Message("aaaa"), _Message("bbbb"), _Message("cccc")]
    candidates = conversation_trim.select_messages_to_evict(messages)
    assert candidates == [messages[0], messages[1]]


def test_token_window_only_evicts_complete_summarized_segments(monkeypatch):
    class _Message:
        def __init__(self, content):
            self.content = content

    monkeypatch.setattr(settings, "CONVERSATION_SEGMENT_ROUNDS", 2)
    messages = [_Message(str(index)) for index in range(10)]
    # 候选前缀有 7 条，但已摘要前缀只允许按 4 条一段删除。
    assert conversation_trim.select_safe_evictable_messages(messages[:7], 8) == messages[:4]
