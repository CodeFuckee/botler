"""API 路由汇总。依赖从 request.app.state 获取（见 main.py 的 AppContext）。"""

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api")


def ctx(request: Request):
    """从请求中取全局依赖容器。"""
    return request.app.state.ctx


from .repos import router as repos_router  # noqa: E402
from .tasks import router as tasks_router  # noqa: E402
from .settings import router as settings_router  # noqa: E402
from .backup import router as backup_router  # noqa: E402
from .notifications import router as notifications_router  # noqa: E402
from .environment import router as environment_router  # noqa: E402
from .auth import router as auth_router  # noqa: E402
from .labels import router as labels_router  # noqa: E402
from .pipelines import router as pipelines_router  # noqa: E402
from .issues import router as issues_router  # noqa: E402
from .inspirations import router as inspirations_router  # noqa: E402
from .plugins import router as plugins_router
from .terminal import router as terminal_router  # noqa: E402  # noqa: E402

router.include_router(repos_router)
router.include_router(tasks_router)
router.include_router(settings_router)
router.include_router(backup_router)
router.include_router(notifications_router)
router.include_router(environment_router)
router.include_router(auth_router)
router.include_router(labels_router)
router.include_router(pipelines_router)
router.include_router(issues_router)
router.include_router(inspirations_router)
router.include_router(plugins_router)
router.include_router(terminal_router)
