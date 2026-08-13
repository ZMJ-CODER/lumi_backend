"""Temporal 编排运行时 —— Workflow / Activities / Worker / 客户端集成.

职责边界：
  - app/agents/temporal_workflows.py  确定性 Workflow（独立轻模块，规避沙箱导入限制）
  - activities.py 副作用 Activity：节点执行（worker + 质检 + React 重试）、清理
  - client.py     编排器 <-> Temporal 桥接：启动/查询/信号 + BYOK key 临时桥接
  - worker.py     Worker 进程入口（独立进程运行，`python -m` 启动）
"""
