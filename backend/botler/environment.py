"""本地环境检测（issue #22）：检测服务器上常见 AI agent / 基础工具是否安装及其版本。

实现方式：shutil.which 定位可执行文件 → 执行 <工具> --version 解析版本号；
已安装且带发布源的 agent 工具再并发查询最新版本（npm registry / GitHub API）。
所有子步骤均容错：任何失败只影响单项字段，不中断整体检测。
"""

from __future__ import annotations

import io
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
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
    # hermes 为 hermes-agent SDK（issue #171 起进程内集成：源码 editable
    # 安装进 botler venv，进程内调用 run_agent.AIAgent），不走 which/
    # --version，按 module 检测；无 npm/GitHub 公开发布源，不查最新版本
    # （前端显示 "—"，与 dsh 的 latest_source: None 同模式）
    {"key": "hermes", "name": "Hermes Agent SDK", "module": "run_agent",
     "pkg": "hermes-agent", "latest_source": None},
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
        # issue #470：pip 包版本保留完整 PEP 440 版本号（含 rc 等预发布
        # 后缀），不做 parse_version 截断——截断会使 rc 版本与 PyPI 最新
        # 版本比较永远不等（up_to_date 恒为 False），前端永远显示
        # 「可升级」，且升级后页面版本号看起来「没有变化」。
        base["version"] = pkg_version(pkg)
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
    """单个工具完整检测：安装 + 版本 + 最新版本 + 落后判定 + 可安装标记。

    installable（issue #468）：工具配置了自动安装来源（npm / pypi /
    github）即为可自动安装，前端据此对未安装工具显示「安装」按钮。
    """
    result = detect_tool(tool, timeout=cmd_timeout)
    result["installable"] = tool.get("latest_source") is not None
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


# ============================================================
# 工具升级（issue #465）：设置页「本地环境检测」对可升级版本工具
# 提供一键升级。升级方式按发布源分派：
#   - npm  → npm install -g <pkg>@latest（claude/codex/gemini/aider）
#   - pypi → 当前解释器 pip install -U <pkg>（dsh SDK，进程内依赖）
#   - github → 下载 gh 最新 release 二进制替换（仅 Linux）
# 升级成功后由 API 层调度延迟重启 botler 服务（与备份恢复同模式），
# 使进程内依赖（dsh SDK 等）新版本生效。
# ============================================================

class UpgradeError(Exception):
    """工具升级失败（用户可读中文信息，API 层转 HTTP 400）。"""


# npm/pip 安装可能较慢，升级命令超时放宽到 300s
UPGRADE_TIMEOUT = 300
# gh 二进制下载超时（秒）
GH_DOWNLOAD_TIMEOUT = 60
# 返回给前端的命令输出截断长度（避免长日志刷屏）
MAX_CMD_OUTPUT = 500


def find_tool(tool_key: str) -> dict | None:
    """按 key 查找工具清单项；不存在返回 None。"""
    return next((t for t in TOOLS if t["key"] == tool_key), None)


# pypi 工具（dsh SDK 等）pip 镜像源（issue #470）：默认 pip 源（清华）
# 未同步 rc 预发布版，升级会静默 "already satisfied" 假成功；显式走
# 阿里源（与 deploy/install-dsh-sdk.sh / Dockerfile 部署基线一致），
# 可用 DSH_INDEX_URL 环境变量覆盖（内网代理场景）。
DSH_INDEX_URL_DEFAULT = "https://mirrors.aliyun.com/pypi/simple"


def _pip_index_url() -> str:
    """pypi 工具升级/安装的 pip 镜像源：DSH_INDEX_URL 优先，缺省阿里源。"""
    return os.environ.get("DSH_INDEX_URL") or DSH_INDEX_URL_DEFAULT


def _upgrade_command(tool: dict) -> list[str]:
    """按发布源构造升级命令（npm / pypi）；无发布源抛 UpgradeError。"""
    source = tool.get("latest_source")
    if source == "npm":
        npm = shutil.which("npm")
        if not npm:
            raise UpgradeError("未找到 npm 命令，无法升级 npm 全局工具")
        return [npm, "install", "-g", f"{tool['latest_pkg']}@latest"]
    if source == "pypi":
        # 使用 botler 当前解释器升级进程内 pip 包，保证升级进同一环境。
        # issue #470：dsh SDK 为 rc 预发布版，必须 --pre 允许预发布版本，
        # 并显式指定镜像源——默认 pip 源（清华）不同步 rc 版，不带源时
        # pip 静默 "already satisfied" 退出码 0，造成「显示升级成功但
        # 版本号未变」的假成功（源与部署基线 deploy/install-dsh-sdk.sh
        # 一致，DSH_INDEX_URL 可覆盖）。
        return [sys.executable, "-m", "pip", "install", "-U", "--pre",
                tool["latest_pkg"], "-i", _pip_index_url()]
    raise UpgradeError(f"工具「{tool['name']}」没有可用的自动升级途径")


def _run_upgrade_command(tool: dict, cmd: list[str], timeout: int) -> dict:
    """执行升级命令：非零退出/超时/执行异常均抛 UpgradeError。"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise UpgradeError(f"升级「{tool['name']}」超时（超过 {timeout}s）") from None
    except OSError as e:
        raise UpgradeError(f"执行升级命令失败: {e}") from None
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        raise UpgradeError(
            f"升级「{tool['name']}」失败（退出码 {result.returncode}）: "
            f"{output[-MAX_CMD_OUTPUT:]}")
    return _upgrade_result(tool, output)


def _upgrade_result(tool: dict, output: str) -> dict:
    """升级成功结果：附带命令输出与升级后重新检测的版本（检测失败不阻断）。"""
    result = {"key": tool["key"], "name": tool["name"], "upgraded": True}
    if output:
        result["output"] = output[-MAX_CMD_OUTPUT:]
    try:
        detected = detect_tool(tool, timeout=10)
        result["version"] = detected.get("version")
    except Exception:  # noqa: BLE001  重新检测失败仅版本未知，不阻断升级结果
        result["version"] = None
    return result


def _gh_arch() -> str:
    """当前 CPU 架构映射到 gh release 资产命名（amd64/arm64）。"""
    machine = platform.machine().lower()
    mapping = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    arch = mapping.get(machine)
    if arch is None:
        raise UpgradeError(f"不支持的 CPU 架构: {machine}")
    return arch


def _extract_gh_binary(content: bytes) -> bytes:
    """从 gh release tar.gz 中提取 bin/gh 可执行文件内容。"""
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tf:
        for member in tf.getmembers():
            if member.isfile() and member.name.endswith("/bin/gh"):
                data = tf.extractfile(member)
                if data is not None:
                    return data.read()
    raise ValueError("升级包中未找到 bin/gh")


def _upgrade_gh(tool: dict) -> dict:
    """升级 GitHub CLI：下载最新 release 二进制原子替换当前可执行文件。

    仅支持 Linux（botler 部署目标均为 Linux 服务器/容器）；替换需要
    对 gh 所在目录的写权限（通常为 root 安装的 /usr/bin）。
    """
    if not sys.platform.startswith("linux"):
        raise UpgradeError("gh 自动升级当前仅支持 Linux 平台")
    latest = fetch_latest(tool, timeout=8)
    if latest is None:
        raise UpgradeError("查询 gh 最新版本失败（网络不可达或发布源异常）")
    current = shutil.which("gh")
    if not current:
        raise UpgradeError("未找到 gh 可执行文件")
    arch = _gh_arch()
    url = (f"https://github.com/{tool['latest_repo']}/releases/download/"
           f"v{latest}/gh_{latest}_linux_{arch}.tar.gz")
    try:
        resp = httpx.get(url, timeout=GH_DOWNLOAD_TIMEOUT, follow_redirects=True)
    except Exception as e:  # noqa: BLE001  网络异常统一转可读错误
        raise UpgradeError(f"下载 gh 升级包失败: {e}") from None
    if resp.status_code != 200:
        raise UpgradeError(f"下载 gh 升级包失败（HTTP {resp.status_code}）")
    try:
        payload = _extract_gh_binary(resp.content)
    except Exception as e:  # noqa: BLE001  解包异常统一转可读错误
        raise UpgradeError(f"解析 gh 升级包失败: {e}") from None
    tmp = f"{current}.botler-upgrade-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
        # nosec B103：gh 发行版二进制本身即 0755 可执行权限位，此处按
        # 原样恢复可执行权限（文件是临时路径，替换后立即生效，无越权风险）
        os.chmod(tmp, 0o755)  # nosec B103
        os.replace(tmp, current)
    except OSError as e:
        raise UpgradeError(f"替换 gh 可执行文件失败（需要写权限）: {e}") from None
    return _upgrade_result(tool, f"已替换为 gh {latest}（{current}）")


def upgrade_tool(tool_key: str, timeout: int = UPGRADE_TIMEOUT) -> dict:
    """升级单个工具到最新版本（issue #465）。

    按发布源分派：npm 全局包走 npm install -g，pip 包走当前解释器
    pip install -U，gh 走 GitHub release 二进制下载替换。工具不存在或
    无发布源抛 UpgradeError（API 层转 400）。
    """
    tool = find_tool(tool_key)
    if tool is None:
        raise UpgradeError(f"未知工具: {tool_key}")
    if tool.get("latest_source") == "github":
        return _upgrade_gh(tool)
    # issue #470：pypi 工具（dsh SDK）先比对已装版本与 PyPI 最新版，
    # 已是最新直接返回（不执行 pip、不重启），避免「显示升级成功但
    # 版本号未变」的假成功。比对失败（网络异常等）不阻断正常升级。
    if tool.get("latest_source") == "pypi":
        detected = detect_tool(tool, timeout=10)
        latest = fetch_latest(tool, timeout=8)
        if (detected.get("installed") and latest
                and detected.get("version") == latest):
            return {"key": tool["key"], "name": tool["name"],
                    "upgraded": False, "already_up_to_date": True,
                    "version": detected.get("version")}
    return _run_upgrade_command(tool, _upgrade_command(tool), timeout)


# 进程生命周期内只调度一次重启（连续升级多个工具不重复重启）
_restart_scheduled = False


def schedule_restart(delay: float = 2.0) -> bool:
    """延迟重启 botler 服务（os.execv 原地替换，与备份恢复同模式）。

    Docker（restart policy 依赖进程退出）下 execv 不退出容器进程本身，
    pm2/systemd/uvicorn --reload 场景同样成立；新进程重新加载
    config.yaml / botler.db，scheduler 自动把 running 任务重新入队。
    返回是否本次实际调度（进程内重复调用返回 False）。
    """
    global _restart_scheduled
    if _restart_scheduled:
        return False
    _restart_scheduled = True

    def _do() -> None:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            logging.shutdown()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:  # noqa: BLE001  execv 失败（理论上极罕见）
            logger.error("进程重启失败: %s", e)
            os._exit(1)  # noqa: PLR1722  让容器 restart policy 兜底

    threading.Timer(delay, _do).start()
    logger.info("工具升级完成，%.1fs 后自动重启服务", delay)
    return True


# ============================================================
# 工具安装（issue #468）：设置页「本地环境检测」对「未安装且具备自动安装
# 来源」的工具提供一键安装。安装方式按发布源分派（与升级 issue #465 对齐）：
#   - npm  → npm install -g <pkg>@latest（claude/codex/gemini/aider）
#   - pypi → 当前解释器 pip install <pkg>（dsh SDK，进程内依赖）
#   - github → 下载 gh 最新 release 二进制安装到 /usr/local/bin（仅 Linux）
# 安装成功后由 API 层调度延迟重启 botler 服务（与升级/备份恢复同模式），
# 使进程内依赖（dsh SDK 等）新安装的版本生效。
# ============================================================

class InstallError(Exception):
    """工具安装失败（用户可读中文信息，API 层转 HTTP 400）。"""


# npm/pip 安装可能较慢，安装命令超时放宽到 300s
INSTALL_TIMEOUT = 300
# gh 二进制默认安装目录（Linux 标准系统 PATH 目录，通常 root 可写）
GH_INSTALL_DIR = "/usr/local/bin"


def _install_command(tool: dict) -> list[str]:
    """按发布源构造安装命令（npm / pypi）；无发布源抛 InstallError。"""
    source = tool.get("latest_source")
    if source == "npm":
        npm = shutil.which("npm")
        if not npm:
            raise InstallError("未找到 npm 命令，无法安装 npm 全局工具")
        return [npm, "install", "-g", f"{tool['latest_pkg']}@latest"]
    if source == "pypi":
        # 使用 botler 当前解释器安装进程内 pip 包，保证装进同一环境。
        # issue #470：dsh SDK 为 rc 预发布版，必须 --pre 允许预发布版本，
        # 并显式指定镜像源（与升级同策略，DSH_INDEX_URL 可覆盖）。
        return [sys.executable, "-m", "pip", "install", "--pre",
                tool["latest_pkg"], "-i", _pip_index_url()]
    raise InstallError(f"工具「{tool['name']}」没有可用的自动安装途径")


def _run_install_command(tool: dict, cmd: list[str], timeout: int) -> dict:
    """执行安装命令：非零退出/超时/执行异常均抛 InstallError。"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise InstallError(f"安装「{tool['name']}」超时（超过 {timeout}s）") from None
    except OSError as e:
        raise InstallError(f"执行安装命令失败: {e}") from None
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        raise InstallError(
            f"安装「{tool['name']}」失败（退出码 {result.returncode}）: "
            f"{output[-MAX_CMD_OUTPUT:]}")
    return _install_result(tool, output)


def _install_result(tool: dict, output: str) -> dict:
    """安装成功结果：附带命令输出与安装后重新检测的版本（检测失败不阻断）。"""
    result = {"key": tool["key"], "name": tool["name"], "installed": True}
    if output:
        result["output"] = output[-MAX_CMD_OUTPUT:]
    try:
        detected = detect_tool(tool, timeout=10)
        result["version"] = detected.get("version")
    except Exception:  # noqa: BLE001  重新检测失败仅版本未知，不阻断安装结果
        result["version"] = None
    return result


def _install_gh(tool: dict) -> dict:
    """安装 GitHub CLI：下载最新 release 二进制原子写入 /usr/local/bin/gh。

    仅支持 Linux（botler 部署目标均为 Linux 服务器/容器）；安装目录需
    已存在且可写（通常为 root 安装的 /usr/local/bin）。
    """
    if not sys.platform.startswith("linux"):
        raise InstallError("gh 自动安装当前仅支持 Linux 平台")
    latest = fetch_latest(tool, timeout=8)
    if latest is None:
        raise InstallError("查询 gh 最新版本失败（网络不可达或发布源异常）")
    arch = _gh_arch()
    url = (f"https://github.com/{tool['latest_repo']}/releases/download/"
           f"v{latest}/gh_{latest}_linux_{arch}.tar.gz")
    try:
        resp = httpx.get(url, timeout=GH_DOWNLOAD_TIMEOUT, follow_redirects=True)
    except Exception as e:  # noqa: BLE001  网络异常统一转可读错误
        raise InstallError(f"下载 gh 安装包失败: {e}") from None
    if resp.status_code != 200:
        raise InstallError(f"下载 gh 安装包失败（HTTP {resp.status_code}）")
    try:
        payload = _extract_gh_binary(resp.content)
    except Exception as e:  # noqa: BLE001  解包异常统一转可读错误
        raise InstallError(f"解析 gh 安装包失败: {e}") from None
    if not os.path.isdir(GH_INSTALL_DIR):
        raise InstallError(f"gh 安装目录 {GH_INSTALL_DIR} 不存在，请先创建")
    if not os.access(GH_INSTALL_DIR, os.W_OK):
        raise InstallError(f"gh 安装目录 {GH_INSTALL_DIR} 不可写（需要 root 权限）")
    dest = os.path.join(GH_INSTALL_DIR, "gh")
    tmp = f"{dest}.botler-install-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
        # nosec B103：gh 发行版二进制本身即 0755 可执行权限位，此处按
        # 原样恢复可执行权限（文件是临时路径，替换后立即生效，无越权风险）
        os.chmod(tmp, 0o755)  # nosec B103
        os.replace(tmp, dest)
    except OSError as e:
        raise InstallError(f"写入 gh 可执行文件失败: {e}") from None
    return _install_result(tool, f"已安装 gh {latest}（{dest}）")


def install_tool(tool_key: str, timeout: int = INSTALL_TIMEOUT) -> dict:
    """安装单个工具到最新版本（issue #468）。

    按发布源分派：npm 全局包走 npm install -g，pip 包走当前解释器
    pip install，gh 走 GitHub release 二进制下载安装到 /usr/local/bin。
    工具不存在 / 已安装 / 无发布源抛 InstallError（API 层转 400）。
    """
    tool = find_tool(tool_key)
    if tool is None:
        raise InstallError(f"未知工具: {tool_key}")
    if detect_tool(tool, timeout=10)["installed"]:
        raise InstallError(f"工具「{tool['name']}」已安装，无需重复安装")
    if tool.get("latest_source") == "github":
        return _install_gh(tool)
    return _run_install_command(tool, _install_command(tool), timeout)
