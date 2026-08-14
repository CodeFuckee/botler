"""本地环境检测（issue #22）：检测服务器上常见 AI agent / 基础工具是否安装及其版本。

实现方式：shutil.which 定位可执行文件 → 执行 <工具> --version 解析版本号；
已安装且带发布源的 agent 工具再并发查询最新版本（npm registry / GitHub API）。
所有子步骤均容错：任何失败只影响单项字段，不中断整体检测。
"""

from __future__ import annotations

import logging
import re
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as pkg_version
from importlib.util import find_spec

import httpx

logger = logging.getLogger(__name__)

# 检测工具清单。latest_source: "npm"（查 npm registry）/ "github"（查 GitHub
# releases）/ "pypi"（查 PyPI）/ None（无发布源，不查最新版本，前端显示 "—"）。
# 带 "module" 键的项为 pip 包检测（find_spec 定位模块 + 读包版本），
# 不走 which/--version（issue #84 dsh SDK）。
TOOLS = [
    {"key": "claude", "name": "Claude Code", "command": "claude",
     "version_args": ["--version"], "latest_source": "npm",
     "latest_pkg": "@anthropic-ai/claude-code"},
    {"key": "codex", "name": "OpenAI Codex", "command": "codex",
     "version_args": ["--version"], "latest_source": "npm",
     "latest_pkg": "@openai/codex"},
    {"key": "gemini", "name": "Gemini CLI", "command": "gemini",
     "version_args": ["--version"], "latest_source": "npm",
     "latest_pkg": "@google/gemini-cli"},
    {"key": "aider", "name": "Aider", "command": "aider",
     "version_args": ["--version"], "latest_source": "npm",
     "latest_pkg": "aider-chat"},
    # hermes 为 git 安装的内部 agent（issue #48），无 npm/GitHub 公开发布源，
    # 只检测安装与版本，不查最新版本（前端显示 "—"）
    {"key": "hermes", "name": "Hermes Agent", "command": "hermes",
     "version_args": ["--version"], "latest_source": None},
    # dsh 为 pip 包（issue #84：deepseek-harness-sdk，可选依赖），不走
    # which/--version，按 module 检测 + PyPI 查最新版本
    {"key": "dsh", "name": "DeepSeek Harness SDK", "module": "deepseek_harness",
     "latest_source": "pypi", "latest_pkg": "deepseek-harness-sdk"},
    {"key": "gh", "name": "GitHub CLI", "command": "gh",
     "version_args": ["--version"], "latest_source": "github",
     "latest_repo": "cli/cli"},
    {"key": "git", "name": "Git", "command": "git",
     "version_args": ["--version"], "latest_source": None},
    {"key": "docker", "name": "Docker", "command": "docker",
     "version_args": ["--version"], "latest_source": None},
    {"key": "node", "name": "Node.js", "command": "node",
     "version_args": ["--version"], "latest_source": None},
    {"key": "npm", "name": "npm", "command": "npm",
     "version_args": ["--version"], "latest_source": None},
    {"key": "python3", "name": "Python", "command": "python3",
     "version_args": ["--version"], "latest_source": None},
    {"key": "uv", "name": "uv", "command": "uv",
     "version_args": ["--version"], "latest_source": None},
]

# --version 输出风格各异（claude 1.2.3 / git version 2.43.0 / Docker version 27.0.0,
# build xxx / v22.0.0），统一取首个 x.y.z
VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def parse_version(text: str | None) -> str | None:
    """从命令输出中提取首个 x.y.z 版本号；没有则返回 None。"""
    if not text:
        return None
    match = VERSION_RE.search(text)
    return match.group(1) if match else None


def detect_tool(tool: dict, timeout: int = 10) -> dict:
    """检测单个工具：which 定位 + 执行 --version（pip 包走 module 检测）。

    返回 {"key", "name", "installed", "version"}。找不到 → 未安装；
    找到但版本读取失败（非零退出/超时/异常/无元数据）→ 已安装但版本
    未知（None）。
    """
    base = {"key": tool["key"], "name": tool["name"], "installed": False, "version": None}
    module = tool.get("module")
    if module is not None:
        return _detect_module(tool, base)
    cmd_path = shutil.which(tool["command"])
    if not cmd_path:
        return base
    base["installed"] = True
    try:
        result = subprocess.run(
            [cmd_path, *tool["version_args"]],
            capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            base["version"] = parse_version(result.stdout)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("读取 %s 版本失败: %s", tool["key"], e)
    return base


def _detect_module(tool: dict, base: dict) -> dict:
    """pip 包检测（issue #84）：find_spec 定位模块 + importlib.metadata 读版本。"""
    module = tool["module"]
    pkg = tool.get("pkg", tool.get("latest_pkg")) or module.replace("_", "-")
    if find_spec(module) is None:
        return base
    base["installed"] = True
    try:
        base["version"] = parse_version(pkg_version(pkg))
    except (PackageNotFoundError, ModuleNotFoundError, ValueError) as e:
        logger.warning("读取 %s 版本失败: %s", tool["key"], e)
    return base


def fetch_latest(tool: dict, timeout: int = 8) -> str | None:
    """查询工具最新版本；无发布源或网络失败返回 None（前端显示 "—"）。"""
    source = tool.get("latest_source")
    if source == "npm":
        pkg = tool["latest_pkg"]
        url = f"https://registry.npmjs.org/{pkg.replace('/', '%2F')}/latest"
        version_key = "version"
    elif source == "github":
        repo = tool["latest_repo"]
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        version_key = "tag_name"  # 形如 "v2.43.1"
    elif source == "pypi":
        pkg = tool["latest_pkg"]
        url = f"https://pypi.org/pypi/{pkg}/json"
        version_key = None  # 从 info.version 取
    else:
        return None
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if source == "pypi":
            info = data.get("info")
            version = info.get("version") if isinstance(info, dict) else None
        else:
            version = data.get(version_key)
        return version.lstrip("v") if isinstance(version, str) else None
    except Exception as e:  # 网络/超时/解析失败一律视为查不到
        logger.warning("查询 %s 最新版本失败: %s", tool["key"], e)
        return None


def _inspect_tool(tool: dict, cmd_timeout: int, net_timeout: int) -> dict:
    """单个工具完整检测：安装 + 版本 + 最新版本 + 落后判定。"""
    result = detect_tool(tool, timeout=cmd_timeout)
    if not result["installed"]:
        result.update(latest=None, up_to_date=None)
        return result
    latest = fetch_latest(tool, timeout=net_timeout) if tool.get("latest_source") else None
    if latest is None:
        up_to_date = None  # 无最新版本信息，不做落后判定
    else:
        up_to_date = result["version"] == latest
    result.update(latest=latest, up_to_date=up_to_date)
    return result


def detect_local_environment(
    cmd_timeout: int = 10, net_timeout: int = 8, overall_timeout: int = 30
) -> dict:
    """并发检测全部工具。返回 hostname / platform / detected_at / tools 列表。

    整体超时后未返回的项直接丢弃（已返回的结果照常可用），不抛异常。
    """
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(TOOLS), 16)) as pool:
        futures = {
            pool.submit(_inspect_tool, t, cmd_timeout, net_timeout): t for t in TOOLS
        }
        for fut in as_completed(futures, timeout=overall_timeout):
            try:
                results.append(fut.result())
            except Exception as e:  # 单项检测意外异常不影响整体
                logger.warning("检测 %s 失败: %s", futures[fut]["key"], e)
    results.sort(key=lambda r: r["key"])
    return {
        "hostname": socket.gethostname(),
        "platform": f"{sys.platform} {socket.uname().machine}" if hasattr(socket, "uname") else sys.platform,
        "detected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tools": results,
    }
