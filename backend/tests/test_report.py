"""结构化执行报告采集与渲染测试（issue #252）。

覆盖：git diff 采集（numstat/name-status 解析、无基线隐藏）、测试摘要
提取（pytest/jest/flutter/golang）、Markdown 表格渲染、空段落隐藏、
评论模版渲染（成功/失败、无数据不报错）。
"""

import subprocess
from pathlib import Path

from botler.report import (
    DEFAULT_COMMENT_TEMPLATE,
    DEFAULT_FAILURE_COMMENT_TEMPLATE,
    EMPTY_DIFF,
    build_diff_table,
    collect_diff_data,
    format_duration,
    format_test_summary,
    parse_test_summary,
    render_comment,
    strip_empty_sections,
)


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    """构造含 base 提交的临时 git 仓库，返回 (workdir, base_sha)。"""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    return tmp_path, sha


def _make_changes(tmp_path: Path) -> None:
    """在 base 之后追加改动：修改 a.txt、新增 new.py、删除 b.txt。"""
    with open(tmp_path / "a.txt", "a", encoding="utf-8") as f:
        f.write("a2\n")
    (tmp_path / "new.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "rm", "-q", "sub/b.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "work"], check=True)


class TestCollectDiffData:
    def test_collects_numstat_and_name_status(self, tmp_path):
        workdir, base = _git_repo(tmp_path)
        _make_changes(tmp_path)
        diff = collect_diff_data(workdir, base)
        assert diff["files"] == [
            {"path": "a.txt", "added": 1, "deleted": 0},
            {"path": "new.py", "added": 1, "deleted": 0},
            {"path": "sub/b.txt", "added": 0, "deleted": 1},
        ]
        assert diff["added"] == ["new.py"]
        assert diff["deleted"] == ["sub/b.txt"]
        assert diff["line_added"] == 2
        assert diff["line_deleted"] == 1

    def test_empty_without_base_sha(self, tmp_path):
        """无基线（任务开始前未采集到 base_sha）→ 空采集结果，不报错。"""
        workdir, _base = _git_repo(tmp_path)
        _make_changes(tmp_path)
        assert collect_diff_data(workdir, "") == EMPTY_DIFF

    def test_empty_without_workdir(self):
        assert collect_diff_data(None, "abc") == EMPTY_DIFF
        assert collect_diff_data(Path("/nonexistent-dir"), "abc") == EMPTY_DIFF

    def test_no_changes_returns_empty_files(self, tmp_path):
        workdir, base = _git_repo(tmp_path)
        diff = collect_diff_data(workdir, base)
        assert diff["files"] == []


class TestParseTestSummary:
    def test_pytest_counts(self):
        text = "tests/test_x.py ..F\n===== 1 failed, 2 passed, 1 error, 3 skipped in 2.1s ====="
        s = parse_test_summary(text)
        assert s == {"passed": 2, "failed": 1, "errors": 1, "skipped": 3}

    def test_last_run_wins_on_multiple_runs(self):
        """同一日志多轮测试：取最后一次出现的计数（最新一轮）。"""
        text = "1 passed in 1s\n===== 5 passed, 1 failed in 3s ====="
        s = parse_test_summary(text)
        assert s["passed"] == 5 and s["failed"] == 1

    def test_jest_format(self):
        text = "Tests: 10 passed, 2 failed\nTest Suites: 1 failed, 3 passed"
        s = parse_test_summary(text)
        assert s["passed"] == 10 and s["failed"] == 2

    def test_flutter_all_passed(self):
        s = parse_test_summary("00:02 +4: All tests passed!")
        assert s is not None and s.get("flutter") is True

    def test_flutter_some_failed(self):
        s = parse_test_summary("Some tests failed.")
        assert s is not None and s.get("flutter") is False

    def test_golang_ok_and_fail(self):
        text = "ok  \tgithub.com/x/y\t0.3s\n--- FAIL: TestZ (0.00s)"
        s = parse_test_summary(text)
        assert s is not None  # 至少识别到测试输出（FAIL 关键字行）

    def test_no_test_output_returns_none(self):
        assert parse_test_summary("") is None
        assert parse_test_summary("只是普通日志，没有任何测试计数") is None

    def test_format_summary(self):
        assert format_test_summary(None) == ""
        assert format_test_summary({"passed": 2, "failed": 0,
                                    "errors": 0, "skipped": 1}) == "✅ 2 passed, 1 skipped"
        assert format_test_summary({"passed": 1, "failed": 1,
                                    "errors": 0, "skipped": 0}) == "❌ 1 passed, 1 failed"
        assert format_test_summary({"flutter": True}) == "✅ 全部测试通过（flutter test）"


class TestRenderComment:
    def test_strip_empty_sections(self):
        text = "头部\n\n## 改动文件\n\n## 测试摘要\n5 passed\n\n尾部"
        assert strip_empty_sections(text) == "头部\n## 测试摘要\n5 passed\n尾部"

    def test_strip_keeps_nonempty_sections(self):
        text = "## 标题\n正文\n\n## 空节\n"
        assert strip_empty_sections(text) == "## 标题\n正文"

    def test_render_comment_substitutes(self):
        out = render_comment("## 提交\n{commit_link}", {"commit_link": "[abc](url)"})
        assert out == "## 提交\n[abc](url)"

    def test_render_comment_hides_empty_section(self):
        out = render_comment("## 改动文件\n{diff_stat}", {"diff_stat": ""})
        assert "## 改动文件" not in out
        assert out.strip() == ""

    def test_default_success_template_no_data(self):
        """无任何数据：渲染不报错，空段落全部隐藏，核心句子保留。"""
        out = render_comment(DEFAULT_COMMENT_TEMPLATE, {
            "result_summary": "", "diff_stat": "", "test_summary": "",
            "commit_link": "", "duration": "5 分 0 秒",
        })
        assert "任务已完成" in out
        assert "## 改动文件" not in out
        assert "## 测试摘要" not in out
        assert "用时：5 分 0 秒" in out

    def test_default_success_template_full_data(self):
        out = render_comment(DEFAULT_COMMENT_TEMPLATE, {
            "result_summary": "全部测试通过",
            "diff_stat": build_diff_table({
                "files": [{"path": "x.py", "added": 2, "deleted": 1}],
                "added": [], "deleted": [], "line_added": 2, "line_deleted": 1}),
            "test_summary": "✅ 2 passed",
            "commit_link": "[abc12345](https://gitlab.example.com/-/commit/abc12345)",
            "duration": "5 分 0 秒",
        })
        assert "## 改动文件" in out
        assert "| x.py | +2 | -1 |" in out
        assert "## 测试摘要" in out
        assert "✅ 2 passed" in out
        assert "[abc12345]" in out

    def test_default_failure_template(self):
        out = render_comment(DEFAULT_FAILURE_COMMENT_TEMPLATE, {
            "error_message": "pytest 失败", "diff_stat": "",
            "test_summary": "❌ 1 failed", "log_tail": "尾部日志",
        })
        assert "无法完成此 issue" in out
        assert "**原因**" not in out  # 旧硬编码文案已由模版替换
        assert "pytest 失败" in out
        assert "## 相关文件" not in out  # 无改动文件 → 段落隐藏
        assert "❌ 1 failed" in out
        assert "尾部日志" in out


class TestDiffTableAndDuration:
    def test_build_diff_table(self):
        table = build_diff_table({
            "files": [{"path": "a.py", "added": 3, "deleted": 1},
                      {"path": "b.md", "added": 0, "deleted": 2}],
            "added": ["a.py"], "deleted": ["b.md"],
            "line_added": 3, "line_deleted": 3,
        })
        assert "| a.py | +3 | -1 |" in table
        assert "| b.md | +0 | -2 |" in table
        assert "新增 3 行" in table
        assert "**新增文件**" in table
        assert "**删除文件**" in table

    def test_build_diff_table_empty(self):
        assert build_diff_table(None) == ""
        assert build_diff_table(EMPTY_DIFF) == ""

    def test_format_duration(self):
        assert format_duration(30) == "30 秒"
        assert format_duration(125) == "2 分 5 秒"
        assert format_duration(3661) == "1 小时 1 分"
