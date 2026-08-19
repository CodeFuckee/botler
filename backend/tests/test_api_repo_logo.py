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
- 同步上传前压缩 + 格式归一（issue #310/#318）：logo 超过 GitLab 项目头像
  200KB 上限时，sync-logo 上传前用 Pillow 压缩到 200KB 以内（本地文件保持
  原始质量）；GitLab 头像不支持 WebP/AVIF/SVG（400「file format is not
  supported」），不受支持格式（或超限图片）统一转码为 png/jpeg/gif/bmp/tiff/
  vnd.microsoft.icon（含透明 → PNG，禁止 WebP），仅对阈值内且格式受支持的
  图片原样直通（mime 按真实内容校正）；Pillow 缺失/图片无法解码回退原始字节。
"""

import base64
from pathlib import Path
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
        # issue #297：项目头像上传调用记录与故障注入
        self.avatar_calls: list[tuple[int, str, bytes, str]] = []
        self.avatar_error: GitLabError | None = None
        self.avatar_result: dict | None = None
        # issue #320：项目头像拉取（GitLab → 本地）桩
        self.avatar_download_calls: list[int] = []
        self.avatar_download: tuple[bytes, str, str] | None = None
        self.avatar_download_error: GitLabError | None = None

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

    def update_project_avatar(self, project_id, filename, data, mime):
        """issue #297：记录项目头像上传调用，可注入 GitLab 故障。"""
        self.avatar_calls.append((project_id, filename, data, mime))
        if self.avatar_error is not None:
            raise self.avatar_error
        return self.avatar_result

    def get_project_avatar(self, project_id):
        """issue #320：记录项目头像拉取调用，可注入 GitLab 故障/无图标。"""
        self.avatar_download_calls.append(project_id)
        if self.avatar_download_error is not None:
            raise self.avatar_download_error
        return self.avatar_download

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
    tc.app.state.ctx.config.update_section("ai_providers", [{
        "name": "deepseek", "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-test", "model": "deepseek-chat", "enabled": True,
    }])
    tc.app.state.ctx.config.update_section("image_models", [{
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
        tc.app.state.ctx.config.update_section("image_models", [{
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
        tc.app.state.ctx.config.update_section("ai_providers", [{
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


class TestSyncLogo:
    """同步 logo 到 GitLab 作为项目图标（issue #297）。

    覆盖：
    - 正常路径：已有 logo 的仓库调 POST /api/repos/{id}/sync-logo → 读
      本地 logo 文件 → GitLab update_project_avatar 收到正确的
      project_id / 文件名 / 字节 / mime → 返回 ok + 项目路径 + avatar_url；
    - 边界：仓库不存在 404 / 软删除 400 / 尚未生成 logo 400 / logo 文件
      缺失 404 / GitLab 调用失败 502；
    - 与生成图标解耦：不配置 AI / 生图模型也能同步（仅依赖已落盘的 logo）。
    """

    def _add_logo_repo(self, db, logo_dir, project_id=42, name="botler",
                       logo="42.png", mime="image/png"):
        """便捷：插入仓库并写入 logo 元信息 + 落盘 logo 文件。"""
        repo_id = _add_repo(db, project_id, name)
        db.update_repo(repo_id, logo_path=logo,
                       logo_updated_at="2026-08-18 10:00:00", logo_mime=mime)
        logo_dir.mkdir(parents=True, exist_ok=True)
        (logo_dir / logo).write_bytes(b"fake-logo-png-bytes")
        return repo_id

    def test_success(self, logo_env):
        """正常路径：logo 文件经 GitLab 上传为项目头像，返回项目信息。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_result = {
            "path_with_namespace": "chenkaidi/botler",
            "avatar_url": "https://gitlab.example.com/uploads/-/system/project/avatar/42/42.png",
        }
        repo_id = self._add_logo_repo(db, logo_dir)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["project"] == "chenkaidi/botler"
        assert data["avatar_url"].endswith("42.png")
        # 上传参数：project_id / 文件名 / 文件字节 / mime 一一对应
        assert stub.avatar_calls == [(42, "42.png", b"fake-logo-png-bytes", "image/png")]

    def test_success_without_ai_or_image_model(self, logo_env):
        """同步不依赖 AI/生图模型配置（仅使用已落盘的 logo 文件）。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_result = {"path_with_namespace": "chenkaidi/botler"}
        repo_id = self._add_logo_repo(db, logo_dir)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

    def test_repo_not_found(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        r = tc.post("/api/repos/999/sync-logo")
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_repo_soft_deleted(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        repo_id = self._add_logo_repo(db, logo_dir)
        db.soft_delete_repo(repo_id)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 400
        assert "已删除" in r.json()["detail"]

    def test_not_generated(self, logo_env):
        """尚未生成 logo：400 引导先点「生成图标」。"""
        tc, stub, db, logo_dir = logo_env
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 400
        assert "尚未生成 logo" in r.json()["detail"]

    def test_logo_file_missing(self, logo_env):
        """DB 有 logo_path 但文件被删：404 引导重新生成。"""
        tc, stub, db, logo_dir = logo_env
        repo_id = _add_repo(db, 42, "botler")
        db.update_repo(repo_id, logo_path="42.png",
                       logo_updated_at="2026-08-18 10:00:00",
                       logo_mime="image/png")
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 404
        assert "logo 文件不存在" in r.json()["detail"]

    def test_gitlab_error(self, logo_env):
        """GitLab 上传失败（如权限不足/格式不支持）：502 透传错误信息。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_error = GitLabError(
            "GitLab API 错误 403: Avatar is not allowed", 403)
        repo_id = self._add_logo_repo(db, logo_dir)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 502
        assert "同步到 GitLab 失败" in r.json()["detail"]
        assert "403" in r.json()["detail"]

    def test_read_file_failure(self, logo_env, monkeypatch):
        """logo 文件读取失败：500 兜底（monkeypatch read_bytes 抛 OSError）。"""
        from pathlib import Path
        tc, stub, db, logo_dir = logo_env
        repo_id = self._add_logo_repo(db, logo_dir)

        def boom(self):
            raise OSError("模拟读取失败")

        monkeypatch.setattr(Path, "read_bytes", boom)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 500
        assert "读取 logo 文件失败" in r.json()["detail"]


class TestSyncLogoFromGitlab:
    """把 GitLab 项目图标（头像）同步回仓库设置页（issue #320）。

    与 TestSyncLogo（本地 logo → GitLab）方向相反：读取 GitLab 项目当前
    图标，落盘为本地 logo 并写 repos 表，仓库设置页最左侧即展示 GitLab
    图标。

    覆盖：
    - 正常路径：GitLab 侧已设置图标 → 字节落盘 <LOGO_DIR>/<repo_id>.<ext>
      → repos 表写 logo_path / logo_updated_at / logo_mime → 返回 ok +
      logo 元信息 + 来源 avatar_url；GET /api/repos/{id}/logo 能读到该图
      标，仓库列表带 logo 字段；
    - 覆盖已有本地 logo：本地已生成 logo 时拉取 GitLab 图标会覆盖同名
      文件并更新 logo_updated_at（GitLab 图标优先级）；
    - 边界：GitLab 侧未设置图标（avatar_url 为空 → None）400 / 仓库不
      存在 404 / 软删除 400 / GitLab 调用失败 502；
    - 与生成图标解耦：不配置 AI / 生图模型也能拉取（仅依赖 GitLab 头像）。
    """

    def _avatar_url(self, project_id=42, filename="1.png"):
        return (f"https://gitlab.example.com/uploads/-/system/project/"
                f"avatar/{project_id}/{filename}")

    def test_success(self, logo_env):
        """正常路径：GitLab 图标落盘并写 repos 表，读取接口返回图片。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_download = (
            b"gitlab-avatar-png-bytes", "image/png", self._avatar_url())
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/sync-logo-from-gitlab")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["logo_path"] == f"{repo_id}.png"
        assert data["logo_mime"] == "image/png"
        assert data["logo_updated_at"]
        assert data["size"] == len(b"gitlab-avatar-png-bytes")
        assert data["avatar_url"].endswith("42/1.png")
        # 拉取的是 GitLab 项目 id（非平台仓库 id）
        assert stub.avatar_download_calls == [42]
        # 落盘文件内容正确
        assert (logo_dir / f"{repo_id}.png").read_bytes() == b"gitlab-avatar-png-bytes"
        # 读取接口返回拉取到的图标
        r2 = tc.get(f"/api/repos/{repo_id}/logo")
        assert r2.status_code == 200
        assert r2.content == b"gitlab-avatar-png-bytes"
        assert r2.headers["content-type"] == "image/png"
        # 仓库列表带 logo 字段
        r3 = tc.get("/api/repos")
        row = next(x for x in r3.json()["repos"] if x["id"] == repo_id)
        assert row["logo_path"] == f"{repo_id}.png"
        assert row["logo_mime"] == "image/png"

    def test_success_without_ai_or_image_model(self, logo_env):
        """拉取不依赖 AI/生图模型配置（仅依赖 GitLab 头像）。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_download = (
            b"gitlab-avatar-png-bytes", "image/png", self._avatar_url())
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/sync-logo-from-gitlab")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

    def test_overwrites_existing_local_logo(self, logo_env):
        """本地已生成 logo 时，拉取 GitLab 图标覆盖同名文件并更新时间。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_download = (
            b"gitlab-avatar-new", "image/png", self._avatar_url())
        repo_id = _add_repo(db, 42, "botler")
        db.update_repo(repo_id, logo_path=f"{repo_id}.png",
                       logo_updated_at="2026-08-18 10:00:00",
                       logo_mime="image/png")
        logo_dir.mkdir(parents=True, exist_ok=True)
        (logo_dir / f"{repo_id}.png").write_bytes(b"old-local-logo")
        r = tc.post(f"/api/repos/{repo_id}/sync-logo-from-gitlab")
        assert r.status_code == 200, r.text
        # 同名文件被 GitLab 图标覆盖
        assert (logo_dir / f"{repo_id}.png").read_bytes() == b"gitlab-avatar-new"
        row = db.get_repo(repo_id)
        assert row["logo_path"] == f"{repo_id}.png"
        assert row["logo_mime"] == "image/png"
        # logo_updated_at 更新（前端据此击穿缓存）
        assert row["logo_updated_at"] != "2026-08-18 10:00:00"

    def test_gitlab_avatar_missing(self, logo_env):
        """GitLab 项目未设置图标（avatar_url 为空）：400 引导先上传。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_download = None
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/sync-logo-from-gitlab")
        assert r.status_code == 400
        assert "未设置图标" in r.json()["detail"]
        # 未落盘任何 logo
        assert db.get_repo(repo_id)["logo_path"] is None

    def test_repo_not_found(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        r = tc.post("/api/repos/999/sync-logo-from-gitlab")
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_repo_soft_deleted(self, logo_env):
        tc, stub, db, logo_dir = logo_env
        repo_id = _add_repo(db, 42, "botler")
        db.soft_delete_repo(repo_id)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo-from-gitlab")
        assert r.status_code == 400
        assert "已删除" in r.json()["detail"]

    def test_gitlab_error(self, logo_env):
        """GitLab 查询/下载失败（如权限不足）：502 透传错误信息。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_download_error = GitLabError(
            "GitLab API 错误 403: Forbidden", 403)
        repo_id = _add_repo(db, 42, "botler")
        r = tc.post(f"/api/repos/{repo_id}/sync-logo-from-gitlab")
        assert r.status_code == 502
        assert "从 GitLab 同步图标失败" in r.json()["detail"]
        assert "403" in r.json()["detail"]


class TestAvatarMimeFromResponse:
    """头像 MIME 推测辅助（issue #320）：具体 Content-Type 优先，泛型
    octet-stream 按 Content-Disposition 文件名 / avatar_url 扩展名推测，
    兜底 image/png。"""

    def _resp(self, content_type="application/octet-stream",
              disposition=None):
        return httpx.Response(
            200, content=b"x",
            headers={k: v for k, v in {
                "content-type": content_type,
                "content-disposition": disposition,
            }.items() if v is not None})

    def test_specific_content_type_wins(self):
        from botler.gitlab_client import _avatar_mime_from_response
        resp = self._resp(content_type="image/jpeg; charset=utf-8")
        assert _avatar_mime_from_response(resp, "https://x/1.png") == "image/jpeg"

    def test_octet_stream_uses_disposition_filename(self):
        from botler.gitlab_client import _avatar_mime_from_response
        resp = self._resp(
            disposition='attachment; filename="1.png"; filename*=UTF-8''1.png')
        assert _avatar_mime_from_response(resp, "https://x/1") == "image/png"

    def test_octet_stream_uses_avatar_url_ext(self):
        from botler.gitlab_client import _avatar_mime_from_response
        resp = self._resp()
        assert _avatar_mime_from_response(
            resp, "https://gitlab.example.com/uploads/avatar/42/1.jpg") == "image/jpeg"

    def test_unknown_ext_falls_back_png(self):
        from botler.gitlab_client import _avatar_mime_from_response
        resp = self._resp()
        assert _avatar_mime_from_response(resp, "https://x/avatar") == "image/png"

    """同步 logo 上传前压缩（issue #310）。

    背景：GitLab 项目头像上传上限 200KB（PUT /projects/{id} 的 avatar
    参数），生图模型产出的 logo（尤其 PNG）经常超过该阈值，超限上传被
    GitLab 拒绝 → 仓库设置页「同步到 GitLab」失败。修复：sync-logo
    上传前用 Pillow 把图片压缩到 200KB 以内（本地 logo 文件保持原始
    质量不变）。

    覆盖：
    - 超限 PNG logo → 上传到 GitLab 的字节被压缩进 200KB 阈值内且仍是
      可解码图片，mime 与上传文件名扩展名一致；
    - 阈值内 logo → 不压缩不转码，字节与 mime 原样直通；
    - Pillow 不可用 → 回退上传原始字节（交由 GitLab 报错兜底）；
    - 图片无法解码 → 回退上传原始字节，不崩溃。
    """

    GITLAB_AVATAR_LIMIT = 200 * 1024

    @staticmethod
    def _make_large_png(width=800, height=800, seed=42) -> bytes:
        """构造确定性高熵 PNG（随机像素，PNG 压缩率差），保证文件大小
        显著超过 GitLab 200KB 头像上限，作为压缩复现输入。"""
        from PIL import Image
        import io
        import random as _random
        rng = _random.Random(seed)
        img = Image.new("RGB", (width, height))
        img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                     for _ in range(width * height)])
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _add_large_logo_repo(self, db, logo_dir, data, project_id=42,
                             name="botler"):
        """便捷：插入仓库 + logo 元信息 + 落盘指定 logo 文件（超限用）。"""
        repo_id = _add_repo(db, project_id, name)
        db.update_repo(repo_id, logo_path="42.png",
                       logo_updated_at="2026-08-18 10:00:00",
                       logo_mime="image/png")
        logo_dir.mkdir(parents=True, exist_ok=True)
        (logo_dir / "42.png").write_bytes(data)
        return repo_id

    def test_large_logo_compressed_below_gitlab_limit(self, logo_env):
        """超限 logo：上传字节 ≤ 200KB 且仍是可解码图片；mime 与上传
        文件名扩展名一致（格式可能从 PNG 转为 JPEG/WebP）。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_result = {
            "path_with_namespace": "chenkaidi/botler",
            "avatar_url": "https://gitlab.example.com/uploads/-/system/project/avatar/42/42.png",
        }
        large = self._make_large_png()
        # 复现前提：构造的输入确实超过 GitLab 头像上限
        assert len(large) > self.GITLAB_AVATAR_LIMIT
        repo_id = self._add_large_logo_repo(db, logo_dir, large)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        pid, filename, data, mime = stub.avatar_calls[0]
        assert pid == 42
        assert len(data) <= self.GITLAB_AVATAR_LIMIT
        # 压缩结果仍是合法图片
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data))
        im.load()
        # 上传格式必须是 GitLab 头像支持的格式（issue #318）：禁止 WebP
        from botler.api.repo_logo import GITLAB_AVATAR_MIMES
        assert mime in GITLAB_AVATAR_MIMES, mime
        # mime 与上传文件名扩展名一致（压缩可能转换格式）
        if mime == "image/jpeg":
            assert filename.endswith(".jpg"), filename
        elif mime == "image/bmp":
            assert filename.endswith(".bmp"), filename
        elif mime == "image/tiff":
            assert filename.endswith(".tiff"), filename
        elif mime == "image/vnd.microsoft.icon":
            assert filename.endswith(".ico"), filename
        else:
            assert filename.endswith(".png"), filename

    def test_within_limit_logo_unchanged(self, logo_env):
        """阈值内 logo：不压缩不转码，字节与 mime 原样直通。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_result = {"path_with_namespace": "chenkaidi/botler"}
        small = self._make_large_png(64, 64)
        assert len(small) <= self.GITLAB_AVATAR_LIMIT
        repo_id = self._add_large_logo_repo(db, logo_dir, small)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 200, r.text
        assert stub.avatar_calls == [(42, "42.png", small, "image/png")]

    def test_pillow_missing_falls_back_to_original(self, logo_env, monkeypatch):
        """Pillow 不可用：回退上传原始字节，不崩溃（GitLab 报错兜底）。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_result = {"path_with_namespace": "chenkaidi/botler"}
        large = self._make_large_png()
        repo_id = self._add_large_logo_repo(db, logo_dir, large)
        # 模拟 Pillow 缺失：import PIL 抛 ImportError
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("No module named 'PIL'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 200, r.text
        assert stub.avatar_calls == [(42, "42.png", large, "image/png")]

    def test_corrupt_image_falls_back_to_original(self, logo_env):
        """logo 文件无法解码（损坏/非图片）：回退上传原始字节，不崩溃。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_result = {"path_with_namespace": "chenkaidi/botler"}
        corrupt = b"not-an-image" * 30000  # ~330KB 垃圾字节，超过 200KB 阈值
        repo_id = self._add_large_logo_repo(db, logo_dir, corrupt)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 200, r.text
        assert stub.avatar_calls == [(42, "42.png", corrupt, "image/png")]

    def test_webp_logo_converted_to_supported_format(self, logo_env):
        """WebP logo（阈值内）同步：GitLab 头像不支持 WebP（issue #318），
        必须转码为 GitLab 支持格式（PNG/JPEG），不能原样直通。"""
        tc, stub, db, logo_dir = logo_env
        stub.avatar_result = {"path_with_namespace": "chenkaidi/botler"}
        import io
        from PIL import Image
        img = Image.new("RGBA", (64, 64), (30, 144, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="WEBP")
        webp = buf.getvalue()
        assert len(webp) <= self.GITLAB_AVATAR_LIMIT  # 阈值内
        repo_id = _add_repo(db, 42, "botler")
        db.update_repo(repo_id, logo_path="42.webp",
                       logo_updated_at="2026-08-19 10:00:00",
                       logo_mime="image/webp")
        logo_dir.mkdir(parents=True, exist_ok=True)
        (logo_dir / "42.webp").write_bytes(webp)
        r = tc.post(f"/api/repos/{repo_id}/sync-logo")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        from botler.api.repo_logo import GITLAB_AVATAR_MIMES
        pid, filename, data, mime = stub.avatar_calls[0]
        assert pid == 42
        assert mime in GITLAB_AVATAR_MIMES, mime
        assert mime != "image/webp"
        assert filename.endswith((".png", ".jpg"))
        im = Image.open(io.BytesIO(data))
        im.load()


class TestCompressImageForGitlab:
    """_compress_image_for_gitlab 单元测试（issue #310）。

    与集成测试互补：直接验证压缩函数本身的边界行为（阈值直通、超限
    压缩、格式转换保留透明、解码失败回退）。
    """

    GITLAB_AVATAR_LIMIT = 200 * 1024

    @staticmethod
    def _make_large_png(width=800, height=800, seed=42) -> bytes:
        from PIL import Image
        import io
        import random as _random
        rng = _random.Random(seed)
        img = Image.new("RGB", (width, height))
        img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                     for _ in range(width * height)])
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_within_limit_passthrough(self):
        """阈值内：字节与 mime 原样返回（不转码不压缩）。"""
        from botler.api.repo_logo import _compress_image_for_gitlab
        data = b"tiny-png"
        out, mime = _compress_image_for_gitlab(data, "image/png")
        assert out == data
        assert mime == "image/png"

    def test_small_webp_converted_to_supported_format(self):
        """阈值内 WebP：GitLab 头像不支持 WebP（issue #318），即使未超限
        也必须转码为支持格式（PNG/JPEG），不能原样直通。"""
        from botler.api.repo_logo import (
            GITLAB_AVATAR_LIMIT, GITLAB_AVATAR_MIMES,
            _compress_image_for_gitlab,
        )
        import io
        from PIL import Image
        img = Image.new("RGBA", (64, 64), (30, 144, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="WEBP")
        data = buf.getvalue()
        assert len(data) <= GITLAB_AVATAR_LIMIT  # 阈值内，尺寸不是问题
        out, mime = _compress_image_for_gitlab(data, "image/webp")
        assert mime in GITLAB_AVATAR_MIMES, mime
        assert mime != "image/webp"
        im = Image.open(io.BytesIO(out))
        im.load()

    def test_small_png_with_mislabeled_mime_corrected(self):
        """存储 mime 与实际内容不一致（声称 webp 实为 PNG）：按真实格式
        直通并校正 mime（不转码不压缩，字节不变）。"""
        from botler.api.repo_logo import _compress_image_for_gitlab
        import io
        from PIL import Image
        img = Image.new("RGB", (32, 32), (30, 144, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        out, mime = _compress_image_for_gitlab(data, "image/webp")
        assert out == data  # 字节原样直通
        assert mime == "image/png"

    def test_large_png_compressed_within_limit(self):
        """超限 PNG：输出 ≤ 目标大小且仍是可解码图片，mime 合法。"""
        from botler.api.repo_logo import (
            GITLAB_AVATAR_LIMIT, _compress_image_for_gitlab,
        )
        import io
        from PIL import Image
        large = self._make_large_png()
        assert len(large) > GITLAB_AVATAR_LIMIT
        out, mime = _compress_image_for_gitlab(large, "image/png")
        assert len(out) <= GITLAB_AVATAR_LIMIT
        # issue #318：输出格式必须在 GitLab 头像支持集合内（禁止 WebP）
        from botler.api.repo_logo import GITLAB_AVATAR_MIMES
        assert mime in GITLAB_AVATAR_MIMES, mime
        im = Image.open(io.BytesIO(out))
        im.load()

    def test_rgba_preserves_transparency_via_png(self):
        """含透明通道的 PNG：压缩输出 PNG（保留 alpha 且是 GitLab 支持
        格式），禁止转 WebP（issue #318，GitLab 头像不支持 WebP）。"""
        from botler.api.repo_logo import (
            GITLAB_AVATAR_LIMIT, _compress_image_for_gitlab,
        )
        import io
        import random as _random
        from PIL import Image
        rng = _random.Random(7)
        img = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
        for x in range(0, 500, 2):
            for y in range(0, 500, 2):
                img.putpixel((x, y), (rng.randrange(256), rng.randrange(256),
                                      rng.randrange(256), rng.randrange(256)))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        rgba = buf.getvalue()
        assert len(rgba) > GITLAB_AVATAR_LIMIT
        out, mime = _compress_image_for_gitlab(rgba, "image/png")
        assert mime == "image/png"
        assert len(out) <= GITLAB_AVATAR_LIMIT
        im = Image.open(io.BytesIO(out))
        im.load()
        assert im.mode == "RGBA"

    def test_corrupt_image_falls_back(self):
        """无法解码：原样返回，不抛异常。"""
        from botler.api.repo_logo import _compress_image_for_gitlab
        bad = b"x" * (300 * 1024)
        out, mime = _compress_image_for_gitlab(bad, "image/png")
        assert out == bad
        assert mime == "image/png"

    def test_large_jpeg_stays_jpeg(self):
        """超限 JPEG：保持 JPEG 格式逐级降质量/降分辨率压缩进 200KB
        （JPEG 本身是 GitLab 支持格式，无需转码）。"""
        from botler.api.repo_logo import (
            GITLAB_AVATAR_LIMIT, _compress_image_for_gitlab,
        )
        import io
        import random as _random
        from PIL import Image
        rng = _random.Random(1)
        img = Image.new("RGB", (800, 800))
        img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                     for _ in range(800 * 800)])
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        data = buf.getvalue()
        assert len(data) > GITLAB_AVATAR_LIMIT
        out, mime = _compress_image_for_gitlab(data, "image/jpeg")
        assert mime == "image/jpeg"
        assert len(out) <= GITLAB_AVATAR_LIMIT
        im = Image.open(io.BytesIO(out))
        im.load()

    def test_pillow_missing_falls_back(self, monkeypatch):
        """Pillow 缺失：原样返回，不抛异常。"""
        from botler.api.repo_logo import _compress_image_for_gitlab
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("No module named 'PIL'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        data = b"y" * (300 * 1024)
        out, mime = _compress_image_for_gitlab(data, "image/png")
        assert out == data
        assert mime == "image/png"


class TestLogoDirPersistence:
    """logo 落盘目录解析（issue #309 回归）。

    背景：生产 pm2 部署运行在 gitlab-runner 构建目录（backend/data 随
    构建目录轮换被清理），而数据库（持久 data/backend/botler.db）仍
    保留 logo_path 元信息 → 部署后 GET /api/repos/{id}/logo 返回 404，
    仓库设置页「生成图标」生成的 logo 在其他设备/新部署上看不到。
    修复：LOGO_DIR 在未显式指定 BOTLER_LOGO_DIR 时，优先解析到持久
    数据目录 BOTLER_DATA_DIR/backend/data/logos（pm2 部署已注入
    BOTLER_DATA_DIR），而不是构建目录 backend/data/logos。
    """

    def test_resolve_prefers_explicit_logo_dir(self, monkeypatch):
        """显式 BOTLER_LOGO_DIR 优先级最高（测试注入 / 手动覆盖入口）。"""
        from botler.api import repo_logo
        monkeypatch.setenv("BOTLER_LOGO_DIR", str(Path("/tmp/explicit-logos")))
        monkeypatch.setenv("BOTLER_DATA_DIR", str(Path("/tmp/persist-data")))
        assert repo_logo._resolve_logo_dir() == Path("/tmp/explicit-logos")

    def test_resolve_uses_persistent_data_dir(self, monkeypatch):
        """未显式指定时（pm2 部署形态）：LOGO_DIR = BOTLER_DATA_DIR/
        backend/data/logos——持久数据目录，不随构建目录轮换丢失。"""
        from botler.api import repo_logo
        monkeypatch.delenv("BOTLER_LOGO_DIR", raising=False)
        monkeypatch.setenv("BOTLER_DATA_DIR", str(Path("/tmp/persist-data")))
        assert (repo_logo._resolve_logo_dir()
                == Path("/tmp/persist-data") / "backend" / "data" / "logos")

    def test_resolve_fallback_backend_data(self, monkeypatch):
        """无任何环境变量（docker-compose 形态 backend/data 由 data 卷
        挂载）：回退 <backend>/data/logos。"""
        from botler.api import repo_logo
        monkeypatch.delenv("BOTLER_LOGO_DIR", raising=False)
        monkeypatch.delenv("BOTLER_DATA_DIR", raising=False)
        assert repo_logo._resolve_logo_dir() == repo_logo._BACKEND_DIR / "data" / "logos"

    def test_generate_writes_to_persistent_dir_and_survives_rotation(
            self, logo_env, tmp_path, monkeypatch):
        """集成回归：BOTLER_DATA_DIR 已注入、未显式指定 LOGO_DIR 时，
        generate-logo 落盘持久数据目录；模拟部署轮换（新进程同环境重新
        解析 LOGO_DIR，仍指向同一持久目录）后 get-logo 仍能读到图片。"""
        tc, stub, db, _ = logo_env
        from botler.api import repo_logo
        data_dir = tmp_path / "persistent-data"
        monkeypatch.delenv("BOTLER_LOGO_DIR", raising=False)
        monkeypatch.setenv("BOTLER_DATA_DIR", str(data_dir))
        # 模拟 pm2 进程启动：按当前环境解析 LOGO_DIR
        monkeypatch.setattr(repo_logo, "LOGO_DIR", repo_logo._resolve_logo_dir())
        root = _write_project(tmp_path)
        repo_id = _add_repo(db, 42, "botler", local_path=str(root))
        r = tc.post(f"/api/repos/{repo_id}/generate-logo")
        assert r.status_code == 201, r.text
        # logo 必须落在持久数据目录（而非构建目录 backend/data/logos）
        persistent_file = data_dir / "backend" / "data" / "logos" / f"{repo_id}.png"
        assert persistent_file.is_file()
        # 模拟部署轮换：新进程同环境重新解析（仍指向同一持久目录，文件还在）
        rotated_dir = repo_logo._resolve_logo_dir()
        assert rotated_dir == data_dir / "backend" / "data" / "logos"
        assert (rotated_dir / f"{repo_id}.png").is_file()
        # get-logo 读取正常（图片不随构建目录轮换丢失）
        monkeypatch.setattr(repo_logo, "LOGO_DIR", rotated_dir)
        r2 = tc.get(f"/api/repos/{repo_id}/logo")
        assert r2.status_code == 200
        assert r2.content == b"fake-logo-png-bytes"
