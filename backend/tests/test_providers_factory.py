"""Provider 工厂与注册表测试（issue #484）。

覆盖：注册/注销/覆盖、按平台创建、未知平台报错、平台清单与元信息、
便捷工厂（create_provider）参数处理、接口完整性（所有 Provider 子类
实现全部抽象方法）。
"""

import pytest

from botler.providers import (
    Provider,
    ProviderError,
    ProviderRegistry,
    create_provider,
    registry,
    supported_platforms,
    GiteaProvider,
    GitHubProvider,
    GitLabProvider,
    LocalDemoProvider,
)


class TestProviderRegistry:
    def test_default_registry_has_four_platforms(self):
        assert registry.supported() == ["gitlab", "github", "gitea", "local_demo"]

    def test_register_and_create(self):
        reg = ProviderRegistry()

        class Dummy(Provider):
            platform = "dummy"
            display_name = "Dummy"

            def test_connection(self):
                return True

            def resolve_project(self, ref):
                raise NotImplementedError

            def get_issue(self, project, iid):
                raise NotImplementedError

            def list_open_issues(self, project, limit=None):
                raise NotImplementedError

            def create_issue(self, project, title, description=None, labels=None):
                raise NotImplementedError

            def update_issue(self, project, iid, *, title=None, description=None,
                             state=None, assignee=None):
                raise NotImplementedError

            def add_comment(self, project, iid, body):
                raise NotImplementedError

            def list_issue_notes(self, project, iid, limit=None):
                raise NotImplementedError

            def add_labels(self, project, iid, labels):
                raise NotImplementedError

            def get_pull_request(self, project, number):
                raise NotImplementedError

            def list_pull_requests(self, project, state=None, limit=None):
                raise NotImplementedError

            def create_pull_request(self, project, *, source_branch,
                                    target_branch, title, description=None):
                raise NotImplementedError

            def merge_pull_request(self, project, number):
                raise NotImplementedError

            def get_latest_pipeline(self, project, ref=None):
                raise NotImplementedError

            def list_pipelines(self, project, limit=20):
                raise NotImplementedError

            def list_pipeline_jobs(self, project, pipeline_id):
                raise NotImplementedError

            def list_webhooks(self, project):
                raise NotImplementedError

            def register_webhook(self, project, url, secret=None, events=None):
                raise NotImplementedError

            def unregister_webhook(self, project, hook_id):
                raise NotImplementedError

        reg.register("dummy", Dummy)
        provider = reg.create("dummy")
        assert isinstance(provider, Dummy)
        assert provider.platform == "dummy"
        assert "dummy" in reg.supported()

    def test_register_normalizes_case(self):
        reg = ProviderRegistry()
        reg.register("MyPlatform", LocalDemoProvider)
        assert "myplatform" in reg.supported()

    def test_register_blank_name_rejected(self):
        reg = ProviderRegistry()
        with pytest.raises(ValueError):
            reg.register("  ", LocalDemoProvider)

    def test_register_overwrites(self):
        reg = ProviderRegistry()
        reg.register("x", GitHubProvider)
        reg.register("x", GiteaProvider)
        assert isinstance(reg.create("x", url="https://x.example.com"), GiteaProvider)

    def test_unregister(self):
        reg = ProviderRegistry()
        reg.register("temp", LocalDemoProvider)
        assert "temp" in reg.supported()
        reg.unregister("temp")
        assert "temp" not in reg.supported()
        reg.register("other", LocalDemoProvider)  # 保证至少一个平台用于错误信息
        with pytest.raises(ProviderError):
            reg.create("temp")

    def test_create_unknown_platform_400(self):
        reg = ProviderRegistry()
        reg.register("gitlab", GitLabProvider)
        with pytest.raises(ProviderError) as exc:
            reg.create("bitbucket")
        assert exc.value.status_code == 400
        assert "bitbucket" in exc.value.message
        # 错误信息列出当前支持的平台
        assert "gitlab" in exc.value.message

    def test_describe_metadata(self):
        reg = ProviderRegistry()
        reg.register("gitlab", GitLabProvider)
        reg.register("meta", LocalDemoProvider)
        meta = {item["platform"]: item["display_name"] for item in reg.describe()}
        assert "meta" in meta
        assert meta["meta"] == "本地演示（LocalDemo）"
        assert meta["gitlab"] == "GitLab"


class TestCreateProvider:
    def test_create_gitlab_with_url_token(self):
        provider = create_provider("gitlab", url="https://gitlab.example.com",
                                   token="glpat-x")
        assert isinstance(provider, GitLabProvider)
        assert provider.platform == "gitlab"

    def test_create_github_with_url_token(self):
        provider = create_provider("github", url="https://api.github.com",
                                   token="ghp_x")
        assert isinstance(provider, GitHubProvider)

    def test_create_gitea_with_url_token(self):
        provider = create_provider("gitea", url="https://gitea.example.com",
                                   token="gitea_x")
        assert isinstance(provider, GiteaProvider)

    def test_create_local_demo_no_url(self):
        provider = create_provider("local_demo")
        assert isinstance(provider, LocalDemoProvider)

    def test_create_missing_url_for_remote_platform(self):
        with pytest.raises(ProviderError) as exc:
            create_provider("gitlab", token="x")
        assert exc.value.status_code == 400

    def test_create_unknown_platform(self):
        with pytest.raises(ProviderError) as exc:
            create_provider("svn", url="https://example.com")
        assert exc.value.status_code == 400

    def test_create_case_insensitive(self):
        provider = create_provider("LOCAL_DEMO")
        assert isinstance(provider, LocalDemoProvider)

    def test_supported_platforms_function(self):
        platforms = supported_platforms()
        assert platforms == ["gitlab", "github", "gitea", "local_demo"]


class TestProviderInterfaceCompleteness:
    """所有内置 Provider 子类必须实现抽象基类全部方法（接口完整性）。"""

    @pytest.mark.parametrize("provider_cls", [
        GitLabProvider, GitHubProvider, GiteaProvider, LocalDemoProvider,
    ])
    def test_no_abstract_methods_left(self, provider_cls):
        assert not provider_cls.__abstractmethods__, \
            f"{provider_cls.__name__} 未实现抽象方法: {provider_cls.__abstractmethods__}"

    @pytest.mark.parametrize("provider_cls", [
        GitLabProvider, GitHubProvider, GiteaProvider, LocalDemoProvider,
    ])
    def test_platform_classvar_set(self, provider_cls):
        assert provider_cls.platform, f"{provider_cls.__name__} 未设置 platform"

    def test_factory_can_instantiate_every_platform(self):
        for platform in supported_platforms():
            if platform == "local_demo":
                provider = create_provider(platform)
            else:
                provider = create_provider(
                    platform, url=f"https://{platform}.example.com", token="t")
            assert provider.platform == platform
