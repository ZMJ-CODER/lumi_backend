"""可见性状态机的纯函数测试。"""

from __future__ import annotations

import pytest

from lumi_capability import state as s


def test_unknown_tool_is_unregistered_not_visible():
    """把"系统不认识"读成"允许使用"是最危险的一类误读，绝不复用 eligible。"""
    assert s.resolve_visibility(known=False, pool_state="eligible") == s.STATE_UNREGISTERED
    assert s.resolve_visibility(known=False) == s.STATE_UNREGISTERED


def test_not_probed_falls_back_to_registered():
    """调用方没探测（或探测失败）时给 registered，而不是乐观地给 visible。"""
    assert s.resolve_visibility(known=True) == s.STATE_REGISTERED
    assert s.resolve_visibility(known=True, pool_state="") == s.STATE_REGISTERED


@pytest.mark.parametrize(
    "pool_state,expected",
    [
        ("eligible", s.STATE_VISIBLE),
        ("catalog", s.STATE_VISIBLE),
        ("available", s.STATE_AVAILABLE),
        ("unavailable", s.STATE_UNAVAILABLE),
        ("something_new", s.STATE_REGISTERED),
    ],
)
def test_pool_states_map_to_visibility(pool_state, expected):
    assert s.resolve_visibility(known=True, pool_state=pool_state) == expected


def test_vocabulary_is_injectable():
    """池自己的状态词表是应用概念：换个应用换个词表，状态机不用改。"""
    custom = s.VisibilityVocabulary(
        available=frozenset({"ready"}), unavailable=frozenset({"blocked"}), visible=frozenset({"listed"})
    )
    assert s.resolve_visibility(known=True, pool_state="ready", vocabulary=custom) == s.STATE_AVAILABLE
    assert s.resolve_visibility(known=True, pool_state="blocked", vocabulary=custom) == s.STATE_UNAVAILABLE
    assert s.resolve_visibility(known=True, pool_state="listed", vocabulary=custom) == s.STATE_VISIBLE
    # 默认词表不认识这些词 → registered（不猜）
    assert s.resolve_visibility(known=True, pool_state="ready") == s.STATE_REGISTERED


def test_states_are_closed_and_ordered():
    assert s.VISIBILITY_STATES == frozenset(
        {"unregistered", "registered", "visible", "available", "unavailable"}
    )
    assert s.visibility_rank(s.STATE_UNREGISTERED) == 0
    assert s.visibility_rank(s.STATE_AVAILABLE) > s.visibility_rank(s.STATE_VISIBLE)
    assert s.visibility_rank(s.STATE_UNREGISTERED) < s.visibility_rank(s.STATE_REGISTERED)
    assert s.visibility_rank("bogus") == 0


def test_visibility_ranked_is_stable_and_descending():
    rows = s.visibility_ranked({"b": "available", "a": "available", "c": "registered"})
    assert rows == [("a", "available"), ("b", "available"), ("c", "registered")]


def test_every_resolved_state_is_in_the_closed_set():
    for known in (True, False):
        for pool_state in (None, "", "eligible", "catalog", "available", "unavailable", "zzz"):
            assert s.resolve_visibility(known=known, pool_state=pool_state) in s.VISIBILITY_STATES
