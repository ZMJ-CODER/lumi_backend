from app.services.rag.scope import RetrievalScope, route_chat_retrieval_scope


def test_document_reference_wins_over_memory_reference():
    assert route_chat_retrieval_scope("按我上次的偏好总结这份文档") == RetrievalScope.PERSONAL_KNOWLEDGE


def test_history_reference_routes_only_to_memory():
    assert route_chat_retrieval_scope("按我上次的偏好继续") == RetrievalScope.MEMORY


def test_normal_question_does_not_search_any_personal_corpus():
    assert route_chat_retrieval_scope("解释一下光的折射") == RetrievalScope.NONE


def test_explicit_retrieval_query_routes_to_knowledge():
    assert route_chat_retrieval_scope("帮我总结", retrieval_query="合同付款条款") == RetrievalScope.PERSONAL_KNOWLEDGE


def test_first_person_uploaded_material_reference_uses_knowledge_scope():
    assert route_chat_retrieval_scope("请根据我上传的资料解释这个概念") == RetrievalScope.PERSONAL_KNOWLEDGE
