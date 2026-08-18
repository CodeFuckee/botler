"""提示词模版渲染。

全局默认模版 + 仓库级覆盖。占位符用逐项 str.replace 替换，
避免 format() 与 issue 正文中的花括号冲突。
"""

from __future__ import annotations

from urllib.parse import quote

from .config import ConfigManager

PLACEHOLDERS = {
    "repo_name": "仓库名",
    "issue_title": "issue 标题",
    "issue_body": "issue 正文",
    "issue_url": "issue 链接",
    "gitlab_url": "GitLab 地址",
    "project_id": "GitLab 项目 ID",
    "issue_iid": "issue 编号",
    "project_path": "仓库路径（group/repo，如 chenkaidi/botler）",
    "project_path_encoded": "仓库路径 URL 编码（chenkaidi%2Fbotler）",
    "gitlab_host": "GitLab 主机 host:port（无 scheme）",
    # issue #223：URL 编码占位符——issue 标题/描述常含 `#`、`%`、反引号、
    # 换行等特殊字符，直接拼进 prompt 可能破坏 Markdown/模板结构或被模型
    # 误解（标题 255 截断 issue #186 已证明标题内容边界问题真实存在）。
    # 编码后特殊字符不再原样进入 prompt（防注入/防模板破坏），模型需要时
    # 可 URL 解码还原；issue_body_urlenc 为完整正文不截断（安全形式）。
    "issue_title_urlenc": "issue 标题 URL 编码（特殊字符安全，防模板结构破坏）",
    "issue_body_urlenc": "issue 正文 URL 编码（完整不截断，防注入/防模板结构破坏）",
    "progress_summary": "中断恢复进度交接单（issue #281：任务进度账本渲染的确定性已完成/下一步摘要，仅恢复引导语模版使用）",
    # 结果评论模版占位符（issue #252）：结构化执行报告评论（改动文件/diff
    # 统计/测试结果），仅在 templates.comment 评论模版中生效，提示词模版
    # 不含这些变量（渲染时保持字面量，不影响 agent 提示词）。
    "result_summary": "任务结果摘要（执行输出 result/final_response，仅评论模版）",
    "diff_stat": "改动文件表格（文件/增/删行数 + 新增/删除文件列表，相对任务开始前 main 基线，仅评论模版）",
    "test_summary": "测试结果摘要（从执行日志提取的 pass/fail/error/skipped 计数，仅评论模版）",
    "commit_link": "提交链接（Markdown 格式，仅评论模版）",
    "commit_sha": "提交短 sha（8 位，仅评论模版）",
    "duration": "任务用时（系统接收到 issue → 收尾，仅评论模版）",
    "error_message": "失败原因（仅失败评论模版）",
    "log_tail": "日志尾部（仅失败评论模版）",
    "failure_category": "失败分类值（env/engine/unsolvable/unknown，仅失败评论模版，issue #274）",
    "failure_category_badge": "失败分类徽章文案（如「环境类（env）」，仅失败评论模版）",
    "failure_advice": "失败分类处理建议（如「环境类：请检查仓库 token 与网络配置后点重试」，仅失败评论模版）"
}

# issue #223：正文注入控制标记文案。截断标记在正文超长（超过
# templates.body_max_chars）时追加在截断后的正文末尾，标注总长度与
# 完整 issue 链接（agent 知道描述被截断，需要时经 issue_url 查看全文）。
BODY_TRUNCATED_MARKER = "\n\n[描述已截断，共 {total} 字，完整见 {url}]"
# 防注入提示：templates.raw_body_in_prompt=false 时原始正文不进 prompt，
# 仅保留指向 issue 的链接（高风险场景可只给 URL，防 prompt injection）。
BODY_NOT_INJECTED_NOTICE = ("[原始描述未注入（防注入开关已开启），"
                            "完整正文见 {url}]")


def project_path_from_url(url: str) -> str:
    """从仓库 URL 提取 group/repo 路径（如 https://host/chenkaidi/botler.git
    → chenkaidi/botler）。解析失败时返回原字符串（占位符保持可见，便于排查）。
    """
    try:
        path = url.split("://", 1)[-1]
        path = path.split("/", 1)[-1]
        if path.endswith(".git"):
            path = path[:-4]
        return path.rstrip("/")
    except (ValueError, TypeError):
        return url


class TemplateRenderer:
    def __init__(self, config: ConfigManager):
        self.config = config

    def render(self, template: str, variables: dict[str, str]) -> str:
        prompt = template
        for key, value in variables.items():
            prompt = prompt.replace("{" + key + "}", value)
        return prompt

    def build_variables(self, repo_name: str, issue: dict,
                        repo_url: str = "") -> dict[str, str]:
        cfg = self.config.get()
        # project_path：从仓库 URL 提取（issue-agent 参数化模板用），
        # 无 URL（如恢复执行路径）时兜底用仓库名
        project_path = project_path_from_url(repo_url) if repo_url else repo_name
        gitlab_url = cfg.gitlab_url.rstrip("/")
        issue_url = issue.get("web_url", "")
        issue_title = issue.get("title", "")
        issue_body = issue.get("description") or ""
        if cfg.raw_body_in_prompt:
            body = issue_body
            # issue #223：长正文注入标注长度与截断标记（agent 知道描述被
            # 截断；body_max_chars=0 = 不截断）
            if cfg.body_max_chars > 0 and len(body) > cfg.body_max_chars:
                body = (body[:cfg.body_max_chars]
                        + BODY_TRUNCATED_MARKER.format(
                            total=len(body), url=issue_url))
        else:
            # 防 prompt injection（issue #223）：原始描述不进 prompt，
            # 高风险场景只给 URL；需要正文时用 {issue_body_urlenc} 或
            # 经 issue_url 查看
            body = BODY_NOT_INJECTED_NOTICE.format(url=issue_url)
        return {
            "repo_name": repo_name,
            "issue_title": issue_title,
            "issue_body": body,
            "issue_url": issue_url,
            "gitlab_url": gitlab_url,
            "gitlab_host": gitlab_url.split("://", 1)[-1],
            "project_id": str(issue["project_id"]),
            "issue_iid": str(issue["iid"]),
            "project_path": project_path,
            "project_path_encoded": quote(project_path, safe=""),
            "issue_title_urlenc": quote(issue_title, safe=""),
            "issue_body_urlenc": quote(issue_body, safe=""),
        }

    def resolve_template(self, repo: dict) -> str:
        """取仓库模版，无则用全局默认。repo 为 database 返回的 Row。"""
        if repo["prompt_template"]:
            return repo["prompt_template"]
        return self.config.get().default_template
