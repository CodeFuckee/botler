"""结构化执行报告采集与渲染（issue #252）。

任务收尾时执行器采集任务改动与测试结果，按可配置评论模版渲染结构化
Markdown 评论（改动文件表格 / 测试摘要 / 提交链接 / 用时），替代原先
模板决定的通用收尾语——用户无需跳转任务详情页即可在 issue 评论里看到
「改了什么、改了哪些文件、测试跑没跑过」。

职责划分：
- 采集（collect_diff_data / parse_test_summary）：纯函数 + subprocess，
  与 GitLab API 无关，可单测；
- 渲染（render_comment / strip_empty_sections / build_diff_table /
  format_test_summary）：纯字符串处理，可单测；
- 默认模版（DEFAULT_COMMENT_TEMPLATE / DEFAULT_FAILURE_COMMENT_TEMPLATE）：
  可在设置页 templates.comment 覆盖（复用模板系统的占位符机制，
  新增 {diff_stat} / {test_summary} 等占位符）。

验收标准：成功任务评论含改动文件与测试摘要；失败任务评论含失败原因与
相关文件；无 diff 数据时对应段落隐藏不报错；有采集与渲染单元测试。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# ---- 默认评论模版 ----
# 占位符见 templates.PLACEHOLDERS（新增 diff_stat/test_summary/commit_link
# 等）；渲染后空段落会被 strip_empty_sections 移除（验收标准 3）。
DEFAULT_COMMENT_TEMPLATE = """🤖 Botler 自动回复：任务已完成。

## 结果摘要
{result_summary}

## 改动文件
{diff_stat}

## 测试摘要
{test_summary}

## 提交
{commit_link}

用时：{duration}

开发已完成，请确认后手动关闭本 issue（平台已打 bot-done 标签）。"""

DEFAULT_FAILURE_COMMENT_TEMPLATE = """🤖 Botler 自动回复：无法完成此 issue。

## 失败原因
{error_message}

## 相关文件
{diff_stat}

## 测试摘要
{test_summary}

## 日志尾部
{log_tail}

开发未能完成，请人工介入处理。"""

# 无 diff 数据时的空采集结果（调用方据此隐藏段落，不报错）
EMPTY_DIFF = {"files": [], "added": [], "deleted": [], "line_added": 0,
              "line_deleted": 0}


# ---- 采集：git diff ----

def _run_git(workdir: Path | None, args: list[str],
             env: dict | None = None, timeout: int = 30) -> str:
    """在工作区执行 git 命令并返回 stdout；失败/无工作区返回空串。

    采集为尽力而为：任何失败返回空串，由渲染层隐藏对应段落，绝不阻塞
    任务收尾（与打标签/写评论一致的容错策略）。
    """
    if workdir is None or not Path(workdir).exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(workdir), *args],
            env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def collect_diff_data(workdir: Path | None, base_sha: str = "",
                      env: dict | None = None) -> dict:
    """采集任务改动（相对任务开始前 main 基线的 diff，issue #252）。

    base_sha 为任务首次执行开始时工作区 HEAD（prepare_workspace 已把
    工作区重置到远端默认主分支最新提交，见 executor._capture_base_sha）。
    无基线或无工作区 → 返回 EMPTY_DIFF（调用方隐藏改动段落，不报错）。

    返回 dict：
    - files: [{"path", "added", "deleted"}]，按 git diff --numstat 解析；
    - added / deleted: 新增/删除文件路径列表（git diff --name-status）；
    - line_added / line_deleted: 行数合计。
    """
    if not base_sha:
        return dict(EMPTY_DIFF)
    numstat = _run_git(workdir, ["diff", "--numstat", base_sha, "HEAD"], env=env)
    files: list[dict] = []
    line_added = line_deleted = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_s, deleted_s, path = parts[0], parts[1], parts[2]
        if added_s == "-" or deleted_s == "-":
            continue  # 二进制文件：行数不可统计，跳过统计行（路径仍列在文件表）
        try:
            added, deleted = int(added_s), int(deleted_s)
        except ValueError:
            continue
        # numstat 重命名行形如 "old => new"，取新路径
        if " => " in path and path.count(" => ") == 1:
            path = path.split(" => ", 1)[1]
        files.append({"path": path, "added": added, "deleted": deleted})
        line_added += added
        line_deleted += deleted

    statuses = _run_git(workdir, ["diff", "--name-status", base_sha, "HEAD"],
                        env=env)
    added: list[str] = []
    deleted: list[str] = []
    for line in statuses.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        status = parts[0]
        path = parts[1] if len(parts) > 1 else ""
        # 重命名/复制行形如 "R100\told\tnew"，取新路径为新增、旧路径为删除
        if len(parts) >= 3:
            old_path, new_path = parts[1], parts[2]
            if status.startswith(("R", "C")):
                deleted.append(old_path)
                added.append(new_path)
                continue
            path = new_path
        if status.startswith("A"):
            added.append(path)
        elif status.startswith("D"):
            deleted.append(path)
    return {"files": files, "added": added, "deleted": deleted,
            "line_added": line_added, "line_deleted": line_deleted}


# ---- 采集：测试摘要 ----

# pytest 摘要行：如 "12 passed, 2 failed, 1 error, 3 skipped in 5.2s"
_TEST_COUNT_RE = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "errors": re.compile(r"(\d+)\s+error"),
    "skipped": re.compile(r"(\d+)\s+skipped"),
}
# jest 摘要行：如 "Tests: 10 passed, 2 failed"
_JEST_RE = re.compile(r"Tests:\s+(\d+)\s+passed,\s+(\d+)\s+failed")


def parse_test_summary(text: str) -> dict | None:
    """从执行输出日志提取测试结果摘要（pass/fail/error/skipped 计数）。

    支持 pytest（"N passed"）、jest（"Tests: N passed, M failed"）、
    flutter（"All tests passed!" / "Some tests failed"）与 golang
    （"ok"/"FAIL"）等常见输出；同一条日志多轮运行时取最后一次出现的
    计数（最新一轮结果）。提取不到任何计数返回 None（渲染层隐藏段落）。
    """
    if not text:
        return None
    counts: dict[str, int | None] = {"passed": None, "failed": None,
                                     "errors": None, "skipped": None}
    last_jest: tuple[int, int] | None = None
    for line in text.splitlines():
        m = _JEST_RE.search(line)
        if m:
            # jest 的 "Tests:" 行为权威计数，后续 "Test Suites" 行
            # （"1 failed, 3 passed"）不覆盖其值
            last_jest = (int(m.group(1)), int(m.group(2)))
            continue
        for key, pattern in _TEST_COUNT_RE.items():
            m = pattern.search(line)
            if m:
                counts[key] = int(m.group(1))
    if last_jest is not None:
        return {"passed": last_jest[0], "failed": last_jest[1],
                "errors": None, "skipped": None}
    if all(v is None for v in counts.values()):
        if re.search(r"All tests? passed", text, re.IGNORECASE):
            return {"passed": None, "failed": 0, "errors": 0, "skipped": None,
                    "flutter": True}
        if re.search(r"Some tests? failed", text, re.IGNORECASE):
            return {"passed": None, "failed": None, "errors": None,
                    "skipped": None, "flutter": False}
        if re.search(r"^---\s*FAIL|^FAIL\b", text, re.MULTILINE):
            return {"passed": None, "failed": None, "errors": None,
                    "skipped": None, "golang": True, "ok": False}
        if re.search(r"^ok\s", text, re.MULTILINE):
            return {"passed": None, "failed": 0, "errors": 0,
                    "skipped": None, "golang": True, "ok": True}
        return None
    return counts


def format_test_summary(summary: dict | None) -> str:
    """把测试摘要 dict 渲染为人类可读的一行（无数据返回空串）。"""
    if not summary:
        return ""
    if summary.get("flutter") is True:
        return "✅ 全部测试通过（flutter test）"
    if summary.get("flutter") is False:
        return "❌ 部分测试失败（flutter test）"
    if summary.get("golang") is True:
        return "✅ go test 全部通过" if summary.get("ok") else "❌ go test 存在失败用例"
    parts: list[str] = []
    for key, label in (("passed", "passed"), ("failed", "failed"),
                       ("errors", "errors"), ("skipped", "skipped")):
        value = summary.get(key)
        if isinstance(value, int) and value > 0:
            parts.append(f"{value} {label}")
    if not parts:
        return ""
    failed = summary.get("failed") or 0
    errors = summary.get("errors") or 0
    emoji = "✅" if (failed == 0 and errors == 0) else "❌"
    return f"{emoji} {', '.join(parts)}"


# ---- 渲染 ----

def build_diff_table(diff: dict | None) -> str:
    """把 diff 采集结果渲染为 Markdown 改动文件表格（文件/增/删）。

    无改动文件 → 返回空串（段落由 strip_empty_sections 隐藏）。
    """
    if not diff:
        return ""
    files = diff.get("files") or []
    if not files:
        return ""
    lines = ["| 文件 | 增 | 删 |", "| --- | ---: | ---: |"]
    for f in files:
        path = (f.get("path") or "").replace("|", "\\|")
        lines.append(f"| {path} | +{f.get('added', 0)} | -{f.get('deleted', 0)} |")
    summary = (f"共 {len(files)} 个文件，新增 {diff.get('line_added', 0)} 行，"
               f"删除 {diff.get('line_deleted', 0)} 行")
    lines.append("")
    lines.append(summary)
    added = diff.get("added") or []
    deleted = diff.get("deleted") or []
    if added:
        lines.append("")
        lines.append("**新增文件**：")
        lines.extend(f"- {p}" for p in added)
    if deleted:
        lines.append("")
        lines.append("**删除文件**：")
        lines.extend(f"- {p}" for p in deleted)
    return "\n".join(lines)


def format_duration(seconds: int) -> str:
    """把秒数格式化为中文用时（<60s 显示秒，否则 分/时）。"""
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def render_comment(template: str, variables: dict[str, str]) -> str:
    """按占位符替换渲染评论模版，并移除渲染后为空的 Markdown 段落。

    占位符逐项 str.replace（与 templates.TemplateRenderer 同机制，避免
    format() 与正文花括号冲突）；空段落隐藏（验收标准 3：无 diff 数据
    时对应段落隐藏不报错）。
    """
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace("{" + key + "}", value if value is not None else "")
    return strip_empty_sections(rendered)


def strip_empty_sections(text: str) -> str:
    """移除渲染后内容为空的 Markdown 小节（'# ' 或 '## ' 标题 + 空正文）。

    例：模版中 "## 改动文件\\n{diff_stat}" 在 diff_stat 为空时，该小节
    正文为空 → 标题行一并移除；非空小节原样保留。多空行压缩为单空行。
    """
    out: list[str] = []
    pending_header: str | None = None
    pending_has_content = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("## ") or stripped.startswith("# "):
            # 新标题到来：上一小节有正文才保留，否则（空段落）整体丢弃
            if pending_header is not None and pending_has_content:
                out.append(pending_header)
            pending_header = raw
            pending_has_content = False
        elif stripped:
            # 非空内容行：挂起标题有正文 → 先落标题再落内容
            if pending_header is not None:
                out.append(pending_header)
                pending_header = None
            out.append(raw)
            pending_has_content = True
        # 空行：不结束小节（标题后允许空行再跟正文）
    if pending_header is not None and pending_has_content:
        out.append(pending_header)
    # 压缩连续空行，去掉首尾空行
    result: list[str] = []
    blank = False
    for line in out:
        if not line.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        result.append(line)
    while result and not result[-1].strip():
        result.pop()
    while result and not result[0].strip():
        result.pop(0)
    return "\n".join(result)
