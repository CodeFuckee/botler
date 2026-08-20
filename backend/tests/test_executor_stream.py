"""executor 实时事件流测试（实时输出功能第二轮）。

需求：任务执行期间逐事件实时可见（claude stream-json / hermes 流式协议）。
- claude 引擎切 `--output-format stream-json` 后输出多行 NDJSON，逐行解析：
  system/init → status（含 session_id，运行一开始即落库）、assistant →
  thinking/text/tool、user → tool_result、result → 结果
- 事件经 executor.event_bus 推送（SSE 订阅），日志文件保留原始行（回放兜底）
- hermes runner 流式输出：事件行推送总线，最后一行结果 JSON 用于判定
"""

import json
import time

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.events import EventBus
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.templates import TemplateRenderer

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {precheck_enabled: false}
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
                          workspace_root=str(tmp_path / "workspace"),
                          event_bus=EventBus())


class _MultiLineStdout:
    """多行输出：readline 逐行返回（含换行符，与真实管道一致），行耗尽返回 EOF。"""

    def __init__(self, lines: list[str]):
        self._lines = [l + "\n" for l in lines]

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


class _FakeStdin:
    """hermes 引擎会写 stdin 请求，fake 静默接收。"""

    def write(self, text: str) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self, lines: list[str], exit_code: int = 0):
        self.stdout = _MultiLineStdout(lines)
        self._exit = exit_code
        self.stdin = _FakeStdin()

    def poll(self):
        return self._exit if not self.stdout._lines else None

    def wait(self, timeout=None):
        return self._exit


def _repo():
    return {"name": "demo", "url": "https://gitlab.example.com/group/demo.git",
            "prompt_template": None}


def _issue():
    return {"project_id": 42, "iid": 7, "title": "修复登录问题",
            "description": "登录报错"}


def _stream_lines() -> list[str]:
    """典型 claude stream-json 输出（事件行 + 结尾 result 行）。"""
    return [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": "sess-live", "cwd": "/work/demo",
                    "model": "claude-fable-5"}, ensure_ascii=False),
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "先定位报错位置"},
                        {"type": "text", "text": "我来修复登录问题。"},
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "git status"}},
                    ]}}, ensure_ascii=False),
        json.dumps({"type": "user",
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "tu1",
                         "content": "On branch main", "is_error": False},
                    ]}}, ensure_ascii=False),
        json.dumps({"type": "result", "subtype": "success",
                    "result": "修复完成，已推送并打 bot-done 标签。",
                    "exit_code": 0, "session_id": "sess-live"},
                   ensure_ascii=False),
    ]


def _drain_events(sub, count: int, timeout: float = 2.0) -> list[dict]:
    """从订阅队列取 count 个事件（超时即失败）。"""
    events = []
    deadline = time.time() + timeout
    while len(events) < count:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        events.append(sub.get(timeout=remaining))
    return events


class TestClaudeStreamEvents:
    """claude 引擎：stream-json 多行输出 → 事件推送总线 + 日志保留原始行。"""

    def test_run_once_pushes_events_to_bus(self, executor, monkeypatch, tmp_path):
        sub = executor.event_bus.subscribe(1)
        try:
            lines = _stream_lines()

            def fake_popen(cmd, **kwargs):
                return _FakeProc(lines, 0)

            monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
            monkeypatch.setattr(executor, "prepare_workspace",
                                lambda repo, resume=False: (tmp_path / "ws", {}))
            monkeypatch.setattr(executor, "_log_file",
                                lambda tid: tmp_path / f"task_{tid}.log")

            exit_code, output = executor._run_once(1, _repo(), _issue())

            assert exit_code == 0
            assert "修复完成" in output
            events = _drain_events(sub, 6)
            kinds = [e["kind"] for e in events]
            # init → thinking/text/tool（assistant 三块）→ tool_result → result
            assert kinds == ["status", "thinking", "text", "tool",
                             "tool_result", "result"]
            assert events[0]["session_id"] == "sess-live"
            assert events[2]["text"] == "我来修复登录问题。"
            assert events[3]["tool"] == "Bash"
            assert events[4]["text"] == "On branch main"
            assert events[5]["result"].startswith("修复完成")
        finally:
            sub.close()

    def test_events_have_monotonic_seq_and_ts(self, executor, monkeypatch, tmp_path):
        sub = executor.event_bus.subscribe(1)
        try:
            def fake_popen(cmd, **kwargs):
                return _FakeProc(_stream_lines(), 0)

            monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
            monkeypatch.setattr(executor, "prepare_workspace",
                                lambda repo, resume=False: (tmp_path / "ws", {}))
            monkeypatch.setattr(executor, "_log_file",
                                lambda tid: tmp_path / f"task_{tid}.log")

            executor._run_once(1, _repo(), _issue())
            events = _drain_events(sub, 6)
            seqs = [e["seq"] for e in events]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
            assert all(e["ts"] for e in events)
        finally:
            sub.close()

    def test_log_file_keeps_raw_lines(self, executor, monkeypatch, tmp_path):
        """日志文件保留引擎原始输出行（历史回放/调试兜底）。"""
        lines = _stream_lines()

        def fake_popen(cmd, **kwargs):
            return _FakeProc(lines, 0)

        monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (tmp_path / "ws", {}))
        log_path = tmp_path / "task_1.log"
        monkeypatch.setattr(executor, "_log_file", lambda tid: log_path)

        executor._run_once(1, _repo(), _issue())

        logged = log_path.read_text(encoding="utf-8").splitlines()
        assert logged == lines

    def test_session_id_persisted_during_run(self, executor, monkeypatch, tmp_path):
        """init 行一出现即落库 session_id（运行中即可实时查看）。"""
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (tmp_path / "ws", {}))
        monkeypatch.setattr(executor, "_log_file",
                            lambda tid: tmp_path / f"task_{tid}.log")

        def fake_popen(cmd, **kwargs):
            return _FakeProc(_stream_lines(), 0)

        monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
        repo_id = executor.db.upsert_repo(
            42, "demo", "https://gitlab.example.com/group/demo.git")
        task_id = executor.db.create_task(repo_id, 42, 7, "t", triggered_by="webhook")

        executor._run_once(task_id, _repo(), _issue())

        row = executor.db.get_task(task_id)
        assert row["claude_session_id"] == "sess-live"

    def test_result_line_required_for_success_judgement(self, executor, monkeypatch, tmp_path):
        """无 result 事件的多行输出（异常中断）→ exit 0 但输出不可判成功。

        该行为由 run_task 判定（_load_json_output 取首个 JSON 对象仍是
        init 行 → result 缺失），此处验证 _run_once 原样返回输出。"""
        lines = [json.dumps({"type": "system", "subtype": "init",
                             "session_id": "sess-x", "cwd": "/w", "model": "m"})]

        def fake_popen(cmd, **kwargs):
            return _FakeProc(lines, 0)

        monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (tmp_path / "ws", {}))
        monkeypatch.setattr(executor, "_log_file",
                            lambda tid: tmp_path / f"task_{tid}.log")

        exit_code, output = executor._run_once(1, _repo(), _issue())
        assert exit_code == 0
        assert '"result"' not in output


class _FakeHermesRunner:
    """假 HermesSdkRunner：start 时同步回放 preset_lines（模拟 SDK worker 输出）。"""

    instances: list["_FakeHermesRunner"] = []
    preset_lines: list[str] = []
    preset_done: bool = True

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._done = _FakeHermesRunner.preset_done
        self.stop_calls = 0
        self.result_lines = list(_FakeHermesRunner.preset_lines)
        _FakeHermesRunner.instances.append(self)

    def start(self):
        for line in self.result_lines:
            self.kwargs["on_line"](line)

    def done(self):
        return self._done

    def finish(self):
        return 0

    def stop(self):
        self.stop_calls += 1


@pytest.fixture
def fake_hermes_runner(monkeypatch):
    """注入假 HermesSdkRunner 到 executor 模块命名空间并重置 preset。"""
    _FakeHermesRunner.instances.clear()
    _FakeHermesRunner.preset_lines = []
    _FakeHermesRunner.preset_done = True
    monkeypatch.setattr("botler.executor.HermesSdkRunner", _FakeHermesRunner)
    monkeypatch.setattr("botler.executor.HermesSdkNotInstalledError",
                        type("HermesSdkNotInstalledError", (Exception,), {}))
    return _FakeHermesRunner


class TestHermesStreamEvents:
    """hermes 引擎（SDK 进程内模式）：SDK 事件行 + 最后结果行 → 总线 + 判定。"""

    def _hermes_lines(self) -> list[str]:
        return [
            json.dumps({"event": "thinking", "text": "定位问题"},
                       ensure_ascii=False),
            json.dumps({"event": "tool_start", "tool": "bash",
                        "input": "pytest"}, ensure_ascii=False),
            json.dumps({"event": "tool_complete", "tool": "bash",
                        "output": "42 passed", "is_error": False},
                       ensure_ascii=False),
            json.dumps({"final_response": "已完成修复",
                        "messages": [{"role": "assistant", "content": "完成"}],
                        "session_id": "hsess-1", "error": None},
                       ensure_ascii=False),
        ]

    def _patch_hermes(self, monkeypatch, executor, tmp_path, lines):
        """配置 engine=hermes + 假 SDK runner 回放输出。"""
        _FakeHermesRunner.preset_lines = lines
        monkeypatch.setattr(executor, "prepare_workspace",
                            lambda repo, resume=False: (tmp_path / "ws", {}))
        monkeypatch.setattr(executor, "_log_file",
                            lambda tid: tmp_path / f"task_{tid}.log")
        cfg = executor.config.get()
        cfg.engine = "hermes"
        monkeypatch.setattr(executor.config, "get", lambda: cfg)

    def test_hermes_events_pushed_to_bus(
            self, executor, monkeypatch, tmp_path, fake_hermes_runner):
        sub = executor.event_bus.subscribe(1)
        try:
            self._patch_hermes(monkeypatch, executor, tmp_path,
                               self._hermes_lines())
            exit_code, output = executor._run_once(1, _repo(), _issue())

            assert exit_code == 0
            events = _drain_events(sub, 3)
            kinds = [e["kind"] for e in events]
            assert kinds == ["thinking", "tool", "tool_result"]
            assert events[1]["tool"] == "bash"
        finally:
            sub.close()

    def test_hermes_result_judged_from_last_line(
            self, executor, monkeypatch, tmp_path, fake_hermes_runner):
        """结果判定取最后一行（事件行在前，结果 JSON 收尾）。"""
        self._patch_hermes(monkeypatch, executor, tmp_path,
                           self._hermes_lines())
        exit_code, output = executor._run_once(1, _repo(), _issue())

        # _hermes_result 取最后一行 final_response
        assert exit_code == 0
        assert executor._hermes_result(output) == "success"
        history = executor._hermes_history_from_output(output)
        assert history == [{"role": "assistant", "content": "完成"}]

    def test_single_result_line_still_works(
            self, executor, monkeypatch, tmp_path, fake_hermes_runner):
        """无回调（安静执行）时只输出结果行：唯一行即结果，无事件。"""
        line = json.dumps({"final_response": "完成", "messages": [],
                           "session_id": "hsess-0", "error": None})
        self._patch_hermes(monkeypatch, executor, tmp_path, [line])
        exit_code, output = executor._run_once(1, _repo(), _issue())

        assert exit_code == 0
        assert executor._hermes_result(output) == "success"
