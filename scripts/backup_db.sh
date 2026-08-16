#!/usr/bin/env bash
# 数据库备份：通过 postgres 容器 pg_dump 导出 lumi_db，保留最近 14 份。
# 用法（宿主机）：
#   bash scripts/backup_db.sh
# 配合 cron 定期执行（示例，每天凌晨 3 点）：
#   0 3 * * * cd /path/to/lumi_backend && bash scripts/backup_db.sh >> logs/backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."

# 可同时备份本地数据目录（上传文件 / office 会话 / 模型缓存清单）
# 用法：BACKUP_DATA=1 bash scripts/backup_db.sh
BACKUP_DIR="${BACKUP_DIR:-backups}"
DATE="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

# 1) 数据库
if docker compose ps postgres >/dev/null 2>&1; then
  docker compose exec -T postgres pg_dump -U postgres -d lumi_db -F c \
    > "$BACKUP_DIR/lumi_db_$DATE.dump"
else
  # 本机 postgres 直接跑
  pg_dump -U postgres -d lumi_db -F c > "$BACKUP_DIR/lumi_db_$DATE.dump"
fi
echo "数据库备份完成: $BACKUP_DIR/lumi_db_$DATE.dump"

# 2) 数据目录（可选）
if [ "${BACKUP_DATA:-0}" = "1" ]; then
  tar -czf "$BACKUP_DIR/data_$DATE.tar.gz" data/ 2>/dev/null || true
  echo "数据目录备份完成: $BACKUP_DIR/data_$DATE.tar.gz"
fi

# 3) 保留最近 14 份
ls -1t "$BACKUP_DIR"/lumi_db_*.dump 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "清理完成，剩余 $(ls -1 "$BACKUP_DIR"/lumi_db_*.dump 2>/dev/null | wc -l) 份"

BACKUP_DIR="backups"
mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
FILE="$BACKUP_DIR/lumi_${STAMP}.sql"

docker compose exec -T postgres pg_dump -U postgres -d lumi_db > "$FILE"

# 只保留最近 14 份
ls -1t "$BACKUP_DIR"/lumi_*.sql 2>/dev/null | tail -n +15 | xargs -r rm -f

echo "backup -> $FILE ($(du -h "$FILE" | cut -f1))"
