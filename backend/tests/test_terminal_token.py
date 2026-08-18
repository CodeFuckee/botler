"""终端 token 测试（issue #183）：短时效 HMAC 签名 token 签发与校验。

token 由主后端签发（/api/terminal/token，SSO 会话保护），由独立终端
服务进程校验（同一会话密钥）；本测试覆盖签发/校验往返、篡改、过期、
非法输入等边界场景。
"""

from botler.auth import (
    create_session,
    create_terminal_token,
    get_session_secret,
    verify_terminal_token,
)

USER = {"sub": "uid-1", "username": "zhangsan", "name": "张三", "email": "zs@example.com"}


class TestTerminalToken:
    def test_roundtrip_carries_user(self, tmp_path):
        secret = get_session_secret(str(tmp_path / "session.key"))
        token = create_terminal_token(USER, secret=secret)
        payload = verify_terminal_token(token, secret=secret)
        assert payload is not None
        assert payload["sub"] == "uid-1"
        assert payload["username"] == "zhangsan"
        assert payload["name"] == "张三"
        assert payload["email"] == "zs@example.com"
        assert "exp" in payload

    def test_missing_optional_fields_tolerated(self, tmp_path):
        secret = get_session_secret(str(tmp_path / "session.key"))
        token = create_terminal_token({"sub": "uid-2"}, secret=secret)
        payload = verify_terminal_token(token, secret=secret)
        assert payload is not None
        assert payload["sub"] == "uid-2"
        assert payload.get("username") is None

    def test_tampered_token_rejected(self, tmp_path):
        secret = get_session_secret(str(tmp_path / "session.key"))
        token = create_terminal_token(USER, secret=secret)
        data, sig = token.rsplit(".", 1)
        # 篡改 payload 后签名不匹配
        tampered = data[:-1] + ("A" if data[-1] != "A" else "B") + "." + sig
        assert verify_terminal_token(tampered, secret=secret) is None

    def test_bad_signature_rejected(self, tmp_path):
        secret = get_session_secret(str(tmp_path / "session.key"))
        other = get_session_secret(str(tmp_path / "other.key"))
        token = create_terminal_token(USER, secret=other)
        assert verify_terminal_token(token, secret=secret) is None

    def test_expired_token_rejected(self, tmp_path):
        secret = get_session_secret(str(tmp_path / "session.key"))
        token = create_terminal_token(USER, ttl_seconds=1, secret=secret, now=1000)
        assert verify_terminal_token(token, secret=secret, now=1002) is None

    def test_not_yet_expired_accepted(self, tmp_path):
        secret = get_session_secret(str(tmp_path / "session.key"))
        token = create_terminal_token(USER, ttl_seconds=60, secret=secret, now=1000)
        assert verify_terminal_token(token, secret=secret, now=1050) is not None

    def test_invalid_inputs_return_none(self, tmp_path):
        secret = get_session_secret(str(tmp_path / "session.key"))
        assert verify_terminal_token(None, secret=secret) is None
        assert verify_terminal_token("", secret=secret) is None
        assert verify_terminal_token("no-dot-here", secret=secret) is None
        assert verify_terminal_token("x.y", secret=secret) is None
        # 合法签名但 payload 非 JSON
        assert verify_terminal_token("!!!!.sig", secret=secret) is None

    def test_default_secret_used_when_omitted(self, tmp_path, monkeypatch):
        # 未显式传 secret 时走 get_session_secret 默认路径（与终端服务一致）
        monkeypatch.setenv("BOTLER_SESSION_SECRET", str(tmp_path / "env.key"))
        token = create_terminal_token(USER)
        assert verify_terminal_token(token) is not None

    def test_default_ttl_about_60s(self, tmp_path):
        # 默认有效期 60 秒：第 60 秒仍有效，第 61 秒过期
        secret = get_session_secret(str(tmp_path / "session.key"))
        token = create_terminal_token(USER, secret=secret, now=1000)
        assert verify_terminal_token(token, secret=secret, now=1060) is not None
        assert verify_terminal_token(token, secret=secret, now=1061) is None

    def test_token_independent_of_session_cookie(self, tmp_path):
        # token 与会话 cookie 结构同构但用途隔离（typ 声明）：cookie 不能当 token 用
        secret = get_session_secret(str(tmp_path / "session.key"))
        cookie = create_session(USER, days=7, secret=secret)
        assert verify_terminal_token(cookie, secret=secret) is None
