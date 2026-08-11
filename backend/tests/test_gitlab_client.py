"""GitLabClient 单元测试：项目识别（resolve_project）等。"""

from botler.gitlab_client import GitLabClient


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
