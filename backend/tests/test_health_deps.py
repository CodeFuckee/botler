"""健康检查依赖探测测试（issue #207）。

背景：docker compose botler 服务 healthcheck 只探测 /api/health，事件
循环卡死 / 依赖失效（MinIO 不可达、磁盘空间不足）时容器仍显示 healthy，
「假死无感知」。本次为 /api/health 补充依赖探测：MinIO 连通（仅启用时，
GET /minio/health/live 免凭据 live 探针，与 compose minio healthcheck
同语义）+ 数据目录磁盘空间。本文件覆盖：
  1. probe_minio：本地 HTTP 服务 200 → ok；连接拒绝 → fail；
  2. probe_disk：正常目录 → ok；剩余空间低于阈值 → fail；目录不存在
     不抛异常 → fail；
  3. build_deps_report：minio 未启用 → skipped（不拖累健康检查）；
     启用 → 按真实 endpoint 探测；数据目录可显式传入 / 环境变量推断；
  4. deps_critical_failed：fail 参与成败、skipped/ok 不参与；
  5. build_health_payload：deps 字段随负载返回、ok 可置 false。
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from threading import Thread

from botler.health import (
    build_deps_report,
    default_data_dir,
    deps_critical_failed,
    probe_disk,
    probe_minio,
)
from botler.version import build_health_payload


class _MinioLiveHandler(BaseHTTPRequestHandler):
    """只应答 /minio/health/live（其余 404），模拟 MinIO live 探针。"""

    def do_GET(self):  # noqa: N802
        if self.path == "/minio/health/live":
            body = b'{"status": "alive"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # 静默，避免测试输出刷屏
        pass


def _live_server() -> ThreadingHTTPServer:
    """启动一个监听随机端口的 live 探针 HTTP 服务（返回 (server, thread)）。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MinioLiveHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class TestProbeMinio:
    def test_probe_minio_ok(self):
        """MinIO live 端点返回 200 → status=ok。"""
        server = _live_server()
        try:
            host, port = server.server_address
            result = probe_minio(f"{host}:{port}", secure=False)
        finally:
            server.shutdown()
        assert result["status"] == "ok"
        assert f"{host}:{port}" in result["detail"]

    def test_probe_minio_connection_refused(self):
        """端点不可达（连接拒绝）→ status=fail，不抛异常。"""
        # 绑定一个端口后立刻关闭，得到「大概率不可达」的地址
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        result = probe_minio(f"127.0.0.1:{port}", secure=False, timeout=1.0)
        assert result["status"] == "fail"
        assert "不可达" in result["detail"]

    def test_probe_minio_http_error(self):
        """端点返回非 200（404）→ status=fail（urlopen 抛 HTTPError）。"""
        class _ErrorHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _ErrorHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            result = probe_minio(f"{host}:{port}", secure=False)
        finally:
            server.shutdown()
        assert result["status"] == "fail"


class TestProbeDisk:
    def test_probe_disk_ok(self, tmp_path):
        """正常目录：free 充足 → status=ok，带字节统计。"""
        result = probe_disk(tmp_path)
        assert result["status"] == "ok"
        assert result["free_bytes"] > 0
        assert result["free_mb"] > 0
        assert result["total_bytes"] > 0
        assert result["min_free_bytes"] == 512 * 1024 * 1024

    def test_probe_disk_below_threshold(self, tmp_path):
        """剩余空间低于阈值（用超大 min_free 模拟）→ status=fail。"""
        result = probe_disk(tmp_path, min_free=10**30)
        assert result["status"] == "fail"
        assert result["free_bytes"] < 10**30

    def test_probe_disk_missing_path(self, tmp_path):
        """目录不存在：disk_usage 抛 OSError → 吞掉并返回 fail。"""
        result = probe_disk(tmp_path / "no-such-dir")
        assert result["status"] == "fail"


class TestBuildDepsReport:
    def test_minio_disabled_skipped(self, tmp_path):
        """minio 未启用 → status=skipped（可选依赖不拖累健康检查）。"""
        settings = SimpleNamespace(minio_enabled=False, minio_endpoint="x:9000")
        deps = build_deps_report(settings, data_dir=tmp_path)
        assert deps["minio"]["status"] == "skipped"
        assert deps["disk"]["status"] == "ok"

    def test_minio_enabled_probes_live_endpoint(self, tmp_path):
        """minio 启用 → 按 endpoint 探测 /minio/health/live（200 → ok）。"""
        server = _live_server()
        try:
            host, port = server.server_address
            settings = SimpleNamespace(
                minio_enabled=True,
                minio_endpoint=f"{host}:{port}",
                minio_secure=False,
            )
            deps = build_deps_report(settings, data_dir=tmp_path)
        finally:
            server.shutdown()
        assert deps["minio"]["status"] == "ok"
        assert deps["disk"]["status"] == "ok"

    def test_minio_enabled_unreachable_fails(self, tmp_path):
        """minio 启用但端点不可达 → status=fail（依赖失效可感知）。"""
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        settings = SimpleNamespace(
            minio_enabled=True,
            minio_endpoint=f"127.0.0.1:{port}",
            minio_secure=False,
        )
        deps = build_deps_report(settings, data_dir=tmp_path)
        assert deps["minio"]["status"] == "fail"

    def test_data_dir_env_override(self, tmp_path, monkeypatch):
        """BOTLER_DATA_DIR 环境变量可覆盖数据目录推断。"""
        monkeypatch.setenv("BOTLER_DATA_DIR", str(tmp_path))
        assert default_data_dir() == tmp_path


class TestDepsCriticalFailed:
    def test_all_ok_skipped_not_failed(self):
        assert deps_critical_failed({"minio": {"status": "skipped"},
                                     "disk": {"status": "ok"}}) is False

    def test_any_fail_means_critical(self):
        assert deps_critical_failed({"minio": {"status": "fail"},
                                     "disk": {"status": "ok"}}) is True

    def test_empty_report_not_failed(self):
        assert deps_critical_failed({}) is False


class TestHealthPayloadWithDeps:
    def test_payload_includes_deps_and_ok_flag(self):
        """deps 随负载返回；关键依赖失败时 ok 可置 false（调用方返回 503）。"""
        deps = {"minio": {"status": "fail", "detail": "x"}, "disk": {"status": "ok"}}
        payload = build_health_payload(
            {"version": "1.0.0"},
            deps=deps,
            ok=not deps_critical_failed(deps),
        )
        assert payload["deps"] == deps
        assert payload["ok"] is False

    def test_payload_without_deps_unchanged(self):
        """不传 deps（旧调用方）→ 负载结构不变，ok 默认 True。"""
        payload = build_health_payload({"version": "1.0.0"})
        assert "deps" not in payload
        assert payload["ok"] is True
