"""Pure routing classifiers shared by legacy routing and policy features."""

from __future__ import annotations

import re


FILE_OPERATION = re.compile(
    r"(?iu)(?:转换|转为|转成|导出|另存为|保存为|批量处理|运行脚本|执行脚本)"
    r".{0,36}(?:文件|附件|文档|表格|\.csv\b|\.tsv\b|\.xlsx\b|\.docx\b|\.pptx\b|\.pdf\b|\.txt\b)"
)
RAG_OPERATION = re.compile(
    r"(?iu)(?:知识库|资料库|检索|查找|查询).{0,28}(?:资料|文档|信息|内容|记录|知识库)|"
    r"(?:根据|从).{0,20}(?:知识库|资料|文档).{0,24}(?:回答|说明|找出|查询)"
)
EXTERNAL_OPERATION = re.compile(
    r"(?iu)(?:打开|启动|发送|删除|修改|编辑|创建|添加|取消|安排|设置)"
    r".{0,32}(?:应用|软件|浏览器|网页|邮件|日程|日历|待办|数据库|系统|文件|文档)|"
    r"(?:(?:更新|同步|提交|推送|发布|写入).{0,32}(?:系统|数据库|平台|服务))"
)
MULTI_OPERATION = re.compile(
    r"(?iu)(?:先.+(?:再|然后)|(?:读取|分析|提取).{0,80}(?:并|再|然后).{0,80}"
    r"(?:核对|检查|发送|写入|修改|导出)|第\s*[一二三四五六七八九十0-9]+\s*步)"
)
STATEFUL_REASONING = re.compile(
    r"(?iu)(?:核对|校验|验证|审查|合规|审批|比对|排查|诊断).{0,32}"
    r"(?:系统|规则|要求|标准|条款|记录|数据|状态)|"
    r"(?:核对|校验|验证|审查|合规|审批|比对|排查|诊断)"
)
FACTUAL_DOCUMENT_QUESTION = re.compile(
    r"(?iu)(?:哪\s*(?:(?:一|几)?(?:个|份))|哪个|哪份|是否|有没有|包含|提到|写了什么|什么是|多少|几页|"
    r"条款|条件|金额|日期|负责人|找出|定位|查一下|看看|"
    r"\bwhich\s+(?:file|document|one)\b|\bdoes\b.{0,60}\b(?:contain|mention|include|state)\b|"
    r"\b(?:payment\s+terms?|amount|date|deadline|clause|owner)\b).{0,80}"
)
