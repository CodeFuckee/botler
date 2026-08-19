"""mypy 类型检查门禁测试（issue #213）。

验收标准对应：
- mypy 对核心模块零错误（gitlab_client / scheduler / database）；
- CI mypy job 存在（backend:mypy，阻断门禁）；
- 关键行类型化（TypedDict：tasks → TaskRow、repos → RepoRow），
  dict key 拼错（如 commit_shaa）在静态检查下即被拦截；
- 类型化改造不改变运行时行为（返回对象仍为 sqlite3.Row，按列名取值）。

环境依赖：mypy 已加入 requirements.txt 锁文件（CI 与本地 venv 均有），
未安装时本文件整体跳过（pytest.importorskip）。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mypy")

BACKEND_DIR = Path(__file__).resolve().parent.parent
MYPY_INI = BACKEND_DIR / "mypy.ini"
CI_YML = BACKEND_DIR.parent / ".gitlab-ci.yml"
REQUIREMENTS = BACKEND_DIR / "requirements.txt"

CORE_MODULES = (
    "botler/gitlab_client.py",
    "botler/scheduler.py",
    "botler/database.py",
)


def _run_mypy(*args: str, cwd: Path = BACKEND_DIR, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """在 backend/ 下运行 venv 自带的 mypy（与 CI backend:mypy job 同命令）。"""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    mypy_bin = Path(sys.executable).parent / "mypy"
    return subprocess.run(
        [str(mypy_bin), *args],
        capture_output=True, text=True, cwd=cwd, env=env)


def test_mypy_zero_errors_on_core_modules():
    """验收标准 1：mypy 对核心模块零错误（无参数运行走 mypy.ini files）。"""
    r = _run_mypy()
    assert r.returncode == 0, f"mypy 未通过：\n{r.stdout}\n{r.stderr}"
    assert "Success: no issues found" in r.stdout


def test_mypy_ini_targets_core_modules():
    """mypy.ini 存在且扫描范围覆盖三个核心模块。"""
    assert MYPY_INI.is_file(), "mypy.ini 缺失"
    content = MYPY_INI.read_text(encoding="utf-8")
    for mod in CORE_MODULES:
        assert mod in content, f"mypy.ini 未覆盖 {mod}"
    # 渐进式启用语义：未注解函数体也要检查（捕获 None 未判 / key 拼错）
    assert "check_untyped_defs = True" in content


def test_mypy_catches_typo_in_taskrow_key(tmp_path):
    """TypedDict 类型化收益验证：TaskRow 上拼错 key 必须被 mypy 拦截。

    反向证明：若门禁形同虚设（key 拼错不报错），本测试失败。
    """
    probe = tmp_path / "probe_typo.py"
    probe.write_text(
        "from botler.database import TaskRow\n"
        "def f(row: TaskRow) -> None:\n"
        "    x = row['commit_shaa']\n", encoding="utf-8")
    r = _run_mypy(
        "--config-file", str(MYPY_INI),
        str(probe), "botler/database.py",
        env_extra={"MYPYPATH": str(BACKEND_DIR)})
    assert r.returncode != 0, "拼错的 key 未被 mypy 拦截，门禁失效"
    assert "commit_shaa" in r.stdout
    assert "TaskRow" in r.stdout


def test_mypy_passes_correct_taskrow_key(tmp_path):
    """对照用例：TaskRow 正确 key（commit_sha）不产生错误。"""
    probe = tmp_path / "probe_ok.py"
    probe.write_text(
        "from botler.database import TaskRow\n"
        "def f(row: TaskRow) -> None:\n"
        "    x = row['commit_sha']\n", encoding="utf-8")
    r = _run_mypy(
        "--config-file", str(MYPY_INI),
        str(probe), "botler/database.py",
        env_extra={"MYPYPATH": str(BACKEND_DIR)})
    assert r.returncode == 0, f"正确 key 反而报错：\n{r.stdout}"


def test_ci_has_backend_mypy_job():
    """验收标准 2：CI mypy job 存在（backend:mypy，build 阶段，阻断）。"""
    assert CI_YML.is_file(), ".gitlab-ci.yml 缺失"
    content = CI_YML.read_text(encoding="utf-8")
    assert "backend:mypy:" in content, "CI 缺少 backend:mypy job"
    assert "allow_failure: false" in content.split("backend:mypy:", 1)[1].split("script:", 1)[0], \
        "backend:mypy job 未声明阻断语义（allow_failure: false）"
    assert "mypy.ini" in content, "CI job 未引用 mypy.ini（应运行 .venv-mypy/bin/mypy 走配置）"


def test_requirements_includes_mypy():
    """mypy 已加入依赖声明（CI 按 requirements.lock.txt 安装）。"""
    assert REQUIREMENTS.is_file()
    content = REQUIREMENTS.read_text(encoding="utf-8")
    assert "mypy" in content


def test_get_task_row_runtime_shape(tmp_path):
    """类型化不改变运行时行为：get_task 返回行含 tasks 全列（含 commit_sha）。"""
    from botler.database import Database
    db = Database(str(tmp_path / "typed.db"))
    repo_id = db.upsert_repo(1, "demo", "https://example.com/demo.git")
    task_id = db.create_task(repo_id, 1, 213, "测试任务")
    row = db.get_task(task_id)
    assert row is not None
    for key in ("id", "repo_id", "project_id", "issue_iid", "issue_title",
                "status", "commit_sha", "issue_labels", "manual_priority", "created_at"):
        assert key in row.keys(), f"get_task 行缺少列 {key}"
    assert row["id"] == task_id
    assert row["repo_id"] == repo_id
    assert row["issue_iid"] == 213
    assert row["status"] == "queued"


def test_get_repo_row_runtime_shape(tmp_path):
    """get_repo 返回行含 repos 全列（含 priority/logo 等）。"""
    from botler.database import Database
    db = Database(str(tmp_path / "typed.db"))
    repo_id = db.upsert_repo(7, "botler", "https://example.com/botler.git")
    row = db.get_repo(repo_id)
    assert row is not None
    for key in ("id", "gitlab_project_id", "name", "url", "local_path",
                "priority", "enabled", "deleted_at", "logo_path"):
        assert key in row.keys(), f"get_repo 行缺少列 {key}"
    assert row["gitlab_project_id"] == 7
    assert row["name"] == "botler"


def test_list_tasks_returns_typed_rows(tmp_path):
    """list_tasks / find_active_task 返回行同样保持按列名取值行为。"""
    from botler.database import Database
    db = Database(str(tmp_path / "typed.db"))
    repo_id = db.upsert_repo(1, "demo", "https://example.com/demo.git")
    task_id = db.create_task(repo_id, 1, 1, "任务一")
    tasks = db.list_tasks(status="queued")
    assert len(tasks) == 1
    assert tasks[0]["id"] == task_id
    active = db.find_active_task(1, 1)
    assert active is not None and active["id"] == task_id
    latest = db.find_latest_task(1, 1)
    assert latest is not None and latest["id"] == task_id
