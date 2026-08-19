"""平台版本信息读取与健康检查负载构建（issue #233）。

版本数据链路：前端构建时 `frontend/scripts/gen-version.mjs` 把版本号 +
构建时间 + commit 写入 `frontend/public/version.json`，vite 构建时自动
复制进 `dist/version.json`（FastAPI 静态托管目录）；后端 `/api/health`
优先读取同一份构建产物，保证前后端版本号一致（验收标准「/api/health
版本一致」，排查「这个功能部署了吗」依赖版本可见）。

读取优先级（逐级回退，健康检查永远可用、绝不抛异常）：
  1. 构建产物 `frontend/dist/version.json`（版本 + 构建时间 + commit，
     与前端 VersionBadge 同源）；
  2. 持久化 `data/version.txt`（gen-version.mjs 版本自增的同一文件，
     本地开发 / CI 测试无 dist 产物时兜底，仅版本号）；
  3. 均不可用 → 返回 None，健康检查仍返回 ok + version="0.0.0"
     （服务本身可用，只是版本信息缺失）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_version_info(
    version_json_path: str | Path,
    version_txt_path: str | Path | None = None,
) -> dict[str, str] | None:
    """读取版本信息：优先构建产物 version.json（版本 + 构建时间 + commit），
    回退 data/version.txt（仅版本号）；均不可用时返回 None。

    version_json_path —— frontend/dist/version.json 等构建产物路径；
    version_txt_path  —— data/version.txt 回退路径（可缺省）。
    容错：文件缺失 / 非法 JSON / 字段为空一律静默降级，不抛异常。
    """
    json_path = Path(version_json_path)
    if json_path.is_file():
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            version = str(raw.get("version", "")).strip()
            if version:
                info: dict[str, str] = {"version": version}
                # buildTime / commit 可选字段：缺失或空串不写入
                for key in ("buildTime", "commit"):
                    value = raw.get(key)
                    if isinstance(value, str) and value.strip():
                        info[key] = value.strip()
                return info

    if version_txt_path is not None:
        txt_path = Path(version_txt_path)
        if txt_path.is_file():
            try:
                version = txt_path.read_text(encoding="utf-8").strip()
            except OSError:
                version = ""
            if version:
                return {"version": version}
    return None


def build_health_payload(
    version_info: dict[str, str] | None,
    scheduler_stats: Any = None,
    task_stats: Any = None,
    deps: dict[str, Any] | None = None,
    ok: bool = True,
) -> dict[str, Any]:
    """组装 /api/health 响应：ok + 版本号（无版本信息时 0.0.0）+ 构建
    信息 build（buildTime/commit 可选）+ 调度/任务统计（可缺省）+ 依赖
    探测 deps（issue #207：MinIO 连通 / 磁盘空间，关键依赖失败时调用方
    置 ok=false 并返回 503）。

    拆成纯函数便于单测：不依赖 FastAPI app / 真实 ctx。
    """
    payload: dict[str, Any] = {
        "ok": ok,
        "version": version_info["version"] if version_info else "0.0.0",
    }
    if version_info:
        payload["build"] = version_info
    if deps is not None:
        payload["deps"] = deps
    if scheduler_stats is not None:
        payload["scheduler"] = scheduler_stats
    if task_stats is not None:
        payload["tasks"] = task_stats
    return payload
