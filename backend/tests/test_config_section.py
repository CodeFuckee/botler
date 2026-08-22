"""config.py 泛型 update_section 写回 + 原子写（issue #193）。

背景（issue #193）：ConfigManager 原先有 15+ 个结构重复的 update_*
方法（读 yaml → 局部改 → 写回 → 重载），本次收敛为 1 个泛型实现
update_section(section, patch)，配合 SECTION_SCHEMAS 集中描述：
- fields：可写字段白名单（与 KNOWN_FIELDS 一致，settings API 只写这些）；
- masked：掩码字段（api_key / owner_token 等）——patch 值为 None / 含 *
  / 空串视为「未修改」，不覆盖真实凭据（语义集中实现）；
- trim：写回前空白归一（如 minio.endpoint / public_base_url）；
- blank_means_default：空串字段移除键恢复内置默认（templates.resume/
  comment，不允许空模版）；
- replace_list：整段为列表整体替换（repos / ai_providers / image_models /
  vision_models）。
写回统一走原子写 save()（temp + rename），模拟写中断不损坏 config.yaml。
"""

from pathlib import Path

import pytest
import yaml

from botler.config import ConfigManager


def _write_config(path: Path, extra: str = "") -> None:
    """写一份最小可用 config.yaml（gitlab 段必须有 url / bot_token）。"""
    path.write_text(
        "gitlab:\n  url: https://gitlab.example.com\n  bot_token: t\n"
        "worker:\n  max_concurrent_repos: 3\n"
        "templates: {}\n"
        "repos: []\n"
        + extra,
        encoding="utf-8")


def _read(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class TestUpdateSectionDict:
    """字典段写回：白名单字段生效、越权字段忽略、内存与磁盘同步。"""

    def test_plain_dict_section_writes_and_refreshes(self, tmp_path):
        """worker 段普通字段写回：磁盘落盘 + 内存 settings 刷新。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        settings = cm.update_section(
            "worker", {"max_concurrent_repos": 7, "max_retries": 5})
        assert settings.max_concurrent_repos == 7
        assert cm.get().max_concurrent_repos == 7
        assert _read(path)["worker"]["max_concurrent_repos"] == 7

    def test_unknown_fields_ignored(self, tmp_path):
        """白名单外字段（如 hacker 注入的 unknown_key）不得写入。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        cm.update_section("worker", {"max_concurrent_repos": 9,
                                     "evil_key": "evil"})
        data = _read(path)
        assert data["worker"]["max_concurrent_repos"] == 9
        assert "evil_key" not in data["worker"]

    def test_unknown_section_raises(self, tmp_path):
        """未登记 schema 的配置段直接拒绝，避免静默写坏 config.yaml。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        with pytest.raises(ValueError):
            cm.update_section("no_such_section", {"a": 1})

    def test_dict_section_requires_dict_patch(self, tmp_path):
        """字典段传非 dict 直接 TypeError。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        with pytest.raises(TypeError):
            cm.update_section("worker", ["not", "a", "dict"])


class TestMaskedFields:
    """掩码字段（api_key / owner_token / client_secret / authorization /
    access_key / secret_key）：空串 / 含 * 掩码值不覆盖真实凭据。"""

    @pytest.mark.parametrize("section,field", [
        ("gitlab", "owner_token"),
        ("dsh", "api_key"),
        ("sso", "client_secret"),
        ("webhook", "authorization"),
        ("minio", "access_key"),
        ("minio", "secret_key"),
    ])
    def test_masked_value_keeps_existing(self, tmp_path, section, field):
        """含 * 的掩码回传不覆盖真实凭据。"""
        path = tmp_path / "config.yaml"
        if section == "gitlab":
            # gitlab 段必须保留 url/bot_token（_to_settings 依赖），owner_token 并入同段
            path.write_text(
                "gitlab:\n  url: https://gitlab.example.com\n"
                "  bot_token: t\n  owner_token: real-secret-1\n"
                "worker:\n  max_concurrent_repos: 3\n"
                "templates: {}\nrepos: []\n", encoding="utf-8")
        else:
            _write_config(path, extra=f"{section}:\n  {field}: real-secret-1\n")
        cm = ConfigManager(str(path))
        cm.update_section(section, {field: "real-****"})
        assert _read(path)[section][field] == "real-secret-1"

    @pytest.mark.parametrize("section,field", [
        ("gitlab", "owner_token"),
        ("dsh", "api_key"),
        ("sso", "client_secret"),
        ("webhook", "authorization"),
        ("minio", "access_key"),
        ("minio", "secret_key"),
    ])
    def test_blank_value_keeps_existing(self, tmp_path, section, field):
        """空串/纯空白回传同样保持现有凭据（语义集中，issue #193）。"""
        path = tmp_path / "config.yaml"
        if section == "gitlab":
            path.write_text(
                "gitlab:\n  url: https://gitlab.example.com\n"
                "  bot_token: t\n  owner_token: real-secret-2\n"
                "worker:\n  max_concurrent_repos: 3\n"
                "templates: {}\nrepos: []\n", encoding="utf-8")
        else:
            _write_config(path, extra=f"{section}:\n  {field}: real-secret-2\n")
        cm = ConfigManager(str(path))
        cm.update_section(section, {field: ""})
        assert _read(path)[section][field] == "real-secret-2"

    @pytest.mark.parametrize("section,field", [
        ("gitlab", "owner_token"),
        ("dsh", "api_key"),
        ("sso", "client_secret"),
        ("webhook", "authorization"),
        ("minio", "access_key"),
        ("minio", "secret_key"),
    ])
    def test_real_value_overwrites(self, tmp_path, section, field):
        """真实新值正常覆盖旧凭据。"""
        path = tmp_path / "config.yaml"
        if section == "gitlab":
            path.write_text(
                "gitlab:\n  url: https://gitlab.example.com\n"
                "  bot_token: t\n  owner_token: old-secret\n"
                "worker:\n  max_concurrent_repos: 3\n"
                "templates: {}\nrepos: []\n", encoding="utf-8")
        else:
            _write_config(path, extra=f"{section}:\n  {field}: old-secret\n")
        cm = ConfigManager(str(path))
        cm.update_section(section, {field: "new-secret"})
        assert _read(path)[section][field] == "new-secret"

    def test_masked_skips_other_fields(self, tmp_path):
        """同段 patch 中掩码字段跳过、其余字段照常写入。"""
        path = tmp_path / "config.yaml"
        _write_config(path, extra="dsh:\n  api_key: real-key\n  model: m1\n")
        cm = ConfigManager(str(path))
        cm.update_section("dsh", {"api_key": "***", "model": "m2"})
        data = _read(path)
        assert data["dsh"]["api_key"] == "real-key"
        assert data["dsh"]["model"] == "m2"


class TestTrimAndBlankDefault:
    """空白归一（minio.endpoint / public_base_url）与空串恢复默认
    （templates.resume / comment）。"""

    def test_minio_endpoint_trimmed(self, tmp_path):
        """endpoint / public_base_url 空白归一为空串或 strip 后值。"""
        path = tmp_path / "config.yaml"
        _write_config(path, extra="minio:\n  endpoint: h:9000\n")
        cm = ConfigManager(str(path))
        cm.update_section("minio", {
            "endpoint": "  new-host:9000  ",
            "public_base_url": "   ",
        })
        data = _read(path)
        assert data["minio"]["endpoint"] == "new-host:9000"
        assert data["minio"]["public_base_url"] == ""

    def test_templates_blank_resume_removes_key(self, tmp_path):
        """resume 空串：移除键恢复内置默认（不允许空模版，issue #116）。"""
        path = tmp_path / "config.yaml"
        _write_config(path, extra="templates:\n  resume: 自定义提示\n")
        cm = ConfigManager(str(path))
        settings = cm.update_section("templates", {"resume": "  "})
        assert "继续处理（中断恢复）" in settings.resume_template
        data = _read(path)
        assert "resume" not in data["templates"]

    def test_templates_blank_comment_removes_key(self, tmp_path):
        """comment 空串：移除键恢复内置默认（issue #252）。"""
        path = tmp_path / "config.yaml"
        _write_config(path, extra="templates:\n  comment: 自定义评论\n")
        cm = ConfigManager(str(path))
        settings = cm.update_section("templates", {"comment": "  "})
        assert settings.comment_template == ""
        data = _read(path)
        assert "comment" not in data["templates"]

    def test_templates_nonblank_writes(self, tmp_path):
        """非空 resume / comment 正常写盘。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        cm.update_section("templates", {"resume": "恢复 {repo_name}"})
        assert "resume: 恢复 {repo_name}" in path.read_text(encoding="utf-8")


class TestListSections:
    """整体列表替换段：patch 为列表，整段替换。"""

    @pytest.mark.parametrize("section", [
        "ai_providers", "image_models", "vision_models", "repos",
    ])
    def test_list_replace(self, tmp_path, section):
        """列表段整体替换落盘。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        # repos 段受 _to_settings 约束（project_id/name/url 必填），
        # 其余列表段无此约束，用同一批数据
        payload = ([{"project_id": 1, "name": "x",
                     "url": "https://example.com/x.git"}]
                   if section == "repos"
                   else [{"name": "x", "enabled": True}])
        cm.update_section(section, payload)
        assert _read(path)[section] == payload

    def test_list_section_requires_list(self, tmp_path):
        """列表段传非列表直接 TypeError。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        with pytest.raises(TypeError):
            cm.update_section("ai_providers", {"name": "x"})

    def test_labels_custom_replaced(self, tmp_path):
        """labels.custom（标记库）整体替换（原 update_custom_labels 语义）。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        cm.update_section("labels", {"custom": [{"name": "急单", "color": "#f00"}]})
        assert [l["name"] for l in cm.get().custom_labels] == ["急单"]

    def test_manual_edit_kept_before_write(self, tmp_path):
        """写盘前重读磁盘：期间用户手动编辑的字段不被覆盖（issue #25 语义）。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        cm.get()  # 缓存旧状态

        # 模拟用户直接编辑 config.yaml（绕过进程内存）
        data = _read(path)
        data.setdefault("templates", {})["default"] = "手动改的模版"
        path.write_text(yaml.safe_dump(data, allow_unicode=True,
                                       sort_keys=False), encoding="utf-8")

        cm.update_section("worker", {"max_concurrent_repos": 5})

        data = _read(path)
        assert data["templates"]["default"] == "手动改的模版"
        assert data["worker"]["max_concurrent_repos"] == 5


class TestSchemaConsistency:
    """KNOWN_FIELDS 与 SECTION_SCHEMAS 保持一致（新增配置段必须登记 schema）。"""

    def test_all_known_fields_have_schema(self):
        from botler.config import KNOWN_FIELDS, SECTION_SCHEMAS
        for section, fields in KNOWN_FIELDS.items():
            assert section in SECTION_SCHEMAS, f"配置段 {section} 缺 schema"
            assert set(SECTION_SCHEMAS[section].fields) == set(fields), \
                f"{section} schema 字段与 KNOWN_FIELDS 不一致"


class TestAtomicWriteInterruption:
    """原子写（temp + rename）：模拟写中断不损坏 config.yaml（issue #193 验收）。"""

    def test_crash_at_rename_keeps_file_intact(self, tmp_path, monkeypatch):
        """模拟 os.replace 阶段崩溃：原文件保持完整可解析，后续写回仍可用。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        cm.get()

        import botler.config as config_mod
        original_replace = config_mod.os.replace

        def boom(src, dst):
            raise OSError("模拟写中断：rename 阶段崩溃")

        monkeypatch.setattr(config_mod.os, "replace", boom)

        with pytest.raises(OSError):
            cm.update_section("worker", {"max_concurrent_repos": 99})

        # 原文件未被破坏：仍是完整可解析的旧配置
        data = _read(path)
        assert data["gitlab"]["url"] == "https://gitlab.example.com"
        assert data["worker"]["max_concurrent_repos"] == 3

        # 不残留临时文件
        leftovers = [p.name for p in tmp_path.iterdir()
                     if p.name.startswith(".config-") or p.name.endswith(".tmp")]
        assert leftovers == []

        # 恢复后（去掉崩溃模拟）后续写回正常
        monkeypatch.setattr(config_mod.os, "replace", original_replace)
        cm.update_section("worker", {"max_concurrent_repos": 6})
        assert _read(path)["worker"]["max_concurrent_repos"] == 6

    def test_crash_mid_dump_keeps_file_intact(self, tmp_path, monkeypatch):
        """模拟序列化中途崩溃：临时文件半截，目标文件不受影响。"""
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))
        cm.get()

        import botler.config as config_mod
        original_dump = config_mod.yaml.safe_dump

        def boom(*args, **kwargs):
            raise OSError("模拟写中断：dump 中途崩溃")

        monkeypatch.setattr(config_mod.yaml, "safe_dump", boom)

        with pytest.raises(OSError):
            cm.update_section("worker", {"max_concurrent_repos": 99})

        # 目标文件仍是旧配置（临时文件被 finally 清理，不残留）
        data = _read(path)
        assert data["worker"]["max_concurrent_repos"] == 3
        leftovers = [p.name for p in tmp_path.iterdir()
                     if p.name.startswith(".config-") or p.name.endswith(".tmp")]
        assert leftovers == []

        monkeypatch.setattr(config_mod.yaml, "safe_dump", original_dump)
        cm.update_section("worker", {"max_concurrent_repos": 6})
        assert _read(path)["worker"]["max_concurrent_repos"] == 6


class TestFallbackEnginesConfig:
    """worker.fallback_engines / fallback_after_failures 配置读写（issue #236）。"""

    WORKER_BASE = (
        "gitlab:\n  url: https://gitlab.example.com\n  bot_token: t\n"
        "worker:\n  max_concurrent_repos: 3\n{worker_extra}"
        "templates: {{}}\nrepos: []\n"
    )

    def _cfg(self, tmp_path, worker_extra=""):
        path = tmp_path / "config.yaml"
        path.write_text(
            self.WORKER_BASE.format(worker_extra=worker_extra),
            encoding="utf-8")
        return path

    def test_defaults_when_absent(self, tmp_path):
        """未配置时备用列表为空、阈值默认 2（保持旧行为）。"""
        s = ConfigManager(str(self._cfg(tmp_path))).load()
        assert s.fallback_engines == []
        assert s.fallback_after_failures == 2

    def test_parses_yaml_list(self, tmp_path):
        """yaml 配置 fallback_engines 解析为列表，fallback_after_failures 生效。"""
        path = self._cfg(
            tmp_path,
            worker_extra="  fallback_engines: [dsh, hermes]\n"
                         "  fallback_after_failures: 1\n")
        s = ConfigManager(str(path)).load()
        assert s.fallback_engines == ["dsh", "hermes"]
        assert s.fallback_after_failures == 1

    def test_normalizes_case_and_whitespace(self, tmp_path):
        """yaml 中的引擎名 strip + 小写归一。"""
        path = self._cfg(
            tmp_path,
            worker_extra='  fallback_engines: [" DSH ", "HERMES"]\n')
        s = ConfigManager(str(path)).load()
        assert s.fallback_engines == ["dsh", "hermes"]

    def test_update_section_writes_and_refreshes(self, tmp_path):
        """update_section("worker", {fallback_engines}) 写回磁盘并刷新内存。"""
        path = self._cfg(tmp_path)
        cm = ConfigManager(str(path))
        settings = cm.update_section(
            "worker", {"fallback_engines": ["dsh"], "fallback_after_failures": 3})
        assert settings.fallback_engines == ["dsh"]
        assert settings.fallback_after_failures == 3
        assert cm.get().fallback_engines == ["dsh"]
        assert _read(path)["worker"]["fallback_engines"] == ["dsh"]
        assert _read(path)["worker"]["fallback_after_failures"] == 3

    def test_after_failures_clamped_to_at_least_one(self, tmp_path):
        """fallback_after_failures 配置 0/负数时解析归一为 1（不静默失效）。"""
        path = self._cfg(tmp_path, worker_extra="  fallback_after_failures: 0\n")
        s = ConfigManager(str(path)).load()
        assert s.fallback_after_failures == 1


class TestGitLabApiRateLimitSettings:
    """issue #195：限速与抖动配置应可读取、写回且安全归一化。"""

    def test_worker_rate_limit_and_jitter_round_trip(self, tmp_path):
        path = tmp_path / "config.yaml"
        _write_config(path)
        cm = ConfigManager(str(path))

        settings = cm.update_section("worker", {
            "gitlab_api_requests_per_second": 6.5,
            "reconcile_jitter_min_seconds": 1.5,
            "reconcile_jitter_max_seconds": 3.0,
        })

        assert settings.gitlab_api_requests_per_second == 6.5
        assert settings.reconcile_jitter_min_seconds == 1.5
        assert settings.reconcile_jitter_max_seconds == 3.0
        assert _read(path)["worker"]["gitlab_api_requests_per_second"] == 6.5

    def test_invalid_or_reversed_values_keep_protection_and_valid_range(self, tmp_path):
        path = tmp_path / "config.yaml"
        _write_config(path, extra=(
            "worker:\n"
            "  gitlab_api_requests_per_second: 0\n"
            "  reconcile_jitter_min_seconds: -1\n"
            "  reconcile_jitter_max_seconds: -2\n"))
        cm = ConfigManager(str(path))

        settings = cm.get()

        assert settings.gitlab_api_requests_per_second == 0.1
        assert settings.reconcile_jitter_min_seconds == 0.0
        assert settings.reconcile_jitter_max_seconds == 0.0
