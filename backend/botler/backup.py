"""备份与恢复。

备份范围：config.yaml（唯一配置事实来源）+ botler.db（SQLite）。
- 备份包：botler-backup-<YYYYmmdd-HHMMSS>.tar.gz，内含 manifest.json（各文件
  sha256 清单，恢复时校验）+ config.yaml + botler.db（用 sqlite backup API
  生成一致性快照，WAL 模式下安全）。
- 存储：备份目录默认为 <backend>/backups（Docker 部署挂载 data/backups）。
- 触发：手动（API）+ 定时（每天 03:00 Asia/Shanghai，config backup.enabled 开关）。
- 保留策略：按 mtime 清理超过 retention_days（默认 30）天的备份。
- 恢复：校验成员名白名单（防路径穿越）+ manifest 校验和，覆盖 config.yaml /
  botler.db 后自动重启进程（os.execv，保持 PID 1，Docker/pm2/systemd/dev
  通用；重启后 scheduler 会把 running 任务标记 interrupted 并重新入队）。
"""

from __future__ import annotations

import hashlib
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
        """创建备份包并返回信息。创建后按保留策略清理旧备份。"""
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
            with tarfile.open(target, "w:gz") as tf:
                for fname, path in files.items():
                    _add_bytes(tf, fname, open(path, "rb").read())
                _add_bytes(tf, "manifest.json",
                           json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        finally:
            if tmp_db and os.path.exists(tmp_db):
                os.unlink(tmp_db)

        pruned = self.prune_backups(self._retention_days())
        info = {"name": name, "size": os.path.getsize(target),
                "created_at": ts.isoformat(timespec="seconds"), "pruned": pruned}
        logger.info("备份已创建: %s（%s，触发=%s，清理 %s 个旧备份）",
                    name, trigger, trigger, len(pruned))
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
                    if name.startswith("/") or ".." in name.split("/") or name not in ALLOWED_FILES:
                        raise BackupError(f"非法成员名: {name}（只允许 {', '.join(ALLOWED_FILES)}）")
                    contents[name] = tf.extractfile(member).read()
        except tarfile.TarError as e:
            raise BackupError(f"备份包损坏: {e}") from e

        if manifest is None:
            raise BackupError("备份包缺少 manifest.json，已拒绝恢复")
        if manifest.get("app") != "botler":
            raise BackupError("备份包不是 botler 备份（app 标识不符），已拒绝恢复")

        # 校验和逐一比对（包内存在即校验；缺失的文件不替换）
        for fname, content in contents.items():
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

    def restore_backup(self, name: str) -> dict:
        """从服务器本地历史备份恢复（覆盖 config.yaml / botler.db）。"""
        path = self._safe_path(name)
        result = self._validate_archive(path)
        self._apply(result["contents"])
        logger.warning("备份 %s 恢复完成，服务即将自动重启", name)
        self._restart(delay=2.0)
        return {"name": name, "restored": list(result["contents"])}

    def restore_upload(self, uploaded_path: str) -> dict:
        """从上传的备份包恢复（同样完整校验，不落盘到备份目录）。"""
        result = self._validate_archive(uploaded_path)
        self._apply(result["contents"])
        logger.warning("上传备份恢复完成，服务即将自动重启")
        self._restart(delay=2.0)
        return {"restored": list(result["contents"])}

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
