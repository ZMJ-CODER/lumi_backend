"""Celery 异步任务定义.

任务类型:
  - process_document:   文档分块 → 向量化 → 入库
  - extract_memories:   从对话中提取长期记忆
  - rebuild_index:      重建向量索引
  - cleanup_vectors:    清理冗余向量
  - delete_user_data:   物理清理用户数据
"""

from celery_app import celery_app


@celery_app.task(bind=True, max_retries=3)
def process_document(self, document_id: str, file_path: str, user_id: str, space_id: str):
    """处理上传的文档：分块 → 嵌入 → 存入 pgvector."""
    # TODO:
    # 1. 读取文件内容
    # 2. LangChain TextSplitter 分块
    # 3. 调用嵌入模型生成向量
    # 4. 批量写入 document_chunks 表
    # 5. 更新 documents.status = 'ready'
    pass


@celery_app.task(bind=True, max_retries=3)
def extract_memories(self, user_id: str, conversation_id: str, user_msg: str, assistant_msg: str):
    """从对话中提取长期记忆关键事实."""
    # TODO:
    # 1. 调用 LLM 提取关键事实
    # 2. 去重（与已有记忆比较）
    # 3. 写入 memories 表
    # 4. 清除 Redis 记忆缓存
    pass


@celery_app.task(bind=True)
def rebuild_index(self, space_id: str | None = None):
    """重建向量索引."""
    # TODO: DROP INDEX → CREATE INDEX ON document_chunks USING ivfflat
    pass


@celery_app.task(bind=True)
def cleanup_vectors(self):
    """清理已删除文档的冗余向量."""
    # TODO: DELETE FROM document_chunks WHERE document_id NOT IN (SELECT id FROM documents)
    pass


@celery_app.task(bind=True)
def delete_user_data(self, user_id: str):
    """物理清理用户所有数据（24h 延迟执行）."""
    # TODO:
    # 1. DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id = ?)
    # 2. DELETE FROM conversations WHERE user_id = ?
    # 3. DELETE FROM memories WHERE user_id = ?
    # 4. DELETE FROM document_chunks WHERE user_id = ?
    # 5. DELETE FROM documents WHERE user_id = ?
    # 6. DELETE FROM knowledge_spaces WHERE user_id = ? AND is_public = false
    # 7. DELETE FROM control_logs WHERE user_id = ?
    # 8. DELETE FROM users WHERE id = ?
    pass
