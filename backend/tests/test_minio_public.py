"""识图图片公网访问端点测试（issue #319）。

识图模型调用时图片以 http URL 传入（issue #163/#164）：后端把图片哈希
上传 MinIO public 桶，识图请求的 image_url 形如
``https://<站点>/minio-public/<bucket>/<sha256 哈希>``（阿里云百炼 qwen
等 OpenAI 兼容网关经该 URL 匿名取图）。

此前 ``/minio-public/`` 在 FastAPI 没有对应路由，请求被 SPA 兜底
（main.py 的 ``/{full_path:path}`` → index.html）当成前端路由吞掉，返回
text/html 页面——外部模型网关取图拿到 HTML，报 ``url error, please check
url！``（issue #319 现象，:448 站点与后端直连均实测复现）。修复：后端新增
``GET /minio-public/{bucket}/{object_name:path}`` 路由（botler/minio_public
.py），直接流式返回 MinIO 图片桶对象（含 Range / Content-Type / ETag /
Cache-Control），不再依赖外部反向代理合并 nginx location（issue #311 的
deploy/nginx-minio-public.conf 方案在可视化网关场景无法配置，实际未生效）。

本文件测试：
- 回归：带 SPA 兜底的 app 中 /minio-public/ 返回图片字节而非 index.html
  （修复前返回 HTML —— 复现 issue #319 根因；不挂路由的对照 app 保持
  返回 HTML，固化失败模式）；
- 路由行为：200 正常 / 206 Range / 404 对象不存在 / 404 非配置桶 /
  400 非法对象名 / 未启用 MinIO 404 / MinIO 异常 502；
- 防回退：main.py 注册 minio_public 路由（静态校验，参考
  test_deploy_minio.py 模式）。
"""

from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from minio.error import S3Error

import botler.minio_public
from botler.config import ConfigManager

# 与 main.py 的 spa_fallback 同构的最小配置（minio 段完整可用）
CONFIG_MINIO_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
minio:
  enabled: true
  endpoint: 127.0.0.1:9000
  secure: false
  access_key: minioadmin
  secret_key: minioadmin
  bucket: public
  public_base_url: https://img.example.com/minio-public
"""

IMAGE_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-content"
ETAG = '"8372d2d30058e5aa707b3e81b427f8c7"'


class FakeObject:
    """模拟 minio SDK get_object 返回的 urllib3 HTTPResponse。"""

    def __init__(self, body: bytes = IMAGE_BYTES, status: int = 200,
                 content_type: str = "image/jpeg", etag: str = ETAG):
        self.body = body
        self.status = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "ETag": etag,
            "Accept-Ranges": "bytes",
        }
        self._closed = False

    def stream(self, amt: int = 65536):
        yield self.body

    def close(self):
        self._closed = True

    def release_conn(self):
        pass


class FakeMinioClient:
    """模拟 MinioImageStore.client（stat_object / get_object）。"""

    def __init__(self, objects: dict | None = None, error: Exception | None = None,
                 s3_error_code: str | None = None,
                 get_error: Exception | None = None,
                 get_s3_error_code: str | None = None):
        # {(bucket, key): FakeObject}
        self.objects = objects or {}
        self.error = error
        self.s3_error_code = s3_error_code
        self.get_error = get_error
        self.get_s3_error_code = get_s3_error_code

    def _raise_get(self, bucket: str, key: str):
        if self.get_s3_error_code:
            raise S3Error(None, self.get_s3_error_code,
                          f"code: {self.get_s3_error_code}", f"/{bucket}/{key}",
                          "", "", bucket, key)
        if self.get_error is not None:
            raise self.get_error

    def _raise(self, bucket: str, key: str):
        if self.s3_error_code:
            raise S3Error(None, self.s3_error_code,
                          f"code: {self.s3_error_code}", f"/{bucket}/{key}",
                          "", "", bucket, key)
        if self.error is not None:
            raise self.error
        raise self._missing(bucket, key)

    def _missing(self, bucket: str, key: str) -> S3Error:
        return S3Error(None, "NoSuchKey", "The specified key does not exist.",
                       f"/{bucket}/{key}", "", "", bucket, key)

    def stat_object(self, bucket: str, key: str):
        obj = self.objects.get((bucket, key))
        if obj is None:
            self._raise(bucket, key)
        return SimpleNamespace(
            content_type=obj.headers.get("Content-Type", "application/octet-stream"),
            etag=obj.headers.get("ETag", ""),
            size=len(obj.body),
        )

    def get_object(self, bucket: str, key: str, request_headers: dict | None = None):
        obj = self.objects.get((bucket, key))
        if obj is None:
            self._raise(bucket, key)
        self._raise_get(bucket, key)
        # Range 透传：返回部分内容（206），模拟 MinIO 行为
        if request_headers and request_headers.get("Range"):
            rng = request_headers["Range"]
            # 仅支持 bytes=start-end 单段（测试用）
            assert rng.startswith("bytes="), rng
            start_s, _, end_s = rng[6:].partition("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(obj.body) - 1
            body = obj.body[start:end + 1]
            partial = FakeObject(
                body=body, status=206,
                content_type=obj.headers.get("Content-Type", "image/jpeg"),
                etag=obj.headers.get("ETag", ""),
            )
            partial.headers["Content-Range"] = (
                f"bytes {start}-{start + len(body) - 1}/{len(obj.body)}")
            partial.headers["Content-Length"] = str(len(body))
            return partial
        return obj


def make_store(objects: dict | None = None, error: Exception | None = None,
             s3_error_code: str | None = None,
             get_error: Exception | None = None,
             get_s3_error_code: str | None = None):
    """构造模拟 MinioImageStore（cfg.bucket=public + 假 client）。"""
    return SimpleNamespace(
        cfg=SimpleNamespace(bucket="public"),
        client=FakeMinioClient(
            objects, error=error, s3_error_code=s3_error_code,
            get_error=get_error, get_s3_error_code=get_s3_error_code),
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    """最小 app：挂 minio_public 路由 + SPA 兜底（与 main.py 顺序一致）。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_MINIO_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    app = FastAPI()
    app.state.ctx = SimpleNamespace(config=config)
    app.include_router(botler.minio_public.router)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>SPA-index</html>", encoding="utf-8")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        candidate = dist / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    monkeypatch.setattr(botler.minio_public, "_image_store",
                        lambda request: make_store(
                            {("public", HASH): FakeObject()}))
    return TestClient(app)


HASH = "366519759fdc019897e384a253891f23cde4c24c1e4a9528ccf81817d205f936"


class TestServeMinioPublicObject:
    """/minio-public/{bucket}/{object_name} 路由行为。"""

    def test_serve_existing_object_returns_image_bytes(self, client):
        """图片 URL 返回图片字节 + image/jpeg + 公开缓存头（issue #319 回归）。"""
        resp = client.get(f"/minio-public/public/{HASH}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["cache-control"] == "public, max-age=86400"
        assert resp.headers["etag"] == ETAG
        assert resp.content == IMAGE_BYTES

    def test_range_request_returns_206(self, client):
        """Range 请求透传 MinIO：206 + Content-Range + 部分内容。"""
        resp = client.get(
            f"/minio-public/public/{HASH}", headers={"Range": "bytes=0-9"})
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 0-9/{len(IMAGE_BYTES)}"
        assert resp.content == IMAGE_BYTES[:10]

    def test_missing_object_returns_404(self, client, monkeypatch):
        """对象不存在 → 404（模型取图失败而非返回 HTML）。"""
        monkeypatch.setattr(
            botler.minio_public, "_image_store",
            lambda request: make_store(objects={}))
        resp = client.get(f"/minio-public/public/{HASH}")
        assert resp.status_code == 404
        assert resp.json()["error"]

    def test_other_bucket_rejected_404(self, client, monkeypatch):
        """非配置桶一律 404——不暴露 MinIO 其他私有桶。"""
        monkeypatch.setattr(
            botler.minio_public, "_image_store",
            lambda request: make_store(
                objects={("private", "secret.png"): FakeObject()}))
        resp = client.get("/minio-public/private/secret.png")
        assert resp.status_code == 404

    def test_store_not_configured_404(self, client, monkeypatch):
        """MinIO 未启用/配置不完整 → 404（无法取图）。"""
        monkeypatch.setattr(botler.minio_public, "_image_store",
                            lambda request: None)
        resp = client.get(f"/minio-public/public/{HASH}")
        assert resp.status_code == 404

    @pytest.mark.parametrize("bad_path", [
        "public/%2e%2e/x",     # 解码后含 .. 段
        "public/%2e/x",        # 解码后含 . 段
        "public//x",           # 空段
        "public/x%5cy",        # 反斜杠
    ])
    def test_invalid_object_name_rejected(self, client, bad_path):
        """路径穿越 / 空段 / 反斜杠等非法对象名 → 400。"""
        resp = client.get(f"/minio-public/{bad_path}")
        assert resp.status_code == 400

    def test_literal_dotdot_rejected_directly(self, monkeypatch):
        """字面 .. 段经路由函数直接校验拒绝（HTTP 层会被客户端归一化）。"""
        class _FakeRequest:
            headers = {}
            app = SimpleNamespace(state=SimpleNamespace(
                ctx=SimpleNamespace(config=None)))
        monkeypatch.setattr(botler.minio_public, "_image_store",
                            lambda request: make_store())
        resp = botler.minio_public.serve_minio_public_object(
            _FakeRequest(), "public", "../../etc/passwd")
        assert resp.status_code == 400

    def test_minio_error_returns_502(self, client, monkeypatch):
        """MinIO 服务异常（连接失败等）→ 502，不裸抛 500。"""
        monkeypatch.setattr(
            botler.minio_public, "_image_store",
            lambda request: make_store(
                error=RuntimeError("connection refused")))
        resp = client.get(f"/minio-public/public/{HASH}")
        assert resp.status_code == 502

    def test_s3_error_other_than_nosuchkey_returns_502(self, client, monkeypatch):
        """S3Error 非 NoSuchKey（如 AccessDenied）→ 502。"""
        monkeypatch.setattr(
            botler.minio_public, "_image_store",
            lambda request: make_store(s3_error_code="AccessDenied"))
        resp = client.get(f"/minio-public/public/{HASH}")
        assert resp.status_code == 502

    def test_image_store_real_path(self, monkeypatch, tmp_path):
        """_image_store 真实路径：ctx 缺失 → None；minio 启用 → 构造 store。

        不经 monkeypatch 直接覆盖 _image_store，验证配置读取（构造
        MinioImageStore 不建连，无 MinIO 依赖）。
        """
        from botler.minio_client import MinioImageStore, MinioConfig
        # ctx 缺失
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=None)))
        assert botler.minio_public._image_store(req) is None
        # minio 启用且配置完整 → 返回可用的 MinioImageStore
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_MINIO_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        req2 = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                ctx=SimpleNamespace(config=config))))
        store = botler.minio_public._image_store(req2)
        assert isinstance(store, MinioImageStore)
        assert store.cfg.bucket == "public"
        assert isinstance(store.cfg, MinioConfig)

    def test_close_failure_does_not_break_response(self, client, monkeypatch):
        """流式响应后对象 close/release_conn 抛异常不影响响应成功。"""
        class _ClosedObj(FakeObject):
            def close(self):
                raise RuntimeError("close failed")

            def release_conn(self):
                raise RuntimeError("release failed")

        monkeypatch.setattr(
            botler.minio_public, "_image_store",
            lambda request: make_store(
                {("public", HASH): _ClosedObj()}))
        resp = client.get(f"/minio-public/public/{HASH}")
        assert resp.status_code == 200
        assert resp.content == IMAGE_BYTES

    def test_get_object_s3_error_returns_502(self, client, monkeypatch):
        """stat 成功但 get_object 抛 S3Error（非 NoSuchKey）→ 502。"""
        monkeypatch.setattr(
            botler.minio_public, "_image_store",
            lambda request: make_store(
                {("public", HASH): FakeObject()},
                get_s3_error_code="InternalError"))
        resp = client.get(f"/minio-public/public/{HASH}")
        assert resp.status_code == 502

    def test_get_object_other_error_returns_502(self, client, monkeypatch):
        """stat 成功但 get_object 抛连接类异常 → 502。"""
        monkeypatch.setattr(
            botler.minio_public, "_image_store",
            lambda request: make_store(
                {("public", HASH): FakeObject()},
                get_error=RuntimeError("connection refused")))
        resp = client.get(f"/minio-public/public/{HASH}")
        assert resp.status_code == 502


class TestSpaFallbackRegression:
    """issue #319 根因回归：/minio-public/ 不得被 SPA 兜底吞掉。"""

    def _app(self, tmp_path, with_router: bool):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_MINIO_TEXT, encoding="utf-8")
        config = ConfigManager(str(config_path))
        app = FastAPI()
        app.state.ctx = SimpleNamespace(config=config)
        if with_router:
            app.include_router(botler.minio_public.router)
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>SPA-index</html>", encoding="utf-8")

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str):
            candidate = dist / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
        return TestClient(app)

    def test_route_not_swallowed_by_spa_fallback(self, tmp_path, monkeypatch):
        """带 SPA 兜底的完整 app：/minio-public/ 返回图片而非 index.html。"""
        monkeypatch.setattr(botler.minio_public, "_image_store",
                            lambda request: make_store(
                                {("public", HASH): FakeObject()}))
        tc = self._app(tmp_path, with_router=True)
        resp = tc.get(f"/minio-public/public/{HASH}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == IMAGE_BYTES
        assert b"SPA-index" not in resp.content

    def test_without_route_spa_fallback_serves_html(self, tmp_path):
        """不挂路由的对照 app：/minio-public/ 被 SPA 兜底返回 HTML。

        固化 issue #319 失败模式（修复前线上行为：模型取图拿到
        text/html → 报 url error）。
        """
        tc = self._app(tmp_path, with_router=False)
        resp = tc.get(f"/minio-public/public/{HASH}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "SPA-index" in resp.text


class TestMainRegistersRoute:
    """main.py 注册 minio_public 路由（防回退静态校验，test_deploy 模式）。"""

    def test_main_imports_and_includes_minio_public_router(self):
        src = (Path(__file__).resolve().parents[2]
               / "backend" / "botler" / "main.py").read_text(encoding="utf-8")
        assert "minio_public" in src
        assert "minio_public_router" in src
        # 路由注册必须在 SPA 兜底（/{full_path:path}）之前，否则被吞
        include_pos = src.index("minio_public_router")
        fallback_pos = src.index("spa_fallback")
        assert include_pos < fallback_pos, \
            "minio_public 路由必须在 spa_fallback 之前注册（issue #319）"
