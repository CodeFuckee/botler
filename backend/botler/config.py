"""配置管理。

config.yaml 是唯一事实来源，Web UI 是编辑它的外壳。
支持 ${ENV_VAR} 引用环境变量（凭据不落明文）。

已知字段集中定义在 KNOWN_FIELDS，settings API 只允许写入这些字段，
写回时重新 dump 整个 YAML，保证磁盘文件与内存状态一致。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger("botler.config")

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


def _notify_default(notify: dict, key: str, default: bool) -> bool:
    """读取通知开关配置：缺省或非布尔值回退默认（issue #21）。"""
    val = notify.get(key, default)
    return val if isinstance(val, bool) else default


@dataclass
class RepoConfig:
    project_id: int
    name: str
    url: str
    enabled: bool = True
    prompt_template: str | None = None
    local_path: str | None = None
    remote_name: str | None = None


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
    # CI 流水线等待（issue #40）：任务成功收尾前等待任务提交触发的流水线
    # 到终态。detect = 探测窗口（GitLab 收到 push 即创建流水线记录，
    # 窗口内找不到匹配 sha 说明仓库无 CI）；timeout = 等待终态总上限；
    # interval = 轮询间隔
    ci_wait_detect_seconds: int = 120
    ci_wait_interval_seconds: int = 15
    ci_wait_timeout_seconds: int = 1800
    claude_command: str = "claude"
    claude_args: list[str] = field(default_factory=lambda: ["-p", "--output-format", "json"])
    default_template: str = ""
    browse_default_path: str | None = None
    backup_enabled: bool = True
    backup_retention_days: int = 30
    ui_timezone: str = ""  # 页面显示时区（IANA 名，空 = 跟随浏览器本机时区；issue #14）
    # 网页通知（issue #21）：总开关 + 各通知时机开关，前端按此过滤弹系统通知
    notifications_enabled: bool = True
    notify_task_needs_interaction: bool = True
    notify_issue_completed: bool = True
    notify_queue_empty: bool = True
    notify_queue_no_work: bool = True
    # Synology SSO 登录（issue #27）：OIDC 授权码模式接入群晖 SSO Server；
    # 启用后访问 Web UI 需用群晖账号登录，会话有效期 sso_session_days 天
    sso_enabled: bool = False
    sso_well_known_url: str = ""
    sso_client_id: str = ""
    sso_client_secret: str = ""
    sso_scope: str = "openid profile email"
    # 默认 30 天（issue #27 第三轮用户确认；历史实现误为 7）
    sso_session_days: int = 30
    sso_redirect_uri: str = ""  # 回调地址；留空 = 按浏览器访问地址动态生成
    sso_verify_ssl: bool = True  # 群晖自签名证书时设 false
    repos: list[RepoConfig] = field(default_factory=list)
    # 自定义标签（issue #29 标记库）：默认清单见 labels.DEFAULT_LABELS（内置不可删），
    # 用户通过 Web UI 添加的自定义标签存 config.yaml 的 labels.custom
    custom_labels: list[dict] = field(default_factory=list)


# settings API 可写字段（写回 config.yaml 用）
KNOWN_FIELDS = {
    "worker": {"max_concurrent_repos", "task_timeout_seconds", "max_retries",
               "reconcile_interval_seconds", "ci_wait_detect_seconds",
               "ci_wait_interval_seconds", "ci_wait_timeout_seconds"},
    "claude": {"command", "args"},
    "templates": {"default"},
    "browse": {"default_path"},
    "backup": {"enabled", "retention_days"},
    "ui": {"timezone"},
    "notifications": {"enabled", "task_needs_interaction", "issue_completed",
                      "queue_empty", "queue_no_work"},
    "sso": {"enabled", "well_known_url", "client_id", "client_secret", "scope",
            "session_days", "redirect_uri", "verify_ssl"},
}

# 网页通知开关 → Settings 字段映射（issue #21）
_NOTIFY_FIELD_MAP = {
    "enabled": "notifications_enabled",
    "task_needs_interaction": "notify_task_needs_interaction",
    "issue_completed": "notify_issue_completed",
    "queue_empty": "notify_queue_empty",
    "queue_no_work": "notify_queue_no_work",
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
4. 推送成功后，在 issue 上留结果评论（平台会自动打 bot-done 标签）；
   不要关闭该 issue——关闭动作留给用户确认后手动执行（模版库规范）
5. 若确实无法解决，不要推送代码，如实汇报失败原因和已做的尝试

注意：只修改与 issue 相关的代码，不要顺手重构无关部分。
"""


class ConfigManager:
    """加载 / 保存 config.yaml，提供 Settings 视图。"""

    def __init__(self, path: str = DEFAULT_CONFIG_PATH):
        self.path = path
        self._data: dict[str, Any] = {}
        self.settings: Settings | None = None
        self._loaded_mtime: float = 0.0

    def load(self) -> Settings:
        if not os.path.exists(self.path):
            raise FileNotFoundError(
                f"配置文件不存在: {self.path}（可复制 config.example.yaml）"
            )
        with open(self.path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self._data = _expand_env(raw)
        self.settings = self._to_settings(self._data)
        try:
            self._loaded_mtime = os.path.getmtime(self.path)
        except OSError:
            self._loaded_mtime = 0.0
        return self.settings

    def _reload_from_disk(self) -> bool:
        """重新读取磁盘 config.yaml（保留用户手动编辑的内容，issue #25）。

        update_* 写盘前调用：以磁盘最新内容为基底，避免 save() 用内存旧值
        覆盖用户直接编辑 config.yaml 的修改。失败（文件缺失/损坏）时保留
        内存状态降级，不中断流程。
        """
        try:
            self.load()
            return True
        except (OSError, yaml.YAMLError, ValueError, KeyError) as e:
            logger.warning("config.yaml 重载失败，沿用当前配置: %s", e)
            return False

    def _to_settings(self, data: dict[str, Any]) -> Settings:
        gitlab = data.get("gitlab", {})
        worker = data.get("worker", {})
        claude = data.get("claude", {})
        tpl = data.get("templates", {})
        browse = data.get("browse", {})
        backup = data.get("backup", {})
        ui = data.get("ui", {})
        notify = data.get("notifications", {})
        sso = data.get("sso", {})
        repos_raw = data.get("repos", []) or []
        labels_raw = (data.get("labels", {}) or {}).get("custom", []) or []

        repos = []
        for r in repos_raw:
            repos.append(RepoConfig(
                project_id=int(r["project_id"]),
                name=r["name"],
                url=r["url"],
                enabled=bool(r.get("enabled", True)),
                prompt_template=r.get("prompt_template"),
                local_path=r.get("local_path"),
                remote_name=r.get("remote_name"),
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
            ci_wait_detect_seconds=int(worker.get("ci_wait_detect_seconds", 120)),
            ci_wait_interval_seconds=int(worker.get("ci_wait_interval_seconds", 15)),
            ci_wait_timeout_seconds=int(worker.get("ci_wait_timeout_seconds", 1800)),
            claude_command=claude.get("command", "claude"),
            claude_args=claude.get("args", ["-p", "--output-format", "json"]),
            default_template=tpl.get("default", DEFAULT_TEMPLATE),
            browse_default_path=browse.get("default_path") or None,
            backup_enabled=bool(backup.get("enabled", True)),
            backup_retention_days=int(backup.get("retention_days", 30)),
            ui_timezone=(ui.get("timezone") or "").strip(),
            notifications_enabled=_notify_default(notify, "enabled", True),
            notify_task_needs_interaction=_notify_default(notify, "task_needs_interaction", True),
            notify_issue_completed=_notify_default(notify, "issue_completed", True),
            notify_queue_empty=_notify_default(notify, "queue_empty", True),
            notify_queue_no_work=_notify_default(notify, "queue_no_work", True),
            sso_enabled=bool(sso.get("enabled", False)),
            sso_well_known_url=sso.get("well_known_url", ""),
            sso_client_id=sso.get("client_id", ""),
            sso_client_secret=sso.get("client_secret", ""),
            sso_scope=(sso.get("scope") or "openid profile email").strip(),
            sso_session_days=int(sso.get("session_days", 30)),
            sso_redirect_uri=sso.get("redirect_uri", ""),
            sso_verify_ssl=bool(sso.get("verify_ssl", True)),
            repos=repos,
            custom_labels=[
                {"name": str(l.get("name", "")).strip(),
                 "color": str(l.get("color", "")).strip(),
                 "description": str(l.get("description") or "").strip()}
                for l in labels_raw
                if l.get("name")
            ],
        )

    def save(self) -> None:
        """将内存数据写回 config.yaml。"""
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)
        try:
            self._loaded_mtime = os.path.getmtime(self.path)
        except OSError:
            pass

    def get(self) -> Settings:
        # 磁盘 mtime 变化（用户直接编辑 config.yaml）→ 自动重载，无需重启
        # 进程、无需再走一遍 Web UI（issue #25「修改了全局模版，但是没有生效」）
        if self.settings is not None:
            try:
                if os.path.getmtime(self.path) > self._loaded_mtime:
                    self._reload_from_disk()
            except OSError:
                pass  # 文件不可读/被临时移走：沿用当前配置
        if self.settings is None:
            self.load()
        return self.settings

    # ---- settings API 支持 ----

    def update_worker(self, patch: dict[str, Any]) -> Settings:
        """更新 worker 配置并写回（写盘前重读磁盘，保留手动编辑，issue #25）。"""
        self._reload_from_disk()
        worker = self._data.setdefault("worker", {})
        for key in KNOWN_FIELDS["worker"]:
            if key in patch:
                worker[key] = patch[key]
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_claude(self, patch: dict[str, Any]) -> Settings:
        self._reload_from_disk()
        claude = self._data.setdefault("claude", {})
        for key in KNOWN_FIELDS["claude"]:
            if key in patch:
                claude[key] = patch[key]
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_default_template(self, text: str) -> Settings:
        self._reload_from_disk()
        self._data.setdefault("templates", {})["default"] = text
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_browse(self, patch: dict[str, Any]) -> Settings:
        """更新 browse 配置并写回（目录选择对话框初始定位目录）。"""
        self._reload_from_disk()
        browse = self._data.setdefault("browse", {})
        for key in KNOWN_FIELDS["browse"]:
            if key in patch:
                browse[key] = patch[key]
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_backup(self, patch: dict[str, Any]) -> Settings:
        """更新 backup 配置并写回（定时备份开关 / 保留天数）。"""
        self._reload_from_disk()
        backup = self._data.setdefault("backup", {})
        for key in KNOWN_FIELDS["backup"]:
            if key in patch:
                backup[key] = patch[key]
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_ui(self, patch: dict[str, Any]) -> Settings:
        """更新 ui 配置并写回（页面显示时区，空 = 跟随浏览器本机时区；issue #14）。"""
        self._reload_from_disk()
        ui = self._data.setdefault("ui", {})
        for key in KNOWN_FIELDS["ui"]:
            if key in patch:
                ui[key] = patch[key]
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_notifications(self, patch: dict[str, Any]) -> Settings:
        """更新 notifications 配置并写回（网页通知开关；issue #21）。"""
        self._reload_from_disk()
        notify = self._data.setdefault("notifications", {})
        for key in KNOWN_FIELDS["notifications"]:
            if key in patch:
                notify[key] = patch[key]
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_sso(self, patch: dict[str, Any]) -> Settings:
        """更新 sso 配置并写回（Synology SSO 登录；issue #27）。

        前端回传的 client_secret 掩码值（含 *）视为"未修改"，不覆盖真实凭据。
        """
        self._reload_from_disk()
        sso = self._data.setdefault("sso", {})
        for key in KNOWN_FIELDS["sso"]:
            if key not in patch:
                continue
            val = patch[key]
            if key == "client_secret" and (val is None or "*" in str(val)):
                continue  # 掩码占位符：保持现有凭据
            sso[key] = val
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_repos(self, repos: list[dict[str, Any]]) -> None:
        """整体替换 repos 列表（增删改仓库后的落盘）。"""
        self._reload_from_disk()
        self._data["repos"] = repos
        self.save()
        self.settings = self._to_settings(self._data)

    def update_custom_labels(self, labels: list[dict[str, Any]]) -> Settings:
        """整体替换自定义标签列表（标记库页增删后落盘，issue #29）。"""
        self._reload_from_disk()
        self._data.setdefault("labels", {})["custom"] = labels
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

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
        if repo.local_path:
            d["local_path"] = repo.local_path
        if repo.remote_name:
            d["remote_name"] = repo.remote_name
        return d
