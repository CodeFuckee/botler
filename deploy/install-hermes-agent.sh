#!/usr/bin/env bash
# ================================================================
# 安装 hermes-agent SDK（hermes 引擎依赖，issue #171）
#
# 用法:
#   deploy/install-hermes-agent.sh                      # 默认装进 backend/.venv
#   deploy/install-hermes-agent.sh <venv路径>           # 指定 venv（如 /opt/venv）
#   HERMES_SOURCE_DIR=<hermes-agent源码目录> deploy/install-hermes-agent.sh  # 覆盖源码位置
#
# 说明:
#   - hermes 引擎（issue #171 起）改为「hermes agent SDK 进程内集成」：
#     botler 自身 venv 内 editable 安装 hermes-agent 源码并进程内调用
#     run_agent.AIAgent（对齐 dsh 引擎的 SDK 集成方式，issue #84），
#     不再经子进程调用部署机独立 venv（旧 hermes.command/hermes.args 已移除）；
#   - hermes-agent 以源码方式分发（PyPI 无 wheel，setup.py 禁 wheel 构建），
#     只能 editable 安装（pip install -e / uv pip install -e）；
#   - hermes-agent 的 pyproject requires-python 为 >=3.11,<3.14（上游注释称
#     Rust 传递依赖缺 cp314 wheel 而封顶，实测 cp314 wheel 已可用），pm2
#     部署 backend/.venv 为 Python 3.14，pip 路径统一加 --ignore-requires-python
#     （uv 不支持该参数，uv 路径仅适用于 Python <3.14 的 CI/Docker venv）；
#   - 幂等：run_agent 已可导入直接跳过（重复部署/重跑不重复安装）；
#   - 安装后 import 校验，装不上立即失败（fail fast，不带病运行）；
#   - 优先 venv 内 pip（支持 --ignore-requires-python），无 pip 时回退 uv
#     （CI venv 由 uv 创建、无 pip seed，Python 3.11 无需该参数）；
#   - LLM 配置仍在 hermes 侧 ~/.hermes（botler 不管理，与旧模式一致）。
#
# CI pm2 部署（deploy_to_code01）在主依赖安装后自动调用本脚本；
# Docker 部署由容器 entrypoint 在启动时对挂载源码执行等价安装
# （见 docker-entrypoint.sh / Dockerfile）。
# ================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${1:-$ROOT/backend/.venv}"
SOURCE="${HERMES_SOURCE_DIR:-$HOME/.hermes/hermes-agent}"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "❌ venv 不存在或不可执行: $VENV（请先创建后端 venv）" >&2
  exit 1
fi

if [ ! -f "$SOURCE/pyproject.toml" ]; then
  echo "❌ hermes-agent 源码不存在: $SOURCE（请先安装 hermes-agent，或设置" >&2
  echo "   HERMES_SOURCE_DIR 指向源码目录；参考 docs/hermes-engine-deployment.md）" >&2
  exit 1
fi

# 幂等：run_agent 已可导入直接跳过（importlib.util.find_spec 检测，
# 与 pip 无关，uv 创建的 venv 无 pip 也可检测；不 import 全模块——
# run_agent 导入较重且依赖其 venv 内工具模块，find_spec 足够判断安装态）
if "$PY" -c 'import importlib.util; raise SystemExit(0 if importlib.util.find_spec("run_agent") else 1)' 2>/dev/null; then
  echo "✓ hermes-agent SDK 已安装（run_agent 可导入），跳过"
  exit 0
fi

echo "→ editable 安装 hermes-agent SDK（$SOURCE → $VENV）"
if "$PY" -m pip --version >/dev/null 2>&1; then
  "$PY" -m pip install --no-cache-dir --ignore-requires-python -e "$SOURCE"
elif command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" -e "$SOURCE"
else
  echo "❌ venv 无 pip 且无 uv，无法安装 hermes-agent SDK（请先安装 pip 或 uv）" >&2
  exit 1
fi

# 安装后 import 校验：装不上立即失败（fail fast，部署失败而非带病运行）
"$PY" -c 'import importlib.util; assert importlib.util.find_spec("run_agent")' || {
  echo "❌ hermes-agent SDK 安装后校验失败（run_agent 不可导入）" >&2
  exit 1
}
echo "✓ hermes-agent SDK 安装完成（$SOURCE，run_agent 可导入）"
