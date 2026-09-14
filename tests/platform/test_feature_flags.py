"""接入灰度开关回归（方案阶段 6：默认全关、未知名字安全、只切行为不切契约）。"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.config import Settings
from app.platform.runtime.feature_flags import (
    ALL_FLAGS,
    FEATURE_FLAGS,
    SHADOW_FLAG,
    feature_enabled,
    flag_snapshot,
    shadow_mode,
)
from lumi_contracts import EVENT_ENVELOPE_VERSION


def test_all_flags_default_to_off_in_settings():
    settings = Settings()
    for flag in ALL_FLAGS:
        assert hasattr(settings, flag), f"缺少开关 {flag}"
        assert getattr(settings, flag) is False, f"{flag} 必须默认关闭（否则新逻辑变成隐式默认）"


def test_feature_enabled_reads_the_setting():
    on = SimpleNamespace(CAPABILITY_PREFLIGHT_V2=True, MODEL_CAPABILITY_ROUTER_V2=False)
    assert feature_enabled("capability_preflight_v2", settings=on) is True
    assert feature_enabled("MODEL_CAPABILITY_ROUTER_V2", settings=on) is False


def test_unknown_flag_is_false_and_never_raises():
    assert feature_enabled("", settings=SimpleNamespace()) is False
    assert feature_enabled("NOT_A_FLAG", settings=SimpleNamespace()) is False
    assert feature_enabled("CAPABILITY_PREFLIGHT_V2_TYPO") is False


def test_shadow_mode_is_separate_from_the_six_switches():
    assert SHADOW_FLAG not in FEATURE_FLAGS
    assert feature_enabled(SHADOW_FLAG, settings=SimpleNamespace(INTEGRATION_SHADOW_MODE=True)) is True
    assert shadow_mode(settings=SimpleNamespace(INTEGRATION_SHADOW_MODE=False)) is False


def test_snapshot_covers_every_flag():
    snapshot = flag_snapshot(settings=Settings())
    assert set(snapshot) == set(ALL_FLAGS)
    assert all(value is False for value in snapshot.values())


def test_flags_do_not_change_the_frozen_contracts():
    """开关只切"新行为是否生效"，不得改契约：协议版本与错误码集合不受影响。"""
    from lumi_contracts.events.errors import FROZEN_ERROR_CODES

    assert EVENT_ENVELOPE_VERSION == 1
    assert "CAPABILITY_UNAVAILABLE" in FROZEN_ERROR_CODES
    assert feature_enabled("ARCHIVE_CONTENT_V2", settings=Settings()) is False
