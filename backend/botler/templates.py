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
