"""GitLabClient 单元测试：项目识别（resolve_project）等。"""

from types import SimpleNamespace

import httpx
import pytest

from botler.gitlab_client import (
    GitLabClient, GitLabError, is_transient_error,
)


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


class TestFindCommitForIssue:
    """find_commit_for_issue：按提交信息匹配引用指定 issue 的提交（issue #19）。

    任务页面的 commit 链接依赖该查询：Claude 按模板提交（message 含
    "issue #N"）后关闭 issue，成功路径据此把对应提交 sha 落库。
    """

    @staticmethod
    def _commits(messages: list[str]) -> list[dict]:
        """构造 commits 列表（id 为完整 sha，列表按 GitLab 返回顺序=新→旧）。"""
        return [{"id": f"deadbeef000{i}", "title": m.splitlines()[0],
                 "message": m} for i, m in enumerate(messages)]

    def _stub(self, client: GitLabClient, result, error=None) -> dict:
        """用桩替换 _request，记录请求参数。"""
        captured: dict = {}

        def fake_request(method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["params"] = kwargs.get("params")
            if error:
                raise error
            return result

        client._request = fake_request
        return captured

    def test_matches_message_with_issue_iid(self):
        """提交信息含 "issue #7" 时返回该提交的完整 sha（取最近的匹配）。"""
        client = make_client()
        commits = self._commits(
            ["chore: 清理无用代码", "fix: 解决 issue #7", "feat: 新增登录功能"])
        captured = self._stub(client, commits)
        sha = client.find_commit_for_issue(42, 7)
        assert sha == "deadbeef0001"
        assert captured["path"] == "/projects/42/repository/commits"
        assert captured["params"]["per_page"] == 100
        # 不指定 ref_name：查询默认分支（HEAD），与模板 push 的 main 一致
        assert "ref_name" not in captured["params"]

    def test_matches_case_insensitive(self):
        client = make_client()
        commits = self._commits(["fix: 解决 ISSUE #7 的问题", "feat: 首页"])
        self._stub(client, commits)
        assert client.find_commit_for_issue(42, 7) == "deadbeef0000"

    def test_matches_whitespace_variant(self):
        """"issue#7"（无空格）与 "issue # 7"（多空格）都应匹配。"""
        client = make_client()
        commits = self._commits(["fix: 解决 issue#7", "feat: 首页"])
        self._stub(client, commits)
        assert client.find_commit_for_issue(42, 7) == "deadbeef0000"
        commits2 = self._commits(["fix: 解决 issue #  7", "feat: 首页"])
        self._stub(client, commits2)
        assert client.find_commit_for_issue(42, 7) == "deadbeef0000"

    def test_no_false_match_for_longer_iid(self):
        """提交信息引用 issue #70 时，查 issue #7 不应误匹配。"""
        client = make_client()
        commits = self._commits(["fix: 解决 issue #70", "feat: 首页"])
        self._stub(client, commits)
        assert client.find_commit_for_issue(42, 7) is None
        assert client.find_commit_for_issue(42, 70) == "deadbeef0000"

    def test_no_match_returns_none(self):
        client = make_client()
        commits = self._commits(["chore: 无 issue 引用的提交"])
        self._stub(client, commits)
        assert client.find_commit_for_issue(42, 7) is None

    def test_empty_list_returns_none(self):
        client = make_client()
        self._stub(client, [])
        assert client.find_commit_for_issue(42, 7) is None

    def test_api_error_propagates(self):
        """GitLab API 报错应向上抛 GitLabError，由调用方决定是否降级。"""
        client = make_client()
        self._stub(client, None, error=GitLabError("GitLab API 错误 500: boom", 500))
        with pytest.raises(GitLabError):
            client.find_commit_for_issue(42, 7)

    def test_commit_without_id_field_is_skipped(self):
        """异常数据结构（无 id 字段）不应使查询崩溃。"""
        client = make_client()
        client._request = lambda method, path, **kwargs: [
            {"title": "x", "message": "fix: 解决 issue #7"}]
        assert client.find_commit_for_issue(42, 7) is None


class TestLastNoteAuthorId:
    """last_note_author_id（issue #34）：领取判定用的「最后一个发言人」。

    最后一条非系统评论的作者 id；无发言（仅系统事件/无评论）返回 None。
    系统评论（assigned/labeled 等事件）不算「发言」。
    """

    def _stub(self, client: GitLabClient, notes: list[dict]) -> list[str]:
        """替换 _paged，返回固定 notes 并记录请求路径与参数。"""
        captured: list = []

        def fake_paged(path, **kwargs):
            captured.append((path, kwargs))
            return notes

        client._paged = fake_paged
        return captured

    def test_returns_last_non_system_author(self):
        """混合评论：跳过 system 评论，返回最后一条普通评论的作者。"""
        client = make_client()
        self._stub(client, [
            {"id": 1, "system": True, "author": {"id": 1}},
            {"id": 2, "system": False, "author": {"id": 99}},
            {"id": 3, "system": True, "author": {"id": 7}},   # 事件不算发言
            {"id": 4, "system": False, "author": {"id": 7}},   # 用户回复
        ])
        assert client.last_note_author_id(42, 7) == 7

    def test_skips_trailing_system_notes(self):
        """最后一条是 system 评论时向前找最近的普通评论作者。"""
        client = make_client()
        self._stub(client, [
            {"id": 2, "system": False, "author": {"id": 99}},
            {"id": 3, "system": True, "author": {"id": 1}},
        ])
        assert client.last_note_author_id(42, 7) == 99

    def test_no_notes_returns_none(self):
        client = make_client()
        self._stub(client, [])
        assert client.last_note_author_id(42, 7) is None

    def test_only_system_notes_returns_none(self):
        """仅系统事件（如新指派）无实质发言 → None（视为新任务可领取）。"""
        client = make_client()
        self._stub(client, [
            {"id": 1, "system": True, "author": {"id": 1}},
            {"id": 2, "system": True, "author": {"id": 1}},
        ])
        assert client.last_note_author_id(42, 7) is None

    def test_note_without_author_is_skipped(self):
        """异常数据结构（无 author 字段）不应崩溃，继续向前找。"""
        client = make_client()
        self._stub(client, [
            {"id": 1, "system": False, "author": {"id": 1}},
            {"id": 2, "system": False},  # 无 author
        ])
        assert client.last_note_author_id(42, 7) == 1


class TestListOpenIssues:
    """list_open_issues（issue #64 扩展）：新增 order_by/sort/limit 透传，
    默认值不变保持向后兼容（reconciler 等既有调用不受影响）。"""

    def _stub(self, client: GitLabClient) -> list[tuple[str, dict]]:
        """替换 _paged，记录请求路径与参数。"""
        captured: list = []

        def fake_paged(path, **kwargs):
            captured.append((path, kwargs))
            return []

        client._paged = fake_paged
        return captured

    def test_default_params_unchanged(self):
        """不传新参数：行为与扩展前一致（state=opened + scope，无 order_by）。"""
        client = make_client()
        captured = self._stub(client)
        client.list_open_issues(42)
        assert captured == [("/projects/42/issues",
                             {"state": "opened", "scope": "all"})]

    def test_assignee_and_scope_still_forwarded(self):
        client = make_client()
        captured = self._stub(client)
        client.list_open_issues(42, assignee_id=7, scope="assigned_to_me")
        assert captured[0][1] == {"state": "opened",
                                  "scope": "assigned_to_me", "assignee_id": 7}

    def test_order_by_and_sort_forwarded(self):
        """聚合概览（issue #64）：仓库内按最后更新时间降序，服务端排序。"""
        client = make_client()
        captured = self._stub(client)
        client.list_open_issues(42, order_by="updated_at", sort="desc")
        assert captured[0][1] == {"state": "opened", "scope": "all",
                                  "order_by": "updated_at", "sort": "desc"}

    def test_limit_forwarded(self):
        client = make_client()
        captured = self._stub(client)
        client.list_open_issues(42, limit=100)
        assert captured[0][1] == {"state": "opened", "scope": "all",
                                  "limit": 100}

    def test_limit_none_not_forwarded(self):
        """limit 未传时不透传 None（保持请求参数干净）。"""
        client = make_client()
        captured = self._stub(client)
        client.list_open_issues(42, order_by=None, sort=None, limit=None)
        assert captured[0][1] == {"state": "opened", "scope": "all"}


class TestCreateIssue:
    """GitLabClient.create_issue：描述为空时用标题填充 description
    （issue #103 用户反馈：未输入描述时，发送 GitLab API 请求应把
    标题内容填充到 description 字段）。用桩替换 _request 记录请求体
    断言发送内容。"""

    def _stub(self, client: GitLabClient) -> list[tuple[str, str, dict]]:
        """用桩替换 _request，记录 (method, path, json) 调用参数。"""
        captured: list[tuple[str, str, dict]] = []

        def fake_request(method, path, **kwargs):
            captured.append((method, path, kwargs.get("json")))
            return {"iid": 99, "title": kwargs["json"]["title"],
                    "state": "opened"}

        client._request = fake_request
        return captured

    def _create(self, client, description=None, title="新 issue",
                assignee_id=None, labels=None):
        return client.create_issue(42, title, description=description,
                                   assignee_id=assignee_id, labels=labels)

    def test_description_none_falls_back_to_title(self):
        """描述缺失（None）→ 发送 API 请求时 description 填充为标题。"""
        client = make_client()
        captured = self._stub(client)

        issue = self._create(client, description=None)

        assert issue["iid"] == 99
        method, path, body = captured[0]
        assert (method, path) == ("POST", "/projects/42/issues")
        assert body == {"title": "新 issue", "description": "新 issue"}

    def test_description_empty_string_falls_back_to_title(self):
        """描述为空字符串 → 同样填充为标题。"""
        client = make_client()
        captured = self._stub(client)

        self._create(client, description="")

        assert captured[0][2] == {"title": "新 issue",
                                  "description": "新 issue"}

    def test_description_preserved_when_provided(self):
        """描述非空（用户手写）→ 保持原样，不被标题覆盖。"""
        client = make_client()
        captured = self._stub(client)

        self._create(client, description="用户手写的描述")

        assert captured[0][2] == {"title": "新 issue",
                                  "description": "用户手写的描述"}

    def test_description_equals_title_unchanged(self):
        """描述与标题相同 → 兜底逻辑不产生变化。"""
        client = make_client()
        captured = self._stub(client)

        self._create(client, description="新 issue")

        assert captured[0][2] == {"title": "新 issue",
                                  "description": "新 issue"}

    def test_assignee_and_labels_still_forwarded(self):
        """回归：兜底改动不影响 assignee_ids/labels 的转发。"""
        client = make_client()
        captured = self._stub(client)

        self._create(client, assignee_id=20, labels=["bug", "ui"])

        assert captured[0][2] == {"title": "新 issue",
                                  "description": "新 issue",
                                  "assignee_ids": [20],
                                  "labels": "bug,ui"}


class TestReopenIssue:
    """reopen_issue：autoclose 误关后平台侧恢复（issue #109）。"""

    def _stub(self, client: GitLabClient) -> list:
        """用桩替换 _request，记录 (method, path, kwargs) 调用序列。"""
        captured: list = []

        def fake_request(method, path, **kwargs):
            captured.append((method, path, kwargs))
            return {"id": 7, "iid": 24, "state": "opened"}

        client._request = fake_request
        return captured

    def test_reopen_sends_state_event(self):
        """reopen 通过 PUT state_event=reopen 实现（与 close_issue 对称）。"""
        client = make_client()
        captured = self._stub(client)

        issue = client.reopen_issue(125, 24)

        assert issue["state"] == "opened"
        assert captured == [("PUT", "/projects/125/issues/24",
                             {"json": {"state_event": "reopen"}})]

    def test_reopen_request_error_propagates(self):
        """API 报错时抛 GitLabError（调用方决定容错策略）。"""
        client = make_client()

        def boom(method, path, **kwargs):
            raise GitLabError("500 Internal Server Error")

        client._request = boom
        with pytest.raises(GitLabError):
            client.reopen_issue(125, 24)


class TestReplyToNote:
    """reply_to_note（issue #125）：回复 issue 评论 = 向该评论所在
    discussion 追加 note。

    notes API 响应不含 discussion_id（GitLab 19 实测），故先 GET
    discussions 解析目标 note 所在 discussion id，再 POST discussions
    带 in_reply_to_discussion_id 追加；note 不存在（含异常数据）抛
    404，回复成功返回创建的 note 对象。
    """

    def _stub(self, client: GitLabClient, discussions: list[dict],
              post: dict | None = None, post_error: Exception | None = None):
        """替换 _paged（discussions 列表）与 _request（POST 回复），
        记录调用参数。"""
        captured: dict = {"paged": [], "request": []}

        def fake_paged(path, **kwargs):
            captured["paged"].append((path, kwargs))
            return discussions

        def fake_request(method, path, **kwargs):
            captured["request"].append((method, path, kwargs))
            if post_error is not None:
                raise post_error
            return post or {"id": "disc-reply",
                            "notes": [{"id": 888, "body": "回复内容",
                                       "system": False}]}

        client._paged = fake_paged
        client._request = fake_request
        return captured

    def test_replies_to_note_in_its_discussion(self):
        """找到目标 note 所在 discussion，向该 discussion 追加回复。"""
        client = make_client()
        captured = self._stub(client, [
            {"id": "d1", "notes": [{"id": 5}, {"id": 6}]},
            {"id": "d2", "notes": [{"id": 7}]},
        ])

        note = client.reply_to_note(42, 7, 6, "收到")

        # 先拉 discussions 解析 note 所在线程
        assert captured["paged"] == [
            ("/projects/42/issues/7/discussions", {})]
        # 再向该 discussion 追加回复（in_reply_to_discussion_id）
        method, path, kwargs = captured["request"][0]
        assert method == "POST"
        assert path == "/projects/42/issues/7/discussions"
        assert kwargs["json"] == {"body": "收到",
                                  "in_reply_to_discussion_id": "d1"}
        assert note["id"] == 888
        assert note["body"] == "回复内容"

    def test_note_in_first_discussion(self):
        """目标 note 在第一条 discussion 中（单评论线程场景）。"""
        client = make_client()
        captured = self._stub(client, [
            {"id": "only", "notes": [{"id": 5}]},
        ])

        client.reply_to_note(42, 7, 5, "hi")

        assert captured["request"][0][2]["json"] == {
            "body": "hi", "in_reply_to_discussion_id": "only"}

    def test_note_not_found_raises_404(self):
        """discussions 中找不到目标 note → 404，且不发回复请求。"""
        client = make_client()
        captured = self._stub(client, [
            {"id": "d1", "notes": [{"id": 5}]},
            {"id": "d2", "notes": [{"id": 6}]},
        ])

        with pytest.raises(GitLabError) as exc:
            client.reply_to_note(42, 7, 999, "hi")

        assert exc.value.status_code == 404
        assert captured["request"] == []

    def test_empty_discussions_raises_404(self):
        """无任何 discussion → 404。"""
        client = make_client()
        captured = self._stub(client, [])

        with pytest.raises(GitLabError) as exc:
            client.reply_to_note(42, 7, 1, "hi")

        assert exc.value.status_code == 404
        assert captured["request"] == []

    def test_malformed_discussion_entries_skipped(self):
        """异常数据（非 dict discussion / 缺 notes / 非 dict note）
        应跳过，不影响正常查找。"""
        client = make_client()
        captured = self._stub(client, [
            "bad-string",
            {"id": "broken", "notes": "not-a-list"},
            {"id": "d3", "notes": [None, "x", {"id": 42}]},
            {"id": "d4", "notes": [{"id": 43}]},
        ])

        note = client.reply_to_note(42, 7, 42, "hi")

        assert note["id"] == 888
        assert captured["request"][0][2]["json"] == {
            "body": "hi", "in_reply_to_discussion_id": "d3"}

    def test_reply_response_without_notes_returns_discussion(self):
        """响应缺 notes（异常结构）时原样返回，由调用方容错。"""
        client = make_client()
        captured = self._stub(client,
                              [{"id": "d1", "notes": [{"id": 1}]}],
                              post={"id": "odd-response"})

        note = client.reply_to_note(42, 7, 1, "hi")

        assert note == {"id": "odd-response"}

    def test_gitlab_error_propagates(self):
        """上游报错原样上抛（由 API 层映射 HTTP 状态）。"""
        client = make_client()
        captured = self._stub(
            client,
            [{"id": "d1", "notes": [{"id": 1}]}],
            post_error=GitLabError("GitLab API 错误 500: boom", 500))

        with pytest.raises(GitLabError) as exc:
            client.reply_to_note(42, 7, 1, "hi")

        assert exc.value.status_code == 500


class TestCreateProjectLabel:
    """GitLabClient.create_project_label（issue #157：添加仓库时补齐标记库
    默认标签）：POST /projects/:id/labels 请求体组装。"""

    def _stub(self, client: GitLabClient) -> list[tuple[str, str, dict]]:
        """用桩替换 _request，记录 (method, path, json) 调用参数。"""
        captured: list[tuple[str, str, dict]] = []

        def fake_request(method, path, **kwargs):
            captured.append((method, path, kwargs.get("json")))
            return {"id": 1, "name": kwargs["json"]["name"],
                    "color": kwargs["json"]["color"]}

        client._request = fake_request
        return captured

    def test_creates_label_with_description(self):
        """带描述创建：POST /projects/42/labels，body 含 name/color/description。"""
        client = make_client()
        captured = self._stub(client)

        label = client.create_project_label(42, "bug", "#d9534f", "缺陷修复")

        assert label["name"] == "bug"
        method, path, body = captured[0]
        assert (method, path) == ("POST", "/projects/42/labels")
        assert body == {"name": "bug", "color": "#d9534f", "description": "缺陷修复"}

    def test_description_none_omitted(self):
        """描述缺省（None）→ 请求体不携带 description 字段。"""
        client = make_client()
        captured = self._stub(client)

        client.create_project_label(42, "feature", "#009966")

        assert captured[0][2] == {"name": "feature", "color": "#009966"}


class TestIsTransientError:
    """is_transient_error：瞬时故障（可重试）与永久性错误分类（issue #280）。

    08-17 生产事故根因：GitLab 短暂不可用返回 502（SafeLine WAF 兜底页），
    任务启动阶段 get_issue 一次 502 即全部判失败。修复后 5xx/429/传输层
    故障视为瞬时、退避重试，4xx 永久性错误不重试。
    """

    def test_transient_status_codes(self):
        for code in (429, 500, 502, 503, 504):
            assert is_transient_error(GitLabError("x", code)), f"{code} 应视为瞬时"

    def test_non_transient_status_codes(self):
        for code in (400, 401, 403, 404, 422):
            assert not is_transient_error(GitLabError("x", code)), f"{code} 不应重试"

    def test_transport_error_is_transient(self):
        """传输层故障（连接超时/拒绝/DNS）由 httpx 异常包裹为 cause → 瞬时。"""
        e = GitLabError("GitLab 请求失败（/user）: timed out")
        e.__cause__ = httpx.ConnectTimeout("timed out")
        assert is_transient_error(e)

    def test_transport_error_with_http_status_is_not_transient(self):
        """带明确 4xx 状态码的错误即使有传输层 cause 也不视为瞬时。"""
        e = GitLabError("token 无效或已过期（401）", 401)
        e.__cause__ = httpx.ConnectTimeout("timed out")
        assert not is_transient_error(e)


class TestTransientRequestRetry:
    """_request/_paged 对 GET 读取做瞬时故障退避重试（issue #280）。"""

    @staticmethod
    def _resp(status_code: int, text: str = "", json_data=None):
        """构造最小响应桩（含 status_code/text/content/json()）。"""
        resp = SimpleNamespace(status_code=status_code, text=text,
                               content=b"x", _json=json_data)
        resp.json = lambda: resp._json
        return resp

    @staticmethod
    def _install(client, responses, monkeypatch):
        """替换 _http：request() 依次弹出 responses（异常直接抛），返回调用记录。"""
        calls: list[tuple] = []

        class FakeHttp:
            def request(self, method, path, **kwargs):
                calls.append((method, path))
                item = responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        client._http = FakeHttp()
        monkeypatch.setattr("botler.gitlab_client.time.sleep", lambda s: None)
        return calls

    def test_get_502_twice_then_success(self, monkeypatch):
        """GET 前两次 502、第三次 200 → get_issue 重试后成功。"""
        client = make_client()
        responses = [self._resp(502, "Bad Gateway"),
                     self._resp(502, "Bad Gateway"),
                     self._resp(200, json_data={"state": "opened"})]
        calls = self._install(client, responses, monkeypatch)
        issue = client.get_issue(123, 280)
        assert issue == {"state": "opened"}
        assert len(calls) == 3, "502 应退避重试到成功"

    def test_get_transient_exhausted_raises_502(self, monkeypatch):
        """持续 502 → 重试耗尽后仍抛 GitLabError(502)。"""
        client = make_client()
        calls = self._install(client, [self._resp(502, "Bad Gateway")] * 3, monkeypatch)
        with pytest.raises(GitLabError) as ei:
            client.get_issue(123, 280)
        assert ei.value.status_code == 502
        assert len(calls) == 3

    def test_get_connect_timeout_retried(self, monkeypatch):
        """传输层超时 → 重试；第三次成功。"""
        client = make_client()
        responses = [httpx.ConnectTimeout("timed out"),
                     httpx.ConnectTimeout("timed out"),
                     self._resp(200, json_data={"state": "opened"})]
        calls = self._install(client, responses, monkeypatch)
        assert client.get_issue(123, 280) == {"state": "opened"}
        assert len(calls) == 3

    def test_get_404_not_retried(self, monkeypatch):
        """404 永久性错误 → 只请求一次。"""
        client = make_client()
        calls = self._install(client, [self._resp(404, "Not Found")], monkeypatch)
        with pytest.raises(GitLabError) as ei:
            client.get_issue(123, 280)
        assert ei.value.status_code == 404
        assert len(calls) == 1

    def test_post_502_not_retried(self, monkeypatch):
        """写操作（评论 POST）遇 502 不重试，避免重复提交。"""
        client = make_client()
        calls = self._install(client, [self._resp(502, "Bad Gateway")], monkeypatch)
        with pytest.raises(GitLabError) as ei:
            client.add_comment(123, 280, "hello")
        assert ei.value.status_code == 502
        assert len(calls) == 1

    def test_paged_502_retried_then_success(self, monkeypatch):
        """分页读取遇瞬时 502 → 重试成功后正常返回，不会整体中断。"""
        client = make_client()
        responses = [self._resp(502, "Bad Gateway"),
                     self._resp(200, json_data=[{"iid": 1}])]
        calls = self._install(client, responses, monkeypatch)
        items = client.list_open_issues(123)
        assert [i["iid"] for i in items] == [1]
        assert len(calls) == 2, "502 重试后成功，一次分页请求完成"
