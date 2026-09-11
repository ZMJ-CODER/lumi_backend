"""验收 1/3 的自动化检查：工作区正文经 server sanitize 后必须存活。

运行：``python -m tests.acceptance.check_sanitize``
"""

from __future__ import annotations

import sys

from tests.acceptance.workspace_navigator_sanitize import collect_report


def main() -> int:
    report = collect_report()
    failures: list[str] = []
    for case in report["cases"]:
        label = case["path"]
        if case["status"] != "ok":
            failures.append(f"{label}: 状态不是 ok（{case['status']}）")
        if not case["path_preserved"]:
            failures.append(f"{label}: data.path 被清洗掉了")
        if case["sections"] < 1:
            failures.append(f"{label}: sections 为空")
        if not case["content_alive"]:
            failures.append(f"{label}: 正文被清空")
        if case["secret_leaked"]:
            failures.append(f"{label}: 敏感原文泄漏")
        print(
            f"[{'OK ' if not failures or failures[-1].split(':')[0] != label else 'FAIL'}] "
            f"{label} path_preserved={case['path_preserved']} sections={case['sections']} "
            f"chars={case['char_count']} redacted={case['redacted']} leaked={case['secret_leaked']}"
        )
    if failures:
        print("\n失败项：")
        for item in failures:
            print(" -", item)
        return 1
    print("\n全部通过：正文存活、path 保留、敏感内容按预期脱敏。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
