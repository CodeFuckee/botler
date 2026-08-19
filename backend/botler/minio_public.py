"""识图图片公网访问端点（issue #319）。

识图模型调用时图片以 http URL 传入（issue #163/#164）：后端把图片哈希
上传 MinIO 图片桶，识图请求的 image_url 形如
``https://<站点>/minio-public/<bucket>/<sha256 哈希>``（阿里云百炼 qwen
等 OpenAI 兼容网关经该 URL 匿名取图）。

此前 ``/minio-public/`` 在 FastAPI 没有对应路由，请求被 SPA 兜底
（main.py 的 ``/{full_path:path}`` → index.html）当成前端路由吞掉，返回
text/html 页面——外部模型网关取图拿到 HTML，报 ``url error, please check
url！``（issue #319 现象：设置页「识图模型」测试按钮失败，日志见 issue）。
issue #311 的修复只更新了 ``deploy/nginx-minio-public.conf``（需把
location 合并进站点 server 块），Synology DSM 等可视化反向代理无法配置
自定义 nginx location，实际部署未生效——图片 URL 仍被 SPA 兜底吞掉。

本模块新增 ``GET /minio-public/{bucket}/{object_name:path}`` 路由：后端
直接流式返回 MinIO 图片桶对象，图片 URL 经站点反向代理直通后端即可取图，
不再依赖外部 nginx location 配置。安全约束：

- 只允许访问配置的图片桶（``minio.bucket``），不暴露 MinIO 其他私有桶；
- 对象名拒绝空段 / ``..`` 段 / 反斜杠（防路径穿越与无意义请求）；
- 对象不存在 / 桶未启用 / 桶不匹配 → 404；MinIO 服务异常 → 502
  （统一 JSON，不裸抛 500）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from minio.error import S3Error

logger = logging.getLogger("botler.minio_public")

router = APIRouter()

# 流式分块大小（与 minio SDK 常见读取一致，64KB）
_CHUNK_SIZE = 64 * 1024


def _image_store(request: Request):
    """按当前配置构造识图图片存储；未启用/配置不完整返回 None。

    每次请求读取最新配置（设置页保存后无需重启即生效）；Minio 客户端
    构造仅存配置不建连，按请求创建开销可忽略。
    """
    from .minio_client import image_store_from_settings
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        return None
    return image_store_from_settings(ctx.config.get())


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@router.get("/minio-public/{bucket}/{object_name:path}")
def serve_minio_public_object(request: Request, bucket: str, object_name: str):
    """流式返回识图图片 URL（/minio-public/<bucket>/<对象名>）指向的图片。

    - bucket 必须等于配置的图片桶（minio.bucket），其他桶一律 404；
    - 支持 Range（模型/浏览器分段取图），透传 206 与 Content-Range；
    - Content-Type / ETag / Content-Length 取自 MinIO 对象元数据，
      Cache-Control 公开缓存（图片内容为公开只读，issue #164）。
    """
    store = _image_store(request)
    if store is None or bucket != store.cfg.bucket:
        return _error(404, "图片对象不存在")
    parts = object_name.split("/")
    if (not object_name or any(p in ("", ".", "..") for p in parts)
            or "\\" in object_name):
        return _error(400, "非法的图片对象名")
    key = "/".join(parts)
    try:
        stat = store.client.stat_object(bucket, key)
    except S3Error as exc:
        if exc.code == "NoSuchKey":
            return _error(404, "图片对象不存在")
        logger.warning("识图图片取元数据失败 %s/%s: %s", bucket, key, exc)
        return _error(502, "图片存储服务异常")
    except Exception as exc:  # noqa: BLE001 底层错误统一降级
        logger.warning("识图图片取元数据失败 %s/%s: %s", bucket, key, exc)
        return _error(502, "图片存储服务异常")
    headers: dict[str, str] = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    try:
        obj = store.client.get_object(bucket, key, request_headers=headers or None)
    except S3Error as exc:
        if exc.code == "NoSuchKey":
            return _error(404, "图片对象不存在")
        logger.warning("识图图片读取失败 %s/%s: %s", bucket, key, exc)
        return _error(502, "图片存储服务异常")
    except Exception as exc:  # noqa: BLE001 底层错误统一降级
        logger.warning("识图图片读取失败 %s/%s: %s", bucket, key, exc)
        return _error(502, "图片存储服务异常")
    resp_headers: dict[str, str] = {
        "Cache-Control": "public, max-age=86400",
        "Content-Type": (obj.headers.get("Content-Type")
                         or getattr(stat, "content_type", None)
                         or "application/octet-stream"),
        "Accept-Ranges": "bytes",
    }
    for name in ("Content-Length", "ETag", "Content-Range"):
        if obj.headers.get(name):
            resp_headers[name] = obj.headers[name]

    def iter_body():
        try:
            for chunk in obj.stream(_CHUNK_SIZE):
                yield chunk
        finally:
            try:
                obj.close()
                obj.release_conn()
            except Exception:  # noqa: BLE001 关闭失败不影响响应
                pass

    return StreamingResponse(
        iter_body(), status_code=obj.status, headers=resp_headers)
