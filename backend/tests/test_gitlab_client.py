"""GitLabClient 单元测试：项目识别（resolve_project）等。"""

import pytest

from botler.gitlab_client import GitLabClient, GitLabError


def make_client(url: str = "https://gitlab.example.com") -> GitLabClient:
    return GitLabClient(url, "test-token", verify_ssl=False)


class TestResolveProject:
    """resolve_project 应从各种 URL 形态识别出 GitLab 项目。"""

    def _stub(self, client: GitLabClient) -> dict:
        """用桩替换 _request，记录请求路径。"""
        captured: dict = {}

        def fake_request(method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            return {"id": 42, "path_with_namespace": "group/project"}

        client._request = fake_request
        return captured

    def test_numeric_id(self):
        client = make_client()
        captured = self._stub(client)
        proj = client.resolve_project("42")
        assert proj["id"] == 42
        assert captured["path"] == "/projects/42"

    def test_https_url(self):
        client = make_client()
        captured = self._stub(client)
        client.resolve_project("https://gitlab.example.com/group/project.git")
        assert captured["path"] == "/projects/group%2Fproject"

    def test_ssh_scheme_url(self):
        client = make_client()
        captured = self._stub(client)
        client.resolve_project("ssh://git@gitlab.example.com/group/project.git")
        assert captured["path"] == "/projects/group%2Fproject"

    def test_scp_like_ssh_url(self):
        """git remote -v 最常见的形态：git@host:group/project.git。

        该形态没有 scheme，urlparse 会把整串当 path，必须单独解析。
        """
        client = make_client()
        captured = self._stub(client)
        proj = client.resolve_project("git@gitlab.example.com:group/project.git")
        assert proj["id"] == 42
        assert captured["path"] == "/projects/group%2Fproject"

    def test_scp_like_ssh_url_no_git_suffix(self):
        client = make_client()
        captured = self._stub(client)
        client.resolve_project("git@gitlab.example.com:group/project")
        assert captured["path"] == "/projects/group%2Fproject"

    def test_scp_like_nested_group(self):
        client = make_client()
        captured = self._stub(client)
        client.resolve_project("git@gitlab.example.com:group/sub/project.git")
        assert captured["path"] == "/projects/group%2Fsub%2Fproject"


class TestIsPrivateUrl:
    """webhook URL 是否指向本地/私有网络（GitLab 默认拒绝注册这类地址）。"""

    @pytest.mark.parametrize("url", [
        "http://10.10.10.10:8000/webhook/gitlab",  # 内网 10.x（用户现场）；避开镜像脱敏精确替换的那个 10.x 地址，否则断言数据被改写
        "http://192.168.1.5/webhook/gitlab",        # 内网 192.168.x
        "http://172.16.0.1/webhook/gitlab",         # 内网 172.16-31.x
        "http://127.0.0.1:8000/webhook/gitlab",     # loopback
        "http://localhost:8000/webhook/gitlab",     # localhost
        "http://[::1]:8000/webhook/gitlab",         # IPv6 loopback
        "http://[fd00::1]/webhook/gitlab",          # IPv6 ULA
    ])
    def test_private_urls_detected(self, url):
        from botler.gitlab_client import _is_private_url
        assert _is_private_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://example.com/webhook/gitlab",       # 域名（不做 DNS 解析）
        "http://8.8.8.8/webhook/gitlab",            # 公网 IP
        "https://home.chenkaidi.top/webhook/gitlab",  # 域名
        "",                                          # 空
        "not-a-url",                                 # 非法
    ])
    def test_public_or_invalid_urls_not_detected(self, url):
        from botler.gitlab_client import _is_private_url
        assert _is_private_url(url) is False


class TestRegisterWebhookErrorHint:
    """register_webhook 422 + 内网地址时，错误信息应包含可操作提示。"""

    def _client_with_422(self, webhook_base_url: str) -> GitLabClient:
        client = GitLabClient("https://gitlab.example.com", "test-token",
                              verify_ssl=False, webhook_base_url=webhook_base_url)
        client.list_webhooks = lambda project_id: []

        def boom(method, path, **kwargs):
            raise GitLabError("GitLab API 错误 422: Invalid url given", 422)

        client._request = boom
        return client

    def test_private_ip_422_has_actionable_hint(self):
        client = self._client_with_422("http://10.10.10.10:8000")
        with pytest.raises(GitLabError) as ei:
            client.register_webhook(123, "secret")
        msg = str(ei.value)
        # 保留原始错误信息，同时追加可操作提示
        assert "Invalid url given" in msg
        assert "Allow requests to the local network" in msg

    def test_public_url_422_no_hint(self):
        client = self._client_with_422("https://example.com")
        with pytest.raises(GitLabError) as ei:
            client.register_webhook(123, "secret")
        assert "Allow requests to the local network" not in str(ei.value)

    def test_other_error_no_hint(self):
        client = GitLabClient("https://gitlab.example.com", "test-token",
                              verify_ssl=False, webhook_base_url="http://10.10.10.10:8000")
        client.list_webhooks = lambda project_id: []

        def boom(method, path, **kwargs):
            raise GitLabError("GitLab API 错误 403: Forbidden", 403)

        client._request = boom
        with pytest.raises(GitLabError) as ei:
            client.register_webhook(123, "secret")
        assert "Allow requests to the local network" not in str(ei.value)
