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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .pause_window import normalize_window, parse_window

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


# issue 标签优先级默认顺序（issue #76）：设置页可自定义，写回 worker.issue_priority
DEFAULT_ISSUE_PRIORITY = ["bug", "test", "feature"]


def _ui_default(ui: dict, key: str, default: bool) -> bool:
    """读取界面显示开关配置：缺省或非布尔值回退默认（issue #142）。"""
    val = ui.get(key, default)
    return val if isinstance(val, bool) else default


# 界面显示主题三态（issue #217）：system（跟随系统）/ light（浅色）/ dark（深色）
THEME_MODES = ("system", "light", "dark")


def _ui_theme(ui: dict) -> str:
    """读取界面显示主题（issue #217）：缺省或非法值回退 system（跟随系统）。

    防御手动编辑 config.yaml 写坏（与 _ui_default 同思路）——只有
    system / light / dark 三个合法取值，其余一律按跟随系统处理。"""
    val = ui.get("theme")
    return val if isinstance(val, str) and val in THEME_MODES else "system"


def _pause_windows(worker: dict) -> list[str]:
    """读取 worker.pause_windows（issue #169）：必须是字符串列表；剔除
    格式非法项（防御手动编辑 config.yaml 写坏），全部非法 = 不启用。"""
    val = worker.get("pause_windows")
    if not isinstance(val, list):
        return []
    cleaned = [normalize_window(str(w).strip()) for w in val if isinstance(w, str)]
    return [w for w in cleaned if parse_window(w) is not None]


def _pause_weekdays(worker: dict) -> list[int]:
    """读取 worker.pause_weekdays（issue #169）：0-6（周一=0…周日=6）整数
    列表，去重保序；非法值剔除（防御手动编辑 config.yaml 写坏）。"""
    val = worker.get("pause_weekdays")
    if not isinstance(val, list):
        return []
    cleaned: list[int] = []
    for x in val:
        if (isinstance(x, int) and not isinstance(x, bool)
                and 0 <= x <= 6 and x not in cleaned):
            cleaned.append(x)
    return cleaned


def _issue_priority_labels(worker: dict) -> list[str]:
    """读取 worker.issue_priority（issue #76）：必须是字符串列表且非空，
    否则回退默认顺序（防御手动编辑 config.yaml 写坏的情况）。"""
    val = worker.get("issue_priority")
    if (isinstance(val, list) and val
            and all(isinstance(x, str) and x.strip() for x in val)):
        return [x.strip() for x in val]
    return list(DEFAULT_ISSUE_PRIORITY)


@dataclass
class RepoConfig:
    project_id: int
    name: str
    url: str
    enabled: bool = True
    prompt_template: str | None = None
    local_path: str | None = None
    remote_name: str | None = None
    remote_username: str | None = None  # 仓库用户（issue #153）：remote url userinfo 用户名
    priority: int = 100  # 调度优先级（issue #51）：1~999，数字越小越优先


@dataclass
class Settings:
    gitlab_url: str
    gitlab_token: str
    webhook_secret: str
    bot_id: int | None = None
    bot_username: str | None = None
    verify_ssl: bool = True
    # Owner GitLab Token（issue #87）：专用于编辑 issue（评论/标签），
    # 严禁用于 git 推送（推送凭据走 _askpass_script 的 bot token）与
    # 流水线操作。空串 = 未配置，编辑 issue 沿用原链路。
    gitlab_owner_token: str = ""
    max_concurrent_repos: int = 3
    task_timeout_seconds: int = 1800
    max_retries: int = 2
    reconcile_interval_seconds: int = 300
    # issue 标签处理优先级（issue #76）：同仓库队列内按此顺序选任务派发，
    # 越靠前的标签越先处理；未列出的标签排在最后；同权重按 issue 更新时间
    # 升序。设置页「任务调度」卡片可修改（默认 bug > test > feature）。
    issue_priority_labels: list[str] = field(
        default_factory=lambda: ["bug", "test", "feature"])
    # 定时暂停窗口（issue #169）：窗口内停止开始新任务，已开始任务继续
    # 执行，未开始任务等到窗口结束后开始。pause_windows 为 "HH:MM-HH:MM"
    # 窗口串列表（支持跨天，如 22:00-02:00），空 = 不启用；pause_weekdays
    # 为生效星期（0=周一…6=周日），空 = 每天生效；pause_timezone 为判断
    # 所用时区（IANA 名），空 = 服务器本地时区。设置页「任务调度」卡片可编辑。
    pause_windows: list[str] = field(default_factory=list)
    pause_weekdays: list[int] = field(default_factory=list)
    pause_timezone: str = ""
    # CI 流水线等待（issue #40）：任务成功收尾前等待任务提交触发的流水线
    # 到终态。detect = 探测窗口（GitLab 收到 push 即创建流水线记录，
    # 窗口内找不到匹配 sha 说明仓库无 CI）；timeout = 等待终态总上限；
    # interval = 轮询间隔
    ci_wait_detect_seconds: int = 120
    ci_wait_interval_seconds: int = 15
    ci_wait_timeout_seconds: int = 1800
    claude_command: str = "claude"
    # stream-json 逐行实时输出（实时输出功能：任务页面逐事件查看执行过程）；
    # claude 2.1.x 要求 stream-json 配 --verbose，executor 会自动补齐
    claude_args: list[str] = field(
        default_factory=lambda: ["-p", "--output-format", "stream-json", "--verbose"])
    # 任务执行引擎（issue #47/#84/#171，插件化 issue #140）：claude =
    # Claude Code CLI（默认，现网行为不变）；hermes = hermes-agent SDK
    # （issue #171 起改为进程内集成，经 hermes_sdk_runner.py 调用
    # run_agent.AIAgent）；dsh = deepseek-harness SDK（经 dsh_runner.py
    # 进程内调用）。引擎名对应执行引擎插件（botler.plugins.executors），
    # 未注册的引擎名回退 claude（executor._engine 校验）。
    engine: str = "claude"
    # 外部插件加载（issue #140）：Python 模块路径列表，应用启动时逐个加载
    # 并注册进插件体系（新增执行引擎 / 大模型供应商 / 消息发送通道）。
    # 模块内调用 botler.plugins.register_plugin 完成登记；加载失败仅记
    # 日志告警，不阻塞应用启动。
    plugin_paths: list[str] = field(default_factory=list)
    # dsh 引擎（issue #84，worker.engine: dsh 时生效）：deepseek-harness
    # Python SDK 运行参数。DeepSeek API Key 走部署机环境变量 DEEPSEEK_API_KEY
    # （或 dsh.base_url/dsh.api_key 显式配置），与 hermes 同模式。
    dsh_provider: str = "deepseek-official"
    dsh_model: str = "deepseek-v4-flash"
    dsh_max_tokens: int | None = None
    # 推理等级（issue #123）：deepseek-harness SDK 支持 reasoningEffort
    # （off / high / max）。空串 = 不设置（SDK 默认 high）；dsh 引擎执行时
    # 通过派生 Cordis 组合注入 llm-deepseek 配置（见 dsh_runner.py）。
    dsh_reasoning_effort: str = ""
    dsh_session_root: str = ""
    dsh_cordis: str = ""
    dsh_runtime_bin: str = ""
    dsh_base_url: str = ""
    dsh_api_key: str = ""
    default_template: str = ""
    # 中断恢复模版（issue #116）：与全局默认模版同机制，用户可在
    # 设置 API / Web UI 模版页编辑；为空或未配置时归一为内置默认
    # DEFAULT_RESUME_PROMPT（中断恢复必须有引导语，不允许空模版）
    resume_template: str = ""
    browse_default_path: str | None = None
    backup_enabled: bool = True
    backup_retention_days: int = 30
    ui_timezone: str = ""  # 页面显示时区（IANA 名，空 = 跟随浏览器本机时区；issue #14）
    # 灵感 / CI/CD 页面是否显示未启用项目（issue #142）：默认 true = 显示
    # （未启用仓库带「未启用」徽章，保持现状）；false = 两个板块只展示
    # 已启用仓库（后端接口直接过滤，未启用仓库不再发起 GitLab 查询）。
    ui_show_disabled_repos: bool = True
    # 界面显示主题（issue #217）：system / light / dark 三态——system 跟随
    # 系统 prefers-color-scheme；light / dark 手动强制。与前端 localStorage
    # 本地偏好（botler.theme）双向同步：设置页保存后写回 config.yaml（跨
    # 设备权威配置），前端首屏按本地偏好渲染防闪烁。
    ui_theme: str = "system"
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
    # AI API 供应商（issue #46）：设置页增删改查的供应商列表，为后续 AI 功能
    # 消费做准备。每项 {name, provider, base_url, api_key, model, enabled}；
    # api_key 落盘 config.yaml（与 sso.client_secret 同模式），API 只返回掩码。
    ai_providers: list[dict] = field(default_factory=list)
    # 生图模型（issue #135）：设置页「生图模型」卡片增删改查的图像模型列表，
    # 为后续 AI 功能消费做准备。每项 {name, provider, base_url, api_key,
    # model, enabled}；api_key 落盘 config.yaml（与 ai_providers 同模式），
    # API 只返回掩码。内置预设见前端 providers.jsx 的 IMAGE_MODEL_PRESETS。
    image_models: list[dict] = field(default_factory=list)
    # 识图模型（issue #152）：设置页「识图模型」卡片增删改查的视觉理解
    # 模型列表，配置后可通过测试按钮上传图片调用模型描述图片。每项
    # {name, provider, base_url, api_key, model, enabled}；api_key 落盘
    # config.yaml（与 image_models 同模式），API 只返回掩码。内置预设见
    # 前端 providers.jsx 的 VISION_MODEL_PRESETS。
    vision_models: list[dict] = field(default_factory=list)
    # MinIO 对象存储（issue #163）：识图模型调用时用户上传的图片先计算
    # SHA-256 哈希、以哈希值为对象名上传 MinIO，识图请求传 http URL
    # （替代 base64 内联）。配置见 config.example.yaml 的 minio 段；
    # access_key / secret_key 留空时回退环境变量 MINIO_ROOT_USER /
    # MINIO_ROOT_PASSWORD（与部署写入 data/backend/.env 的凭据同源）。
    minio_enabled: bool = False
    minio_endpoint: str = "127.0.0.1:9000"
    minio_secure: bool = False
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "public"
    minio_public_base_url: str = ""
    minio_verify_ssl: bool = True

    # Webhook 消息推送（issue #136）：任务完成（成功收尾）时调用 webhook
    # 进行消息推送。设置页可配置：
    #   url             webhook 地址（POST 目标）
    #   content_type    Content-Type 请求头（默认 application/json）
    #   authorization   Authorization 请求头（可选，如 Bearer 令牌）
    #   body_template   POST 结构体模板，支持全局模板占位符（与提示词模版
    #                   同机制，见 templates.PLACEHOLDERS），请求时自动填充；
    #                   留空 = 使用内置默认模板 DEFAULT_WEBHOOK_TEMPLATE
    webhook_enabled: bool = False
    webhook_url: str = ""
    webhook_content_type: str = "application/json"
    webhook_authorization: str = ""
    webhook_body_template: str = ""
    # 任务 token 用量与费用（issue #235）：usage.currency 为估算费用货币
    # （默认 USD）；usage.pricing 为模型单价表——每项 {model（支持子串
    # 匹配，如 deepseek-v4-flash / claude）, input_per_million（每百万
    # 输入 token 单价）, output_per_million（每百万输出 token 单价）}，
    # 空表 = 未配置单价，任务详情只展示 token 数不估算费用。
    usage_currency: str = "USD"
    usage_pricing: list[dict] = field(default_factory=list)



# settings API 可写字段（写回 config.yaml 用）
KNOWN_FIELDS = {
    "gitlab": {"owner_token"},
    "worker": {"max_concurrent_repos", "task_timeout_seconds", "max_retries",
               "reconcile_interval_seconds", "ci_wait_detect_seconds",
               "ci_wait_interval_seconds", "ci_wait_timeout_seconds",
               "engine", "plugin_paths", "issue_priority",
               "pause_windows", "pause_weekdays", "pause_timezone"},
    "claude": {"command", "args"},
    "dsh": {"provider", "model", "max_tokens", "reasoning_effort",
            "session_root", "cordis", "runtime_bin", "base_url", "api_key"},
    "templates": {"default", "resume"},
    "browse": {"default_path"},
    "backup": {"enabled", "retention_days"},
    "ui": {"timezone", "show_disabled_repos", "theme"},
    "notifications": {"enabled", "task_needs_interaction", "issue_completed",
                      "queue_empty", "queue_no_work"},
    "webhook": {"enabled", "url", "content_type", "authorization",
               "body_template"},
    "sso": {"enabled", "well_known_url", "client_id", "client_secret", "scope",
            "session_days", "redirect_uri", "verify_ssl"},
    # MinIO 对象存储（issue #163）：识图图片上传配置。凭据掩码/空串 =
    # 保持现有值（update_minio 处理），与 webhook.authorization 同模式。
    "minio": {"enabled", "endpoint", "secure", "access_key", "secret_key",
              "bucket", "public_base_url", "verify_ssl"},
    # 任务 token 用量（issue #235）：currency 为费用货币；pricing 为模型
    # 单价表（每项 model / input_per_million / output_per_million）。
    "usage": {"currency", "pricing"},
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
1. 本地工作区已由平台自动切到默认主分支并 git pull 拉取最新代码，
   直接基于最新代码分析问题、定位根因（无需自行切换分支或 git pull）
2. 编写修复代码并自测通过（运行相关测试/构建验证）
3. 自测通过后，直接推送到当前分支（平台已自动切到仓库默认主分支，
   可能是 main / master 等，不要假设分支名）：
   git add -A && git commit -m "fix: 解决（issue #{issue_iid}）" && git push origin HEAD
4. 推送成功后，在 issue 上留结果评论（平台会自动打 bot-done 标签）；
   不要关闭该 issue——关闭动作留给用户确认后手动执行（模版库规范）
5. 提交信息严禁 `fix: #N` / `fixes #N` / `closes #N` / `resolves #N` 等
   「关闭关键词 + #编号」写法：GitLab 实例开启了 autoclose 机制，命中
   默认关闭模式会在推送后自动关闭 issue（用户侧表现为「agent 自己
   close issue」）；issue 引用一律写全角括号 `（issue #N）` 形式。
6. 若确实无法解决，不要推送代码，如实汇报失败原因和已做的尝试

注意：只修改与 issue 相关的代码，不要顺手重构无关部分。
"""

# 恢复执行引导语（issue #8）：中断恢复时不用完整模版重发 issue 描述，
# 而是让 Claude 检查工作区现状后从断点继续（避免重复分析与重复评论）。
# issue #116 起迁入 config 作为内置默认（templates.resume 可编辑覆盖），
# 并同步修正 issue #109 政策：不再指示「用 GitLab API 关闭 issue」，
# 关闭动作留给用户确认后手动执行。
# issue #281 §4.4 升级为「确定性交接单」渲染：新增 {progress_summary}
# 占位符，由平台从 task_progress 账本渲染「已完成步骤 + 证据 / 下一步」
# （确定性状态交接，非模型自查反推）；账本为空时如实降级，不再声称
# 「对话与工作区改动已保留」。
DEFAULT_RESUME_PROMPT = """【继续处理（中断恢复）】你正在处理 {repo_name} 仓库的 issue #{issue_iid}「{issue_title}」：{issue_url}

上次处理因平台重新部署而中断。{progress_summary}

请基于以上进度从断点继续：完成剩余的修复/实现 → 自测 → 推送 → 在 issue 上留结果评论。
不要重复已经完成的工作（除非确认上次未开始实质工作）。
不要关闭该 issue——关闭动作留给用户确认后手动执行（模版库规范）。"""

# Webhook POST 结构体默认模板（issue #136）：与全局提示词模版同占位符机制
# （{repo_name}/{issue_title}/{issue_body}/{issue_url}/{gitlab_url}/
# {project_id}/{issue_iid}/{project_path}/{project_path_encoded}/
# {gitlab_host}），任务完成时自动渲染填充；设置页留空 = 使用此默认值。
DEFAULT_WEBHOOK_TEMPLATE = """{
  "event": "task_succeeded",
  "repo": "{repo_name}",
  "issue": {
    "iid": "{issue_iid}",
    "title": "{issue_title}",
    "url": "{issue_url}",
    "body": "{issue_body}"
  },
  "gitlab": {
    "url": "{gitlab_url}",
    "host": "{gitlab_host}",
    "project_id": "{project_id}",
    "project_path": "{project_path}",
    "project_path_encoded": "{project_path_encoded}"
  }
}"""



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
        # 先完整解析并校验（_to_settings），全部成功后才替换内存 _data：
        # 磁盘文件损坏/半截（如并发写盘）时抛异常，内存保持旧值不污染，
        # 避免后续 update_* 在残缺数据上 _to_settings 抛 KeyError（issue #181
        # CI 诊断：settings 保存偶发 500，gitlab['url'] KeyError）。
        data = _expand_env(raw)
        settings = self._to_settings(data)
        self._data = data
        self.settings = settings
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
        dsh = data.get("dsh", {})
        tpl = data.get("templates", {})
        browse = data.get("browse", {})
        backup = data.get("backup", {})
        ui = data.get("ui", {})
        notify = data.get("notifications", {})
        webhook = data.get("webhook", {}) or {}
        sso = data.get("sso", {})
        repos_raw = data.get("repos", []) or []
        labels_raw = (data.get("labels", {}) or {}).get("custom", []) or []
        providers_raw = data.get("ai_providers", []) or []
        image_models_raw = data.get("image_models", []) or []
        vision_models_raw = data.get("vision_models", []) or []
        minio = data.get("minio", {}) or {}
        usage = data.get("usage", {}) or {}

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
                remote_username=r.get("remote_username"),
                priority=int(r.get("priority", 100)),
            ))

        bot_id = gitlab.get("bot_id")
        return Settings(
            gitlab_url=gitlab["url"].rstrip("/"),
            gitlab_token=gitlab["bot_token"],
            webhook_secret=gitlab.get("webhook_secret", ""),
            bot_id=int(bot_id) if bot_id not in (None, "") else None,
            bot_username=gitlab.get("bot_username"),
            verify_ssl=bool(gitlab.get("verify_ssl", True)),
            gitlab_owner_token=gitlab.get("owner_token", ""),
            max_concurrent_repos=int(worker.get("max_concurrent_repos", 3)),
            task_timeout_seconds=int(worker.get("task_timeout_seconds", 1800)),
            max_retries=int(worker.get("max_retries", 2)),
            reconcile_interval_seconds=int(worker.get("reconcile_interval_seconds", 300)),
            issue_priority_labels=_issue_priority_labels(worker),
            pause_windows=_pause_windows(worker),
            pause_weekdays=_pause_weekdays(worker),
            pause_timezone=str(worker.get("pause_timezone") or "").strip(),
            ci_wait_detect_seconds=int(worker.get("ci_wait_detect_seconds", 120)),
            ci_wait_interval_seconds=int(worker.get("ci_wait_interval_seconds", 15)),
            ci_wait_timeout_seconds=int(worker.get("ci_wait_timeout_seconds", 1800)),
            claude_command=claude.get("command", "claude"),
            claude_args=claude.get("args", ["-p", "--output-format", "json"]),
            engine=str(worker.get("engine", "claude")).strip() or "claude",
            plugin_paths=[str(p).strip() for p in (worker.get("plugin_paths") or [])
                          if str(p).strip()],
            dsh_provider=str(dsh.get("provider", "deepseek-official")).strip() or "deepseek-official",
            dsh_model=str(dsh.get("model", "deepseek-v4-flash")).strip() or "deepseek-v4-flash",
            dsh_max_tokens=dsh.get("max_tokens") if isinstance(dsh.get("max_tokens"), int) else None,
            dsh_reasoning_effort=str(dsh.get("reasoning_effort", "")).strip(),
            dsh_session_root=str(dsh.get("session_root", "")).strip(),
            dsh_cordis=str(dsh.get("cordis", "")).strip(),
            dsh_runtime_bin=str(dsh.get("runtime_bin", "")).strip(),
            dsh_base_url=str(dsh.get("base_url", "")).strip(),
            dsh_api_key=str(dsh.get("api_key", "")).strip(),
            default_template=tpl.get("default", DEFAULT_TEMPLATE),
            # 中断恢复模版（issue #116）：缺失/空串均归一为内置默认
            resume_template=tpl.get("resume", "") or DEFAULT_RESUME_PROMPT,
            browse_default_path=browse.get("default_path") or None,
            backup_enabled=bool(backup.get("enabled", True)),
            backup_retention_days=int(backup.get("retention_days", 30)),
            ui_timezone=(ui.get("timezone") or "").strip(),
            ui_show_disabled_repos=_ui_default(ui, "show_disabled_repos", True),
            ui_theme=_ui_theme(ui),
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
            ai_providers=[
                {
                    "name": str(p.get("name", "")).strip(),
                    "provider": str(p.get("provider", "")).strip() or "custom",
                    "base_url": str(p.get("base_url", "")).strip(),
                    # api_key 支持 ${ENV} 引用（load 时已展开为明文）
                    "api_key": str(p.get("api_key") or ""),
                    "model": str(p.get("model", "")).strip(),
                    "enabled": bool(p.get("enabled", True)),
                }
                for p in providers_raw
                if isinstance(p, dict) and p.get("name")
            ],
            image_models=[
                {
                    "name": str(m.get("name", "")).strip(),
                    "provider": str(m.get("provider", "")).strip() or "custom",
                    "base_url": str(m.get("base_url", "")).strip(),
                    # api_key 支持 ${ENV} 引用（load 时已展开为明文）
                    "api_key": str(m.get("api_key") or ""),
                    "model": str(m.get("model", "")).strip(),
                    "enabled": bool(m.get("enabled", True)),
                }
                for m in image_models_raw
                if isinstance(m, dict) and m.get("name")
            ],
            vision_models=[
                {
                    "name": str(m.get("name", "")).strip(),
                    "provider": str(m.get("provider", "")).strip() or "custom",
                    "base_url": str(m.get("base_url", "")).strip(),
                    # api_key 支持 ${ENV} 引用（load 时已展开为明文）
                    "api_key": str(m.get("api_key") or ""),
                    "model": str(m.get("model", "")).strip(),
                    "enabled": bool(m.get("enabled", True)),
                }
                for m in vision_models_raw
                if isinstance(m, dict) and m.get("name")
            ],
            minio_enabled=bool(minio.get("enabled", False)),
            minio_endpoint=str(minio.get("endpoint") or "").strip(),
            minio_secure=bool(minio.get("secure", False)),
            minio_access_key=str(minio.get("access_key") or "").strip(),
            minio_secret_key=str(minio.get("secret_key") or "").strip(),
            minio_bucket=str(minio.get("bucket") or "public").strip(),
            minio_public_base_url=str(minio.get("public_base_url") or "").strip(),
            minio_verify_ssl=bool(minio.get("verify_ssl", True)),
            # issue #235：usage 段——currency（默认 USD）+ pricing 单价表
            usage_currency=(str(usage.get("currency") or "USD").strip() or "USD"),
            usage_pricing=[
                p for p in (usage.get("pricing") or []) if isinstance(p, dict)],
            webhook_enabled=bool(webhook.get("enabled", False)),
            webhook_url=str(webhook.get("url", "")).strip(),
            webhook_content_type=str(webhook.get("content_type", "")).strip()
            or "application/json",
            # authorization 支持 ${ENV} 引用（load 时已展开为明文）
            webhook_authorization=str(webhook.get("authorization") or ""),
            # body_template 留空 = 内置默认模板（推送内容保证关键信息）
            webhook_body_template=webhook.get("body_template", "")
            or DEFAULT_WEBHOOK_TEMPLATE,
        )

    def save(self) -> None:
        """将内存数据写回 config.yaml（原子写）。

        先写同目录临时文件再 os.replace 原子替换：修复前直接 open('w')
        截断写盘，并发读（get() 的 mtime 自动重载 / update_* 的
        _reload_from_disk）会读到半截 YAML，偶发 YAMLError / 解析出残缺
        配置导致 settings 保存 500（issue #181 CI 诊断）。
        """
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._data, f, allow_unicode=True,
                               sort_keys=False)
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
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

    def update_gitlab(self, patch: dict[str, Any]) -> Settings:
        """更新 gitlab 配置并写回（owner token；issue #87）。

        前端回传的掩码值（含 *）或空串视为"未修改"，不覆盖真实凭据
        （与 sso.client_secret 同模式）。
        """
        self._reload_from_disk()
        gitlab = self._data.setdefault("gitlab", {})
        for key in KNOWN_FIELDS["gitlab"]:
            if key not in patch:
                continue
            val = patch[key]
            if key == "owner_token" and (val is None or "*" in str(val)
                                         or not str(val).strip()):
                continue  # 掩码占位符/空串：保持现有凭据
            gitlab[key] = val
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

    def update_dsh(self, patch: dict[str, Any]) -> Settings:
        """更新 dsh 配置并写回（issue #84）。

        前端回传的 api_key 掩码值（含 *）或空串视为"未修改"，不覆盖
        真实凭据（与 gitlab.owner_token 同模式）。
        """
        self._reload_from_disk()
        dsh = self._data.setdefault("dsh", {})
        for key in KNOWN_FIELDS["dsh"]:
            if key not in patch:
                continue
            val = patch[key]
            if key == "api_key" and (val is None or "*" in str(val)
                                     or not str(val).strip()):
                continue  # 掩码占位符/空串：保持现有凭据
            dsh[key] = val
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_default_template(self, text: str) -> Settings:
        self._reload_from_disk()
        self._data.setdefault("templates", {})["default"] = text
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_resume_template(self, text: str) -> Settings:
        """更新中断恢复模版并写回（issue #116）。

        与 update_default_template 不同：空白文本 = 移除自定义键恢复
        内置默认（中断恢复必须有引导语，不允许空模版导致恢复会话
        无提示词裸跑）；非空则写入 templates.resume。
        """
        self._reload_from_disk()
        templates = self._data.setdefault("templates", {})
        text = text.strip()
        if text:
            templates["resume"] = text
        else:
            templates.pop("resume", None)
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

    def remove_repo(self, project_id: int) -> None:
        """从 repos 列表移除指定 project_id 的仓库并落盘（issue #61）。

        删除仓库时由 API 层调用；先重读磁盘再过滤保存，
        避免覆盖并发写入的其他配置（与 update_* 系列一致）。
        """
        self._reload_from_disk()
        self._data["repos"] = [
            r for r in self._data["repos"]
            if r.get("project_id") != project_id
        ]
        self.save()
        self.settings = self._to_settings(self._data)

    def update_custom_labels(self, labels: list[dict[str, Any]]) -> Settings:
        """整体替换自定义标签列表（标记库页增删后落盘，issue #29）。"""
        self._reload_from_disk()
        self._data.setdefault("labels", {})["custom"] = labels
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_ai_providers(self, providers: list[dict[str, Any]]) -> Settings:
        """整体替换 AI 供应商列表（设置页增删改后落盘，issue #46）。"""
        self._reload_from_disk()
        self._data["ai_providers"] = providers
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_image_models(self, models: list[dict[str, Any]]) -> Settings:
        """整体替换生图模型列表（设置页增删改后落盘，issue #135）。"""
        self._reload_from_disk()
        self._data["image_models"] = models
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_vision_models(self, models: list[dict[str, Any]]) -> Settings:
        """整体替换识图模型列表（设置页增删改后落盘，issue #152）。"""
        self._reload_from_disk()
        self._data["vision_models"] = models
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_minio(self, patch: dict[str, Any]) -> Settings:
        """更新 MinIO 对象存储配置并写回（issue #163）。

        access_key / secret_key 的掩码值（含 *）或空串视为「未修改」，
        不覆盖真实凭据（与 webhook.authorization / sso.client_secret
        同模式）；endpoint / public_base_url 空白归一为空串（由
        minio_client 回退默认/环境变量）。
        """
        self._reload_from_disk()
        minio = self._data.setdefault("minio", {})
        for key in KNOWN_FIELDS["minio"]:
            if key not in patch:
                continue
            val = patch[key]
            if key in ("access_key", "secret_key") and (
                    val is None or "*" in str(val) or not str(val).strip()):
                continue  # 掩码占位符/空串：保持现有凭据
            if key in ("endpoint", "public_base_url"):
                minio[key] = str(val or "").strip()
                continue
            minio[key] = val
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def update_webhook(self, patch: dict[str, Any]) -> Settings:
        """更新 webhook 推送配置并写回（issue #136）。

        前端回传的 authorization 掩码值（含 *）或空串视为「未修改」，
        不覆盖真实凭据（与 sso.client_secret 同模式）。
        """
        self._reload_from_disk()
        webhook = self._data.setdefault("webhook", {})
        for key in KNOWN_FIELDS["webhook"]:
            if key not in patch:
                continue
            val = patch[key]
            if key == "authorization" and (val is None or "*" in str(val)
                                             or not str(val).strip()):
                continue  # 掩码占位符/空串：保持现有凭据
            webhook[key] = val
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
            "priority": repo.priority,
        }
        if repo.prompt_template:
            d["prompt_template"] = repo.prompt_template
        if repo.local_path:
            d["local_path"] = repo.local_path
        if repo.remote_name:
            d["remote_name"] = repo.remote_name
        if repo.remote_username:
            d["remote_username"] = repo.remote_username
        return d
