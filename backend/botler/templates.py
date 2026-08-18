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
    "log_tail": "日志尾部（仅失败评论模版）"
}


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
        return {
            "repo_name": repo_name,
            "issue_title": issue.get("title", ""),
            "issue_body": issue.get("description") or "",
            "issue_url": issue.get("web_url", ""),
            "gitlab_url": gitlab_url,
            "gitlab_host": gitlab_url.split("://", 1)[-1],
            "project_id": str(issue["project_id"]),
            "issue_iid": str(issue["iid"]),
            "project_path": project_path,
            "project_path_encoded": quote(project_path, safe=""),
        }

    def resolve_template(self, repo: dict) -> str:
        """取仓库模版，无则用全局默认。repo 为 database 返回的 Row。"""
        if repo["prompt_template"]:
            return repo["prompt_template"]
        return self.config.get().default_template
