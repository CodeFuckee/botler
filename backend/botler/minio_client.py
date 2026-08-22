"""MinIO 对象存储客户端（issue #163）。

识图模型调用时，用户上传的图片先计算 SHA-256 哈希，以哈希值作为对象名
上传到 MinIO（同内容图片对象名相同，天然幂等去重：已存在则不重复上传），
再以 http(s) URL 形式传给识图模型（OpenAI 兼容 image_url），替代直接把
base64 塞进请求体的做法——base64 图片数据可达数十万字符，网关/模型对
请求体大小敏感，且会撑爆错误提示（issue #156 的截断展示也是为此）。

配置来自 config.yaml 的 minio 段（凭据支持 ${ENV} 引用，与 gitlab /
sso 同模式；access_key / secret_key 留空时回退环境变量
MINIO_ROOT_USER / MINIO_ROOT_PASSWORD，与部署写入 data/backend/.env
的凭据同源）：

    minio:
      enabled: true                     # 识图图片上传开关
      endpoint: "127.0.0.1:9000"        # MinIO API 地址（host:port）
      secure: false                     # endpoint 是否 https
      access_key: ${MINIO_ROOT_USER}    # 访问凭据
      secret_key: ${MINIO_ROOT_PASSWORD}
      bucket: public                   # 图片对象桶（不存在自动创建；桶权限
                                        #   自动设为公开只读，识图模型可匿名取图）
      public_base_url: ""               # 识图模型取图的 http(s) 前缀（建议经
                                        #   nginx 代理 MinIO 桶，如
                                        #   https://gitlab.example.com/minio-public），
                                        #   对象 URL =
                                        #   public_base_url/bucket/<sha256 哈希>
      verify_ssl: true                  # endpoint 证书校验（自签证书设 false）

issue #164：OpenAI 兼容识图模型（openai_vision / custom）不再支持 base64
内联图片——网关会拒绝 data: URL（如阿里云百炼 qwen 报 "url error"）。未
启用 / 配置不完整时，识图调用会明确报错引导启用 MinIO（不再静默回退
base64）；Gemini 官方 generateContent 接口仅支持 base64 inline_data
（Google API 限制），保持 base64 内联输入。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("botler.minio_client")

# 环境变量回退键：与部署（issue #160）写入 data/backend/.env 的凭据同源
_ENV_ACCESS_KEY = "MINIO_ROOT_USER"
_ENV_SECRET_KEY = "MINIO_ROOT_PASSWORD"
_ENV_ENDPOINT = "MINIO_ENDPOINT"
_ENV_BUCKET = "MINIO_BUCKET"
_ENV_PUBLIC_BASE_URL = "MINIO_PUBLIC_BASE_URL"


class MinioStoreError(RuntimeError):
    """图片上传 MinIO 失败（连接 / 凭据 / 桶创建 / 对象写入等）。"""


@dataclass
class MinioConfig:
    """MinIO 连接与识图图片上传配置（来自 config.yaml minio 段）。"""

    enabled: bool = False
    endpoint: str = "127.0.0.1:9000"
    secure: bool = False
    access_key: str = ""
    secret_key: str = ""
    bucket: str = "public"
    public_base_url: str = ""
    verify_ssl: bool = True

    def is_usable(self) -> bool:
        """配置是否可用于识图图片上传（开关 + 端点 + 凭据 + 公网前缀）。

        public_base_url 是识图模型取图的访问前缀，缺失时无法构造 http
        URL，视为不可用（回退 base64 内联输入）。
        """
        return bool(
            self.enabled
            and str(self.endpoint or "").strip()
            and str(self.access_key or "").strip()
            and str(self.secret_key or "").strip()
            and str(self.public_base_url or "").strip()
        )


def config_from_settings(settings: Any) -> MinioConfig:
    """从 Settings（config.yaml）构造 MinIO 配置，凭据回退环境变量。"""
    return MinioConfig(
        enabled=bool(settings.minio_enabled),
        endpoint=str(settings.minio_endpoint
                     or os.environ.get(_ENV_ENDPOINT, "127.0.0.1:9000")).strip(),
        secure=bool(settings.minio_secure),
        access_key=str(settings.minio_access_key
                       or os.environ.get(_ENV_ACCESS_KEY, "")).strip(),
        secret_key=str(settings.minio_secret_key
                       or os.environ.get(_ENV_SECRET_KEY, "")).strip(),
        bucket=str(settings.minio_bucket
                   or os.environ.get(_ENV_BUCKET, "public")).strip(),
        public_base_url=str(settings.minio_public_base_url
                            or os.environ.get(_ENV_PUBLIC_BASE_URL, "")).strip(),
        verify_ssl=bool(settings.minio_verify_ssl),
    )


def image_store_from_settings(settings: Any) -> MinioImageStore | None:
    """按 Settings 构造识图图片存储；未启用/配置不完整返回 None。

    None = 未配置 MinIO 图片上传。issue #164 起 OpenAI 兼容识图模型
    （openai_vision / custom）不再支持 base64 内联——image_store 为
    None 时识图调用会明确报错引导启用 MinIO（不静默回退）；仅 Gemini
    官方接口（不支持 http URL）保持 base64 inline_data 内联输入。
    启用但配置不完整（缺 endpoint / 凭据 / public_base_url）时记
    warning 日志提示，不抛错不阻塞。
    """
    cfg = config_from_settings(settings)
    if not cfg.enabled:
        return None
    if not cfg.is_usable():
        logger.warning(
            "MinIO 已启用但配置不完整（需 endpoint / access_key / "
            "secret_key / public_base_url），识图图片上传不可用——OpenAI "
            "兼容识图模型将报错引导启用 MinIO（不再回退 base64 内联，"
            "issue #164）")
        return None
    return MinioImageStore(cfg)


class MinioImageStore:
    """识图图片对象存储：SHA-256 哈希命名 + 幂等上传，返回 http URL。

    :meth:`put_image` 接收图片字节，计算 SHA-256 哈希（对象名 = 哈希值，
    issue #163「文件名为哈希值」），桶不存在自动创建，对象已存在跳过
    上传（同内容图片复用，避免重复写盘），返回
    ``<public_base_url>/<bucket>/<哈希>`` 形式的 http(s) URL。

    ``client`` 参数供测试注入 mock（缺省按配置惰性创建 minio 客户端）。
    """

    def __init__(self, cfg: MinioConfig, client: Any | None = None) -> None:
        self.cfg = cfg
        self._client = client
        # 桶公开只读策略是否已应用（实例内只设置一次，幂等）
        self._policy_applied = False

    @property
    def client(self) -> Any:
        """minio SDK 客户端（惰性创建，测试可注入 mock 替代）。"""
        if self._client is None:
            from minio import Minio
            self._client = Minio(
                self.cfg.endpoint,
                access_key=self.cfg.access_key,
                secret_key=self.cfg.secret_key,
                secure=self.cfg.secure,
                cert_check=self.cfg.verify_ssl,
            )
        return self._client

    def put_image(self, data: bytes, mime_type: str = "image/png") -> str:
        """上传图片并返回 http(s) URL。

        :param data: 图片原始字节
        :param mime_type: 图片 MIME 类型（如 image/png / image/jpeg），
            随对象一并写入 Content-Type，模型经 URL 拉取时类型正确
        :return: 形如 ``<public_base_url>/<bucket>/<sha256 哈希>`` 的 URL
        :raises MinioStoreError: 图片为空 / MinIO 连接、凭据或写入失败
        """
        if not data:
            raise MinioStoreError("图片内容为空，无法上传 MinIO")
        digest = hashlib.sha256(data).hexdigest()
        try:
            self._ensure_bucket()
            self._ensure_public_read_policy()
            if not self._object_exists(digest):
                self.client.put_object(
                    self.cfg.bucket,
                    digest,
                    io.BytesIO(data),
                    len(data),
                    content_type=mime_type or "application/octet-stream",
                )
                logger.info("识图图片已上传 MinIO: %s/%s", self.cfg.bucket, digest)
            else:
                logger.info("识图图片已存在，复用 MinIO 对象: %s/%s",
                            self.cfg.bucket, digest)
        except MinioStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 底层错误统一转业务异常
            raise MinioStoreError(f"图片上传 MinIO 失败: {exc}") from exc
        return (f"{self.cfg.public_base_url.rstrip('/')}/"
                f"{self.cfg.bucket}/{digest}")

    def _ensure_bucket(self) -> None:
        """桶不存在则自动创建（首次上传时；默认桶名 public，issue #164）。"""
        if not self.client.bucket_exists(self.cfg.bucket):
            self.client.make_bucket(self.cfg.bucket)
            logger.info("MinIO 桶已自动创建: %s", self.cfg.bucket)

    def _ensure_public_read_policy(self) -> None:
        """把桶权限设置为公开只读（匿名 s3:GetObject，issue #164）。

        识图模型（含外部公网网关）需要能匿名访问图片 URL 才能取图，
        桶对象策略固定为「公开只读」。设置动作幂等（重复设置覆盖为同一
        策略），实例内首次上传后缓存标志，避免每次上传都调用。
        """
        if self._policy_applied:
            return
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{self.cfg.bucket}/*"],
            }],
        }
        try:
            self.client.set_bucket_policy(
                self.cfg.bucket, json.dumps(policy))
        except Exception as exc:  # noqa: BLE001 底层错误统一转业务异常
            raise MinioStoreError(
                f"设置 MinIO 桶「{self.cfg.bucket}」公开只读权限失败: {exc}"
                "（识图模型需匿名读图片 URL，请检查 MinIO 凭据权限）"
            ) from exc
        self._policy_applied = True
        logger.info("MinIO 桶「%s」已设置为公开只读", self.cfg.bucket)

    def _object_exists(self, name: str) -> bool:
        """对象是否已存在（幂等去重：同内容图片不重复上传）。"""
        try:
            self.client.stat_object(self.cfg.bucket, name)
            return True
        except Exception:  # noqa: BLE001 不存在/无权限统一视为未存在
            return False
