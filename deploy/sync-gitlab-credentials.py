#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步 GitLab bot token 到生产配置（issue #481）。

背景（深层根因）：生产 botler 的 data/backend/config.yaml 与
data/backend/.env 中 gitlab.bot_token 是人工维护的陈旧值。token 轮换
（如更新 CI 变量 GITLAB_BOT_TOKEN）后生产配置不跟着变，生产应用所有
GitLab API 调用 401（对账每 5 分钟失败、应用启动失败、概览数据拿不到）
→ 页面数据加载不出/卡顿。之前修复（#461 renice / #467 稳定目录 /
#469 迁移 backend:test）针对资源争抢，从未同步失效的生产 token，
因此「CI 期间生产页面卡顿」一直没修好。

本脚本在 deploy 阶段把 CI 变量中的有效 token 同步进生产配置：
  1. data/backend/.env 的 GITLAB_BOT_TOKEN（覆盖陈旧值——修复前
     「.env 已存在则保留」导致失效 token 永不过期）；
  2. data/backend/config.yaml 的 gitlab.bot_token（覆盖陈旧值）。

用法：sync-gitlab-credentials.py <data_dir>
  - data_dir：BOTLER_DATA_DIR（含 backend/.env 与 backend/config.yaml）
  - token 来源：环境变量 GITLAB_BOT_TOKEN（deploy job 的 CI 变量）
  - token 未注入或为空时跳过（幂等安全，不破坏现有配置）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_FILE_REL = Path("backend") / ".env"
CONFIG_FILE_REL = Path("backend") / "config.yaml"
ENV_KEY = "GITLAB_BOT_TOKEN"
CONFIG_KEY = "bot_token"  # config.yaml gitlab 段下的键


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _atomic_write(path: Path, lines: list[str]) -> None:
    """原子写回（临时文件 + rename），避免半写文件被应用读到。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(path)


def sync_env_file(path: Path, token: str) -> bool:
    """把 .env 的 GITLAB_BOT_TOKEN 覆盖为 token；返回是否发生变更。"""
    lines = _read_lines(path)
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(f"{ENV_KEY}="):
            if line.strip() == f"{ENV_KEY}={token}":
                return False  # 已是目标值，幂等
            lines[i] = f"{ENV_KEY}={token}\n"
            replaced = True
            break
    if not replaced:
        if not lines or not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{ENV_KEY}={token}\n")
    _atomic_write(path, lines)
    return True


def sync_config_yaml(path: Path, token: str) -> bool:
    """把 config.yaml gitlab 段的 bot_token 覆盖为 token；返回是否变更。

    采用行级替换（仅动 bot_token 一行），保留文件其余内容原样
    （配置由 ConfigManager 生成，但行级修改最不易引入回归）。
    """
    lines = _read_lines(path)
    # 定位 gitlab: 顶层段（通常是文件第一个顶层键）
    gitlab_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not line.startswith((" ", "\t")):
            if stripped.rstrip(":") == "gitlab":
                gitlab_start = i
                break
            # 第一个顶层键不是 gitlab → 无处可写，跳过
            return False
    if gitlab_start is None:
        return False

    # 在该段内查找 bot_token 行（gitlab 段内 2 空格缩进的 bot_token:）
    for i in range(gitlab_start + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            continue
        # 遇到下一个顶层键（无缩进）→ 段结束
        if not line.startswith((" ", "\t")):
            break
        if line.startswith("  ") and stripped.startswith(f"{CONFIG_KEY}:"):
            if stripped == f"{CONFIG_KEY}: {token}":
                return False  # 已是目标值，幂等
            lines[i] = f"  {CONFIG_KEY}: {token}\n"
            _atomic_write(path, lines)
            return True
    # gitlab 段内无 bot_token → 插到段首字段前（紧跟 gitlab: 行后）
    insert_at = gitlab_start + 1
    # 若 gitlab: 后有注释/空行，插到第一个字段前
    while insert_at < len(lines) and (not lines[insert_at].strip()
                                      or lines[insert_at].lstrip().startswith("#")):
        insert_at += 1
    lines.insert(insert_at, f"  {CONFIG_KEY}: {token}\n")
    _atomic_write(path, lines)
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: sync-gitlab-credentials.py <data_dir>", file=sys.stderr)
        return 2
    data_dir = Path(sys.argv[1])
    token = os.environ.get(ENV_KEY, "").strip()
    if not token:
        print("⚠️  环境变量 GITLAB_BOT_TOKEN 未注入，跳过 token 同步")
        return 0

    env_path = data_dir / ENV_FILE_REL
    config_path = data_dir / CONFIG_FILE_REL
    changed = []
    if env_path.is_file():
        if sync_env_file(env_path, token):
            changed.append(str(env_path))
    else:
        print(f"⚠️  {env_path} 不存在，跳过 .env 同步", file=sys.stderr)
    if config_path.is_file():
        if sync_config_yaml(config_path, token):
            changed.append(str(config_path))
    else:
        print(f"⚠️  {config_path} 不存在，跳过 config.yaml 同步", file=sys.stderr)

    if changed:
        print(f"✓ GitLab bot token 已同步: {', '.join(changed)}")
    else:
        print("✓ GitLab bot token 已是最新，无需变更")
    return 0


if __name__ == "__main__":
    sys.exit(main())
