"""运行数据保留 API：读取配置与手动执行清理（issue #204）。"""

from fastapi import APIRouter, Request

router = APIRouter(prefix="/retention", tags=["retention"])


@router.get("")
def get_retention(request: Request):
    s = request.app.state.ctx.config.get()
    return {
        "enabled": s.retention_enabled,
        "task_logs_days": s.retention_task_logs_days,
        "notification_events_days": s.retention_notification_events_days,
        "log_files_days": s.retention_log_files_days,
        "pm2_max_log_size_mb": s.retention_pm2_max_log_size_mb,
    }


@router.post("/cleanup")
def cleanup_retention(request: Request):
    """手动执行一次清理，方便部署后验证保留策略。"""
    return request.app.state.ctx.retention.cleanup()
