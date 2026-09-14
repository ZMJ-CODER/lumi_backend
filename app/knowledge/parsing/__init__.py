"""Knowledge 域 · 解析层：文档 → 文本 → 分块 → 分类/质检。

============================  ================================================
模块                          作用
============================  ================================================
``document_parser``           纯文本解析与分块（``parse_document`` / ``parse_file``）
``docling_parser``            Office/PDF 结构化解析（docling 后端）
``chunker``                   分块（``chunk_document``）
``cleaner``                   内容清洗与质量评估（``clean_document`` / ``assess_document``）
``classifier``                文档分类（``classify_document``）
============================  ================================================

依赖方向：本层只依赖本域（``chunker`` → ``document_parser``，``classifier`` → ``embedding``，
``document_parser`` ↔ ``docling_parser``），不依赖检索层。
"""
