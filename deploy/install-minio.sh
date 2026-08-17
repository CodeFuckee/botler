#!/usr/bin/env bash
# ================================================================
# 安装 MinIO Server 二进制（后端部署附属对象存储服务，issue #160）
#
# 用法:
#   deploy/install-minio.sh                       # 默认装到 $HOME/.local/bin
#   deploy/install-minio.sh /usr/local/bin        # 指定安装目录
#   MINIO_DOWNLOAD_URL=<镜像> deploy/install-minio.sh   # 覆盖下载地址
#
# 说明:
#   - 目标版本 MINIO_VERSION 显式锁定（幂等检测与确定性部署依据，
#     不用 latest 避免版本漂移）；
#   - 国内无法访问 dl.min.io 时可设 MINIO_DOWNLOAD_URL 指向镜像
#     （如 https://minio.example.com/minio）；
#   - 幂等：已安装且版本一致直接跳过（重复部署/重跑不重复下载）；
#   - 临时文件 + mv 原子替换，避免下载中断留下半截二进制；
#   - 安装后 minio --version 校验，失败即 exit 1（fail fast，不带病运行）。
#
# CI pm2 部署（deploy_to_code01）自动调用本脚本；
# Docker 部署无需本脚本（docker-compose.yml 直接使用 minio/minio 镜像）。
# ================================================================
set -euo pipefail

MINIO_VERSION="RELEASE.2025-04-22T22-12-26Z"
INSTALL_DIR="${1:-$HOME/.local/bin}"
MINIO_BIN="$INSTALL_DIR/minio"
# 锁定版本的 archive 地址（dl.min.io 官方源；内网/镜像场景用
# MINIO_DOWNLOAD_URL 覆盖，URL 以 minio 文件名结尾）
DOWNLOAD_URL="${MINIO_DOWNLOAD_URL:-https://dl.min.io/server/minio/release/linux-amd64/archive/minio.${MINIO_VERSION}}"

# 幂等：已安装且版本一致直接跳过
if [ -x "$MINIO_BIN" ] && "$MINIO_BIN" --version 2>/dev/null | grep -q "${MINIO_VERSION}"; then
  echo "✓ minio ${MINIO_VERSION} 已安装（$MINIO_BIN），跳过"
  exit 0
fi

mkdir -p "$INSTALL_DIR"
echo "→ 下载 minio ${MINIO_VERSION} → $MINIO_BIN"
echo "  下载地址: ${DOWNLOAD_URL}"
TMP_FILE="$INSTALL_DIR/.minio.tmp.$$"
trap 'rm -f "$TMP_FILE"' EXIT
curl -fsSL --retry 3 "$DOWNLOAD_URL" -o "$TMP_FILE"
chmod +x "$TMP_FILE"
mv -f "$TMP_FILE" "$MINIO_BIN"

# 安装后版本校验：失败即部署失败（fail fast，不带病运行）
if ! "$MINIO_BIN" --version 2>/dev/null | grep -q "${MINIO_VERSION}"; then
  echo "❌ minio 安装校验失败：$MINIO_BIN 版本与 ${MINIO_VERSION} 不符" >&2
  exit 1
fi
echo "✓ minio ${MINIO_VERSION} 安装完成（$MINIO_BIN）"
