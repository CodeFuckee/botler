"""配置管理。

config.yaml 是唯一事实来源，Web UI 是编辑它的外壳。
支持 ${ENV_VAR} 引用环境变量（凭据不落明文）。

已知字段集中定义在 KNOWN_FIELDS，settings API 只允许写入这些字段，
写回时重新 dump 整个 YAML，保证磁盘文件与内存状态一致。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 自动加载 backend/.env（凭据引用 ${ENV} 时使用）
_BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(_BACKEND_DIR / ".env")

# 引用环境变量的占位符，如 ${GITLAB_BOT_TOKEN}
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_CONFIG_PATH = os.environ.get("BOTLER_CONFIG", "config.yaml")


def _expand_env(value: Any) -> Any:
    """递归展开配置中的 ${ENV_VAR} 引用。"""
    if isinstance(value, str):
        def _sub(m: re.Match) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ValueError(f"环境变量 {name} 未设置（config.yaml 中引用了它）")
            return os.environ[name]
        return _ENV_REF.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


@dataclass
class RepoConfig:
    project_id: int
    name: str
    url: str
    enabled: bool = True
    prompt_template: str | None = None


@dataclass
class Settings:
    gitlab_url: str
    gitlab_token: str
    webhook_secret: str
    bot_id: int | None = None
    bot_username: str | None = None
    verify_ssl: bool = True
    max_concurrent_repos: int = 3
    task_timeout_seconds: int = 1800
    max_retries: int = 2
    reconcile_interval_seconds: int = 300
    claude_command: str = "claude"
    claude_args: list[str] = field(default_factory=lambda: ["-p", "--output-format", "json"])
    default_template: str = ""
    repos: list[RepoConfig] = field(default_factory=list)


# settings API 可写字段（写回 config.yaml 用）
KNOWN_FIELDS = {
    "worker": {"max_concurrent_repos", "task_timeout_seconds", "max_retries", "reconcile_interval_seconds"},
    "claude": {"command", "args"},
    "templates": {"default"},
}

DEFAULT_TEMPLATE = """你是 {repo_name} 仓库的 AI 维护者。请处理以下指派给你的 issue：

标题: {issue_title}
正文: {issue_body}
链接: {issue_url}

工作要求：
1. 在本地工作区（已检出 main 最新代码）分析问题，定位根因
2. 编写修复代码并自测通过（运行相关测试/构建验证）
3. 自测通过后，直接推送到 main 分支：
   git add -A && git commit -m "fix: 解决 issue #{issue_iid}" && git push origin main
4. 推送成功后，调用 GitLab API 关闭该 issue：
   curl -s -X PUT "{gitlab_url}/api/v4/projects/{project_id}/issues/{issue_iid}" \
     -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
     -d "state_event=close"
5. 若确实无法解决，不要推送代码，如实汇报失败原因和已做的尝试

注意：只修改与 issue 相关的代码，不要顺手重构无关部分。
"""


class ConfigManager:
    """加载 / 保存 config.yaml，提供 Settings 视图。"""

    def __init__(self, path: str = DEFAULT_CONFIG_PATH):
        self.path = path
        self._data: dict[str, Any] = {}
        self.settings: Settings | None = None

    def load(self) -> Settings:
        if not os.path.exists(self.path):
            raise FileNotFoundError(
                f"配置文件不存在: {self.path}（可复制 config.example.yaml）"
            )
        with open(self.path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self._data = _expand_env(raw)
        self.settings = self._to_settings(self._data)
        return self.settings

    def _to_settings(self, data: dict[str, Any]) -> Settings:
        gitlab = data.get("gitlab", {})
        worker = data.get("worker", {})
        claude = data.get("claude", {})
        tpl = data.get("templates", {})
        repos_raw = data.get("repos", []) or []

        repos = []
        for r in repos_raw:
            repos.append(RepoConfig(
                project_id=int(r["project_id"]),
                name=r["name"],
                url=r["url"],
                enabled=bool(r.get("enabled", True)),
                prompt_template=r.get("prompt_template"),
            ))

        bot_id = gitlab.get("bot_id")
        return Settings(
            gitlab_url=gitlab["url"].rstrip("/"),
            gitlab_token=gitlab["bot_token"],
            webhook_secret=gitlab.get("webhook_secret", ""),
            bot_id=int(bot_id) if bot_id not in (None, "") else None,
            bot_username=gitlab.get("bot_username"),
            verify_ssl=bool(gitlab.get("verify_ssl", True)),
            max_concurrent_repos=int(worker.get("max_concurrent_repos", 3)),
            task_timeout_seconds=int(worker.get("task_timeout_seconds", 1800)),
            max_retries=int(worker.get("max_retries", 2)),
            reconcile_interval_seconds=int(worker.get("reconcile_interval_seconds", 300)),
            claude_command=claude.get("command", "claude"),
            claude_args=claude.get("args", ["-p", "--output-format", "json"]),
            default_template=tpl.get("default", DEFAULT_TEMPLATE),
            repos=repos,
        )

    def save(self) -> None:
        """将内存数据写回 config.yaml。"""
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)

    def get(self) -> Settings:
        if self.settings is None:
            self.load()
        return self.settings

    # ---- settings API 支持 ----

    def update_worker(self, patch: dict[str, Any]) -> Settings:
        """更新 worker 配置并写回。"""
        worker = self._data.setdefault("worker", {})
        for key in KNOWN_FIELDS["worker"]:
            if key in patch:
                worker[key] = patch[key]
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_claude(self, patch: dict[str, Any]) -> Settings:
        claude = self._data.setdefault("claude", {})
        for key in KNOWN_FIELDS["claude"]:
            if key in patch:
                claude[key] = patch[key]
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_default_template(self, text: str) -> Settings:
        self._data.setdefault("templates", {})["default"] = text
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_repos(self, repos: list[dict[str, Any]]) -> None:
        """整体替换 repos 列表（增删改仓库后的落盘）。"""
        self._data["repos"] = repos
        self.save()
        self.settings = self._to_settings(self._data)

    @staticmethod
    def repo_to_config_dict(repo: RepoConfig) -> dict[str, Any]:
        d: dict[str, Any] = {
            "project_id": repo.project_id,
            "name": repo.name,
            "url": repo.url,
            "enabled": repo.enabled,
        }
        if repo.prompt_template:
            d["prompt_template"] = repo.prompt_template
        return d
