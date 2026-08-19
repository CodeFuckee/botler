#!/usr/bin/env bash
# 启动 E2E 前后端服务并运行 Playwright 测试（issue #212）
#
# 架构：真实浏览器 → vite preview（前端构建产物，SPA 路由）→ /api 代理
#       → uvicorn（真实 FastAPI 后端，独立端口避开生产 8000）。
# 稳定性：GitLab 依赖接口由浏览器级 mock 兜底（见 e2e/support/mock-api.js），
# 后端不依赖真实 GitLab；种子数据库保证任务/灵感数据确定。
#
# 用法：
#   bash frontend/e2e/scripts/start-servers.sh            # 跑全部 E2E
#   bash frontend/e2e/scripts/start-servers.sh tests/overview.spec.js
# 环境变量：
#   E2E_BACKEND_PORT   uvicorn 端口（默认 8011，避开生产 8000）
#   E2E_FRONTEND_PORT  vite preview 端口（默认 4173）
#   E2E_RUN_DIR        运行目录（默认 mktemp 临时目录，退出自动清理）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
FRONTEND="$ROOT/frontend"
BACKEND="$ROOT/backend"
E2E_DIR="$FRONTEND/e2e"

BACKEND_PORT="${E2E_BACKEND_PORT:-8011}"
FRONTEND_PORT="${E2E_FRONTEND_PORT:-4173}"
BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
FRONTEND_URL="http://127.0.0.1:$FRONTEND_PORT"

RUN_DIR="${E2E_RUN_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/botler-e2e.XXXXXX")}"
CONFIG_FILE="$RUN_DIR/config.yaml"
DB_PATH="$RUN_DIR/e2e.db"
LOG_FILE="$RUN_DIR/task-log.ndjson"

UVICORN_PID=""
PREVIEW_PID=""

log() { echo "[e2e] $*"; }

cleanup() {
  log "清理 E2E 服务进程（进程组）…"
  # setsid 启动的进程组（负 PID 杀整组，含 npm/node 子进程）
  [ -n "$UVICORN_PID" ] && kill -- "-$UVICORN_PID" 2>/dev/null || true
  [ -n "$PREVIEW_PID" ] && kill -- "-$PREVIEW_PID" 2>/dev/null || true
  if [ -n "${E2E_RUN_DIR:-}" ]; then
    log "保留运行目录: $RUN_DIR（E2E_RUN_DIR 已指定）"
  else
    rm -rf "$RUN_DIR"
  fi
}
trap cleanup EXIT

# ---------- 0. 前置检查 ----------
if [ ! -x "$BACKEND/.venv/bin/python" ]; then
  log "错误：后端虚拟环境不存在（$BACKEND/.venv），请先创建（uv venv + uv pip install -r requirements.lock.txt，issue #209）"
  exit 1
fi
if [ ! -f "$FRONTEND/dist/index.html" ]; then
  log "前端 dist 未构建，先执行 npm run build…"
  (cd "$FRONTEND" && npm run build)
fi

# ---------- 1. 后端配置 + 种子数据库 ----------
mkdir -p "$RUN_DIR"
cp "$E2E_DIR/backend-config.yaml" "$CONFIG_FILE"
log "生成种子数据库: $DB_PATH"
BOTLER_DB="$DB_PATH" E2E_LOG_FILE="$LOG_FILE" \
  "$BACKEND/.venv/bin/python" "$E2E_DIR/scripts/seed-e2e-db.py"

# ---------- 2. 启动 uvicorn（真实后端） ----------
log "启动 uvicorn: $BACKEND_URL（配置 $CONFIG_FILE）"
# setsid 新会话启动，进程组整体可被 cleanup 一次性回收
setsid bash -c '
  cd "$1" || exit 1
  BOTLER_CONFIG="$2" BOTLER_DB="$3" \
    exec .venv/bin/python -m uvicorn botler.main:app \
    --host 127.0.0.1 --port "$4" --log-level warning
' _ "$BACKEND" "$CONFIG_FILE" "$DB_PATH" "$BACKEND_PORT" &
UVICORN_PID=$!

for _ in $(seq 1 60); do
  if curl -sf "$BACKEND_URL/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -sf "$BACKEND_URL/api/health" >/dev/null \
  || { log "错误：后端启动超时（$BACKEND_URL/api/health）"; exit 1; }
log "后端就绪"

# ---------- 3. 启动 vite preview（真实前端构建产物，/api 代理到后端） ----------
log "启动 vite preview: $FRONTEND_URL（/api → $BACKEND_URL）"
setsid bash -c '
  cd "$1" || exit 1
  E2E_BACKEND_URL="$2" \
    exec npx vite preview --host 127.0.0.1 --port "$3"
' _ "$FRONTEND" "$BACKEND_URL" "$FRONTEND_PORT" &
PREVIEW_PID=$!

for _ in $(seq 1 30); do
  if curl -sf "$FRONTEND_URL/" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -sf "$FRONTEND_URL/" >/dev/null \
  || { log "错误：前端启动超时（$FRONTEND_URL/）"; exit 1; }
log "前端就绪"

# ---------- 4. 运行 Playwright ----------
log "运行 Playwright E2E（参数: $*）"
(
  cd "$FRONTEND"
  E2E_BASE_URL="$FRONTEND_URL" npx playwright test "$@"
)
