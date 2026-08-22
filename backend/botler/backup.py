"""备份与恢复。

备份范围：config.yaml（唯一配置事实来源）+ botler.db（SQLite）
+ MinIO 图片桶对象（启用时，issue #205）。
- 备份包：botler-backup-<YYYYmmdd-HHMMSS>.tar.gz，内含 manifest.json（各文件
  sha256 清单，恢复时校验）+ config.yaml + botler.db（用 sqlite backup API
  生成一致性快照，WAL 模式下安全）+ MinIO 桶对象（``minio/<对象名>`` 成员，
  MinIO 启用且配置完整时——对象名即图片 SHA-256 哈希，天然幂等可增量）。
- manifest.json 的 minio 段记录桶名 / 对象数 / 总大小 / 每对象
  content_type（恢复时按原类型还原上传，识图 URL 的 Content-Type 保持一致）。
- 存储：备份目录默认为 <backend>/backups（Docker 部署挂载 data/backups）。
- 触发：手动（API）+ 定时（每天 03:00 Asia/Shanghai，config backup.enabled 开关）。
- 保留策略：按 mtime 清理超过 retention_days（默认 30）天的备份。
- 恢复：校验成员名白名单（防路径穿越）+ manifest 校验和，先还原 MinIO 对象
  （对象名 = 内容哈希，幂等覆盖），再覆盖 config.yaml / botler.db，最后自动
  重启进程（os.execv，保持 PID 1，Docker/pm2/systemd/dev 通用；重启后
  scheduler 会把 running 任务标记 interrupted 并重新入队）。
  MinIO 未启用 / 未配置完整时创建的备份不含 MinIO 数据（兼容旧版备份包）。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import sqlite3
import sys
import tarfile
import tempfile
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# 备份包内允许的成员（白名单，防路径穿越与任意文件注入）
ALLOWED_FILES = ("config.yaml", "botler.db")
# MinIO 对象在备份包内的成员前缀（minio/<对象名>，issue #205）
MINIO_MEMBER_PREFIX = "minio/"

# 备份目录：环境变量覆盖，默认 <backend>/backups（compose 挂载 data/backups）
BACKUP_DIR = os.environ.get(
    "BOTLER_BACKUP_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups"),
)

# 定时备份时刻（每天 03:00，Asia/Shanghai）
SCHEDULED_HOUR = 3
SCHEDULED_MINUTE = 0
SCHEDULE_TIMEZONE = "Asia/Shanghai"

_NAME_PREFIX = "botler-backup-"


class BackupError(Exception):
    """备份/恢复业务错误。status_code 供 API 层映射（404 / 400）。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _add_bytes(tf: tarfile.TarFile, name: str, content: bytes) -> None:
    data = tempfile.SpooledTemporaryFile()
    data.write(content)
    data.seek(0)
    ti = tarfile.TarInfo(name)
    ti.size = len(content)
    ti.mtime = int(datetime.now().timestamp())
    tf.addfile(ti, data)
    data.close()

def _minio_object_name_ok(obj: str) -> bool:
    """MinIO 对象名安全校验：拒绝空名 / 反斜杠 / 路径穿越段（``..`` 等）。

    备份包内对象成员统一以 ``minio/<对象名>`` 存放，对象名必须可安全
    还原成 MinIO 对象键（本仓库对象名即图片 SHA-256 哈希，天然满足）。
    """
    if not obj or "\\" in obj or obj.startswith("/"):
        return False
    return all(p and p not in (".", "..") for p in obj.split("/"))


def _minio_store(config):
    """按当前配置构造识图图片存储（惰性客户端，不建连）。

    返回 None 表示 MinIO 未启用或配置不完整（endpoint / 凭据 /
    public_base_url 缺失）——此时桶内不会有业务图片数据，备份跳过
    MinIO 段，与 issue #164「配置不完整视为不可用」语义一致。
    """
    if config is None:
        return None
    try:
        from .minio_client import image_store_from_settings
        return image_store_from_settings(config.get())
    except Exception as exc:  # noqa: BLE001 配置异常时跳过 MinIO 备份
        logger.warning("读取 MinIO 配置失败，本次备份跳过 MinIO 数据: %s", exc)
        return None


class BotlerBackup:
    """备份管理：创建 / 列表 / 删除 / 保留清理 / 恢复 + 定时任务。

    config 参数用于读取 backup.enabled / backup.retention_days（None 时用默认值）。
    """

    def __init__(self, config_path: str, db_path: str,
                 backup_dir: str = BACKUP_DIR, config=None):
        self.config_path = config_path
        self.db_path = db_path
        self.backup_dir = backup_dir
        self.config = config
        # 应用入口注入数据保留清理回调（issue #204）；独立使用/既有测试保持可选。
        self.pre_backup_cleanup = None
        self._aps = BackgroundScheduler(timezone=SCHEDULE_TIMEZONE)

    # ---- 配置读取 ----

    def _retention_days(self) -> int:
        if self.config is not None:
            try:
                return int(self.config.get().backup_retention_days)
            except Exception:  # noqa: BLE001 配置解析异常时回落默认值
                pass
        return 30

    # ---- 路径安全 ----

    def _safe_path(self, name: str) -> str:
        """校验备份文件名：仅允许 backups/ 下 botler-backup-*.tar.gz，拒绝路径穿越。"""
        base = os.path.basename(name)
        if base != name or not base.startswith(_NAME_PREFIX) or not base.endswith(".tar.gz"):
            raise BackupError("非法备份文件名", status_code=404)
        path = os.path.join(self.backup_dir, base)
        if not os.path.isfile(path):
            raise BackupError(f"备份不存在: {base}", status_code=404)
        return path

    # ---- 创建 / 列表 / 删除 ----

    def create_backup(self, trigger: str = "manual") -> dict:
        """创建备份包并返回信息。创建后按保留策略清理旧备份。

        快照前先执行数据保留清理，避免备份包重新携带已过期的运行明细。
        """
        if self.pre_backup_cleanup is not None:
            self.pre_backup_cleanup()
        for path in (self.config_path, self.db_path):
            if not os.path.isfile(path):
                raise BackupError(f"待备份文件不存在: {path}（备份中止）")
        os.makedirs(self.backup_dir, exist_ok=True)

        ts = datetime.now()
        name = f"{_NAME_PREFIX}{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        target = os.path.join(self.backup_dir, name)

        # db 一致性快照：sqlite backup API（WAL 下含未 checkpoint 数据）
        tmp_db = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                tmp_db = f.name
            with sqlite3.connect(self.db_path) as src, sqlite3.connect(tmp_db) as dst:
                src.backup(dst)

            files = {"config.yaml": self.config_path, "botler.db": tmp_db}
            manifest = {
                "app": "botler",
                "backup_version": 1,
                "created_at": ts.isoformat(timespec="seconds"),
                "trigger": trigger,
                "files": {fname: {"sha256": _sha256(path)}
                          for fname, path in files.items()},
            }
            minio_info = None
            store = _minio_store(self.config)
            try:
                with tarfile.open(target, "w:gz") as tf:
                    for fname, path in files.items():
                        _add_bytes(tf, fname, open(path, "rb").read())
                    # MinIO 启用且配置完整：把图片桶全部对象镜像进备份包
                    if store is not None:
                        minio_info = self._backup_minio(tf, store)
                        manifest["minio"] = {
                            "bucket": minio_info["bucket"],
                            "object_count": minio_info["object_count"],
                            "size_bytes": minio_info["size_bytes"],
                            "objects": minio_info["objects"],
                        }
                    _add_bytes(tf, "manifest.json",
                               json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            except BackupError:
                # MinIO 备份失败：删除半成品备份包（要么完整、要么没有）
                if os.path.exists(target):
                    os.unlink(target)
                raise
            except Exception:
                if os.path.exists(target):
                    os.unlink(target)
                raise
        finally:
            if tmp_db and os.path.exists(tmp_db):
                os.unlink(tmp_db)

        pruned = self.prune_backups(self._retention_days())
        info = {"name": name, "size": os.path.getsize(target),
                "created_at": ts.isoformat(timespec="seconds"), "pruned": pruned}
        if minio_info is not None:
            info["minio"] = {k: v for k, v in minio_info.items() if k != "objects"}
        logger.info("备份已创建: %s（%s 字节，触发=%s，清理 %s 个旧备份）",
                    name, os.path.getsize(target), trigger, len(pruned))
        return info

    def list_backups(self) -> list[dict]:
        """列出全部备份（按创建时间倒序）。"""
        items = []
        if os.path.isdir(self.backup_dir):
            for name in os.listdir(self.backup_dir):
                if not (name.startswith(_NAME_PREFIX) and name.endswith(".tar.gz")):
                    continue
                path = os.path.join(self.backup_dir, name)
                if not os.path.isfile(path):
                    continue
                items.append({
                    "name": name,
                    "size": os.path.getsize(path),
                    "created_at": datetime.fromtimestamp(os.path.getmtime(path))
                        .isoformat(timespec="seconds"),
                })
        items.sort(key=lambda b: b["created_at"], reverse=True)
        return items

    def delete_backup(self, name: str) -> dict:
        path = self._safe_path(name)
        os.unlink(path)
        logger.info("备份已删除: %s", name)
        return {"name": name, "deleted": True}

    def prune_backups(self, retention_days: int) -> list[str]:
        """删除 mtime 早于 retention_days 的备份，返回被删列表。"""
        if retention_days <= 0:
            removed = [b["name"] for b in self.list_backups()]
            for name in removed:
                os.unlink(os.path.join(self.backup_dir, name))
            if removed:
                logger.info("保留策略（%s 天）清理备份: %s", retention_days, removed)
            return removed
        cutoff = datetime.now().timestamp() - retention_days * 24 * 3600
        removed = []
        for item in self.list_backups():
            if os.path.getmtime(os.path.join(self.backup_dir, item["name"])) < cutoff:
                os.unlink(os.path.join(self.backup_dir, item["name"]))
                removed.append(item["name"])
        if removed:
            logger.info("保留策略（%s 天）清理备份: %s", retention_days, removed)
        return removed

    # ---- MinIO 对象备份（issue #205） ----

    def _backup_minio(self, tf: tarfile.TarFile, store) -> dict:
        """把 MinIO 图片桶全部对象镜像进备份包 ``minio/<对象名>`` 成员。

        :return: {"bucket", "object_count", "size_bytes", "objects"}
            objects 为 {对象名: content_type}（还原时按原类型上传）。
        :raises BackupError: 对象名非法 / MinIO 读取失败（备份中止）。
        """
        cfg = store.cfg
        objects: dict[str, str] = {}
        object_count = 0
        size_bytes = 0
        try:
            for obj in store.client.list_objects(cfg.bucket, recursive=True):
                name = obj.object_name
                if not _minio_object_name_ok(name):
                    raise BackupError(f"MinIO 对象名非法，无法备份: {name}")
                try:
                    resp = store.client.get_object(cfg.bucket, name)
                except Exception as exc:  # noqa: BLE001 底层错误统一转业务异常
                    raise BackupError(f"读取 MinIO 对象失败 {cfg.bucket}/{name}: {exc}") from exc
                try:
                    data = resp.read()
                    content_type = (resp.headers.get("Content-Type")
                                    or getattr(obj, "content_type", None)
                                    or "application/octet-stream")
                    _add_bytes(tf, f"{MINIO_MEMBER_PREFIX}{name}", data)
                    objects[name] = content_type
                    object_count += 1
                    size_bytes += len(data)
                finally:
                    try:
                        resp.close()
                        resp.release_conn()
                    except Exception:  # noqa: BLE001 关闭失败不影响备份
                        pass
        except BackupError:
            raise
        except Exception as exc:  # noqa: BLE001 列表/迭代异常统一转业务异常
            raise BackupError(f"读取 MinIO 桶对象失败 {cfg.bucket}: {exc}") from exc
        logger.info("MinIO 桶已纳入备份: %s（%s 个对象，%s 字节）",
                    cfg.bucket, object_count, size_bytes)
        return {"bucket": cfg.bucket, "object_count": object_count,
                "size_bytes": size_bytes, "objects": objects}

    # ---- 恢复 ----

    def _validate_archive(self, archive_path: str) -> dict:
        """校验备份包：成员白名单 + manifest + sha256，返回 {manifest, contents}。"""
        contents: dict[str, bytes] = {}
        manifest: dict | None = None
        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    name = member.name
                    if name == "manifest.json":
                        manifest = json.loads(tf.extractfile(member).read().decode("utf-8"))
                        continue
                    if name.startswith("/") or ".." in name.split("/"):
                        raise BackupError(
                            f"非法成员名: {name}（只允许 {', '.join(ALLOWED_FILES)} 与 minio/<对象名>）")
                    if name in ALLOWED_FILES:
                        contents[name] = tf.extractfile(member).read()
                    elif name.startswith(MINIO_MEMBER_PREFIX):
                        if not _minio_object_name_ok(name[len(MINIO_MEMBER_PREFIX):]):
                            raise BackupError(
                                f"非法成员名: {name}（只允许 {', '.join(ALLOWED_FILES)} 与 minio/<对象名>）")
                        contents[name] = tf.extractfile(member).read()
                    else:
                        raise BackupError(
                            f"非法成员名: {name}（只允许 {', '.join(ALLOWED_FILES)} 与 minio/<对象名>）")
        except tarfile.TarError as e:
            raise BackupError(f"备份包损坏: {e}") from e

        if manifest is None:
            raise BackupError("备份包缺少 manifest.json，已拒绝恢复")
        if manifest.get("app") != "botler":
            raise BackupError("备份包不是 botler 备份（app 标识不符），已拒绝恢复")

        # MinIO 段一致性：包内成员与 manifest 声明必须互相印证
        minio_meta = manifest.get("minio") or {}
        minio_members = [k for k in contents if k.startswith(MINIO_MEMBER_PREFIX)]
        if minio_members and not minio_meta:
            raise BackupError("备份包包含 MinIO 对象但 manifest 缺少 minio 段，已拒绝恢复")
        if minio_meta.get("object_count", 0) > 0 and not minio_members:
            raise BackupError("manifest 声明包含 MinIO 对象但包内缺失，备份包不完整")

        # 校验和逐一比对（包内存在即校验；缺失的文件不替换；
        # MinIO 对象不做逐对象校验和——对象名即内容 SHA-256，tar 层有 CRC）
        for fname, content in contents.items():
            if fname.startswith(MINIO_MEMBER_PREFIX):
                continue
            expected = (manifest.get("files") or {}).get(fname, {}).get("sha256")
            if not expected:
                raise BackupError(f"manifest 未记录 {fname} 的校验和，已拒绝恢复")
            if hashlib.sha256(content).hexdigest() != expected:
                raise BackupError(f"{fname} 校验和不符（文件损坏或已被篡改），已拒绝恢复")
        return {"manifest": manifest, "contents": contents}

    def _apply(self, contents: dict[str, bytes]) -> None:
        """原子替换目标文件（先写临时文件再 rename）。"""
        for fname in ALLOWED_FILES:
            if fname not in contents:
                continue
            target = self.config_path if fname == "config.yaml" else self.db_path
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = f"{target}.restore-tmp"
            with open(tmp, "wb") as f:
                f.write(contents[fname])
            os.replace(tmp, target)
            logger.warning("已恢复文件: %s", target)

    def _apply_minio(self, contents: dict[str, bytes], manifest: dict) -> dict | None:
        """把备份包内 MinIO 对象还原上传回 MinIO（对象名 = 内容哈希，幂等覆盖）。

        在覆盖 config.yaml / botler.db 之前执行：MinIO 还原失败时抛
        BackupError 中止，config/db 保持不变、不重启，可修复后重试。
        包内无 MinIO 数据（旧版备份 / 未启用时创建）返回 None。
        """
        minio_meta = manifest.get("minio") or {}
        members = {k: v for k, v in contents.items()
                   if k.startswith(MINIO_MEMBER_PREFIX)}
        if not members:
            return None
        store = _minio_store(self.config)
        if store is None:
            raise BackupError(
                "备份包含 MinIO 对象数据，但当前配置未启用 / 未配置完整 "
                "MinIO，无法还原——请先在设置页启用并配好 MinIO（minio 段）后重试")
        objects_meta = minio_meta.get("objects") or {}
        bucket = store.cfg.bucket
        for name, data in members.items():
            obj_name = name[len(MINIO_MEMBER_PREFIX):]
            content_type = objects_meta.get(obj_name) or "application/octet-stream"
            try:
                store.client.put_object(
                    bucket, obj_name, io.BytesIO(data), len(data),
                    content_type=content_type)
            except Exception as exc:  # noqa: BLE001 底层错误统一转业务异常
                raise BackupError(
                    f"MinIO 对象还原失败 {bucket}/{obj_name}: {exc}") from exc
        logger.warning("MinIO 对象已还原: %s 桶 %s 个对象", bucket, len(members))
        return {"bucket": bucket, "object_count": len(members)}

    def restore_backup(self, name: str) -> dict:
        """从服务器本地历史备份恢复（还原 MinIO 对象 + 覆盖 config.yaml / botler.db）。"""
        path = self._safe_path(name)
        result = self._validate_archive(path)
        minio_restored = self._apply_minio(result["contents"], result["manifest"])
        self._apply(result["contents"])
        logger.warning("备份 %s 恢复完成，服务即将自动重启", name)
        self._restart(delay=2.0)
        resp: dict = {"name": name, "restored": list(result["contents"])}
        if minio_restored is not None:
            resp["minio"] = minio_restored
        return resp

    def restore_upload(self, uploaded_path: str) -> dict:
        """从上传的备份包恢复（同样完整校验，不落盘到备份目录）。"""
        result = self._validate_archive(uploaded_path)
        minio_restored = self._apply_minio(result["contents"], result["manifest"])
        self._apply(result["contents"])
        logger.warning("上传备份恢复完成，服务即将自动重启")
        self._restart(delay=2.0)
        resp: dict = {"restored": list(result["contents"])}
        if minio_restored is not None:
            resp["minio"] = minio_restored
        return resp

    # ---- 重启 ----

    def _restart(self, delay: float = 2.0) -> None:
        """延迟重启当前进程：os.execv 原地替换（保持 PID 1）。

        Docker（restart policy 依赖进程退出）下 execv 不退出容器进程本身，
        pm2/systemd/uvicorn --reload 场景同样成立；新进程重新加载
        config.yaml / botler.db，scheduler 自动把 running 任务重新入队。
        """

        def _do() -> None:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                logging.shutdown()
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception as e:  # noqa: BLE001  execv 失败（理论上极罕见）
                logger.error("进程重启失败: %s", e)
                os._exit(1)  # noqa: PLR1722  让容器 restart policy 兜底

        if delay > 0:
            threading.Timer(delay, _do).start()
        else:
            _do()

    # ---- 定时备份 ----

    def start_scheduler(self) -> None:
        """每天 03:00 定时备份（Asia/Shanghai）。开关读取 config backup.enabled。"""
        if self._aps.running:
            return
        trigger = CronTrigger(hour=SCHEDULED_HOUR, minute=SCHEDULED_MINUTE)
        self._aps.add_job(
            self._scheduled_backup, trigger,
            id="botler-backup", name="定时备份",
            coalesce=True, max_instances=1, replace_existing=True,
        )
        self._aps.start()
        logger.info("定时备份已启动（每天 %02d:%02d Asia/Shanghai，保留 %s 天）",
                    SCHEDULED_HOUR, SCHEDULED_MINUTE, self._retention_days())

    def stop_scheduler(self) -> None:
        if self._aps.running:
            self._aps.shutdown(wait=False)

    def _scheduled_backup(self) -> None:
        if self.config is not None:
            try:
                if not self.config.get().backup_enabled:
                    logger.info("定时备份已关闭（backup.enabled=false），本次跳过")
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning("定时备份读取配置失败，按默认继续: %s", e)
        try:
            self.create_backup(trigger="scheduled")
        except Exception as e:  # noqa: BLE001
            logger.error("定时备份失败: %s", e)
