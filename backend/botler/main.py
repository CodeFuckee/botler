"""Botler FastAPI 应用入口。

启动流程：
1. 加载 config.yaml → 初始化 SQLite / GitLab 客户端 / 执行器 / 调度器 / 对账
2. 把 config.yaml 中的仓库同步到数据库镜像
3. 挂载 /api 路由、/webhook/gitlab、前端静态文件

启动：cd backend && uvicorn botler.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .auth import SsoAuth, SsoGuardMiddleware
from .backup import BotlerBackup
from .config import ConfigManager
from .database import Database
from .executor import ClaudeExecutor
from .gitlab_client import GitLabClient
from .reconciler import Reconciler
from .scheduler import TaskScheduler
from .templates import TemplateRenderer
from .webhook import WebhookHandler, WebhookError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("botler")

# 前端构建产物目录（frontend/dist），路径随包位置回退
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


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
    sso: SsoAuth
    config_path: str = ""


def build_context(config_path: str | None = None) -> AppContext:
    config = ConfigManager(config_path or os.environ.get("BOTLER_CONFIG", str(BACKEND_DIR / "config.yaml")))
    settings = config.load()
    # 外部插件加载（issue #140）：worker.plugin_paths 配置的模块路径逐个
    # 加载注册（失败仅记日志不阻塞启动，见 PluginRegistry.load_external）
    from .plugins import get_registry
    loaded = get_registry().load_external(settings.plugin_paths)
    if loaded:
        logger.info("外部插件加载完成（%s 个模块）", len(loaded))
    db = Database()
    gitlab = GitLabClient(
        settings.gitlab_url, settings.gitlab_token,
        verify_ssl=settings.verify_ssl,
    )
    renderer = TemplateRenderer(config)
    executor = ClaudeExecutor(config, db, gitlab, renderer)
    scheduler = TaskScheduler(config, db, executor)
    reconciler = Reconciler(config, db, gitlab, scheduler)
    webhook = WebhookHandler(config, db, gitlab, scheduler)
    backup = BotlerBackup(config.path, db.path, config=config)
    sso = SsoAuth(config)
    return AppContext(
        config=config, db=db, gitlab=gitlab, renderer=renderer,
        executor=executor, scheduler=scheduler, reconciler=reconciler,
        webhook=webhook, backup=backup, sso=sso, config_path=config.path,
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
            )
        else:
            ctx.db.upsert_repo(
                project_id=repo.project_id, name=repo.name, url=repo.url,
                prompt_template=repo.prompt_template, enabled=repo.enabled,
                local_path=repo.local_path, remote_name=repo.remote_name)
    logger.info("config.yaml → db 同步完成（%s 个仓库）", len(ctx.config.get().repos))


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx = app.state.ctx
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
    logger.info("Botler 启动完成")
    yield
    ctx.scheduler.stop()
    ctx.reconciler.stop()
    ctx.backup.stop_scheduler()
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
    app.add_middleware(SsoGuardMiddleware)
    app.state.ctx = build_context(config_path)
    app.include_router(api_router)

    # ---- webhook 接收器 ----
    @app.post("/webhook/gitlab")
    async def webhook_gitlab(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
        token = request.headers.get("X-Gitlab-Token")
        try:
            result = app.state.ctx.webhook.handle(body, token)
            return result
        except WebhookError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status_code)

    # ---- 健康检查 ----
    @app.get("/api/health")
    def health():
        ctx = app.state.ctx
        return {
            "ok": True,
            "version": "1.0.0",
            "scheduler": ctx.scheduler.stats(),
            "tasks": ctx.db.task_stats(),
        }

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
