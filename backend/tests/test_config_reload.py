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

        cm.update_section("worker", {"max_concurrent_repos": 5})

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
        cm.update_section("templates", {"default": TEMPLATE_B})
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


# ---- issue #181 CI 诊断：save 非原子写 + load 先赋值后校验 ----
# E2E settings 测试偶发失败根因：save() 直接 open('w') 截断写盘，并发读
# （get() mtime 自动重载 / update_* 写盘前 _reload_from_disk）读到半截
# config.yaml → YAMLError 或解析出残缺配置（gitlab 段缺 url）→
# update_webhook 内 _to_settings 抛 KeyError: 'url' → PUT /api/settings 500。
# 修复：save() 原子写（临时文件 + rename）；load() 先解析校验成功后才
# 替换内存 _data，失败不污染内存。

class TestAtomicSaveAndLoad:
    def test_partial_yaml_does_not_pollute_memory(self, tmp_path):
        """磁盘文件被并发写为残缺内容（gitlab 段缺 url）时，重载失败不得
        污染内存 _data；后续 update_webhook 不得再抛 KeyError（修复前 load
        先把残缺内容赋给 _data 再 _to_settings，reload 失败但内存已被污染，
        settings 保存 500 的根因）。"""
        path = tmp_path / "config.yaml"
        _write_config(path, TEMPLATE_A)
        cm = ConfigManager(str(path))
        cm.get()

        # 模拟并发写盘截断：残缺但可解析的 YAML（gitlab 段缺 url）
        path.write_text(
            "gitlab:\n  bot_token: t\n"
            "worker:\n  max_concurrent_repos: 3\n"
            "repos: []\n",
            encoding="utf-8")

        # get() 触发 mtime 重载：解析失败应降级保留旧配置
        assert cm.get().default_template == TEMPLATE_A

        # 修复前：update_webhook 在 reload 失败后继续用被污染的 _data，
        # _to_settings 抛 KeyError: 'url'（CI 中 PUT /api/settings 500）
        cm.update_section("webhook", {"enabled": True, "url": ""})
        assert cm.get().default_template == TEMPLATE_A
        # 写盘恢复完整配置（gitlab.url 仍在）
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["gitlab"]["url"] == "https://gitlab.example.com"

    def test_save_atomic_concurrent_reads_never_partial(self, tmp_path):
        """并发读下 save() 原子写：写入过程中任何时刻读取都能得到完整配置
        （修复前 open('w') 截断写，读线程会读到半截 YAML / 残缺配置）。"""
        import threading

        path = tmp_path / "config.yaml"
        _write_config(path, TEMPLATE_A)
        cm = ConfigManager(str(path))
        cm.get()

        stop = threading.Event()
        errors = []

        def reader():
            while not stop.is_set():
                try:
                    with open(path, encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    assert data["gitlab"]["url"] == \
                        "https://gitlab.example.com", "读到残缺配置"
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    return

        def writer():
            for i in range(100):
                cm.update_section("worker", {"max_concurrent_repos": 3 + i % 5})
            stop.set()

        threads = [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        try:
            writer()
        finally:
            stop.set()
        for t in threads:
            t.join()

        assert errors == [], f"并发读读到半截/残缺配置: {errors}"

    def test_save_leaves_no_temp_files(self, tmp_path):
        """原子写后不残留临时文件。"""
        path = tmp_path / "config.yaml"
        _write_config(path, TEMPLATE_A)
        cm = ConfigManager(str(path))
        cm.get()
        cm.update_section("worker", {"max_concurrent_repos": 5})
        leftovers = [p.name for p in tmp_path.iterdir()
                     if p.name.startswith(".config-") or p.name.endswith(".tmp")]
        assert leftovers == []
