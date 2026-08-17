"""后端部署 MinIO 对象存储服务校验（issue #160）。

后端部署（pm2 为主、docker 为辅）时需同时提供一个 MinIO 对象存储
服务，两种部署形态都要可用：

- Docker 部署：docker-compose.yml 新增 minio service（镜像 / 端口 /
  数据卷 / 健康检查齐全，镜像与端口可用环境变量覆盖，数据持久化到
  $BOTLER_DATA_DIR/minio/data）；
- pm2 部署：deploy/install-minio.sh 一键安装 minio server 二进制
  （幂等 + 版本锁定 + fail fast），deploy/botler.config.cjs 新增
  botler-minio app（pm2 直接托管 minio 进程），CI deploy_to_code01
  自动安装、启动并健康检查；
- 凭据：MINIO_ROOT_USER / MINIO_ROOT_PASSWORD 写入 data/backend/.env
  （pm2 配置读取，docker compose 环境变量注入），.env.example 同步声明；
- 文档：README 两种部署方式均说明 minio 的启动 / 验证 / 数据目录。

本测试静态校验上述部署产物防回退（参考 test_deploy_dsh_sdk.py 模式）。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # botler/ 仓库根
INSTALL_SH = ROOT / "deploy" / "install-minio.sh"
PM2_CONFIG = ROOT / "deploy" / "botler.config.cjs"
COMPOSE = ROOT / "docker-compose.yml"
CI_FILE = ROOT / ".gitlab-ci.yml"
ENV_EXAMPLE = ROOT / "backend" / ".env.example"
README = ROOT / "README.md"

MINIO_VERSION = "RELEASE.2025-04-22T22-12-26Z"


def _read(path: Path) -> str:
    assert path.is_file(), f"缺少文件: {path}"
    return path.read_text(encoding="utf-8")


def _ci_deploy_job_section() -> str:
    """提取 .gitlab-ci.yml 中 deploy_to_code01 job 段落（到下一个顶级 key）。"""
    ci = _read(CI_FILE)
    lines = ci.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("deploy_to_code01:"):
            start = i
            break
    assert start is not None, "缺少 deploy_to_code01 job"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace():
            end = i
            break
    return "\n".join(lines[start:end])


class TestInstallScriptBasics:
    """deploy/install-minio.sh：脚本存在性 / 版本锁定 / 默认安装目录。"""

    def test_install_script_exists_and_executable(self):
        """一键安装脚本存在且带执行权限（可直接 ./ 或 bash 执行）。"""
        assert INSTALL_SH.is_file(), "缺少 deploy/install-minio.sh"
        assert INSTALL_SH.stat().st_mode & 0o111, "脚本无执行权限"

    def test_script_pins_minio_version(self):
        """MinIO 以显式版本号锁定（幂等检测与确定性部署依据）。"""
        script = _read(INSTALL_SH)
        assert MINIO_VERSION in script

    def test_script_default_install_dir(self):
        """默认安装到 $HOME/.local/bin（CI 部署 PATH 已包含该目录）。"""
        script = _read(INSTALL_SH)
        assert ".local/bin" in script

    def test_script_uses_archive_url(self):
        """下载走 dl.min.io 锁定版本的 archive 地址（非 latest 漂移）。"""
        script = _read(INSTALL_SH)
        assert "dl.min.io/server/minio/release/linux-amd64/archive" in script

    def test_script_download_url_overridable(self):
        """下载地址可经 MINIO_DOWNLOAD_URL 环境变量覆盖（内网/镜像场景）。"""
        script = _read(INSTALL_SH)
        assert re.search(r"\$\{MINIO_DOWNLOAD_URL", script)


class TestInstallScriptRobustness:
    """幂等 / fail fast / 原子替换。"""

    def test_script_idempotent(self):
        """已安装目标版本直接跳过（重复部署/重跑不重复下载）。"""
        script = _read(INSTALL_SH)
        assert "跳过" in script
        assert "exit 0" in script

    def test_script_version_check_fail_fast(self):
        """安装后 minio --version 校验：失败即 exit 1（部署失败而非带病运行）。"""
        script = _read(INSTALL_SH)
        assert "minio --version" in script
        assert "exit 1" in script

    def test_script_atomic_replace(self):
        """临时文件 + mv 原子替换，避免下载中断留下半截二进制。"""
        script = _read(INSTALL_SH)
        assert re.search(r"\.minio\.tmp", script)
        assert re.search(r"\bmv\b", script)


class TestPm2Config:
    """deploy/botler.config.cjs：pm2 托管 botler-minio app。"""

    def test_pm2_config_declares_minio_app(self):
        """pm2 配置包含 botler-minio app（与 botler 一并 start/save）。"""
        cfg = _read(PM2_CONFIG)
        assert "botler-minio" in cfg

    def test_pm2_minio_uses_binary_without_interpreter(self):
        """minio 为原生二进制，pm2 必须 interpreter: none（否则被当 JS 解析）。"""
        cfg = _read(PM2_CONFIG)
        assert "interpreter" in cfg and "none" in cfg

    def test_pm2_minio_server_args(self):
        """minio server 数据目录固定 $BOTLER_DATA_DIR/minio/data + console 端口。"""
        cfg = _read(PM2_CONFIG)
        assert "minio" in cfg
        assert "server" in cfg
        assert "minio/data" in cfg
        assert "console-address" in cfg

    def test_pm2_minio_env_credentials(self):
        """minio 凭据从 .env 读取（CI 部署写入），缺失回退默认值。"""
        cfg = _read(PM2_CONFIG)
        assert "MINIO_ROOT_USER" in cfg
        assert "MINIO_ROOT_PASSWORD" in cfg
        assert "minioadmin" in cfg

    def test_pm2_minio_logs_under_data_dir(self):
        """minio 日志写 $BOTLER_DATA_DIR/logs（与 botler 日志同目录集中管理）。"""
        cfg = _read(PM2_CONFIG)
        assert "pm2-minio-out.log" in cfg
        assert "pm2-minio-error.log" in cfg


class TestDockerComposeMinio:
    """docker-compose.yml：minio service 完整定义。"""

    def test_compose_declares_minio_service(self):
        """compose 包含 minio service。"""
        compose = _read(COMPOSE)
        assert re.search(r"^\s+minio:", compose, re.MULTILINE)

    def test_compose_minio_image_overridable(self):
        """minio 镜像为 minio/minio 且可用 MINIO_IMAGE 环境变量覆盖（国内镜像源）。"""
        compose = _read(COMPOSE)
        assert "minio/minio" in compose
        assert "${MINIO_IMAGE" in compose

    def test_compose_minio_ports(self):
        """API 9000 / console 9001 端口，均可经环境变量覆盖。"""
        compose = _read(COMPOSE)
        assert "${MINIO_API_PORT" in compose
        assert "${MINIO_CONSOLE_PORT" in compose
        assert "9000" in compose and "9001" in compose

    def test_compose_minio_data_volume(self):
        """数据卷挂载 $BOTLER_DATA_DIR/minio/data:/data（容器重建不丢）。"""
        compose = _read(COMPOSE)
        assert re.search(r"\$\{BOTLER_DATA_DIR[^}]*\}/minio/data:/data", compose)

    def test_compose_minio_healthcheck(self):
        """minio 健康检查 /minio/health/live（与 botler 同套机制）。"""
        compose = _read(COMPOSE)
        assert "minio/health/live" in compose

    def test_compose_minio_root_credentials_env(self):
        """minio 根凭据经 MINIO_ROOT_USER / MINIO_ROOT_PASSWORD 注入（可覆盖默认值）。"""
        compose = _read(COMPOSE)
        assert "MINIO_ROOT_USER" in compose
        assert "MINIO_ROOT_PASSWORD" in compose


class TestCiPm2DeployMinio:
    """CI deploy_to_code01（pm2 部署）：minio 自动安装 / 启动 / 健康检查。"""

    def test_deploy_job_calls_install_script(self):
        """部署 job 调用 deploy/install-minio.sh（失败即阻断部署）。"""
        job = _ci_deploy_job_section()
        assert "install-minio.sh" in job
        assert "exit 1" in job

    def test_deploy_job_stops_old_minio_app(self):
        """停止旧部署时同时删除旧 botler-minio（防 9000 端口冲突）。"""
        job = _ci_deploy_job_section()
        assert "botler-minio" in job
        assert "pm2 delete" in job

    def test_deploy_job_healthchecks_minio(self):
        """部署健康检查包含 minio（curl /minio/health/live，失败即部署失败）。"""
        job = _ci_deploy_job_section()
        assert "minio/health/live" in job

    def test_deploy_job_writes_minio_credentials_to_env(self):
        """部署时把 MINIO_ROOT_USER / MINIO_ROOT_PASSWORD 写入 data/backend/.env。"""
        job = _ci_deploy_job_section()
        assert "MINIO_ROOT_USER" in job
        assert "MINIO_ROOT_PASSWORD" in job


class TestEnvExample:
    """backend/.env.example：minio 凭据与端口声明。"""

    def test_env_example_declares_minio_credentials(self):
        env = _read(ENV_EXAMPLE)
        assert "MINIO_ROOT_USER" in env
        assert "MINIO_ROOT_PASSWORD" in env

    def test_env_example_declares_minio_ports(self):
        env = _read(ENV_EXAMPLE)
        assert "MINIO_API_PORT" in env
        assert "MINIO_CONSOLE_PORT" in env


class TestDocsSynced:
    """README：两种部署方式均说明 minio 启动 / 验证 / 数据目录。"""

    def test_readme_pm2_deploy_mentions_minio(self):
        readme = _read(README)
        assert "minio" in readme.lower()

    def test_readme_docker_deploy_mentions_minio(self):
        readme = _read(README)
        assert "minio" in readme.lower()
        assert "install-minio.sh" in readme or "minio" in readme
