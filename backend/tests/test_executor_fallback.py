"""执行引擎健康探测与自动降级重试测试（issue #236）。

覆盖：任务开始前探测不可用 → 立即降级备用引擎并注明原因；连续 N 次
「引擎类」失败 → 降级备用引擎；任务级失败不降级；未配置备用引擎保持
旧行为；全部备用引擎耗尽后保持当前引擎；探测全失败仍按主引擎执行；
引擎恢复后下一任务自动回到主引擎；降级评论每任务只发一次。
"""

import json
from types import SimpleNamespace

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.templates import TemplateRenderer

# 主引擎 = claude，备用 = [dsh, hermes]
CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker:
  engine: claude
  fallback_engines: ["dsh", "hermes"]
  fallback_after_failures: 2
claude: {}
templates: {}
repos: []
"""


@pytest.fixture
def executor(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "test-token", verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


def _mk_repo(db) -> int:
    db.upsert_repo(42, "demo", "https://gitlab.example.com/group/demo.git")
    return db.get_repo_by_project_id(42)["id"]


def _mk_task(db, repo_id: int, issue_iid: int = 1) -> int:
    return db.create_task(repo_id, 42, issue_iid, "失败任务")


DSH_SUCCESS = json.dumps({"final_response": "已修复并推送",
                          "finish_reason": "completed",
                          "session_id": "dsh-sess-1"}, ensure_ascii=False)
CLAUDE_SUCCESS = json.dumps({"result": "开发完成，已推送代码"},
                            ensure_ascii=False)
# 引擎类失败特征（命中 failure_classify engine 分类）
ENGINE_FAIL_OUTPUT = "Error: command not found: claude"
# 任务级失败特征（未命中 engine 分类）
TASK_FAIL_OUTPUT = "处理失败: 代码逻辑写错了，需要调整实现"


def _install(executor, monkeypatch, tmp_path, run_once, *, probe_map=None):
    """装配 run_task 依赖：GitLab 桩 + _run_once 录制 + 探测桩。

    probe_map：{engine: status}，None = 全部 ok。返回 (calls, engines_used)。
    calls 记录 add_comment/add_labels 调用；engines_used 记录 _run_once
    实际收到的引擎（第 6 个位置参数）。
    """
    calls = []
    engines_used = []
    executor.gitlab = SimpleNamespace(
        get_issue=lambda pid, iid: {"state": "opened"},
        add_comment=lambda *a, **k: calls.append(("comment", a)),
        add_labels=lambda *a, **k: calls.append(("labels", a)),
        find_commit_for_issue=lambda pid, iid: None,
        last_note_author_id=lambda pid, iid: None,
    )

    def fake_run_once(*a):
        engines_used.append(a[5] if len(a) > 5 else "?")
        return run_once(*a)

    monkeypatch.setattr(executor, "_run_once", fake_run_once)
    monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
    monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")

    if probe_map is None:
        monkeypatch.setattr(
            "botler.engine_health.probe_engine",
            lambda engine, cfg: {"status": "ok", "detail": f"{engine} 正常"})
    else:
        def fake_probe(engine, cfg):
            status = probe_map.get(engine, "ok")
            return {"status": status, "detail": f"probe-{engine}-{status}"}
        monkeypatch.setattr("botler.engine_health.probe_engine", fake_probe)
    return calls, engines_used


class TestProbeDegrade:
    def test_probe_fail_degrades_to_fallback_and_succeeds(self, executor, monkeypatch, tmp_path):
        """主引擎探测不可用 → 任务开始前立即降级 dsh，且结果正确、原因落库。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls, engines = _install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (0, DSH_SUCCESS),
            probe_map={"claude": "fail", "dsh": "ok", "hermes": "ok"})

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert engines == ["dsh"], f"应降级到 dsh 执行，实际引擎序列: {engines}"
        assert task["engine"] == "dsh"
        assert "引擎 claude 不可用" in (task["engine_fallback"] or "")
        assert "已降级 dsh 执行" in (task["engine_fallback"] or "")
        # issue 评论注明降级（验收标准 2）
        deg_comments = [a for kind, a in calls if kind == "comment"
                        and "已降级 dsh 执行" in (a[2] if len(a) > 2 else "")]
        assert len(deg_comments) == 1
        # 任务记录（日志）同步注明
        logs = db.list_logs(task_id)
        assert any("已降级 dsh 执行" in l["message"] for l in logs)

    def test_probe_fail_all_engines_runs_main(self, executor, monkeypatch, tmp_path):
        """链上全部引擎探测失败 → 保持主引擎执行（探测建议性，不直接判失败）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls, engines = _install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (1, TASK_FAIL_OUTPUT),
            probe_map={"claude": "fail", "dsh": "fail", "hermes": "fail"})

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed"
        assert engines == ["claude", "claude", "claude"]
        assert not task["engine_fallback"], "无可用备用引擎时不应落降级原因"
        # 无降级评论（未发生降级）
        assert not any("已降级" in (a[2] if len(a) > 2 else "")
                       for kind, a in calls if kind == "comment")


class TestConsecutiveFailureDegrade:
    def test_engine_failures_trigger_fallback(self, executor, monkeypatch, tmp_path):
        """连续 2 次引擎类失败 → 第 3 次尝试降级 dsh 并成功。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        # 前 2 次引擎类失败（claude），第 3 次 dsh 成功
        seq = [lambda: (1, ENGINE_FAIL_OUTPUT),
               lambda: (1, ENGINE_FAIL_OUTPUT),
               lambda: (0, DSH_SUCCESS)]
        state = {"i": 0}
        def run_once(*a):
            result = seq[state["i"]]()
            state["i"] += 1
            return result
        calls, engines = _install(executor, monkeypatch, tmp_path, run_once)

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "succeeded"
        assert engines == ["claude", "claude", "dsh"]
        assert "连续 2 次引擎类失败" in (task["engine_fallback"] or "")
        assert "已降级 dsh 执行" in (task["engine_fallback"] or "")
        deg_comments = [a for kind, a in calls if kind == "comment"
                        and "已降级" in (a[2] if len(a) > 2 else "")]
        assert len(deg_comments) == 1

    def test_task_level_failure_does_not_degrade(self, executor, monkeypatch, tmp_path):
        """任务级失败（代码改不对）不累计、不降级——换引擎重试无意义。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls, engines = _install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (1, TASK_FAIL_OUTPUT))

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed"
        assert engines == ["claude", "claude", "claude"]
        assert not task["engine_fallback"]
        assert not any("已降级" in (a[2] if len(a) > 2 else "")
                       for kind, a in calls if kind == "comment")

    def test_all_fallbacks_exhausted(self, executor, monkeypatch, tmp_path):
        """备用链 [dsh, hermes] 全部耗尽 → 保持最后引擎，重试耗尽失败。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        # fallback_after_failures=1：每次引擎类失败立即降级到下一引擎
        executor.config.get().fallback_after_failures = 1
        calls, engines = _install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (1, ENGINE_FAIL_OUTPUT))

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed"
        assert engines == ["claude", "dsh", "hermes"], \
            f"应依次降级 claude→dsh→hermes，实际: {engines}"
        # 最后一次降级原因落库（dsh → hermes）
        assert "已降级 hermes 执行" in (task["engine_fallback"] or "")
        # 降级评论只发一次（每任务最多一条，不刷屏）
        deg_comments = [a for kind, a in calls if kind == "comment"
                        and "已降级" in (a[2] if len(a) > 2 else "")]
        assert len(deg_comments) == 1
        # 最终失败任务失败评论 + 处理中评论
        assert any("重试耗尽" in (a[2] if len(a) > 2 else "")
                   for kind, a in calls if kind == "comment")

    def test_no_fallback_configured_keeps_old_behavior(self, executor, monkeypatch, tmp_path):
        """未配置 fallback_engines → 与旧版一致：同引擎重试 max_retries 次。"""
        executor.config.get().fallback_engines = []
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        calls, engines = _install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (1, ENGINE_FAIL_OUTPUT))

        executor.run_task(task_id)

        task = db.get_task(task_id)
        assert task["status"] == "failed"
        assert engines == ["claude", "claude", "claude"]
        assert "重试耗尽（2 次）后仍失败" in task["error_message"]

    def test_engine_recovers_next_task(self, executor, monkeypatch, tmp_path):
        """引擎恢复后自动回到主引擎：每任务开始前重新探测（跨任务不粘滞）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_a = _mk_task(db, repo_id, 1)
        task_b = _mk_task(db, repo_id, 2)

        # 探测结果可控：先 claude 不可用，之后恢复正常
        probe_state = {"claude_broken": True}
        def fake_probe(engine, cfg):
            if engine == "claude" and probe_state["claude_broken"]:
                return {"status": "fail", "detail": "claude 挂了"}
            return {"status": "ok", "detail": f"{engine} 正常"}
        monkeypatch.setattr("botler.engine_health.probe_engine", fake_probe)

        engines_used = []
        monkeypatch.setattr(
            executor, "_run_once",
            lambda *a: engines_used.append(a[5])
            or (0, DSH_SUCCESS if a[5] == "dsh" else CLAUDE_SUCCESS))
        monkeypatch.setattr("botler.executor.time.sleep", lambda s: None)
        monkeypatch.setattr(executor, "_log_file", lambda tid: tmp_path / f"task_{tid}.log")
        executor.gitlab = SimpleNamespace(
            get_issue=lambda pid, iid: {"state": "opened"},
            add_comment=lambda *a, **k: None,
            add_labels=lambda *a, **k: None,
            find_commit_for_issue=lambda pid, iid: None,
            last_note_author_id=lambda pid, iid: None,
        )

        executor.run_task(task_a)
        assert engines_used == ["dsh"], "任务 A 主引擎不可用 → dsh"

        probe_state["claude_broken"] = False  # 引擎恢复
        executor.run_task(task_b)
        assert engines_used == ["dsh", "claude"], "任务 B 主引擎已恢复 → 回到 claude"
        assert db.get_task(task_b)["engine"] == "claude"
        assert not db.get_task(task_b)["engine_fallback"]


class TestEngineChain:
    def test_chain_filters_and_dedupes(self, executor):
        """引擎链：去重、剔除主引擎、剔除未注册引擎、小写归一。"""
        cfg = executor.config.get()
        cfg.fallback_engines = ["DSH", "claude", "hermes", "not-a-engine", "dsh"]
        assert executor._engine_chain(cfg) == ["claude", "dsh", "hermes"]

    def test_chain_empty_by_default(self, executor):
        """未配置备用引擎 → 链只有主引擎。"""
        cfg = executor.config.get()
        cfg.fallback_engines = []
        assert executor._engine_chain(cfg) == ["claude"]

    def test_failed_attempts_record_engine(self, executor, monkeypatch, tmp_path):
        """error_detail 每次尝试记录实际引擎（任务详情可追溯）。"""
        db = executor.db
        repo_id = _mk_repo(db)
        task_id = _mk_task(db, repo_id)
        executor.config.get().fallback_after_failures = 1
        calls, engines = _install(
            executor, monkeypatch, tmp_path,
            run_once=lambda *a: (1, ENGINE_FAIL_OUTPUT))

        executor.run_task(task_id)

        task = db.get_task(task_id)
        detail = json.loads(task["error_detail"])
        assert [a.get("engine") for a in detail["attempts"]] == \
            ["claude", "dsh", "hermes"]
