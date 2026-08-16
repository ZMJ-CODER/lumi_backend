#!/usr/bin/env bash
# 数据库恢复：从 backups/lumi_db_*.dump 恢复 lumi_db（危险操作，会覆盖当前数据）
#
# 用法：
#   bash scripts/restore_db.sh backups/lumi_db_20260815_120000.dump

set -euo pipefail

cd "$(dirname "$0")/.."
DUMP="${1:-}"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
  echo "用法: bash scripts/restore_db.sh <备份文件.dump>"
  exit 1
fi

read -r -p "恢复会覆盖当前 lumi_db 全部数据，确认？[y/N] " ans
if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
  echo "已取消"
  exit 0
fi

if docker compose ps postgres >/dev/null 2>&1; then
  # 先清空再恢复（custom 格式用 pg_restore --clean）
  docker compose exec -T postgres pg_restore -U postgres -d lumi_db --clean --if-exists \
    < "$DUMP"
else
  pg_restore -U postgres -d lumi_db --clean --if-exists < "$DUMP"
fi
echo "恢复完成: $DUMP"
