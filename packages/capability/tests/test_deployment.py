"""部署位置判定的纯函数测试。"""

from __future__ import annotations

from dataclasses import dataclass


from lumi_capability import deployment as d


@dataclass
class _Descriptor:
    allows: bool

    def allows_deployment(self, deployment: str) -> bool:
        return self.allows


def test_all_descriptors_must_allow():
    assert d.descriptor_allows_deployment([_Descriptor(True), _Descriptor(True)], "server") is True
    assert d.descriptor_allows_deployment([_Descriptor(True), _Descriptor(False)], "server") is False


def test_one_denial_is_enough_to_reject():
    """注册项是"这组能力的整体承诺"：部分可执行会让 Broker 得到矛盾结论。"""
    descriptors = [_Descriptor(False)] + [_Descriptor(True)] * 5
    assert d.descriptor_allows_deployment(descriptors, "client") is False


def test_empty_registration_is_allowed():
    assert d.descriptor_allows_deployment([], "server") is True
