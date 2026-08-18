"""提示词渲染 / 环境注入 / 输出脱敏（issue #192 拆分）。

从原 executor.py 拆出的提示词相关职责：模版渲染、任务环境变量注入
（GITLAB_TOKEN 等）、gitconfig/输出脱敏、转义解码与展示行重排。
"""

from __future__ import annotations

import json
import os
import re

from ..config import DEFAULT_RESUME_PROMPT
from .common import _load_json_output, _row_get

# 宽松转义解码（issue #16）：claude result 内嵌工具调用记录时，\n \" \'
# 等转义按字面量存放，直接展示可读性差。json.loads 对 \' 等 Python 风格
# 转义会抛 Invalid \escape，这里用正则宽松解码常见转义，其余 \X 保留原样。
_ESCAPE_MAP = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
               "\\": "\\", "'": "'", '"': '"', "/": "/"}
_ESCAPE_RE = re.compile(r"\\([nrtbf\\'\"\/])")

PROGRESS_REPORT_INSTRUCTION = """
【进度上报约定】（中断恢复机制 issue #281）：每完成一个里程碑（定位根因 /
编写代码 / 运行测试 / 推送等），请单独输出一行固定格式进度，平台会记录并在
中断恢复时生成确定性交接单，避免你中断恢复后反复检查、重复实现：
[PROGRESS] step=<序号> status=<done|failed|pending> desc=\"<本步做了什么>\" evidence=\"<验证命令与结果摘要>\"
示例：[PROGRESS] step=2 status=done desc=\"编写修复代码\" evidence=\"pytest tests/test_x.py -q 通过\"
"""

def _strip_credential_sections(text: str) -> str:
    """从 gitconfig 文本中剥离 [credential] section（含子键，如 [credential "https://x"]）。

    返回去除 credential 配置后的文本；无 credential section 时原样返回。
    用于生成净化版全局 gitconfig（见 ClaudeExecutor._git_global_config）。
    """
    out: list[str] = []
    skip = False
    for line in text.splitlines():
        if line.startswith("["):
            section = line.strip("[]").split()[0].split('"')[0].strip()
            skip = section == "credential"
        if not skip:
            out.append(line)
    return "\n".join(out)
def _format_struct(value, depth: int = 0) -> str:
    """把解码后的 JSON 结构递归展开为可读文本（字符串值不再二次转义）。"""
    pad = "  " * depth
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for k, v in value.items():
            lines.append(f"{pad}  {k}: {_format_struct(v, depth + 1)}")
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for v in value:
            lines.append(f"{pad}  {_format_struct(v, depth + 1)}")
        lines.append(pad + "]")
        return "\n".join(lines)
    if isinstance(value, str):
        return _decode_escapes(value, depth + 1)
    return json.dumps(value, ensure_ascii=False)
def _decode_escapes(text: str, depth: int = 0) -> str:
    """递归解码 result 中嵌套序列化的转义文本（issue #16）。

    外层 json.loads 已解码一次 JSON 转义；result 内嵌的工具调用记录是
    再次序列化的 JSON 文本（\\n \\" 等按字面量存放）。这里逐层解码：
    先试严格 json.loads（标准 JSON → 结构展开），失败则宽松解码一层
    常见转义后继续递归。普通可读文本（无转义）原样返回。
    """
    if depth > 4 or not text:
        return text
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        decoded = None
    if isinstance(decoded, str):
        return _decode_escapes(decoded, depth + 1)
    if isinstance(decoded, (dict, list)):
        return _format_struct(decoded, depth + 1)
    # 严格解码失败（含 \' 等非标准转义）→ 宽松解码一层后继续
    unescaped = _ESCAPE_RE.sub(lambda m: _ESCAPE_MAP[m.group(1)], text)
    if unescaped == text:
        return text
    return _decode_escapes(unescaped, depth + 1)
def format_display_line(line: str) -> str:
    """把 claude 输出行重排为可读文本（issue #16）。

    JSON 行：解码 result 字段的嵌套转义（\\n → 换行等），只保留对排查
    有用的核心字段，丢弃 ttft_ms / uuid 等机器噪音；非 JSON 行原样返回。
    """
    data = _load_json_output(line)
    if data is None or not isinstance(data.get("result"), str):
        return line
    parts = []
    for key in ("type", "subtype", "session_id", "exit_code", "error"):
        if key in data:
            parts.append(f"{key}: {json.dumps(data[key], ensure_ascii=False)}")
    parts.append("result:\n" + _decode_escapes(data["result"]))
    return "\n".join(parts)


# ---- 实时查看任务执行（issue #20）----

_TRANSCRIPT_MAX_MESSAGES = 500
_TRANSCRIPT_MAX_TEXT = 5000


class PromptMixin:
    """提示词构建与任务环境注入（依赖 ClaudeExecutor 实例状态）。"""

    def _build_prompt(self, repo: dict, issue: dict) -> str:
        template = self.renderer.resolve_template(repo)
        variables = self.renderer.build_variables(
            repo["name"], issue, repo_url=_row_get(repo, "url") or "")
        prompt = self.renderer.render(template, variables)
        if self._repo_workdir(repo) in self._pull_conflict_workdirs:
            prompt += self._conflict_handoff_instructions()
        return prompt
    def _task_gitlab_token(self, repo: dict) -> str | None:
        """任务会话 GITLAB_TOKEN 注入源：仓库 remote url 内嵌 token（issue #79）。

        与平台 _call_with_fallback 的 per-repo 兜底（issue #65）对齐：
        全局 bot token 失效后 Claude 会话内的 API（读 issue/写结果评论）
        401 失败，改用 remote 内嵌 token（与仓库绑定的凭据通常更新鲜）。
        解析失败 / 无 token 时返回 None（调用方回退全局 token）。
        """
        # 动态取包级符号：测试 monkeypatch botler.executor.list_local_remotes
        # 等模块属性才能生效（与 process.py 的 DshRunner/HermesSdkRunner 同机制）
        from botler.executor import (
            NoGitRemoteError, list_local_remotes, parse_remote_url,
        )
        try:
            remotes = list_local_remotes(str(self._repo_workdir(repo)))
        except NoGitRemoteError:
            return None
        remote_name = _row_get(repo, "remote_name") or "origin"
        match = next((r for r in remotes if r["name"] == remote_name), None)
        if match is None:
            return None
        return parse_remote_url(match["url"])["token"]
    def _build_env(self, repo: dict, issue: dict) -> dict:
        cfg = self.config.get()
        env = self._clean_process_env()
        # 会话 GITLAB_TOKEN 注入（issue #130 调整）：agent 会话绝不注入
        # owner token（owner token 只允许在概览页 issue 编辑操作时由平台
        # 使用，见 api/issues.py；agent 无论如何都不能使用 owner token）。
        # 优先级：remote url 内嵌 token（仓库自己的认证 token）> 全局
        # bot token。issue #79：全局 bot token 失效后 Claude 侧 API
        # （写结果评论等）401 失败，remote 内嵌 token 与平台侧 per-repo
        # 兜底对齐。git 推送凭据不走 GITLAB_TOKEN（走 GIT_ASKPASS 的
        # bot token）。
        env["GITLAB_TOKEN"] = (self._task_gitlab_token(repo)
                               or cfg.gitlab_token)
        env["GITLAB_URL"] = cfg.gitlab_url
        env["PROJECT_ID"] = str(issue["project_id"])
        env["ISSUE_IID"] = str(issue["iid"])
        # git 凭据统一走 GIT_ASKPASS（bot token）：claude 内部 git push/fetch
        # 同样受全局 credential store 中失效 job token 污染（issue #16 推送时
        # 已遇 403），此处一并净化，保证 push 凭据与 API 一致
        askpass = self._askpass_script(repo["name"])
        env["GIT_ASKPASS"] = str(askpass)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_GLOBAL"] = str(self._git_global_config())
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        return env

    # ---- 会话断点续跑（issue #8）----
    def _resume_prompt(self, repo: dict, issue: dict,
                      task_id: int | None = None) -> str:
        """恢复执行引导语：确定性交接单渲染（issue #281 §4.4）。

        模版优先取 config 的 templates.resume（issue #116 起用户可编辑，
        与全局默认模版同机制）；未配置/清空时回退内置默认。占位符与
        全局模版共用 build_variables（claude/hermes/dsh 三引擎统一入口），
        另注入 {progress_summary}：有 task_id 且账本非空时渲染「已完成
        步骤 + 证据 / 下一步」确定性交接单；账本为空（确属首次/状态
        丢失）如实说明「无进度记录」，不再声称「对话与改动已保留」。
        """
        template = self.config.get().resume_template or DEFAULT_RESUME_PROMPT
        variables = self.renderer.build_variables(
            repo["name"], issue, repo_url=_row_get(repo, "url") or "")
        variables["progress_summary"] = self._render_progress_handoff(task_id)
        prompt = self.renderer.render(template, variables)
        if self._repo_workdir(repo) in self._pull_conflict_workdirs:
            prompt += self._conflict_handoff_instructions()
        return prompt
