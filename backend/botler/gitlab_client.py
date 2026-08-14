"""GitLab REST API 集成层（/api/v4）。

统一身份：bot 账号 PAT（scope: api + write_repository），
各仓库角色 Maintainer（webhook 注册需要）。

提供：token 校验、项目识别、webhook 注册/注销、issue 查询/评论/标签、open issues 列表。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from urllib.parse import urlparse, unquote

import httpx

logger = logging.getLogger(__name__)

HOOK_URL_PATH = "/webhook/gitlab"
# 超过该延迟即丢弃 webhook 事件（GitLab 默认超时 10s）
HOOK_TIMEOUT_SECONDS = 5

# 流水线终态（issue #40）：任务成功收尾前等待任务触发的流水线到达
# 这些状态之一。success/skipped 视为通过，failed/canceled 视为失败。
PIPELINE_TERMINAL_STATES = ("success", "failed", "canceled", "skipped")


def _is_private_url(url: str) -> bool:
    """URL 是否指向本地/私有网络地址（GitLab 默认拒绝注册这类 webhook）。

    只判断字面 IP（含 localhost / loopback / 私有网段）；域名不做 DNS 解析，
    视为外部地址——域名是否解析到内网由 GitLab 侧判断。
    """
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _looks_like_scp_url(value: str) -> bool:
    """git remote -v 的 scp-like 形态：user@host:path/to/repo.git。

    特征：含 ':' 与 '/'（有仓库路径），且不是带 scheme 的 URL
    （带 scheme 的会被 urlparse 分支处理）。
    """
    return ":" in value and "/" in value and "://" not in value


class GitLabError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GitLabClient:
    def __init__(self, url: str, token: str, verify_ssl: bool = True,
                 webhook_base_url: str | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self.webhook_base_url = (webhook_base_url or self.url).rstrip("/")
        self.verify_ssl = verify_ssl
        self._bot_id: int | None = None
        self._http = httpx.Client(
            base_url=f"{self.url}/api/v4",
            headers={"PRIVATE-TOKEN": token},
            timeout=30,
            verify=verify_ssl,
        )

    # ---- 基础请求 ----

    def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        resp = self._http.request(method, path, **kwargs)
        if resp.status_code == 401:
            raise GitLabError("token 无效或已过期（401）", 401)
        if resp.status_code == 403:
            raise GitLabError(f"权限不足（403）: {resp.text[:200]}", 403)
        if resp.status_code == 404:
            raise GitLabError(f"资源不存在（404）: {path}", 404)
        if resp.status_code >= 400:
            raise GitLabError(f"GitLab API 错误 {resp.status_code}: {resp.text[:300]}", resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _paged(self, path: str, limit: int | None = None, **kwargs) -> list[dict]:
        """分页拉取（per_page=100）；limit 非空时最多取 limit 条即停止翻页。"""
        items: list[dict] = []
        page = 1
        while True:
            resp = self._http.request(
                "GET", path, params={"page": page, "per_page": 100, **kwargs})
            if resp.status_code == 404:
                break
            if resp.status_code >= 400:
                raise GitLabError(f"GitLab API 错误 {resp.status_code}: {resp.text[:300]}", resp.status_code)
            batch = resp.json()
            items.extend(batch)
            if limit is not None and len(items) >= limit:
                break
            if len(batch) < 100:
                break
            page += 1
        return items[:limit] if limit is not None else items

    # ---- 认证与 bot 身份 ----

    def test_connection(self) -> dict:
        """验证 token 有效性，返回当前用户信息。"""
        user = self._request("GET", "/user")
        assert isinstance(user, dict)
        return user

    def get_bot_id(self, force: bool = False) -> int:
        """获取 bot 账号的 GitLab 用户 ID（首次调用缓存）。"""
        if self._bot_id is None or force:
            user = self.test_connection()
            self._bot_id = int(user["id"])
        return self._bot_id

    def get_user_id_by_username(self, username: str) -> int | None:
        """按用户名查用户 id；用户不存在返回 None。

        issue #65：remote URL userinfo 的用户名（如 agent）作为 bot 身份
        提示时，用它解析真实账号 id 加入身份集合。
        """
        username = (username or "").strip()
        if not username:
            return None
        users = self._request("GET", "/users", params={"username": username})
        if isinstance(users, list) and users:
            return int(users[0]["id"])
        return None

    # ---- 项目 ----

    def get_project(self, project_id: int) -> dict:
        proj = self._request("GET", f"/projects/{project_id}")
        assert isinstance(proj, dict)
        return proj

    def get_project_by_path(self, path: str) -> dict:
        """path 形如 group/project，需 URL 编码。"""
        encoded = path.replace("/", "%2F")
        proj = self._request("GET", f"/projects/{encoded}")
        assert isinstance(proj, dict)
        return proj

    def resolve_project(self, url_or_id: str) -> dict:
        """从仓库 URL 或数字 ID 识别项目。

        URL 支持 https://host/group/project.git、ssh://git@host/group/project.git，
        以及 git remote -v 常见的 scp-like 形态 git@host:group/project.git。
        """
        value = url_or_id.strip()
        if value.isdigit():
            return self.get_project(int(value))
        parsed = urlparse(value)
        if parsed.scheme in ("http", "https", "ssh"):
            path = parsed.path.strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return self.get_project_by_path(unquote(path))
        if _looks_like_scp_url(value):
            # scp-like 无 scheme，urlparse 会把整串当 path；
            # 从最后一个 ':' 之后取仓库路径
            path = value.rpartition(":")[2].strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return self.get_project_by_path(unquote(path))
        return self.get_project_by_path(unquote(value.lstrip("/")))

    def list_projects(self, membership: bool = True, search: str | None = None) -> list[dict]:
        return self._paged("/projects", membership=membership, search=search)

    # ---- webhook ----

    def webhook_url(self) -> str:
        return f"{self.webhook_base_url}{HOOK_URL_PATH}"

    def list_webhooks(self, project_id: int) -> list[dict]:
        return self._paged(f"/projects/{project_id}/hooks")

    def register_webhook(self, project_id: int, secret: str) -> dict:
        """注册 issue 事件 webhook；若已存在同 URL 的 hook 则更新其配置。"""
        url = self.webhook_url()
        for hook in self.list_webhooks(project_id):
            if hook.get("url") == url:
                updated = self._request(
                    "PUT", f"/projects/{project_id}/hooks/{hook['id']}",
                    json={
                        "url": url, "issues_events": True,
                        "push_events": False, "token": secret,
                        "enable_ssl_verification": False,
                    })
                assert isinstance(updated, dict)
                logger.info("更新已有 webhook: project=%s hook=%s", project_id, hook["id"])
                return updated
        try:
            created = self._request(
                "POST", f"/projects/{project_id}/hooks",
                json={
                    "url": url, "issues_events": True,
                    "push_events": False, "token": secret,
                    "enable_ssl_verification": False,
                })
        except GitLabError as e:
            if e.status_code == 422 and _is_private_url(url):
                raise GitLabError(
                    f"{e}。GitLab 默认禁止向本地/私有网络地址注册 webhook，请在 "
                    "GitLab Admin → Settings → Network → Outbound requests 勾选 "
                    "「Allow requests to the local network from webhooks and "
                    "integrations」，或用公网可达的回调地址（添加仓库时的 "
                    "webhook_url 字段）。",
                    e.status_code)
            raise
        assert isinstance(created, dict)
        logger.info("注册 webhook: project=%s hook=%s", project_id, created["id"])
        return created

    def unregister_webhook(self, project_id: int) -> int:
        """注销平台注册的 webhook，返回删除个数。"""
        url = self.webhook_url()
        removed = 0
        for hook in self.list_webhooks(project_id):
            if hook.get("url") == url:
                self._request("DELETE", f"/projects/{project_id}/hooks/{hook['id']}")
                removed += 1
        if removed:
            logger.info("注销 webhook: project=%s removed=%s", project_id, removed)
        return removed

    def test_webhook(self, project_id: int) -> tuple[bool, str]:
        """测试 webhook 连通性（GitLab 向平台发 ping）。"""
        hooks = self.list_webhooks(project_id)
        url = self.webhook_url()
        hook = next((h for h in hooks if h.get("url") == url), None)
        if not hook:
            return False, "平台 webhook 未注册"
        hook_id = hook["id"]
        last = hook.get("last_response") or {}
        if last.get("http_status") in (200, 201, 202, 204):
            return True, f"最近一次触发 HTTP {last.get('http_status')}"
        if last.get("http_status") == 404:
            return False, "平台返回 404（webhook 路径不可达，检查端口/防火墙）"
        if last.get("http_status") == 401:
            return False, "平台返回 401（webhook secret 不匹配）"
        return False, f"尚未触发或状态未知: {last}"

    # ---- pipelines ----

    def get_latest_pipeline(self, project_id: int) -> dict | None:
        """项目最新一次流水线（按 id 倒序第一条）；无流水线返回 None。"""
        pipelines = self._request(
            "GET", f"/projects/{project_id}/pipelines",
            params={"per_page": 1, "order_by": "id", "sort": "desc"})
        if not isinstance(pipelines, list) or not pipelines:
            return None
        return pipelines[0]

    def get_pipeline(self, project_id: int, pipeline_id: int) -> dict | None:
        """单条流水线详情（executor 等待终态时轮询用）。"""
        pipeline = self._request("GET", f"/projects/{project_id}/pipelines/{pipeline_id}")
        if not isinstance(pipeline, dict):
            return None
        return pipeline

    def list_pipeline_jobs(self, project_id: int, pipeline_id: int) -> list[dict]:
        """流水线全部 jobs（含 stage / status / allow_failure，供概览页聚合 stage 状态）。"""
        return self._paged(f"/projects/{project_id}/pipelines/{pipeline_id}/jobs")

    def get_commit(self, project_id: int, sha: str) -> dict | None:
        """单条提交详情（issue #43：概览页取最近流水线对应提交的提交时间）。

        返回 GitLab commit 对象（含 committed_date）；非 dict 返回 None。
        commit 不存在（force-push 后 sha 失效）由 _request 抛 404 GitLabError，
        由调用方决定降级策略。
        """
        commit = self._request("GET", f"/projects/{project_id}/repository/commits/{sha}")
        if not isinstance(commit, dict):
            return None
        return commit

    # ---- issues ----

    def get_issue(self, project_id: int, iid: int) -> dict:
        issue = self._request("GET", f"/projects/{project_id}/issues/{iid}")
        assert isinstance(issue, dict)
        return issue

    def list_project_labels(self, project_id: int) -> list[dict]:
        """项目标签清单（issue #71：概览页 issue 标签胶囊取 GitLab 标签色）。

        GitLab labels API 返回 [{id, name, color, text_color, description}]，
        color/text_color 为 6 位 hex（不带 #）。标签数量通常远少于一页，
        直接用 _paged 默认分页拉全。
        """
        return self._paged(f"/projects/{project_id}/labels")

    def list_open_issues(self, project_id: int, assignee_id: int | None = None,
                         scope: str = "all", order_by: str | None = None,
                         sort: str | None = None,
                         limit: int | None = None) -> list[dict]:
        """列出 open issues。assignee_id 传入时为该 assignee 的 open issues。

        issue #64：聚合概览需服务端按最后更新时间排序（order_by/sort 透传
        GitLab API）并限制每仓库条数（limit 截断，防大仓库翻页打爆 API）；
        不传新参数时行为与扩展前一致（reconciler 等既有调用不受影响）。
        """
        params: dict = {"state": "opened", "scope": scope}
        if assignee_id is not None:
            params["assignee_id"] = assignee_id
        if order_by:
            params["order_by"] = order_by
        if sort:
            params["sort"] = sort
        if limit is not None:
            return self._paged(f"/projects/{project_id}/issues",
                               limit=limit, **params)
        return self._paged(f"/projects/{project_id}/issues", **params)

    def add_comment(self, project_id: int, iid: int, body: str) -> dict:
        note = self._request(
            "POST", f"/projects/{project_id}/issues/{iid}/notes",
            json={"body": body})
        assert isinstance(note, dict)
        return note

    def add_labels(self, project_id: int, iid: int, labels: list[str],
                   remove: list[str] | None = None) -> dict:
        """加标签；remove 非空时同一次请求移除对应标签（issue #67：收尾
        移除 in-progress，避免与终态标签并存）。"""
        body: dict = {"add_labels": ",".join(labels)}
        if remove:
            body["remove_labels"] = ",".join(remove)
        issue = self._request(
            "PUT", f"/projects/{project_id}/issues/{iid}",
            json=body)
        assert isinstance(issue, dict)
        return issue

    def last_note_author_id(self, project_id: int, iid: int) -> int | None:
        """最后一条非系统评论的作者 id；无发言（仅系统事件/无评论）返回 None。

        领取判定（issue #34）用：bot 提问/处理完留评论后用户未回复时，
        最后发言人是 bot 本人，领取方应跳过；用户回复后（或新任务无评论）
        才允许领取。系统评论（assigned/labeled 等事件）不算「发言」。
        """
        notes = self._paged(
            f"/projects/{project_id}/issues/{iid}/notes",
            sort="asc", order_by="created_at")
        for note in reversed(notes):
            if note.get("system"):
                continue
            author_id = (note.get("author") or {}).get("id")
            if author_id is not None:
                return author_id
        return None

    def is_issue_open(self, project_id: int, iid: int) -> bool:
        issue = self.get_issue(project_id, iid)
        return issue.get("state") == "opened"

    def close_issue(self, project_id: int, iid: int) -> dict:
        """供平台侧使用（对账时误收/放弃任务后关闭用）。"""
        issue = self._request(
            "PUT", f"/projects/{project_id}/issues/{iid}",
            json={"state_event": "close"})
        assert isinstance(issue, dict)
        return issue

    def wait_issue_state(self, project_id: int, iid: int,
                         expect: str, timeout: float = 60) -> bool:
        """轮询等待 issue 到达期望状态（如等待 Claude Code 关闭 issue）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.get_issue(project_id, iid).get("state") == expect:
                    return True
            except GitLabError:
                pass
            time.sleep(2)
        return False

    # ---- commits ----

    def find_commit_for_issue(self, project_id: int, issue_iid: int,
                              limit: int = 100) -> str | None:
        """在仓库提交历史中查找引用指定 issue 的最近提交，返回完整 sha。

        任务页面 commit 链接依赖此查询（issue #19）：Claude 按模板提交
        （message 含 "issue #N"，如 "fix: 解决 issue #7"）后完成任务，
        executor 成功路径据此把对应提交 sha 落库。

        默认查询分支 HEAD（GitLab 默认分支）最近 limit 条提交，按信息
        "issue #N"（大小写/空格不敏感，数字边界精确匹配）取最近一条；
        找不到返回 None（不抛错，由调用方决定页面是否显示链接）。
        """
        commits = self._request(
            "GET", f"/projects/{project_id}/repository/commits",
            params={"per_page": limit})
        if not isinstance(commits, list):
            return None
        pattern = re.compile(rf"issue\s*#\s*{issue_iid}\b", re.IGNORECASE)
        for commit in commits:
            message = (commit or {}).get("message") or ""
            if pattern.search(message):
                sha = commit.get("id")
                return sha if isinstance(sha, str) and sha else None
        return None
