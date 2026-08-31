from app.agents.orchestration.policy import route_intent_patterns
from app.agents.orchestration.task_routing import RouteChannel, route_atomic_instruction


def test_deployment_patterns_cover_common_file_batch_operations():
    route_intent_patterns.load_route_intent_patterns.cache_clear()
    assert route_atomic_instruction("把客户名单.csv 按城市分组").channel == RouteChannel.DETERMINISTIC_SCRIPT
    assert route_atomic_instruction("从访问日志.csv 筛选状态码五百以上记录").channel == RouteChannel.DETERMINISTIC_SCRIPT


def test_deployment_patterns_cover_retrieval_synonyms():
    assert route_atomic_instruction("检索团队手册里的代码评审要求").channel == RouteChannel.RAG
    assert route_atomic_instruction("从产品文档定位试用期规则").channel == RouteChannel.RAG


def test_deployment_patterns_cover_coordination_synonyms():
    assert route_atomic_instruction("协调模拟团队处理跨系统数据不一致").channel == RouteChannel.AGENT
    assert route_atomic_instruction("分析测试接口失败并安排排障顺序").channel == RouteChannel.AGENT

