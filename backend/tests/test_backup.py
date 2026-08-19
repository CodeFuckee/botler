"""备份与恢复模块测试：创建 / 列表 / 删除 / 保留清理 / 恢复 / 安全校验。

覆盖边界：空目录、sqlite WAL 连接中备份、路径穿越成员、缺 manifest、
校验和失败、非法文件名、恢复后数据还原。
"""

import io
import json
import os
import sqlite3
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from botler.backup import ALLOWED_FILES, BotlerBackup, BackupError


@pytest.fixture
def backup_obj(tmp_path, monkeypatch):
    """临时 config.yaml + botler.db + 独立备份目录。

    _restart 一律 mock（真实实现会 os.execv 重启进程，会连 pytest 一起重启）。
    """
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "botler.db"
    backup_dir = tmp_path / "backups"

    config_path.write_text("gitlab:\n  url: https://gitlab.example.com\n", encoding="utf-8")

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(BotlerBackup, "_restart", lambda self, delay=0.0: None)
    return BotlerBackup(str(config_path), str(db_path), str(backup_dir))


def _read_archive(bak: BotlerBackup, name: str) -> dict[str, bytes]:
    """解包 tar.gz，返回 {成员名: 内容}。"""
    with tarfile.open(os.path.join(bak.backup_dir, name), "r:gz") as tf:
        return {m.name: tf.extractfile(m).read() for m in tf.getmembers()}


class TestCreateBackup:
    def test_create_creates_tarball_with_all_files(self, backup_obj):
        """备份包应包含 manifest.json / config.yaml / botler.db 三个成员。"""
        info = backup_obj.create_backup()
        assert info["name"].startswith("botler-backup-")
        assert info["name"].endswith(".tar.gz")

        members = _read_archive(backup_obj, info["name"])
        assert set(members) == {"manifest.json", "config.yaml", "botler.db"}

    def test_manifest_checksums_match(self, backup_obj):
        """manifest 中记录的 sha256 应与实际文件一致。"""
        import hashlib
        info = backup_obj.create_backup()
        members = _read_archive(backup_obj, info["name"])
        manifest = __import__("json").loads(members["manifest.json"])
        for fname in ("config.yaml", "botler.db"):
            actual = hashlib.sha256(members[fname]).hexdigest()
            assert manifest["files"][fname]["sha256"] == actual

    def test_manifest_records_trigger(self, backup_obj):
        import json
        info = backup_obj.create_backup(trigger="scheduled")
        members = _read_archive(backup_obj, info["name"])
        assert json.loads(members["manifest.json"])["trigger"] == "scheduled"

    def test_create_with_wal_connection_open(self, backup_obj):
        """db 被 WAL 连接占用时仍能创建一致备份（sqlite backup API）。"""
        conn = sqlite3.connect(backup_obj.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS wal_t (id INTEGER)")
        conn.execute("INSERT INTO wal_t VALUES (42)")
        conn.commit()
        try:
            info = backup_obj.create_backup()
            # 校验备份内容包含未 checkpoint 的数据（sqlite 连接不支持内存 DB，落盘验证）
            with tarfile.open(os.path.join(backup_obj.backup_dir, info["name"]), "r:gz") as tf:
                db_bytes = tf.extractfile("botler.db").read()
            restore_path = os.path.join(os.path.dirname(backup_obj.backup_dir), "wal-restore.db")
            with open(restore_path, "wb") as f:
                f.write(db_bytes)
            restore_conn = sqlite3.connect(restore_path)
            assert restore_conn.execute("SELECT id FROM wal_t").fetchall() == [(42,)]
            restore_conn.close()
        finally:
            conn.close()


class TestListBackups:
    def test_list_empty(self, backup_obj):
        assert backup_obj.list_backups() == []

    def test_list_after_create(self, backup_obj):
        info = backup_obj.create_backup()
        items = backup_obj.list_backups()
        assert len(items) == 1
        assert items[0]["name"] == info["name"]
        assert items[0]["size"] > 0
        assert items[0]["created_at"]

    def test_list_sorted_newest_first(self, backup_obj):
        backup_obj.create_backup()
        time.sleep(1.1)  # 文件名秒级精度
        second = backup_obj.create_backup()
        items = backup_obj.list_backups()
        assert items[0]["name"] == second["name"]
        assert len(items) == 2


class TestDeleteBackup:
    def test_delete_existing(self, backup_obj):
        info = backup_obj.create_backup()
        backup_obj.delete_backup(info["name"])
        assert backup_obj.list_backups() == []

    def test_delete_missing_raises(self, backup_obj):
        with pytest.raises(BackupError):
            backup_obj.delete_backup("no-such.tar.gz")

    def test_delete_rejects_path_traversal(self, backup_obj):
        with pytest.raises(BackupError):
            backup_obj.delete_backup("../config.yaml")


class TestPrune:
    def test_prune_keeps_recent_deletes_old(self, backup_obj):
        old = backup_obj.create_backup()
        time.sleep(1.1)
        new = backup_obj.create_backup()
        # 把 old 的 mtime 拨到 10 天前
        old_path = os.path.join(backup_obj.backup_dir, old["name"])
        ten_days_ago = time.time() - 10 * 24 * 3600
        os.utime(old_path, (ten_days_ago, ten_days_ago))

        removed = backup_obj.prune_backups(retention_days=7)
        assert removed == [old["name"]]
        remaining = [b["name"] for b in backup_obj.list_backups()]
        assert remaining == [new["name"]]

    def test_prune_retention_zero_removes_all(self, backup_obj):
        backup_obj.create_backup()
        time.sleep(1.1)
        backup_obj.create_backup()
        backup_obj.prune_backups(retention_days=0)
        assert backup_obj.list_backups() == []


class TestRestore:
    def test_restore_roundtrip(self, backup_obj):
        """备份 → 修改数据 → 恢复 → 数据还原。"""
        info = backup_obj.create_backup()

        # 修改 config 与 db
        Path(backup_obj.config_path).write_text("gitlab:\n  url: https://changed.example.com\n", encoding="utf-8")
        conn = sqlite3.connect(backup_obj.db_path)
        conn.execute("INSERT INTO t (v) VALUES ('dirty')")
        conn.commit()
        conn.close()

        backup_obj.restore_backup(info["name"])

        assert "changed.example.com" not in Path(backup_obj.config_path).read_text(encoding="utf-8")
        conn = sqlite3.connect(backup_obj.db_path)
        values = [r[0] for r in conn.execute("SELECT v FROM t")]
        conn.close()
        assert values == ["hello"]

    def test_restore_upload_flow(self, backup_obj):
        """上传恢复：任意路径的合法备份包同样能恢复。"""
        info = backup_obj.create_backup()
        import shutil
        copy_path = os.path.join(os.path.dirname(backup_obj.backup_dir), "upload-copy.tar.gz")
        shutil.copy(os.path.join(backup_obj.backup_dir, info["name"]), copy_path)

        Path(backup_obj.config_path).write_text("changed: true\n", encoding="utf-8")
        backup_obj.restore_upload(copy_path)
        assert "changed: true" not in Path(backup_obj.config_path).read_text(encoding="utf-8")


class TestSecurity:
    @staticmethod
    def _make_archive(backup_obj, members: dict[str, bytes], name="evil.tar.gz"):
        """构造自定义备份包（可放恶意成员 / 缺文件 / 错校验和）。"""
        path = os.path.join(os.path.dirname(backup_obj.backup_dir), name)
        with tarfile.open(path, "w:gz") as tf:
            for mname, content in members.items():
                data = io.BytesIO(content)
                ti = tarfile.TarInfo(mname)
                ti.size = len(content)
                tf.addfile(ti, data)
        return str(path)

    def test_rejects_path_traversal_members(self, backup_obj):
        """成员名含 ../ 或绝对路径必须拒绝。"""
        path = self._make_archive(backup_obj, {"../../etc/evil": b"x"})
        with pytest.raises(BackupError, match="非法成员名"):
            backup_obj.restore_upload(path)

    def test_rejects_non_whitelist_members(self, backup_obj):
        """白名单外的成员（如 .env）必须拒绝。"""
        import json
        manifest = json.dumps({"app": "botler", "files": {}})
        path = self._make_archive(backup_obj, {
            "manifest.json": manifest.encode(),
            ".env": b"SECRET=1",
        })
        with pytest.raises(BackupError, match="非法成员名"):
            backup_obj.restore_upload(path)

    def test_rejects_missing_manifest(self, backup_obj):
        path = self._make_archive(backup_obj, {"config.yaml": b"x"})
        with pytest.raises(BackupError, match="manifest"):
            backup_obj.restore_upload(path)

    def test_rejects_bad_checksum(self, backup_obj):
        """manifest 校验和与实际内容不符必须拒绝。"""
        import hashlib
        import json
        content = b"tampered"
        manifest = json.dumps({
            "app": "botler",
            "files": {
                "config.yaml": {"sha256": hashlib.sha256(b"original").hexdigest()},
            },
        })
        path = self._make_archive(backup_obj, {
            "manifest.json": manifest.encode(),
            "config.yaml": content,
        })
        with pytest.raises(BackupError, match="校验和"):
            backup_obj.restore_upload(path)

    def test_restore_does_not_touch_whitelist_absent_files(self, backup_obj):
        """只替换包内白名单文件：包缺 botler.db 时 config.yaml 仍恢复。"""
        import hashlib
        import json
        content = "restored-config\n".encode()
        manifest = json.dumps({
            "app": "botler",
            "files": {"config.yaml": {"sha256": hashlib.sha256(content).hexdigest()}},
        })
        path = self._make_archive(backup_obj, {
            "manifest.json": manifest.encode(),
            "config.yaml": content,
        })
        Path(backup_obj.config_path).write_text("old\n", encoding="utf-8")
        backup_obj.restore_upload(path)
        assert Path(backup_obj.config_path).read_text(encoding="utf-8") == "restored-config\n"


class TestBackupDir:
    def test_missing_source_files_raise(self, tmp_path):
        """config / db 不存在时创建备份必须报错而非静默产生坏包。"""
        bak = BotlerBackup(str(tmp_path / "no-config.yaml"), str(tmp_path / "no.db"), str(tmp_path / "backups"))
        with pytest.raises(BackupError):
            bak.create_backup()

    def test_allowed_files_whitelist(self):
        assert set(ALLOWED_FILES) == {"config.yaml", "botler.db"}


# ---- MinIO 对象备份/还原（issue #205，mock MinIO，不做真实外呼） ----

class FakeMinioGetResponse:
    """minio get_object 响应最小替身：read / headers / close / release_conn。"""

    def __init__(self, data: bytes, content_type: str):
        self._data = data
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._data

    def close(self):
        pass

    def release_conn(self):
        pass


class FakeMinioClient:
    """minio SDK 客户端最小替身：内存桶 + 对象（含 content_type），记录调用。"""

    def __init__(self, objects: dict[str, tuple[bytes, str]] | None = None):
        self.buckets = {"public"}
        self.objects: dict[str, tuple[bytes, str]] = dict(objects or {})
        self.put_calls: list[tuple] = []

    def list_objects(self, bucket, prefix=None, recursive=False):
        for name, (data, ct) in sorted(self.objects.items()):
            yield SimpleNamespace(object_name=name, size=len(data), content_type=ct)

    def get_object(self, bucket, name):
        if name not in self.objects:
            raise Exception(f"NoSuchKey: {name}")
        data, ct = self.objects[name]
        return FakeMinioGetResponse(data, ct)

    def put_object(self, bucket, name, data, length, content_type=None):
        self.put_calls.append((bucket, name, length, content_type))
        self.objects[name] = (data.read(), content_type)


def _minio_settings(enabled: bool = True) -> SimpleNamespace:
    """带 minio_* 属性的 Settings 替身（对应 config.yaml minio 段）。"""
    return SimpleNamespace(
        minio_enabled=enabled,
        minio_endpoint="127.0.0.1:9000",
        minio_secure=False,
        minio_access_key="test-access",
        minio_secret_key="test-secret",
        minio_bucket="public",
        minio_public_base_url="http://img.example.com:9000",
        minio_verify_ssl=True,
    )


def _minio_backup_obj(tmp_path, monkeypatch, settings, objects):
    """构造启用 MinIO 的 BotlerBackup：config.get() 返回 settings 替身，
    minio.Minio 被替换为内存 fake（MinioImageStore 惰性建连时命中）。"""
    fake = FakeMinioClient(objects)
    monkeypatch.setattr("minio.Minio", lambda *a, **k: fake)

    config_path = tmp_path / "config.yaml"
    config_path.write_text("minio:\n  enabled: true\n", encoding="utf-8")
    db_path = tmp_path / "botler.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()

    config = SimpleNamespace(get=lambda: settings)
    bak = BotlerBackup(str(config_path), str(db_path), str(tmp_path / "backups"),
                       config=config)
    monkeypatch.setattr(BotlerBackup, "_restart", lambda self, delay=0.0: None)
    return bak, fake


class TestMinioBackup:
    IMG1 = b"\x89PNG-first-image"
    IMG2 = b"\x89PNG-second-image"

    @staticmethod
    def _objects():
        import hashlib
        return {
            hashlib.sha256(TestMinioBackup.IMG1).hexdigest(): (TestMinioBackup.IMG1, "image/png"),
            hashlib.sha256(TestMinioBackup.IMG2).hexdigest(): (TestMinioBackup.IMG2, "image/jpeg"),
        }

    def test_create_backup_includes_minio_objects(self, tmp_path, monkeypatch):
        """MinIO 启用时备份包应包含 minio/<哈希> 成员 + manifest minio 段。"""
        bak, _ = _minio_backup_obj(tmp_path, monkeypatch,
                                   _minio_settings(), self._objects())
        info = bak.create_backup()
        members = _read_archive(bak, info["name"])

        minio_members = [m for m in members if m.startswith("minio/")]
        assert len(minio_members) == 2
        # 对象名 = 图片 SHA-256 哈希
        import hashlib
        assert f"minio/{hashlib.sha256(self.IMG1).hexdigest()}" in minio_members
        # 对象内容原样入包
        digest2 = hashlib.sha256(self.IMG2).hexdigest()
        assert members[f"minio/{digest2}"] == self.IMG2

        manifest = json.loads(members["manifest.json"])
        assert manifest["minio"]["bucket"] == "public"
        assert manifest["minio"]["object_count"] == 2
        assert manifest["minio"]["size_bytes"] == len(self.IMG1) + len(self.IMG2)
        assert manifest["minio"]["objects"] == {
            hashlib.sha256(self.IMG1).hexdigest(): "image/png",
            hashlib.sha256(self.IMG2).hexdigest(): "image/jpeg",
        }
        # 返回信息带 minio 统计（不含逐对象 map）
        assert info["minio"]["object_count"] == 2
        assert "objects" not in info["minio"]

    def test_create_backup_disabled_minio_has_no_minio_members(self, tmp_path, monkeypatch):
        """MinIO 未启用时备份包与旧版一致：无 minio 成员 / 无 manifest minio 段。"""
        bak, fake = _minio_backup_obj(tmp_path, monkeypatch,
                                      _minio_settings(enabled=False), {})
        info = bak.create_backup()
        members = _read_archive(bak, info["name"])
        assert not any(m.startswith("minio/") for m in members)
        assert "minio" not in json.loads(members["manifest.json"])
        assert "minio" not in info
        assert fake.objects == {}

    def test_create_backup_empty_bucket_still_records_minio(self, tmp_path, monkeypatch):
        """启用但桶为空：备份仍记录 minio 段（object_count=0），无成员。"""
        bak, _ = _minio_backup_obj(tmp_path, monkeypatch, _minio_settings(), {})
        info = bak.create_backup()
        members = _read_archive(bak, info["name"])
        manifest = json.loads(members["manifest.json"])
        assert manifest["minio"]["object_count"] == 0
        assert manifest["minio"]["size_bytes"] == 0
        assert info["minio"]["object_count"] == 0
        assert not any(m.startswith("minio/") for m in members)

    def test_restore_roundtrip_with_minio(self, tmp_path, monkeypatch):
        """备份（含 MinIO）→ 篡改 config/db + 清空 MinIO → 恢复 → 三者全部还原。"""
        bak, fake = _minio_backup_obj(tmp_path, monkeypatch,
                                      _minio_settings(), self._objects())
        info = bak.create_backup()

        # 篡改 config 与 db
        Path(bak.config_path).write_text("changed: true\n", encoding="utf-8")
        conn = sqlite3.connect(bak.db_path)
        conn.execute("INSERT INTO t (v) VALUES ('dirty')")
        conn.commit()
        conn.close()
        # 清空 MinIO 桶（模拟数据丢失）
        fake.objects.clear()

        result = bak.restore_backup(info["name"])
        assert "changed: true" not in Path(bak.config_path).read_text(encoding="utf-8")
        conn = sqlite3.connect(bak.db_path)
        assert [r[0] for r in conn.execute("SELECT v FROM t")] == ["hello"]
        conn.close()
        # MinIO 对象按原内容 + 原 content_type 还原
        import hashlib
        d1, d2 = hashlib.sha256(self.IMG1).hexdigest(), hashlib.sha256(self.IMG2).hexdigest()
        assert fake.objects[d1] == (self.IMG1, "image/png")
        assert fake.objects[d2] == (self.IMG2, "image/jpeg")
        assert result["minio"] == {"bucket": "public", "object_count": 2}

    def test_restore_upload_flow_with_minio(self, tmp_path, monkeypatch):
        """上传恢复同样还原 MinIO 对象。"""
        bak, fake = _minio_backup_obj(tmp_path, monkeypatch,
                                      _minio_settings(), self._objects())
        info = bak.create_backup()
        import shutil
        copy_path = os.path.join(os.path.dirname(bak.backup_dir), "upload-copy.tar.gz")
        shutil.copy(os.path.join(bak.backup_dir, info["name"]), copy_path)
        fake.objects.clear()

        bak.restore_upload(copy_path)
        assert len(fake.objects) == 2
        assert fake.put_calls and all(c[3] in ("image/png", "image/jpeg")
                                      for c in fake.put_calls)

    def test_restore_with_minio_but_currently_disabled_raises(self, tmp_path, monkeypatch):
        """备份含 MinIO 数据但当前配置未启用 MinIO → 明确报错且不改 config/db。"""
        bak, _ = _minio_backup_obj(tmp_path, monkeypatch,
                                   _minio_settings(), self._objects())
        info = bak.create_backup()
        orig_config = Path(bak.config_path).read_text(encoding="utf-8")

        # 恢复时 MinIO 已停用（复用同一 config/db，仅切换配置替身）
        bak2 = BotlerBackup(bak.config_path, bak.db_path, bak.backup_dir,
                            config=SimpleNamespace(get=lambda: _minio_settings(enabled=False)))
        with pytest.raises(BackupError, match="MinIO"):
            bak2.restore_backup(info["name"])
        # config / db 未被覆盖（MinIO 还原失败则中止，不进入文件覆盖阶段）
        assert Path(bak.config_path).read_text(encoding="utf-8") == orig_config

    def test_minio_backup_failure_aborts_without_partial_file(self, tmp_path, monkeypatch):
        """MinIO 读取失败 → 备份中止报错，且不留半成品 tar.gz。"""
        bak, fake = _minio_backup_obj(tmp_path, monkeypatch,
                                      _minio_settings(), self._objects())

        def boom(bucket, name):
            raise Exception("connection refused")

        monkeypatch.setattr(fake, "get_object", boom)
        with pytest.raises(BackupError, match="MinIO"):
            bak.create_backup()
        assert bak.list_backups() == []  # 半成品已清理

    def test_rejects_minio_path_traversal_member(self, tmp_path, monkeypatch):
        """minio/../evil 成员必须拒绝（防路径穿越）。"""
        bak, _ = _minio_backup_obj(tmp_path, monkeypatch,
                                   _minio_settings(), self._objects())
        import hashlib
        content = b"evil"
        manifest = json.dumps({
            "app": "botler",
            "minio": {"bucket": "public", "object_count": 1,
                      "objects": {"../evil": "image/png"}},
            "files": {
                "config.yaml": {"sha256": hashlib.sha256(b"c").hexdigest()},
            },
        })
        path = os.path.join(os.path.dirname(bak.backup_dir), "evil.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            for mname, data in (("manifest.json", manifest.encode()),
                                ("config.yaml", b"c"),
                                ("minio/../evil", content)):
                bio = io.BytesIO(data)
                ti = tarfile.TarInfo(mname)
                ti.size = len(data)
                tf.addfile(ti, bio)
        with pytest.raises(BackupError, match="非法成员名"):
            bak.restore_upload(path)

    def test_rejects_minio_members_without_manifest_section(self, tmp_path, monkeypatch):
        """包内有 minio 成员但 manifest 缺 minio 段 → 拒绝（不一致）。"""
        bak, _ = _minio_backup_obj(tmp_path, monkeypatch,
                                   _minio_settings(), self._objects())
        import hashlib
        content = b"img"
        manifest = json.dumps({
            "app": "botler",
            "files": {"config.yaml": {"sha256": hashlib.sha256(b"c").hexdigest()}},
        })
        path = os.path.join(os.path.dirname(bak.backup_dir), "no-section.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            for mname, data in (("manifest.json", manifest.encode()),
                                ("config.yaml", b"c"),
                                ("minio/abc", content)):
                bio = io.BytesIO(data)
                ti = tarfile.TarInfo(mname)
                ti.size = len(data)
                tf.addfile(ti, bio)
        with pytest.raises(BackupError, match="minio"):
            bak.restore_upload(path)

    def test_restore_legacy_archive_without_minio_still_works(self, tmp_path, monkeypatch):
        """旧版备份包（无 minio 段）在 MinIO 启用环境下照常恢复，不触碰 MinIO。"""
        bak, fake = _minio_backup_obj(tmp_path, monkeypatch,
                                      _minio_settings(), self._objects())
        # 先创建旧版备份：用 config=None 的 BotlerBackup 生成（不含 minio）
        legacy = BotlerBackup(bak.config_path, bak.db_path, str(tmp_path / "legacy-backups"))
        monkeypatch.setattr(BotlerBackup, "_restart", lambda self, delay=0.0: None)
        legacy_info = legacy.create_backup()

        Path(bak.config_path).write_text("changed: true\n", encoding="utf-8")
        result = bak.restore_upload(os.path.join(legacy.backup_dir, legacy_info["name"]))
        assert "changed: true" not in Path(bak.config_path).read_text(encoding="utf-8")
        assert "minio" not in result
        assert fake.put_calls == []  # 未触碰 MinIO
