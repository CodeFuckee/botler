"""统一日志脱敏工具测试（issue #259）。

验收标准：
1. 构造含 token 的日志输入，输出中敏感串被打码（保留前后几位便于定位）；
2. 正常日志内容不受影响；
3. 脱敏不显著影响性能；
4. 有脱敏单元测试，全量测试无 regression。
"""

import logging
import time

from botler.config import ConfigManager
from botler.log_redact import (
    RedactFilter,
    redact,
    register_config_secrets,
    register_secret,
)


class TestPatternRules:
    """内置正则规则：git remote URL userinfo / Authorization 头 /
    Bearer / GitLab PAT / ${ENV} 引用名。"""

    def test_git_remote_url_userinfo_masked(self):
        """git remote URL 内嵌凭据：密码部分打码，保留前后几位便于定位。"""
        text = "克隆失败：https://agent:glpat-abcdefghijklmnop@gitlab.example.com/repo.git"
        assert redact(text) == \
            "克隆失败：https://agent:glp***nop@gitlab.example.com/repo.git"

    def test_url_userinfo_only_username_kept(self):
        """只有用户名没有密码的 URL 不误伤。"""
        text = "https://agent@gitlab.example.com/repo.git"
        assert redact(text) == text

    def test_scp_like_url_kept(self):
        """scp-like / ssh 形态（无 scheme）不误伤。"""
        text = "git@gitlab.example.com:group/project.git"
        assert redact(text) == text

    def test_authorization_header_masked(self):
        """Authorization 头：Bearer 前缀保留，令牌部分打码。"""
        text = "Authorization: Bearer sk-ant-abcdefghijklmnopqrstuvwxyz"
        assert redact(text) == "Authorization: Bearer sk-***xyz"

    def test_proxy_authorization_header_masked(self):
        """Proxy-Authorization 头同样打码。"""
        text = "Proxy-Authorization: Bearer abcdefgh12345678"
        assert redact(text) == "Proxy-Authorization: Bearer abc***678"

    def test_bare_bearer_token_masked(self):
        """正文里的 Bearer 令牌（无 Authorization 头前缀）打码。"""
        text = "访问失败，请使用 Bearer abcdefgh12345678 重新认证"
        assert redact(text) == "访问失败，请使用 Bearer abc***678 重新认证"

    def test_bearer_common_english_phrase_not_masked(self):
        """「Bearer authentication」等常见英文短语不打码（避免误伤正常日志）。"""
        text = "本接口使用 Bearer authentication 方式认证"
        assert redact(text) == text

    def test_gitlab_pat_pattern_masked(self):
        """裸 GitLab PAT（glpat- 前缀）打码，保留前后几位。"""
        text = "token=glpat-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        assert redact(text) == "token=glpat-ABC***456"

    def test_env_ref_name_masked(self):
        """${ENV} 引用名打码（配置 dump 不暴露凭据引用名）。"""
        text = 'api_key: "${GEMINI_API_KEY}"'
        assert redact(text) == 'api_key: "${GEM***KEY}"'

    def test_url_and_glpat_no_double_mask(self):
        """URL userinfo 与 glpat 规则互不二次打码（幂等）。"""
        text = "https://user:glpat-ABCDEFGHIJ@host/x.git"
        once = redact(text)
        assert once == "https://user:glp***HIJ@host/x.git"
        assert redact(once) == once


class TestDynamicSecrets:
    """register_secret：动态注册密钥子串规则（配置声明的 Key 自动纳入）。"""

    def test_registered_secret_masked(self):
        register_secret("sk-test-secret-987654321")
        text = "上游返回 401，密钥 sk-test-secret-987654321 已失效"
        assert redact(text) == "上游返回 401，密钥 sk-***321 已失效"

    def test_secret_inside_longer_word_not_masked(self):
        """边界防护：密钥嵌在更长单词中间时不误伤（前后加字符边界）。"""
        register_secret("sk-boundary-abcdef")
        text = "xxsk-boundary-abcdefyy 不是独立密钥"
        assert redact(text) == text

    def test_short_secret_not_registered(self):
        """过短的值（<6 字符）不注册，避免把普通内容当密钥误伤。"""
        register_secret("abc")
        text = "abc 是普通内容，不应被打码"
        assert redact(text) == text

    def test_secret_mask_keeps_prefix_suffix(self):
        """打码格式：保留前 3 位 + *** + 后 3 位（如 tok***abc）。"""
        register_secret("tok-1234567890-abc")
        assert redact("值 tok-1234567890-abc 结束") == "值 tok***abc 结束"


class TestConfigSecrets:
    """register_config_secrets：配置中声明的 Key 自动纳入脱敏规则。"""

    def test_config_secret_fields_registered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_REDACT_BOT_TOKEN", "glpat-envref-secret-123456")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "gitlab:\n"
            "  url: https://gitlab.example.com\n"
            "  bot_token: ${TEST_REDACT_BOT_TOKEN}\n"
            "  webhook_secret: whsec-config-secret-123456\n"
            "  verify_ssl: false\n"
            "worker: {}\n"
            "claude: {}\n"
            "templates: {}\n"
            "ai_providers:\n"
            "  - name: p1\n"
            "    provider: custom\n"
            "    base_url: https://api.example.com\n"
            "    api_key: sk-provider-key-123456789\n"
            "    model: m1\n"
            "    enabled: true\n"
            "repos: []\n",
            encoding="utf-8",
        )
        config = ConfigManager(str(cfg))
        settings = config.load()
        register_config_secrets(settings)

        # gitlab.bot_token 的 ${ENV} 引用展开值（来自环境变量）纳入规则：
        # glpat- 前缀先被内置 PAT 规则命中（动态密钥规则兜底其余形态）
        assert "glpat-env***456" in redact(
            "请求带 token glpat-envref-secret-123456 认证")
        # webhook_secret 明文值纳入规则
        assert "whs***456" in redact(
            "校验失败，webhook secret=whsec-config-secret-123456")
        # ai_providers[].api_key 自动纳入规则
        assert "sk-***789" in redact(
            "AI 网关拒绝，api_key=sk-provider-key-123456789")

    def test_repo_url_embedded_credential_registered(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "gitlab:\n"
            "  url: https://gitlab.example.com\n"
            "  bot_token: t\n"
            "  verify_ssl: false\n"
            "worker: {}\n"
            "repos:\n"
            "  - project_id: 1\n"
            "    name: demo\n"
            "    url: https://agent:repopass-secret-123456@gitlab.example.com/demo.git\n"
            "    enabled: true\n",
            encoding="utf-8",
        )
        config = ConfigManager(str(cfg))
        register_config_secrets(config.load())
        assert "rep***456" in redact(
            "仓库地址 https://agent:repopass-secret-123456@gitlab.example.com/demo.git")


class TestRobustness:
    """正常内容不受影响 / 空输入 / 幂等 / 性能。"""

    def test_normal_log_unaffected(self):
        text = ("任务 123 执行完成，用时 5.2s，状态 success\n"
                "第 1 次尝试开始，工作区 /tmp/workspace 已就绪\n"
                "GitLab API 200 OK，流水线 #456 进入 running")
        assert redact(text) == text

    def test_empty_and_none_input(self):
        assert redact("") == ""
        assert redact(None) == ""

    def test_idempotent(self):
        text = ("https://agent:glpat-abcdefghijklmnop@host/x.git "
                "Authorization: Bearer abcdefgh12345678 "
                "glpat-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")
        once = redact(text)
        assert redact(once) == once

    def test_multiline_text(self):
        text = ("line1 正常\n"
                "line2 token=glpat-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456\n"
                "line3 正常结束")
        assert redact(text) == (
            "line1 正常\n"
            "line2 token=glpat-ABC***456\n"
            "line3 正常结束")

    def test_performance_large_input(self):
        """脱敏不显著影响性能：1MB 量级日志在宽松时限内完成。"""
        line = ("任务 1 执行中 Authorization: Bearer abcdefgh12345678 "
                "token=glpat-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 正常日志\n")
        text = line * 8000  # 约 1MB
        start = time.monotonic()
        out = redact(text)
        elapsed = time.monotonic() - start
        assert "abc***678" in out and "glpat-ABC***456" in out
        assert elapsed < 3.0  # 宽松阈值，避免 CI 抖动


class TestRedactFilter:
    """RedactFilter：logging 层统一脱敏入口。"""

    def test_filter_redacts_record_message(self):
        record = logging.LogRecord(
            name="botler.test", level=logging.WARNING, pathname=__file__,
            lineno=1, msg="token=glpat-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            args=(), exc_info=None)
        assert RedactFilter().filter(record) is True
        assert "glpat-ABC***456" in record.getMessage()

    def test_filter_keeps_normal_record(self):
        record = logging.LogRecord(
            name="botler.test", level=logging.INFO, pathname=__file__,
            lineno=1, msg="任务 %s 正常完成", args=(42,), exc_info=None)
        assert RedactFilter().filter(record) is True
        assert record.getMessage() == "任务 42 正常完成"

    def test_filter_with_args_redacted(self):
        record = logging.LogRecord(
            name="botler.test", level=logging.ERROR, pathname=__file__,
            lineno=1, msg="认证失败：%s", args=("Bearer abcdefgh12345678",),
            exc_info=None)
        assert RedactFilter().filter(record) is True
        assert "Bearer abc***678" in record.getMessage()


class TestIntegration:
    """集成点：Database.add_log / GitLabError 构造处统一走脱敏。"""

    def test_database_add_log_redacts(self, tmp_path):
        from botler.database import Database

        db = Database(str(tmp_path / "test.db"))
        db.add_log(1, "error",
                   "失败：Authorization: Bearer abcdefgh12345678 已失效")
        db.add_logs(1, [("info", "token=glpat-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")])
        logs = [dict(row) for row in db.list_logs(1)]
        assert logs[0]["message"] == "失败：Authorization: Bearer abc***678 已失效"
        assert logs[1]["message"] == "token=glpat-ABC***456"

    def test_database_add_log_keeps_normal_message(self, tmp_path):
        from botler.database import Database

        db = Database(str(tmp_path / "test.db"))
        db.add_log(1, "info", "任务 1 执行完成，用时 5.2s")
        logs = [dict(row) for row in db.list_logs(1)]
        assert logs[0]["message"] == "任务 1 执行完成，用时 5.2s"

    def test_gitlab_error_message_redacted(self):
        from botler.gitlab_client import GitLabError

        err = GitLabError("GitLab API 错误 401: https://agent:glpat-abcdefghijklmnop@host/x")
        assert "glp***nop" in str(err)
        assert "glpat-abcdefghijklmnop" not in str(err)

    def test_gitlab_error_keeps_normal_message(self):
        from botler.gitlab_client import GitLabError

        err = GitLabError("资源不存在（404）: /api/v4/projects/123")
        assert str(err) == "资源不存在（404）: /api/v4/projects/123"

    def test_root_handler_filter_covers_child_logger(self):
        """RedactFilter 挂 root handler 后，模块 logger 输出同样脱敏
        （子 logger 记录传播到 root handler 时经过 filter）。"""
        import io

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(RedactFilter())
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.INFO)
        try:
            root.addHandler(handler)
            child = logging.getLogger("botler.integration.test")
            child.warning("认证头 Authorization: Bearer abcdefgh12345678 异常")
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        assert "Bearer abc***678" in stream.getvalue()
