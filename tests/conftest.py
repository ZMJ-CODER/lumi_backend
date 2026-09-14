"""pytest 公共配置：把项目根目录加入 sys.path（仓库根运行 pytest 即可）."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_effect_journal():
    """每个用例前后把副作用安全日志复位为**内存实现**。

    背景：``app.agents.orchestration.runtime.effects._repository`` 是模块级全局。写类步骤第一次
    访问它时会惰性创建 ``PostgresEffectJournalRepository``（本机没有数据库连接），此后
    所有用例的写步骤都会以"副作用安全日志不可用，已阻止执行"失败——一个用例把全局
    状态带坏，后面的用例看起来像"功能坏了"，实际是**测试间污染**。

    这个保护此前只存在于少数文件（``test_process_event_semantics`` /
    ``test_orchestration`` / ``test_effect_journal``）自己装，别的文件一旦碰写步骤就会
    踩到。放在 conftest 里对所有用例生效：默认给一个干净的内存实现，用例想测
    "日志不可用"仍可自己 monkeypatch（用例级 fixture 在 autouse 之后执行）。
    """
    try:
        from app.agents.orchestration.runtime import effects
        from app.repositories.effect_journal_repository import InMemoryEffectJournalRepository
    except Exception:  # noqa: BLE001 - 导入失败不该让整个会话挂掉
        yield
        return
    original = getattr(effects, "_repository", None)
    effects._repository = InMemoryEffectJournalRepository()
    try:
        yield
    finally:
        effects._repository = original
