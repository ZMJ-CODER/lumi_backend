# 文档编辑 Skill

编辑前必须 Read 当前文件。将自然语言要求转换为最小补丁，写入 workspace staging，并返回变更预览；禁止直接覆盖用户原文件。用户确认后 commit，用户拒绝则 rollback。
