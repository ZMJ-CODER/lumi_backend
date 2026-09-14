"""角色提示词服务测试：frontmatter 解析 + 内置角色（无需数据库）."""

from app.services.prompts import _get_builtin, _list_builtin, _parse_frontmatter


def test_parse_frontmatter():
    meta, content = _parse_frontmatter("---\nid: x\nname: 测试角色\n---\n角色正文内容")
    assert meta["id"] == "x"
    assert meta["name"] == "测试角色"
    assert content == "角色正文内容"


def test_parse_no_frontmatter():
    meta, content = _parse_frontmatter("没有 frontmatter 的纯文本")
    assert meta == {}
    assert content == "没有 frontmatter 的纯文本"


def test_builtin_list_contains_default():
    items = _list_builtin()
    ids = {i["prompt_id"] for i in items}
    assert "lumi_default" in ids
    assert "lumi_friend" in ids
    assert all(i["is_custom"] is False for i in items)


def test_get_builtin_content():
    p = _get_builtin("lumi_friend")
    assert p is not None
    assert "知心朋友" in p["content"]
    assert _get_builtin("not_exist") is None
