"""提示词模版渲染。

全局默认模版 + 仓库级覆盖。占位符用逐项 str.replace 替换，
避免 format() 与 issue 正文中的花括号冲突。
"""

from __future__ import annotations

from .config import ConfigManager

PLACEHOLDERS = {
    "repo_name": "仓库名",
    "issue_title": "issue 标题",
    "issue_body": "issue 正文",
    "issue_url": "issue 链接",
    "gitlab_url": "GitLab 地址",
    "project_id": "GitLab 项目 ID",
    "issue_iid": "issue 编号",
}


class TemplateRenderer:
    def __init__(self, config: ConfigManager):
        self.config = config

    def render(self, template: str, variables: dict[str, str]) -> str:
        prompt = template
        for key, value in variables.items():
            prompt = prompt.replace("{" + key + "}", value)
        return prompt

    def build_variables(self, repo_name: str, issue: dict) -> dict[str, str]:
        cfg = self.config.get()
        return {
            "repo_name": repo_name,
            "issue_title": issue.get("title", ""),
            "issue_body": issue.get("description") or "",
            "issue_url": issue.get("web_url", ""),
            "gitlab_url": cfg.gitlab_url,
            "project_id": str(issue["project_id"]),
            "issue_iid": str(issue["iid"]),
        }

    def resolve_template(self, repo: dict) -> str:
        """取仓库模版，无则用全局默认。repo 为 database 返回的 Row。"""
        if repo["prompt_template"]:
            return repo["prompt_template"]
        return self.config.get().default_template
