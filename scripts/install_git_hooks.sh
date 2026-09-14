#!/usr/bin/env bash
# 安装仓库自带的 git 钩子（目前只有一个：高危调用门禁）。
#
#     bash scripts/install_git_hooks.sh
#
# 幂等：重复执行只会覆盖成最新版本；已有的无关钩子会被保留（只写 pre-commit）。
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

HOOK_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOK_DIR"

cp scripts/pre-commit.sample "$HOOK_DIR/pre-commit"
chmod +x "$HOOK_DIR/pre-commit"

echo "已安装 pre-commit 钩子：$HOOK_DIR/pre-commit"
echo "手动验证：bash $HOOK_DIR/pre-commit"
