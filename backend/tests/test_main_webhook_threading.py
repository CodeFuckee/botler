"""Webhook 路由不得阻塞 Uvicorn 事件循环（issue #190）。"""

from __future__ import annotations

import os
import socket
import tempfile
from contextlib import asynccontextmanager
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

# botler.main 在模块导入时创建全局 app，会立即读取配置。CI 不提供
# backend/config.yaml，因此必须在导入前为该测试进程准备隔离配置，避免依赖
# 开发机本地的未跟踪配置文件。
_MODULE_TMP = tempfile.mkdtemp(prefix="botler-webhook-threading-")
os.environ["BOTLER_CONFIG"] = os.path.join(_MODULE_TMP, "config.yaml")
os.environ["BOTLER_DB"] = os.path.join(_MODULE_TMP, "botler.db")
Path(os.environ["BOTLER_CONFIG"]).write_text(
    """gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
""",
    encoding="utf-8",
)

import botler.main as main  # noqa: E402

os.environ.pop("BOTLER_CONFIG")
os.environ.pop("BOTLER_DB")


class _SlowWebhook:
    """模拟 GitLab REST API 慢响应的同步 webhook 处理器。"""

    def __init__(self) -> None:
        self.started = threading.Event()

    def handle(self, body: dict, token: str | None) -> dict:
        self.started.set()
        time.sleep(2)
        return {"accepted": True}


class _FakeConfig:
    def get(self):
        return SimpleNamespace(minio_enabled=False)


class _FakeScheduler:
    def stats(self):
        return {}


class _FakeDatabase:
    def task_stats(self):
        return {}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def app_with_slow_webhook(monkeypatch):
    """创建真实生产路由，并替换外部依赖为可控桩。"""
    webhook = _SlowWebhook()
    ctx = SimpleNamespace(
        webhook=webhook,
        config=_FakeConfig(),
        scheduler=_FakeScheduler(),
        db=_FakeDatabase(),
        sso=SimpleNamespace(enabled=lambda: False),
    )
    @asynccontextmanager
    async def test_lifespan(app):
        yield

    monkeypatch.setattr(main, "build_context", lambda config_path=None: ctx)
    monkeypatch.setattr(main, "lifespan", test_lifespan)
    return main.create_app(), webhook


def test_slow_webhook_does_not_delay_health_check(app_with_slow_webhook):
    """同步 GitLab 调用持续两秒时，健康检查仍须在 200ms 内返回。"""
    app, webhook = app_with_slow_webhook
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    assert _wait_until(lambda: server.started), "测试服务器未能启动"

    base_url = f"http://127.0.0.1:{port}"
    webhook_result: list[httpx.Response] = []

    def post_webhook() -> None:
        with httpx.Client(base_url=base_url, timeout=5) as client:
            webhook_result.append(client.post("/webhook/gitlab", json={}))

    request_thread = threading.Thread(target=post_webhook)
    request_thread.start()
    try:
        assert webhook.started.wait(timeout=1), "webhook 未进入慢速同步处理"
        with httpx.Client(base_url=base_url, timeout=1) as client:
            started_at = time.perf_counter()
            response = client.get("/api/health")
            elapsed = time.perf_counter() - started_at
        assert response.status_code == 200
        assert elapsed < 0.2, f"健康检查被 webhook 阻塞 {elapsed:.3f}s"
    finally:
        request_thread.join(timeout=4)
        server.should_exit = True
        server_thread.join(timeout=3)

    assert not request_thread.is_alive()
    assert webhook_result[0].status_code == 200


def _wait_until(predicate, timeout: float = 3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
