"""GitLab REST API 集成层（/api/v4）。

统一身份：bot 账号 PAT（scope: api + write_repository），
各仓库角色 Maintainer（webhook 注册需要）。

提供：token 校验、项目识别、webhook 注册/注销、issue 查询/评论/标签、open issues 列表。
"""

from __future__ import annotations

import ipaddress
import logging
import time
from urllib.parse import urlparse, unquote

import httpx

logger = logging.getLogger(__name__)

HOOK_URL_PATH = "/webhook/gitlab"
# 超过该延迟即丢弃 webhook 事件（GitLab 默认超时 10s）
HOOK_TIMEOUT_SECONDS = 5


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

    def _paged(self, path: str, **kwargs) -> list[dict]:
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
            if len(batch) < 100:
                break
            page += 1
        return items

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

    # ---- issues ----

    def get_issue(self, project_id: int, iid: int) -> dict:
        issue = self._request("GET", f"/projects/{project_id}/issues/{iid}")
        assert isinstance(issue, dict)
        return issue

    def list_open_issues(self, project_id: int, assignee_id: int | None = None,
                         scope: str = "all") -> list[dict]:
        """列出 open issues。assignee_id 传入时为该 assignee 的 open issues。"""
        params: dict = {"state": "opened", "scope": scope}
        if assignee_id is not None:
            params["assignee_id"] = assignee_id
        return self._paged(f"/projects/{project_id}/issues", **params)

    def add_comment(self, project_id: int, iid: int, body: str) -> dict:
        note = self._request(
            "POST", f"/projects/{project_id}/issues/{iid}/notes",
            json={"body": body})
        assert isinstance(note, dict)
        return note

    def add_labels(self, project_id: int, iid: int, labels: list[str]) -> dict:
        issue = self._request(
            "PUT", f"/projects/{project_id}/issues/{iid}",
            json={"add_labels": ",".join(labels)})
        assert isinstance(issue, dict)
        return issue

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
