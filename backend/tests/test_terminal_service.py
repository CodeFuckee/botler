"""终端服务进程测试（issue #183）：Tornado + terminado WebSocket 认证与 PTY 会话。

覆盖：
- /terminal/health 健康检查；
- WebSocket 认证：无 token / 伪造 token 拒绝（close code 4001）、有效 token
  放行并建立真实 PTY 会话（shell 用 cat 做输入回显验证，避免依赖具体环境）；
- 多标签隔离：不同 name 创建独立终端会话。
"""

import json
import os
import tempfile

from tornado import gen
from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.websocket import websocket_connect

from botler.auth import create_terminal_token, get_session_secret
from terminado import NamedTermManager
from terminal_service import make_terminal_app

USER = {"sub": "uid-1", "username": "zhangsan", "name": "张三", "email": "zs@example.com"}


class TerminalServiceTest(AsyncHTTPTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.secret_path = os.path.join(self._tmp.name, "session_secret.key")
        self._secret = get_session_secret(self.secret_path)
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self._tmp.cleanup()

    def get_app(self):
        # cat：输入回显，测试可稳定断言 PTY 会话真实运行
        manager = NamedTermManager(shell_command=["cat"], max_terminals=8)
        return make_terminal_app(term_manager=manager, secret_path=self.secret_path)

    def _token(self):
        return create_terminal_token(USER, secret=self._secret)

    def test_health_ok(self):
        resp = self.fetch("/terminal/health")
        assert resp.code == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["service"] == "botler-terminal"

    @gen_test
    async def test_ws_rejects_without_token(self):
        ws = await websocket_connect(f"ws://127.0.0.1:{self.get_http_port()}/terminal/ws/t1")
        msg = await ws.read_message()
        assert msg is None, f"应直接关闭而非收发消息: {msg}"
        assert ws.close_code == 4001, f"关闭码应为 4001，实际 {ws.close_code}"

    @gen_test
    async def test_ws_rejects_forged_token(self):
        ws = await websocket_connect(f"ws://127.0.0.1:{self.get_http_port()}/terminal/ws/t1?token=forged.token")
        msg = await ws.read_message()
        assert msg is None
        assert ws.close_code == 4001

    @gen_test
    async def test_ws_rejects_expired_token(self):
        token = create_terminal_token(USER, secret=self._secret, now=1000, ttl_seconds=1)
        ws = await websocket_connect(f"ws://127.0.0.1:{self.get_http_port()}/terminal/ws/t1?token={token}")
        msg = await ws.read_message()
        assert msg is None
        assert ws.close_code == 4001

    @gen_test
    async def test_ws_valid_token_runs_shell(self):
        ws = await websocket_connect(f"ws://127.0.0.1:{self.get_http_port()}/terminal/ws/tab-1?token={self._token()}")
        # 首个消息为 setup
        first = json.loads(await ws.read_message())
        assert first[0] == "setup"
        # stdin 回显验证 PTY 会话运行（cat 会回显输入）
        ws.write_message(json.dumps(["stdin", "hello-terminal\n"]))
        got = ""
        for _ in range(5):
            msg = await ws.read_message()
            if msg is None:
                break
            arr = json.loads(msg)
            if arr[0] == "stdout":
                got += arr[1]
                if "hello-terminal" in got:
                    break
        assert "hello-terminal" in got, f"stdout 未包含回显输入: {got!r}"
        ws.close()

    @gen_test
    async def test_multi_tab_terminals_isolated(self):
        # 不同 name = 不同 PTY 会话（terminado NamedTermManager 隔离）
        ws1 = await websocket_connect(f"ws://127.0.0.1:{self.get_http_port()}/terminal/ws/alpha?token={self._token()}")
        ws2 = await websocket_connect(f"ws://127.0.0.1:{self.get_http_port()}/terminal/ws/beta?token={self._token()}")
        await ws1.read_message()  # setup
        await ws2.read_message()  # setup
        # 向 alpha 输入，只有 alpha 收到回显
        ws1.write_message(json.dumps(["stdin", "only-alpha\n"]))
        got = ""
        for _ in range(5):
            msg = await ws1.read_message()
            if msg is None:
                break
            arr = json.loads(msg)
            if arr[0] == "stdout":
                got += arr[1]
                if "only-alpha" in got:
                    break
        assert "only-alpha" in got
        # beta 不应收到 alpha 的内容：短超时读取（无消息到达即通过）
        ws2.write_message(json.dumps(["set_size", 24, 80]))
        while True:
            try:
                msg = await gen.with_timeout(
                    self.io_loop.time() + 1.0, ws2.read_message())
            except gen.TimeoutError:
                break
            if msg is None:
                break
            arr = json.loads(msg)
            if arr[0] == "stdout":
                assert "only-alpha" not in arr[1], "终端会话未隔离，beta 收到 alpha 输出"
        ws1.close()
        ws2.close()
