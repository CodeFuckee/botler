"""任务完成 Webhook 消息推送测试（issue #136）。

覆盖：
- 配置解析默认值（enabled=false / content_type=application/json /
  body_template 留空 = 内置默认模板）
- 占位符变量构建（task / repo / issue 组合、issue 链接兜底拼接）
- POST 结构体模板渲染（占位符逐项替换、默认模板）
- 发送链路（httpx mock：成功 / 非 2xx / 网络异常）
- 未启用 / 未配置地址不发送；测试推送未配置地址报错
"""

from types import SimpleNamespace

import httpx
import pytest

from botler.config import ConfigManager, DEFAULT_WEBHOOK_TEMPLATE
from botler.webhook_push import WebhookPusher, WebhookPushError

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
"""

TASK = {
    "id": 42,
    "project_id": 123,
    "issue_iid": 7,
    "issue_title": "修复登录页崩溃",
    "repo_id": 1,
}
REPO = {"name": "my/repo", "url": "https://gitlab.example.com/my/repo.git"}
ISSUE = {
    "title": "修复登录页崩溃",
    "description": "点击登录按钮崩溃",
    "web_url": "https://gitlab.example.com/my/repo/-/issues/7",
    "project_id": 123,
    "iid": 7,
}


@pytest.fixture
def pusher(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    config.load()
    return WebhookPusher(config)


@pytest.fixture
def enabled_config(tmp_path):
    """已启用且配置完整 webhook 地址的配置。"""
    text = CONFIG_TEXT + """\
webhook:
  enabled: true
  url: https://example.com/hooks/botler
  content_type: application/json
  authorization: Bearer secret-token
  body_template: '{"repo":"{repo_name}","issue":"{issue_title}","url":"{issue_url}"}'
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    config = ConfigManager(str(config_path))
    config.load()
    return WebhookPusher(config)


class TestConfigDefaults:
    def test_defaults(self, pusher):
        """未配置 webhook 段时：关闭、默认 content_type、默认模板。"""
        s = pusher.config.get()
        assert s.webhook_enabled is False
        assert s.webhook_url == ""
        assert s.webhook_content_type == "application/json"
        assert s.webhook_authorization == ""
        assert s.webhook_body_template == DEFAULT_WEBHOOK_TEMPLATE

    def test_config_reads_values(self, enabled_config):
        """配置了 webhook 段后 Settings 正确解析。"""
        s = enabled_config.config.get()
        assert s.webhook_enabled is True
        assert s.webhook_url == "https://example.com/hooks/botler"
        assert s.webhook_content_type == "application/json"
        assert s.webhook_authorization == "Bearer secret-token"
        assert s.webhook_body_template == '{"repo":"{repo_name}","issue":"{issue_title}","url":"{issue_url}"}'

    def test_empty_body_template_falls_back_default(self, tmp_path):
        """body_template 留空归一为内置默认模板（推送内容保证关键信息）。"""
        text = CONFIG_TEXT + 'webhook:\n  enabled: true\n  url: "https://x.example.com/h"\n  body_template: ""\n'
        config_path = tmp_path / "config.yaml"
        config_path.write_text(text, encoding="utf-8")
        config = ConfigManager(str(config_path))
        config.load()
        assert config.get().webhook_body_template == DEFAULT_WEBHOOK_TEMPLATE


class TestBuildVariables:
    def test_full_issue(self, pusher):
        """issue 完整信息时占位符全部填充（含 issue_body/issue_url）。"""
        variables = pusher.build_variables(TASK, repo_name=REPO["name"],
                                           repo_url=REPO["url"], issue=ISSUE)
        assert variables["repo_name"] == "my/repo"
        assert variables["issue_title"] == "修复登录页崩溃"
        assert variables["issue_body"] == "点击登录按钮崩溃"
        assert variables["issue_url"] == "https://gitlab.example.com/my/repo/-/issues/7"
        assert variables["project_id"] == "123"
        assert variables["issue_iid"] == "7"
        assert variables["project_path"] == "my/repo"
        assert variables["project_path_encoded"] == "my%2Frepo"
        assert variables["gitlab_url"] == "https://gitlab.example.com"
        assert variables["gitlab_host"] == "gitlab.example.com"

    def test_issue_none_falls_back(self, pusher):
        """无 issue 信息时降级：正文为空、链接按仓库 URL 拼接。"""
        variables = pusher.build_variables(TASK, repo_name=REPO["name"],
                                           repo_url=REPO["url"])
        assert variables["issue_body"] == ""
        assert variables["issue_url"] == "https://gitlab.example.com/my/repo/-/issues/7"
        assert variables["issue_title"] == "修复登录页崩溃"

    def test_repo_url_missing(self, pusher):
        """无仓库 URL 时 project_path 用仓库名，链接拼接用仓库名。"""
        variables = pusher.build_variables(TASK, repo_name="my/repo")
        assert variables["project_path"] == "my/repo"
        assert variables["issue_url"] == "https://gitlab.example.com/my/repo/-/issues/7"

    def test_issue_url_priority(self, pusher):
        """issue 快照提供的 web_url 优先于拼接兜底。"""
        custom_issue = dict(ISSUE, web_url="https://else.example.com/issue/7")
        variables = pusher.build_variables(TASK, repo_name=REPO["name"],
                                           repo_url=REPO["url"], issue=custom_issue)
        assert variables["issue_url"] == "https://else.example.com/issue/7"


class TestBuildPayload:
    def test_render_custom_template(self, enabled_config):
        """自定义 POST 结构体模板占位符逐项替换。"""
        variables = enabled_config.build_variables(
            TASK, repo_name=REPO["name"], repo_url=REPO["url"], issue=ISSUE)
        payload = enabled_config.build_payload(variables)
        assert '"repo":"my/repo"' in payload
        assert '"issue":"修复登录页崩溃"' in payload
        assert '"url":"https://gitlab.example.com/my/repo/-/issues/7"' in payload
        # 无未替换占位符残留（逐项检查 PLACEHOLDERS，JSON 花括号是合法字符）
        from botler.templates import PLACEHOLDERS
        for key in PLACEHOLDERS:
            assert "{" + key + "}" not in payload, f"占位符 {{{key}}} 未替换"
        # 渲染后仍是合法 JSON（默认/自定义模板均为 JSON 结构体）
        import json
        json.loads(payload)

    def test_render_default_template(self, pusher):
        """默认模板渲染：JSON 结构完整、全部占位符已替换。"""
        variables = pusher.build_variables(TASK, repo_name=REPO["name"],
                                           repo_url=REPO["url"], issue=ISSUE)
        payload = pusher.build_payload(variables)
        for key, val in variables.items():
            assert "{" + key + "}" not in payload, f"占位符 {{{key}}} 未替换"
        assert '"event": "task_succeeded"' in payload
        assert '"iid": "7"' in payload


class TestSend:
    def test_send_success(self, enabled_config, monkeypatch):
        """2xx 响应返回状态码与响应摘要。"""
        fake = httpx.Response(200, text="ok", request=httpx.Request("POST", "https://example.com"))
        captured = {}

        def fake_post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["content"] = content.decode("utf-8")
            captured["headers"] = headers
            return fake

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = enabled_config.send(enabled_config.build_variables(
            TASK, repo_name=REPO["name"], repo_url=REPO["url"], issue=ISSUE))
        assert result["status_code"] == 200
        assert captured["url"] == "https://example.com/hooks/botler"
        assert captured["headers"]["Content-Type"] == "application/json"
        assert captured["headers"]["Authorization"] == "Bearer secret-token"
        assert '"issue":"修复登录页崩溃"' in captured["content"]

    def test_send_non_2xx_raises(self, enabled_config, monkeypatch):
        """非 2xx 响应抛 WebhookPushError（含状态码与响应体摘要）。"""
        fake = httpx.Response(500, text="boom", request=httpx.Request("POST", "https://example.com"))
        monkeypatch.setattr(httpx.Client, "post", lambda self, url, content=None, headers=None: fake)
        with pytest.raises(WebhookPushError, match="500"):
            enabled_config.send(enabled_config.build_variables(TASK, repo_name="r"))

    def test_send_network_error_raises(self, enabled_config, monkeypatch):
        """网络异常抛 WebhookPushError。"""
        def boom(self, url, content=None, headers=None):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx.Client, "post", boom)
        with pytest.raises(WebhookPushError, match="连接|connection"):
            enabled_config.send(enabled_config.build_variables(TASK, repo_name="r"))

    def test_send_without_url_raises(self, pusher):
        """未配置地址时抛 WebhookPushError。"""
        with pytest.raises(WebhookPushError, match="未配置"):
            pusher.send({"repo_name": "r"})

    def test_send_verify_ssl_uses_config(self, enabled_config, monkeypatch):
        """请求 verify 跟随配置（verify_ssl=false）。"""
        created = {}

        class FakeClient:
            def __init__(self, **kwargs):
                created.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, content=None, headers=None):
                return httpx.Response(
                    200, text="ok", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "Client", FakeClient)
        enabled_config.send(enabled_config.build_variables(TASK, repo_name="r"))
        assert created["verify"] is False


class TestSendTaskSucceeded:
    def test_disabled_returns_none(self, pusher):
        """未启用时不发送（返回 None）。"""
        assert pusher.send_task_succeeded(TASK, repo_name=REPO["name"]) is None

    def test_no_url_returns_none(self, tmp_path):
        """启用但未配置地址时不发送（返回 None）。"""
        text = CONFIG_TEXT + 'webhook:\n  enabled: true\n  url: ""\n'
        config_path = tmp_path / "config.yaml"
        config_path.write_text(text, encoding="utf-8")
        config = ConfigManager(str(config_path))
        config.load()
        pusher = WebhookPusher(config)
        assert pusher.send_task_succeeded(TASK, repo_name=REPO["name"]) is None

    def test_enabled_sends(self, enabled_config, monkeypatch):
        """启用且配置地址时发送，返回响应摘要。"""
        fake = httpx.Response(200, text="ok", request=httpx.Request("POST", "https://example.com"))
        monkeypatch.setattr(httpx.Client, "post", lambda self, url, content=None, headers=None: fake)
        result = enabled_config.send_task_succeeded(
            TASK, repo_name=REPO["name"], repo_url=REPO["url"], issue=ISSUE)
        assert result["status_code"] == 200


class TestSendTest:
    def test_no_url_raises(self, pusher):
        """测试推送未配置地址抛 WebhookPushError。"""
        with pytest.raises(WebhookPushError, match="webhook 地址未配置"):
            pusher.send_test()

    def test_send_test_ok(self, enabled_config, monkeypatch):
        """测试推送发送成功，使用测试数据渲染模板。"""
        captured = {}

        def fake_post(self, url, content=None, headers=None):
            captured["content"] = content.decode("utf-8")
            return httpx.Response(200, text="ok", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = enabled_config.send_test()
        assert result["status_code"] == 200
        assert "测试推送" in captured["content"]
        assert '"issue":"测试推送（Botler 设置页）"' in captured["content"]
