"""任务失败自动创建 GitLab issue 上报插件（issue #347）。

任务失败收尾时（_finish_failed / _finish_asked 均发出 task_failed 事件，
经 executor._emit_task_event 向全部 notifier 插件分发），本插件在任务所属
仓库的 GitLab 项目自动创建一个失败上报 issue：

- 标题写明失败任务 id（任务详情页的任务编号）；
- 正文包含失败原因 / 失败分类（issue #274 分类徽章与处理建议）/
  原始 issue 链接 / 仓库 / 引擎 / 重试次数等；
- 标签 ``bug``（需求指定）+ ``bot-failed``（bot-failed 属于
  labels.CLAIM_SKIP_LABELS，对账调度器不会重新领取上报 issue——
  否则 bot 会去「修复」自己的失败上报形成 失败→上报→再失败 死循环）；
- 负责人按配置 ``auto_issue.assignee``（默认 ``agent``）解析 GitLab 用户
  id 指定。

配置（config.yaml ``auto_issue`` 段，设置页「任务失败自动上报」卡片）：
- ``enabled``：总开关（默认 true）；
- ``assignee``：上报 issue 负责人用户名（默认 ``agent``）。

通道自检：未启用（auto_issue.enabled=false）返回 None 跳过；创建失败抛
GitLabError（调用方统一容错，仅记日志不阻塞任务收尾，与 webhook 同模式）。
同一任务只上报一次：成功创建后在任务日志落「已自动提交失败上报 issue」标记
（issue #347 去重，多实例/重复分发时跳过）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..failure_classify import CATEGORY_ADVICE, category_label
from ..gitlab_client import GitLabError, GITLAB_ISSUE_TITLE_MAX_LEN
from ..templates import project_path_from_url
from .base import NotifierPlugin, register_plugin

logger = logging.getLogger("botler.plugins.auto_issue")

# 上报 issue 标签（issue #347）：bug = 需求指定；bot-failed = 防止对账
# 调度器重新领取（CLAIM_SKIP_LABELS），避免失败上报自身再被 bot 处理
# 形成死循环。
AUTO_ISSUE_LABELS = ("bug", "bot-failed")

# 错误详情最大长度（字符）：描述字段容纳远大于标题，但正文不宜过长，
# 超长截断并加省略号标记（与评论尾部截断同思路）。
AUTO_ISSUE_DETAIL_MAX_CHARS = 2000

# 任务日志去重标记前缀（add_log 落库，同一任务只上报一次）
DEDUP_LOG_MARK = "已自动提交失败上报 issue"

# 大模型 API 请求错误识别规则（issue #494）：任务失败由大模型 API 请求
# 错误导致（LLM 供应商限流 / 过载 / 服务不可用 / 请求失败等）时不自动
# 创建失败上报 issue——属平台侧共因（一个供应商故障影响所有任务），逐
# 任务上报只会刷屏，且重试后大概率自愈，无需人工逐个介入。
# 匹配原则：必须带供应商名 / Anthropic 错误码 / 「大模型|LLM」上下文，
# 避免误伤 GitLab 侧独立出现的 401/403/429 等错误（那些仍需上报）。
LLM_API_ERROR_PATTERNS: tuple[str, ...] = (
    # ① 供应商名 + 请求失败/API 错误：覆盖 chat/识图/生图模型统一报错
    #    格式「X 请求失败: HTTP ...」（chat_models / vision_models /
    #    models / image_models）与 Claude Code 的 "Anthropic API error"
    r"(anthropic|claude|deepseek|openai|gemini|qwen|zhipu|智谱|通义|"
    r"豆包|kimi|moonshot|doubao|dashscope|ollama)"
    r"[^。\n]{0,80}(请求失败|api[ _-]?error|overloaded|rate[ _-]?limit|"
    r"限流|过载|不可用|unavailable|billing|credit)",
    # ② Claude/Anthropic 供应商错误码（Anthropic messages API 返回的
    #    error.type 值：请求已发出、供应商拒绝或异常）
    r"(anthropic api error|overloaded_error|rate_limit_error|"
    r"authentication_error|permission_error|billing_not_active)",
    # ③ 大模型 API 请求失败通用表述（带「大模型|LLM」上下文，避免误伤
    #    GitLab 侧 401/403/429 等独立错误）
    r"(大模型|llm)[^。\n]{0,40}(api|接口)[^。\n]{0,40}"
    r"(请求失败|error|failed|失败|超时|timeout|限流|过载)",
)


def is_llm_api_error(*texts: str | None) -> bool:
    """判断失败文本是否由大模型 API 请求错误导致（issue #494）。

    多段文本（失败原因 / 错误详情）任一段命中即返回 True；全部未命中 /
    输入为空返回 False（兜底不误伤，保持自动上报）。
    """
    blob = "\n".join(t for t in texts if t)
    if not blob.strip():
        return False
    for pattern in LLM_API_ERROR_PATTERNS:
        if re.search(pattern, blob, re.IGNORECASE):
            return True
    return False


def build_issue_title(task: dict, repo_name: str = "") -> str:
    """构建上报 issue 标题：写明失败任务 id（issue #347）。

    格式：``[任务失败上报] 任务 #N 处理失败（issue #M）``；标题剩余空间
    足够时追加原始 issue 标题（截断到 GitLab 255 字符硬上限，issue #186）。
    """
    task_id = task.get("id") or 0
    issue_iid = task.get("issue_iid") or ""
    base = f"[任务失败上报] 任务 #{task_id} 处理失败"
    if issue_iid:
        base += f"（issue #{issue_iid}）"
    title = base
    origin = (task.get("issue_title") or "").strip()
    if origin:
        room = GITLAB_ISSUE_TITLE_MAX_LEN - len(base) - 1  # 留 1 个「：」
        if room > 8:  # 空间太小不值得追加
            if len(origin) > room:
                origin = origin[:room - 1] + "…"
            title = f"{base}：{origin}"
    return title


def build_issue_description(task: dict, reason: str,
                            repo_name: str = "",
                            repo_url: str = "",
                            gitlab_url: str = "",
                            category: str = "",
                            detail: str | None = None) -> str:
    """构建上报 issue 正文：失败任务 id + 原始 issue 链接 + 失败原因 +
    分类徽章与处理建议 + 引擎/重试/时间等上下文（issue #347）。

    纯函数便于单测：所有字段缺失时优雅降级（不抛错），GitLab 标题硬
    上限约束只在 build_issue_title 处理（正文无此限制）。
    """
    task_id = task.get("id") or 0
    issue_iid = task.get("issue_iid") or ""
    issue_title = (task.get("issue_title") or "").strip() or f"issue #{issue_iid}"
    # 分类未显式传入时从任务记录读取（调用方两种传法都兼容）
    category = category or str(task.get("failure_category") or "")
    # 原始 issue 链接：优先按仓库 URL 解析项目路径拼 GitLab 地址兜底
    issue_url = ""
    try:
        if repo_url:
            project_path = project_path_from_url(repo_url)
            base_url = (gitlab_url or "").rstrip("/")
            if base_url and project_path and issue_iid:
                issue_url = f"{base_url}/{project_path}/-/issues/{issue_iid}"
    except (ValueError, TypeError):
        issue_url = ""
    lines = [
        "🤖 Botler 自动上报：任务执行失败，请人工介入处理。",
        "",
        "## 失败任务",
        f"- **任务 ID**：`{task_id}`",
    ]
    if issue_iid:
        if issue_url:
            lines.append(f"- **原始 issue**：[{issue_title}]({issue_url})（#{issue_iid}）")
        else:
            lines.append(f"- **原始 issue**：{issue_title}（#{issue_iid}）")
    else:
        lines.append(f"- **原始 issue**：{issue_title}")
    if repo_name:
        lines.append(f"- **仓库**：{repo_name}")
    cat_label = category_label(category) if category else ""
    if cat_label:
        lines.append(f"- **失败分类**：{cat_label}（{category}）")
    engine = (task.get("engine") or "").strip()
    if engine:
        lines.append(f"- **执行引擎**：{engine}")
    if task.get("attempt_count") is not None:
        lines.append(f"- **尝试次数**：{task['attempt_count']}")
    if task.get("finished_at"):
        lines.append(f"- **失败时间**：{task['finished_at']}")
    lines += ["", "## 失败原因", "", reason or "（无失败原因）"]
    if cat_label:
        advice = CATEGORY_ADVICE.get(category) or ""
        if advice:
            lines += ["", "## 处理建议", "", advice]
    detail_text = (detail or "").strip()
    if detail_text:
        if len(detail_text) > AUTO_ISSUE_DETAIL_MAX_CHARS:
            detail_text = detail_text[:AUTO_ISSUE_DETAIL_MAX_CHARS] + "…"
        lines += ["", "## 错误详情", "", "```text", detail_text, "```"]
    return "\n".join(lines)


class AutoIssueNotifierPlugin(NotifierPlugin):
    """任务失败自动创建 GitLab issue 上报（issue #347）。

    包装为 notifier 通道插件：任务失败收尾事件（task_failed）经
    executor._emit_task_event 分发到本插件；未启用（auto_issue.enabled=
    false）返回 None 跳过；创建失败抛 GitLabError（调用方统一容错，
    仅记日志不阻塞任务收尾，与 webhook 通道同模式）。
    """

    name = "auto_issue"
    description = "任务失败自动创建 GitLab issue 上报（issue #347）"
    version = "1.0"

    def send_task_failed(self, context: Any, task: dict,
                         reason: str, repo_name: str = "") -> dict | None:
        cfg = context.config.get()
        if not cfg.auto_issue_enabled:
            return None
        task_id = int(task.get("id") or 0)
        # issue #494：大模型 API 请求错误导致的失败不自动上报——LLM 供应
        # 商限流/过载/服务不可用属平台侧共因（一个供应商故障影响所有
        # 任务），逐任务上报只会刷屏，且重试后大概率自愈，无需人工介入
        if is_llm_api_error(reason, task.get("error_detail")):
            logger.info("任务 %s 失败由大模型 API 请求错误导致，跳过失败上报",
                        task_id)
            return None
        # 去重（issue #347）：同一任务只上报一次——成功创建后在任务日志
        # 落标记；重复分发（多实例/事件重放）时跳过，避免重复 issue
        try:
            for log in context.db.list_logs(task_id) or []:
                if DEDUP_LOG_MARK in (log["message"] or ""):
                    logger.info("任务 %s 失败上报已存在，跳过重复创建", task_id)
                    return None
        except Exception:  # noqa: BLE001 查询日志失败不阻塞上报
            logger.warning("任务 %s 查询上报去重日志失败，继续创建", task_id)
        repo = None
        try:
            repo = context.db.get_repo(task.get("repo_id") or 0)
        except Exception:  # noqa: BLE001 仓库查询失败降级（用全局 client）
            logger.warning("任务 %s 查询仓库失败，降级用全局 client", task_id)
        repo_name = repo_name or (repo["name"] if repo else "")
        repo_url = repo["url"] if repo else ""
        category = task.get("failure_category") or ""
        title = build_issue_title(task, repo_name=repo_name)
        description = build_issue_description(
            task, reason or "", repo_name=repo_name, repo_url=repo_url,
            gitlab_url=cfg.gitlab_url, category=category,
            detail=task.get("error_detail"))
        project_id = int(task.get("project_id") or 0)
        if not project_id:
            logger.warning("任务 %s 缺少 project_id，跳过失败上报", task_id)
            return None
        assignee_id = self._resolve_assignee(context, repo, cfg)
        try:
            # _call_with_fallback 返回 (结果, client) 元组（与收尾评论同链路）
            issue, _client = context._transient_retry(
                "创建失败上报 issue",
                lambda: context._call_with_fallback(
                    repo, lambda cl: cl.create_issue(
                        project_id, title,
                        description=description,
                        assignee_id=assignee_id,
                        labels=list(AUTO_ISSUE_LABELS))))
            assert isinstance(issue, dict)
        except GitLabError as e:
            try:
                context.db.add_log(
                    task_id, "error", f"创建失败上报 issue 失败: {e}")
            except Exception:  # noqa: BLE001 日志落库失败忽略
                pass
            raise
        except Exception:  # noqa: BLE001 非 GitLab 错误同样上报失败
            try:
                context.db.add_log(
                    task_id, "error", "创建失败上报 issue 异常（非 GitLab 错误）")
            except Exception:  # noqa: BLE001
                pass
            raise
        iid = issue.get("iid") or ""
        url = issue.get("web_url") or ""
        try:
            context.db.add_log(
                task_id, "info",
                f"{DEDUP_LOG_MARK} #{iid}（{url}）")
        except Exception:  # noqa: BLE001
            pass
        logger.info("任务 %s 失败上报 issue 已创建 #%s", task_id, iid)
        return issue

    def _resolve_assignee(self, context: Any, repo: dict | None,
                          cfg) -> int | None:
        """解析负责人用户名 → GitLab 用户 id（issue #347）。

        未配置 / 解析失败（用户不存在、API 故障）返回 None（创建时
        不指定负责人，不阻塞上报）；网络类错误同样降级不阻塞。
        """
        username = (cfg.auto_issue_assignee or "").strip()
        if not username:
            return None
        try:
            uid = context._call_with_fallback(
                repo, lambda cl: cl.get_user_id_by_username(username))
            # _call_with_fallback 返回 (结果, client)；用户不存在时结果为
            # None（get_user_id_by_username 返回 None，不抛错）
            user_id = uid[0] if uid else None
            return int(user_id) if user_id else None
        except GitLabError as e:
            logger.warning("解析失败上报负责人 %s 失败，跳过指定负责人: %s",
                           username, e)
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("解析失败上报负责人 %s 异常，跳过指定负责人: %s",
                           username, str(e)[:200])
            return None


# 模块导入即注册（plugins 包导入 auto_issue 时完成登记）
register_plugin(AutoIssueNotifierPlugin())
