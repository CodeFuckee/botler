"""git 工作区管理（issue #192 拆分）。

从原 executor.py 拆出的工作区职责：仓库目录解析、git 命令执行、
prepare（fetch / 切默认主分支 / reset / clean / pull --rebase）、
默认分支解析（ls-remote → remote show → 本地跟踪引用三级降级）、
拉取冲突检测与 agent 手工解决交接、untracked 残留尽力清理。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .common import ExecutorError, _row_get, logger
from .prompt import _strip_credential_sections

# git clean 权限失败的中英文输出标志（issue #91 容错；中文 locale 下 git
# 报「无法删除 …: 权限不够」而非英文 "Permission denied"，单匹配英文会让
# 容错逻辑失效、权限残留直接拖垮任务——本机中文 git 复现）
_CLEAN_PERMISSION_DENIED_RE = re.compile(
    r"permission denied|权限不够", re.IGNORECASE)

def _on_rmtree_error(func, path, exc_info) -> None:
    """rmtree 删除条目失败时的恢复处理（issue #91）。

    先尝试恢复该条目及其父目录的权限后重试一次：残留属主为本进程
    用户时（如目录被 chmod 只读）可借此删除干净；属主是其他用户
    （root 残留）时 chmod 同样失败，放弃该条目由 rmtree 继续处理
    其余条目，最终残留项由 _force_remove 报告并降级警告。
    """
    for target in (path, str(Path(path).parent)):
        try:
            os.chmod(target, 0o700)
        except OSError:
            continue
    try:
        func(path)
    except OSError:
        pass


def _split_https_remote_url(url: str | None) -> dict | None:
    """拆分 https remote URL 为 {scheme, userinfo, host, path}。

    userinfo 为 scheme:// 与最后一个 @ 之间的原始段（可能为空）；
    path 为 host 之后第一个 / 起的部分（含 .git 后缀，未做 URL 解码）。
    scp-like / ssh 形态（无 ://）、非 http(s) scheme、解析失败返回 None
    （调用方跳过规范化）。独立为纯函数便于单元测试。
    """
    value = (url or "").strip()
    if "://" not in value:
        return None
    scheme, rest = value.split("://", 1)
    if scheme not in ("http", "https"):
        return None
    userinfo = ""
    if "@" in rest:
        userinfo, rest = rest.rsplit("@", 1)
    if "/" not in rest:
        return None
    host, path = rest.split("/", 1)
    if not host or not path:
        return None
    return {"scheme": scheme, "userinfo": userinfo, "host": host, "path": path}


def parse_symref_branches(stdout: str) -> tuple[str | None, set[str]]:
    """解析 ``git ls-remote --symref`` 输出 → (HEAD 候选分支, 存在的分支集合)。

    本地与远程工作区准备共用（远程经 SSH 拿到同样的 stdout 文本）。
    HEAD 符号引用行：``ref: refs/heads/main\tHEAD``；分支行：
    ``<sha>\trefs/heads/main``。

    解析主键：分支行的 ref 名称在**第二列**（第一列是提交 sha），
    HEAD 符号引用行才是 ``ref:`` 开头——此前误判第一列导致 heads 集合
    恒为空、服务端权威解析失效（本机中文 locale 下 `git remote show` 又
    输出 ``HEAD 分支：`` 而非英文 ``HEAD branch:``，三级降级一路滑落到
    本地跟踪引用，单分支克隆只剩 dev → 工作区被错误停在 dev 分支；
    测试 test_single_branch_clone_switches_to_default_branch 复现）。
    """
    candidate: str | None = None
    heads: set[str] = set()
    for line in (stdout or "").splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "ref:" and len(parts) == 3 and parts[2] == "HEAD":
            if parts[1].startswith("refs/heads/"):
                candidate = parts[1].rsplit("/", 1)[-1]
        elif len(parts) >= 2 and parts[1].startswith("refs/heads/"):
            heads.add(parts[1].rsplit("/", 1)[-1])
    return candidate, heads


# 「git pull 失败输出是否为合并冲突」的文本标志（本地与远程共用；
# 本地另有文件系统级探测，远程只能依赖文本判定）
PULL_CONFLICT_TEXT_MARKERS = (
    "conflict", "automatic merge failed", "fix conflicts",
    "could not apply", "untracked working tree files "
    "would be overwritten", "divergent branches",
    "have diverged", "unmerged files",
)


class WorkspaceMixin:
    """git 工作区准备与管理（依赖 ClaudeExecutor 实例状态）。"""

    def _repo_workdir(self, repo: dict) -> Path:
        """仓库工作区：远程项目用 remote_path（SSH 执行的路径标识）、
        有 local_path（本地文件夹方式添加）时直接用该文件夹。"""
        if _row_get(repo, "remote_host") and _row_get(repo, "remote_path"):
            # 远程项目：Path 仅作路径标识（键一致性/冲突跟踪），不表示
            # 本机存在该目录；远端命令构造时转 str 使用
            return Path(str(repo["remote_path"]))
        if _row_get(repo, "local_path"):
            return Path(repo["local_path"])
        return self.workspace_root / repo["name"]
    def _remote_cfg_for(self, repo: dict) -> dict:
        """按仓库 remote_host 名取远程服务器配置；缺失抛 ExecutorError。"""
        name = _row_get(repo, "remote_host")
        host = next((r for r in self.config.get().remotes
                     if r.get("name") == name), None)
        if host is None:
            raise ExecutorError(
                f"远程服务器「{name}」不存在（请先在设置页 config remotes 配置）")
        return host

    @staticmethod
    def _remote_path_str(repo: dict) -> str:
        """远程项目远端路径**原串**（不经 Path 规范化）。

        拼进远端命令的路径必须是配置原样（POSIX 绝对路径）；经 Path
        往返会在 Windows 部署机上被规范化成反斜杠（CI Windows runner
        实测），远端 shell 无法识别。
        """
        return str(_row_get(repo, "remote_path") or "")
    def _git(self, workdir: Path, *args: str, env: dict | None = None,
             timeout: int = 300) -> None:
        """执行 git 命令，失败抛 ExecutorError。"""
        cmd = ["git", "-c", "http.sslVerify=false"] + list(args)
        try:
            result = subprocess.run(
                cmd, cwd=workdir, env=env, capture_output=True, text=True,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ExecutorError(f"git 命令超时: {args[0]} {args[1] if len(args) > 1 else ''}")
        if result.returncode != 0:
            raise ExecutorError(
                f"git {args[0]} 失败 (exit {result.returncode}): "
                f"{(result.stderr or result.stdout).strip()[-500:]}")
    def _clean_process_env(self) -> dict:
        """剔除 gitlab-runner CI 环境变量，避免污染 git/claude 子进程。

        CI 部署在构建目录里 pm2 start，作业环境（CI_JOB_TOKEN、GITLAB_CI、
        GIT_CONFIG_* 等）被 pm2 进程继承；git 子进程凭据流程可能经 runner
        注入的 GIT_CONFIG_* 或 credential store 误用 CI_JOB_TOKEN → GitLab
        403（"Authentication by CI/CD job token not allowed..."）→ 403 不
        触发凭据重试 → fetch/push 必失败（issue #18 部署后任务频繁失败根因）。
        """
        return {k: v for k, v in os.environ.items()
                if not (k.startswith("CI_") or k == "GITLAB_CI"
                        or k.startswith("GIT_CONFIG_"))}
    def _git_global_config(self) -> Path:
        """净化版全局 gitconfig 路径：剥离 [credential] section，其余原样保留。

        直接 GIT_CONFIG_GLOBAL=/dev/null 会连带丢失 user.name/email
        （claude 子进程 commit 报错）、http.sslVerify（自签名 GitLab 握手
        失败）等全局设置；这里复制 ~/.gitconfig 并仅剥离 [credential]——
        其中失效的 gitlab-ci-token store 条目会被 git 优先于 GIT_ASKPASS
        选用（store helper 先于 askpass，且 403 不重试），是任务失败的
        直接来源。原文件无 credential 配置时直接复用原路径；无全局配置
        时返回 /dev/null（等价于无全局配置）。
        """
        src = Path.home() / ".gitconfig"
        if not src.is_file():
            return Path(os.devnull)
        text = src.read_text(encoding="utf-8", errors="replace")
        cleaned = _strip_credential_sections(text)
        if cleaned == text:
            return src
        out = self.workspace_root / ".gitconfig-sanitized"
        out.write_text(cleaned, encoding="utf-8")
        return out
    def _askpass_script(self, repo_name: str) -> Path:
        """生成 GIT_ASKPASS 脚本（用户名 oauth2，密码 = bot token）。

        放在工作区父目录（不能在 clone 目标目录里，否则 git clone 拒绝非空目录）。
        """
        script = self.workspace_root / f".botler-askpass-{repo_name}.sh"
        token = self.config.get().gitlab_token
        # token 里可能含引号，用单引号包裹并转义
        esc = token.replace("'", "'\\''")
        script.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*) echo "oauth2" ;;\n'
            '  *Password*) echo \'%s\' ;;\n'
            '  *) echo \'%s\' ;;\n'
            "esac\n" % (esc, esc),
            encoding="utf-8",
        )
        script.chmod(0o700)
        return script
    def _build_git_env(self, repo: dict) -> dict:
        """构建 git 子进程凭据环境（issue #238：预检与 prepare_workspace 共用）。

        先剔除 CI 环境变量再设置关键项：gitlab-runner 的 CI_JOB_TOKEN 等
        会被 git 凭据流程误用（经 store 优先于 GIT_ASKPASS → 403 不重试），
        且外部 GIT_ASKPASS 可能指向别处覆盖凭据注入；GIT_ASKPASS 指向 bot
        token 脚本（prepare_workspace 的 clone/fetch/push 与预检的 ls-remote
        探测使用同一凭据来源，保证预检结论与真实执行一致）。
        """
        askpass = self._askpass_script(repo["name"])
        git_env = self._clean_process_env()
        git_env["GIT_ASKPASS"] = str(askpass)
        git_env["GIT_TERMINAL_PROMPT"] = "0"
        git_env["HOME"] = str(Path.home())
        # 禁用全局 credential store（失效 job token 条目优先于 askpass 被选用）
        git_env["GIT_CONFIG_GLOBAL"] = str(self._git_global_config())
        git_env["GIT_CONFIG_SYSTEM"] = os.devnull
        return git_env

    def _normalize_origin_url(self, workdir: Path, repo: dict, remote: str,
                              git_env: dict) -> None:
        """工作区 origin 仓库路径与配置 url 仅大小写不一致时规范化为配置路径。

        issue #416（任务 #605 根因）：local_path 工作区由用户手工 clone /
        历史遗留，origin URL 的仓库路径与平台配置 url 大小写不一致
        （chenkaidi/Graph2plan vs chenkaidi/graph2plan），而注入 agent 提示
        词的仓库锁定自检是严格字符串比较（模板注入配置路径）→ agent 误判
        「仓库不一致」终止任务，重试耗尽 failed。GitLab 项目路径本身大小写
        不敏感，这里把 origin 的 path 对齐配置 url 的 path（保留 origin 原有
        凭据与 host），保证自检通过。

        防护：仅处理「路径大小写不敏感相等」的差异——路径真实不同（不同
        仓库）、host 不同、ssh/scp-like 形态一律不修改，留给 agent 自检按
        原规则拦截；读取 origin 失败（git 异常等）静默跳过不阻塞任务。
        """
        try:
            result = subprocess.run(
                ["git", "-C", str(workdir), "config", "--get",
                 f"remote.{remote}.url"],
                capture_output=True, text=True, timeout=30)
        except Exception:  # git 缺失/异常时跳过，agent 自检兜底
            logger.debug("读取 origin URL 失败，跳过 origin 路径规范化",
                         exc_info=True)
            return
        if result.returncode != 0:
            return
        origin = _split_https_remote_url(result.stdout.strip())
        config_url = _split_https_remote_url(_row_get(repo, "url") or "")
        if not origin or not config_url:
            return  # ssh/scp-like 或解析失败：不修改
        if origin["host"] != config_url["host"]:
            return  # host 不同：不越权修改
        origin_path = origin["path"]
        if origin_path.endswith(".git"):
            origin_path = origin_path[:-4]
        config_path = config_url["path"]
        if config_path.endswith(".git"):
            config_path = config_path[:-4]
        if origin_path == config_path:
            return  # 已一致
        if origin_path.casefold() != config_path.casefold():
            return  # 真实不同仓库：不修改，agent 自检按原规则拦截
        userinfo = origin["userinfo"]
        prefix = f"{origin['scheme']}://"
        if userinfo:
            prefix += f"{userinfo}@"
        new_url = f"{prefix}{origin['host']}/{config_url['path']}"
        logger.warning(
            "%s: origin 仓库路径大小写与配置不一致（%s vs %s），"
            "规范化 origin 为配置路径",
            workdir, origin["path"], config_url["path"])
        self._git(workdir, "remote", "set-url", remote, new_url, env=git_env)

    def prepare_workspace(self, repo: dict, resume: bool = False) -> tuple[Path, dict]:
        """确保工作区存在且干净，返回 (workdir, git_env)。

        远程项目（remote_host）经 SSH 在远程主机上做工作区准备
        （_prepare_workspace_remote，git_env 为远端 env 键值）；local_path
        仓库直接用该文件夹（不 clone）；普通仓库首次执行时 clone。
        resume=True（会话断点续跑）时只 fetch 更新远端引用，跳过
        checkout / reset --hard / clean -fd——保留 Claude 上次的未提交改动
        与本地提交，供恢复会话接续使用。
        """
        if _row_get(repo, "remote_host"):
            return self._prepare_workspace_remote(repo, resume=resume)
        workdir = self._repo_workdir(repo)
        git_env = self._build_git_env(repo)

        if not (workdir / ".git").exists():
            if _row_get(repo, "local_path"):
                raise ExecutorError(
                    f"本地文件夹不是 git 仓库: {workdir}（local_path 方式要求存在 .git 目录）")
            logger.info("首次克隆仓库 %s", repo["name"])
            # 不要预先创建 workdir：git clone 要求目标目录不存在（或为空）
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "-c", "http.sslVerify=false", "clone", repo["url"], str(workdir)]
            try:
                result = subprocess.run(cmd, env=git_env, capture_output=True,
                                        text=True, timeout=600)
            except subprocess.TimeoutExpired:
                raise ExecutorError(f"克隆仓库 {repo['name']} 超时")
            if result.returncode != 0:
                raise ExecutorError(
                    f"克隆仓库 {repo['name']} 失败: {(result.stderr or result.stdout).strip()[-500:]}")

        # 每次执行前重置到远端默认主分支，从根上消除脏状态。
        # issue #147：模版库前两点「任务开始先校验当前分支切回默认主分支 +
        # 每次开发前 git pull 同步」下沉为平台代码自动完成，agent 无需再
        # 自行执行（节省 token）。
        # remote_name 记录本地方式添加时用户选中的 remote（老数据缺省为 origin）
        remote = _row_get(repo, "remote_name") or "origin"
        self._normalize_origin_url(workdir, repo, remote, git_env)
        self._git(workdir, "fetch", remote, "--prune", env=git_env)
        if not resume:
            # 1) 解析远端默认主分支名：优先 ls-remote --symref 的服务端权威
            #    HEAD 符号引用，不依赖本地 {remote}/HEAD——手工加 remote 的
            #    仓库可能缺失该引用（issue #12）
            branch = self._resolve_default_branch(workdir, remote, git_env)
            # 2) 校验当前分支，非默认主分支 → checkout 切回主分支
            self._checkout_default_branch(workdir, remote, branch, git_env)
            self._git(workdir, "reset", "--hard", f"{remote}/{branch}", env=git_env)
            self._clean_untracked(workdir, git_env)
            # 3) git pull --rebase 显式同步远端默认主分支最新提交（兜底
            #    fetch 之后、本次执行前远端新推送的提交）。若拉取遇到合并
            #    冲突（本地提交与远端分叉、untracked 残留被远端新提交占用
            #    等），不直接失败：保留冲突现场交由 agent 手工合并
            #    （issue #147 补充需求「如果拉取代码的时候出现了冲突，
            #    让 agent 来进行合并」）。
            try:
                self._git(workdir, "pull", "--rebase", remote, branch, env=git_env)
            except ExecutorError as exc:
                if not self._is_pull_conflict(workdir, git_env, exc):
                    raise
                self._pull_conflict_workdirs.add(workdir)
                logger.warning(
                    "%s: git pull --rebase 出现合并冲突，保留冲突现场交由 "
                    "agent 手工合并: %s", repo["name"], str(exc)[:200])
            else:
                self._pull_conflict_workdirs.discard(workdir)
                logger.info("%s: 工作区已切到默认主分支 %s 并 git pull 同步最新",
                            repo["name"], branch)
        # askpass 脚本保留不删除（issue #12）：并发任务/重试时序下脚本被删 →
        # fetch 回退 credential helper 旧凭据 → HTTP Basic: Access denied。
        # 脚本内容每次 prepare 覆盖刷新（token 轮换自动生效），权限 0700，
        # 且在工作区父目录，不受 clean -fd 波及。
        return workdir, git_env

    # ---- 远程项目工作区准备（SSH，远程项目专用）----

    def _remote_askpass(self, host: dict, repo_name: str) -> str:
        """把 GIT_ASKPASS 脚本写到远程主机 ~/.botler/ 下，返回远端路径。

        内容与本地 _askpass_script 一致（Username=oauth2，
        Password=bot token）；经 heredoc（引号定界符）传输，token 不会被
        远端 shell 二次展开。每次 prepare 覆盖刷新（token 轮换自动生效）。
        """
        from ..remote_exec import run_remote

        safe = "".join(c if c.isalnum() or c in "._-" else "_"
                       for c in repo_name) or "repo"
        remote_path = f"~/.botler/askpass-{safe}.sh"
        token = self.config.get().gitlab_token
        esc = token.replace("'", "'\\''")
        script = ("#!/bin/sh\ncase \"$1\" in\n"
                  "  *Username*) echo \"oauth2\" ;;\n"
                  f"  *Password*) echo '{esc}' ;;\n"
                  f"  *) echo '{esc}' ;;\n"
                  "esac\n")
        cmd = ("mkdir -p ~/.botler && cat > "
               f"{remote_path} <<'BOTLER_ASKPASS_EOF'\n"
               f"{script}BOTLER_ASKPASS_EOF\nchmod 700 {remote_path}")
        cp = run_remote(host, cmd, timeout=30)
        if cp.returncode != 0:
            raise ExecutorError(
                "写入远程 GIT_ASKPASS 脚本失败: "
                f"{(cp.stderr or cp.stdout).strip()[-300:]}")
        return remote_path

    def _run_remote_checked(self, host: dict, command: str, what: str,
                            timeout: int = 600):
        """执行远端命令（run_remote 包装）：超时/SSH 故障/非零退出统一抛
        ExecutorError，成功返回 CompletedProcess（stderr/stdout 可读）。"""
        from ..remote_exec import run_remote

        try:
            cp = run_remote(host, command, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ExecutorError(f"远程 git {what} 超时（>{timeout}s）")
        except OSError as e:
            raise ExecutorError(f"SSH 连接失败（远程 git {what}）: {e}")
        if cp.returncode != 0:
            raise ExecutorError(
                f"远程 git {what} 失败 (exit {cp.returncode}): "
                f"{(cp.stderr or cp.stdout).strip()[-500:]}")
        return cp

    def _prepare_workspace_remote(self, repo: dict,
                                  resume: bool = False) -> tuple[Path, dict]:
        """远程项目工作区准备：经 SSH 在远程主机执行等效 git 序列。

        序列（与本地 prepare 语义对齐，MVP 简化为服务端权威探测 +
        显式补齐跟踪引用）：
        1. 校验远端目录为 git 仓库（.git 存在）；
        2. 写 GIT_ASKPASS 脚本到远端（bot token 凭据）；
        3. fetch --prune；
        4. 非续跑：ls-remote --symref 解析远端默认分支（拿不到回退 main）→
           显式拉取该分支跟踪引用（issue #148 等价）→ checkout -B →
           reset --hard → clean -fd → pull --rebase（冲突保留现场交 agent，
           文本标志判定，PULL_CONFLICT_TEXT_MARKERS）。
        返回 (workdir, git_env)：workdir 为 Path(remote_path)（远端路径
        标识），git_env 为远端 env 键值（GIT_ASKPASS/GIT_TERMINAL_PROMPT），
        供引擎执行时拼进远端命令环境。
        """
        from ..remote_exec import run_remote, sh_quote

        host = self._remote_cfg_for(repo)
        workdir = self._repo_workdir(repo)
        path = self._remote_path_str(repo)
        remote = _row_get(repo, "remote_name") or "origin"

        # 1. 远端目录必须是 git 仓库（与 local_path 校验语义一致）
        cp = run_remote(host, f"test -d {sh_quote(path)}/.git", timeout=20)
        if cp.returncode != 0:
            raise ExecutorError(
                f"远程目录不是 git 仓库（或不可访问）: {host['name']}:{path}"
                "（remote_path 方式要求远端存在 .git 目录）")

        # 2. 远端 askpass 凭据脚本
        askpass = self._remote_askpass(host, repo["name"])
        git_env = {"GIT_ASKPASS": askpass, "GIT_TERMINAL_PROMPT": "0"}

        def git_cmd(*args: str) -> str:
            return ("env "
                    f"GIT_ASKPASS={sh_quote(askpass)} GIT_TERMINAL_PROMPT=0 "
                    f"git -C {sh_quote(path)} -c http.sslVerify=false "
                    + " ".join(args))

        # 3. fetch
        self._run_remote_checked(
            host, git_cmd("fetch", sh_quote(remote), "--prune"), "fetch")

        if not resume:
            # 4a. 远端服务端权威解析默认分支（ls-remote --symref），
            #     拿不到回退 main（本地流程的完整三级降级在远程 MVP 简化）
            cp = self._run_remote_checked(
                host, git_cmd("ls-remote", "--symref", sh_quote(remote)),
                "ls-remote")
            candidate, heads = parse_symref_branches(cp.stdout)
            if candidate and candidate in heads:
                branch = candidate
            elif "main" in heads:
                branch = "main"
            elif "master" in heads:
                branch = "master"
            elif heads:
                branch = sorted(heads)[0]
            else:
                branch = "main"
            # 4b. 显式补齐跟踪引用（单分支克隆/受限 refspec，issue #148 等价）
            self._run_remote_checked(
                host,
                git_cmd("fetch", sh_quote(remote),
                        sh_quote(f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")),
                "fetch branch")
            self._run_remote_checked(
                host,
                git_cmd("checkout", "-B", sh_quote(branch),
                        sh_quote(f"{remote}/{branch}")),
                "checkout")
            self._run_remote_checked(
                host,
                git_cmd("reset", "--hard", sh_quote(f"{remote}/{branch}")),
                "reset")
            self._run_remote_checked(host, git_cmd("clean", "-fd"), "clean")
            # 4c. pull --rebase 兜底同步；冲突保留现场（文本标志判定）
            cp = run_remote(host, git_cmd("pull", "--rebase",
                                          sh_quote(remote), sh_quote(branch)),
                            timeout=300)
            if cp.returncode != 0:
                text = (cp.stderr or cp.stdout or "").lower()
                if any(m in text for m in PULL_CONFLICT_TEXT_MARKERS):
                    self._pull_conflict_workdirs.add(workdir)
                    logger.warning(
                        "%s@%s: 远程 git pull --rebase 出现合并冲突，保留冲突"
                        "现场交由 agent 手工合并", host["name"], path)
                else:
                    raise ExecutorError(
                        f"远程 git pull 失败: "
                        f"{(cp.stderr or cp.stdout).strip()[-500:]}")
            else:
                self._pull_conflict_workdirs.discard(workdir)
                logger.info("%s@%s: 远程工作区已切到默认主分支 %s 并同步最新",
                            host["name"], path, branch)
        return workdir, git_env

    def _resolve_default_branch(self, workdir: Path, remote: str,
                                git_env: dict) -> str:
        """解析远端默认主分支名（issue #147 / #148 强化，不再硬编码 main）。

        优先读取服务端权威信息：``git ls-remote --symref`` 返回的 HEAD
        符号引用（并校验该分支真实存在于远端 refs，``git init --bare`` 的
        裸仓库 HEAD 可能指向不存在的 master，只有 main 被推送）。ls-remote
        探测失败（网络/认证抖动、超时、git 异常等）时不直接回退硬编码
        main，而是逐级降级：

          1. ``git ls-remote --symref <remote>``（服务端权威）；
          2. ``git remote show <remote>`` 解析 "HEAD branch:" 行；
          3. 本地跟踪引用兜底：优先 ``refs/remotes/<remote>/HEAD`` 符号
             引用，再按 main → master → 字典序 在本地已存在的跟踪分支中
             探测（远端彻底不可达时也能拿到实际存在的分支）。

        任一环节拿到的分支名都必须与「远端/本地实际存在的分支集合」核对，
        避免解析出不存在的分支导致 checkout / pull 失败（任务 #249 根因：
        远端只有 master 时解析出 main → fetch/checkout main 必然失败）。
        全链路均失败（远端不可达且本地无任何跟踪引用）才最终回退 "main"。
        """
        branch = self._remote_default_branch_via_lsremote(
            workdir, remote, git_env)
        if branch:
            return branch
        branch = self._remote_default_branch_via_show(workdir, remote, git_env)
        if branch:
            return branch
        branch = self._local_default_branch(workdir, remote)
        if branch:
            return branch
        return "main"
    def _remote_default_branch_via_lsremote(self, workdir: Path, remote: str,
                                            git_env: dict) -> str | None:
        """git ls-remote --symref 解析服务端权威默认主分支，失败返回 None。

        HEAD 符号引用指向的分支不存在（如新裸仓库）时按 main → master →
        字典序 在远端真实存在的分支中回退；远端可达但没有任何分支返回
        None（空仓库交由下一级兜底）。
        """
        cmd = ["git", "-c", "http.sslVerify=false", "ls-remote", "--symref", remote]
        try:
            result = subprocess.run(cmd, cwd=workdir, env=git_env,
                                    capture_output=True, text=True, timeout=120)
        except Exception:  # git 缺失/超时/异常等一律走下一级降级，探测不阻塞任务
            logger.debug("ls-remote --symref 解析默认主分支失败，走 git remote show 降级",
                         exc_info=True)
            return None
        if result.returncode != 0:
            logger.debug("ls-remote --symref 返回非零（%s），走 git remote show 降级: %s",
                         result.returncode, (result.stderr or result.stdout).strip()[-200:])
            return None
        candidate, heads = parse_symref_branches(result.stdout)
        if candidate and candidate in heads:
            return candidate
        # HEAD 符号引用指向的分支不存在（如新裸仓库）→ 按常见命名回退
        for name in ("main", "master"):
            if name in heads:
                return name
        if heads:
            return sorted(heads)[0]
        return None
    def _remote_default_branch_via_show(self, workdir: Path, remote: str,
                                        git_env: dict) -> str | None:
        """git remote show <remote> 解析 "HEAD branch:" 行，失败返回 None。

        ls-remote --symref 不可用（服务器不支持 / 探测异常）时的二次服务端
        探测。``git remote show`` 是 git 查询远端默认分支的标准命令，输出
        ``HEAD branch: <名>``（中文 locale 下为 ``HEAD 分支：<名>``，
        两种前缀都识别）；HEAD 悬空时输出 ``(unknown)``。
        """
        cmd = ["git", "remote", "show", remote]
        try:
            result = subprocess.run(cmd, cwd=workdir, env=git_env,
                                    capture_output=True, text=True, timeout=120)
        except Exception:  # 同上：任何失败走本地跟踪引用兜底
            logger.debug("git remote show 解析默认主分支失败，走本地跟踪引用兜底",
                         exc_info=True)
            return None
        if result.returncode != 0:
            logger.debug("git remote show 返回非零（%s），走本地跟踪引用兜底",
                         result.returncode)
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            for prefix in ("HEAD branch:", "HEAD 分支："):
                if stripped.startswith(prefix):
                    name = stripped[len(prefix):].strip()
                    if name and name != "(unknown)":
                        return name
        return None
    def _local_default_branch(self, workdir: Path, remote: str) -> str | None:
        """远端不可达时用本地跟踪引用兜底解析默认主分支，没有则返回 None。

        先收集本地已拉取的 ``refs/remotes/<remote>/*`` 跟踪分支集合，再
        按优先级探测：① ``refs/remotes/<remote>/HEAD`` 符号引用（clone /
        git remote set-head 生成，目标分支必须真实存在于本地，避免陈旧
        HEAD 指向已删除分支）；② main → master → 字典序 取实际存在的分支。
        注意：单分支克隆的 origin/HEAD 指向克隆分支而非远端默认分支——
        远端不可达时无法确认真实默认分支，取本地已有分支已是最优近似
        （远端恢复后 ls-remote 会纠正）。
        """
        try:
            result = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname:short)",
                 f"refs/remotes/{remote}/"],
                cwd=workdir, capture_output=True, text=True, timeout=30)
        except Exception:  # git 缺失/超时等一律视为无本地引用
            return None
        if result.returncode != 0:
            return None
        names = {line.strip().rsplit("/", 1)[-1]
                 for line in result.stdout.splitlines() if line.strip()}
        names.discard("HEAD")  # 排除符号引用本身（refs/remotes/<remote>/HEAD）
        try:
            result = subprocess.run(
                ["git", "symbolic-ref", f"refs/remotes/{remote}/HEAD"],
                cwd=workdir, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                ref = result.stdout.strip()
                prefix = f"refs/remotes/{remote}/"
                if ref.startswith(prefix):
                    name = ref[len(prefix):]
                    if name in names:  # 陈旧 HEAD 指向已删除分支时忽略
                        return name
        except Exception:  # 同上：任何失败按本地跟踪分支探测
            logger.debug("本地 remote HEAD 符号引用解析失败，按本地跟踪分支探测",
                         exc_info=True)
        for name in ("main", "master"):
            if name in names:
                return name
        if names:
            return sorted(names)[0]
        return None
    def _checkout_default_branch(self, workdir: Path, remote: str,
                                 branch: str, git_env: dict) -> None:
        """校验当前分支：非默认主分支则 checkout 切回主分支（issue #147）。

        已处于默认主分支时直接返回（不重复切换）；detached HEAD（rev-parse
        输出 HEAD）同样视为非默认分支重新检出。``-B`` 保证本地分支不存在时
        基于远端分支创建、已存在时重置到远端提交，随后显式写
        branch.<name>.remote / branch.<name>.merge 建立上游跟踪
        （受限 fetch refspec 下 ``--track`` 无法建立跟踪，见下）。

        issue #148：执行前先补齐远端默认主分支的本地跟踪引用
        （refs/remotes/<remote>/<branch>）。工作区仓库可能是单分支克隆
        （--single-branch）或手工配置了受限 fetch refspec，fetch 只拉取了
        部分分支——此时即便远端确实存在默认主分支，本地也查不到对应跟踪
        引用：checkout -B <branch> --track <remote>/<branch> 会报
        "'origin/main' is not a commit"（任务 #249 失败根因），后续
        reset --hard <remote>/<branch> 同样报 'ambiguous argument'。
        缺失时用显式 refspec 拉取该分支补齐（命令行 refspec 不受受限配置
        影响），再走切回/重置流程。
        """
        if not self._remote_tracking_ref_exists(workdir, remote, branch, git_env):
            logger.warning(
                "%s: 远端默认主分支 %s 的本地跟踪引用 refs/remotes/%s/%s "
                "缺失（单分支克隆或受限 fetch refspec），显式拉取补齐",
                workdir, branch, remote, branch)
            self._git(workdir, "fetch", remote,
                      f"{branch}:refs/remotes/{remote}/{branch}", env=git_env)
        current = ""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=workdir, env=git_env, capture_output=True, text=True,
                timeout=30)
            if result.returncode == 0:
                current = result.stdout.strip()
        except Exception:  # 探测失败视为未知分支，走 checkout 切回
            logger.debug("读取当前分支失败，按非默认分支处理", exc_info=True)
        if current == branch:
            return
        logger.info("工作区当前分支 %s ≠ 默认主分支 %s，切回主分支",
                    current or "（detached HEAD）", branch)
        # 不用 --track 建跟踪：受限 fetch refspec（单分支克隆等）下 git 无法
        # 把 refs/remotes/<remote>/<branch> 映射回远端分支名，--track 会报
        # "cannot set up tracking information; starting point ... is not a
        # branch"（与引用是否已补齐无关）。改为 checkout 后直接写
        # branch.<name>.remote / branch.<name>.merge，标准仓库结果等价。
        self._git(workdir, "checkout", "-B", branch,
                  f"{remote}/{branch}", env=git_env)
        self._git(workdir, "config", f"branch.{branch}.remote", remote,
                  env=git_env)
        self._git(workdir, "config", f"branch.{branch}.merge",
                  f"refs/heads/{branch}", env=git_env)
    def _remote_tracking_ref_exists(self, workdir: Path, remote: str,
                                    branch: str, git_env: dict) -> bool:
        """判断本地远端跟踪引用 refs/remotes/<remote>/<branch> 是否存在。

        返回 False 的情形：引用从未拉取过（单分支克隆/受限 refspec）、
        被 --prune 清除、git 异常等。探测失败一律按缺失处理，由调用方
        显式拉取补齐。
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet",
                 f"refs/remotes/{remote}/{branch}"],
                cwd=workdir, env=git_env, capture_output=True, text=True,
                timeout=30)
        except Exception:  # git 缺失/超时等一律视为引用不存在
            return False
        return result.returncode == 0
    def _is_pull_conflict(self, workdir: Path, git_env: dict,
                          exc: ExecutorError) -> bool:
        """判断 git pull 失败是否为可交由 agent 手工解决的合并冲突。

        issue #147 补充：拉取冲突不应让任务在准备阶段直接失败，而应保留
        冲突现场交由 agent 合并。判断依据按权威性排序：
        1) 工作区实际处于冲突状态——rebase/merge 进行中（.git/rebase-merge、
           .git/rebase-apply、.git/MERGE_HEAD）或存在未合并路径
           （git ls-files -u 非空）；
        2) git 输出包含明确的冲突标志（CONFLICT / could not apply /
           untracked 文件被远端新提交覆盖等）。
        凭据/网络等非冲突失败不在此列，照常抛错。
        """
        git_dir = workdir / ".git"
        # worktree 等场景 .git 可能是文件：先解析实际 git 目录再探测
        try:
            result = subprocess.run(
                ["git", "-C", str(workdir), "rev-parse", "--git-dir"],
                cwd=workdir, env=git_env, capture_output=True, text=True,
                timeout=30)
            if result.returncode == 0:
                git_dir = Path(result.stdout.strip())
                if not git_dir.is_absolute():
                    git_dir = (workdir / git_dir).resolve()
        except Exception:  # 探测失败时退回默认 .git 目录
            logger.debug("解析 git 目录失败，按默认 .git 处理", exc_info=True)
        for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
            if (git_dir / marker).exists():
                return True
        try:
            result = subprocess.run(
                ["git", "-C", str(workdir), "ls-files", "-u"],
                cwd=workdir, env=git_env, capture_output=True, text=True,
                timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return True
        except Exception:  # git 缺失/异常时仅靠错误文本兜底
            logger.debug("git ls-files -u 探测未合并路径失败", exc_info=True)
        text = str(exc).lower()
        if any(m in text for m in PULL_CONFLICT_TEXT_MARKERS):
            return True
        return False

    @staticmethod
    @staticmethod
    def _conflict_handoff_instructions() -> str:
        """拉取冲突交接指引（issue #147 补充）：prepare 的 git pull 遇到
        合并冲突时追加到任务提示词末尾，引导 agent 先手工解决冲突再继续。"""
        return (
            "\n\n【重要：工作区存在拉取冲突，请先手工解决再开始任务】\n"
            "平台在任务开始前的 git pull --rebase 同步最新代码时遇到合并冲突，\n"
            "冲突现场已原样保留（未回退、未丢弃任何内容）。请先完成合并：\n"
            "1. 运行 git status 查看冲突文件与当前 rebase/merge 状态；\n"
            "2. 用 git diff 或编辑器逐个解决冲突文件，保留两侧合理内容；\n"
            "3. 解决后 git add <冲突文件>，rebase 冲突执行 git rebase --continue，\n"
            "   merge 冲突执行 git commit 完成合并；\n"
            "4. 严禁 git push --force / --force-with-lease 强制覆盖远端；\n"
            "5. 若冲突确实无法解决，如实汇报失败原因与冲突文件清单，不要强行提交。"
        )
    def _clean_untracked(self, workdir: Path, git_env: dict) -> None:
        """清理未跟踪文件，容忍无权限删除的外部残留（issue #91）。

        用户以 root 等身份在 local_path 工作区跑过构建（如 flutter build
        生成的 .plugin_symlinks）会留下属主非本进程用户的 untracked 目录，
        git clean -fd 删除其中条目时 Permission denied 而整体失败（issue #91
        诊断的任务 #136 场景：daymark 仓库重试 3 次全败）。
        此类残留不影响 fetch / checkout / reset（只涉及 tracked 文件），
        不应拖垮整个任务：先尝试 Python 层尽力删除，仍删不掉的降级为
        警告继续执行，由用户手动清理。
        """
        try:
            self._git(workdir, "clean", "-fd", env=git_env)
            return
        except ExecutorError as exc:
            if not _CLEAN_PERMISSION_DENIED_RE.search(str(exc)):
                raise
        logger.warning("%s: git clean 权限受限，尝试 Python 层清理残留", workdir)
        for rel in self._untracked_paths(workdir, git_env):
            path = workdir / rel
            if not self._force_remove(path):
                logger.warning("无法删除残留项（可能需要 root 权限手动清理）: %s", path)
        # 复检：残留清干净则无感；仍权限失败则警告放行（不阻塞任务）
        try:
            self._git(workdir, "clean", "-fd", env=git_env)
        except ExecutorError as exc:
            if _CLEAN_PERMISSION_DENIED_RE.search(str(exc)):
                logger.warning("git clean 仍有权限受限残留，跳过继续执行: %s",
                               str(exc)[:200])
            else:
                raise

    @staticmethod
    @staticmethod
    def _untracked_paths(workdir: Path, git_env: dict) -> list[str]:
        """列出当前 untracked 条目（相对路径），供失败后的尽力清理使用。"""
        cmd = ["git", "-c", "http.sslVerify=false",
               "ls-files", "--others", "--exclude-standard"]
        try:
            result = subprocess.run(cmd, cwd=workdir, env=git_env,
                                    capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return []
        if result.returncode != 0:
            return []
        paths = []
        for line in result.stdout.splitlines():
            rel = line.rstrip("/")
            if rel and not rel.startswith("..") and not os.path.isabs(rel):
                paths.append(rel)
        return paths

    @staticmethod
    @staticmethod
    def _force_remove(path: Path) -> bool:
        """尽力删除 untracked 残留（文件/符号链接/目录），返回是否删除成功。

        issue #91：残留目录无写权限（chmod 只读 / 属主非本进程用户）时，
        删除内部条目受父目录写权限约束而 EACCES。先常规删除，失败后尝试
        恢复条目及父目录权限再重试一次；chmod 也失败（root 属主）则放弃。
        """
        for attempt in (False, True):
            try:
                if path.is_symlink() or not path.is_dir():
                    path.unlink(missing_ok=True)
                else:
                    shutil.rmtree(path, onerror=_on_rmtree_error)
                if not path.exists():
                    return True
            except OSError:
                pass
            if attempt:
                return False
            # 恢复权限后重试：条目 chmod 失败 = 非本进程用户属主，不再折腾
            try:
                os.chmod(path, 0o700)
            except OSError:
                return False
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
        return False

    # ---- 提示词与环境 ----
