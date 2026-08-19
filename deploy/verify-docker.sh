#!/usr/bin/env bash
# ================================================================
# Botler Docker 部署验证（冒烟测试）
#
# 用法:
#   ./deploy/verify-docker.sh             # 静态校验 + 镜像构建
#   ./deploy/verify-docker.sh --full      # 加上完整冒烟（起容器 + 健康检查 + API）
#
# --full 使用临时数据目录与 18000 端口，不触碰真实数据：
#   - 构造临时 config.yaml（假凭据）/ .env / botler.db
#   - docker compose up 起容器 → 等 healthcheck → 校验 /api/health 与前端页面
#   - 校验幂等（重复 up 无报错）→ down 清理
#   - MinIO（issue #160）随 compose 一并启动：19000/19001 临时端口校验
#     /minio/health/live 与 /data 数据目录
#   - healthcheck 状态（issue #207）：compose ps 显示 botler healthy（容器
#     内 curl 探针生效），并模拟事件循环卡死（SIGSTOP 主进程）→ 容器变
#     unhealthy → 恢复（SIGCONT）→ 重新 healthy，验证假死可感知
# ================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
ok()   { PASS=$((PASS+1)); echo "✅ $1"; }
bad()  { FAIL=$((FAIL+1)); echo "❌ $1"; }
die()  { echo "❌ $1"; exit 1; }

echo "===== 1. 前置检查 ====="
docker --version >/dev/null 2>&1 || die "docker 不可用"
docker compose version >/dev/null 2>&1 || die "docker compose 不可用"
[ -f Dockerfile ]        || die "缺少 Dockerfile"
[ -f docker-compose.yml ] || die "缺少 docker-compose.yml"
ok "docker / compose 可用，Dockerfile 与 docker-compose.yml 存在"

echo "===== 2. compose 配置校验 ====="
docker compose config -q || die "docker compose config 校验失败"
ok "docker compose config 语法有效"

echo "===== 3. 镜像构建 ====="
# 国内环境可 export DOCKER_BUILD_ARGS="--build-arg NODE_IMAGE=... --build-arg RUNTIME_IMAGE=..." 覆盖基础镜像
docker build ${DOCKER_BUILD_ARGS:-} -t botler:verify . || die "docker build 失败"
ok "docker build 成功（botler:verify）"

if [[ "${1:-}" == "--full" ]]; then
  echo "===== 4. 完整冒烟（临时数据目录 + 18000 端口）====="
  TMP="$(mktemp -d)"
  trap 'docker compose -p botler-verify down -v >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT

  # compose 构建阶段的基础镜像覆盖（与 DOCKER_BUILD_ARGS 保持一致）
  NODE_IMAGE="${NODE_IMAGE:-$(echo "$DOCKER_BUILD_ARGS" | sed -n 's/.*NODE_IMAGE=\([^ ]*\).*/\1/p')}"
  RUNTIME_IMAGE="${RUNTIME_IMAGE:-$(echo "$DOCKER_BUILD_ARGS" | sed -n 's/.*RUNTIME_IMAGE=\([^ ]*\).*/\1/p')}"
  export NODE_IMAGE RUNTIME_IMAGE

  # 构造部署环境：config.example.yaml 复制为 config.yaml；
  # 假凭据写入 .env（后端 load_dotenv 从挂载的 .env 读取，health 不依赖
  # GitLab 连通性——启动时连接失败自动降级）
  mkdir -p "$TMP/backend" "$TMP/workspace" "$TMP/logs"
  cp backend/config.example.yaml "$TMP/backend/config.yaml"
  printf 'GITLAB_BOT_TOKEN=fake-token-for-smoke\nWEBHOOK_SECRET=fake-secret-for-smoke\n' > "$TMP/backend/.env"
  touch "$TMP/backend/botler.db"   # 空 db，SQLite 首次运行自动建表
  # 容器内 git 凭据占位（冒烟不涉及真实 git 操作，空凭据文件即可；
  # 不 export 的话 compose 会挂载 /dev/null 兜底，此处显式指定更贴近真实部署）
  touch "$TMP/.git-credentials"
  export GIT_CREDENTIALS_FILE="$TMP/.git-credentials"

  # 用假凭据起容器
  BOTLER_DATA_DIR="$TMP" BOTLER_HTTP_PORT=18000 \
  MINIO_API_PORT=19000 MINIO_CONSOLE_PORT=19001 \
  docker compose -p botler-verify up -d || die "docker compose up 失败"
  ok "容器已启动"

  echo "----- 等待健康检查（最长 90s）-----"
  HEALTHY=0
  for i in $(seq 1 45); do
    if curl -fsS --max-time 3 http://127.0.0.1:18000/api/health >/dev/null 2>&1; then
      HEALTHY=1; break
    fi
    sleep 2
  done
  [ "$HEALTHY" = 1 ] || {
    echo "--- 容器状态 ---"; docker compose -p botler-verify ps
    echo "--- 容器日志（尾部）---"; docker compose -p botler-verify logs --tail 50
    die "健康检查超时"
  }
  ok "健康检查通过（/api/health 200）"

  echo "----- 校验 compose healthcheck 状态（botler healthy，issue #207）-----"
  # 宿主机 curl 通过 ≠ 容器内 healthcheck 探针生效：等待 docker compose ps
  # 显示 botler 为 healthy（容器内 curl /api/health 由 docker 守护进程驱动）
  COMPOSE_HEALTHY=0
  for i in $(seq 1 45); do
    if docker compose -p botler-verify ps --format '{{.Name}}|{{.Status}}' 2>/dev/null \
      | grep -q '^botler|.*healthy'; then
      COMPOSE_HEALTHY=1; break
    fi
    sleep 2
  done
  [ "$COMPOSE_HEALTHY" = 1 ] || {
    echo "--- 容器状态 ---"; docker compose -p botler-verify ps
    die "botler 容器未显示 healthy（compose healthcheck 未生效）"
  }
  ok "compose ps 显示 botler healthy（容器内 healthcheck 探针生效）"

  echo "----- 校验 MinIO 服务（issue #160）-----"
  docker compose -p botler-verify ps | grep -q "minio" || die "minio 容器未运行"
  ok "minio 容器已启动"
  MINIO_HEALTHY=0
  for i in $(seq 1 45); do
    if curl -fsS --max-time 3 http://127.0.0.1:19000/minio/health/live >/dev/null 2>&1; then
      MINIO_HEALTHY=1; break
    fi
    sleep 2
  done
  [ "$MINIO_HEALTHY" = 1 ] || {
    echo "--- minio 容器日志（尾部）---"
    docker compose -p botler-verify logs minio --tail 30
    die "MinIO 健康检查超时"
  }
  ok "MinIO 健康检查通过（/minio/health/live 200，端口 19000）"
  docker compose -p botler-verify exec -T minio     sh -c "ls /data >/dev/null 2>&1" || die "minio 数据目录 /data 不可用"
  ok "minio 数据目录 /data 可读写"

  echo "----- 校验 API 与前端页面 -----"
  HEALTH=$(curl -fsS http://127.0.0.1:18000/api/health)
  echo "$HEALTH" | grep -q '"ok":true' || die "health 响应异常: $HEALTH"
  ok "health 响应含 ok:true"

  curl -fsS http://127.0.0.1:18000/ | grep -qi 'id="root"' || die "前端页面未正常返回"
  ok "前端首页正常返回（含 #root 挂载点）"

  echo "----- 校验容器时区（Asia/Shanghai）-----"
  TZ_OUT=$(docker compose -p botler-verify exec -T botler date +%Z) || die "容器内 date 不可用"
  [ "$TZ_OUT" = "CST" ] || die "容器时区异常: $TZ_OUT（应为 CST = Asia/Shanghai）"
  ok "容器时区为 Asia/Shanghai（date +%Z = CST）"

  echo "----- 校验 dsh 引擎 SDK 已内置（deepseek-harness，issue #112）-----"
  docker compose -p botler-verify exec -T botler \
    /opt/venv/bin/python -c "from deepseek_harness import DeepSeekHarness" \
    || die "deepseek-harness SDK 未内置（dsh 引擎不可用）"
  ok "deepseek-harness SDK 已内置（可导入 DeepSeekHarness）"

  echo "----- 模拟事件循环卡死（issue #207）：SIGSTOP 主进程 → unhealthy → 恢复 -----"
  # entrypoint 最终 exec uvicorn → 容器 PID 1 即应用进程；SIGSTOP 冻结事件
  # 循环（进程活着但完全不响应），healthcheck curl 超时 → 连续 3 次失败后
  # docker 标记 unhealthy——验证「假死无感知」被修复（compose ps 可感知）
  docker compose -p botler-verify exec -T botler kill -STOP 1 || die "SIGSTOP 主进程失败"
  HANG_UNHEALTHY=0
  for i in $(seq 1 60); do
    if docker compose -p botler-verify ps --format '{{.Name}}|{{.Status}}' 2>/dev/null \
      | grep -q '^botler|.*unhealthy'; then
      HANG_UNHEALTHY=1; break
    fi
    sleep 3
  done
  [ "$HANG_UNHEALTHY" = 1 ] || {
    echo "--- 容器状态 ---"; docker compose -p botler-verify ps
    die "事件循环卡死未标记 unhealthy（healthcheck 未感知假死）"
  }
  ok "模拟事件循环卡死 → 容器变为 unhealthy（假死可感知）"

  # 恢复运行：SIGCONT 继续，healthcheck 下一次探测成功 → 重新 healthy
  docker compose -p botler-verify exec -T botler kill -CONT 1 || die "SIGCONT 恢复失败"
  RESUME_HEALTHY=0
  for i in $(seq 1 30); do
    if docker compose -p botler-verify ps --format '{{.Name}}|{{.Status}}' 2>/dev/null \
      | grep -q '^botler|.*healthy'; then
      RESUME_HEALTHY=1; break
    fi
    sleep 3
  done
  [ "$RESUME_HEALTHY" = 1 ] || {
    echo "--- 容器状态 ---"; docker compose -p botler-verify ps
    die "恢复运行后未重新 healthy"
  }
  ok "恢复运行（SIGCONT）→ 容器重新 healthy"

  echo "----- 幂等性：重复 up 无报错 -----"
  BOTLER_DATA_DIR="$TMP" BOTLER_HTTP_PORT=18000 \
    MINIO_API_PORT=19000 MINIO_CONSOLE_PORT=19001 \
    docker compose -p botler-verify up -d >/dev/null 2>&1 || die "重复 up 失败"
  ok "重复 up 幂等"

  echo "----- 边界：数据目录挂载可写（config.yaml 写回 / botler.db 落盘）-----"
  docker compose -p botler-verify exec -T botler test -w /app/backend/config.yaml || die "config.yaml 不可写（Web UI 设置页会写回）"
  docker compose -p botler-verify exec -T botler test -s /app/backend/botler.db || die "botler.db 未落盘"
  ok "config.yaml 可写、botler.db 已落盘"

  echo "----- 清理 -----"
  docker compose -p botler-verify down -v >/dev/null 2>&1
  ok "冒烟容器已清理（含 minio）"
fi

echo ""
echo "========================================="
echo "验证结果: ${PASS} 项通过, ${FAIL} 项失败"
[ "$FAIL" -eq 0 ] || exit 1
echo "🎉 Docker 部署验证全部通过"
