"""GitLab REST API 集成层（/api/v4）。

统一身份：bot 账号 PAT（scope: api + write_repository），
各仓库角色 Maintainer（webhook 注册需要）。

提供：token 校验、项目识别、webhook 注册/注销、issue 查询/评论/标签、open issues 列表。
"""

from __future__ import annotations

import ipaddress
import logging
import random
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

# GitLab issue 标题长度上限（字符）：服务端硬性限制，超过 255 字符创建
# 接口直接 400（实测 "title is too long (maximum is 255 characters)"）；
# 描述字段上限远大于标题（1MB 量级），正文超长不受此限制（issue #186）。
GITLAB_ISSUE_TITLE_MAX_LEN = 255


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


# 瞬时故障状态码（issue #280）：网关/WAF 短暂不可用（502/503/504）、服务端
# 过载（500）、限流（429）时，GET 读取可安全重试；其余 4xx 为永久性错误。
TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# GET 读取重试次数与指数退避上限（含首次最多 RETRY_MAX_ATTEMPTS 次）。
# 08-17 生产事故（issue #280）：GitLab 短暂不可用返回 502，44 个排队任务
# 启动阶段 get_issue 一次 502 即全部判失败，且失败评论同样发不出。这里对
# 幂等读取做退避重试，非 GET（评论/标签等写操作）绝不重试避免重复提交。
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 4.0


def _retry_delay(attempt: int) -> float:
    """指数退避 + 小抖动（attempt 从 0 开始：1s、2s、4s…封顶）。"""
    return min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY) + random.uniform(0, 0.3)


def is_transient_error(e: GitLabError) -> bool:
    """GitLabError 是否属于可重试的瞬时故障（issue #280）。

    瞬时故障 = 网关/服务端短暂不可用（429/500/502/503/504），或传输层故障
    （连接超时/拒绝/DNS 解析失败，httpx 异常被 _request 包裹为 __cause__）。
    显式状态码优先判定；仅当没有状态码（传输层故障）时才看 __cause__。
    """
    if e.status_code is not None:
        return e.status_code in TRANSIENT_STATUS_CODES
    return isinstance(e.__cause__, httpx.TransportError)


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

    def _http_request_with_retry(self, method: str, path: str, **kwargs) -> httpx.Response:
        """执行一次 HTTP 请求；GET 读取遇瞬时故障退避重试（issue #280）。

        瞬时故障 = 传输层异常（超时/连接拒绝/DNS）或网关/服务端短暂不可用
        （429/500/502/503/504）。GET 幂等可安全重试；非 GET（评论/标签等
        写操作）不重试，避免网络抖动导致重复提交。
        """
        attempts = RETRY_MAX_ATTEMPTS if method == "GET" else 1
        for attempt in range(attempts):
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.HTTPError:
                if attempt >= attempts - 1:
                    raise
                delay = _retry_delay(attempt)
                logger.warning("GitLab %s %s 传输层瞬时故障，%.1fs 后重试（第 %d/%d 次）",
                               method, path, delay, attempt + 1, attempts)
                time.sleep(delay)
                continue
            if resp.status_code in TRANSIENT_STATUS_CODES and attempt < attempts - 1:
                delay = _retry_delay(attempt)
                logger.warning("GitLab %s %s 瞬时故障（HTTP %s），%.1fs 后重试（第 %d/%d 次）",
                               method, path, resp.status_code, delay, attempt + 1, attempts)
                time.sleep(delay)
                continue
            return resp
        raise AssertionError("unreachable")  # 循环内必 return / raise

    def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        try:
            resp = self._http_request_with_retry(method, path, **kwargs)
        except httpx.HTTPError as e:
            # 传输层故障（DNS 解析失败 / 连接拒绝 / 超时等）统一转 GitLabError，
            # 调用方按「GitLab 故障」优雅降级（对账/概览单仓库失败不中断整体，
            # issue #212 E2E 用假 GitLab 地址启动时不再裸抛 httpx 异常）
            raise GitLabError(f"GitLab 请求失败（{path}）: {e}") from e
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
        """分页拉取（per_page=100）；limit 非空时最多取 limit 条即停止翻页。

        分页请求同走 _http_request_with_retry（issue #280）：瞬时故障退避重试，
        避免对账/概览扫描被一次 502 整体中断。
        """
        items: list[dict] = []
        page = 1
        while True:
            try:
                resp = self._http_request_with_retry(
                    "GET", path, params={"page": page, "per_page": 100, **kwargs})
            except httpx.HTTPError as e:
                # 与 _request 一致：传输层故障统一转 GitLabError（issue #212）
                raise GitLabError(f"GitLab 请求失败（{path}）: {e}") from e
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

    def get_personal_access_token_self(self) -> dict:
        """当前 PAT 自身信息（issue #133：owner token 保存前校验用）。

        GET /personal_access_tokens/self 返回当前 token 的 scopes、
        expires_at 等（GitLab >= 15.7；只需 token 本身有效即可调用，
        read_api 级 token 实测可返回）。旧版 GitLab 无此端点时抛 404，
        由调用方降级为仅校验 token 有效性。
        """
        info = self._request("GET", "/personal_access_tokens/self")
        assert isinstance(info, dict)
        return info

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

    def update_project_avatar(self, project_id: int, filename: str, data: bytes,
                              mime: str) -> dict | None:
        """上传项目头像（issue #297）：PUT /projects/{id} 的 avatar 文件参数。

        仓库管理页「同步到 GitLab」按钮调用——把本地生成的 logo 图片设为
        GitLab 项目图标。multipart 上传（httpx files= 参数自动构造
        multipart/form-data），返回项目对象（含 path_with_namespace /
        avatar_url 等字段）。需要 Maintainer 及以上角色（bot token /
        仓库 remote token 均满足）。
        """
        return self._request(
            "PUT", f"/projects/{project_id}",
            files={"avatar": (filename, data, mime)})

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
        color/text_color 为 6 位 hex 且带 # 前缀（实测 "#6699cc"，issue
        #100 起由 API 层归一化为无 # 透传）。标签数量通常远少于一页，
        直接用 _paged 默认分页拉全。
        """
        return self._paged(f"/projects/{project_id}/labels")

    def create_project_label(self, project_id: int, name: str, color: str,
                             description: str | None = None) -> dict:
        """在指定项目创建标签（issue #157：添加仓库时补齐标记库默认标签）。

        GitLab labels API POST /projects/:id/labels 接受 {name, color,
        description}；同名标签已存在时返回 409——调用方应先
        list_project_labels 比对再创建（add_repo 的默认标签补齐即如此）。
        color 需为 #RRGGBB 格式（labels.DEFAULT_LABELS 内置颜色即该格式）。
        """
        body: dict = {"name": name, "color": color}
        if description:
            body["description"] = description
        label = self._request("POST", f"/projects/{project_id}/labels", json=body)
        assert isinstance(label, dict)
        return label

    def list_project_members(self, project_id: int) -> list[dict]:
        """项目成员清单（含继承，members/all，issue #92：添加 issue 弹窗
        的分配人下拉数据源）。

        GitLab members/all 返回项包含 user_id（用户 ID，创建 issue 的
        assignee_ids 需要该值）与 username/name/access_level；顶层 id
        是成员关系 id，不能用于 assignee_ids。
        """
        return self._paged(f"/projects/{project_id}/members/all")

    def create_issue(self, project_id: int, title: str,
                     description: str | None = None,
                     assignee_id: int | None = None,
                     labels: list[str] | None = None) -> dict:
        """在指定项目创建 issue（issue #92：概览页「添加 Issue」按钮）。

        assignee_id 为 GitLab 用户 id（members/all 的 user_id）；labels
        为标签名数组（GitLab API 接受逗号分隔字符串，不存在的标签会自动
        创建——前端限定仓库已有标签多选，此处兜底拼逗号）。

        issue #103：用户未输入描述（description 为 None/空串）时，发送
        API 请求前将标题填充到 description 字段，保证「只输标题」创建
        的 issue 描述恒等于标题；描述非空（用户手写）时保持原样。
        （纯空白字符串的 strip 由 API 层负责，客户端只兜底 falsy 值。）
        """
        body: dict = {"title": title, "description": description or title}
        if assignee_id is not None:
            body["assignee_ids"] = [assignee_id]
        if labels:
            body["labels"] = ",".join(labels)
        issue = self._request(
            "POST", f"/projects/{project_id}/issues", json=body)
        assert isinstance(issue, dict)
        return issue

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
        """添加 issue 评论（issue #125：概览页右边栏「添加评论」）。

        GitLab notes API：POST /projects/{id}/issues/{iid}/notes，
        返回新建的 note 对象（system=false）。
        """
        note = self._request(
            "POST", f"/projects/{project_id}/issues/{iid}/notes",
            json={"body": body})
        assert isinstance(note, dict)
        return note

    def reply_to_note(self, project_id: int, iid: int, note_id: int,
                      body: str) -> dict:
        """回复 issue 某条评论（issue #125：概览页右边栏「回复评论」）。

        GitLab 回复语义：评论（note）挂在 discussion 下，回复 = 向该
        discussion 追加一条 note——POST /projects/{id}/issues/{iid}/
        discussions 带 in_reply_to_discussion_id。notes API 响应不含
        discussion_id（GitLab 19 实测无该字段），故先 GET discussions
        解析目标 note 所在 discussion id；找不到（note 不存在/异常
        数据）抛 404（由调用方映射为 HTTP 404）。

        回复成功返回创建的 note 对象（discussions API 响应为
        discussion 对象，取 notes[0]；异常响应缺 notes 时原样返回，
        由调用方 _trim_note 容错）。
        """
        discussions = self._paged(
            f"/projects/{project_id}/issues/{iid}/discussions")
        discussion_id = None
        for disc in discussions or []:
            if not isinstance(disc, dict):
                continue
            for n in disc.get("notes") or []:
                if isinstance(n, dict) and n.get("id") == note_id:
                    discussion_id = disc.get("id")
                    break
            if discussion_id:
                break
        if discussion_id is None:
            raise GitLabError(f"评论 {note_id} 不存在", 404)
        resp = self._request(
            "POST", f"/projects/{project_id}/issues/{iid}/discussions",
            json={"body": body,
                  "in_reply_to_discussion_id": discussion_id})
        assert isinstance(resp, dict)
        notes = resp.get("notes")
        if isinstance(notes, list) and notes:
            return notes[0]
        return resp

    def list_issue_notes(self, project_id: int, iid: int,
                         limit: int | None = None) -> list[dict]:
        """issue 评论与活动流（issue #97：概览页右边栏展示）。

        GitLab issue 页「活动」的数据源即 notes API——含用户评论
        （system=false）与系统事件（system=true，如分配/标签/状态
        变更）。升序拉取（时间线顺序）；limit 非空时最多取最近
        limit 条（防大 issue 翻页打爆 API）。
        """
        return self._paged(f"/projects/{project_id}/issues/{iid}/notes",
                           limit=limit, sort="asc", order_by="created_at")

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

    def reopen_issue(self, project_id: int, iid: int) -> dict:
        """重新打开 issue（issue #109：autoclose 误关后平台侧恢复用）。

        与 close_issue 对称：GitLab 实例开启 autoclose_referenced_issues
        后，提交信息命中默认关闭模式（fix: #NN 等）推送即被系统自动
        关闭（closed_by 为 project bot），executor 收尾时检测到该特征
        调用本方法恢复 opened，关闭操作仍保留给人工。
        """
        issue = self._request(
            "PUT", f"/projects/{project_id}/issues/{iid}",
            json={"state_event": "reopen"})
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
