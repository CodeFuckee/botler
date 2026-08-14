"""本地 git 仓库 remote 解析：运行 git remote -v 读取 remote 列表。

供「本地文件夹方式」添加仓库使用：用户填本地文件夹路径，
平台在服务端运行 git remote -v 拿到 remote URL，再用 URL 识别 GitLab 项目。

issue #60：remote URL 可能内嵌凭据（https://user:token@host/path.git），
提供 URL 凭据解析（parse_remote_url）与展示脱敏（mask_url_token）。
issue #63：提供按仓库 remote 内嵌 token 构建 per-repo GitLabClient 的
公共函数（build_repo_client），概览页流水线与对账兜底共用。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

# 任务执行工作区根目录（与 executor 默认一致：backend/botler/workspace）
_WORKSPACE_ROOT = Path(__file__).resolve().parents[0] / "workspace"


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


def parse_remote_url(url: str | None) -> dict:
    """解析 remote URL 中的凭据信息（issue #60）。

    支持 https://user:token@host:port/path.git 形式，返回
    {"scheme", "host", "username", "token"}；host 含端口（如有）。
    scp-like（git@host:path）与 ssh 形态无内嵌 token；URL 中无凭据、
    空值或解析失败时 username / token 为 None。token 按 URL 解码还原。
    """
    result = {"scheme": None, "host": None, "username": None, "token": None}
    value = (url or "").strip()
    if not value:
        return result
    # scp-like 无 scheme：凭据走 ssh key / agent，无内嵌 token
    if "://" not in value:
        return result
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return result
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    result["scheme"] = parsed.scheme
    result["host"] = host
    # netloc 形如 user:token@host:port；最后一个 @ 前是 userinfo 段
    if "@" in parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", 1)[0]
        if ":" in userinfo:
            username, token = userinfo.split(":", 1)
            result["username"] = unquote(username)
            result["token"] = unquote(token)
        else:
            # 只有用户名没有密码：不是 token
            result["username"] = unquote(userinfo)
    return result


def mask_url_token(url: str | None) -> str:
    """仓库 URL 脱敏（issue #60）：user:token@ → user:***@。

    无凭据、只有用户名（无密码）、scp-like / ssh 形态原样返回；
    已脱敏的 URL 幂等（userinfo 无冒号或密码本就是 *** 时结果不变）。
    """
    value = (url or "").strip()
    if not value or "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    if "@" not in rest:
        return value
    userinfo, tail = rest.rsplit("@", 1)
    if ":" not in userinfo:
        return value  # 只有用户名没有 token，无需脱敏
    username = userinfo.split(":", 1)[0]
    return f"{scheme}://{username}:***@{tail}"


def build_repo_client(row, verify_ssl: bool = True):
    """为仓库构建 per-repo GitLabClient（issue #60 / #63 公共函数）。

    在仓库本地目录（local_path 优先，否则 workspace/<name>，与 executor
    工作区一致）运行 git remote -v，从 remote_name（缺省 origin）对应的
    URL 解析内嵌 token：有 token 则用该 token 与 remote host 建客户端，
    每个仓库使用自己的 token。remote 无 token / 本地目录不存在 / 不是
    git 仓库时返回 None（调用方回退全局 bot token 客户端，兼容旧仓库）。
    """
    from .gitlab_client import GitLabClient

    d = dict(row)  # sqlite3.Row / dict 统一按 dict 访问
    workdir = None
    local_path = d.get("local_path")
    if local_path:
        workdir = Path(local_path)
    else:
        workdir = _WORKSPACE_ROOT / str(d["name"])
    try:
        remotes = list_local_remotes(str(workdir))
    except NoGitRemoteError:
        return None
    remote_name = d.get("remote_name") or "origin"
    match = next((r for r in remotes if r["name"] == remote_name), None)
    if match is None:
        return None
    info = parse_remote_url(match["url"])
    token, host, scheme = info["token"], info["host"], info["scheme"]
    if not token or not host or not scheme:
        return None
    return GitLabClient(f"{scheme}://{host}", token, verify_ssl=verify_ssl)
