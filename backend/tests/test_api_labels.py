"""标记库 API 测试（issue #29）：默认清单不可删除、自定义标签增删与校验。

覆盖：GET 列表、POST 添加（含格式/重名/默认名冲突校验）、DELETE 删除
（默认标签拒绝、不存在 404）、持久化到 config.yaml。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botler.api import router as api_router
from botler.config import ConfigManager
from botler.database import Database
from botler.gitlab_client import GitLabError
from botler.labels import DEFAULT_LABELS

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
def client(tmp_path):
    """最小测试 app：挂完整 api 路由，ctx 用临时 config + db。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    ctx = SimpleNamespace(config=config, db=Database(str(tmp_path / "test.db")))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app), tmp_path, config


class TestListLabels:
    def test_get_returns_default_and_custom(self, client):
        api, _, _ = client
        r = api.get("/api/labels")
        assert r.status_code == 200
        data = r.json()
        assert len(data["default"]) == 15
        assert data["default"][0]["name"] == "bug"
        # 默认清单含流程标签与调度标签
        names = {l["name"] for l in data["default"]}
        assert {"bug", "feature", "bot-done", "in-progress", "need-verify"} <= names
        assert data["custom"] == []


class TestAddLabel:
    def test_add_custom_label_persists(self, client):
        api, _, config = client
        r = api.post("/api/labels", json={
            "name": "security", "color": "#111111", "description": "安全相关"})
        assert r.status_code == 200
        data = r.json()
        assert data["custom"] == [{
            "name": "security", "color": "#111111", "description": "安全相关"}]
        # 持久化到 config.yaml，且默认清单不落盘（内置）
        assert config.get().custom_labels == [{
            "name": "security", "color": "#111111", "description": "安全相关"}]

    def test_add_duplicate_custom_rejected(self, client):
        api, _, _ = client
        assert api.post("/api/labels", json={"name": "security"}).status_code == 200
        r = api.post("/api/labels", json={"name": "security"})
        assert r.status_code == 400
        assert "已存在" in r.json()["detail"]

    def test_add_default_name_rejected(self, client):
        api, _, _ = client
        r = api.post("/api/labels", json={"name": "bug"})
        assert r.status_code == 400
        assert "默认标签" in r.json()["detail"]

    def test_add_invalid_name_rejected(self, client):
        api, _, _ = client
        r = api.post("/api/labels", json={"name": "../evil"})
        assert r.status_code == 400
        assert "标签名须以字母或数字开头" in r.json()["detail"]
        # 空名
        r = api.post("/api/labels", json={"name": "  "})
        assert r.status_code == 400

    def test_add_invalid_color_rejected(self, client):
        api, _, _ = client
        r = api.post("/api/labels", json={"name": "security", "color": "red"})
        assert r.status_code == 400
        assert "#RRGGBB" in r.json()["detail"]

    def test_add_without_color_uses_default(self, client):
        api, _, config = client
        r = api.post("/api/labels", json={"name": "hotfix"})
        assert r.status_code == 200
        assert r.json()["custom"][0]["color"] == "#6699cc"

    def test_add_with_space_and_dash_name(self, client):
        api, _, _ = client
        r = api.post("/api/labels", json={"name": "urgent-1"})
        assert r.status_code == 200


class TestDeleteLabel:
    def test_delete_custom_label(self, client):
        api, _, config = client
        api.post("/api/labels", json={"name": "security"})
        r = api.delete("/api/labels/security")
        assert r.status_code == 200
        assert r.json()["custom"] == []
        assert config.get().custom_labels == []

    def test_delete_default_rejected(self, client):
        api, _, _ = client
        r = api.delete("/api/labels/bug")
        assert r.status_code == 400
        assert "默认标签" in r.json()["detail"]
        r = api.delete("/api/labels/bot-done")
        assert r.status_code == 400

    def test_delete_missing_404(self, client):
        api, _, _ = client
        r = api.delete("/api/labels/nonexistent")
        assert r.status_code == 404


def test_default_labels_match_sync_script():
    """内置默认清单与 docs/labels.md / scripts/sync_labels.py 保持一致。"""
    import importlib.util
    from pathlib import Path
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "sync_labels.py"
    spec = importlib.util.spec_from_file_location("sync_labels", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    script_names = {l["name"] for l in mod.LABELS}
    default_names = {l["name"] for l in DEFAULT_LABELS}
    assert default_names == script_names


class StubGitLab:
    """默认标签同步（issue #307）用的 GitLab 桩：记录 list/create 调用。"""

    def __init__(self):
        self.existing_labels: list[dict] = []
        self.created_labels: list[dict] = []

    def list_project_labels(self, project_id):
        return list(self.existing_labels)

    def create_project_label(self, project_id, name, color, description=None):
        self.created_labels.append({
            "project_id": project_id, "name": name,
            "color": color, "description": description})
        return {"name": name, "color": color}


@pytest.fixture
def sync_client(tmp_path):
    """标记库同步测试专用：ctx 含 gitlab 桩 + db，返回 (api, tmp_path, db, stub, config)。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    config = ConfigManager(str(config_path))
    db = Database(str(tmp_path / "test.db"))
    stub = StubGitLab()
    ctx = SimpleNamespace(config=config, db=db, gitlab=stub)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(api_router)
    return TestClient(app), tmp_path, db, stub, config


class TestSyncDefaultLabel:
    """默认标签一键同步到全部已添加仓库（issue #307）。

    需求：标记库页点击默认标签后，自动把该标签同步到已添加的所有仓库，
    包括启用和未启用的。语义与「添加仓库时补齐默认标签」（issue #157）
    一致——目标项目缺失才创建，已存在不覆盖；单仓库失败尽力而为。

    覆盖：启用+未启用仓库全部补齐 / 已存在跳过 / 非默认标签 400 /
    无仓库空跑 / 单仓库失败不中断 / per-repo client（remote token）优先。
    """

    @staticmethod
    def _add_repos(db, *specs):
        for project_id, name, enabled in specs:
            db.upsert_repo(
                project_id=project_id, name=name, enabled=enabled,
                url=f"https://gitlab.example.com/group/{name}.git")

    def test_sync_creates_label_in_all_repos_including_disabled(self, sync_client):
        """启用与未启用仓库都同步：远端缺失 → 全部创建，颜色/描述与规范一致。"""
        api, _, db, stub, _ = sync_client
        self._add_repos(db, (11, "repo-a", True), (22, "repo-b", False))
        r = api.post("/api/labels/bug/sync")
        assert r.status_code == 200
        data = r.json()
        assert data["total_repos"] == 2
        assert sorted(data["created"]) == ["repo-a", "repo-b"]
        assert data["already_exists"] == [] and data["failed"] == []
        assert data["label"]["name"] == "bug"
        assert {c["project_id"] for c in stub.created_labels} == {11, 22}
        assert stub.created_labels[0]["color"] == "#d9534f"
        assert stub.created_labels[0]["description"] == "缺陷修复"

    def test_sync_skips_existing_label(self, sync_client):
        """远端已存在该标签（颜色/描述被用户改过）→ 跳过不覆盖。"""
        api, _, db, stub, _ = sync_client
        self._add_repos(db, (11, "repo-a", True), (22, "repo-b", False))
        stub.existing_labels = [
            {"name": "bug", "color": "#000000", "description": "用户自定义"}]
        r = api.post("/api/labels/bug/sync")
        assert r.status_code == 200
        data = r.json()
        assert data["created"] == []
        assert sorted(data["already_exists"]) == ["repo-a", "repo-b"]
        assert stub.created_labels == []

    def test_sync_rejects_non_default_label(self, sync_client):
        """自定义/不存在的标签 → 400 拒绝（仅默认标签支持一键同步）。"""
        api, _, db, _, _ = sync_client
        self._add_repos(db, (11, "repo-a", True))
        r = api.post("/api/labels/security/sync")
        assert r.status_code == 400
        assert "不是默认标签" in r.json()["detail"]
        r = api.post("/api/labels/nonexistent/sync")
        assert r.status_code == 400

    def test_sync_with_no_repos(self, sync_client):
        """未添加任何仓库 → 200 空跑，total_repos=0。"""
        api, _, _, _, _ = sync_client
        r = api.post("/api/labels/feature/sync")
        assert r.status_code == 200
        data = r.json()
        assert data["total_repos"] == 0
        assert data["created"] == []
        assert data["already_exists"] == [] and data["failed"] == []

    def test_sync_best_effort_on_repo_failure(self, sync_client):
        """单个仓库同步失败（GitLab 报错）→ 记入 failed，其余仓库照常同步。"""
        api, _, db, stub, _ = sync_client
        self._add_repos(db, (11, "repo-a", True), (22, "repo-b", True))
        original = stub.list_project_labels

        def failing_list(project_id):
            if project_id == 22:
                raise GitLabError("权限不足", 403)
            return original(project_id)

        stub.list_project_labels = failing_list
        r = api.post("/api/labels/bug/sync")
        assert r.status_code == 200
        data = r.json()
        assert data["created"] == ["repo-a"]
        assert len(data["failed"]) == 1
        assert data["failed"][0]["repo"] == "repo-b"
        assert "权限不足" in data["failed"][0]["error"]

    def test_sync_uses_per_repo_client_when_available(self, sync_client, monkeypatch):
        """仓库 remote url 内嵌 token 时优先用 per-repo client（身份链路 issue #307）。"""
        api, _, db, stub, _ = sync_client
        self._add_repos(db, (11, "repo-a", True), (22, "repo-b", False))
        from botler import git_remote

        per_repo = StubGitLab()
        monkeypatch.setattr(
            git_remote, "build_repo_client",
            lambda row, verify_ssl=True: per_repo)
        r = api.post("/api/labels/bug/sync")
        assert r.status_code == 200
        assert {c["project_id"] for c in per_repo.created_labels} == {11, 22}
        # 全局 stub 未被用于创建标签
        assert stub.created_labels == []


class TestSyncAllLabels:
    """一键同步全部默认标签到所有已添加仓库（issue #358）。

    需求：标记库页增加一键同步按钮，把所有默认标签一次性同步到已添加的
    全部仓库（含启用与未启用的）。语义与单标签同步（issue #307）一致：
    缺失才创建、已存在不覆盖、单仓库/单标签失败尽力而为不中断。

    覆盖：启用+未启用仓库全部补齐 / 已存在跳过 / 混合场景 / 无仓库空跑 /
    单仓库失败不中断 / per-repo client 优先 / 响应结构完整。
    """

    @staticmethod
    def _add_repos(db, *specs):
        for project_id, name, enabled in specs:
            db.upsert_repo(
                project_id=project_id, name=name, enabled=enabled,
                url=f"https://gitlab.example.com/group/{name}.git")

    def test_sync_all_creates_all_default_labels_in_all_repos(self, sync_client):
        """启用与未启用仓库都补齐全部默认标签（颜色/描述与规范一致）。"""
        api, _, db, stub, _ = sync_client
        self._add_repos(db, (11, "repo-a", True), (22, "repo-b", False))
        r = api.post("/api/labels/sync-all")
        assert r.status_code == 200
        data = r.json()
        assert data["total_repos"] == 2
        assert len(data["labels"]) == len(DEFAULT_LABELS)
        # 每个默认标签都出现在结果里
        names = {item["label"] for item in data["labels"]}
        assert names == {l["name"] for l in DEFAULT_LABELS}
        # 14 个标签 × 2 仓库全部创建
        assert data["total_created"] == len(DEFAULT_LABELS) * 2
        assert data["total_already_exists"] == 0
        assert data["total_failed"] == 0
        assert all(item["failed"] == [] for item in data["labels"])
        # 每个仓库都收到全部默认标签，且颜色/描述与规范一致
        per_repo = {pid: [] for pid in (11, 22)}
        for created in stub.created_labels:
            per_repo[created["project_id"]].append(created["name"])
        assert sorted(per_repo[11]) == sorted(l["name"] for l in DEFAULT_LABELS)
        assert sorted(per_repo[22]) == sorted(l["name"] for l in DEFAULT_LABELS)
        spec_by_name = {l["name"]: l for l in DEFAULT_LABELS}
        for created in stub.created_labels:
            spec = spec_by_name[created["name"]]
            assert created["color"] == spec["color"]
            assert created["description"] == spec["description"]

    def test_sync_all_skips_existing_labels(self, sync_client):
        """远端已存在某标签 → 跳过不覆盖（保留用户已有的颜色/描述）。"""
        api, _, db, stub, _ = sync_client
        self._add_repos(db, (11, "repo-a", True), (22, "repo-b", False))
        stub.existing_labels = [
            {"name": "bug", "color": "#000000", "description": "用户自定义"},
            {"name": "feature", "color": "#111111", "description": "用户自定义"}]
        r = api.post("/api/labels/sync-all")
        assert r.status_code == 200
        data = r.json()
        # 2 个标签已存在 × 2 仓库 = 4 次跳过
        assert data["total_already_exists"] == 4
        assert data["total_created"] == (len(DEFAULT_LABELS) - 2) * 2
        assert data["total_failed"] == 0
        # 跳过的标签没有 create 调用
        assert not any(c["name"] in {"bug", "feature"} for c in stub.created_labels)

    def test_sync_all_mixed_repos(self, sync_client):
        """仓库 A 全缺失、仓库 B 已存在部分 → 各自按缺失创建。"""
        api, _, db, stub, _ = sync_client
        self._add_repos(db, (11, "repo-a", True), (22, "repo-b", True))

        def list_by_project(project_id):
            # 仅仓库 B（22）已存在 bug，仓库 A 全缺失
            return [{"name": "bug", "color": "#000000"}] if project_id == 22 else []

        stub.list_project_labels = list_by_project
        r = api.post("/api/labels/sync-all")
        data = r.json()
        assert data["total_created"] == len(DEFAULT_LABELS) * 2 - 1  # 仅 repo-b 跳过 bug
        assert data["total_already_exists"] == 1  # bug 只在 repo-b 跳过
        assert data["total_failed"] == 0
        # repo-b（22）没有创建 bug
        assert not any(c["name"] == "bug" and c["project_id"] == 22
                       for c in stub.created_labels)
        # repo-a（11）创建了 bug
        assert any(c["name"] == "bug" and c["project_id"] == 11
                   for c in stub.created_labels)

    def test_sync_all_with_no_repos(self, sync_client):
        """未添加任何仓库 → 200 空跑：total_repos=0、每个标签结果为空条目
        （与单标签同步接口在无仓库时返回完整结构的一致契约）。"""
        api, _, _, _, _ = sync_client
        r = api.post("/api/labels/sync-all")
        assert r.status_code == 200
        data = r.json()
        assert data["total_repos"] == 0
        # 每个默认标签都有结果条目，且全部为空
        assert len(data["labels"]) == len(DEFAULT_LABELS)
        assert all(item["created"] == [] and item["already_exists"] == []
                   and item["failed"] == [] for item in data["labels"])
        assert data["total_created"] == 0
        assert data["total_already_exists"] == 0
        assert data["total_failed"] == 0

    def test_sync_all_best_effort_on_repo_failure(self, sync_client):
        """单仓库失败（GitLab 报错）→ 记入该仓库每个标签的 failed，其余仓库照常。"""
        api, _, db, stub, _ = sync_client
        self._add_repos(db, (11, "repo-a", True), (22, "repo-b", True))
        original = stub.list_project_labels

        def failing_list(project_id):
            if project_id == 22:
                raise GitLabError("权限不足", 403)
            return original(project_id)

        stub.list_project_labels = failing_list
        r = api.post("/api/labels/sync-all")
        assert r.status_code == 200
        data = r.json()
        # 仓库 A 全部创建，仓库 B 每个标签失败
        assert data["total_created"] == len(DEFAULT_LABELS)
        assert data["total_failed"] == len(DEFAULT_LABELS)
        assert all(len(item["failed"]) == 1 for item in data["labels"])
        assert all(item["failed"][0]["repo"] == "repo-b" for item in data["labels"])
        assert all("权限不足" in item["failed"][0]["error"] for item in data["labels"])
        # 失败的标签在仓库 A 仍然创建成功
        assert {c["project_id"] for c in stub.created_labels} == {11}

    def test_sync_all_uses_per_repo_client_when_available(self, sync_client, monkeypatch):
        """仓库 remote url 内嵌 token 时优先用 per-repo client（身份链路 issue #307/#358）。"""
        api, _, db, stub, _ = sync_client
        self._add_repos(db, (11, "repo-a", True), (22, "repo-b", False))
        from botler import git_remote

        per_repo = StubGitLab()
        monkeypatch.setattr(
            git_remote, "build_repo_client",
            lambda row, verify_ssl=True: per_repo)
        r = api.post("/api/labels/sync-all")
        assert r.status_code == 200
        # 全部创建走 per-repo client
        assert len(per_repo.created_labels) == len(DEFAULT_LABELS) * 2
        assert {c["project_id"] for c in per_repo.created_labels} == {11, 22}
        # 全局 stub 未被用于创建标签
        assert stub.created_labels == []
