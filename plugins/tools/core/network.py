"""联网工具已统一到 ``plugins.tools.network.web_search``。

该文件不再声明可被模型发现的 Tool，避免旧的 WebSearch/WebFetch 与规范名称
重复注册。保留模块入口是为了让历史导入在升级期间得到明确的空实现。
"""
