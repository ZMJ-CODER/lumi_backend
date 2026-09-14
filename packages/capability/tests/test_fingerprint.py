"""参数指纹的纯函数测试：审批绑定的正确性全靠它。"""

from __future__ import annotations

from lumi_capability import fingerprint as f


def test_same_call_same_fingerprint_regardless_of_key_order():
    """同一语义调用必须得到同一指纹，否则审批永远对不上。"""
    first = f.capability_fingerprint("workspace.write", {"a": 1, "b": 2})
    second = f.capability_fingerprint("workspace.write", {"b": 2, "a": 1})
    assert first == second


def test_different_arguments_give_different_fingerprints():
    first = f.capability_fingerprint("workspace.write", {"path": "/a"})
    second = f.capability_fingerprint("workspace.write", {"path": "/b"})
    assert first != second


def test_reserved_runtime_keys_are_ignored():
    """``_lumi_*`` 是执行期透传键（追踪/重试标记），不参与指纹。"""
    plain = f.capability_fingerprint("workspace.write", {"path": "/a"})
    with_reserved = f.capability_fingerprint(
        "workspace.write", {"path": "/a", "_lumi_trace": "t-1", "_lumi_attempt": 3}
    )
    assert plain == with_reserved


def test_capability_version_does_not_participate():
    """Provider 升版不该让已批准的调用失效：只有**基名**进指纹。"""
    assert f.capability_fingerprint("workspace.write@2", {"a": 1}) == f.capability_fingerprint(
        "workspace.write", {"a": 1}
    )


def test_scope_participates():
    """作用域不同 = 调用不同（跨用户/跨工作区的审批不能互相顶替）。"""
    first = f.capability_fingerprint("workspace.write", {"a": 1}, scope={"user": "u1"})
    second = f.capability_fingerprint("workspace.write", {"a": 1}, scope={"user": "u2"})
    assert first != second


def test_empty_inputs_are_stable():
    assert f.capability_fingerprint("") == f.capability_fingerprint("", {})
    assert len(f.capability_fingerprint("workspace.write")) == 64


def test_unserializable_values_do_not_raise():
    """参数里出现非 JSON 类型时按 ``default=str`` 处理，不能因为审计而抛异常。"""

    class Opaque:
        def __str__(self) -> str:
            return "opaque"

    assert len(f.capability_fingerprint("workspace.write", {"obj": Opaque()})) == 64
