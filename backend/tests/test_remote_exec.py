"""SSH 远程执行通道与远程服务器配置测试。

覆盖：
- remote_exec.ssh_argv：选项聚合（BatchMode/keepalive/accept-new）、
  私钥/端口/用户/附加选项、缺 host 报错；
- run_remote / stream_remote：argv 透传（subprocess mock）；
- sh_quote 引用；
- config remotes 段归一化（合法/缺 name/host 跳过/重名跳过/port 兜底/
  extra_options 过滤）；
- settings API：GET 返回 zcode 与 remotes 段；PUT remotes 整段替换
  （校验非法值 400 + 写回 config.yaml）；remotes-test 端点（连通成功/
  连接失败/remote 不存在/未保存主机直测）。
"""

import subprocess
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.remote_exec import run_remote, sh_quote, ssh_argv

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: test-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
zcode:
  command: zcode
templates: {}
repos: []
remotes:
  - name: build-server
    host: 192.168.1.20
    port: 2222
    user: bot
    key_path: /home/me/.ssh/botler_ed25519
    extra_options: ["IdentitiesOnly=yes"]
  - host: no-name.example.com
  - name: dup
    host: a.example.com
  - name: dup
    host: b.example.com
  - name: bad-port
    host: c.example.com
    port: not-a-number
"""


@pytest.fixture
def client(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app), tmp_path, config


def _remote():
    return {"name": "build", "host": "10.0.0.9", "port": 2222,
            "user": "bot", "key_path": "/keys/id_ed25519",
            "extra_options": ["IdentitiesOnly=yes"]}


class TestSshArgv:
    def test_full_options(self):
        argv = ssh_argv(_remote(), "echo ok")
        assert argv[0] == "ssh"
        for flag in ["BatchMode=yes", "ServerAliveInterval=15",
                     "ServerAliveCountMax=3", "StrictHostKeyChecking=accept-new",
                     "IdentitiesOnly=yes"]:
            assert flag in argv
        assert argv[argv.index("-i") + 1] == "/keys/id_ed25519"
        assert argv[argv.index("-p") + 1] == "2222"
        assert argv[-2] == "bot@10.0.0.9"
        assert argv[-1] == "echo ok"

    def test_minimal_remote(self):
        argv = ssh_argv({"host": "h.example.com"}, "git status")
        assert argv[-2] == "h.example.com"
        assert "-i" not in argv
        assert argv[argv.index("-p") + 1] == "22"

    def test_missing_host_raises(self):
        with pytest.raises(ValueError):
            ssh_argv({"name": "x"}, "echo ok")


class TestRunAndStream:
    def test_run_remote_passes_argv(self, monkeypatch):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        monkeypatch.setattr(
            "botler.remote_exec.subprocess.run", fake_run)
        cp = run_remote(_remote(), "echo ok", timeout=5)
        assert cp.returncode == 0
        assert captured["argv"] == ssh_argv(_remote(), "echo ok")
        assert captured["kwargs"]["timeout"] == 5

    def test_stream_remote_passes_argv(self, monkeypatch):
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return SimpleNamespace(stdout=None)

        monkeypatch.setattr(
            "botler.remote_exec.subprocess.Popen", fake_popen)
        stream_remote = __import__("botler.remote_exec", fromlist=["stream_remote"]).stream_remote
        stream_remote(_remote(), "tail -f x.log")
        assert captured["argv"] == ssh_argv(_remote(), "tail -f x.log")
        assert captured["kwargs"]["start_new_session"] is True


class TestShQuote:
    def test_quote_safe_and_injection(self):
        assert sh_quote("plain") in ("plain", "'plain'")
        # 含空格/引号/分号必须被整体引用，无法逃逸出额外命令
        quoted = sh_quote("a b'; rm -rf /; echo ")
        assert subprocess.list2cmdline([quoted]).count("rm -rf") >= 0
        import shlex
        assert shlex.split(f"echo {quoted}") == ["echo", "a b'; rm -rf /; echo "]


class TestRemotesConfigParsing:
    def test_normalized_and_invalid_skipped(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(CONFIG_TEXT, encoding="utf-8")
        remotes = ConfigManager(str(config_path)).get().remotes
        # 合法项保留；缺 name / 重复 name（首见优先）/ port 非数字项被处理
        assert [r["name"] for r in remotes] == ["build-server", "dup", "bad-port"]
        build = remotes[0]
        assert build["host"] == "192.168.1.20"
        assert build["port"] == 2222
        assert build["user"] == "bot"
        assert build["key_path"] == "/home/me/.ssh/botler_ed25519"
        assert build["extra_options"] == ["IdentitiesOnly=yes"]
        assert remotes[1]["host"] == "a.example.com"
        assert remotes[2]["port"] == 22  # 非法 port 防御性兜底

    def test_empty_section(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            CONFIG_TEXT.split("remotes:")[0], encoding="utf-8")
        assert ConfigManager(str(config_path)).get().remotes == []


class TestSettingsApiZcodeRemotes:
    def test_get_includes_zcode_and_remotes(self, client):
        tc, tmp_path, config = client
        resp = tc.get("/api/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["zcode"] == {"command": "zcode",
                                 "args": ["-p", "--output-format",
                                          "stream-json", "--verbose"]}
        assert [r["name"] for r in body["remotes"]] == \
               ["build-server", "dup", "bad-port"]

    def test_put_zcode_section(self, client):
        tc, tmp_path, config = client
        resp = tc.put("/api/settings", json={
            "zcode": {"command": "/usr/local/bin/zcode", "args": ["-p"]}})
        assert resp.status_code == 200
        assert resp.json()["zcode"]["command"] == "/usr/local/bin/zcode"
        assert config.get().zcode_args == ["-p"]

    def test_put_zcode_rejects_bad_args(self, client):
        tc, tmp_path, config = client
        resp = tc.put("/api/settings", json={"zcode": {"args": "not-a-list"}})
        assert resp.status_code == 400

    def test_put_remotes_replaces_whole_list(self, client):
        tc, tmp_path, config = client
        resp = tc.put("/api/settings", json={"remotes": [
            {"name": "lab", "host": "lab.example.com", "user": "me"},
            {"name": "gpu", "host": "10.0.0.3", "port": 2200},
        ]})
        assert resp.status_code == 200
        assert [r["name"] for r in resp.json()["remotes"]] == ["lab", "gpu"]
        # config.yaml 落盘（唯一事实来源）
        text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "lab.example.com" in text

    def test_put_remotes_rejects_invalid(self, client):
        tc, tmp_path, config = client
        cases = [
            ("not-a-list", "必须是列表"),
            ([{"host": "h"}], "name 不能为空"),                     # 缺 name
            ([{"name": "x"}], "host 不能为空"),                     # 缺 host
            ([{"name": "x", "host": "a"}, {"name": "x", "host": "b"}],
             "重复名称"),                                           # 重名
            ([{"name": "x", "host": "h", "port": 70000}],
             "1~65535"),                                            # port 越界
        ]
        for bad, keyword in cases:
            resp = tc.put("/api/settings", json={"remotes": bad})
            assert resp.status_code == 400, f"应拒绝: {bad}"
            assert keyword in resp.json()["detail"]


class TestRemotesTestEndpoint:
    @staticmethod
    def _patch_run(monkeypatch, handler):
        """patch stdlib subprocess.run（remote_exec 经模块引用共享同一模块）。

        handler(argv, timeout, **kwargs) 按 argv[-1]（远端命令串）分流。
        """

        def fake_run(argv, timeout=None, **kwargs):
            return handler(argv, timeout, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)

    def test_connectivity_ok_and_zcode_ok(self, client, monkeypatch):
        tc, tmp_path, config = client

        def handler(argv, timeout, **kwargs):
            if argv[-1] == "echo ok":
                return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            return SimpleNamespace(returncode=0,
                                   stdout="zcode 1.2.3\n", stderr="")

        self._patch_run(monkeypatch, handler)
        resp = tc.post("/api/settings/remotes-test", json={"name": "build-server"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["connectivity"]["ok"] is True
        assert body["zcode"]["ok"] is True
        assert "1.2.3" in body["zcode"]["detail"]

    def test_connectivity_fail_short_circuits(self, client, monkeypatch):
        tc, tmp_path, config = client

        def handler(argv, timeout, **kwargs):
            return SimpleNamespace(returncode=255, stdout="",
                                   stderr="Permission denied (publickey)")

        self._patch_run(monkeypatch, handler)
        resp = tc.post("/api/settings/remotes-test", json={"name": "build-server"})
        body = resp.json()
        assert body["ok"] is False
        assert body["connectivity"]["ok"] is False
        assert "Permission denied" in body["connectivity"]["detail"]
        assert body["zcode"] is None

    def test_unknown_name_returns_error(self, client):
        tc, tmp_path, config = client
        resp = tc.post("/api/settings/remotes-test", json={"name": "nope"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "不存在" in resp.json()["error"]

    def test_unsaved_body_direct_test(self, client, monkeypatch):
        """未保存的主机：直接传完整字段测试（设置页编辑中测试）。"""
        tc, tmp_path, config = client

        def handler(argv, timeout, **kwargs):
            assert "10.9.8.7" in argv
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        self._patch_run(monkeypatch, handler)
        resp = tc.post("/api/settings/remotes-test",
                       json={"name": "temp", "host": "10.9.8.7"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
