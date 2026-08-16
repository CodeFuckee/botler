#!/usr/bin/env bash
# ================================================================
# 安装 deepseek-harness SDK（dsh 引擎依赖，issue #84 / #112）
#
# 用法:
#   deploy/install-dsh-sdk.sh                      # 默认装进 backend/.venv
#   deploy/install-dsh-sdk.sh <venv路径>           # 指定 venv（如 /opt/venv）
#   DSH_INDEX_URL=<镜像源> deploy/install-dsh-sdk.sh   # 覆盖 pip 镜像源
#
# 说明:
#   - SDK 为 rc 预发布版，不在 requirements.txt（主依赖继续走清华源，
#     避免 rc 版解析失败阻塞全部依赖安装），按部署形态单独安装；
#   - 清华 pip 镜像未同步 rc 版（仅有 0.0.0.dev0 占位），默认走阿里
#     镜像，可用 DSH_INDEX_URL 环境变量覆盖（内网代理场景）；
#   - 幂等：已装目标版本直接跳过（重复部署/重跑不重复安装）；
#   - 安装后 import 校验，装不上立即失败（fail fast，不带病运行）；
#   - 优先 uv pip（CI venv 由 uv 创建、无 pip seed），无 uv 时回退
#     venv 内 pip，再回退 python -m pip（覆盖手动部署的 python3 -m venv）。
#
# CI pm2 部署（deploy_to_code01）在主依赖安装后自动调用本脚本；
# Docker 部署无需本脚本（Dockerfile 构建期已内置 SDK）。
# ================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${1:-$ROOT/backend/.venv}"
SDK_PIN="deepseek-harness-sdk==0.1.0rc6"
INDEX_URL="${DSH_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "❌ venv 不存在或不可执行: $VENV（请先创建后端 venv）" >&2
  exit 1
fi

# 幂等：已装目标版本直接跳过（importlib.metadata 检测，与 pip 无关，
# uv 创建的 venv 无 pip 也可检测）
if "$PY" -c 'import importlib.metadata as m; raise SystemExit(0 if m.version("deepseek-harness-sdk") == "0.1.0rc6" else 1)' 2>/dev/null; then
  echo "✓ deepseek-harness-sdk 0.1.0rc6 已安装，跳过"
  exit 0
fi

echo "→ 安装 $SDK_PIN 到 $VENV（镜像源: $INDEX_URL）"
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" "$SDK_PIN" -i "$INDEX_URL"
elif [ -x "$VENV/bin/pip" ]; then
  "$VENV/bin/pip" install --no-cache-dir "$SDK_PIN" -i "$INDEX_URL"
else
  "$PY" -m pip install --no-cache-dir "$SDK_PIN" -i "$INDEX_URL"
fi

# 安装后 import 校验：装不上立即失败（fail fast，部署失败而非带病运行）
"$PY" -c "from deepseek_harness import DeepSeekHarness" || {
  echo "❌ SDK 安装后 import 校验失败（deepseek_harness 不可导入）" >&2
  exit 1
}
echo "✓ deepseek-harness SDK 安装完成（$SDK_PIN，可导入 DeepSeekHarness）"
