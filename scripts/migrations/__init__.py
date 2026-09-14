"""一次性数据迁移脚本（方案《结构重构方案 v2》P1 从 ``scripts/`` 归拢到这里）。

这些脚本**不参与运行时**：它们直接连数据库做结构/数据搬迁，跑完就结束。与
``alembic/`` 的分工是——

* ``alembic/``：可重放的 schema 迁移（部署路径，``alembic upgrade head``）；
* 这里：**一次性、幂等、需要人工确认**的数据迁移（老库补列、换嵌入模型、
  重嵌入向量、轮换记忆主密钥）。

执行方式（读 ``DATABASE_URL``，容器内直接跑）::

    docker compose exec api python scripts/migrations/migrate_projects.py

注意：本目录比原来的 ``scripts/`` 深了一层，脚本里的 ``sys.path`` 基准已同步
（``reembed_vectors.py`` 用 ``parents[2]`` 指回仓库根）。
"""
