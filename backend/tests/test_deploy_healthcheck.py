"""Docker 部署 healthcheck 产物校验（issue #207）。

背景：docker compose botler 主服务无 healthcheck 时，uvicorn 进程活着但
事件循环卡死 / 依赖失效（MinIO 不可达、磁盘写满）容器仍显示 running，
`docker compose ps` 看不出异常（假死无感知）。本次交付：

- docker-compose.yml botler 服务 healthcheck：容器内 curl /api/health
  （镜像内置 curl，Dockerfile apt 安装），interval 30s / timeout 5s /
  retries 3（issue #207 验收标准「compose ps 显示 botler healthy」）；
- Dockerfile HEALTHCHECK 指令同参（镜像级兜底，不经 compose 也能感知）；
- /api/health 扩展依赖探测（backend/botler/health.py：MinIO 连通——仅
  minio.enabled=true 时探测、磁盘空间），关键依赖失败返回 503 →
  healthcheck 失败 → 容器 unhealthy（依赖失效可感知）；
- deploy/verify-docker.sh 同步校验：compose ps 显示 botler healthy +
  模拟事件循环卡死（SIGSTOP 主进程）→ 容器变 unhealthy → 恢复 → healthy
  （验收标准「模拟事件循环卡死时容器变 unhealthy」「verify-docker.sh 覆盖」）。

本测试静态校验上述部署产物防回退（参考 test_deploy_minio.py 模式）。
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]  # botler/ 仓库根
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"
VERIFY_SH = ROOT / "deploy" / "verify-docker.sh"
HEALTH_PY = ROOT / "backend" / "botler" / "health.py"
MAIN_PY = ROOT / "backend" / "botler" / "main.py"
README = ROOT / "README.md"

# issue #207 约定的 healthcheck 参数（compose 与 Dockerfile 必须一致）
HC_PARAMS = {
    "interval": "30s",
    "timeout": "5s",
    "retries": "3",
    "start_period": "60s",
}


def _read(path: Path) -> str:
    assert path.is_file(), f"缺少文件: {path}"
    return path.read_text(encoding="utf-8")


def _compose_service_block(name: str) -> str:
    """提取 docker-compose.yml 中指定 service 的定义块（到下一个顶级 key）。"""
    compose = _read(COMPOSE)
    lines = compose.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line == f"  {name}:":
            start = i
            break
    assert start is not None, f"缺少 {name} service"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^  [a-z]", lines[i]):  # 下一个顶级 service key
            end = i
            break
    return "\n".join(lines[start:end])


class TestComposeBotlerHealthcheck:
    """docker-compose.yml：botler 服务 healthcheck 完整定义。"""

    def test_compose_botler_has_healthcheck(self):
        """botler 服务含 healthcheck（探测 /api/health，容器内 curl）。"""
        block = _compose_service_block("botler")
        assert "healthcheck:" in block
        assert "curl" in block
        assert "127.0.0.1:8000/api/health" in block

    def test_compose_botler_healthcheck_params(self):
        """healthcheck 参数与 issue 约定一致：interval 30s / timeout 5s /
        retries 3（start_period 60s 覆盖启动期依赖就绪）。"""
        block = _compose_service_block("botler")
        for key, value in HC_PARAMS.items():
            assert re.search(rf"^\s*{key}:\s*{value}\s*$", block, re.MULTILINE), (
                f"botler healthcheck 缺少 {key}: {value}"
            )

    def test_compose_healthcheck_url_unique_to_botler(self):
        """8000 端口 /api/health 探针只属于 botler（terminal 8765 / minio 9000）。"""
        compose = _read(COMPOSE)
        assert compose.count("127.0.0.1:8000/api/health") == 1
        assert "127.0.0.1:8765/terminal/health" in compose  # terminal 独立探针
        assert "127.0.0.1:9000/minio/health/live" in compose  # minio 独立探针


class TestDockerfileHealthcheck:
    """Dockerfile：镜像内置 curl + HEALTHCHECK 指令（镜像级兜底）。"""

    def test_dockerfile_installs_curl(self):
        """镜像 apt 安装 curl（容器内 healthcheck 探针可用）。"""
        dockerfile = _read(DOCKERFILE)
        assert "curl" in dockerfile
        assert "apt-get install" in dockerfile

    def test_dockerfile_has_healthcheck_instruction(self):
        """Dockerfile HEALTHCHECK 探测 /api/health 且参数与 compose 一致
        （Dockerfile 参数为连字符写法 --start-period，compose 为下划线
        start_period，语义一致）。"""
        dockerfile = _read(DOCKERFILE)
        assert "HEALTHCHECK" in dockerfile
        assert "127.0.0.1:8000/api/health" in dockerfile
        # Dockerfile 标志写法（连字符）与 compose 键（下划线）一一对应
        docker_flags = {k.replace("_", "-"): v for k, v in HC_PARAMS.items()}
        for key, value in docker_flags.items():
            assert f"--{key}={value}" in dockerfile, f"HEALTHCHECK 缺少 --{key}={value}"


class TestHealthEndpointDeps:
    """/api/health 依赖探测接入（issue #207 可选增强：MinIO/磁盘）。"""

    def test_health_module_exists(self):
        """backend/botler/health.py 存在（依赖探测纯函数模块）。"""
        assert HEALTH_PY.is_file()

    def test_health_module_probes_minio_and_disk(self):
        """依赖探测覆盖 MinIO 连通（live 探针）与磁盘空间。"""
        src = _read(HEALTH_PY)
        assert "minio/health/live" in src
        assert "disk_usage" in src

    def test_main_health_endpoint_uses_deps(self):
        """main.py /api/health 调用依赖探测并支持 503（关键依赖失败）。"""
        main_src = _read(MAIN_PY)
        assert "build_deps_report" in main_src
        assert "deps_critical_failed" in main_src
        assert "status_code=503" in main_src


class TestVerifyScript:
    """deploy/verify-docker.sh：healthcheck 状态与假死模拟覆盖。"""

    def test_verify_script_checks_compose_healthy(self):
        """冒烟脚本校验 compose ps 显示 botler healthy（不只是宿主机 curl）。"""
        script = _read(VERIFY_SH)
        assert "compose ps" in script or "compose -p botler-verify ps" in script
        assert "healthy" in script

    def test_verify_script_hang_simulation(self):
        """冒烟脚本模拟事件循环卡死：SIGSTOP 主进程 → 断言容器 unhealthy。"""
        script = _read(VERIFY_SH)
        assert "kill -STOP 1" in script
        assert "unhealthy" in script

    def test_verify_script_hang_recovery(self):
        """卡死恢复：SIGCONT 后断言容器重新 healthy。"""
        script = _read(VERIFY_SH)
        assert "kill -CONT 1" in script
        # 恢复段至少出现两次 healthy 断言（首次进入 healthy + 恢复后 healthy）
        assert script.count("healthy") >= 2


class TestDocsSynced:
    """README：Docker 部署说明包含 healthcheck 状态与冒烟覆盖。"""

    def test_readme_docker_ps_shows_healthy(self):
        """README Docker 部署验证列出 compose ps 状态 healthy。"""
        readme = _read(README)
        assert "docker compose ps" in readme
        assert "healthy" in readme

    def test_readme_mentions_verify_docker(self):
        """README 提到 verify-docker.sh 冒烟校验。"""
        readme = _read(README)
        assert "verify-docker.sh" in readme
