"""仓库 logo 生成 API 测试（issue #188）。

需求——仓库管理（设置）页面每个仓库的右侧增加「生成图标」按钮：点击后
agent 根据该仓库 README 生成 logo 提示词，用提示词调用生图模型生成
logo（简约美观大方）；生成的 logo 显示在仓库页面每个仓库的最左侧，可
点击放大并下载。

覆盖：
- POST /api/repos/{repo_id}/generate-logo 正常路径：本地 README 收集 →
  AI 生成提示词 → 生图模型生成 logo → 落盘（<LOGO_DIR>/<repo_id>.<ext>）
  → repos 表写 logo_path / logo_updated_at / logo_mime → 返回 201 与
  logo 元信息 + 生成提示词；
- README 收集兜底：无本地文件夹时走 GitLab 仓库 API 读 README；两处都
  没有时仅基于仓库元信息继续生成（提示词内容含「未能读取到 README」）；
- 读取接口 GET /api/repos/{repo_id}/logo：正常返回图片字节与正确
  Content-Type；?download=1 附加 Content-Disposition attachment；
- 边界：仓库不存在 404 / 软删除 400 / 未启用 400 / 未配置 AI 对话模型
  400 / 未配置生图模型 400 / AI 生成提示词失败与空回复 502 / 生图模型
  调用失败与未返回图片 502 / 未生成 logo 404 / logo 文件缺失 404；
- 重复生成覆盖同名文件；生成后 GET /api/repos 仓库列表带 logo 字段。
"""

import base64
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabError

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


@pytest.fixture
def api_app(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    ctx = SimpleNamespace(config=config, db=db, gitlab=StubGitLab(),
                          config_path=str(config_path))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return app, db


@pytest.fixture
def client(api_app):
    app, db = api_app
    return TestClient(app), db


def _add_repo(db, project_id, name, priority=100, enabled=True,
              remote_username=None, url=None, local_path=None,
              remote_name=None):
    """便捷：插入一个仓库并返回本地 id（与自省 API 测试同构）。"""
    return db.upsert_repo(
        project_id, name,
        url or f"https://gitlab.example.com/{name}.git",
        enabled=enabled, priority=priority, remote_username=remote_username,
        local_path=local_path, remote_name=remote_name)


def _write_project(tmp_path, subdir="proj"):
    """构造一个本地项目文件夹（README），返回路径。"""
    root = tmp_path / subdir
    root.mkdir(parents=True)
    (root / "README.md").write_text(
        "# 测试项目\n\n这是一个用于生成图标测试的示例项目，提供任务调度与 CI 集成。",
        encoding="utf-8")
    return root


class StubGitLab:
    """logo API 的 GitLab 桩：仓库文件树 / README 文件内容（GitLab 兜底
    上下文），可配置返回值与故障注入。"""

    def __init__(self):
        self.calls: list[str] = []
        self.tree_by_project: dict[int, list[dict]] = {}
        self.readme_by_project: dict[int, str] = {}
        self.fail_tree_projects: set[int] = set()

    def _request(self, method, path, params=None):
        self.calls.append(f"{method} {path}")
        if path.endswith("/repository/tree"):
            if int(path.split("/")[2]) in self.fail_tree_projects:
                raise GitLabError("模拟文件树读取故障")
            return list(self.tree_by_project.get(int(path.split("/")[2]), []))
        if "/repository/files/" in path:
            proj_id = int(path.split("/")[2])
            fname = path.rsplit("/", 1)[1]
            content = self.readme_by_project.get(proj_id)
            if fname == "README.md" and content:
                encoded = base64.b64encode(content.encode("utf-8")).decode()
                return {"content": encoded}
            return {"content": ""}
        raise AssertionError(f"未预期的 GitLab 请求: {method} {path}")

    # 下列方法仅需存在（_issue_create_client 可能触达），无实际调用断言
    def get_project(self, project_id):
        return {}

    def get_user_id_by_username(self, username):
        return None

    def create_issue(self, project_id, title, description=None,
                     assignee_id=None, labels=None):
        raise AssertionError("logo API 不应创建 issue")

    def list_open_issues(self, project_id, assignee_id=None, scope="all",
                         order_by=None, sort=None, limit=None):
        return []

    def list_project_labels(self, project_id):
        return []


class StubChatClient:
    """AI 生成提示词 ChatModelClient 桩：记录 chat 调用，可注入回复/故障。"""

    instances: list["StubChatClient"] = []
    reply: str = "a minimal flat geometric logo for a task scheduler, single accent color, no text"
    raise_error: Exception | None = None
    raise_http_error: bool = False

    def __init__(self, **kwargs):
        from botler.chat_models import DEFAULT_BASE_URLS, ChatModelError
        if str(kwargs.get("provider") or "") not in DEFAULT_BASE_URLS:
            raise ChatModelError(
                f"不支持的 AI 对话模型类型: {kwargs.get('provider')}")
        self.kwargs = kwargs
        self.chat_calls: list[list[dict]] = []
        StubChatClient.instances.append(self)

    def chat(self, messages):
        self.chat_calls.append(messages)
        if StubChatClient.raise_error is not None:
            raise StubChatClient.raise_error
        if StubChatClient.raise_http_error:
            raise httpx.ConnectError("模拟网络故障")
        return StubChatClient.reply


class StubImageModelClient:
    """生图 ImageModelClient 桩：记录 generate 调用，可注入结果/故障。"""

    instances: list["StubImageModelClient"] = []
    results: list = []
    raise_error: Exception | None = None
    raise_http_error: bool = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.generate_calls: list[tuple[str, dict]] = []
        StubImageModelClient.instances.append(self)

    def generate(self, prompt, image=None, **kwargs):
        self.generate_calls.append((prompt, kwargs))
        if StubImageModelClient.raise_error is not None:
            raise StubImageModelClient.raise_error
        if StubImageModelClient.raise_http_error:
            raise httpx.ConnectError("模拟网络故障")
        return StubImageModelClient.results


@pytest.fixture
def logo_env(client, tmp_path, monkeypatch):
    """logo 测试夹具：配置 AI 供应商 + 生图模型 + 打桩 ChatModelClient /
    ImageModelClient / LOGO_DIR，返回 (tc, stub_gitlab, db, logo_dir)。"""
    tc, db = client
    tc.app.state.ctx.config.update_ai_providers([{
        "name": "deepseek", "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-test", "model": "deepseek-chat", "enabled": True,
    }])
    tc.app.state.ctx.config.update_image_models([{
        "name": "gemini-nano", "provider": "gemini_nano_banana",
        "base_url": "", "api_key": "ai-test", "model": "",
        "enabled": True,
    }])
    from botler import chat_models as chat_mod
    StubChatClient.instances = []
    StubChatClient.reply = "a minimal flat geometric logo for a task scheduler, single accent color, no text"
    StubChatClient.raise_error = None
    StubChatClient.raise_http_error = False
    monkeypatch.setattr(chat_mod, "ChatModelClient", StubChatClient)

    from botler import image_models as image_mod
    from botler.plugins.models import ImageResult
    StubImageModelClient.instances = []
    StubImageModelClient.results = [
        ImageResult(mime_type="image/png", data=b"fake-logo-png-bytes")]
    StubImageModelClient.raise_error = None
    StubImageModelClient.raise_http_error = False
    monkeypatch.setattr(image_mod, "ImageModelClient", StubImageModelClient)

    from botler.api import repo_logo as repo_logo_mod
    logo_dir = tmp_path / "logos"
    monkeypatch.setattr(repo_logo_mod, "LOGO_DIR", logo_dir)

    from botler.api import issues as issues_mod
    monkeypatch.setattr(
        issues_mod, "GitLabClient",
        lambda url, token, verify_ssl=True, webhook_base_url=None: tc.app.state.ctx.gitlab)
    return tc, tc.app.state.ctx.gitlab, db, logo_dir


class TestValidation:
    """仓库校验与前置条件。"""

    def test_repo_not_found(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        r = tc.post("/api/repos/999/generate-logo")
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_repo_soft_deleted(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        repo_id = _add_repo(db, 42, "botler")
        db.soft_delete_repo(repo_id)
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 400
        assert "已删除" in r.json()["detail"]

    def test_repo_disabled(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        repo_id = _add_repo(db, 42, "botler", enabled=False)
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 400
        assert "未启用" in r.json()["detail"]

    def test_without_ai_provider(self, client):
        """未配置 AI 对话模型：400 引导设置页（生图模型已配置也先拦）。"""
        tc, db = client
        tc.app.state.ctx.config.update_image_models([{
            "name": "gemini-nano", "provider": "gemini_nano_banana",
            "base_url": "", "api_key": "ai-test", "model": "",
            "enabled": True,
        }])
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 400
        assert "AI 对话模型" in r.json()["detail"]

    def test_without_image_model(self, client):
        """未配置生图模型：400 引导设置页「生图模型」配置。"""
        tc, db = client
        tc.app.state.ctx.config.update_ai_providers([{
            "name": "deepseek", "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test", "model": "deepseek-chat", "enabled": True,
        }])
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 400
        assert "生图模型" in r.json()["detail"]


class TestGenerateLogo:
    """正常路径与 README 收集策略。"""

    def test_success_local_readme(self, logo_env, tmp_path):
        """本地 README：AI 提示词含 README 内容，生图模型收到该提示词，
        logo 落盘、repos 表写元信息，读取接口返回图片。"""
        tc, stub, db, logo_dir = logo_env
        root = _write_project(tmp_path)
        repo_id = _add_repo(db, 42, "botler", local_path=str(root))
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["logo_path"] == f"{repo_id}.png"
        assert data["logo_mime"] == "image/png"
        assert data["logo_updated_at"]
        assert data["size"] == len(b"fake-logo-png-bytes")
        assert data["logo_prompt"] == StubChatClient.reply

        # AI 提示词用户消息应含 README 内容（本地优先）
        chat_msgs = StubChatClient.instances[0].chat_calls[0]
        user_content = chat_msgs[1]["content"]
        assert "测试项目" in user_content
        assert "README" in user_content
        # 生图模型收到的 prompt = AI 生成的提示词
        assert StubImageModelClient.instances[0].generate_calls[0][0] == StubChatClient.reply

        # logo 文件落盘
        assert (logo_dir / f"{repo_id}.png").read_bytes() == b"fake-logo-png-bytes"
        # repos 表写入元信息
        row = db.get_repo(repo_id)
        assert row["logo_path"] == f"{repo_id}.png"
        assert row["logo_mime"] == "image/png"
        assert row["logo_updated_at"]

        # 读取接口：图片字节 + 正确 Content-Type
        r2 = tc.get(f"/api/repos/{repo_id}/logo")
        assert r2.status_code == 200
        assert r2.headers["content-type"] == "image/png"
        assert r2.content == b"fake-logo-png-bytes"
        # 下载参数：Content-Disposition attachment
        r3 = tc.get(f"/api/repos/{repo_id}/logo?download=1")
        assert r3.status_code == 200
        assert "attachment" in r3.headers["content-disposition"]
        assert "botler-logo.png" in r3.headers["content-disposition"]

    def test_success_gitlab_readme_fallback(self, logo_env):
        """无本地文件夹：GitLab 仓库 API 兜底读 README。"""
        tc, stub, db, logo_dir = logo_env
        stub.readme_by_project[42] = (
            "# 远程项目\n\n远程 README 中的项目说明，用于 GitLab 兜底测试。")
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 201, r.text
        user_content = StubChatClient.instances[0].chat_calls[0][1]["content"]
        assert "远程项目" in user_content
        assert "README" in user_content
        assert (logo_dir / f"{repo_id}.png").is_file()

    def test_success_without_readme(self, logo_env):
        """本地与 GitLab 都无 README：仅基于仓库元信息继续生成。"""
        tc, stub, db, logo_dir = logo_env
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 201, r.text
        user_content = StubChatClient.instances[0].chat_calls[0][1]["content"]
        assert "未能读取到该仓库的 README.md" in user_content
        assert (logo_dir / f"{repo_id}.png").is_file()

    def test_readme_collect_failure_falls_back(self, logo_env):
        """GitLab 文件树读取故障：不阻塞，仅基于元信息继续生成。"""
        tc, stub, db, logo_dir = logo_env
        stub.fail_tree_projects.add(42)
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 201, r.text
        assert "未能读取到该仓库的 README.md" in (
            StubChatClient.instances[0].chat_calls[0][1]["content"])

    def test_regenerate_overwrites_same_file(self, logo_env):
        """重复生成：同名文件覆盖，目录内仍只有一份 logo。"""
        tc, stub, db, logo_dir = logo_env
        repo_id = _add_repo(db, 42, "botler")
        r1 = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r1.status_code == 201
        r2 = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r2.status_code == 201
        assert r2.json()["logo_path"] == r1.json()["logo_path"]
        assert list(logo_dir.iterdir()) == [logo_dir / f"{repo_id}.png"]

    def test_repos_list_returns_logo_fields(self, logo_env):
        """生成后 GET /api/repos 仓库列表应带 logo 元信息字段。"""
        tc, stub, db, logo_dir = logo_env
        repo_id = _add_repo(db, 42, "botler")
        tc.post(f"/api/repos/{repo_id}/generate-logo")
        r = tc.get("/api/repos")
        assert r.status_code == 200
        repo = next(x for x in r.json()["repos"] if x["id"] == repo_id)
        assert repo["logo_path"] == f"{repo_id}.png"
        assert repo["logo_mime"] == "image/png"
        assert repo["logo_updated_at"]


class TestErrors:
    """AI / 生图链路故障。"""

    def test_chat_error(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        from botler.chat_models import ChatModelError
        StubChatClient.raise_error = ChatModelError("模拟 AI 故障")
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 502
        assert "AI 生成提示词失败" in r.json()["detail"]

    def test_chat_http_error(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        StubChatClient.raise_http_error = True
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 502
        assert "网络错误" in r.json()["detail"]

    def test_chat_empty_reply(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        StubChatClient.reply = "   "
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 502
        assert "提示词为空" in r.json()["detail"]

    def test_image_model_error(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        from botler.image_models import ImageModelError
        StubImageModelClient.raise_error = ImageModelError("模拟生图故障")
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 502
        assert "生图模型调用失败" in r.json()["detail"]

    def test_image_model_empty_results(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        StubImageModelClient.results = []
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 502
        assert "未返回图片数据" in r.json()["detail"]


class TestGetLogo:
    """logo 读取接口边界。"""

    def test_repo_not_found(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        r = tc.get("/api/repos/999/logo")
        assert r.status_code == 404

    def test_not_generated(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        repo_id = _add_repo(db, 42, "botler")
        r = tc.get(f"/api/repos/{repo_id}/logo")
        assert r.status_code == 404
        assert "尚未生成 logo" in r.json()["detail"]

    def test_logo_file_missing(self, logo_env):
        """DB 有 logo_path 但文件被删：404 引导重新生成。"""
        tc, stub, db, logo_dir = logo_env
        repo_id = _add_repo(db, 42, "botler")
        db.update_repo(repo_id, logo_path=f"{repo_id}.png",
                       logo_updated_at="2026-08-18 10:00:00",
                       logo_mime="image/png")
        r = tc.get(f"/api/repos/{repo_id}/logo")
        assert r.status_code == 404
        assert "logo 文件不存在" in r.json()["detail"]
