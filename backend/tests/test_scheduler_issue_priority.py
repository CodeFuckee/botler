"""调度器 issue 标签优先级测试（issue #76，方案 C）。

需求：同仓库队列内按「issue 标签权重」排序派发，默认 bug 最优先；
配置项 worker.issue_priority 可自定义标签顺序（设置页可修改）。
排序键：(标签权重, issue 创建时间, task_id)，权重 = 任务 issue_labels
在配置列表中首个命中的索引，未命中任何配置标签（或无标签）排最后；
同权重按 issue 创建时间升序（issue #234，创建时间缺失时按 issue
更新时间、再按任务创建时间兜底）。

此前同仓库队列纯 FIFO（入队顺序），本测试先行编写，实现前应全部失败。

bot-issue 调度规则（issue #360）：标记库新增 `bot-issue` 默认标记，任务
带 `bot-issue` 标记时排在**所有任务之后**（权重 = len(priority)+1，比未命中
配置标签/无标签任务还靠后）；用户若在配置 worker.issue_priority 中显式包含
`bot-issue`，则按配置顺序派发（允许自定义）。
"""

import json
import sqlite3
import threading

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.scheduler import TaskScheduler

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
"""


class FakeExecutor:
    """记录 run_task 调用的执行器桩（不真正执行任务）。"""

    def __init__(self):
        self.run_ids: list[int] = []
        self._started: list[threading.Event] = []
        self._done: list[threading.Event] = []

    def run_task(self, task_id: int):
        self.run_ids.append(task_id)
        self._started.pop(0).set()
        self._done.pop(0).set()

    def request_stop(self, task_id: int):
        pass

    def expect(self) -> tuple[threading.Event, threading.Event]:
        """登记下一次 run_task 的 (started, done) 事件对。"""
        started = threading.Event()
        done = threading.Event()
        self._started.append(started)
        self._done.append(done)
        return started, done


@pytest.fixture
def config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    return ConfigManager(str(config_path))


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def executor():
    return FakeExecutor()


def _mk_repo(db, project_id: int, priority: int | None = None) -> int:
    """插入一条仓库记录（可选指定优先级），返回 repo_id。"""
    kwargs = {}
    if priority is not None:
        kwargs["priority"] = priority
    db.upsert_repo(project_id, f"repo-{project_id}",
                   f"https://gitlab.example.com/group/repo-{project_id}.git", **kwargs)
    return db.get_repo_by_project_id(project_id)["id"]


def _mk_task(db, repo_id: int, project_id: int, issue_iid: int,
             issue_labels: list[str] | None = None,
             issue_created_at: str | None = None,
             issue_updated_at: str | None = None,
             created_at: str | None = None) -> int:
    """创建排队任务（带标签/issue 创建时间），可按需改写 created_at。"""
    task_id = db.create_task(
        repo_id, project_id, issue_iid, f"任务 {project_id}#{issue_iid}",
        triggered_by="webhook",
        issue_labels=issue_labels or [],
        issue_created_at=issue_created_at or "",
        issue_updated_at=issue_updated_at or "")
    if created_at is not None:
        conn = sqlite3.connect(db.path)
        conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (created_at, task_id))
        conn.commit()
        conn.close()
    return task_id


def _worker_config(tmp_path, issue_priority: list[str]) -> ConfigManager:
    """生成带 worker.issue_priority 自定义顺序的配置。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        CONFIG_TEXT.replace(
            "worker: {}",
            "worker:\n  issue_priority: " + json.dumps(issue_priority)),
        encoding="utf-8",
    )
    return ConfigManager(str(config_path))


def _scheduler(config, db, executor) -> TaskScheduler:
    return TaskScheduler(config, db, executor)


class TestDispatchIssueLabelPriority:
    """同仓库队列内按标签权重派发（不启动调度线程，手动触发 _dispatch）。"""

    def test_bug_dispatched_before_feature_in_same_repo(self, config, db, executor):
        """同仓库反序入队：bug 任务比 feature 任务先派发（默认 bug 最优先）。"""
        repo = _mk_repo(db, project_id=11)
        t_feature = _mk_task(db, repo, project_id=11, issue_iid=1,
                             issue_labels=["feature"])
        t_bug = _mk_task(db, repo, project_id=11, issue_iid=2,
                         issue_labels=["bug"])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_feature)  # feature 先入队
        sched.enqueue(t_bug)      # bug 后入队，应插到队首

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2), "应派发一个任务"
        assert executor.run_ids == [t_bug], "bug 任务应优先于 feature 任务派发"

    def test_default_order_bug_test_feature(self, config, db, executor):
        """默认顺序 bug > test > feature（config 未配置时）。"""
        repo = _mk_repo(db, project_id=11)
        t_feature = _mk_task(db, repo, project_id=11, issue_iid=1,
                             issue_labels=["feature"])
        t_test = _mk_task(db, repo, project_id=11, issue_iid=2,
                          issue_labels=["test"])
        t_bug = _mk_task(db, repo, project_id=11, issue_iid=3,
                         issue_labels=["bug"])

        sched = _scheduler(config, db, executor)
        # 入队顺序：feature → test → bug，派发顺序应与入队无关
        for tid in (t_feature, t_test, t_bug):
            sched.enqueue(tid)

        for expected in (t_bug, t_test, t_feature):
            _, done = executor.expect()
            sched._dispatch()
            assert done.wait(timeout=2), f"任务 {expected} 应被派发"
        assert executor.run_ids == [t_bug, t_test, t_feature], \
            "默认顺序应为 bug > test > feature"

    def test_custom_priority_config_effective(self, tmp_path, db, executor):
        """自定义配置：worker.issue_priority 可调整标签顺序（feature 排最前）。"""
        cfg = _worker_config(tmp_path, ["feature", "bug", "test"])
        repo = _mk_repo(db, project_id=11)
        t_bug = _mk_task(db, repo, project_id=11, issue_iid=1, issue_labels=["bug"])
        t_feature = _mk_task(db, repo, project_id=11, issue_iid=2,
                             issue_labels=["feature"])

        sched = _scheduler(cfg, db, executor)
        sched.enqueue(t_bug)
        sched.enqueue(t_feature)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_feature], "自定义顺序下 feature 应先派发"

    def test_unlisted_labels_last(self, config, db, executor):
        """未列入配置的标签（如 docs）排在所有已配置标签之后。"""
        repo = _mk_repo(db, project_id=11)
        t_docs = _mk_task(db, repo, project_id=11, issue_iid=1,
                          issue_labels=["docs"])
        t_bug = _mk_task(db, repo, project_id=11, issue_iid=2,
                         issue_labels=["bug"])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_docs)
        sched.enqueue(t_bug)

        _, done1 = executor.expect()
        sched._dispatch()
        assert done1.wait(timeout=2)
        _, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t_bug, t_docs], "未配置标签的任务应排最后"

    def test_no_labels_last(self, config, db, executor):
        """无标签任务排在所有带配置标签的任务之后。"""
        repo = _mk_repo(db, project_id=11)
        t_none = _mk_task(db, repo, project_id=11, issue_iid=1, issue_labels=[])
        t_feature = _mk_task(db, repo, project_id=11, issue_iid=2,
                             issue_labels=["feature"])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_none)
        sched.enqueue(t_feature)

        _, done1 = executor.expect()
        sched._dispatch()
        assert done1.wait(timeout=2)
        _, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t_feature, t_none], "无标签任务应排最后"

    def test_same_weight_ordered_by_issue_created_at(self, config, db, executor):
        """同权重（都是 bug）：按 issue 创建时间升序，创建早的先派发（issue #234）。"""
        repo = _mk_repo(db, project_id=11)
        t_late = _mk_task(db, repo, project_id=11, issue_iid=1,
                          issue_labels=["bug"],
                          issue_created_at="2026-08-14 12:00:00")
        t_early = _mk_task(db, repo, project_id=11, issue_iid=2,
                           issue_labels=["bug"],
                           issue_created_at="2026-08-14 08:00:00")

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_late)   # 创建晚的 issue 先入队
        sched.enqueue(t_early)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_early], "同权重应按 issue 创建时间升序派发"

    def test_issue_created_at_precedes_updated_at(self, config, db, executor):
        """创建时间优先于更新时间：创建早但更新晚的 issue 先派发（issue #234）。

        回归保护：修复前按 issue 更新时间排序，创建早但更新晚的 issue 会被
        排在后面；修复后一律按创建时间排序。
        """
        repo = _mk_repo(db, project_id=11)
        t_created_late = _mk_task(db, repo, project_id=11, issue_iid=1,
                                  issue_labels=["bug"],
                                  issue_created_at="2026-08-14 10:00:00",
                                  issue_updated_at="2026-08-14 08:00:00")
        t_created_early = _mk_task(db, repo, project_id=11, issue_iid=2,
                                   issue_labels=["bug"],
                                   issue_created_at="2026-08-14 08:00:00",
                                   issue_updated_at="2026-08-14 12:00:00")

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_created_late)
        sched.enqueue(t_created_early)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_created_early], \
            "创建更早的 issue 应先派发（即使其更新时间更晚）"

    def test_missing_issue_created_at_falls_back_to_updated_at(self, config, db, executor):
        """issue_created_at 缺失（修复前历史数据）：按 issue 更新时间兜底。"""
        repo = _mk_repo(db, project_id=11)
        t_new = _mk_task(db, repo, project_id=11, issue_iid=1,
                         issue_labels=["bug"],
                         issue_updated_at="2026-08-14 12:00:00")
        t_old = _mk_task(db, repo, project_id=11, issue_iid=2,
                         issue_labels=["bug"],
                         issue_updated_at="2026-08-14 08:00:00")

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_new)
        sched.enqueue(t_old)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_old], \
            "创建时间缺失时按 issue 更新时间升序兜底"

    def test_missing_updated_at_falls_back_to_created_at(self, config, db, executor):
        """issue_updated_at 缺失（历史数据）：按任务创建时间兜底。"""
        repo = _mk_repo(db, project_id=11)
        t_b = _mk_task(db, repo, project_id=11, issue_iid=1,
                       issue_labels=["bug"], created_at="2026-08-14 10:00:00")
        t_a = _mk_task(db, repo, project_id=11, issue_iid=2,
                       issue_labels=["bug"], created_at="2026-08-14 09:00:00")

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_b)
        sched.enqueue(t_a)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_a], "更新时间缺失时按任务创建时间升序兜底"

    def test_config_change_applies_to_queued_tasks(self, tmp_path, db, executor):
        """配置修改后，已入队任务按新配置排序（派发时动态读配置）。"""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        cfg = ConfigManager(str(config_path))
        repo = _mk_repo(db, project_id=11)
        t_bug = _mk_task(db, repo, project_id=11, issue_iid=1, issue_labels=["bug"])
        t_feature = _mk_task(db, repo, project_id=11, issue_iid=2,
                             issue_labels=["feature"])

        sched = _scheduler(cfg, db, executor)
        sched.enqueue(t_bug)
        sched.enqueue(t_feature)

        # 修改配置：feature 提到最前（模拟设置页保存）
        config_path.write_text(
            CONFIG_TEXT.replace(
                "worker: {}",
                'worker:\n  issue_priority: ["feature", "bug"]'),
            encoding="utf-8",
        )

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_feature], "已入队任务应使用最新配置排序"

    def test_repo_priority_dominates_label_weight(self, config, db, executor):
        """跨仓库：仓库优先级（issue #51）优先于队列内标签权重。"""
        hi = _mk_repo(db, project_id=11, priority=100)
        lo = _mk_repo(db, project_id=22, priority=1)  # 仓库优先级数字小先派发
        t_hi_bug = _mk_task(db, hi, project_id=11, issue_iid=1, issue_labels=["bug"])
        t_lo_feature = _mk_task(db, lo, project_id=22, issue_iid=1,
                                issue_labels=["feature"])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_hi_bug)
        sched.enqueue(t_lo_feature)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_lo_feature], \
            "仓库优先级高（数字小）的仓库应先派发（即使其任务标签权重低）"

    def test_all_unlisted_keeps_fifo_by_created_at(self, config, db, executor):
        """全部任务权重相同（都未命中配置）：保持按创建时间升序（原 FIFO 语义）。"""
        repo = _mk_repo(db, project_id=11)
        t2 = _mk_task(db, repo, project_id=11, issue_iid=1,
                      issue_labels=["docs"], created_at="2026-08-14 10:00:00")
        t1 = _mk_task(db, repo, project_id=11, issue_iid=2,
                      issue_labels=["docs"], created_at="2026-08-14 09:00:00")

        sched = _scheduler(config, db, executor)
        sched.enqueue(t2)
        sched.enqueue(t1)

        _, done1 = executor.expect()
        sched._dispatch()
        assert done1.wait(timeout=2)
        _, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t1, t2], "权重相同时应按创建时间升序（FIFO）"

    def test_multi_label_uses_highest_priority_match(self, config, db, executor):
        """多标签任务：按列表中首个命中的标签定权重（bug+ui 按 bug 权重）。"""
        repo = _mk_repo(db, project_id=11)
        t_feature = _mk_task(db, repo, project_id=11, issue_iid=1,
                             issue_labels=["feature"])
        t_bug_ui = _mk_task(db, repo, project_id=11, issue_iid=2,
                            issue_labels=["ui", "bug"])  # bug 在 ui 之后仍应命中 bug

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_feature)
        sched.enqueue(t_bug_ui)

        started, done = executor.expect()
        sched._dispatch()
        assert done.wait(timeout=2)
        assert executor.run_ids == [t_bug_ui], "多标签任务应按首个命中配置的标签定权重"

    def test_same_repo_serial_execution_kept(self, config, db, executor):
        """同仓库串行约束不受影响：running 任务存在时不派发同仓库下一任务。"""
        repo = _mk_repo(db, project_id=11)
        t_bug = _mk_task(db, repo, project_id=11, issue_iid=1, issue_labels=["bug"])
        t_feature = _mk_task(db, repo, project_id=11, issue_iid=2,
                             issue_labels=["feature"])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_bug)
        sched.enqueue(t_feature)
        # 模拟 bug 任务已派发、该仓库进入 running（手动登记状态，
        # 与 test_scheduler_priority.py 的做法一致）
        with sched._lock:
            sched._running[repo] = t_bug
            sched._queues[repo].remove(t_bug)

        # 同仓库仍 running：不应派发队列中剩余的 feature 任务
        sched._dispatch()
        assert executor.run_ids == [], "同仓库有 running 任务时不应派发同仓库下一任务"


class TestDispatchBotIssueLabel:
    """带 bot-issue 标记的任务排在所有任务之后（issue #360）。"""

    def test_bot_issue_dispatched_after_unlisted_labels(self, config, db, executor):
        """bot-issue 任务排在未命中配置标签（如 docs）的任务之后。"""
        repo = _mk_repo(db, project_id=11)
        t_bot = _mk_task(db, repo, project_id=11, issue_iid=1,
                         issue_labels=["bot-issue"])
        t_docs = _mk_task(db, repo, project_id=11, issue_iid=2,
                          issue_labels=["docs"])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_bot)
        sched.enqueue(t_docs)

        _, done1 = executor.expect()
        sched._dispatch()
        assert done1.wait(timeout=2)
        _, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t_docs, t_bot], \
            "bot-issue 任务应排在未命中配置标签的任务之后"

    def test_bot_issue_dispatched_after_no_label(self, config, db, executor):
        """无标签任务也排在 bot-issue 任务之前（bot-issue 严格排所有任务之后）。"""
        repo = _mk_repo(db, project_id=11)
        t_bot = _mk_task(db, repo, project_id=11, issue_iid=1,
                         issue_labels=["bot-issue"])
        t_none = _mk_task(db, repo, project_id=11, issue_iid=2, issue_labels=[])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_bot)
        sched.enqueue(t_none)

        _, done1 = executor.expect()
        sched._dispatch()
        assert done1.wait(timeout=2)
        _, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t_none, t_bot], \
            "bot-issue 任务应排在无标签任务之后"

    def test_bot_issue_with_bug_label_still_last(self, config, db, executor):
        """bug + bot-issue 共存：只要带 bot-issue 就排所有任务之后。"""
        repo = _mk_repo(db, project_id=11)
        t_bug_bot = _mk_task(db, repo, project_id=11, issue_iid=1,
                             issue_labels=["bug", "bot-issue"])
        t_feature = _mk_task(db, repo, project_id=11, issue_iid=2,
                             issue_labels=["feature"])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_bug_bot)
        sched.enqueue(t_feature)

        _, done1 = executor.expect()
        sched._dispatch()
        assert done1.wait(timeout=2)
        _, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t_feature, t_bug_bot], \
            "带 bot-issue 的任务即使含 bug 也应排最后"

    def test_bot_issue_configured_in_priority_uses_config_order(self, tmp_path, db, executor):
        """配置 worker.issue_priority 显式包含 bot-issue 时按配置顺序（可自定义）。"""
        cfg = _worker_config(tmp_path, ["bug", "bot-issue", "test"])
        repo = _mk_repo(db, project_id=11)
        t_test = _mk_task(db, repo, project_id=11, issue_iid=1,
                          issue_labels=["test"])
        t_bot = _mk_task(db, repo, project_id=11, issue_iid=2,
                         issue_labels=["bot-issue"])

        sched = _scheduler(cfg, db, executor)
        sched.enqueue(t_test)
        sched.enqueue(t_bot)

        _, done1 = executor.expect()
        sched._dispatch()
        assert done1.wait(timeout=2)
        _, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t_bot, t_test], \
            "配置显式包含 bot-issue 时应按配置索引派发"

    def test_bot_issue_tasks_ordered_by_issue_created_at(self, config, db, executor):
        """多个 bot-issue 任务：同权重按 issue 创建时间升序（创建早的先派发）。"""
        repo = _mk_repo(db, project_id=11)
        t_late = _mk_task(db, repo, project_id=11, issue_iid=1,
                          issue_labels=["bot-issue"],
                          issue_created_at="2026-08-14 12:00:00")
        t_early = _mk_task(db, repo, project_id=11, issue_iid=2,
                           issue_labels=["bot-issue"],
                           issue_created_at="2026-08-14 08:00:00")

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_late)
        sched.enqueue(t_early)

        _, done1 = executor.expect()
        sched._dispatch()
        assert done1.wait(timeout=2)
        _, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t_early, t_late], \
            "bot-issue 任务之间按 issue 创建时间升序派发"


class TestCrossRepoBotIssue:
    """跨仓库场景下 bot-issue 必须排所有任务之后（issue #363）。

    #360 只解决了同仓库队列内 bot-issue 排最后；跨仓库时 _repo_sort_key
    先按仓库优先级（数字小先派发）选仓库，再取该仓库队内最优任务——若
    bot-issue 任务所在仓库 priority 更小，其 bot-issue 任务会被优先派发，
    违反「带 bot-issue 的 issue 在所有不带 bot-issue 的 issue 执行完成后
    执行」的预期。
    """

    def test_bot_issue_not_dispatched_before_other_repo_normal_task(
            self, config, db, executor):
        """仓库 A（priority=1）有 bot-issue 任务、仓库 B（priority=2）有
        普通 bug 任务：必须优先派发 B 的普通任务，bot-issue 任务不能
        因仓库优先级靠前而被提前派发。"""
        repo_a = _mk_repo(db, project_id=11, priority=1)   # 高优先级仓库
        repo_b = _mk_repo(db, project_id=22, priority=2)   # 低优先级仓库
        t_a_bot = _mk_task(db, repo_a, project_id=11, issue_iid=1,
                           issue_labels=["bot-issue"])
        t_b_bug = _mk_task(db, repo_b, project_id=22, issue_iid=1,
                           issue_labels=["bug"])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_a_bot)
        sched.enqueue(t_b_bug)

        # 第一轮派发：应派发仓库 B 的普通 bug 任务（即使仓库 A 优先级更高）
        _, done1 = executor.expect()
        sched._dispatch()
        assert done1.wait(timeout=2)
        assert executor.run_ids == [t_b_bug], \
            "仓库 A 优先级更高但任务带 bot-issue，应先派发仓库 B 的普通任务"

        # 第二轮派发：普通任务执行完成后，才派发 bot-issue 任务
        _, done2 = executor.expect()
        sched._dispatch()
        assert done2.wait(timeout=2)
        assert executor.run_ids == [t_b_bug, t_a_bot], \
            "普通任务完成后才派发 bot-issue 任务"

    def test_bot_issue_wait_for_other_repo_running_task(self, config, db, executor):
        """其他仓库有普通任务运行中：不应派发 bot-issue 任务（需等其完成）。"""
        repo_a = _mk_repo(db, project_id=11, priority=1)
        repo_b = _mk_repo(db, project_id=22, priority=2)
        t_a_bot = _mk_task(db, repo_a, project_id=11, issue_iid=1,
                           issue_labels=["bot-issue"])
        t_b_bug = _mk_task(db, repo_b, project_id=22, issue_iid=1,
                           issue_labels=["bug"])

        sched = _scheduler(config, db, executor)
        sched.enqueue(t_a_bot)
        sched.enqueue(t_b_bug)
        # 仓库 B 普通任务已派发并在运行中
        with sched._lock:
            sched._running[repo_b] = t_b_bug
            sched._queues[repo_b].remove(t_b_bug)

        # 仓库 B 普通任务运行中：bot-issue 任务不应被派发
        sched._dispatch()
        assert executor.run_ids == [], \
            "其他仓库普通任务运行中时不应派发 bot-issue 任务"
