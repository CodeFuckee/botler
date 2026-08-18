#!/usr/bin/env python3
"""Botler Web 终端服务（issue #183）：Tornado + terminado 独立进程。

职责
----
- 提供标准 WebSocket 终端服务：/terminal/ws/<name>（terminado 协议，
  多标签 = 每个标签一个独立 PTY 会话，name 参数隔离）；
- 与 Botler 主后端「共享用户验证」：WebSocket 握手时校验主后端签发的
  短时效 token（同一份 HMAC 会话密钥，见 botler.auth.verify_terminal_token）；
- 默认只监听 127.0.0.1（安全隔离），对外经 Botler 主后端反向代理
  （backend/botler/api/terminal.py）或 Nginx 统一入口
  （deploy/nginx-terminal.conf）访问，不直接暴露端口。

启动
----
    python backend/terminal_service.py

环境变量
--------
    BOTLER_TERM_PORT        监听端口（默认 8765）
    BOTLER_TERM_BIND        监听地址（默认 127.0.0.1）
    BOTLER_TERM_SHELL       shell 命令（默认 $SHELL，未设置回退 /bin/bash）
    BOTLER_TERM_MAX_TERMINALS  最大并发终端数（默认 64）
    BOTLER_SESSION_SECRET   会话密钥路径（与主后端一致，默认
                            backend/data/session_secret.key）
"""

from __future__ import annotations

import logging
import os
import shlex

import tornado.ioloop
import tornado.web
import tornado.websocket
from terminado import NamedTermManager
from terminado.websocket import TermSocket

from botler.auth import get_session_secret, verify_terminal_token

logger = logging.getLogger("botler.terminal")


def default_shell_command() -> list[str]:
    """终端默认 shell 命令（环境变量覆盖，支持带参数）。"""
    shell = os.environ.get("BOTLER_TERM_SHELL") or os.environ.get("SHELL") or "/bin/bash"
    return shlex.split(shell)


class HealthHandler(tornado.web.RequestHandler):
    """终端服务健康检查：GET /terminal/health。"""

    def get(self) -> None:
        manager = self.application.settings.get("term_manager")
        count = len(getattr(manager, "terminals", {})) if manager else 0
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.write({"ok": True, "service": "botler-terminal", "active_terminals": count})


class AuthTermSocket(TermSocket):
    """校验终端 token 后再交给 terminado 托管 PTY（issue #183）。

    认证失败（token 缺失/无效/过期）以 WebSocket close code 4001 拒绝，
    不创建任何 PTY 会话。
    """

    def check_origin(self, origin: str) -> bool:
        # 认证以短时效 token 为准；经反向代理/nginx 场景 Origin 可能与
        # 服务端 Host 不一致，统一放行（不引入额外配置面）。
        return True

    def open(self, url_component=None):  # type: ignore[override]
        secret_path = self.application.settings.get("secret_path")
        token = self.get_query_argument("token", default="") or ""
        secret = get_session_secret(secret_path) if secret_path else None
        self.term_user = verify_terminal_token(token, secret=secret) if token else None
        if self.term_user is None:
            logger.warning(
                "终端 WebSocket 认证失败（token 缺失/无效/过期）: name=%s",
                url_component,
            )
            self.close(code=4001, reason="认证失败：token 无效或已过期")
            return
        logger.info("终端 WebSocket 认证通过: user=%s name=%s",
                    self.term_user.get("username"), url_component)
        super().open(url_component)


def make_terminal_app(
    term_manager: NamedTermManager | None = None,
    secret_path: str | None = None,
) -> tornado.web.Application:
    """构造终端服务 Tornado 应用（term_manager/secret_path 可注入便于测试）。"""
    manager = term_manager or NamedTermManager(
        shell_command=default_shell_command(),
        max_terminals=int(os.environ.get("BOTLER_TERM_MAX_TERMINALS", "64")),
    )
    return tornado.web.Application(
        [
            (r"/terminal/health", HealthHandler),
            (r"/terminal/ws/([^/?]+)", AuthTermSocket, {"term_manager": manager}),
        ],
        term_manager=manager,
        secret_path=secret_path,
        websocket_ping_interval=20,
        websocket_ping_timeout=20,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    port = int(os.environ.get("BOTLER_TERM_PORT", "8765"))
    bind = os.environ.get("BOTLER_TERM_BIND", "127.0.0.1")
    app = make_terminal_app()
    app.listen(port, address=bind)
    logger.info("Botler 终端服务启动: ws://%s:%s/terminal/ws/<name>", bind, port)
    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        logger.info("Botler 终端服务已停止")


if __name__ == "__main__":
    main()
