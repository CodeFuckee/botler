"""生产默认模板回归测试：模板必须是「处理当前指派 issue」的标准 botler 模板。

背景（部署后任务一直失败）：data/backend/config.yaml 的 templates.default
曾被替换为 gitlab-issue-agent 提示词（跨会话领取队列），executor 渲染后
Claude 收到错误指令，不处理当前 issue，且所有 GitLab 操作权限被拒 →
任务必然失败（pm2 日志 task_7/8/9 的 permission_denials）。

本测试锁定 config.example.yaml 模板的关键特征，防止再次被替换/改坏。
"""

from pathlib import Path

import pytest

from botler.config import ConfigManager
from botler.templates import TemplateRenderer


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """config.example.yaml 的凭据用 ${ENV} 引用，测试环境补默认值。"""
    monkeypatch.setenv("GITLAB_BOT_TOKEN", "test-token")
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")


def _example_template() -> str:
    """读取 backend/config.example.yaml 的全局默认模板。"""
    root = Path(__file__).resolve().parents[1]  # backend/
    config = ConfigManager(str(root / "config.example.yaml"))
    return config.get().default_template


class TestDefaultTemplate:
    def test_template_targets_current_issue(self):
        """模板必须面向「当前指派 issue」：含全部必填占位符与处理指令。"""
        t = _example_template()
        for ph in ("{repo_name}", "{issue_title}", "{issue_body}", "{issue_url}"):
            assert ph in t
        assert "AI 维护者" in t

    def test_template_not_issue_agent_prompt(self):
        """不得含 gitlab-issue-agent 提示词特征（跨会话领取队列/强制 /new 等）。"""
        t = _example_template()
        assert "gitlab_issue_agent" not in t
        assert "跨会话循环" not in t
        assert "强制提示用户" not in t
        assert "队列" not in t

    def test_template_curl_close_issue_uses_insecure_flag(self):
        """关闭 issue 的 curl 必须带 -k：自建 GitLab 为自签证书（verify_ssl:
        false），不带 -k 时 curl 报证书错误 → issue 永远关不上、任务无法成功。"""
        assert "curl -s -k -X PUT" in _example_template()
        assert '"state_event=close"' in _example_template()

    def test_template_renders_issue_info(self):
        """渲染后包含 issue 标题与关闭指令（executor 实际发给 Claude 的内容）。"""
        t = _example_template()
        rendered = (t
                    .replace("{repo_name}", "botler")
                    .replace("{issue_title}", "测试 issue")
                    .replace("{issue_body}", "正文")
                    .replace("{issue_url}", "https://gitlab.example.com/x/-/issues/1")
                    .replace("{gitlab_url}", "https://gitlab.example.com")
                    .replace("{project_id}", "123")
                    .replace("{issue_iid}", "1"))
        assert "botler" in rendered
        assert "测试 issue" in rendered
        assert "git push origin main" in rendered
        assert "state_event=close" in rendered


class TestProjectPathPlaceholder:
    """{project_path} 占位符渲染（issue-agent 参数化模板）。

    背景：用户全局模板采用跨会话 issue-agent 模式，但模板写死了
    chenkaidi/shipyard——botler 对任何仓库的任务都收到「处理 shipyard
    队列」指令，不处理当前指派的 issue（任务反复失败的根因之一）。
    修复：新增 {project_path} / {project_path_encoded} 占位符，渲染时从
    仓库 URL 提取（如 chenkaidi/botler），模板不再写死单仓库。
    """

    ISSUE = {"state": "opened", "title": "标题", "description": "正文",
             "web_url": "https://gitlab.example.com/x/-/issues/7",
             "project_id": 42, "iid": 7}

    def _renderer(self, tmp_path) -> TemplateRenderer:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "gitlab:\n  url: https://gitlab.example.com\n  bot_token: t\n"
            "worker: {}\nclaude: {}\ntemplates: {}\nrepos: []\n",
            encoding="utf-8")
        return TemplateRenderer(ConfigManager(str(config_path)))

    def test_project_path_extracted_from_repo_url(self, tmp_path):
        """渲染 {project_path} 时替换为仓库路径（去 scheme/host/.git）。"""
        r = self._renderer(tmp_path)
        tpl = "项目: `{project_path}`；API: {project_path_encoded}"
        v = r.build_variables("botler", self.ISSUE,
                              repo_url="https://home.chenkaidi.top:509/chenkaidi/botler.git")
        rendered = r.render(tpl, v)
        assert "chenkaidi/botler" in rendered
        assert "chenkaidi%2Fbotler" in rendered
        assert "home.chenkaidi.top" not in rendered

    def test_project_path_without_git_suffix(self, tmp_path):
        """URL 无 .git 后缀同样正确提取。"""
        r = self._renderer(tmp_path)
        v = r.build_variables("demo", self.ISSUE,
                              repo_url="https://gitlab.example.com/group/sub/demo")
        assert v["project_path"] == "group/sub/demo"

    def test_project_path_fallback_to_repo_name(self, tmp_path):
        """无 repo_url（如恢复执行）时兜底用仓库名。"""
        r = self._renderer(tmp_path)
        v = r.build_variables("botler", self.ISSUE)
        assert v["project_path"] == "botler"
        assert v["project_path_encoded"] == "botler"

    def test_issue_agent_template_no_hardcoded_repo(self, tmp_path):
        """参数化后的 issue-agent 模板：渲染后不含 {project_path} 残留占位符，
        且不再写死单仓库路径（如 chenkaidi/shipyard 应被 {project_path} 替换）。"""
        r = self._renderer(tmp_path)
        # 模拟参数化后的全局模板关键片段（与 data/backend/config.yaml 同步）
        tpl = ("项目路径: `{project_path}`（当前仓库根目录即项目）\n"
               "glab issue list --repo {project_path} --state opened\n"
               "curl -k -H \"PRIVATE-TOKEN: $GITLAB_TOKEN\" "
               "\"{gitlab_url}/api/v4/projects/{project_path_encoded}/issues\"\n"
               "export GITLAB_HOST={gitlab_host}")
        v = r.build_variables("botler", self.ISSUE,
                              repo_url="https://home.chenkaidi.top:509/chenkaidi/botler.git")
        rendered = r.render(tpl, v)
        assert "chenkaidi/botler" in rendered
        assert "chenkaidi%2Fbotler" in rendered
        assert "GITLAB_HOST=gitlab.example.com" in rendered  # gitlab_host 去 scheme
        assert "{project_path" not in rendered
        assert "{gitlab_host" not in rendered
        assert "shipyard" not in rendered
