#!/usr/bin/env bash
# ================================================================
# Botler 容器启动入口（issue #171：hermes agent SDK 进程内集成）
#
# hermes-agent 以源码分发（PyPI 无 wheel，setup.py 禁 wheel 构建），
# Docker 构建期无法访问宿主机 hermes-agent 源码（源码在 NAS 上经
# docker-compose 只读挂载进容器 /opt/hermes/hermes-agent）。因此 SDK
# 的 editable 安装放在容器启动时执行（幂等：已装直接跳过，重启开销
# 仅一次 pip 检查）：
#   - 挂载了 hermes-agent 源码（pyproject.toml 存在）且 run_agent 尚未
#     可导入 → pip install -e（Python 3.11 venv 满足 requires-python，
#     无需 --ignore-requires-python，统一带上无副作用）；
#   - 未挂载源码 → 跳过并告警（hermes 引擎启动时由
#     HermesSdkNotInstalledError 给出安装指引，与 dsh 未装 SDK 同语义）；
#   - HERMES_SOURCE_DIR 环境变量可覆盖源码路径（默认 /opt/hermes/hermes-agent）。
#
# 随后 exec 主进程（uvicorn，见 Dockerfile CMD）。
# ================================================================
set -euo pipefail

HERMES_SOURCE="${HERMES_SOURCE_DIR:-/opt/hermes/hermes-agent}"

if [ -f "$HERMES_SOURCE/pyproject.toml" ]; then
  if /opt/venv/bin/python -c 'import importlib.util; raise SystemExit(0 if importlib.util.find_spec("run_agent") else 1)' 2>/dev/null; then
    echo "✓ hermes-agent SDK 已安装（run_agent 可导入），跳过"
  else
    echo "→ editable 安装 hermes-agent SDK（$HERMES_SOURCE → /opt/venv）..."
    /opt/venv/bin/pip install --no-cache-dir --ignore-requires-python -e "$HERMES_SOURCE"
    /opt/venv/bin/python -c 'import importlib.util; assert importlib.util.find_spec("run_agent")'
    echo "✓ hermes-agent SDK 安装完成"
  fi
else
  echo "⚠ hermes-agent 源码未挂载（$HERMES_SOURCE 不存在），hermes 引擎将提示 SDK 未安装" >&2
fi

exec "$@"
