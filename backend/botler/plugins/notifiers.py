"""任务消息发送通道插件（issue #140）。

把现有任务收尾的两类消息发送迁移为插件体系：任务成功 / 失败时向全部
已注册的 notifier 插件分发（各通道自检启用条件，未启用跳过；任一通道
失败仅记日志，不阻塞任务收尾）。

内置插件：
- ``webhook``：任务完成外部 Webhook HTTP 消息推送（issue #136）
- ``in_app``：网页通知事件记录（issue #21）

新增通道：实现 ``NotifierPlugin`` 的 send_* 并注册（或外部插件模块
加载），收尾分发自动覆盖新通道。
"""

from __future__ import annotations

from typing import Any

from .base import NotifierPlugin, register_plugin


class WebhookNotifierPlugin(NotifierPlugin):
    """外部 Webhook HTTP 消息推送（issue #136）。

    包装 WebhookPusher：按设置页 webhook 段配置发送（url / content_type /
    authorization / body_template）；未启用（webhook.enabled=false）或未
    配置地址返回 None（跳过）；发送失败抛 WebhookPushError（调用方统一
    容错，仅记日志不阻塞任务收尾）。
    """

    name = "webhook"
    description = "任务完成外部 Webhook HTTP 消息推送（issue #136）"

    def send_task_succeeded(self, context: Any, task: dict,
                            repo_name: str = "", repo_url: str = "",
                            issue: dict | None = None) -> dict | None:
        from ..webhook_push import WebhookPusher
        pusher = WebhookPusher(context.config)
        return pusher.send_task_succeeded(
            dict(task), repo_name=repo_name, repo_url=repo_url, issue=issue)

    def send_test(self, context: Any, repo_name: str = "测试仓库") -> dict:
        from ..webhook_push import WebhookPusher
        return WebhookPusher(context.config).send_test(repo_name=repo_name)


class InAppNotifierPlugin(NotifierPlugin):
    """网页通知事件记录（issue #21）。

    包装 Notifier：任务类事件靠 database 唯一索引幂等（同一任务收尾只记
    一次），前端轮询弹系统通知。
    """

    name = "in_app"
    description = "网页通知事件记录（issue #21）"

    def send_task_succeeded(self, context: Any, task: dict,
                            repo_name: str = "", repo_url: str = "",
                            issue: dict | None = None) -> dict | None:
        return context.notifier.task_succeeded(dict(task), repo_name or None)

    def send_task_failed(self, context: Any, task: dict,
                         reason: str, repo_name: str = "") -> dict | None:
        return context.notifier.task_failed(dict(task), reason, repo_name or None)


# 模块导入即注册内置消息通道插件（注册顺序 = 分发顺序）
register_plugin(WebhookNotifierPlugin())
register_plugin(InAppNotifierPlugin())
