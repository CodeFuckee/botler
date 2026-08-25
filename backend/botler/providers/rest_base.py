"""GitHub / Gitea 共用的 REST API 适配基类（issue #484，内部实现不对外导出）。

两类平台 API 结构高度相似（issue / pull request / webhook 端点一致），
把「HTTP 请求 → 分页 → 异常转换」沉淀为基类，子类只提供平台差异：
- ``api_prefix``：GitHub 为空（GHES 可配 /api/v3），Gitea 为 /api/v1；
- ``auth_header``：GitHub 用 Bearer，Gitea 用 token；
- ``resolve_project`` / 流水线 / 标签等平台细节由子类覆盖。
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx

from .base import Provider, ProviderError
from .domain import Project

logger = logging.getLogger(__name__)

# 瞬时故障可安全重试（GET 幂等读取）：网关/限流/服务端过载
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


class RestProvider(Provider):
    """基于 httpx 的 REST 适配基类。"""

    api_prefix = ""

    def __init__(self, url: str, token: str | None = None,
                 verify_ssl: bool = True):
        super().__init__(url, token, verify_ssl)
        headers = self._build_headers(token)
        self._http = httpx.Client(
            base_url=f"{self.url}{self.api_prefix}",
            headers=headers,
            timeout=30,
            verify=verify_ssl,
        )

    # ---- 平台差异点 ----

    def _build_headers(self, token: str | None) -> dict[str, str]:
        """构造请求头（子类覆盖平台鉴权方式）。"""
        raise NotImplementedError

    # ---- 基础请求 ----

    def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        """统一请求入口：瞬时故障（429/5xx）GET 重试一次，异常转 ProviderError。

        path 以 / 开头（如 /repos/owner/repo/issues），与 base_url 拼接。
        """
        attempts = 2 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"平台 API 请求失败（{path}）: {exc}", platform=self.platform) from exc
            if resp.status_code in _TRANSIENT_STATUS and attempt < attempts - 1:
                time.sleep(0.3 * (attempt + 1))
                continue
            break
        return self._parse_response(resp)

    def _parse_response(self, resp: httpx.Response) -> dict | list | None:
        if resp.status_code == 204 or not resp.content:
            return None
        if resp.status_code >= 400:
            raise ProviderError(
                f"平台 API 错误 {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
                platform=self.platform,
            )
        try:
            return resp.json()
        except ValueError:
            return None

    def _paged(self, path: str, limit: int | None = None, **kwargs) -> list[dict]:
        """分页拉取（per_page=100）；limit 非空时最多取 limit 条。

        多数 REST 平台（GitHub/Gitea）用 Link 头翻页；无 Link 头时按
        per_page 数量推断是否还有下一页（与 GitLabClient._paged 同策略）。
        """
        items: list[dict] = []
        page = 1
        while True:
            resp = self._http.request(
                "GET", path,
                params={"page": page, "per_page": 100, **kwargs})
            if resp.status_code in _TRANSIENT_STATUS:
                time.sleep(0.3)
                resp = self._http.request(
                    "GET", path,
                    params={"page": page, "per_page": 100, **kwargs})
            if resp.status_code >= 400:
                raise ProviderError(
                    f"平台 API 错误 {resp.status_code}: {resp.text[:300]}",
                    status_code=resp.status_code,
                    platform=self.platform,
                )
            batch = resp.json()
            if not isinstance(batch, list):
                break
            items.extend(batch)
            if limit is not None and len(items) >= limit:
                break
            if len(batch) < 100:
                break
            page += 1
        return items[:limit] if limit is not None else items

    # ---- 项目引用解析（owner/repo）----

    @staticmethod
    def _split_owner_repo(ref: str) -> tuple[str, str]:
        """把 owner/repo 或仓库 URL 解析为 (owner, repo)。"""
        value = ref.strip()
        if "://" in value:
            parsed = urlparse(value)
            path = parsed.path.strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            value = path
        elif ":" in value and "/" in value and "://" not in value:
            # scp-like（git@host:owner/repo.git）
            value = value.rpartition(":")[2].strip("/")
            if value.endswith(".git"):
                value = value[:-4]
        parts = value.strip("/").split("/")
        if len(parts) < 2:
            raise ProviderError(
                f"无法解析仓库引用: {ref}（需要 owner/repo 或仓库 URL）",
                400, None)
        owner = parts[-2]
        repo = parts[-1].removesuffix(".git")
        if not owner or not repo:
            raise ProviderError(
                f"无法解析仓库引用: {ref}（需要 owner/repo 或仓库 URL）",
                400, None)
        return owner, repo

    def _repo_path(self, project: Project | str) -> str:
        """通用 project 参数 → /repos/{owner}/{repo} 路径。"""
        ref = project.id if isinstance(project, Project) else str(project)
        owner, repo = self._split_owner_repo(ref)
        return f"/repos/{owner}/{repo}"

    # ---- 认证与项目 ----

    def test_connection(self) -> bool:
        self._request("GET", "/user")
        return True

    def resolve_project(self, ref: str) -> Project:
        owner, repo = self._split_owner_repo(ref)
        payload = self._request("GET", f"/repos/{owner}/{repo}")
        assert isinstance(payload, dict)
        return Project.from_github_like(payload, f"{owner}/{repo}")
