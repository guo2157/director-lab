#!/bin/bash
# worker.log 日志轮转（无 root 权限方案）
# 保留 3 个历史副本，每个最大 5MB
LOG=/home/ubuntu/director-lab/backend/worker.log
MAX_BYTES=5242880  # 5MB
KEEP=3

if [ ! -f "$LOG" ]; then
  exit 0
fi

SIZE=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
if [ "$SIZE" -lt "$MAX_BYTES" ]; then
  exit 0
fi

# 轮转：删除最旧的，逐级重命名
for i in $(seq $((KEEP - 1)) -1 1); do
  [ -f "${LOG}.${i}" ] && mv "${LOG}.${i}" "${LOG}.$((i + 1))"
done
mv "$LOG" "${LOG}.1"
touch "$LOG"
