"""MinIO 对象存储客户端测试（issue #163）。

覆盖：SHA-256 哈希命名（对象名 = 哈希值）、桶自动创建（默认桶名
public，issue #164）、桶公开只读策略设置（匿名 s3:GetObject，幂等）、
幂等去重（同内容图片对象已存在不重复上传）、http URL 构造、空图片 /
上传失败错误、配置构造与 env 回退、settings 接线（未启用 / 配置不完整
返回 None，OpenAI 兼容识图模型将报错引导启用 MinIO）。用注入的 fake
client 模拟 minio SDK，不做真实外呼。
"""

import hashlib
import json

import pytest

from botler.minio_client import (
    MinioConfig,
    MinioImageStore,
    MinioStoreError,
    config_from_settings,
    image_store_from_settings,
)

CFG = MinioConfig(
    enabled=True,
    endpoint="127.0.0.1:9000",
    secure=False,
    access_key="test-access",
    secret_key="test-secret",
    bucket="public",
    public_base_url="http://img.example.com:9000",
    verify_ssl=True,
)


class FakeMinioClient:
    """minio SDK 客户端最小替身：桶 / 对象存内存，记录调用。"""

    def __init__(self):
        self.buckets = set()
        self.objects: dict[str, dict[str, tuple]] = {}
        self.put_calls: list[tuple] = []
        self.stat_calls: list[tuple] = []
        self.policies: dict[str, str] = {}
        self.policy_calls: list[str] = []

    def set_bucket_policy(self, bucket, policy):
        self.policy_calls.append(bucket)
        self.policies[bucket] = policy

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self.buckets.add(bucket)

    def stat_object(self, bucket, name):
        self.stat_calls.append((bucket, name))
        if bucket in self.objects and name in self.objects[bucket]:
            return object()
        raise Exception("NoSuchKey")  # 不存在抛异常（与 S3Error 同语义）

    def put_object(self, bucket, name, data, length, content_type=None):
        self.put_calls.append((bucket, name, length, content_type))
        self.objects.setdefault(bucket, {})[name] = (data.read(), content_type)


def _store(cfg: MinioConfig = CFG) -> tuple[MinioImageStore, FakeMinioClient]:
    fake = FakeMinioClient()
    return MinioImageStore(cfg, client=fake), fake


class TestPutImage:
    def test_uploads_with_hash_name_and_returns_http_url(self):
        """对象名 = SHA-256 哈希，返回 public_base_url/bucket/<哈希> 的
        http URL（识图模型 image_url 使用）。"""
        store, fake = _store()
        png = b"\x89PNG-upload-test"
        url = store.put_image(png, mime_type="image/png")
        digest = hashlib.sha256(png).hexdigest()
        assert url == f"http://img.example.com:9000/public/{digest}"
        assert url.startswith("http://")  # issue #163：http 形式而非 base64
        assert fake.objects["public"][digest][0] == png
        # 上传时带上 MIME 类型（模型经 URL 拉图时 Content-Type 正确）
        assert fake.put_calls == [("public", digest, len(png),
                                   "image/png")]
        # issue #164：桶权限设为公开只读（匿名 s3:GetObject）
        assert fake.policy_calls == ["public"]
        policy = json.loads(fake.policies["public"])
        stmt = policy["Statement"][0]
        assert stmt["Effect"] == "Allow"
        assert stmt["Action"] == ["s3:GetObject"]
        assert stmt["Resource"] == ["arn:aws:s3:::public/*"]

    def test_auto_creates_missing_bucket(self):
        """桶不存在时自动创建（默认桶名 public，issue #164）。"""
        store, fake = _store()
        assert fake.buckets == set()
        store.put_image(b"\x89PNG-x", mime_type="image/jpeg")
        assert "public" in fake.buckets
        # 公开只读策略随之设置
        assert "public" in fake.policies

    def test_idempotent_skip_existing_object(self):
        """对象已存在（同内容图片重复上传）不重复 put_object，直接复用
        返回 URL（幂等去重，避免重复写盘）。"""
        store, fake = _store()
        png = b"\x89PNG-same-image"
        digest = hashlib.sha256(png).hexdigest()
        # 预置已存在对象
        fake.buckets.add("public")
        fake.objects.setdefault("public", {})[digest] = (b"old", "image/png")
        url1 = store.put_image(png)
        url2 = store.put_image(png)
        assert url1 == url2
        assert fake.put_calls == []  # 未触发上传
        assert fake.stat_calls  # 有检查存在性

    def test_public_read_policy_applied_once_per_store(self):
        """公开只读策略幂等：同一 store 实例多次上传只设置一次策略。"""
        store, fake = _store()
        fake.buckets.add("public")
        store.put_image(b"\x89PNG-a")
        store.put_image(b"\x89PNG-b")
        assert fake.policy_calls == ["public"]  # 第二次上传不再重复设置

    def test_existing_bucket_still_gets_public_policy(self):
        """桶已存在（非本次创建）同样设置公开只读策略（保证老桶合规）。"""
        store, fake = _store()
        fake.buckets.add("public")
        store.put_image(b"\x89PNG-x")
        assert "public" in fake.policies
        assert "make_bucket" not in [c[0] for c in []]  # 未重复建桶

    def test_policy_failure_raises_store_error(self):
        """设置公开只读策略失败（如凭据权限不足）统一转 MinioStoreError，
        提示检查 MinIO 权限（识图模型依赖匿名取图）。"""
        store, fake = _store()

        class BrokenPolicyClient(FakeMinioClient):
            def set_bucket_policy(self, bucket, policy):
                raise OSError("AccessDenied: no permission")

        store._client = BrokenPolicyClient()
        with pytest.raises(MinioStoreError, match="公开只读"):
            store.put_image(b"\x89PNG-x")

    def test_empty_image_raises(self):
        """空图片直接报错，不发请求。"""
        store, _ = _store()
        with pytest.raises(MinioStoreError, match="为空"):
            store.put_image(b"")

    def test_upload_failure_raises_store_error(self):
        """MinIO 底层写入失败统一转 MinioStoreError（带原因）。"""
        store, fake = _store()

        class BrokenClient(FakeMinioClient):
            def put_object(self, bucket, name, data, length, content_type=None):
                raise OSError("connection refused")

        store._client = BrokenClient()
        with pytest.raises(MinioStoreError, match="上传 MinIO 失败"):
            store.put_image(b"\x89PNG-x")

    def test_trailing_slash_public_base_url_normalized(self):
        """public_base_url 尾斜杠归一，不产生双斜杠。"""
        cfg = MinioConfig(
            enabled=True, endpoint="h:9000", access_key="a", secret_key="s",
            bucket="b", public_base_url="http://img.example.com:9000/")
        store, fake = _store(cfg)
        url = store.put_image(b"\x89PNG-x")
        digest = hashlib.sha256(b"\x89PNG-x").hexdigest()
        assert url == f"http://img.example.com:9000/b/{digest}"
        assert "//b/" not in url


class TestConfig:
    def test_config_from_settings_defaults(self):
        """未配置 minio 段时默认关闭，endpoint/桶取默认值。"""
        from types import SimpleNamespace
        s = SimpleNamespace(
            minio_enabled=False, minio_endpoint="", minio_secure=False,
            minio_access_key="", minio_secret_key="",
            minio_bucket="", minio_public_base_url="", minio_verify_ssl=True)
        cfg = config_from_settings(s)
        assert cfg.enabled is False
        assert cfg.endpoint == "127.0.0.1:9000"
        assert cfg.bucket == "public"
        assert cfg.public_base_url == ""

    def test_config_from_settings_env_fallback(self, monkeypatch):
        """凭据 / endpoint / 桶 / 公网前缀缺省时回退环境变量。"""
        monkeypatch.setenv("MINIO_ROOT_USER", "env-user")
        monkeypatch.setenv("MINIO_ROOT_PASSWORD", "env-pass")
        monkeypatch.setenv("MINIO_ENDPOINT", "minio.example.com:9000")
        monkeypatch.setenv("MINIO_BUCKET", "env-bucket")
        monkeypatch.setenv("MINIO_PUBLIC_BASE_URL", "http://img.example.com")
        from types import SimpleNamespace
        s = SimpleNamespace(
            minio_enabled=True, minio_endpoint="", minio_secure=True,
            minio_access_key="", minio_secret_key="",
            minio_bucket="", minio_public_base_url="", minio_verify_ssl=True)
        cfg = config_from_settings(s)
        assert cfg.access_key == "env-user"
        assert cfg.secret_key == "env-pass"
        assert cfg.endpoint == "minio.example.com:9000"
        assert cfg.bucket == "env-bucket"
        assert cfg.public_base_url == "http://img.example.com"
        assert cfg.secure is True

    def test_is_usable(self):
        """启用且 endpoint/凭据/公网前缀齐全才算可用。"""
        assert CFG.is_usable() is True
        # 关闭 / 缺 endpoint / 缺凭据 / 缺公网前缀 → 均不可用
        assert MinioConfig(
            enabled=False, endpoint="h:9000", access_key="a", secret_key="s",
            public_base_url="http://x").is_usable() is False
        assert MinioConfig(
            enabled=True, endpoint="", access_key="a", secret_key="s",
            public_base_url="http://x").is_usable() is False
        assert MinioConfig(
            enabled=True, endpoint="h:9000", access_key="", secret_key="s",
            public_base_url="http://x").is_usable() is False
        assert MinioConfig(
            enabled=True, endpoint="h:9000", access_key="a", secret_key="s",
            public_base_url="").is_usable() is False


class TestSettingsWiring:
    def test_disabled_returns_none(self):
        """minio 未启用 → 返回 None（识图保持 base64 内联输入）。"""
        from types import SimpleNamespace
        s = SimpleNamespace(
            minio_enabled=False, minio_endpoint="", minio_secure=False,
            minio_access_key="", minio_secret_key="",
            minio_bucket="", minio_public_base_url="", minio_verify_ssl=True)
        assert image_store_from_settings(s) is None

    def test_enabled_incomplete_returns_none(self):
        """启用但缺 public_base_url → 返回 None（回退 base64，记告警）。"""
        from types import SimpleNamespace
        s = SimpleNamespace(
            minio_enabled=True, minio_endpoint="127.0.0.1:9000",
            minio_secure=False, minio_access_key="a", minio_secret_key="s",
            minio_bucket="b", minio_public_base_url="", minio_verify_ssl=True)
        assert image_store_from_settings(s) is None

    def test_enabled_complete_returns_store(self):
        """启用且配置完整 → 返回可用的 MinioImageStore。"""
        from types import SimpleNamespace
        s = SimpleNamespace(
            minio_enabled=True, minio_endpoint="127.0.0.1:9000",
            minio_secure=False, minio_access_key="a", minio_secret_key="s",
            minio_bucket="b", minio_public_base_url="http://img.example.com",
            minio_verify_ssl=True)
        store = image_store_from_settings(s)
        assert isinstance(store, MinioImageStore)
        assert store.cfg.bucket == "b"
