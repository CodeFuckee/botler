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


def _worker_bool(worker: dict, key: str, default: bool) -> bool:
    """读取 worker 段布尔开关：缺省或非布尔值回退默认（issue #238）。"""
    val = worker.get(key, default)
    return val if isinstance(val, bool) else default


def _tpl_bool(tpl: dict, key: str, default: bool) -> bool:
    """读取 templates 段布尔开关：缺省或非布尔值回退默认（issue #223）。"""
    val = tpl.get(key, default)
    return val if isinstance(val, bool) else default


def _tpl_body_max_chars(tpl: dict) -> int:
    """读取 templates.body_max_chars（issue #223）：正文注入提示词的最大
    字符数，非负整数；缺省/非法值回退 8000（防御手动编辑 config.yaml
    写坏）；0 = 不截断。"""
    val = tpl.get("body_max_chars", 8000)
    if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
        return val
    return 8000


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


def _pause_priority_threshold(worker: dict) -> int:
    """读取 worker.pause_priority_threshold（issue #299）：0~999 整数。
    0 = 关闭（所有仓库都受暂停窗口约束）；缺省/非法值回退 0（防御手动
    编辑 config.yaml 写坏，保证调度行为与未配置一致）。"""
    val = worker.get("pause_priority_threshold")
    if (isinstance(val, int) and not isinstance(val, bool)
            and 0 <= val <= 999):
        return val
    return 0


def _issue_priority_labels(worker: dict) -> list[str]:
    """读取 worker.issue_priority（issue #76）：必须是字符串列表且非空，
    否则回退默认顺序（防御手动编辑 config.yaml 写坏的情况）。"""
    val = worker.get("issue_priority")
    if (isinstance(val, list) and val
            and all(isinstance(x, str) and x.strip() for x in val)):
        return [x.strip() for x in val]
    return list(DEFAULT_ISSUE_PRIORITY)


def _alert_threshold(alerts: dict) -> float:
    """聚合告警失败率阈值解析（issue #229）：百分比 0~100，钳制在合法区间。

    支持 0~100 的百分比写法（如 50 = 50%）；缺失/非法值回退默认 50。
    """
    try:
        val = float(alerts.get("failure_rate_threshold", 50.0))
    except (TypeError, ValueError):
        return 50.0
    return min(100.0, max(0.0, val))


def _failure_classify_rules(cfg: dict) -> dict | None:
    """读取 config failure_classify.rules（issue #274）。

    格式为 {分类名: [正则串, ...]}；仅接受 env/engine/unsolvable 三类
    合法分类（unknown 为兜底，不允许配置），值必须是字符串列表。
    空配置/全部非法 → 返回 None（调用方使用内置默认规则 DEFAULT_RULES）。
    """
    rules = cfg.get("rules")
    if not isinstance(rules, dict) or not rules:
        return None
    from botler.failure_classify import VALID_CATEGORIES
    cleaned: dict[str, list[str]] = {}
    for category, patterns in rules.items():
        if category not in VALID_CATEGORIES or category == "unknown":
            continue
        if not isinstance(patterns, list):
            continue
        cleaned[category] = [str(p) for p in patterns if isinstance(p, str)]
    return cleaned if cleaned else None


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
    # 仓库级任务参数覆盖（issue #237）：None = 继承全局 worker 段对应配置
    max_retries: int | None = None  # 任务最大重试次数，覆盖 worker.max_retries
    engine: str | None = None  # 执行引擎，覆盖 worker.engine
    token_expires_at: str | None = None  # issue #279：仓库 PAT 到期日（YYYY-MM-DD）


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
    # issue #279：owner token 到期日（YYYY-MM-DD，空=未记录）。
    gitlab_owner_token_expires_at: str = ""
    max_concurrent_repos: int = 3
    # issue #424：执行引擎不设时限；保留字段仅兼容历史调用方。
    task_timeout_seconds: int | None = None
    max_retries: int = 2
    reconcile_interval_seconds: int = 300
    # GitLab API 防突发（issue #195）：所有 GitLabClient 共享该速率；全量
    # 对账还会在仓库之间按 jitter_min/max 加随机等待。
    gitlab_api_requests_per_second: float = 10.0
    reconcile_jitter_min_seconds: float = 0.5
    reconcile_jitter_max_seconds: float = 2.0
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
    # 暂停窗口豁免优先级阈值（issue #299）：仓库调度优先级（repos[].priority，
    # 1~999，数字越小越优先）不差于该阈值（priority <= 阈值）的仓库，在定时
    # 暂停窗口内仍可开始新任务（不受暂停窗口影响）；0 = 关闭（所有仓库都受
    # 暂停窗口约束，issue #169 行为不变）。设置页「任务调度」卡片可编辑。
    pause_priority_threshold: int = 0
    # 任务执行前预检（issue #238）：领取任务后、消耗模型调用前对环境做快速
    # 检查（git 凭据/token 有效性、local_path 可写、磁盘剩余空间、工作区
    # 可用），环境性失败直接判任务失败（不重试、不消耗模型调用），检查明细
    # 落库 tasks.precheck_result，任务详情页「元信息」区展示（✓/✗）。
    # precheck_enabled = 总开关（默认开启）；precheck_disk_min_free_mb =
    # 磁盘剩余空间阈值（MiB，默认 2048 = 2GB）。
    precheck_enabled: bool = True
    precheck_disk_min_free_mb: int = 2048
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
    # 备用引擎降级（issue #236）：主引擎健康探测不可用或连续
    # fallback_after_failures 次引擎类失败时，按此顺序自动降级到备用引擎
    # 重试任务（如 ["dsh", "hermes"]）。空列表 = 不降级（保持旧行为）。
    # 运行时自动剔除主引擎名与未注册引擎；全部备用引擎耗尽后保持当前引擎
    # 执行（探测为建议性，执行仍会暴露真实故障）。设置页「任务调度」卡片可编辑。
    fallback_engines: list[str] = field(default_factory=list)
    # 连续引擎类失败降级阈值（issue #236）：引擎执行失败被分类为「引擎类」
    # （命令缺失 / API key 无效 / SDK 错误，见 failure_classify.py）的连续次数
    # 达到该值后，自动降级到 worker.fallback_engines 的下一个备用引擎；任务级
    # 失败（代码改不对）不累计不降级（换引擎无意义）。正整数，默认 2。
    fallback_after_failures: int = 2
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
    # issue #397：dsh 段是否显式配置了 model（True = 用户手动指定，引擎
    # 直接透传；False = 未指定，模型跟随凭据解析链选中的 AI 供应商）
    dsh_model_explicit: bool = False
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
    # 结果评论模版（issue #252）：任务收尾时按结构化执行报告模版在 issue
    # 留评论（改动文件/diff 统计/测试摘要/commit 链接/用时）。为空或未配置
    # 时使用内置默认（report.DEFAULT_COMMENT_TEMPLATE / 失败用
    # DEFAULT_FAILURE_COMMENT_TEMPLATE）；支持 {diff_stat}/{test_summary}
    # 等占位符（见 templates.PLACEHOLDERS），空段落自动隐藏。
    comment_template: str = ""
    # 原始 issue 正文是否注入提示词（issue #223）：false = 正文不进 prompt，
    # {issue_body} 渲染为指向 issue 链接的提示（防 prompt injection，高风险
    # 场景可只给 URL）；{issue_body_urlenc} 仍可用（URL 编码形式安全）。
    raw_body_in_prompt: bool = True
    # issue 正文注入提示词的最大字符数（issue #223）：超长截断并在末尾追加
    # 「[描述已截断，共 N 字，完整见 issue_url]」标记；0 = 不截断。
    body_max_chars: int = 8000
    # 任务失败原因分类规则（issue #274）：config.yaml 的 failure_classify.rules
    # 可整体覆盖内置默认规则（botler.failure_classify.DEFAULT_RULES）——键为
    # 分类名（env/engine/unsolvable），值为正则字符串列表（re.IGNORECASE
    # 匹配）。None = 使用内置默认规则（代码常量，同样满足「规则可配置扩展」）。
    # 非法分类/非法正则被忽略，兜底 unknown 不报错。
    failure_classify_rules: dict | None = None
    browse_default_path: str | None = None
    backup_enabled: bool = True
    backup_retention_days: int = 30
    # 数据保留（issue #204）：仅删除终态任务明细和执行日志，任务摘要保留。
    retention_enabled: bool = True
    retention_task_logs_days: int = 90
    retention_notification_events_days: int = 30
    retention_log_files_days: int = 90
    retention_pm2_max_log_size_mb: int = 10
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
    # 任务失败自动创建 GitLab issue 上报（issue #347）：任务失败收尾时
    # 自动在任务所属项目创建失败上报 issue（标题含任务 id、bug+bot-failed
    # 标签、负责人按 assignee 指定）。设置页「任务失败自动上报」卡片可编辑。
    auto_issue_enabled: bool = True
    # 上报 issue 负责人用户名（GitLab 用户名，创建时解析为用户 id 指定；
    # 未配置 / 解析失败时不指定负责人，不阻塞上报）
    auto_issue_assignee: str = "agent"
    # 任务 token 用量与费用（issue #235）：usage.currency 为估算费用货币
    # （默认 USD）；usage.pricing 为模型单价表——每项 {model（支持子串
    # 匹配，如 deepseek-v4-flash / claude）, input_per_million（每百万
    # 输入 token 单价）, output_per_million（每百万输出 token 单价）}，
    # 空表 = 未配置单价，任务详情只展示 token 数不估算费用。
    usage_currency: str = "USD"
    usage_pricing: list[dict] = field(default_factory=list)
    # 聚合告警（issue #229）：平台异常主动通知（网页通知 in_app + webhook
    # 推送），替代「用户打开页面才发现」。检测并入对账循环（reconciler
    # 定时扫描），阈值在设置页「聚合告警」卡片可配置：
    #   enabled               聚合告警总开关（关闭 = 全部告警不检测不通知）
    #   notify_failure_rate   近 failure_rate_window 秒任务失败率 >
    #                         failure_rate_threshold → 通知
    #   failure_rate_threshold 失败率阈值（百分比 0~100，默认 50 = 50%）
    #   failure_rate_window   失败率统计窗口（秒，默认 3600 = 近 1 小时）
    #   notify_queue_backlog  队列堆积告警：活跃任务数 >
    #                         queue_backlog_threshold 且窗口内无任务收尾
    #                         （queue_stall_minutes 内无进度）→ 通知
    #   queue_backlog_threshold 队列积压条数阈值（活跃任务数）
    #   queue_stall_minutes   无进度判定窗口（分钟）
    #   notify_token_invalid  GitLab token 失效（401/403）→ 立即通知
    #   notify_disk_low       数据目录磁盘剩余 < disk_min_free_mb → 通知
    #   disk_min_free_mb      磁盘剩余阈值（MiB，默认 512，与 health 一致）
    #   throttle_seconds      同类告警节流窗口（秒，默认 3600）
    alerts_enabled: bool = True
    alert_failure_rate: bool = True
    alert_failure_rate_threshold: float = 50.0
    alert_failure_rate_window: int = 3600
    alert_queue_backlog: bool = True
    alert_queue_backlog_threshold: int = 5
    alert_queue_stall_minutes: int = 30
    alert_token_invalid: bool = True
    alert_token_expiry: bool = True
    alert_disk_low: bool = True
    alert_disk_min_free_mb: int = 512
    alert_throttle_seconds: int = 3600



# settings API 可写字段（写回 config.yaml 用）
KNOWN_FIELDS = {
    "gitlab": {"owner_token", "owner_token_expires_at"},
    "worker": {"max_concurrent_repos", "max_retries",
               "reconcile_interval_seconds", "gitlab_api_requests_per_second",
               "reconcile_jitter_min_seconds", "reconcile_jitter_max_seconds", "ci_wait_detect_seconds",
               "ci_wait_interval_seconds", "ci_wait_timeout_seconds",
               "engine", "plugin_paths", "issue_priority",
               "pause_windows", "pause_weekdays", "pause_timezone",
               "pause_priority_threshold",
               "fallback_engines", "fallback_after_failures",
               "precheck_enabled", "precheck_disk_min_free_mb"},
    "claude": {"command", "args"},
    "dsh": {"provider", "model", "max_tokens", "reasoning_effort",
            "session_root", "cordis", "runtime_bin", "base_url", "api_key"},
    "templates": {"default", "resume", "comment",
                  "raw_body_in_prompt", "body_max_chars"},
    "browse": {"default_path"},
    "backup": {"enabled", "retention_days"},
    "retention": {"enabled", "task_logs_days", "notification_events_days", "log_files_days", "pm2_max_log_size_mb"},
    "ui": {"timezone", "show_disabled_repos", "theme"},
    "notifications": {"enabled", "task_needs_interaction", "issue_completed",
                      "queue_empty", "queue_no_work"},
    "webhook": {"enabled", "url", "content_type", "authorization",
               "body_template"},
    "auto_issue": {"enabled", "assignee"},
    "sso": {"enabled", "well_known_url", "client_id", "client_secret", "scope",
            "session_days", "redirect_uri", "verify_ssl"},
    # MinIO 对象存储（issue #163）：识图图片上传配置。凭据掩码/空串 =
    # 保持现有值（SECTION_SCHEMAS["minio"].masked 处理），与 webhook.authorization 同模式。
    "minio": {"enabled", "endpoint", "secure", "access_key", "secret_key",
              "bucket", "public_base_url", "verify_ssl"},
    # 任务 token 用量（issue #235）：currency 为费用货币；pricing 为模型
    # 单价表（每项 model / input_per_million / output_per_million）。
    "usage": {"currency", "pricing"},
    "alerts": {"enabled", "notify_failure_rate", "failure_rate_threshold",
               "failure_rate_window", "notify_queue_backlog",
               "queue_backlog_threshold", "queue_stall_minutes",
               "notify_token_invalid", "notify_token_expiry", "notify_disk_low",
               "disk_min_free_mb", "throttle_seconds"},
}
# 配置段写回 schema（issue #193 泛化 update_*）：settings API 写回 config.yaml
# 的唯一入口 update_section(section, patch) 按此描述执行，替代原先 15+ 个
# 结构重复的 update_* 方法（读 yaml → 局部改 → 写回 → 重载同一骨架）。
# 新增配置段 = 在这里登记一行，无需再复制粘贴方法。
@dataclass(frozen=True)
class SectionSchema:
    """配置段写回描述。

    fields:              可写字段白名单（与 KNOWN_FIELDS 一致）；
    masked:              掩码字段（api_key / owner_token / client_secret /
                         authorization / access_key / secret_key）——patch
                         值为 None / 含 * / 空串视为「未修改」，不覆盖真实
                         凭据（语义集中实现，杜绝复制时漏掉）；
    trim:                写回前 str().strip() 空白归一（如 minio.endpoint /
                         public_base_url）；
    blank_means_default: 空串字段 → 移除键恢复内置默认（templates.resume /
                         comment，不允许空模版）；
    replace_list:        True 表示整段为列表（repos / ai_providers /
                         image_models / vision_models），patch 直接传列表
                         整体替换。
    """
    fields: tuple[str, ...]
    masked: tuple[str, ...] = ()
    trim: tuple[str, ...] = ()
    blank_means_default: tuple[str, ...] = ()
    replace_list: bool = False


SECTION_SCHEMAS: dict[str, SectionSchema] = {
    "worker": SectionSchema(fields=tuple(KNOWN_FIELDS["worker"])),
    "gitlab": SectionSchema(fields=tuple(KNOWN_FIELDS["gitlab"]),
                            masked=("owner_token",)),
    "claude": SectionSchema(fields=tuple(KNOWN_FIELDS["claude"])),
    "dsh": SectionSchema(fields=tuple(KNOWN_FIELDS["dsh"]),
                         masked=("api_key",)),
    "templates": SectionSchema(fields=tuple(KNOWN_FIELDS["templates"]),
                               blank_means_default=("resume", "comment")),
    "browse": SectionSchema(fields=tuple(KNOWN_FIELDS["browse"])),
    "backup": SectionSchema(fields=tuple(KNOWN_FIELDS["backup"])),
    "retention": SectionSchema(fields=tuple(KNOWN_FIELDS["retention"])),
    "ui": SectionSchema(fields=tuple(KNOWN_FIELDS["ui"])),
    "notifications": SectionSchema(fields=tuple(KNOWN_FIELDS["notifications"])),
    "webhook": SectionSchema(fields=tuple(KNOWN_FIELDS["webhook"]),
                             masked=("authorization",)),
    "auto_issue": SectionSchema(fields=tuple(KNOWN_FIELDS["auto_issue"])),
    "sso": SectionSchema(fields=tuple(KNOWN_FIELDS["sso"]),
                         masked=("client_secret",)),
    "minio": SectionSchema(fields=tuple(KNOWN_FIELDS["minio"]),
                           masked=("access_key", "secret_key"),
                           trim=("endpoint", "public_base_url")),
    "usage": SectionSchema(fields=tuple(KNOWN_FIELDS["usage"])),
    "alerts": SectionSchema(fields=tuple(KNOWN_FIELDS["alerts"])),
    "labels": SectionSchema(fields=("custom",)),
    "repos": SectionSchema(fields=(), replace_list=True),
    "ai_providers": SectionSchema(fields=(), replace_list=True),
    "image_models": SectionSchema(fields=(), replace_list=True),
    "vision_models": SectionSchema(fields=(), replace_list=True),
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

GitLab API 认证约定（issue #403：任务 #585 因误判认证失效而终止）：
- 认证验证以实际 API 调用为准：优先执行 `glab api user`（平台已配置
  GITLAB_HOST 与 glab 有效 token）；或从 `git remote get-url origin`
  内嵌凭据解析 token，用 `curl -k -H "PRIVATE-TOKEN: <token>" "{gitlab_url}/api/v4/user"` 验证。
- 注意：`glab auth status` 对 4 段式新格式 PAT（>26 字符）会误报
  "Invalid token provided"（glab 1.36 缺陷），不能据此判定认证失效；
  环境变量 `GITLAB_TOKEN` 在 dsh 引擎的 bash 工具中可能不可见（运行时
  按安全策略过滤 *TOKEN* 环境变量名），不要依赖它。
- 仅当实际 API 调用返回 401/403 且确认为凭据问题时，才按「认证失效即
  终止」处理并如实汇报；不得因验证方法误报而直接终止任务。

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
# （{repo_name}/{issue_title}/{issue_body}/{issue_title_urlenc}/
# {issue_body_urlenc}/{issue_url}/{gitlab_url}/{project_id}/{issue_iid}/
# {project_path}/{project_path_encoded}/{gitlab_host}），任务完成时自动
# 渲染填充；设置页留空 = 使用此默认值。
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
        retention = data.get("retention", {}) or {}
        ui = data.get("ui", {})
        notify = data.get("notifications", {})
        webhook = data.get("webhook", {}) or {}
        auto_issue = data.get("auto_issue", {}) or {}
        sso = data.get("sso", {})
        repos_raw = data.get("repos", []) or []
        labels_raw = (data.get("labels", {}) or {}).get("custom", []) or []
        providers_raw = data.get("ai_providers", []) or []
        image_models_raw = data.get("image_models", []) or []
        vision_models_raw = data.get("vision_models", []) or []
        minio = data.get("minio", {}) or {}
        usage = data.get("usage", {}) or {}
        alerts = data.get("alerts", {}) or {}
        failure_classify = data.get("failure_classify", {}) or {}

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
                # issue #424：忽略历史仓库 timeout_seconds，执行引擎不限时。
                max_retries=(int(r["max_retries"])
                             if r.get("max_retries") not in (None, "")
                             else None),
                engine=(str(r["engine"]).strip()
                        if r.get("engine") not in (None, "")
                        else None),
                token_expires_at=(str(r["token_expires_at"]).strip()
                                  if r.get("token_expires_at") not in (None, "")
                                  else None),
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
            gitlab_owner_token_expires_at=str(gitlab.get("owner_token_expires_at") or "").strip(),
            max_concurrent_repos=int(worker.get("max_concurrent_repos", 3)),
            # issue #424：忽略历史 task_timeout_seconds，三个执行引擎均无限时。
            task_timeout_seconds=None,
            max_retries=int(worker.get("max_retries", 2)),
            reconcile_interval_seconds=int(worker.get("reconcile_interval_seconds", 300)),
            gitlab_api_requests_per_second=max(0.1, float(worker.get("gitlab_api_requests_per_second", 10))),
            reconcile_jitter_min_seconds=max(0.0, float(worker.get("reconcile_jitter_min_seconds", 0.5))),
            reconcile_jitter_max_seconds=max(
                max(0.0, float(worker.get("reconcile_jitter_min_seconds", 0.5))),
                float(worker.get("reconcile_jitter_max_seconds", 2.0))),
            issue_priority_labels=_issue_priority_labels(worker),
            pause_windows=_pause_windows(worker),
            pause_weekdays=_pause_weekdays(worker),
            pause_timezone=str(worker.get("pause_timezone") or "").strip(),
            pause_priority_threshold=_pause_priority_threshold(worker),
            precheck_enabled=_worker_bool(worker, "precheck_enabled", True),
            precheck_disk_min_free_mb=max(1, int(worker.get("precheck_disk_min_free_mb", 2048))),
            ci_wait_detect_seconds=int(worker.get("ci_wait_detect_seconds", 120)),
            ci_wait_interval_seconds=int(worker.get("ci_wait_interval_seconds", 15)),
            ci_wait_timeout_seconds=int(worker.get("ci_wait_timeout_seconds", 1800)),
            claude_command=claude.get("command", "claude"),
            claude_args=claude.get("args", ["-p", "--output-format", "json"]),
            engine=str(worker.get("engine", "claude")).strip() or "claude",
            fallback_engines=[str(e).strip().lower() for e in
                              (worker.get("fallback_engines") or [])
                              if str(e).strip()],
            fallback_after_failures=max(1, int(worker.get("fallback_after_failures", 2))),
            plugin_paths=[str(p).strip() for p in (worker.get("plugin_paths") or [])
                          if str(p).strip()],
            dsh_provider=str(dsh.get("provider", "deepseek-official")).strip() or "deepseek-official",
            dsh_model=str(dsh.get("model", "deepseek-v4-flash")).strip() or "deepseek-v4-flash",
            dsh_model_explicit=bool(str(dsh.get("model", "") or "").strip()),
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
            # 结果评论模版（issue #252）：缺失/空串 = 内置默认（收尾评论
            # 渲染层 fallback，见 executor._build_report_comment）
            comment_template=tpl.get("comment", ""),
            # issue #223：正文注入控制——原始描述开关 + 长度上限
            raw_body_in_prompt=_tpl_bool(tpl, "raw_body_in_prompt", True),
            body_max_chars=_tpl_body_max_chars(tpl),
            # issue #274：失败分类规则可配置扩展——config failure_classify.rules
            # 整体覆盖内置默认规则；空/缺失/非法值归一为 None（用内置默认）
            failure_classify_rules=_failure_classify_rules(failure_classify),
            browse_default_path=browse.get("default_path") or None,
            backup_enabled=bool(backup.get("enabled", True)),
            backup_retention_days=int(backup.get("retention_days", 30)),
            retention_enabled=bool(retention.get("enabled", True)),
            retention_task_logs_days=max(1, int(retention.get("task_logs_days", 90))),
            retention_notification_events_days=max(1, int(retention.get("notification_events_days", 30))),
            retention_log_files_days=max(1, int(retention.get("log_files_days", 90))),
            retention_pm2_max_log_size_mb=max(1, int(retention.get("pm2_max_log_size_mb", 10))),
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
            alerts_enabled=bool(alerts.get("enabled", True)),
            alert_failure_rate=bool(alerts.get("notify_failure_rate", True)),
            alert_failure_rate_threshold=_alert_threshold(alerts),
            alert_failure_rate_window=max(60, int(alerts.get("failure_rate_window", 3600))),
            alert_queue_backlog=bool(alerts.get("notify_queue_backlog", True)),
            alert_queue_backlog_threshold=max(1, int(alerts.get("queue_backlog_threshold", 5))),
            alert_queue_stall_minutes=max(1, int(alerts.get("queue_stall_minutes", 30))),
            alert_token_invalid=bool(alerts.get("notify_token_invalid", True)),
            alert_token_expiry=bool(alerts.get("notify_token_expiry", True)),
            alert_disk_low=bool(alerts.get("notify_disk_low", True)),
            alert_disk_min_free_mb=max(1, int(alerts.get("disk_min_free_mb", 512))),
            alert_throttle_seconds=max(60, int(alerts.get("throttle_seconds", 3600))),
            webhook_enabled=bool(webhook.get("enabled", False)),
            webhook_url=str(webhook.get("url", "")).strip(),
            webhook_content_type=str(webhook.get("content_type", "")).strip()
            or "application/json",
            # authorization 支持 ${ENV} 引用（load 时已展开为明文）
            webhook_authorization=str(webhook.get("authorization") or ""),
            # body_template 留空 = 内置默认模板（推送内容保证关键信息）
            webhook_body_template=webhook.get("body_template", "")
            or DEFAULT_WEBHOOK_TEMPLATE,
            # 任务失败自动上报 issue（issue #347）：enabled 默认开启（需求
            # 指定自动提交）；assignee 默认 agent（负责人），配置为空串时
            # 归一为内置默认
            auto_issue_enabled=bool(auto_issue.get("enabled", True)),
            auto_issue_assignee=str(auto_issue.get("assignee", "")).strip()
            or "agent",
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

    def update_section(self, section: str, patch: Any) -> Settings:
        """泛型配置段写回（issue #193）：读 yaml → 局部改 → 原子写 → 重载。

        原先 15+ 个结构重复的 update_* 方法（update_worker / update_gitlab /
        update_claude / update_dsh / update_browse / update_backup / update_ui /
        update_notifications / update_sso / update_minio / update_webhook /
        update_repos / update_custom_labels / update_ai_providers /
        update_image_models / update_vision_models / update_default_template /
        update_resume_template / update_comment_template）全部收敛于此：
        按 SECTION_SCHEMAS[section] 的描述集中处理字段白名单、掩码保持
        （空串/含 * 不覆盖真实凭据）、空白归一、空串恢复内置默认与整体
        列表替换；写回前重读磁盘保留用户手动编辑（issue #25），统一走
        原子写 save()（temp + rename，写一半崩溃不损坏 config.yaml）。

        patch 形状由 schema 决定：
        - replace_list 段（repos / ai_providers / image_models /
          vision_models）：patch 为列表，整体替换；
        - 其余段：patch 为字段字典，仅写白名单字段。

        未知配置段抛 ValueError（拒绝静默写坏 config.yaml）。
        """
        schema = SECTION_SCHEMAS.get(section)
        if schema is None:
            raise ValueError(f"未知配置段: {section}")
        self._reload_from_disk()
        if schema.replace_list:
            if not isinstance(patch, list):
                raise TypeError(
                    f"配置段 {section} 需要列表整体替换，"
                    f"收到 {type(patch).__name__}")
            self._data[section] = patch
        else:
            if not isinstance(patch, dict):
                raise TypeError(
                    f"配置段 {section} 需要字段字典，"
                    f"收到 {type(patch).__name__}")
            target = self._data.setdefault(section, {})
            for key in schema.fields:
                if key not in patch:
                    continue
                val = patch[key]
                if key in schema.masked and (
                        val is None or "*" in str(val)
                        or not str(val).strip()):
                    continue  # 掩码占位符/空串：保持现有凭据
                if key in schema.trim:
                    target[key] = str(val or "").strip()
                    continue
                if key in schema.blank_means_default:
                    val = str(val or "").strip()
                    if val:
                        target[key] = val
                    else:
                        target.pop(key, None)  # 空串：移除键恢复内置默认
                    continue
                target[key] = val
        self.save()
        self.settings = self._to_settings(self._data)
        return self.settings

    def remove_repo(self, project_id: int) -> None:
        """从 repos 列表移除指定 project_id 的仓库并落盘（issue #61）。

        删除仓库时由 API 层调用；先重读磁盘再过滤保存，
        避免覆盖并发写入的其他配置（与 update_section 一致）。
        """
        self._reload_from_disk()
        self._data["repos"] = [
            r for r in self._data["repos"]
            if r.get("project_id") != project_id
        ]
        self.save()
        self.settings = self._to_settings(self._data)

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
        # issue #424：历史 timeout_seconds 不再写回配置，执行引擎始终不限时。
        if repo.max_retries is not None:
            d["max_retries"] = repo.max_retries
        if repo.engine:
            d["engine"] = repo.engine
        if repo.token_expires_at:
            d["token_expires_at"] = repo.token_expires_at
        return d

