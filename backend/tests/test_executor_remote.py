"""远程项目执行链路测试（SSH 直连，全部 mock remote_exec 的 subprocess）。

覆盖：
- _repo_workdir / _remote_cfg_for：远程仓库路径标识与主机配置解析；
- _prepare_workspace_remote：远端 git 序列（校验目录 → askpass 写入 →
  fetch → ls-remote 解析分支 → 补跟踪引用 → checkout/reset/clean →
  pull --rebase；冲突保留现场；resume 只 fetch）；
- _run_zcode_once 远程分支：远端命令构造（cd/env 凭据/标记/skip-permissions/
  --resume）、prompt 经 stdin、session 落 zcode_session_id；
- request_stop：本地杀进程组 + 远程 pkill 任务标记；
- _run_precheck_remote：SSH 连通/远端仓库/磁盘检查与失败短路。
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

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
zcode:
  command: zc
templates: {}
repos: []
remotes:
  - name: build
    host: 10.0.0.9
    user: bot
"""

REMOTE_REPO = {
    "name": "proj", "url": "https://gitlab.example.com/group/proj.git",
    "remote_host": "build", "remote_path": "/srv/apps/proj",
    "remote_name": "origin", "prompt_template": None,
}


class _MultiLineStdout:
    def __init__(self, lines):
        self._lines = [l + "\n" for l in lines]

    def readline(self):
        return self._lines.pop(0) if self._lines else ""


class _FakeProc:
    def __init__(self, lines, exit_code=0):
        self.stdout = _MultiLineStdout(lines)
        self._exit = exit_code
        self.stdin = SimpleNamespace(write=lambda s: None, close=lambda: None)

    def poll(self):
        return self._exit if not self.stdout._lines else None

    def wait(self, timeout=None):
        return self._exit


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


def _fake_run_remote(monkeypatch, script):
    """mock subprocess.run（remote_exec 通道），按远端命令特征分流。

    script(runner) —— runner(argv_tail, command) 返回
    (returncode, stdout, stderr)。
    """
    from types import SimpleNamespace

    commands: list[str] = []

    def fake_run(argv, timeout=None, **kwargs):
        command = argv[-1]
        commands.append(command)
        rc, out, err = script(command)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


class TestRemoteWorkdir:
    def test_remote_workdir_is_remote_path(self, executor):
        # Path 相等比较按解析结果（Windows 下反斜杠规范化后仍相等）；
        # 不用 str() 比较——Windows 上 str(Path) 是反斜杠形态
        p = executor._repo_workdir(REMOTE_REPO)
        assert p == Path("/srv/apps/proj")

    def test_remote_cfg_found_and_missing(self, executor):
        assert executor._remote_cfg_for(REMOTE_REPO)["host"] == "10.0.0.9"
        bad = dict(REMOTE_REPO, remote_host="nope")
        from botler.executor.common import ExecutorError
        with pytest.raises(ExecutorError):
            executor._remote_cfg_for(bad)


class TestPrepareWorkspaceRemote:
    def test_full_sequence_happy_path(self, executor, monkeypatch):
        commands = _fake_run_remote(monkeypatch, lambda cmd: (
            _script_response(cmd)))

        workdir, git_env = executor.prepare_workspace(dict(REMOTE_REPO))

        assert workdir == Path("/srv/apps/proj")
        assert git_env["GIT_ASKPASS"].startswith("~/.botler/askpass-")
        joined = "\n".join(commands)
        # 序列：目录校验 → askpass 写入 → fetch → ls-remote → 补跟踪引用
        # → checkout -B → reset --hard → clean → pull --rebase
        # （远端命令一律用配置原样路径串，不经 Path 规范化——Windows
        # 部署机上 Path 会产生反斜杠，远端 POSIX shell 无法识别；
        # 无特殊字符的参数经 sh_quote 不加引号）
        assert "test -d /srv/apps/proj/.git" in joined
        assert "BOTLER_ASKPASS_EOF" in joined
        assert "fetch origin --prune" in joined
        assert "ls-remote --symref origin" in joined
        assert "+refs/heads/main:refs/remotes/origin/main" in joined
        assert "checkout -B main origin/main" in joined
        assert "reset --hard origin/main" in joined
        assert "clean -fd" in joined
        assert "pull --rebase origin main" in joined

    def test_not_git_dir_fails(self, executor, monkeypatch):
        _fake_run_remote(monkeypatch, lambda cmd: (
            (1, "", "") if cmd.startswith("test -d") else (0, "", "")))
        from botler.executor.common import ExecutorError
        with pytest.raises(ExecutorError, match="不是 git 仓库"):
            executor.prepare_workspace(dict(REMOTE_REPO))

    def test_pull_conflict_handoff(self, executor, monkeypatch):
        def script(cmd):
            if "pull --rebase" in cmd:
                return (1, "", "CONFLICT (content): Merge conflict in a.py")
            return _script_response(cmd)

        _fake_run_remote(monkeypatch, script)
        workdir, _ = executor.prepare_workspace(dict(REMOTE_REPO))
        assert workdir in executor._pull_conflict_workdirs

    def test_pull_non_conflict_fails(self, executor, monkeypatch):
        def script(cmd):
            if "pull --rebase" in cmd:
                return (128, "", "fatal: Authentication failed")
            return _script_response(cmd)

        _fake_run_remote(monkeypatch, script)
        from botler.executor.common import ExecutorError
        with pytest.raises(ExecutorError, match="远程 git pull 失败"):
            executor.prepare_workspace(dict(REMOTE_REPO))

    def test_resume_only_fetches(self, executor, monkeypatch):
        commands = _fake_run_remote(monkeypatch, lambda cmd: (
            _script_response(cmd)))
        executor.prepare_workspace(dict(REMOTE_REPO), resume=True)
        joined = "\n".join(commands)
        assert "fetch origin --prune" in joined
        assert "checkout" not in joined
        assert "reset --hard" not in joined
        assert "pull --rebase" not in joined


def _script_response(cmd: str):
    """远端 git 序列各命令的标准返回（默认分支 main）。"""
    if cmd.startswith("test -d"):
        return (0, "", "")
    if "BOTLER_ASKPASS_EOF" in cmd:
        return (0, "", "")
    if "ls-remote --symref" in cmd:
        return (0,
                "ref: refs/heads/main\tHEAD\n"
                "abc111\trefs/heads/main\n"
                "abc222\trefs/heads/dev\n", "")
    if "pull --rebase" in cmd:
        return (0, "Already up to date.", "")
    return (0, "", "")


class TestRemoteZcodeRun:
    def _toolkit(self, executor, monkeypatch, tmp_path):
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["stdin"] = kwargs.get("stdin")
            return _FakeProc([
                json.dumps({"type": "system", "subtype": "init",
                            "session_id": "sess-remote", "cwd": "/srv/apps/proj"}),
                json.dumps({"type": "result", "subtype": "success",
                            "result": "远程修复完成", "session_id": "sess-remote"}),
            ], 0)

        monkeypatch.setattr("botler.executor.subprocess.Popen", fake_popen)
        monkeypatch.setattr(executor, "_log_file",
                            lambda tid: tmp_path / f"task_{tid}.log")
        return captured

    def test_remote_run_command_and_session(self, executor, monkeypatch, tmp_path):
        captured = self._toolkit(executor, monkeypatch, tmp_path)
        commands = _fake_run_remote(monkeypatch, lambda cmd: _script_response(cmd))
        db = executor.db
        repo_id = db.upsert_repo(42, "proj", REMOTE_REPO["url"],
                                 remote_host="build", remote_path="/srv/apps/proj",
                                 remote_name="origin", engine="zcode")
        task_id = db.create_task(repo_id, 42, 7, "t", triggered_by="webhook")

        exit_code, output = executor._run_zcode_once(task_id, dict(REMOTE_REPO),
                                                     {"project_id": 42, "iid": 7,
                                                      "title": "修复", "description": ""})
        assert exit_code == 0
        # 输出为 stream-json 原始行（ensure_ascii 转义），结果行可解析出最终回复
        result_line = json.loads(output.strip().splitlines()[-1])
        assert result_line["result"] == "远程修复完成"
        # 本地拉起的是 ssh 进程（stream_remote 构造），命令末段为远端命令串
        argv = captured["argv"]
        assert argv[0] == "ssh"
        remote_cmd = argv[-1]
        assert "cd /srv/apps/proj && env" in remote_cmd
        assert "GITLAB_TOKEN=" in remote_cmd
        assert "BOTLER_TASK_MARKER=botler-task-" in remote_cmd
        assert "--dangerously-skip-permissions" in remote_cmd
        # prompt 走 stdin（不进 argv）
        assert "修复" not in remote_cmd
        assert captured["stdin"] == subprocess.PIPE
        # 远端 git 序列已执行（工作区准备）
        joined = "\n".join(commands)
        assert "ls-remote --symref" in joined
        # 会话 id 落 zcode 列
        assert db.get_task(task_id)["zcode_session_id"] == "sess-remote"

    def test_remote_base_sha_captured(self, executor, monkeypatch, tmp_path):
        self._toolkit(executor, monkeypatch, tmp_path)

        def script(cmd):
            if "rev-parse HEAD" in cmd:
                return (0, "abcabcabc\n", "")
            return _script_response(cmd)

        _fake_run_remote(monkeypatch, script)
        db = executor.db
        repo_id = db.upsert_repo(42, "proj", REMOTE_REPO["url"],
                                 remote_host="build", remote_path="/srv/apps/proj")
        task_id = db.create_task(repo_id, 42, 7, "t", triggered_by="webhook")
        executor._run_zcode_once(task_id, dict(REMOTE_REPO),
                                 {"project_id": 42, "iid": 7,
                                  "title": "修复", "description": ""})
        assert db.get_task(task_id)["base_sha"] == "abcabcabc"


class TestRemoteStop:
    def test_request_stop_kills_local_and_remote_pkill(self, executor, monkeypatch):
        import threading


        pkilled = []

        def fake_run(argv, timeout=None, **kwargs):
            pkilled.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        killed = {}
        monkeypatch.setattr(executor, "_kill_process_group",
                            lambda proc: killed.update(pid=proc.pid))

        proc = SimpleNamespace(pid=4321, poll=lambda: None)
        with executor._proc_lock:
            executor._procs[7] = proc
            executor._remote_tasks[7] = (
                {"name": "build", "host": "10.0.0.9"}, "botler-task-7")
        executor.request_stop(7)

        assert executor._stop_requested(7)
        assert killed["pid"] == 4321
        # 远程 pkill 在后台线程执行，等待完成
        for _ in range(50):
            if pkilled:
                break
            threading.Event().wait(0.02)
        assert any("pkill -f botler-task-7" in c for c in pkilled)


class TestPrecheckRemote:
    def test_all_ok(self, executor, monkeypatch):
        _fake_run_remote(monkeypatch, lambda cmd: (
            (0, "ok\n", "") if cmd == "echo ok"
            else (0, "" , "") if cmd.startswith("test -d")
            else (0, "8388608\n", "")))  # df -k 输出 8GB（KB）
        result = executor._run_precheck_remote(
            1, dict(REMOTE_REPO), executor.config.get())
        assert result["ok"] is True
        names = [c["name"] for c in result["checks"]]
        assert names == ["ssh_connect", "remote_repo", "disk_space"]

    def test_ssh_fail_short_circuits(self, executor, monkeypatch):
        _fake_run_remote(monkeypatch, lambda cmd: (
            (255, "", "Permission denied")))
        result = executor._run_precheck_remote(
            1, dict(REMOTE_REPO), executor.config.get())
        assert result["ok"] is False
        assert result["checks"][0]["name"] == "ssh_connect"
        assert "Permission denied" in result["checks"][0]["detail"]

    def test_unknown_host_fails(self, executor):
        result = executor._run_precheck_remote(
            1, dict(REMOTE_REPO, remote_host="nope"), executor.config.get())
        assert result["ok"] is False
        assert "不存在" in result["checks"][0]["detail"]

    def test_disk_low_fails(self, executor, monkeypatch):
        _fake_run_remote(monkeypatch, lambda cmd: (
            (0, "ok\n", "") if cmd == "echo ok"
            else (0, "", "") if cmd.startswith("test -d")
            else (0, "1024\n", "")))  # 1MB < 2048MB 阈值
        result = executor._run_precheck_remote(
            1, dict(REMOTE_REPO), executor.config.get())
        assert result["ok"] is False
        disk = next(c for c in result["checks"] if c["name"] == "disk_space")
        assert disk["ok"] is False
