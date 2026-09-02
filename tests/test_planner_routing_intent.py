"""通用自然语言路由回归测试。"""

import asyncio

from app.agents.orchestration.planner import RulePlanner
from app.agents.orchestration.routing_intent import classify_route_with_llm, infer_route_intent
from app.agents.orchestration.task_routing import RouteChannel, route_atomic_instruction


def _plan(
    request: str,
    office_docs: list[dict] | None = None,
    prior_summaries: str = "",
):
    return asyncio.run(RulePlanner().plan(
        "u1", request, office_docs=office_docs, prior_summaries=prior_summaries,
    ))


def test_explicit_public_web_query_routes_to_web_research():
    tree = _plan("请联网搜索今天上海天气，并给我网页来源")
    assert [node.agent for node in tree.nodes] == ["web_research"]
    assert tree.nodes[0].params["query"] == "请联网搜索今天上海天气，并给我网页来源"


def test_multi_action_routes_to_react_without_domain_keywords():
    tree = _plan(
        "先读取这份材料，再判断关键结论，最后打开应用",
        [{"doc_id": "a", "filename": "材料.pdf"}],
    )
    assert [node.agent for node in tree.nodes] == ["react_step"]


def test_side_effect_is_not_hidden_as_retrieval():
    tree = _plan("打开计算器然后算 2+2")
    assert [node.agent for node in tree.nodes] == ["react_step"]


def test_ambiguous_file_request_asks_for_clarification():
    tree = _plan("帮我整理一下那个文件")
    assert tree.nodes == []
    assert tree.clarification


def test_unknown_request_does_not_default_to_retrieval():
    tree = _plan("请帮我处理这个")
    assert tree.nodes == []
    assert tree.clarification


def test_clear_question_still_routes_to_retrieval():
    tree = _plan("唐朝长安城有多少人")
    assert [node.agent for node in tree.nodes] == ["retrieval"]


def test_multiple_adjacent_filenames_compile_to_static_dag():
    docs = [
        {"doc_id": "a", "filename": "合同A.pdf"},
        {"doc_id": "b", "filename": "合同B.pdf"},
    ]
    tree = _plan("对比合同A.pdf和合同B.pdf", docs)
    assert [node.agent for node in tree.nodes] == ["retrieval", "direct_llm"]
    assert tree.nodes[1].depends_on == [tree.nodes[0].id]
    assert tree.nodes[0].params["office_docs"] == [
        {"doc_id": "a", "filename": "合同A.pdf"},
        {"doc_id": "b", "filename": "合同B.pdf"},
    ]


def test_route_intent_is_domain_neutral():
    intent = infer_route_intent("查询最新的公开资料")
    assert intent.requires_network is True
    assert "weather" not in intent.actions
    assert "contract" not in intent.objects


def test_colloquial_private_time_reference_does_not_route_to_web():
    tree = _plan("你帮我看下我今天的任务现在什么情况")
    assert [node.agent for node in tree.nodes] != ["web_research"]


def test_colloquial_compare_uses_uploaded_documents_without_react():
    docs = [
        {"doc_id": "a", "filename": "甲方方案.pdf"},
        {"doc_id": "b", "filename": "乙方方案.pdf"},
    ]
    tree = _plan("把这两个文件放一起比一下", docs)
    assert [node.agent for node in tree.nodes] == ["retrieval", "direct_llm"]


def test_colloquial_read_single_document_routes_to_retrieval():
    tree = _plan("你帮我看下这个文件里说了啥", [{"doc_id": "a", "filename": "材料.pdf"}])
    assert [node.agent for node in tree.nodes] == ["retrieval"]


def test_colloquial_chained_request_compiles_static_dag():
    tree = _plan(
        "先帮我把材料看一遍，然后给我弄成一份新的",
        [{"doc_id": "a", "filename": "材料.pdf"}],
    )
    assert [node.agent for node in tree.nodes] == ["retrieval", "direct_llm"]


def test_feedback_with_implicit_repair_request_routes_to_react():
    tree = _plan(
        "刚刚跑出来的结果不太对，金额都变成字符串了。你先看看输入和输出的差别，能自动修就修。",
        [{"doc_id": "a", "filename": "输入.csv"}, {"doc_id": "b", "filename": "输出.csv"}],
    )
    assert [node.agent for node in tree.nodes] == ["react_step"]


def test_conditional_branch_routes_to_dynamic_execution_instead_of_retrieval():
    tree = _plan(
        "先读取两个附件，把差异摆出来；如果金额超过十万就通知财务，否则只给我一段结论。",
        [
            {"doc_id": "a", "filename": "one.xlsx"},
            {"doc_id": "b", "filename": "two.xlsx"},
        ],
    )
    assert [node.agent for node in tree.nodes] == ["react_step"]


def test_english_feedback_and_repair_routes_to_dynamic_execution():
    tree = _plan(
        "The upload finished but the output is garbled. Figure out what went wrong, "
        "try a low-risk fix, and tell me what you changed.",
        [{"doc_id": "a", "filename": "input.csv"}],
    )
    assert [node.agent for node in tree.nodes] == ["react_step"]


def test_colloquial_diagnostic_question_routes_to_react():
    tree = _plan("帮我看看这个结果对不对，哪里不对就先标出来。")
    assert [node.agent for node in tree.nodes] == ["react_step"]


def test_long_colloquial_request_keeps_file_delivery_contract():
    tree = _plan(
        "帮我把这份月度数据看一下，先别改原文件，按部门统计缺失值和异常值，最后生成新的 Excel，文件名叫 review_result.xlsx。",
        [{"doc_id": "a", "filename": "月度数据.xlsx"}],
    )
    assert [node.agent for node in tree.nodes] == ["office_script"]
    assert tree.nodes[0].params["output_contract"]["expected_output_names"] == ["review_result.xlsx"]


def test_history_feedback_routes_to_dynamic_recall():
    tree = _plan("你还记得我们前面说的那个方案吗？把当时最后确认的版本找出来发给我。")
    assert [node.agent for node in tree.nodes] == ["react_step"]


def test_feedback_without_an_action_still_asks_what_to_do():
    tree = _plan("已完成：同步成功，处理了 238 条记录，耗时 18 秒。")
    assert tree.nodes == []
    assert tree.clarification


def test_subjective_conversation_does_not_use_knowledge_retrieval():
    tree = _plan("我最近总觉得做什么都提不起劲，你怎么看？")
    assert [node.agent for node in tree.nodes] == ["direct_llm"]


def test_route_keeps_action_order_for_implicit_follow_up():
    intent = infer_route_intent("也发给李四", prior_summaries="上一轮生成了报告并发给张三")
    assert [step.action for step in intent.action_steps] == ["lookup_history", "send"]
    assert intent.requires_side_effect is True
    assert intent.confidence_detail is not None


def test_route_intent_recognizes_mixed_language_actions_and_order():
    intent = infer_route_intent(
        "先提炼 release note 的重点，然后 translate 成英文，最后做一个 Markdown table"
    )

    assert {"analyze", "transform", "create"}.issubset(set(intent.actions))
    assert intent.has_multiple_actions
    assert [step.action for step in intent.action_steps][:3] == ["analyze", "transform", "create"]


def test_missing_context_for_implicit_follow_up_clarifies():
    tree = _plan("也发给李四")
    assert tree.nodes == []
    assert tree.clarification


def test_static_plan_exposes_typed_step_contracts():
    tree = _plan("看看这份合同有没有坑再告诉我", [{"doc_id": "a", "filename": "合同.pdf"}])
    assert [node.agent for node in tree.nodes] == ["retrieval", "direct_llm"]
    step = tree.nodes[1].metadata["plan_step"]
    assert step["output_contract"]["artifact_type"] == "text"
    assert step["input_contract"][0]["source_step"] == tree.nodes[0].id


def test_exploration_remains_dynamic_even_when_query_words_are_present():
    tree = _plan("帮我探索一下这个数据集有什么规律")
    assert [node.agent for node in tree.nodes] == ["react_step"]


def test_risky_route_sets_approval_gate():
    tree = _plan("打开计算器然后算 2+2")
    assert [node.agent for node in tree.nodes] == ["react_step"]
    assert tree.nodes[0].approval is True
    assert tree.nodes[0].metadata["routing"]["risk_level"] == "system_command"


def test_long_tail_classifier_fills_missing_action_without_bypassing_compiler(monkeypatch):
    calls = []

    async def fake_classifier(prompt, *, user_id, api_key, max_tokens):
        calls.append((user_id, api_key, max_tokens))
        return {
            "actions": ["transform"],
            "objects": ["document"],
            "requires_dynamic": False,
            "needs_clarification": False,
            "confidence": 0.86,
            "reason": "用户希望将文件加工成新的交付格式",
        }

    monkeypatch.setattr("app.agents.langchain.planning.invoke_json_object", fake_classifier)
    tree = asyncio.run(RulePlanner().plan(
        "u1",
        "麻烦把这玩意儿弄成客户能直接看的版本",
        llm_api_key="test-key",
        office_docs=[{"doc_id": "a", "filename": "原始数据.xlsx"}],
    ))
    assert calls == [("u1", "test-key", 350)]
    assert [node.agent for node in tree.nodes] == ["office_script"]


def test_llm_confidence_is_retained_as_hint_without_raising_deterministic_confidence(monkeypatch):
    async def fake_classifier(prompt, *, user_id, api_key, max_tokens):
        return {
            "actions": ["query"],
            "objects": ["data"],
            "requires_dynamic": False,
            "needs_clarification": False,
            "confidence_hint": 0.99,
        }

    monkeypatch.setattr("app.agents.langchain.planning.invoke_json_object", fake_classifier)
    base = infer_route_intent("这段话比较长，帮我从里面找出关键数据并解释一下变化原因")
    candidate = asyncio.run(classify_route_with_llm(
        "这段话比较长，帮我从里面找出关键数据并解释一下变化原因",
        user_id="u1", api_key="test-key",
    ))
    assert candidate["confidence_hint"] == 0.99
    from app.agents.orchestration.routing_intent import merge_llm_route_intent
    merged = merge_llm_route_intent(base, candidate, "这段话比较长，帮我从里面找出关键数据并解释一下变化原因")
    assert merged.classifier_confidence_hint == 0.99
    assert merged.confidence == base.confidence


def test_long_tail_classifier_cannot_send_without_a_target(monkeypatch):
    async def fake_classifier(prompt, *, user_id, api_key, max_tokens):
        return {
            "actions": ["send"],
            "objects": [],
            "requires_dynamic": False,
            "needs_clarification": False,
            "confidence": 0.98,
        }

    monkeypatch.setattr("app.agents.langchain.planning.invoke_json_object", fake_classifier)
    tree = asyncio.run(RulePlanner().plan(
        "u1",
        "让它发出去",
        llm_api_key="test-key",
    ))
    assert tree.nodes == []
    assert tree.clarification


def test_long_colloquial_review_request_is_not_misread_as_system_execution():
    tree = _plan(
        "我不太确定这个方案是否合理，你先从风险、成本和后续执行难度几个方面帮我过一遍",
        [{"doc_id": "a", "filename": "方案.pdf"}],
    )
    assert [node.agent for node in tree.nodes] == ["retrieval", "direct_llm"]


def test_long_feedback_and_delivery_request_keeps_approval_gate():
    tree = _plan(
        "我刚才把销售数据跑了一遍，结果里面有几列格式不对，你先看看是哪一步出了问题，能修的话不要覆盖原文件，修好后另存一份给我",
        [{"doc_id": "a", "filename": "销售数据.xlsx"}],
    )
    assert [node.agent for node in tree.nodes] == ["react_step"]
    assert tree.nodes[0].approval is True


def test_long_transform_then_external_delivery_is_dynamic_and_approved():
    tree = _plan(
        "把刚刚那份报告改成英文，保留原来的结构，然后发给李四",
        [{"doc_id": "a", "filename": "报告.docx"}],
    )
    assert [node.agent for node in tree.nodes] == ["react_step"]
    assert tree.nodes[0].approval is True


def test_negated_external_action_is_not_routed_as_send():
    tree = _plan(
        "你先帮我把这份会议纪要过一遍，挑出需要跟进的地方，整理成一份清单，但暂时不要直接发出去",
        [{"doc_id": "a", "filename": "会议纪要.docx"}],
    )
    assert [node.agent for node in tree.nodes] == ["office_script"]
    assert tree.nodes[0].approval is False


def test_long_subjective_request_stays_direct_conversation():
    tree = _plan(
        "我最近在考虑要不要换工作，心里其实有点舍不得现在的团队，但又担心继续留下来会错过机会，你听完之后你觉得呢",
    )
    assert [node.agent for node in tree.nodes] == ["direct_llm"]


def test_long_history_recall_with_explicit_delivery_is_approved():
    tree = _plan(
        "你帮我回想一下我们之前反复讨论过的那个方案，把最后确认的版本找出来，整理好之后再发给我",
        prior_summaries="之前讨论了方案，最后确认版本已经生成并保存在任务结果中。",
    )
    assert [node.agent for node in tree.nodes] == ["react_step"]
    assert tree.nodes[0].approval is True


def test_long_unknown_request_still_clarifies():
    tree = _plan(
        "我手头有点乱，刚才看到一些东西但现在也说不清楚具体是什么，你先帮我弄一下吧",
    )
    assert tree.nodes == []
    assert tree.clarification


def test_route_classifier_prefers_request_key(monkeypatch):
    seen = []

    async def fake_classifier(prompt, *, user_id, api_key, max_tokens):
        seen.append(api_key)
        return {"actions": ["query"], "objects": ["data"], "confidence": 0.9}

    monkeypatch.setattr("app.agents.langchain.planning.invoke_json_object", fake_classifier)
    asyncio.run(classify_route_with_llm(
        "帮我看看这个数据到底是怎么回事，顺便告诉我重点",
        user_id="u1",
        api_key="user-key",
    ))
    assert seen == ["user-key"]


def test_route_classifier_uses_configured_key_when_request_key_missing(monkeypatch):
    seen = []

    async def fake_config(scene, user_id=None):
        return {"api_key": "preset-key"}

    async def fake_classifier(prompt, *, user_id, api_key, max_tokens):
        seen.append(api_key)
        return {"actions": ["query"], "objects": ["data"], "confidence": 0.9}

    monkeypatch.setattr("app.core.llm_config.get_llm_config", fake_config)
    monkeypatch.setattr("app.agents.langchain.planning.invoke_json_object", fake_classifier)
    asyncio.run(classify_route_with_llm(
        "这段话比较长，帮我从里面找出关键数据并解释一下变化原因",
        user_id="u1",
        api_key=None,
    ))
    assert seen == ["preset-key"]


def test_english_compound_request_uses_preset_key_fallback(monkeypatch):
    seen = []

    async def fake_config(scene, user_id=None):
        return {"api_key": "preset-key"}

    async def fake_classifier(prompt, *, user_id, api_key, max_tokens):
        seen.append(api_key)
        return {
            "actions": ["read", "analyze", "send"],
            "objects": ["document", "message"],
            "requires_dynamic": False,
            "needs_clarification": False,
            "confidence": 0.91,
        }

    monkeypatch.setattr("app.core.llm_config.get_llm_config", fake_config)
    monkeypatch.setattr("app.agents.langchain.planning.invoke_json_object", fake_classifier)
    tree = asyncio.run(RulePlanner().plan(
        "u1",
        "Please summarize this document, highlight the risks, and send it to Alice.",
        office_docs=[{"doc_id": "a", "filename": "report.pdf"}],
    ))
    assert seen == ["preset-key"]
    assert [node.agent for node in tree.nodes] == ["react_step"]
    assert tree.nodes[0].approval is True


def test_multi_document_fact_request_uses_fixed_targeting_before_answer():
    docs = [
        {"doc_id": f"doc-{index}", "filename": f"供应商资料-{index}.pdf"}
        for index in range(1, 8)
    ]
    tree = _plan(
        "这几个文件我一时分不清。帮我找出写了付款期限和违约条款的那一份，"
        "读完确认条件满足后再继续下一步，但别把每份文件都拆成一个任务。",
        docs,
    )

    assert [node.agent for node in tree.nodes] == ["document_targeting", "direct_llm"]
    target, answer = tree.nodes
    assert target.metadata["route_channel"] == "rag"
    assert len(target.params["office_docs"]) == 7
    assert target.metadata["document_discovery_required"] is True
    assert answer.depends_on == [target.id]


def test_multi_document_fact_unit_routes_to_rag_before_agent():
    decision = route_atomic_instruction(
        "帮我查一下哪份附件写了交付日期",
        has_authorized_documents=True,
        office_document_count=7,
    )
    assert decision.channel == RouteChannel.RAG


def test_ambiguous_multi_document_targeting_replans_to_bounded_react():
    docs = [
        {"doc_id": "a", "filename": "合同一.pdf"},
        {"doc_id": "b", "filename": "合同二.pdf"},
    ]
    tree = asyncio.run(RulePlanner().plan(
        "u1",
        "哪份附件写了付款期限？",
        office_docs=docs,
        prior_summaries='{"error_code":"DOCUMENT_SELECTION_AMBIGUOUS"}',
    ))
    assert [node.agent for node in tree.nodes] == ["react_step"]
    assert tree.nodes[0].params["max_rounds"] == 4
    assert tree.nodes[0].metadata["route_channel"] == "agent"
