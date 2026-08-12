"""git 凭据净化测试：失效 CI job token 不再污染 git fetch（部署后任务频繁失败）。

背景（根因）：gitlab-runner 部署时向 ~/.git-credentials（credential store）
写入 gitlab-ci-token 条目（CI job token），且 pm2 进程从构建目录启动、
继承了 CI_JOB_TOKEN 等 CI 环境变量。executor 的 git 子进程凭据解析顺序中
credential store 优先于 GIT_ASKPASS——失效 job token 命中后 GitLab 返回
403（job token 不允许跨项目访问，如 "from shipyard to project #123"），
而 git 对 403 不重试，GIT_ASKPASS 提供的 bot token 永远用不上 → fetch 必失败。

本测试用本地 HTTP 服务器（认证层 + git http-backend 代理）模拟 GitLab
远端：job token 一律 403，bot token 正常服务；断言 git 实际发送的凭据。
"""

import base64
import http.server
import os
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest

from botler.config import ConfigManager
from botler.database import Database
from botler.executor import ClaudeExecutor
from botler.gitlab_client import GitLabClient
from botler.templates import TemplateRenderer

CONFIG_TEXT = """\
gitlab:
  url: https://gitlab.example.com
  bot_token: bot-token
  webhook_secret: test-secret
  verify_ssl: false
worker: {}
claude: {}
templates: {}
repos: []
"""

BOT_BASIC = "Basic " + base64.b64encode(b"oauth2:bot-token").decode()
JOB_BASIC = "Basic " + base64.b64encode(b"gitlab-ci-token:stale-job-token").decode()


class _AuthGitHTTPHandler(http.server.BaseHTTPRequestHandler):
    """认证层 + git http-backend 代理：job token → 403（复现 GitLab 行为）。"""

    def _authorized(self) -> bool:
        """模拟真实 GitLab 认证时序：无凭据 → 401，job token → 403，bot token → 放行。"""
        auth = self.headers.get("Authorization", "")
        self.server.seen_auth.append(auth)
        if auth == BOT_BASIC:
            return True
        if auth == JOB_BASIC:
            # 与真实 GitLab 一致：job token 跨项目访问返回 403（git 不重试）
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        # 无凭据：401（必须带 WWW-Authenticate 头，与真实 GitLab 一致——
        # git 依据该头决定认证方式并重试）触发 git 获取凭据后重试
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="GitLab"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _proxy(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        env = {
            "GIT_PROJECT_ROOT": str(self.server.bare_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": parts.path,
            "QUERY_STRING": parts.query,
            "REQUEST_METHOD": self.command,
            "REMOTE_USER": "oauth2",
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
        }
        body = b""
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
        env["CONTENT_LENGTH"] = str(len(body))
        proc = subprocess.run(["git", "http-backend"], env={**os.environ, **env},
                              input=body, capture_output=True)
        head, _, payload = proc.stdout.partition(b"\r\n\r\n")
        if not payload:
            head, _, payload = proc.stdout.partition(b"\n\n")
        status, ctype = "200 OK", "text/plain"
        for line in head.splitlines():
            if line.lower().startswith(b"status:"):
                status = line.split(b":", 1)[1].strip().decode()
            elif line.lower().startswith(b"content-type:"):
                ctype = line.split(b":", 1)[1].strip().decode()
        self.send_response(int(status.split()[0]))
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self) -> None:
        if not self._authorized():
            self.send_response(403)  # 403：git 不会重试，与真实 GitLab 一致
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._proxy()

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def log_message(self, *args):
        pass


@pytest.fixture
def git_remote(tmp_path):
    """裸仓库 + http 认证代理服务器。返回 (url, seen_auth)。

    http-backend 的 GIT_PROJECT_ROOT 是仓库父目录，PATH_INFO=/demo.git
    定位到 demo.git 裸仓库。
    """
    root = tmp_path / "git-root"
    root.mkdir()
    subprocess.run(["git", "init", "--bare", str(root / "demo.git")],
                   check=True, capture_output=True)
    # 播种初始 commit（prepare_workspace 会 reset --hard origin/main）
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True,
                   capture_output=True)
    (seed / "f.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-m", "seed"], cwd=seed, check=True,
                   capture_output=True)
    subprocess.run(["git", "push", str(root / "demo.git"), "main"], cwd=seed,
                   check=True, capture_output=True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AuthGitHTTPHandler)
    server.bare_root = root
    server.seen_auth = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/demo.git"
    yield url, server.seen_auth
    server.shutdown()


@pytest.fixture
def executor(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    gitlab = GitLabClient("https://gitlab.example.com", "bot-token",
                          verify_ssl=False)
    renderer = TemplateRenderer(config)
    return ClaudeExecutor(config, db, gitlab, renderer,
                          workspace_root=str(tmp_path / "workspace"))


def _mk_work_repo(tmp_path: Path, remote_url: str) -> Path:
    """本地工作仓库：有 main 分支提交，remote 指向 http 测试服务器。"""
    workdir = tmp_path / "work"
    subprocess.run(["git", "init", "-b", "main", str(workdir)], check=True,
                   capture_output=True)
    (workdir / "a.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workdir, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-m", "init"], cwd=workdir, check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=workdir,
                   check=True, capture_output=True)
    return workdir


def _stale_store_config(tmp_path: Path, url: str, port: int) -> dict:
    """构造「失效 job token 在前、有效 oauth2 在后」的 credential store 场景。

    复现 ~/.git-credentials 被 gitlab-runner 写入 gitlab-ci-token 条目后
    （排在有效 oauth2 之前）的真实状态；全局 gitconfig 用 GIT_CONFIG_GLOBAL
    指向临时文件，避免污染真实 ~/.gitconfig。
    """
    creds = tmp_path / "git-credentials"
    creds.write_text(
        f"http://gitlab-ci-token:stale-job-token@127.0.0.1:{port}\n"
        f"http://oauth2:bot-token@127.0.0.1:{port}\n",
        encoding="utf-8")
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f"[credential]\n\thelper = store --file={creds}\n",
                         encoding="utf-8")
    return {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_SYSTEM": "/dev/null"}


class TestGitAuth:
    """prepare_workspace 的 git 子进程必须只使用 askpass 的 bot token。"""

    def test_fetch_uses_askpass_bot_token_not_stale_job_token(
            self, executor, git_remote, tmp_path, monkeypatch):
        """失效 job token 存于 store 时，fetch 仍用 askpass 的 bot token。

        修复前：store 命中 job token → 服务器 403 → git 不重试 → fetch 失败，
        且服务器只收到 job token 凭据（断言失败，复现 bug）。
        修复后：credential store 被禁用 → 仅 GIT_ASKPASS 提供凭据 → 服务器
        收到 bot token，fetch 成功。
        """
        url, seen_auth = git_remote
        port = urllib.parse.urlsplit(url).port
        workdir = _mk_work_repo(tmp_path, url)
        # 显式构造「失效 job token 在前」的 store 场景（与真实 ~/.git-credentials
        # 被 CI 作业写入 gitlab-ci-token 条目后的状态一致），不依赖机器真实配置
        stale_env = _stale_store_config(tmp_path, url, port)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", stale_env["GIT_CONFIG_GLOBAL"])
        monkeypatch.setenv("CI_JOB_TOKEN", "stale-job-token")
        monkeypatch.setenv("GITLAB_CI", "true")
        # 假 HOME：构造带 credential store 的全局 gitconfig（模拟真实 ~/.gitconfig）
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".gitconfig").write_text(
            "[user]\n\tname = t\n\temail = t@t\n"
            "[credential]\n\thelper = store --file=/tmp/never-used\n",
            encoding="utf-8")
        monkeypatch.setenv("HOME", str(fake_home))

        repo = {"name": "demo", "local_path": str(workdir),
                "url": url, "remote_name": "origin"}
        workdir2, git_env = executor.prepare_workspace(repo)

        # 修复后环境：全局 gitconfig 为剥离 credential 的净化版、剔除 CI 变量
        cleaned = Path(git_env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
        assert "credential" not in cleaned.lower(), "净化版 gitconfig 不应含 credential 配置"
        assert "name = t" in cleaned, "净化版应保留 user 等其他全局配置"
        assert git_env["GIT_CONFIG_GLOBAL"] != stale_env["GIT_CONFIG_GLOBAL"]
        assert "CI_JOB_TOKEN" not in git_env
        assert "GITLAB_CI" not in git_env
        assert workdir2 == workdir
        # git 实际发送的凭据必须是 bot token（oauth2:bot-token）：
        # 首次无凭据请求 → 401 → 禁用 store 后仅 askpass 提供凭据 → 重试成功
        assert seen_auth, "git 未发起任何认证请求"
        assert BOT_BASIC in seen_auth, \
            f"git 未使用 bot token: {seen_auth!r}"
        assert JOB_BASIC not in seen_auth, "git 使用了失效 job token"
        assert seen_auth[0] == "", "git 应首先发起无凭据请求（401 后重试）"

    def test_fetch_without_fix_uses_stale_job_token(self, git_remote,
                                                    tmp_path, monkeypatch):
        """对照：旧行为（env 直接继承 os.environ + 只设 GIT_ASKPASS）时，
        git 优先选用 store 中的失效 job token 而非 askpass 的 bot token。

        模拟修复前 executor 的 env 构造（不经过净化逻辑），断言服务器收到
        job token 凭据且 fetch 失败——证明 bug 根因与修复后行为的差异。
        """
        url, seen_auth = git_remote
        port = urllib.parse.urlsplit(url).port
        workdir = _mk_work_repo(tmp_path, url)
        stale_env = _stale_store_config(tmp_path, url, port)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", stale_env["GIT_CONFIG_GLOBAL"])
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", stale_env["GIT_CONFIG_SYSTEM"])
        monkeypatch.setenv("CI_JOB_TOKEN", "stale-job-token")
        monkeypatch.setenv("GITLAB_CI", "true")

        # 旧行为 env：dict(os.environ) + askpass/HOME（store 优先于 askpass）
        askpass = tmp_path / "askpass.sh"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in\n  *Username*) echo oauth2 ;;\n'
            '  *) echo bot-token ;;\nesac\n', encoding="utf-8")
        askpass.chmod(0o700)
        old_env = dict(os.environ)
        old_env["GIT_ASKPASS"] = str(askpass)
        old_env["GIT_TERMINAL_PROMPT"] = "0"
        old_env["HOME"] = str(Path.home())

        r = subprocess.run(
            ["git", "-c", "http.sslVerify=false", "fetch", "origin", "--prune"],
            cwd=workdir, env=old_env, capture_output=True, text=True, timeout=30)
        # 请求序列：无凭据 → 401 → store 提供失效 job token 重试 → 403 失败
        assert r.returncode != 0
        assert seen_auth == ["", JOB_BASIC], f"凭据序列异常: {seen_auth!r}"
        assert BOT_BASIC not in seen_auth, "askpass 的 bot token 不应被使用（store 抢了先）"

    def test_build_env_cleans_ci_variables(self, executor, tmp_path, monkeypatch):
        """claude 子进程环境同样净化：剔除 CI 变量、禁用全局 credential store。"""
        monkeypatch.setenv("CI_JOB_TOKEN", "stale-job-token")
        monkeypatch.setenv("GITLAB_CI", "true")
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".gitconfig").write_text(
            "[user]\n\tname = t\n\temail = t@t\n"
            "[credential]\n\thelper = store --file=/tmp/never-used\n",
            encoding="utf-8")
        monkeypatch.setenv("HOME", str(fake_home))
        repo = {"name": "demo", "prompt_template": None}
        issue = {"state": "opened", "title": "标题", "description": "正文",
                 "web_url": "https://gitlab.example.com/x/-/issues/7",
                 "project_id": 42, "iid": 7}
        env = executor._build_env(repo, issue)
        assert "CI_JOB_TOKEN" not in env
        assert "GITLAB_CI" not in env
        assert env["GITLAB_TOKEN"] == "bot-token"
        cleaned = Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
        assert "credential" not in cleaned.lower()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"].endswith(".botler-askpass-demo.sh")
