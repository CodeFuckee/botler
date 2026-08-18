"""任务完成 Webhook 消息推送（issue #136）。

任务成功收尾（打 bot-done 标签）时，按设置页配置的 Webhook 推送消息：
- url：webhook 地址（POST 目标）
- content_type：Content-Type 请求头（默认 application/json）
- authorization：Authorization 请求头（可选，如 Bearer 令牌）
- body_template：POST 结构体模板，支持全局模板占位符（与提示词模版
  同机制，见 templates.PLACEHOLDERS），请求时自动填充

推送为尽力而为：失败仅记日志，绝不阻塞任务收尾（与网页通知同容错策略）。
"""

from __future__ import annotations

import json
import logging

import httpx

from .config import ConfigManager, DEFAULT_WEBHOOK_TEMPLATE
from .templates import TemplateRenderer, project_path_from_url

logger = logging.getLogger(__name__)

# 推送请求超时（秒）：webhook 地址可能为外部服务，超时上限放宽到 15s，
# 但绝不阻塞任务收尾（发送失败仅记日志）
PUSH_TIMEOUT_SECONDS = 15


def _escape_json_str(value: str) -> str:
    """把变量值转义为 JSON 字符串字面量内容（不含外层引号）。

    如换行 → \\n、双引号 → \\"、反斜杠 → \\\\，用于嵌入「JSON
    字符串内嵌 JSON 文本」的双编码上下文（issue #298）。
    """
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _substitute_placeholders(node, variables: dict[str, str]):
    """在解析后的模板结构里递归替换占位符。

    - 普通字符串值（单编码上下文）：原始值替换，最终整体 json.dumps
      统一转义，任何特殊字符都不会破坏外层 JSON；
    - 「JSON 字符串内嵌 JSON 文本」的值（双编码上下文，如飞书 content
      字段）：先判定（占位符换成安全哨兵后仍能解析为 JSON 对象/数组），
      变量按 JSON 转义后替换，保证内层 JSON 也合法。
    """
    if isinstance(node, str):
        # 双编码上下文判定：占位符换成安全哨兵后仍是 JSON 对象/数组文本
        probe = node
        for key in variables:
            probe = probe.replace("{" + key + "}", "x")
        try:
            inner = json.loads(probe)
        except (TypeError, ValueError):
            inner = None
        if isinstance(inner, (dict, list)):
            out = node
            for key, value in variables.items():
                out = out.replace("{" + key + "}", _escape_json_str(value))
            try:
                reparsed = json.loads(out)
            except (TypeError, ValueError):
                return out
            return json.dumps(reparsed, ensure_ascii=False)
        out = node
        for key, value in variables.items():
            out = out.replace("{" + key + "}", value)
        return out
    if isinstance(node, list):
        return [_substitute_placeholders(v, variables) for v in node]
    if isinstance(node, dict):
        return {k: _substitute_placeholders(v, variables) for k, v in node.items()}
    return node


class WebhookPushError(Exception):
    """webhook 推送失败（未配置 / 网络错误 / 非 2xx 响应）。"""


class WebhookPusher:
    """读取配置、渲染 POST 结构体并发送 webhook 请求。"""

    def __init__(self, config: ConfigManager):
        self.config = config

    # ---- 构建 ----

    def build_variables(self, task: dict, repo_name: str = "",
                        repo_url: str = "", issue: dict | None = None) -> dict[str, str]:
        """构建占位符变量（与全局提示词模版一致，见 templates.PLACEHOLDERS）。

        issue 为可选完整 issue 信息（含 description/web_url，任务成功收尾
        前由 executor 拉取）；缺失时降级用任务记录数据（正文为空、链接按
        仓库 URL 拼接兜底）。
        """
        cfg = self.config.get()
        issue = issue or {}
        project_path = project_path_from_url(repo_url) if repo_url else repo_name
        gitlab_url = cfg.gitlab_url.rstrip("/")
        iid = str(issue.get("iid") or task.get("issue_iid") or "")
        merged = {
            "title": str(issue.get("title") or task.get("issue_title") or ""),
            "description": str(issue.get("description") or ""),
            # issue 链接：优先 issue 快照，缺失时按
            # {gitlab_url}/{project_path}/-/issues/{iid} 拼接兜底
            "web_url": str(issue.get("web_url") or ""),
            "project_id": str(issue.get("project_id") or task.get("project_id") or ""),
            "iid": iid,
        }
        if not merged["web_url"] and project_path and iid:
            merged["web_url"] = f"{gitlab_url}/{project_path}/-/issues/{iid}"
        return TemplateRenderer(self.config).build_variables(repo_name, merged, repo_url)

    def build_payload(self, variables: dict[str, str]) -> str:
        """渲染 POST 结构体模板为 JSON payload（issue #136 / #298）。

        JSON 感知渲染（issue #298）：模板按 JSON 解析后在字符串值上替换
        占位符、再整体序列化——issue 正文/标题含换行、引号、反斜杠等
        特殊字符时会被 JSON 正确转义，保证渲染结果始终是合法 JSON。

        背景：此前占位符是逐项原始替换，issue 描述含换行时 body 字段会
        出现裸换行，渲染出非法 JSON，被飞书等目标以 HTTP 400（code 9499）
        拒绝，用户感知为「webhook 通知不推送了」。修复后任意特殊字符下
        推送都稳定可用。

        双编码兼容：content 等字段是「JSON 字符串内嵌 JSON 文本」（飞书
        消息模板常见写法），替换后若值仍是合法 JSON 对象/数组则内层再
        序列化一次，内外层转义都正确。

        body_template 留空（配置未填）时用内置默认模板
        DEFAULT_WEBHOOK_TEMPLATE；模板不是合法 JSON 时退回逐项字符串
        替换（历史兼容，非 JSON 模板仍可正常渲染）。
        """
        template = self.config.get().webhook_body_template or DEFAULT_WEBHOOK_TEMPLATE
        try:
            structure = json.loads(template)
        except (TypeError, ValueError):
            logger.warning("webhook body_template 不是合法 JSON，退回逐项替换")
            payload = template
            for key, value in variables.items():
                payload = payload.replace("{" + key + "}", value)
            return payload
        substituted = _substitute_placeholders(structure, variables)
        return json.dumps(substituted, ensure_ascii=False, indent=2)

    # ---- 发送 ----

    def send(self, variables: dict[str, str]) -> dict:
        """按当前配置 POST 推送，返回 {"status_code", "text"}。

        未配置地址 / 网络失败 / 非 2xx 响应抛 WebhookPushError（调用方
        决定如何容错，任务收尾路径只记日志）。
        """
        cfg = self.config.get()
        url = (cfg.webhook_url or "").strip()
        if not url:
            raise WebhookPushError("webhook 地址未配置")

        payload = self.build_payload(variables)
        headers = {"Content-Type": cfg.webhook_content_type or "application/json"}
        if cfg.webhook_authorization:
            headers["Authorization"] = cfg.webhook_authorization

        try:
            with httpx.Client(timeout=PUSH_TIMEOUT_SECONDS,
                              verify=cfg.verify_ssl) as client:
                resp = client.post(url, content=payload.encode("utf-8"),
                                   headers=headers)
        except httpx.HTTPError as e:
            raise WebhookPushError(f"webhook 请求失败: {e}") from e

        if resp.status_code >= 400:
            raise WebhookPushError(
                f"webhook 目标返回 HTTP {resp.status_code}: {resp.text[:200]}")
        return {"status_code": resp.status_code, "text": resp.text[:200]}

    def send_task_succeeded(self, task: dict, repo_name: str = "",
                            repo_url: str = "", issue: dict | None = None) -> dict | None:
        """任务成功完成推送（issue #136）。

        未启用（webhook.enabled=false）或未配置地址时返回 None（不发送）；
        发送成功返回响应摘要；失败抛 WebhookPushError。
        """
        cfg = self.config.get()
        if not cfg.webhook_enabled or not (cfg.webhook_url or "").strip():
            return None
        variables = self.build_variables(task, repo_name, repo_url, issue)
        return self.send(variables)

    def send_test(self, repo_name: str = "测试仓库") -> dict:
        """设置页「测试推送」：发送一条测试消息验证配置可用性。

        与任务完成推送共用 send()（同样的地址/请求头/模板渲染），
        仅变量为测试数据；未配置地址抛 WebhookPushError。
        """
        cfg = self.config.get()
        gitlab_url = cfg.gitlab_url.rstrip("/")
        variables = {
            "repo_name": repo_name,
            "issue_title": "测试推送（Botler 设置页）",
            "issue_body": "这是一条来自 Botler 设置页的测试消息，"
                          "用于验证 webhook 配置是否可用。",
            "issue_url": f"{gitlab_url}/-/issues/0",
            "gitlab_url": gitlab_url,
            "gitlab_host": gitlab_url.split("://", 1)[-1],
            "project_id": "",
            "issue_iid": "0",
            "project_path": repo_name,
            "project_path_encoded": repo_name,
        }
        return self.send(variables)
