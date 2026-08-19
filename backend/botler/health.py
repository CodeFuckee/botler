"""健康检查依赖探测（issue #207）。

背景：docker compose 中 botler 主服务的 healthcheck 只探测
`GET /api/health`（uvicorn 进程活着即返回 ok），事件循环卡死 / 依赖
失效（MinIO 不可达、磁盘写满）时仍显示 healthy，容器「假死无感知」。
本模块为 /api/health 补充依赖探测：

- **MinIO 连通性**：仅当 config.yaml `minio.enabled: true`（依赖启用）时
  探测 `GET <endpoint>/minio/health/live`（与 compose minio 服务
  healthcheck 同语义，live 探针免凭据），短超时（2s）避免拖慢健康检查
  （compose healthcheck timeout=5s 必须保证探测在期限内返回）；
- **磁盘空间**：探测数据目录（BOTLER_DATA_DIR / BOTLER_CONFIG 所在目录）
  剩余空间，低于 512 MiB 视为依赖失效（防止日志/数据库/图片桶写满后
  平台带病运行）；
- GitLab 连通性**不**纳入依赖探测：GitLab 短暂不可达时平台应保持
  运行（webhook 有重试/降级），避免网络抖动导致容器被反复标记
  unhealthy 引发告警误报。

任一关键依赖 status=fail → 健康检查返回 503 + ok=false，compose
healthcheck（curl -f）随之失败，容器被标记 unhealthy——「假死有感知」。
"""
from __future__ import annotations

import os
import shutil
import urllib.request
from pathlib import Path
from typing import Any

# 依赖探测阈值
MINIO_PROBE_TIMEOUT = 2.0  # MinIO live 探测超时（秒）；healthcheck timeout=5s 内必须返回
MIN_FREE_DISK_BYTES = 512 * 1024 * 1024  # 数据目录最低剩余空间 512 MiB，低于视为依赖失效


def probe_minio(endpoint: str, secure: bool, timeout: float = MINIO_PROBE_TIMEOUT) -> dict:
    """MinIO live 探针：GET <endpoint>/minio/health/live（免凭据，与
    compose minio healthcheck 同语义）。连接失败 / 超时 / 非 200 → fail。

    :param endpoint: host:port（如 127.0.0.1:9000）
    :param secure:   endpoint 是否 https
    :param timeout:  探测超时秒数
    :return: {"status": "ok"|"fail", "detail": ...}
    """
    scheme = "https" if secure else "http"
    url = f"{scheme}://{endpoint}/minio/health/live"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec B310（与 tools.py 同款：端点来自配置白名单）
            if resp.status == 200:
                return {"status": "ok", "detail": f"{url} 返回 200"}
            return {"status": "fail", "detail": f"{url} 返回 HTTP {resp.status}"}
    except Exception as exc:  # 连接拒绝 / 超时 / DNS 解析失败等
        return {"status": "fail", "detail": f"{url} 不可达: {exc}"}


def probe_disk(path: str | Path, min_free: int = MIN_FREE_DISK_BYTES) -> dict:
    """数据目录磁盘剩余空间探测。

    :param path:     数据目录（容器内为 BOTLER_CONFIG 所在目录，即绑定
                     挂载的数据卷）
    :param min_free: 最低剩余字节数
    :return: {"status": "ok"|"fail", "free_bytes": ..., "free_mb": ...,
              "total_bytes": ..., "min_free_bytes": ...}
    """
    try:
        usage = shutil.disk_usage(str(path))
    except OSError as exc:
        return {"status": "fail", "detail": f"disk_usage 失败: {exc}"}
    return {
        "status": "ok" if usage.free >= min_free else "fail",
        "free_bytes": usage.free,
        "free_mb": usage.free // (1024 * 1024),
        "total_bytes": usage.total,
        "min_free_bytes": min_free,
    }


def default_data_dir() -> Path:
    """数据目录默认值：优先 BOTLER_DATA_DIR 环境变量（部署时数据集中到
    data/ 下），否则取 BOTLER_CONFIG 所在目录（容器内为 /app/backend，
    即数据卷挂载点）。"""
    env_dir = os.environ.get("BOTLER_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    config_path = Path(os.environ.get("BOTLER_CONFIG", "config.yaml"))
    return config_path.resolve().parent


def build_deps_report(settings: Any, data_dir: str | Path | None = None) -> dict:
    """组装依赖探测报告 deps（供 /api/health 返回）。

    - MinIO 未启用（minio_enabled=false）→ status=skipped，不参与成败
      判定（可选依赖不拖累健康检查，与 image_store 语义一致）；
    - MinIO 启用 → 探测 live 端点；
    - 磁盘空间始终探测。

    :param settings: Settings（config.yaml），需含 minio_enabled /
                     minio_endpoint / minio_secure 属性
    :param data_dir: 数据目录（缺省按 default_data_dir 推断）
    :return: {"minio": {...}, "disk": {...}}
    """
    deps: dict[str, dict] = {}
    if getattr(settings, "minio_enabled", False):
        endpoint = str(getattr(settings, "minio_endpoint", "") or "127.0.0.1:9000")
        secure = bool(getattr(settings, "minio_secure", False))
        deps["minio"] = probe_minio(endpoint, secure)
    else:
        deps["minio"] = {
            "status": "skipped",
            "detail": "minio 未启用（config.yaml minio.enabled=false），跳过探测",
        }
    deps["disk"] = probe_disk(data_dir or default_data_dir())
    return deps


def deps_critical_failed(deps: dict) -> bool:
    """是否存在关键依赖失败（任一 status=fail）。skipped/ok 不算失败。"""
    return any(
        isinstance(item, dict) and item.get("status") == "fail"
        for item in deps.values()
    )
