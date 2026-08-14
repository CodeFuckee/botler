"""config.yaml 手动编辑即时生效 + UI 保存不覆盖手动修改（issue #25）。

背景（issue #25「我修改了全局模版，但是没有生效」）：
docs 约定 config.yaml 是唯一事实来源、Web UI 是编辑它的外壳，用户
直接编辑 config.yaml 修改全局模板。但 ConfigManager.get() 只在进程
启动时 load 一次并缓存：
- 手动编辑 config.yaml → 运行中进程仍用旧模板渲染（修改"不生效"）；
- 更糟：手动编辑后只要在 Web UI 保存任意设置，save() 用内存旧值
  整体覆盖写回 config.yaml → 手动修改被静默丢弃。
修复：get() 检测磁盘 mtime 变化自动重载；update_* 写盘前重新读取
磁盘，以磁盘最新内容为基底。
"""

from pathlib import Path

import pytest
import yaml

from botler.config import ConfigManager

TEMPLATE_A = "模板A：处理 issue {issue_title}"
TEMPLATE_B = "模板B：手动编辑后的新模板 {issue_title}"


def _write_config(path: Path, template: str = TEMPLATE_A) -> None:
    path.write_text(
        "gitlab:\n  url: https://gitlab.example.com\n  bot_token: t\n"
        f"templates:\n  default: {template!r}\n"
        "worker:\n  max_concurrent_repos: 3\n"
        "repos: []\n",
        encoding="utf-8")


def _manual_edit_template(path: Path, template: str = TEMPLATE_B) -> None:
    """模拟用户直接编辑 config.yaml（绕过 Web UI，不触碰进程内存）。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data.setdefault("templates", {})["default"] = template
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


class TestManualEditAutoReload:
    def test_manual_edit_visible_after_get(self, tmp_path):
        """手动编辑 config.yaml 后，get() 应自动返回新模板（无需重启/无需
        UI 保存）——修复前返回缓存的旧模板。"""
        path = tmp_path / "config.yaml"
        _write_config(path, TEMPLATE_A)
        cm = ConfigManager(str(path))
        assert cm.get().default_template == TEMPLATE_A

        _manual_edit_template(path, TEMPLATE_B)  # 用户直接改文件

        assert cm.get().default_template == TEMPLATE_B

    def test_manual_edit_visible_after_multiple_get(self, tmp_path):
        """连续 get() 只触发一次重载，且多次调用后仍返回新值。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        cm.get()  # 首次 load
        cm.get()  # 磁盘未变，应走缓存

        _manual_edit_template(path, TEMPLATE_B)
        assert cm.get().default_template == TEMPLATE_B
        assert cm.get().default_template == TEMPLATE_B  # 重载后稳定


class TestUiSaveKeepsManualEdit:
    def test_update_worker_keeps_manual_template(self, tmp_path):
        """手动编辑模板后，在 Web UI 保存任意设置（如 worker），不得用内存
        旧值覆盖磁盘上的手动修改——修复前 save() 整体写回导致模板回退。"""
        path = tmp_path / "config.yaml"
        _write_config(path, TEMPLATE_A)
        cm = ConfigManager(str(path))
        cm.get()  # 内存缓存 TEMPLATE_A

        _manual_edit_template(path, TEMPLATE_B)  # 用户手动改为 B

        cm.update_worker({"max_concurrent_repos": 5})

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["templates"]["default"] == TEMPLATE_B, \
            "UI 保存不得覆盖用户手动编辑的模板"
        assert data["worker"]["max_concurrent_repos"] == 5
        assert cm.get().default_template == TEMPLATE_B

    def test_update_default_template_still_works(self, tmp_path):
        """回归：通过 Web UI 保存模板本身仍然生效。"""
        path = tmp_path / "config.yaml"
        _write_config(path, TEMPLATE_A)
        cm = ConfigManager(str(path))
        cm.update_default_template(TEMPLATE_B)
        assert cm.get().default_template == TEMPLATE_B
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["templates"]["default"] == TEMPLATE_B


class TestReloadRobustness:
    def test_corrupted_file_keeps_old_settings(self, tmp_path):
        """磁盘文件损坏（非法 YAML / 半写状态）时自动重载失败，应保留当前
        配置并继续工作，不得抛异常。"""
        path = tmp_path / "config.yaml"
        _write_config(path, TEMPLATE_A)
        cm = ConfigManager(str(path))
        cm.get()

        path.write_text("templates: [未闭合", encoding="utf-8")  # 非法 YAML

        assert cm.get().default_template == TEMPLATE_A  # 降级保留旧值

    def test_missing_file_keeps_old_settings(self, tmp_path):
        """磁盘文件被临时移走时同样降级保留旧值。"""
        path = tmp_path / "config.yaml"
        _write_config(path, TEMPLATE_A)
        cm = ConfigManager(str(path))
        cm.get()

        path.unlink()

        assert cm.get().default_template == TEMPLATE_A


def _write_config_with_repos(path: Path, repos: list[dict]) -> None:
    data = {
        "gitlab": {"url": "https://gitlab.example.com", "bot_token": "t"},
        "worker": {},
        "repos": repos,
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


class TestRemoveRepo:
    """ConfigManager.remove_repo（issue #61）：删除仓库时从 config.yaml 移除条目。"""

    def test_remove_repo_persists_and_refreshes(self, tmp_path):
        """移除指定 project_id：内存 settings 刷新 + 磁盘落盘，其余仓库保留。"""
        path = tmp_path / "config.yaml"
        _write_config_with_repos(path, [
            {"project_id": 11, "name": "graph2plan", "url": "https://g.example.com/g2p.git"},
            {"project_id": 22, "name": "botler", "url": "https://g.example.com/botler.git"},
        ])
        cm = ConfigManager(str(path))

        cm.remove_repo(11)

        # 内存已刷新（无需重新 get 也应可见）
        assert [r.project_id for r in cm.get().repos] == [22]
        # 磁盘已落盘
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert [r["project_id"] for r in data["repos"]] == [22]

    def test_remove_repo_missing_id_noop(self, tmp_path):
        """移除不存在的 project_id：其余仓库原样保留，不报错。"""
        path = tmp_path / "config.yaml"
        _write_config_with_repos(path, [
            {"project_id": 22, "name": "botler", "url": "https://g.example.com/botler.git"},
        ])
        cm = ConfigManager(str(path))

        cm.remove_repo(999)

        assert [r.project_id for r in cm.get().repos] == [22]

    def test_remove_repo_keeps_manual_edit(self, tmp_path):
        """remove_repo 先重读磁盘再保存：期间用户手动编辑的其他字段不被覆盖。"""
        path = tmp_path / "config.yaml"
        _write_config_with_repos(path, [
            {"project_id": 11, "name": "graph2plan", "url": "https://g.example.com/g2p.git"},
        ])
        cm = ConfigManager(str(path))
        cm.get()  # 缓存旧状态

        # 模拟用户手动编辑（绕过进程内存）：改默认模版
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data.setdefault("templates", {})["default"] = "手动改的模版"
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

        cm.remove_repo(11)

        # 手动编辑的模版保留（未被内存旧值覆盖）
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["templates"]["default"] == "手动改的模版"
        assert data["repos"] == []
