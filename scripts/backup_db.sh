#!/usr/bin/env bash
# 数据库备份：通过 postgres 容器 pg_dump 导出 lumi_db，保留最近 14 份。
# 用法（宿主机）：
#   bash scripts/backup_db.sh
# 配合 cron 定期执行（示例，每天凌晨 3 点）：
#   0 3 * * * cd /path/to/lumi_backend && bash scripts/backup_db.sh >> logs/backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="backups"
mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
FILE="$BACKUP_DIR/lumi_${STAMP}.sql"

docker compose exec -T postgres pg_dump -U postgres -d lumi_db > "$FILE"

# 只保留最近 14 份
ls -1t "$BACKUP_DIR"/lumi_*.sql 2>/dev/null | tail -n +15 | xargs -r rm -f

echo "backup -> $FILE ($(du -h "$FILE" | cut -f1))"
