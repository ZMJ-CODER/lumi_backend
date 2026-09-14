"""预检：任务级入口检查 + 能力级预检。

`task_preflight` 管"这个任务
该不该接"；`capability_preflight` / `capability_preflight_service` 管"这次执行需要的
能力/Provider/工具是否就绪"，并负责把**底层事实**映射成对外的冻结状态。
"""
