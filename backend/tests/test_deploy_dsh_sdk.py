"""pm2 部署 deepseek-harness SDK 自动安装校验（issue #112 跟进）。

issue #112 首轮仅覆盖 Docker 镜像内置 SDK（test_dockerfile_dsh.py 校验）；
用户反馈 pm2 部署实例仍缺 SDK。本次新增 `deploy/install-dsh-sdk.sh`
一键安装脚本（单一事实来源）：CI pm2 部署（deploy_to_code01）在主依赖
安装后自动调用，手动 pm2/systemd 部署也可直接执行。本测试静态校验
部署产物防回退：

- 脚本存在且可执行，默认装入 backend/.venv（pm2 后端 venv）；
- 显式全版本号（rc 预发布版缩写装不到）+ 阿里镜像（清华源未同步
  rc 版）+ DSH_INDEX_URL 环境变量可覆盖（内网代理场景）；
- 安装后 import 校验（装不上立即失败，fail fast）；
- 幂等：已装目标版本直接跳过；
- 优先 uv pip（CI venv 由 uv 创建、无 pip seed），回退 venv 内 pip；
- CI deploy_to_code01 job 在主依赖安装后调用脚本；
- 部署文档 / README / dsh_runner 安装指引同步指向脚本。
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]  # botler/ 仓库根
INSTALL_SH = ROOT / "deploy" / "install-dsh-sdk.sh"
CI_FILE = ROOT / ".gitlab-ci.yml"
DSH_DOC = ROOT / "docs" / "dsh-engine-deployment.md"
DSH_RUNNER = ROOT / "backend" / "botler" / "dsh_runner.py"
README = ROOT / "README.md"

SDK_PIN = "deepseek-harness-sdk==0.1.0rc6"


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
    # job 段落结束于下一个顶格（无缩进）的 key 行
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace():
            end = i
            break
    return "\n".join(lines[start:end])


class TestInstallScriptBasics:
    """deploy/install-dsh-sdk.sh：脚本存在性 / 默认 venv / 版本锁定。"""

    def test_install_script_exists_and_executable(self):
        """一键脚本存在且带执行权限（可直接 ./ 或 bash 执行）。"""
        assert INSTALL_SH.is_file(), "缺少 deploy/install-dsh-sdk.sh"
        assert INSTALL_SH.stat().st_mode & 0o111, "脚本无执行权限"

    def test_script_pins_sdk_full_version(self):
        """SDK 以显式全版本号声明（rc 预发布版缩写装不到）。"""
        script = _read(INSTALL_SH)
        assert SDK_PIN in script

    def test_script_defaults_to_backend_venv(self):
        """默认装进 backend/.venv（pm2 后端运行时 venv）。"""
        script = _read(INSTALL_SH)
        assert "backend/.venv" in script


class TestInstallScriptMirror:
    """镜像源：阿里镜像默认 + DSH_INDEX_URL 可覆盖。"""

    def test_script_uses_aliyun_mirror(self):
        """默认走阿里镜像（清华源未同步 rc 预发布版）。"""
        script = _read(INSTALL_SH)
        assert "mirrors.aliyun.com" in script

    def test_script_mirror_overridable_via_env(self):
        """镜像源可经 DSH_INDEX_URL 环境变量覆盖（内网代理场景）。"""
        script = _read(INSTALL_SH)
        assert re.search(r"\$\{DSH_INDEX_URL", script)


class TestInstallScriptRobustness:
    """幂等 / fail fast / uv 优先（CI venv 无 pip seed）。"""

    def test_script_idempotent(self):
        """已装目标版本直接跳过（重复部署/重跑不重复安装）。"""
        script = _read(INSTALL_SH)
        assert "跳过" in script
        # 版本检测走 importlib.metadata，与 pip show 无关（uv 创建的
        # venv 无 pip 也可检测）
        assert "importlib.metadata" in script

    def test_script_import_check_fail_fast(self):
        """安装后 import 校验：装不上立即 exit 1（部署失败而非带病运行）。"""
        script = _read(INSTALL_SH)
        assert "from deepseek_harness import DeepSeekHarness" in script

    def test_script_prefers_uv_pip(self):
        """优先 uv pip（CI venv 由 uv 创建、无 pip seed，.venv/bin/pip 不存在）。"""
        script = _read(INSTALL_SH)
        assert "uv pip install" in script
        # 显式 --python 指向目标 venv，避免装进其他 venv
        assert "--python" in script


class TestCiPm2DeployAutoInstallsSdk:
    """CI deploy_to_code01（pm2 部署）：主依赖安装后自动调用脚本。"""

    def test_deploy_job_calls_install_script(self):
        job = _ci_deploy_job_section()
        assert "install-dsh-sdk.sh" in job

    def test_deploy_job_installs_sdk_into_backend_venv(self):
        """脚本参数指向 backend/.venv（与 pm2 启动的 venv 一致）。"""
        job = _ci_deploy_job_section()
        assert re.search(r"install-dsh-sdk\.sh\s+backend/\.venv", job)

    def test_deploy_job_script_failure_blocks_deploy(self):
        """脚本失败必须 exit 1（部署失败），不允许静默跳过。"""
        job = _ci_deploy_job_section()
        assert "exit 1" in job

    def test_ci_variables_declare_dsh_index_url(self):
        """全局变量声明 DSH_INDEX_URL（UI 可覆盖，默认阿里镜像）。"""
        ci = _read(CI_FILE)
        assert re.search(
            r"DSH_INDEX_URL:\s*[\"']?https://mirrors\.aliyun\.com/pypi/simple",
            ci,
        )


class TestPm2DeployDocsSynced:
    """文档 / README / 安装指引同步：pm2 自动安装 + 手动部署一键脚本。"""

    def test_dsh_deployment_doc_pm2_auto_install(self):
        """部署文档：pm2 CI 部署自动安装（不再要求手动 pip）。"""
        doc = _read(DSH_DOC)
        assert "自动" in doc
        assert "install-dsh-sdk.sh" in doc

    def test_dsh_runner_hint_points_to_script(self):
        """dsh_runner 安装指引指向一键脚本（而非裸 pip 命令）。"""
        runner = _read(DSH_RUNNER)
        assert "install-dsh-sdk.sh" in runner

    def test_readme_manual_deploy_mentions_script(self):
        """README 手动部署步骤含脚本（手动 pm2/systemd 部署补装 SDK）。"""
        readme = _read(README)
        assert "install-dsh-sdk.sh" in readme
