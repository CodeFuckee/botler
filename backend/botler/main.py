"""Botler FastAPI 应用入口。

启动流程：
1. 加载 config.yaml → 初始化 SQLite / GitLab 客户端 / 执行器 / 调度器 / 对账
2. 把 config.yaml 中的仓库同步到数据库镜像
3. 挂载 /api 路由、/webhook/gitlab、前端静态文件

启动：cd backend && uvicorn botler.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .minio_public import router as minio_public_router
from .auth import CsrfGuardMiddleware, SsoAuth, SsoGuardMiddleware
from .audit import config_diff_summary
from .backup import BotlerBackup
from .config import ConfigManager
from .database import Database
from .executor import ClaudeExecutor
from .gitlab_client import GitLabClient, configure_default_rate_limiter
from .log_redact import install_redact_filter, register_config_secrets
from .reconciler import Reconciler
from .retention import RetentionManager
from .scheduler import TaskScheduler
from .templates import TemplateRenderer
from .health import build_deps_report, deps_critical_failed
from .metrics import CONTENT_TYPE_LATEST, render_metrics
from .version import build_health_payload, read_version_info
from .webhook import WebhookHandler, WebhookError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
# 统一日志脱敏（issue #259）：全仓库所有 logging 输出（executor 子进程
# 输出、GitLab API 请求细节、webhook 处理日志等）在写入前自动打码
# token/密钥。挂 root handler 而非 root logger——子 logger 不继承父
# logger 的 filter，但都会传播到 root handler（见 install_redact_filter）
install_redact_filter()
logger = logging.getLogger("botler")

# 前端构建产物目录（frontend/dist），路径随包位置回退
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


def _default_version_txt() -> Path:
    """版本号回退文件 data/version.txt 路径（issue #233）：与
    frontend/scripts/gen-version.mjs 的 BOTLER_DATA_DIR 约定同源，
    无 dist 构建产物时（本地开发/CI 测试）读取纯版本号兜底。"""
    data_dir = os.environ.get("BOTLER_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "version.txt"
    return BACKEND_DIR.parent / "data" / "version.txt"


def load_version_info() -> dict | None:
    """读取当前平台版本信息（issue #233）：优先 frontend/dist/version.json
    （与前端 VersionBadge 同源的构建产物，含版本 + 构建时间 + commit，
    由 CI frontend:build 产物下载到 FastAPI 静态托管目录），回退
    data/version.txt（仅版本号）。"""
    return read_version_info(FRONTEND_DIST / "version.json", _default_version_txt())


@dataclass
class AppContext:
    """全局依赖容器，挂在 app.state.ctx 上供路由访问。"""
    config: ConfigManager
    db: Database
    gitlab: GitLabClient
    renderer: TemplateRenderer
    executor: ClaudeExecutor
    scheduler: TaskScheduler
    reconciler: Reconciler
    webhook: WebhookHandler
    backup: BotlerBackup
    retention: RetentionManager
    sso: SsoAuth
    config_path: str = ""


def build_context(config_path: str | None = None) -> AppContext:
    config = ConfigManager(config_path or os.environ.get("BOTLER_CONFIG", str(BACKEND_DIR / "config.yaml")))
    settings = config.load()
    # 统一日志脱敏（issue #259）：配置中声明的凭据 Key（gitlab token /
    # ai_providers[].api_key 等）自动纳入脱敏规则，日志打码据此生效
    register_config_secrets(settings)
    # 外部插件加载（issue #140）：worker.plugin_paths 配置的模块路径逐个
    # 加载注册（失败仅记日志不阻塞启动，见 PluginRegistry.load_external）
    from .plugins import get_registry
    loaded = get_registry().load_external(settings.plugin_paths)
    if loaded:
        logger.info("外部插件加载完成（%s 个模块）", len(loaded))
    db = Database()
    # 启动即应用配置，保证首次 webhook/手动对账前所有 GitLab API
    # 请求也使用用户设定的共享限速；Reconciler 每轮再刷新以支持热重载。
    configure_default_rate_limiter(settings.gitlab_api_requests_per_second)
    gitlab = GitLabClient(
        settings.gitlab_url, settings.gitlab_token,
        verify_ssl=settings.verify_ssl,
    )
    renderer = TemplateRenderer(config)
    executor = ClaudeExecutor(config, db, gitlab, renderer)
    scheduler = TaskScheduler(config, db, executor)
    reconciler = Reconciler(config, db, gitlab, scheduler)
    webhook = WebhookHandler(config, db, gitlab, scheduler)
    retention = RetentionManager(db, config)
    backup = BotlerBackup(config.path, db.path, config=config)
    # 手动与定时备份都先按 retention 策略收缩运行数据，降低备份成本。
    backup.pre_backup_cleanup = retention.cleanup
    sso = SsoAuth(config)
    # issue #260：直接编辑 config.yaml 的场景（mtime 变化触发磁盘重载时）
    # 记录一次「外部修改」审计——差异摘要含变更段/字段（敏感值打码）与
    # webhook 轮换标记；回调失败不影响配置加载主流程（audit 尽力而为）。
    def _on_config_external_change(old_data, new_data):
        db.add_audit_log(
            actor="external",
            action="config.external_edit",
            target_type="config",
            target_id=None,
            detail=config_diff_summary(old_data, new_data),
            ip="",
        )
    config.set_external_change_callback(_on_config_external_change)
    return AppContext(
        config=config, db=db, gitlab=gitlab, renderer=renderer,
        executor=executor, scheduler=scheduler, reconciler=reconciler,
        webhook=webhook, backup=backup, retention=retention, sso=sso, config_path=config.path,
    )


def _sync_config_repos_to_db(ctx: AppContext) -> None:
    """config.yaml 是唯一事实来源，启动时把仓库同步到 db 镜像。"""
    for repo in ctx.config.get().repos:
        existing = ctx.db.get_repo_by_project_id(repo.project_id)
        if existing:
            ctx.db.update_repo(
                existing["id"],
                name=repo.name, url=repo.url,
                prompt_template=repo.prompt_template,
                enabled=repo.enabled,
                local_path=repo.local_path, remote_name=repo.remote_name,
                remote_username=repo.remote_username,
                # issue #237/#424：仅同步仍有效的仓库级任务参数。
                # 历史 timeout_seconds 由数据库兼容保留，但不再参与执行。
                max_retries=repo.max_retries,
                engine=repo.engine,
                token_expires_at=repo.token_expires_at,
            )
        else:
            ctx.db.upsert_repo(
                project_id=repo.project_id, name=repo.name, url=repo.url,
                prompt_template=repo.prompt_template, enabled=repo.enabled,
                local_path=repo.local_path, remote_name=repo.remote_name,
                remote_username=repo.remote_username,
                max_retries=repo.max_retries,
                engine=repo.engine,
                token_expires_at=repo.token_expires_at)
    logger.info("config.yaml → db 同步完成（%s 个仓库）", len(ctx.config.get().repos))


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx = app.state.ctx
    # uvicorn 配置日志可能晚于应用导入，启动时幂等补挂日志脱敏 filter
    # （issue #259），覆盖 uvicorn 新增的 root / access handler
    install_redact_filter()
    _sync_config_repos_to_db(ctx)
    # 确认 bot 身份（网络不可达时降级为 None，webhook/对账时再取）
    try:
        ctx.gitlab.get_bot_id()
        if ctx.config.get().bot_username is None:
            logger.info("bot 账号确认: %s (id=%s)",
                        ctx.gitlab.test_connection().get("username"),
                        ctx.gitlab.get_bot_id())
    except Exception as e:  # noqa: BLE001
        logger.warning("启动时无法连接 GitLab（对账/入队将重试）: %s", e)
    ctx.scheduler.start()
    ctx.reconciler.start()
    ctx.backup.start_scheduler()
    ctx.retention.start_scheduler()
    logger.info("Botler 启动完成")
    yield
    ctx.scheduler.stop()
    ctx.reconciler.stop()
    ctx.backup.stop_scheduler()
    ctx.retention.stop_scheduler()
    logger.info("Botler 已停止")


def create_app(config_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="Botler",
        description="GitLab AI Issue Bot 平台",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 单机自用；如需收紧改为具体域名
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # SSO 登录保护（issue #27）：启用后 /api/* 除登录流程/健康检查外需登录
    # 执行顺序（add 逆序）：SsoGuard（登录校验）→ CsrfGuard（写请求
    # CSRF 双提交校验，issue #263）→ CORS → 路由
    app.add_middleware(CsrfGuardMiddleware)
    app.add_middleware(SsoGuardMiddleware)
    app.state.ctx = build_context(config_path)
    app.include_router(api_router)
    # issue #319：识图图片公网 URL（/minio-public/...）由后端直接流式返回
    # MinIO 图片桶对象，必须在 SPA 兜底（/{full_path:path}）之前注册，
    # 否则图片 URL 会被兜底成 index.html（模型取图拿到 HTML 报 url error）
    app.include_router(minio_public_router)

    # ---- webhook 接收器 ----
    @app.post("/webhook/gitlab")
    async def webhook_gitlab(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
        token = request.headers.get("X-Gitlab-Token")
        try:
            # WebhookHandler 会同步访问 GitLab REST API。该调用可能持续数秒，
            # 必须移出事件循环，避免阻塞健康检查、SSE 和其他 API 请求。
            result = await asyncio.to_thread(app.state.ctx.webhook.handle, body, token)
            return result
        except WebhookError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status_code)

    # ---- 健康检查 ----
    @app.get("/api/health")
    def health():
        ctx = app.state.ctx
        # 依赖探测（issue #207）：MinIO 连通（仅启用时）+ 数据目录磁盘
        # 空间；关键依赖失败 → 503 + ok=false，compose healthcheck
        # （curl -f）随之失败，容器被标记 unhealthy，假死可感知
        deps = build_deps_report(ctx.config.get())
        payload = build_health_payload(
            # 版本号与前端构建产物同源（issue #233）：读取 dist/version.json
            # （含构建时间与 commit），缺失时回退 data/version.txt
            load_version_info(),
            scheduler_stats=ctx.scheduler.stats(),
            task_stats=ctx.db.task_stats(),
            deps=deps,
            ok=not deps_critical_failed(deps),
        )
        if deps_critical_failed(deps):
            return JSONResponse(payload, status_code=503)
        return payload

    # ---- Prometheus 指标（issue #208）----
    # 根路径 /metrics（不在 /api/ 前缀下）：SSO 中间件只保护 /api/*，
    # 天然放行，符合 issue 验收「无 SSO 或受限访问保护该端点」；
    # Prometheus/Grafana 直接按此地址抓取
    @app.get("/metrics")
    def metrics():
        """Prometheus 文本格式运行指标：任务状态计数 / 执行时长 histogram /
        webhook 接收计数 / GitLab API 调用与错误计数 / 队列深度 / 磁盘与
        DB 大小 gauge（供 Prometheus + Grafana 观测平台运行状态）。"""
        return Response(
            content=render_metrics(app.state.ctx),
            media_type=CONTENT_TYPE_LATEST,
        )

    # ---- 前端静态托管 ----
    if FRONTEND_DIST.exists():
        assets = FRONTEND_DIST / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/")
        def index():
            return FileResponse(FRONTEND_DIST / "index.html")

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str):
            """React Router 前端路由回退到 index.html。"""
            candidate = FRONTEND_DIST / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")
    else:
        @app.get("/")
        def index_missing():
            return JSONResponse(
                {"error": "前端未构建。请在 frontend/ 下执行 npm install && npm run build"},
                status_code=503)

    return app


app = create_app()
