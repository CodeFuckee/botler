"""任务执行环境快照（issue #276）。

任务开始时采集执行环境并落库 tasks.environment JSON，供任务详情页「元信息」
区折叠面板展示与后续按引擎版本过滤统计：
- 引擎名与版本：claude --version / hermes SDK 版本 / dsh SDK 版本（复用
  environment.detect_tool，不做网络最新版本查询，保证采集低延迟）
- 实际模型：按引擎取对应配置（dsh → worker.dsh_model；hermes → ~/.hermes
  config.yaml 的 model.default；claude → ~/.claude/settings.json 的模型配置）
- 起始 commit sha 与分支：执行开始时工作区 HEAD（prepare_workspace 之后）
- 平台版本：与前端 VersionBadge 同源的 version.json（dist/public），
  回退 data/version.txt
- config 关键项 hash：执行相关配置项的 sha256（可追溯配置差异）

采集全程尽力而为：任何单项失败只影响对应字段（warning 记录），不抛异常、
不阻塞任务执行；整体失败由调用方落库 {"error": "环境快照获取失败"} 标记。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

import yaml

from .environment import TOOLS, detect_tool

logger = logging.getLogger(__name__)

# 采集失败时落库的固定错误标记（前端据此显示「环境快照获取失败」）
SNAPSHOT_ERROR_MARKER = "环境快照获取失败"

# config 关键项 hash 覆盖的字段（执行行为相关配置，变化会导致执行差异）
CONFIG_HASH_KEYS = [
    "engine", "claude_command", "claude_args",
    "max_retries", "max_concurrent_repos", "issue_priority_labels",
    "dsh_provider", "dsh_model", "dsh_reasoning_effort", "dsh_max_tokens",
    "default_template", "resume_template", "pause_windows", "pause_weekdays",
]

# 平台版本文件候选（与前端 VersionBadge 的 /version.json 同源）：
# 1) frontend/dist/version.json（构建产物，镜像/本地构建均存在）
# 2) frontend/public/version.json（构建前生成，vite 复制进 dist）
# 3) data/version.txt（gen-version.mjs 持久化版本文件）
# 4) ${BOTLER_DATA_DIR}/version.txt（部署数据目录显式指定）
_VERSION_FILE_CANDIDATES = (
    lambda root: root / "frontend" / "dist" / "version.json",
    lambda root: root / "frontend" / "public" / "version.json",
    lambda root: root / "data" / "version.txt",
)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _config_hash(cfg) -> str:
    """config 关键项 hash：关键字段规范化 JSON 的 sha256（十六进制）。

    使用 sort_keys + ensure_ascii 保证跨运行稳定；字段缺失按 None 参与
    哈希（配置缺省与显式 None 等价，避免缺省项漂移导致 hash 抖动）。
    """
    items = {k: getattr(cfg, k, None) for k in CONFIG_HASH_KEYS}
    canonical = json.dumps(items, sort_keys=True, ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _platform_version() -> str | None:
    """平台版本号（与前端 VersionBadge 同源）。读取失败返回 None。"""
    data_dir = os.environ.get("BOTLER_DATA_DIR", "").strip()
    candidates = [fn(_PROJECT_ROOT) for fn in _VERSION_FILE_CANDIDATES]
    if data_dir:
        candidates.insert(0, Path(data_dir) / "version.txt")
    for path in candidates:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8").strip()
            if path.name == "version.json":
                data = json.loads(text)
                version = data.get("version") if isinstance(data, dict) else None
            else:
                version = text
            if isinstance(version, str) and version.strip():
                return version.strip()
        except (OSError, ValueError):
            continue
    return None


def _git_info(workdir: Path, timeout: int = 5) -> dict:
    """工作区起始提交信息：当前分支 + HEAD commit sha。

    任一命令失败（非 git 仓库/命令不存在/超时）返回空 dict（字段缺省），
    不抛异常——环境快照为尽力而为。
    """
    info: dict = {}
    try:
        result = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch:
                info["branch"] = branch
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("读取工作区分支失败: %s", e)
    try:
        result = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                info["commit_sha"] = sha
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("读取工作区 commit 失败: %s", e)
    return info


def _engine_version(engine: str) -> str | None:
    """引擎版本号：按引擎 key 在 TOOLS 清单中定位后 detect_tool 读取。

    claude → claude --version；hermes/dsh → 对应 pip 包 module 检测
    （environment.detect_tool 不做网络查询，返回快）。引擎不在清单或
    检测失败返回 None。
    """
    tool = next((t for t in TOOLS if t["key"] == engine), None)
    if tool is None:
        return None
    try:
        result = detect_tool(tool, timeout=10)
        return result.get("version")
    except Exception as e:  # noqa: BLE001 单项检测失败不影响整体
        logger.warning("读取引擎 %s 版本失败: %s", engine, e)
        return None


def _claude_model() -> str | None:
    """claude 引擎模型：~/.claude/settings.json 的模型配置（尽力而为）。

    优先顶层 model 字段（新版 CLI），回退 env.ANTHROPIC_MODEL（旧版
    settings.json 把模型放 env）；读取失败返回 None。
    """
    try:
        path = Path.home() / ".claude" / "settings.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        model = data.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
        env = data.get("env")
        model = env.get("ANTHROPIC_MODEL") if isinstance(env, dict) else None
        return model.strip() if isinstance(model, str) and model.strip() else None
    except (OSError, ValueError) as e:
        logger.warning("读取 claude 模型配置失败: %s", e)
        return None


def _hermes_model() -> dict:
    """hermes 引擎模型：~/.hermes/config.yaml 的 model.default / provider。

    直接读 YAML（不 import hermes_cli，避免额外依赖加载开销）；读取失败
    返回空 dict（模型字段缺省）。
    """
    try:
        path = Path.home() / ".hermes" / "config.yaml"
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        model_cfg = data.get("model")
        if not isinstance(model_cfg, dict):
            return {}
        out = {}
        default = model_cfg.get("default")
        if isinstance(default, str) and default.strip():
            out["name"] = default.strip()
        provider = model_cfg.get("provider")
        if isinstance(provider, str) and provider.strip():
            out["provider"] = provider.strip()
        return out
    except (OSError, ValueError) as e:
        logger.warning("读取 hermes 模型配置失败: %s", e)
        return {}


def _model_info(engine: str, cfg) -> dict:
    """实际模型信息（按引擎取对应配置，尽力而为）。

    - dsh：worker.dsh_model / dsh_provider（平台配置）
    - hermes：~/.hermes/config.yaml 的 model.default / provider
    - claude：~/.claude/settings.json 的模型配置
    其余引擎或读取失败返回空 dict。
    """
    if engine == "dsh":
        out = {}
        model = str(getattr(cfg, "dsh_model", "") or "").strip()
        if model:
            out["name"] = model
        provider = str(getattr(cfg, "dsh_provider", "") or "").strip()
        if provider:
            out["provider"] = provider
        return out
    if engine == "hermes":
        return _hermes_model()
    if engine == "claude":
        model = _claude_model()
        return {"name": model} if model else {}
    return {}


def collect_env_snapshot(engine: str, workdir: Path, cfg) -> dict:
    """采集一次任务执行环境快照（引擎版本/模型/起始提交/平台版本/配置 hash）。

    全程尽力而为：单项失败只影响对应字段并记 warning，不抛异常。调用方
    捕获到未预期异常时落库 SNAPSHOT_ERROR_MARKER 标记。
    """
    snapshot: dict = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    snapshot["engine"] = {"name": engine}
    try:
        version = _engine_version(engine)
        if version:
            snapshot["engine"]["version"] = version
    except Exception as e:  # noqa: BLE001 单项失败不影响整体
        logger.warning("采集引擎版本失败: %s", e)
    try:
        model = _model_info(engine, cfg)
        if model:
            snapshot["model"] = model
    except Exception as e:  # noqa: BLE001 单项失败不影响整体
        logger.warning("采集模型信息失败: %s", e)
    try:
        git = _git_info(workdir)
        if git:
            snapshot["git"] = git
    except Exception as e:  # noqa: BLE001 单项失败不影响整体
        logger.warning("采集起始提交失败: %s", e)
    try:
        platform_version = _platform_version()
        if platform_version:
            snapshot["platform"] = {"version": platform_version}
    except Exception as e:  # noqa: BLE001 单项失败不影响整体
        logger.warning("采集平台版本失败: %s", e)
    try:
        snapshot["config_hash"] = _config_hash(cfg)
    except Exception as e:  # noqa: BLE001 配置 hash 失败不影响其余字段
        logger.warning("计算 config hash 失败: %s", e)
    return snapshot


def serialize_snapshot(snapshot: dict) -> str:
    """快照 dict → JSON 字符串（落库 tasks.environment）。"""
    return json.dumps(snapshot, ensure_ascii=False, default=str)


def parse_snapshot(text: str | None) -> dict | None:
    """tasks.environment JSON 字符串 → dict；空/非法返回 None。"""
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def error_snapshot() -> dict:
    """采集失败时的落库标记（前端显示「环境快照获取失败」）。"""
    return {
        "error": SNAPSHOT_ERROR_MARKER,
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
