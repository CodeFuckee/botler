"""本地 git 仓库 remote 解析：运行 git remote -v 读取 remote 列表。

供「本地文件夹方式」添加仓库使用：用户填本地文件夹路径，
平台在服务端运行 git remote -v 拿到 remote URL，再用 URL 识别 GitLab 项目。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class NoGitRemoteError(Exception):
    """路径不是 git 仓库，或仓库没有任何可用 remote。"""


def parse_git_remote_output(text: str) -> list[dict]:
    """解析 git remote -v 输出，返回 [{"name", "url"}, ...]。

    git remote -v 每行格式：<name>\\t<url> (fetch|push)。
    只保留 fetch 行，同名同 URL 去重。没有任何 fetch remote 时抛 NoGitRemoteError。
    """
    remotes: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name, rest = parts
        if not rest.endswith("(fetch)"):
            continue
        url = rest[: -len("(fetch)")].strip()
        if not url or (name, url) in seen:
            continue
        seen.add((name, url))
        remotes.append({"name": name, "url": url})
    if not remotes:
        raise NoGitRemoteError("本地仓库没有任何可用 remote")
    return remotes


def list_local_remotes(path: str, timeout: int = 15) -> list[dict]:
    """在本地文件夹运行 git remote -v，返回 remote 列表。

    路径不存在、不是 git 仓库或读取失败时抛 NoGitRemoteError。
    """
    local = Path(path)
    if not local.is_dir():
        raise NoGitRemoteError(f"路径不存在或不是文件夹: {path}")
    try:
        result = subprocess.run(
            ["git", "remote", "-v"], cwd=local,
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise NoGitRemoteError(f"读取 git remote 超时: {path}")
    if result.returncode != 0:
        raise NoGitRemoteError(
            f"不是有效的 git 仓库: {(result.stderr or result.stdout).strip()[-300:]}")
    return parse_git_remote_output(result.stdout)
