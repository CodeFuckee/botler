# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]

### Fixed

- **远端默认主分支跟踪引用缺失时工作区准备失败（issue #148）**：任务 #249
  （graph2plan，125#38）执行报 `[executor] git checkout 失败 (exit 128):
  fatal: 'origin/main' is not a commit and a branch 'main' cannot be created
  from it`。根因：工作区仓库当前分支 master ≠ 远端默认主分支 main，且本地
  缺失 `refs/remotes/origin/main` 跟踪引用——仓库为单分支克隆
  （`--single-branch`）或手工配置了受限 fetch refspec 时，`git fetch --prune`
  只拉取配置的分支，远端默认分支（经 `ls-remote --symref` 权威解析）从未在
  本地落盘；`_checkout_default_branch` 却假设 `git checkout -B main --track
  origin/main` 必然可用。改动：
  - 后端 `executor.py`：`_checkout_default_branch` 执行前先校验本地跟踪引用
    `refs/remotes/<remote>/<branch>` 是否存在（新增 `_remote_tracking_ref_exists`），
    缺失时用命令行显式 refspec `git fetch <remote> <branch>:refs/remotes/<remote>/<branch>`
    拉取补齐（命令行 refspec 不受受限配置影响，也保证后续 `reset --hard
    <remote>/<branch>` 有目标可依）；切回分支不再用 `--track`——受限 refspec
    下 git 无法把跟踪引用映射回远端分支名，`--track` 即使引用已补齐仍报
    "cannot set up tracking information"，改为 checkout 后直接写
    `branch.<name>.remote` / `branch.<name>.merge` 建立上游，标准仓库结果与
    `--track` 等价；
  - **测试**：`test_executor_local_path.py` 新增 2 用例（单分支克隆切回默认
    主分支不失败、本地已在默认分支名但跟踪引用缺失时 `reset --hard` 不失败）；
    后端全量 1308 用例通过（含既有 default-branch/pull 回归）。

### Changed

- **任务开始前自动切回默认主分支并 git pull 同步最新代码（issue #147）**：
  需求「全局模版库中前两点（任务开始先校验当前分支、非主分支切回默认主分支；
  每次开发前 git pull 同步远端最新代码）通过代码实现，完成后再让 agent 操作，
  节省 token」。此前这两点写在全局模版里由 agent（LLM）自行执行，每次任务都
  重复消耗 token；且执行器工作区准备硬编码 `main → master` 回退，未按仓库真实
  默认主分支处理。改动：
  - 后端 `executor.py`：`prepare_workspace`（claude / hermes / dsh 三引擎共用）
    新增 `_resolve_default_branch`（优先 `git ls-remote --symref` 解析服务端
    权威默认主分支并校验分支真实存在，兜底本地 `refs/remotes/<remote>/HEAD`
    符号引用 → `main` → `master`，兼容 `git init --bare` 默认 HEAD 指向不存在
    分支的裸仓库）与 `_checkout_default_branch`（校验当前分支，非默认主分支或
    detached HEAD 时 `git checkout -B <默认主分支> --track <remote>/<默认主分支>`
    切回）；非恢复路径在 `fetch` / `reset --hard` / `clean -fd` 之后显式执行
    `git pull --rebase <remote> <默认主分支>` 兜底同步 fetch 之后的新推送；
    `resume=True`（断点续跑）保持只 fetch 不动工作区，不强制切分支/拉取；
  - 后端 `config.py`：`DEFAULT_TEMPLATE` 工作要求第 1 点改为「本地工作区已由
    平台自动切到默认主分支并 git pull 拉取最新代码……（无需自行切换分支或
    git pull）」；部署侧全局模版库中的前两点指令可随之移除；
  - 文档：`docs/设计方案.md` §5.5 工作区准备命令更新（默认主分支解析 + 显式
    git pull）、§5.6 示例模版同步为当前 `DEFAULT_TEMPLATE`（含 no-close 政策与
    全角括号 issue 引用规范）；`README.md` 执行器职责描述补充自动切回默认主分支
    与 git pull；
  - **测试**：`test_executor_local_path.py` 新增 `TestPrepareWorkspaceDefaultBranchAndPull`
    6 用例（非默认分支自动切回默认主分支、默认主分支非 main（trunk）场景、
    detached HEAD 重新检出默认主分支、远端新提交经 git pull 同步、prepare 显式
    执行 `git pull --rebase`、bare HEAD 指向不存在分支时回退已有 main）；
    既有 11 用例与凭据注入回归（test_executor_credentials.py）全部通过。

### Added

- **git pull 拉取冲突时保留现场交由 agent 手工合并（issue #147 补充）**：
  需求「如果拉取代码的时候出现了冲突，让 agent 来进行合并」。此前
  `prepare_workspace` 的 `git pull --rebase` 遇到合并冲突（本地提交与远端
  分叉、untracked 残留被远端新提交占用等）会直接抛错导致任务在准备阶段失败，
  agent 没有机会介入合并。改动：
  - 后端 `executor.py`：`prepare_workspace` 的 `git pull --rebase` 包上
    冲突判定 `_is_pull_conflict`——工作区实际处于冲突状态（`.git/rebase-merge`
    / `.git/rebase-apply` / `.git/MERGE_HEAD` 存在或 `git ls-files -u` 有未合并
    路径）或 git 输出含明确冲突标志（CONFLICT / could not apply / untracked
    文件将被远端新提交覆盖等）时，不再抛错：保留冲突现场并登记工作区到
    `_pull_conflict_workdirs`，由 agent 手工合并；凭据/网络等非冲突失败照常
    报错。`_build_prompt` / `_resume_prompt`（claude / hermes / dsh 三引擎
    共用）检测到冲突工作区时在提示词末尾追加「先手工解决冲突」指引
    （git status 查看冲突 → 手工合并 → git add + git rebase --continue /
    git commit，严禁 force push）；下一次干净 prepare 自动清除冲突登记；
  - 文档：`docs/设计方案.md` §5.5 补充「拉取冲突处理」说明；`README.md`
    执行器职责描述同步；
  - **测试**：`test_executor_local_path.py` 新增
    `TestPrepareWorkspacePullConflict` 7 用例（pull --rebase 真实冲突不抛错、
    冲突现场保留、提示词追加解决指引、非冲突失败照常抛错、冲突登记在干净
    prepare 后清除、未合并路径/untracked 覆盖错误文本判定、无冲突提示词不
    追加指引）；既有 17 用例全部通过。

- **插件管理页面（issue #145）**：
  需求「增加一个插件页面可以插件进行管理，所有插件的安装、卸载和插件的设置
  都在这个界面」。基于插件体系（issue #140）落地设计文档规划的「前端插件
  管理页」演进项，新增独立「插件」页面（顶部导航入口，路由 `/plugins`）：
  - 后端 `api/plugins.py`（新）：`GET /api/plugins` 按分类（executor /
    model_provider / notifier）分组返回全部已注册插件，含内置/外部来源
    （外部插件附来源路径）与模型供应商默认预设（display_name /
    default_base_url / default_model，与设置页「生图模型」预设同源）；
    `POST /api/plugins/install` 安装外部插件模块——隔离注册表试加载校验
    （文件存在 / 模块可导入且至少注册一个插件 / 与已安装插件无同名同分类
    冲突），通过后写入 `worker.plugin_paths` 并热加载到全局注册表（无需
    重启），任何校验失败 400 拒绝且不落盘，热加载防御性失败回滚配置；
    `POST /api/plugins/uninstall` 卸载外部插件（配置与注册表同时移除，
    内置插件命中「未安装」校验不可卸载）；`POST /api/plugins/reload` 按
    当前 `plugin_paths` 清空并重载外部插件；`PUT /api/plugins/settings`
    设置默认执行引擎（executor 插件设置，复用 `worker.engine`，白名单由
    插件注册表派生，外部引擎插件自动纳入）；
  - 后端 `plugins/base.py`：`PluginRegistry` 增加外部插件来源跟踪
    （`_external_by_path` / `_external_path`）与 `registered_external` /
    `path_of` / `remove_external` / `clear_external`，`load_external`
    支持 `errors` 收集器（安装校验需要精确失败原因）；
  - 前端 `pages/Plugins.jsx`（新）：按分类分组展示全部插件卡片（名称/描述/
    版本/内置徽章/外部来源路径/供应商默认预设）；「安装外部插件」表单
    （多行路径输入，逐项提交）；外部插件「卸载」按钮（confirmDialog
    确认）；「默认执行引擎」radio 设置（executor 插件）；「重新加载外部
    插件」按钮；操作结果 alert 反馈；`App.jsx` 新增「插件」导航与 `/plugins`
    路由；`styles.css` 新增 `.plugin-list` / `.plugin-card` /
    `.badge-external` / `.plugin-install-input` / `.plugin-engine-options`
    等样式；
  - 文档：`README.md` 插件体系章节补充插件管理页说明、API 一览新增
    `/api/plugins` 五个端点；
  - 测试：后端 `test_api_plugins.py`（新）17 用例（列表分组与内置完整性 /
    供应商预设 / 外部来源标记 / 安装成功落盘+热加载 / 空白 strip+重复安装
    拒绝 / 空路径与文件不存在拒绝 / 模块未注册插件拒绝 / 模块加载失败拒绝 /
    与内置插件冲突拒绝 / 卸载移除配置+注册表 / 未安装与空路径拒绝 / 重载
    幂等 / 引擎设置合法持久化与非法拒绝）；前端 `plugins-page.test.mjs`
    （新）12 用例（导航与路由 / 页面结构与 API 端点源码断言 / 分组渲染
    内置与外部插件 / 安装提交 POST / 引擎切换保存 PUT）。



- **灵感一键提交为 GitLab issue（issue #143）**：
  需求「灵感组件，在编辑按钮左边添加一个，添加issue的按钮，点击之后将灵感的
  文本，作为issue的标题和描述，通过gitlab api添加issue，默认添加标记feature
  以及ui」。
  实现：
  - 后端 `api/inspirations.py`：新增 `POST /api/inspirations/{id}/add-issue`——
    灵感内容同时作为 issue 标题与描述，默认标签 `feature` + `ui`（GitLab 会
    自动创建不存在的标签），不指定分配人；写操作走 owner token（复用
    `issues._issue_edit_call`，未配置 owner token 返回 400 引导设置、绝不回退
    bot token）；灵感不存在 404 / 仓库不存在或已软删除 400 / 仓库未启用 400 /
    GitLab 故障 502；创建成功后清空概览缓存，返回精简 issue 对象
    （含 iid/web_url 供前端提示与跳转）；
  - 前端 `Overview.jsx`：灵感条目操作区在「编辑」按钮左侧新增
    「📌 添加 Issue」按钮——请求中禁用防重复提交，成功展示新 issue 链接并
    立即刷新开放 issue 列表，失败展示错误；板块说明文案同步更新；
  - 样式 `styles.css`：新增 `.inspiration-add-issue-btn`（主色描边区分动作、
    禁用态）（创建成功提示沿用既有 `.alert-ok` 样式）；
  - 文档：`README.md` 后端 API 表新增 `POST /api/inspirations/{id}/add-issue`
    一行；
  - 测试：后端 `test_api_inspirations.py` 新增 `TestAddIssueFromInspiration`
    8 用例（正常路径 / 多行内容保留换行 / 灵感不存在 404 / 仓库未启用 400 /
    仓库软删除 400 / GitLab 故障 502 / 未配置 owner token 400 / 成功后清空
    概览缓存）；前端 `overview-inspirations.test.mjs` 新增 6 用例（按钮位于
    编辑左侧源码断言 / 提交调用路径 / 渲染按钮顺序 / 点击提交并刷新列表 /
    提交中禁用防重复 / 失败显示错误）。


- **灵感 / CI/CD 页面可配置是否显示未启用项目（issue #142）**：
  需求「灵感页面和 ci\cd 页面显示是否显示未启用项目，增加通过设置去配置」。
  实现：
  - 设置项 `ui.show_disabled_repos`（默认 `true` = 显示，保持现状）：灵感板块
    与 CI/CD 流水线板块是否展示 Botler 中未启用的仓库；`false` 时两个板块只
    展示已启用仓库，未启用仓库不再发起 GitLab 流水线查询（省流量）；
  - 后端 `config.py`：`Settings` 新增 `ui_show_disabled_repos` 字段（布尔，
    缺省/非布尔回退默认），`KNOWN_FIELDS["ui"]` 加入 `show_disabled_repos`；
    `api/settings.py` 读写（GET 返回 / PUT 校验布尔，非法值 400）与保存后
    清空流水线概览缓存（开关立即生效，不等 10 秒 TTL）；
  - 后端 `api/pipelines.py` / `api/inspirations.py`：`_collect` 与
    `inspiration_overview` 在设置关闭时过滤 `enabled=false` 的仓库；
  - 前端 `Settings.jsx`「界面显示」卡片新增「显示未启用项目」复选框
    （`ui.show_disabled_repos`），随全局「保存」提交；
  - 文档：`README.md` 关键配置表、`backend/config.example.yaml` 新增
    `ui.show_disabled_repos` 说明；
  - 测试：`test_api_settings.py` 新增 `TestShowDisabledReposSettings` 5 用例
    （默认 true / false 持久化 / 重新开启 / 非布尔拒绝 / 部分更新不影响
    timezone）；`test_api_pipelines.py` 新增设置关闭时过滤未启用仓库用例
    （不发起 GitLab 查询）；`test_api_inspirations.py` 新增默认包含未启用
    仓库 / 关闭时过滤 / 已启用仓库灵感不受影响 3 用例；前端
    `settings-show-disabled-repos.test.mjs`（新）3 用例（开关存在 / 说明覆盖
    灵感与 CI/CD / 保存提交）。


- **插件体系：执行引擎 / 大模型 API 供应商 / webhook 发送任务消息插件化（issue #140）**：
  需求「平台想要实现一个插件体系，把执行引擎、大模型 api 供应商、webhook 发送
  任务消息都做成插件的形式，设计一个实现方案」。
  实现：
  - 设计文档 `docs/插件体系设计方案.md`（新）：插件分类 / 插件基类与注册表 /
    三类插件接口定义 / 内置插件迁移清单 / 调用方改造点 / 配置设计 / 测试计划 /
    演进方向；
  - 后端 `botler/plugins/base.py`（新）：插件体系核心——`PluginKind` 枚举
    （executor / model_provider / notifier 三分类）、`Plugin` 基类（分类 + 标识 +
    描述 + 版本）、`ExecutorPlugin` / `ImageProviderPlugin` / `NotifierPlugin`
    三类插件接口、`PluginRegistry` 注册表（注册 / 查询 / 列表 / 外部加载，
    重复注册抛冲突异常，未注册抛缺失异常）、全局注册表单例与便捷函数
    （`get_plugin` / `list_plugins` / `has_plugin` 等）；
  - 后端 `botler/plugins/executors.py`（新）：执行引擎插件——`claude`
    （Claude Code CLI，默认）/ `hermes`（hermes runner，issue #47）/ `dsh`
    （deepseek-harness SDK，issue #84）三个内置引擎插件（适配器委托现有实现，
    断点续跑语义不变）；`executor._engine` 校验改为注册表驱动（未注册引擎名
    回退 claude），`_run_once` 按引擎名查插件委托执行（原 claude 分支抽为
    `_run_claude_once`）；
  - 后端 `botler/plugins/models.py`（新）：大模型（生图）供应商插件——
    `ImageResult` / `ImageModelError` / 默认超时与尺寸常量迁移至此，
    `gemini_nano_banana`（Gemini generateContent）与 `openai_gpt_image`
    （OpenAI images API）两个内置供应商插件（实现自 `image_models._generate_*`
    迁移）；`image_models.ImageModelClient` 改为按 provider 查插件注册表委托
    调用，`IMAGE_MODEL_PRESETS` 由注册表派生，对外导出与行为完全兼容；
  - 后端 `botler/plugins/notifiers.py`（新）：任务消息发送通道插件——
    `webhook`（外部 Webhook HTTP 推送，issue #136）与 `in_app`（网页通知事件，
    issue #21）两个内置通道插件；executor 收尾统一向全部 notifier 插件分发
    （成功 / 失败事件，webhook 推送前拉取 issue 完整信息，任一通道失败仅记
    日志不阻塞任务收尾）；
  - 后端 `config.py` / `config.example.yaml`：新增可选 `worker.plugin_paths`
    （外部插件 Python 模块路径列表，启动时 `importlib` 加载注册，失败仅记
    日志不阻塞启动）；`api/settings.py` 引擎白名单改为插件注册表派生（新增
    引擎自动可配置）、`plugin_paths` 读写与校验（字符串数组、空白项剔除）；
    `main.py` 应用启动时加载外部插件；
  - 文档：`README.md` 新增「插件体系」章节与配置说明（`worker.plugin_paths`）、
    目录结构更新；
  - 测试：`test_plugins.py`（新）22 用例（注册表注册/查询/重复冲突/缺失/列表
    顺序/独立注册表隔离/外部加载·缺失文件·异常容错/插件能力方法默认行为/
    全局注册表内置插件完整性）；`test_executor_plugins.py`（新）19 用例
    （引擎名校验走注册表·未注册回退 / _run_once 三引擎委托路径·断点续跑参数
    透传 / 引擎插件元信息 / notifier 插件分发·单通道失败不阻塞其他通道）；
    `test_api_settings.py` 追加 `TestPluginPathsSettings` 5 用例（plugin_paths
    读写 / 空白剔除 / 非法值拒绝 / 部分更新）+ 全量回归（引擎白名单行为不变）。


- **设置页整理分组并新增左侧导航栏：可搜索设置项、折叠/展开分组子项（issue #139）**：
  需求「设置页设置项太多，整理一下设置页面的分支，并在设置左侧添加一个导航栏，
  导航栏可以搜索设置项，同时也可以折叠和展开分组中的子项」。
  实现：
  - 前端 `components/SettingsNav.jsx`（新）：设置页左侧导航栏组件，导出分组配置
    `SETTINGS_GROUPS`（6 组 14 项，子项带锚点 id / 显示名 / 搜索关键字）——
    顶部搜索框按名称与关键字过滤设置项（中英文别名、配置键均可命中），搜索时自动
    展开命中分组并显示命中数，无结果给出空状态提示；每个分组可折叠/展开其子项
    （`aria-expanded` 无障碍状态），导航头部提供「全部收起 / 全部展开」；
    点击子项平滑滚动（`scrollIntoView` smooth）到页面相应设置区块并高亮当前项；
  - 前端 `pages/Settings.jsx`：设置页改为两栏布局（左侧导航 + 右侧内容区），
    按功能整理为 6 个分组（外部服务接入 / 系统设置 / 执行引擎 / 运维与数据 /
    账号与安全 / 关于），每个设置区块包裹 `<section id=...>` 锚点（与导航子项
    一一对应），分组间插入分组标题；既有卡片顺序与功能全部保持不变；
  - 前端 `styles.css`：新增 `settings-layout` / `settings-sidebar` / `settings-nav*`
    等样式（240px 吸顶导航 + 内容区，Geist 设计令牌），`scroll-margin-top` 保证
    滚动定位不被 sticky 顶导航遮挡，窄视口（≤860px）回落单栏导航置顶；
  - 文档：`README.md` 配置说明补充设置页导航分组与搜索/折叠交互说明；
  - 测试：`frontend/tests/settings-nav.test.mjs`（新）15 用例（导航挂载与两栏布局 /
    分组标题与锚点归属一一对应 / SETTINGS_GROUPS 配置完整性 / 渲染分组与子项 /
    搜索过滤·关键字命中·自动展开·无结果空状态·清空恢复 / 分组折叠展开·全部收起
    展开 / 点击子项 scrollIntoView 滚动与高亮 / 样式规则）+ 设置页既有用例全量回归。

- **设置里配置了 deepseek api 时，概览页展示 DeepSeek 账户余额（issue #138）**：
  需求「如果设置里配置了 deepseek api，则在概览页面显示 deepseek 账户里的余额」，
  请求接口为 `GET https://api.deepseek.com/user/balance`（`Authorization: Bearer <TOKEN>`）。
  实现：
  - 后端 `deepseek_balance.py`（新模块）：`resolve_deepseek_credentials` 凭据解析链
    （与 executor._dsh_credentials 一致：dsh 段 api_key > 设置页「AI API 供应商」
    provider=deepseek 且启用的项 > 环境变量 `DEEPSEEK_API_KEY`）；
    `DeepSeekBalanceClient`——httpx 代调 user/balance（10s 超时，verify_ssl 跟随全局，
    `Authorization: Bearer` 认证，预设 base_url 尾部 `/v1` 归一化后拼 `/user/balance`，
    与需求示例地址一致）；API Key 只存在于服务端，明文不流转到前端；
  - 后端 `api/settings.py`：新增 `GET /api/settings/deepseek-balance`——未配置 Key 返回
    configured=false（前端不渲染卡片）；已配置则代调查询返回
    `{configured, balance:{is_available, balance_infos, fetched_at}, error}`；
    查询失败返回 error 字段（不抛 500，与 webhook-test 同容错策略）；
  - 前端 `pages/Overview.jsx`：新增「DeepSeek 账户余额」卡片——配置了 deepseek api 时
    展示（未配置整卡不渲染），显示账户可用状态 / 币种 / 总余额 / 赠送 / 充值余额 /
    更新时间，60 秒低频轮询 + 「↻ 刷新」按钮；接口失败保留上次数据不打扰页面；
  - 前端 `styles.css`：新增 deepseek-balance-* 余额卡片样式（与其他概览板块视觉一致）；
  - 文档：`README.md` API 一览新增余额端点、配置说明补充 `ai_providers[]` 行并说明
    余额 Key 解析链；
  - 测试：后端 `test_deepseek_balance.py`（新）18 用例（凭据解析链优先级 / 停用与空
    key 跳过 / 环境变量兜底 / base_url 归一化 / 客户端请求构造与响应解析 / 非 2xx、
    超时、网络异常 / 端点未配置、成功、接口报错、环境变量配置且无 Key 泄露）+
    `test_api_settings.py` 全量回归；前端 `overview-deepseek-balance.test.mjs`（新）
    9 用例（源码断言 / 渲染卡片各字段 / 未配置不渲染 / 接口报错与网络失败容错 /
    刷新按钮重新请求 / 空余额空状态）+ 概览页既有 240 用例全量回归。

- **设置页「识图模型」改为「生图模型」，并新增测试按钮验证配置可用（issue #137）**：
  需求「把设置里的识图模型改成生图模型，同时增加一个测试按钮，在用户配置好url、
  apikey以及生图模式后，可以调用测试按钮，测试生图模型是否能用」。上一轮（issue
  #135）设置的图像模型本身即为图像生成接口（Gemini generateContent 图像输出 /
  OpenAI images 接口），本期将设置页命名纠正为「生图模型」，并补上配置可用性验证：
  - 后端 `api/settings.py`：新增 `POST /api/settings/image-model-test` 测试端点——
    接收表单提交的 provider（生图模式）/ base_url / api_key / model，用
    `ImageModelClient.generate` 真实调用一次生图接口（轻量测试 prompt，60s
    超时，verify_ssl 跟随全局设置）；api_key 掩码/留空、url/model 留空按 name
    回退已保存配置（与 image_models 保存同模式）；生图成功返回 ok=true + 生成
    张数/mime，缺配置/接口报错/网络异常均返回 ok=false + 原因，不抛 500（与
    webhook-test 同容错策略）；
  - 后端 `image_models.py` / `config.py` / `config.example.yaml`：文案统一
    「识图模型」→「生图模型」（错误提示、注释、示例配置）；
  - 前端 `components/ImageModelsCard.jsx`：卡片标题与全部文案改为「生图模型」，
    「模型类型」字段改名「生图模式」；新增测试按钮——列表行「测试」（只提交
    name + provider，按已保存配置测试）与编辑表单内「测试配置」（用当前表单
    值，未保存也可测），测试中/成功/失败均有状态提示；
  - 前端 `providers.jsx` / `pages/Settings.jsx`：注释文案同步改为「生图模型」；
  - 测试：`backend/tests/test_api_settings.py` 新增 `TestImageModelTestEndpoint`
    （成功/缺生图模式/掩码回退已保存配置/接口报错/未知 provider/verify_ssl 跟随
    全局），`backend/tests/test_image_models.py` 文案同步；
    `frontend/tests/settings-image-models-card.test.mjs` 新增测试按钮与结果提示
    断言、文案改为「生图模型」。
- **任务完成时调用 webhook 进行消息推送，可在设置页配置（issue #136）**：
  需求「任务完成时，添加调用webhook来进行消息推送，可以在设置页面配置webhook地址」，
  配置选项：webhook 地址、Content-Type、Authorization、POST 结构体（可使用全局
  模板中的占位符，请求的时候自动填充）。任务成功收尾（打 bot-done 标签）后按配置
  POST 推送，推送为尽力而为——失败仅记日志，不阻塞任务收尾（与网页通知同容错策略）。
  实现：
  - 后端 `config.py`：`Settings` 新增 `webhook_enabled / webhook_url /
    webhook_content_type（默认 application/json）/ webhook_authorization /
    webhook_body_template` 五个字段 + `update_webhook` 落盘（authorization
    掩码值/空串 = 保持现有凭据，与 sso.client_secret 同模式）；新增内置默认
    POST 结构体模板 `DEFAULT_WEBHOOK_TEMPLATE`（body_template 留空时归一使用）；
    `KNOWN_FIELDS` 增加 `webhook` 段；
  - 后端 `webhook_push.py`（新模块）：`WebhookPusher`——复用提示词模版占位符
    机制（`templates.PLACEHOLDERS`：repo_name / issue_title / issue_body /
    issue_url / gitlab_url / project_id / issue_iid / project_path /
    project_path_encoded / gitlab_host）渲染 POST 结构体；`send_task_succeeded`
    任务成功推送（未启用/未配置地址不发送）、`send_test` 测试推送；请求带
    Content-Type / Authorization 头，verify 跟随全局 verify_ssl，15s 超时，
    非 2xx 与网络异常抛 `WebhookPushError`；
  - 后端 `executor.py`：`ClaudeExecutor` 注入 `WebhookPusher`，`_finish_succeeded`
    成功收尾时调用 `_push_webhook_succeeded`——推送前拉取 issue 完整信息
    （正文/链接供占位符渲染，失败降级用任务记录数据 + 链接拼接兜底），
    推送成功/失败均记任务日志，异常不阻塞收尾；
  - 后端 `api/settings.py`：`GET /api/settings` 返回 `webhook` 段
    （authorization 只返回掩码）；`PUT` 支持 webhook 段更新（`_validate_webhook`：
    enabled 布尔 / url 须 http(s) 开头 / content_type 空白归一）；新增
    `POST /api/settings/webhook-test` 测试推送端点（未配置地址 / 非 2xx /
    网络异常均返回 ok=false + 原因，不抛 500）；
  - 前端 `pages/Settings.jsx`：新增「消息推送 Webhook」卡片（网页通知卡片之后）——
    启用开关、地址、Content-Type、Authorization（密码输入框，留空 = 保持现有
    凭据）、POST 结构体（textarea + 全局模板占位符说明，从
    templates.placeholders 动态展示）、「发送测试推送」按钮（调用
    webhook-test 端点展示结果）；跟随全局「保存」提交 webhook 段；
  - 文档：`README.md` 关键配置表、`backend/config.example.yaml` 新增
    webhook 段注释示例；
  - **测试**：后端 `test_webhook_push.py`（新）19 用例（配置默认值 / 占位符
    变量构建与链接兜底 / 模板渲染 / httpx mock 发送链路：成功、非 2xx、网络
    异常、未配置地址 / 未启用与无地址不发送 / 测试推送）+ `test_api_settings.py`
    新增 `TestWebhookSettings` 10 用例（GET 默认值 / PUT 持久化与掩码保持 /
    URL 与类型校验 / content_type 归一 / webhook-test 未配置、成功、非 2xx、
    异常容错）+ 前端 `settings-webhook-card.test.mjs`（新）7 用例（卡片挂载与
    位置 / 字段齐全 / 保存走 webhook 段与 authorization 留空保持 / 占位符说明 /
    测试推送端点）；后端 1175 + 前端 600 全量测试通过。

- **设置页新增「识图模型」配置，内置 Gemini Nano Banana Pro 与 GPT Image 2 两个接口（issue #135）**：
  需求「设置页面，增加配置识图模型的设置，目前先实现 gemini 的 nano banana pro 以及
  gpt image 2 的接口」。参照 AI API 供应商配置（issue #46）模式实现——本期交付
  「配置存储 + 调用接口封装」，为后续 AI 功能消费做准备，不接入具体业务。
  实现：
  - 后端 `config.py`：`Settings` 新增 `image_models` 列表（每项
    `{name, provider, base_url, api_key, model, enabled}`，与 ai_providers
    同构，api_key 支持 `${ENV}` 引用落盘 config.yaml）+ `update_image_models`
    整体替换落盘；
  - 后端 `api/settings.py`：`GET /api/settings` 返回 `image_models`
    （api_key 只返回掩码）、`PUT` 校验（name 必填且不重复 / base_url 须
    http(s) 开头 / provider 缺省归一 custom / enabled 布尔 / 掩码或留空 =
    保持现有），新增 `_validate_image_models`；
  - 后端 `image_models.py`（新模块）：统一 `ImageModelClient.generate()`
    调用封装——`gemini_nano_banana`（默认模型 `gemini-3-pro-image`，走
    `generateContent`，支持文本 prompt + base64 inline_data 图片输入、
    输出 inlineData 解码）与 `openai_gpt_image`（默认模型 `gpt-image-2`，
    走 images/generations JSON 与 images/edits multipart）；含超时/网络/
    非 2xx/缺 key/缺图像结果等错误诊断；`find_enabled` / `client_from_config`
    工具函数；
  - 前端 `providers.jsx`：新增 `IMAGE_MODEL_PRESETS`（gemini_nano_banana /
    openai_gpt_image / custom，默认 base_url 与 model 自动填充，logo 复用
    gemini / openai 品牌图标）；
  - 前端 `components/ImageModelsCard.jsx`（新组件）：设置页「识图模型」
    卡片，增删改查 + 独立保存按钮（只提交 image_models 段）+ API Key
    掩码回填；`pages/Settings.jsx` 在 AI 供应商卡片之后挂载；
  - 文档：`README.md` 目录结构与关键配置表、`backend/config.example.yaml`
    新增 image_models 段注释示例；
  - **测试**：后端 `test_api_settings.py` 新增 `TestImageModelsSettings`
    13 用例（GET 空列表 / PUT 持久化与掩码 / 掩码与留空不覆盖 / 整体替换 /
    清空 / name 必填与唯一 / base_url 校验 / provider 归一 / enabled 布尔 /
    `${ENV}` 引用）+ `test_image_models.py` 16 用例（两 provider 请求构造
    URL/认证头/请求体、参考图片输入、非 2xx 与缺图像响应报错、缺 key/空
    prompt 拦截、预设与工具函数）；前端新增
    `settings-image-models-card.test.mjs` 7 用例（卡片挂载与位置、增删改
    表单、保存走 image_models 段、预设与 logo）；后端 1146 + 前端 594 全量
    测试通过。

- **概览页开放 Issue 板块：每个仓库卡片右上角新增「对账」按钮（issue #134）**：
  需求「概览页面中的开放issue组件，每个仓库的右上角添加一个对账按钮，点击后对账该仓库issue」。
  复用仓库页对账接口 `POST /api/repos/{repo_id}/reconcile`（issue #17）：点击后同步扫描该
  仓库，把「assignee 是 bot 但任务表无活跃记录」的 open issues 补入任务队列，并以小字展示
  对账结果（入队 N 个 = 发现待处理；0 个 = 无需处理；仓库停用显示后端 note；失败显示错误）。
  实现：
  - 前端 `Overview.jsx`：仓库卡片头右上角新增「对账」按钮（`↻ 对账`，请求中 `↻ 对账中…`
    并禁用防重复点击，与仓库页对账按钮一致为低危操作无需确认），与「添加 Issue」按钮
    并排成组（新增 `.issue-repo-actions` 操作组容器整体推右）；新增 `reconcileRepo`
    回调与 `ReconcileResult` 结果展示组件（入队 >0 / 无需处理 / 停用 note / 失败错误
    四种形态）；
  - 前端 `styles.css`：新增 `.issue-repo-actions`（margin-left:auto 推右、inline-flex
    并排、gap 用间距 token）、`.reconcile-btn`（white-space:nowrap 防换行）、
    `.reconcile-result`（卡片头下方间距）；「添加 Issue」按钮推右职责移交操作组容器；
  - 后端：无改动（复用 issue #17 的 `POST /api/repos/{id}/reconcile` 接口）；
  - 文档：`README.md` API 一览补充 `POST /api/repos/{id}/reconcile` 行；
  - **测试**：前端新增 `overview-repo-reconcile.test.mjs` 10 用例（按钮渲染与操作组
    结构、点击调对账接口参数正确、请求中禁用防重复点击、入队 >0/=0 两种成功结果、
    停用 note、失败错误、低危无需确认、源码与样式断言）；前端 587 + 后端 1117 全量
    测试通过。

- **概览页新增「灵感」板块：开放 Issue 下方、CI/CD 流水线上方按仓库随手记录新功能灵感（issue #131）**：
  需求「概览页面，在开放issue下方，ci\cd流水线上方增加灵感页面，可以在这里按仓库随手记录下来
  关于对应仓库的一些新功能的灵感，这些灵感只保存在项目的数据库，不要提交到issue上」。灵感是
  用户对对应仓库新功能的随手笔记，与 GitLab issue 完全隔离——只保存在 Botler 本地 SQLite
  数据库，不调用任何 GitLab API、不创建/修改 issue。
  实现：
  - 后端 `database.py`：新增 `inspirations` 表（repo_id / content / created_at / updated_at），
    v8 迁移（`PRAGMA user_version` 7 → 8，旧库启动自动建表）；新增
    `list_inspirations` / `get_inspiration` / `create_inspiration` /
    `update_inspiration`（刷新 updated_at）/ `delete_inspiration` 方法；
  - 后端 `api/inspirations.py`（新模块，注册进 /api 路由）：
    `GET /api/inspirations/overview`（聚合所有未软删除仓库 + 各自灵感，仓库按优先级排序、
    灵感按 updated_at 降序，无灵感的仓库也返回供前端展示空状态与添加表单）、
    `POST /api/inspirations`（创建，repo_id 须指向存在且未软删除的仓库，内容去首尾空白
    后非空且 ≤ 5000 字）、`PUT /api/inspirations/{id}`（更新内容）、
    `DELETE /api/inspirations/{id}`（删除）；
  - 前端 `Overview.jsx`：新增 `inspirations-section`（源码位置在 issues-section 与
    pipelines-section 之间），每仓库一张卡：灵感列表（内容 / 更新时间 / 编辑 / 删除）+
    底部「随手记录」表单；本地增删改成功后立即刷新列表，15 秒轮询兜底多标签页并发；
    新增 `INSPIRATION_POLL_MS = 15000` 常量与 `loadInspirations` /
    `submitNewInspiration` / `saveInspiration` / `deleteInspiration`；
  - 前端 `styles.css`：新增 `.inspirations-section` 系列样式（网格/卡片/列表/表单，
    与开放 Issue 板块视觉语言一致，含 hover 轻抬升动效）；
  - 文档：`README.md` API 一览补充 4 个灵感接口；
  - **测试**：后端 `test_api_inspirations.py` 20 用例（overview 聚合与排序、软删除仓库
    过滤、创建/更新/删除、空内容/纯空白/超长/仓库不存在/记录不存在等边界）；
    `test_database_migrate.py` 新增 v8 迁移与灵感 CRUD 4 用例；
    前端 `overview-inspirations.test.mjs` 12 用例（板块顺序、渲染、增删改交互、空状态、
    失败兜底）；`apple-hig` / `overview-issue-task` 既有用例适配三板块结构；
    后端 1108 + 前端 577 全量测试通过。

### Fixed

- **dsh 引擎（deepseek-harness SDK）任务详情页事件流同一语句出现两次（issue #144）**：
  用户反馈「使用 deepseek harness sdk 作为执行引擎时候，任务详情页里的事件流，同样
  的语句都是出现两次」。根因：SDK 对同一 assistant 输出会先后发射两类通知——
  `assistant/chunk`（text-delta / reasoning-delta 流式增量）与 `assistant/message`
  （由增量拼成的完整 content 块）；`dsh_runner._format_assistant_content` 把两类
  都转成 `stream_delta` / `thinking` 事件行，导致事件流（SSE 实时 + 日志回放）里
  每条语句都出现两次（任务 #242 日志实测：143 条唯一文本中 31 条精确成对重复，
  其余为增量片段 + 完整句的成对重复）。
  实现：
  - 后端 `botler/dsh_runner.py`：`_format_assistant_content` 仅保留 chunks 未覆盖
    的 `tool_use` 块（转 `tool_start`），`text` / `thinking` / `reasoning` 完整块
    不再产事件行——流式阶段（chunk 增量）已实时展示过同一内容，完整块重复发布是
    冗余；`format_dsh_notification` docstring 同步说明（issue #144）；
  - 测试：`test_dsh_runner.py` 新增复现用例
    `test_message_after_chunk_delta_not_duplicated`（chunk 增量 + 完整块同内容
    时，完整块不产行）；原 `test_text_block_becomes_stream_delta` /
    `test_thinking_block_becomes_thinking` / `test_reasoning_block_becomes_thinking`
    / `test_multi_block_keeps_order` 适配新语义（message 文本/思考块不产行，仅
    tool_use 转 tool_start）；
  - 后端全量 pytest 1264 passed、前端 node --test 645 passed。

- **「界面显示」卡片取消勾选「显示未启用项目」后没有保存按钮，无法保存设置（issue #142 反馈轮）**：
  用户反馈「取消勾选后没有保存按钮，无法保存设置」。现状与差距：issue #142 新增的
  `ui.show_disabled_repos` 开关位于「界面显示」卡片，而设置页全局「保存」按钮在上方
  「任务调度」卡片内——用户滚动到「界面显示」卡片修改开关后找不到保存入口，误以为
  无法保存（与 issue #27 SSO 卡片 / issue #141 Webhook 卡片同型问题）。
  实现：
  - 前端 `pages/Settings.jsx`：「界面显示」卡片内新增独立「保存界面显示配置」按钮与
    成功提示（uiSaved 2 秒自动消失），绑定新增 `saveUi`——只提交 ui 段
    （`PUT /api/settings {ui: ...}`，后端支持部分更新，与 `saveSso` / `saveWebhook`
    同模式），不影响 worker/claude/notifications 等其他设置；ui 段统一由新增
    `buildUiPatch` 构建（timezone + show_disabled_repos，全局 save 与卡片内 saveUi
    共用，行为一致）；保存成功后同步更新页面显示时区（与全局保存一致，无需刷新）；
    卡片说明文字由「点击上方『保存』」改为「点击下方『保存界面显示配置』」，不再
    误导用户去其他卡片找保存按钮；全局「保存」按钮与既有 ui 保存链路保持不变；
  - **测试**：新增 `frontend/tests/settings-ui-save-button.test.mjs` 5 用例
    （卡片内含独立保存按钮且绑定 saveUi / saveUi 只提交 ui 段不携带 worker 等
    其他字段 / buildUiPatch 同时携带 timezone 与 show_disabled_repos / 说明文字
    不再指向「上方保存」/ 全局保存按钮仍存在）；既有
    `settings-show-disabled-repos.test.mjs` 适配 buildUiPatch 结构；
    前端 645 + 后端 1263 全量测试通过。

- **消息推送 Webhook 设置没有保存按钮，无法保存设置（issue #141）**：
  需求「消息推送 Webhook设置没有保存按钮，无法保存设置」。现状与差距：设置页
  全局「保存」按钮位于上方「任务调度」卡片，而「消息推送 Webhook」卡片在页面
  下方，卡片内只有表单字段与「发送测试推送」按钮——用户滚动到 Webhook 卡片
  修改配置后找不到保存入口，误以为无法保存（与 issue #27 SSO 卡片同型问题）。
  实现：
  - 前端 `pages/Settings.jsx`：Webhook 卡片内新增独立「保存 Webhook 配置」
    按钮与成功提示（webhookSaved 2 秒自动消失），绑定新增 `saveWebhook`——
    只提交 webhook 段（`PUT /api/settings {webhook: ...}`，后端支持部分更新，
    与 `saveSso` 同模式），不影响 worker/claude/notifications 等其他设置；
    authorization 留空 = 保持现有凭据（复用 buildWebhookPatch，与全局 save 共用）；
    卡片说明文字由「点击上方『保存』」改为「点击下方『保存 Webhook 配置』」，
    不再误导用户去其他卡片找保存按钮；全局「保存」按钮与既有 webhook 保存链路
    保持不变；
  - **测试**：新增 `frontend/tests/settings-webhook-save-button.test.mjs`
    5 用例（卡片内含独立保存按钮且绑定 saveWebhook / saveWebhook 只提交
    webhook 段不携带 worker 等其他字段 / authorization 留空不覆盖现有凭据 /
    说明文字不再指向「上方保存」/ 全局保存按钮仍存在）；
    前端 616 + 后端 1200 全量测试通过。

- **显示 gitlab token 隔离：设置页 owner gitlab token 所有 Agent 均不可使用（issue #130）**：
  需求「显示gitlab token隔离，设置页面里的owner gitlab token，所有agent都不可以实现，
  已经在系统架构上实现隔离，避免agent错误调用；owner gitlab token只允许在概览页面上
  编辑issue、添加issue、关闭issue、在issue添加评论以及回复issue评论的时候使用，其他
  场景都不得使用；agent无论如何都不能使用owner gitlab token，他只能使用自己仓库的
  认证token来进行issue编辑」。现状与差距：owner token（issue #87）已掩码展示、git
  推送凭据走 bot token 不含 owner，但 agent 会话环境 `GITLAB_TOKEN` 仍把 owner
  token 作为最高优先级注入——agent 能拿到并错误调用 owner token，与"所有 Agent
  不可使用"矛盾；概览页 issue 编辑操作也未优先使用 owner token。
  实现：
  - 后端 `executor.py`：`_build_env` 会话 `GITLAB_TOKEN` 注入改为
    remote url 内嵌 token（仓库自己的认证 token）→ 全局 bot token，**绝不注入
    owner token**（owner 只允许在概览页 issue 编辑操作时由平台使用）；git 推送
    凭据仍走 `GIT_ASKPASS` 的 bot token，不受影响；
  - 后端 `api/issues.py`：新增 `_owner_client`（按 token 值缓存重建）与
    `_issue_edit_call`（owner 优先、owner 401/403 回退 per-repo → 全局）；
    概览页 5 处 issue 编辑操作（关闭 issue、编辑标签、添加评论、回复评论、
    添加 issue）统一优先 owner token，符合"只允许在概览页面上编辑 issue 时
    使用"；form-meta（只读查询）保持原链路；
  - 前端 `Settings.jsx`：Owner GitLab Token 卡片标题新增「已隔离 · Agent
    不可用」徽章（`h2 .badge` 对齐样式），说明区明确隔离规则——所有 Agent
    均不可使用、允许使用范围（仅概览页编辑/添加/关闭 issue、添加/回复评论）、
    Agent 只能使用自己仓库的认证 token 编辑 issue；
  - 文档：`config.example.yaml` owner_token 注释补充隔离说明；
  - **测试**：后端 `test_owner_token.py` 原"会话 GITLAB_TOKEN 优先 owner"
    用例改为"绝不注入 owner token"；`test_api_issues.py` 新增 7 用例（关闭/
    标签/评论/回复/添加 issue 优先 owner、未配置 owner 沿用原链路、owner
    401 回退原链路）；前端 `settings-owner-token.test.mjs` 新增 2 用例（隔离
    徽章显示、隔离规则说明），`cardSource` 正则兼容 h2 内徽章；后端 1084 +
    前端 565 全量测试通过。
- **概览页 issue 编辑必须使用 owner token：修复未配置时静默以 code01 身份发布（issue #132）**：
  需求「我在概览页面的最新回复，为什么是以code01的用户回复的，我不是在 issue #130里说明了，
  概览页面的所有issue编辑都要使用owner token吗，帮我诊断并修复」。诊断：概览页 5 处 issue
  编辑操作（关闭 issue、编辑标签、添加评论、回复评论、添加 issue）虽已「优先」owner token
  （issue #130），但部署环境 `gitlab.owner_token` **未配置**时 `_issue_edit_call` 会静默
  回退 per-repo/全局 bot token——用户经概览页发布的评论/回复以 code01（bot）身份发出
  （实测复现：issue #78 最新回复「如果将hermes 的集成方式从http api改成hermes agent sdk集成…」
  即因此以 code01 发布）。同时发现 issue #130 合规缺口：executor/reconciler 的任务生命周期
  评论与终态标签补打仍传 `prefer_owner=True`，一旦配置 owner token，会把「🤖 Botler」机器人
  消息以 owner 身份发布，同样违背「owner token 只允许概览页使用、agent 无论如何都不能使用」。
  修复：
  - 后端 `api/issues.py`：`_issue_edit_call` 改为**强制 owner token**——未配置
    `gitlab.owner_token` 返回 400 明确提示先到设置页配置，owner 401/403 返回 502 提示
    更新 token，**不再静默回退 bot token**（杜绝再次以 code01 身份发布）；
  - 后端 `executor.py`：移除 `_owner_gitlab_client` 与 `_call_with_fallback` 的
    `prefer_owner` 机制，7 处任务生命周期评论/打标签调用固定走「全局 bot token →
    remote 内嵌 token」链路（agent 绝不使用 owner token）；
  - 后端 `reconciler.py`：移除 `_owner_client` 与 `prefer_owner` 机制，终态标签补打
    固定走 bot 身份（对账非概览页操作）；
  - 前端 `Overview.jsx`：启动时检测 owner token 配置状态，未配置时开放 Issue 板块
    顶部显示醒目警示横幅（提示概览页 issue 编辑必须使用 owner token、操作会被拦截、
    不会以 code01 身份发布）；`styles.css` 新增 `.alert-warning` 样式；
  - 文档：`config.example.yaml` 与 `docs/GitLab-Owner-Token-申请教程.md` 更新（未配置
    owner token 时概览页编辑被拦截，不再回退 bot token；任务侧绝不使用 owner token）；
  - **测试**：后端 `test_api_issues.py` 原「未配置 owner 沿用原链路 / owner 401 回退」
    用例改为「未配置 owner 400 拦截、owner 401 502 拦截」（关闭/标签/评论/回复/添加
    issue 全覆盖，新增 8 用例），编辑类既有用例统一走 `client_edit` 夹具（已配置
    owner）；`test_owner_token.py` executor/reconciler 用例改为「绝不使用 owner token」
    （bot 身份固定，全局 401 回退 remote）；后端 1110 + 前端 577 全量测试通过。



### Fixed

- **dsh 引擎（deepseek-harness SDK）提示词未持久化、聊天记录不显示提示词（issue #146）**：
  需求「使用deepseek harness sdk作为执行引擎的时候，提示词未持久化，聊天记录中也没有显示
  提示词」。根因：`GET /api/tasks/{id}/execution` 的 `prompt`（「查看提示词」按钮数据源，
  issue #90）与 `transcript`（聊天记录，issue #20）此前只从 claude 会话 jsonl
  （`claude_session_id` 定位 `~/.claude/projects/*/<sid>.jsonl`）解析；dsh 引擎会话 id 落库在
  `dsh_session_id`、渲染后的提示词无处落库，SDK 会话文件又是 runtime 内部格式无法像 jsonl
  那样解析——dsh 任务详情页「查看提示词」固定显示「提示词未持久化，见执行日志」、聊天记录
  显示「暂无聊天记录（会话尚未开始或会话文件不可读）」。修复：参照 hermes 引擎
  `hermes_history` 落库模式，新增任务字段 `dsh_transcript`，由执行侧把提示词与消息落库，
  execution 接口读取返回。
  实现：
  - 后端 `database.py`：tasks 表新增 `dsh_transcript TEXT` 列（迁移链 v8→v9，旧库自动
    ALTER 补列），`_TASK_FIELDS` 白名单加入 `dsh_transcript`；
  - 后端 `executor.py`：`_run_dsh_once` 在 runner 启动前先把渲染后的完整提示词落库
    （运行中「查看提示词」即可用，不等执行结束）；`_on_line` 回调从事件行累积聊天消息
    ——`stream_delta` 流式文本收口为 `assistant` 消息（连续增量合并为一条回复，与 claude
    jsonl 写完完整行才落盘一致）、`tool_start` 转 `tool` 消息（含工具名与 input）、
    `thinking`/`status`/`raw` 不进聊天记录（事件流 SSE 已实时呈现，与 claude transcript
    只保留 text 对齐）；每次事件行后落库（运行中实时可见），停止/超时/正常结束均收口
    落库；新增 `_persist_dsh_transcript`（消息超 `_TRANSCRIPT_MAX_MESSAGES` 条时保留首条
    提示词与最近 N-1 条并置 truncated，与 parse_transcript 截断语义一致，落库失败不阻塞
    任务收尾）与 `_dsh_resume_messages`（断点续跑保留上次会话历史，追加本次恢复引导语为
    新 user 消息，与 SDK 会话内的真实输入一致）；
  - 后端 `api/tasks.py`：`task_execution` 的 `session_id` 回退取 `dsh_session_id`；
    `claude_session_id` 缺失时从 `dsh_transcript` 读取 `prompt` / `transcript` /
    `transcript_truncated`（非 JSON 脏数据容错返回空，不 500）；
  - 前端零改动：`TaskDetail.jsx` 的「查看提示词」与「聊天记录」消费的
    `execution` 接口字段契约不变；
  - **测试**：后端 `test_executor_dsh.py` 新增 `TestDshTranscript` 8 用例（fresh 落库
    提示词与 user/assistant 消息、流式增量合并、tool_start 工具消息、thinking/status
    不入列表、停止路径收口落库、断点续跑追加历史、runner 启动前提示词已落库、垃圾输出
    容错）；`test_api_tasks.py` 新增 3 用例（dsh_transcript 返回 prompt/transcript/
    session_id、非 JSON 脏数据空列表、truncated 标记透传）；`test_database_migrate.py`
    新增 3 用例（旧库迁移补列、新库建表含列、set_task_status 白名单写入）；同步更新
    `test_user_version_marker`（v8→v9）与 inspirations 迁移断言；后端 1294 全量测试通过（pytest 全量收集 1294 项全部通过）。

- **配置 Owner GitLab Token 后概览页评论/回复仍报「owner token 失效（403）」（issue #133）**：
  需求「我配置了gitlab owner token，但是回复评论、添加评论的时候报错，概览页 issue 编辑
  owner token 失效（403）：请在设置页更新 Owner GitLab Token 后重试」。诊断（部署日志
  实测复现）：设置页保存 owner token 时**不做任何校验**，用户误将只勾了 `read_api` 等
  只读 scope 的 PAT 保存进 config.yaml；GitLab REST 写操作（添加/回复评论、添加/关闭
  issue、编辑标签）要求 token 具备 `api` scope，只读 token 提交写操作返回 403，响应体
  `{"error":"insufficient_scope","error_description":"The request requires higher
  privileges than provided by the access token."}`——用户反复重新保存同一只读 token，
  概览页编辑持续 403 且错误提示笼统，无法定位根因。修复：
  - 后端 `api/settings.py`：PUT /api/settings 保存**真实** owner token（掩码/空串 =
    保持现有，跳过）前调用 GitLab `/personal_access_tokens/self` 校验有效性 + scopes：
    401 → 「token 无效或已过期」400 拒绝；403 → 「缺少 api scope」400 拒绝；404（旧版
    GitLab < 15.7 无 self 端点）→ 降级只校验 token 有效性；scopes 不含 `api` → 400
    拒绝并列出当前 scopes、明确指引重新生成勾选 api；校验失败一律**不落盘**；
  - 后端 `gitlab_client.py`：新增 `get_personal_access_token_self()`（self 端点封装）；
  - 后端 `api/issues.py`：`_issue_edit_call` 对 owner 403 且 GitLab 返回
    `insufficient_scope` 的响应直接提示「token 缺少 api scope（只读 scope 无法写评论/
    编辑 issue），请在设置页重新保存勾选了 api scope 的 Owner GitLab Token」，其余
    401/403 保留原通用提示；
  - 文档：`docs/GitLab-Owner-Token-申请教程.md` 安全提示补充「保存时校验」，FAQ 新增
    「缺少 api scope / 403 权限不足」诊断条目；
  - **测试**：后端 `test_api_settings.py` 新增 `TestOwnerTokenSaveValidation` 5 用例
    （缺 api scope 拒绝且不落盘、有效 token 正常保存、401 无效拒绝、掩码/空串保持
    现有跳过校验、旧版 GitLab 降级放行）；`test_api_issues.py` 新增 2 用例
    （insufficient_scope 403 提示 api scope、通用 403 保留原提示）；`test_owner_token.py`
    既有保存用例补校验桩；后端 1117 全量测试通过。


- **dsh 引擎事件流极其冗长：一句话拆成好多个单独字（issue #122）**：
  需求「使用 deepseek harness sdk 执行引擎时，事件流会变得极其冗长，
  因为一句话会拆成好多个单独字」。诊断：dsh 引擎（deepseek-harness
  SDK）的 `assistant/chunk` 按 token/字粒度回调 `text-delta` /
  `reasoning-delta`，每个增量（中文场景往往是一个字）都被
  `DshRunner._on_notification` 转成一条独立 `stream_delta` / `thinking`
  事件行——一句话被拆成几十上百行，SSE 事件流、事件总线（队列上限
  1000，满丢最旧）与任务日志文件同步爆炸式增长，任务执行页逐字刷屏。
  修复（`dsh_runner.py`，仅 dsh 引擎路径）：
  - 新增 `_DeltaCoalescer` 增量合并缓冲：连续同类型增量（文本/思考）
    先拼进缓冲，遇异类事件（工具调用/状态/回合结束等）、类型切换或
    超过刷新间隔（0.5s，保持 UI 实时感）时一次性冲刷为一条事件行；
    worker 结束产结果行前冲刷尾部缓冲，增量文本不丢失；
  - 事件行协议不变（`stream_delta` / `thinking` 语义与字段一致），
    executor 结果判定（`_dsh_result`）、SSE 解析（
    `parse_hermes_event_line`）与日志回放零改动；结果行仍是输出最后
    一行（issue #119 的多行解析不受影响）。
  - **测试**：`test_dsh_runner.py` 新增 6 个用例——逐字 text-delta
    合并为一条完整句、reasoning-delta 合并、类型切换保序冲刷、非增量
    行先冲刷再直发（顺序保持）、结果行前尾部冲刷（文本不丢）、20 字
    增量事件行总量降至 2 行（修复前 20 行逐字碎行）；后端 1040 +
    前端 529 全量测试通过。


- **概览页 issue 右边栏「执行引擎」全部误显当前全局引擎（issue #120）**：
  需求「issue 现在显示的执行引擎全都是 deepseek harness sdk，但是一开始
  的 issue 都是 claude code 为执行引擎」。诊断：`IssueDrawer` 的「执行引擎」
  行（issue #118 引入）读取的是全局配置 `worker.engine`（issue #113），只
  表示「新领取任务用什么引擎」，不代表该 issue 实际由哪个引擎处理——全局
  引擎从 claude 切到 dsh 后，所有 issue（含历史由 Claude Code CLI 处理的）
  都显示 deepseek-harness SDK。修复：
  - **执行引擎按任务落库**（database v7 迁移）：`tasks` 表新增 `engine`
    列，`executor.run_task` 执行时把本次实际引擎写入任务；迁移自动回填
    存量任务——按断点续跑会话字段推断历史引擎（`dsh_session_id` →
    dsh，`hermes_history` → hermes，`claude_session_id` → claude）；
  - **detail 接口返回该 issue 实际引擎**：`GET /api/issues/{project_id}/
    {iid}/detail` 新增 `engine` 字段（回退链：任务落库 engine > 会话字段
    推断 > 全局 `worker.engine`）；`GET /api/tasks` 列表/详情同步透出
    `engine`；
  - **前端改为按 issue 展示**：`IssueDrawer` 不再单独拉取 `/api/settings`
    全局配置，改为从 detail 响应读取 `d.engine` 归一展示（拉取失败仍显示
    「—」兜底）。
  - **测试**：后端新增数据库迁移回填、detail 引擎回退链、tasks 透出、
    executor 落库共 13 个新用例；前端重写抽屉引擎用例 7 个（detail
    返回 dsh / 空值回退 claude / 失败「—」/ 未知原样 + 源码不再依赖
    /api/settings）；后端 1034 + 前端 516 全量测试通过。

- **dsh 引擎任务重复开发直至失败（任务 #198 #199 根因，issue #119）**：
  需求「使用 deepseek harness SDK 作为执行引擎开发的时候，为什么会重复
  开发任务，直至显示失败，但是 claude code 作为执行引擎就不会」。诊断
  （任务 #198 #199 日志）：dsh 引擎多次尝试均 `exit 0`、输出结果行
  `finish_reason: completed` 且 final_response 非空（agent 实际已完成
  开发），却仍被判失败并重试，每次重试重新开发（约 4 分钟/次），重试
  耗尽后任务显示失败。根因：`_run_dsh_once` 收集 DshRunner 的
  `on_line` 回调事件行（行尾无换行符）后以 `''.join(lines)` 拼接，
  `output` 整串无换行分隔——下游 `_last_json_object` 按行扫描只能
  `raw_decode` 出**首个**事件对象（如 `{"event": "raw", ...}`），
  拿不到末尾结果行的 `finish_reason` → `_dsh_result` 误判 `failed`
  → 触发重试；`_persist_dsh_session_id` 同样解析不到 `session_id`
  → 断点续跑失效 → 每次重试都是全新会话（重复开发任务）。claude
  （stream-json 逐行）与 hermes（NDJSON）输出天然换行分隔，故不受
  影响。修复（executor `_run_dsh_once`）：
  - 事件行拼接改为 `'\n'.join(lines)`（与日志落盘 `line + "\n"`
    一致），结果行可被 `_dsh_result` / `_persist_dsh_session_id` /
    `_extract_error` 正确解析：成功判定恢复、会话 id 正常落库、
    断点续跑生效，不再重复开发。
  - **测试**：`test_executor_dsh.py` 新增 1 用例（多行事件 + 结果行
    输出：`_dsh_result == "success"` 且 `dsh_session_id` 落库，
    修复前该用例复现 `failed` 误判）；后端 1021 + 前端 516 全量
    测试通过。

- **dsh 引擎任务全部运行失败（任务 #194 #195 根因，issue #115）**：
  需求「任务 #194 #195 使用 deepseek 执行引擎都运行失败了，请诊断
  原因并修复 bug」。诊断：任务切换 `worker.engine: dsh` 后执行，
  dsh runtime 启动正常、prompt 正常发出，但 DeepSeek API 因无 Key
  拒绝（401 AUTH）→ `turn/end` reason.kind=error → 判定失败、3 次
  重试耗尽。Key 缺失链：config.yaml 无 dsh 段（dsh.api_key 空）、
  部署机环境无 `DEEPSEEK_API_KEY`（.env 未配），而用户实际把
  DeepSeek key 配在设置页「AI 供应商」（`ai_providers`），该配置
  此前无任何消费者——用户在平台上配过 key 却完全不生效。修复：
  - **凭据解析链**（executor 新增 `_dsh_credentials`）：dsh 段显式
    配置 > `ai_providers` 中 `provider=deepseek` 且 enabled 的项 >
    环境变量 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`（SDK 默认读取，
    botler 不覆盖）；`_run_dsh_once` 按解析结果传 runner；
  - **失败诊断透传**（dsh_runner）：`turn/end` 非 completed 时把
    reason.error/failure.message 拼进状态行；`assistant/chunk` 的
    finish/error 块（LLM 失败细节所在）透传为「模型调用失败: …」
    状态行——此前两者均被丢弃，任务日志与失败详情只有「回合结束:
    error」，无法诊断 401 等具体原因；
  - **验证**：本地以生产 config 回退解析出 key 后真实跑 SDK 会话
    `finish_reason=completed`；假 key 场景输出含完整 AUTH 401 文本。
  - **测试**：`test_executor_dsh.py` 新增 8 用例（显式优先 / deepseek
    项回退 / 缺项互填 / disabled·非 deepseek 跳过 / 无源为 None /
    runner 透传与 None 透传），`test_dsh_runner.py` 新增 6 用例
    （turn/end error·failure 透传 / max-tokens 不附加 / assistant
    chunk finish error 转状态行 / 非 error 仍 raw）；前端 493 +
    后端 989 全量测试通过。

- **dsh 引擎事件透传对真实 SDK 事件结构覆盖不全（issue #115 第二轮）**：
  上轮修复按猜测的事件结构透传失败诊断，本轮在部署机以真实
  deepseek-harness SDK（0.1.0rc6）实测发现：`assistant/message` 的
  思考块类型是 `reasoning`（字段 `text`）而非 `thinking`——思考内容
  从未出现在任务日志 / SSE；`assistant/chunk` 承载流式增量
  （`block-start` / `reasoning-delta` / `text-delta` / `block-end` /
  `usage` / `finish`），此前仅透传 finish/error 块，`reasoning-delta`
  与 `text-delta` 全部落 raw——任务执行页在回合结束前无任何实时
  流式输出，失败诊断只能依赖回合末的贫瘠摘要。修复（dsh_runner）：
  - `assistant/message` 的 `reasoning` 块 → `thinking` 事件行
    （与既有 `thinking` 块同语义，前端折叠展示）；
  - `assistant/chunk` 的 `text-delta` → `stream_delta` 事件行、
    `reasoning-delta` → `thinking` 事件行（实时流式可见）；
  - 其余簿记块（block-start/block-end/usage/finish 正常结束）与
    未识别类型仍落 raw 供日志诊断，行为不变。
  - **验证**：部署机真实 SDK 端到端跑会话（生产 key 回退解析 +
    修复后代码）：输出含 19 个 thinking 增量与流式 stream_delta，
    `finish_reason=completed`；假 key 场景「模型调用失败: 401 AUTH」
    透传依然生效。
  - **测试**：`test_dsh_runner.py` 新增 3 用例（reasoning 块 →
    thinking / text-delta → stream_delta / reasoning-delta →
    thinking），前端 493 + 后端 992 全量测试通过。

### Added

- **概览页 issue 右边栏新增「添加评论」与「回复评论」（issue #125）**：
  需求「在概览页面弹出的issue的右边栏，实现可以添加评论、以及回复
  评论」。实现：
  - **后端**：新增 `POST /api/issues/{project_id}/{iid}/comments`
    （添加评论，复用 `GitLabClient.add_comment` 的 notes API）与
    `POST /api/issues/{project_id}/{iid}/comments/{note_id}/reply`
    （回复评论）两个端点；回复语义经实测确认——GitLab 的 notes API
    响应不含 `discussion_id`，回复需先 GET discussions 解析目标评论
    所在线程 id，再 POST discussions 带 `in_reply_to_discussion_id`
    追加（`GitLabClient.reply_to_note` 新增方法，note 不存在抛 404）；
    正文去空白后为空 → 400（GitLab 对空正文同样拒绝，提前校验）；
    成功后清空概览缓存（user_notes_count/updated_at 已变化）并返回
    精简评论对象（与 detail 的 notes 条目同结构）；
  - **前端**（`IssueDrawer`）：评论区块底部新增评论输入区（占位
    「写下你的评论…」，空内容/提交中禁用按钮），每条评论下「回复」
    按钮展开内联回复框（占位「回复 @作者…」，支持取消）；添加/回复
    成功后本地即时追加新评论、清空输入并叠加评论计数（快照 + 本次
    新增），无需重新拉取详情；失败保留输入内容可重试；详情加载中/
    失败时不显示输入区（避免对不可知目标发言）；
  - **测试**：后端新增 21 用例（评论/回复端点 14：成功/空正文 400/
    仓库不存在/未启用 404/GitLab 404/5xx→502/网络错误→502/清缓存，
    以及 `reply_to_note` 单元 7：线程解析/404/异常数据跳过/响应容错）；
    前端新增 9 用例（渲染态、添加评论成功/空输入/失败保留、回复
    成功/取消/失败保留、加载失败隐藏输入区）；后端 1077 + 前端 563
    全量测试通过。

- **使用 apple-design skill 优化全部页面（issue #124）**：
  需求「使用apple-design skill优化所有页面，禁止少优化一个页面」。按
  apple-design skill（Apple 流体交互/材质与深度/响应/排版/可及性原则）
  对全部 8 个页面（概览/仓库/任务/任务详情/模版/标记库/设置/登录）落地
  统一优化，全部收敛在共享设计系统 `styles.css`，无页面遗漏：
  - **材质与深度（Materials & depth）**：顶导航由实心不透明条改为半透明
    毛玻璃悬浮层——`@supports (backdrop-filter)` 下 `blur(20px)
    saturate(180%)` + 半透明 `--nav-bg-glass`（浅色 rgba(255,255,255,.78)
    / 深色 rgba(10,10,10,.72)），内容从其下方滚动；不支持时回退纯色
    `--nav-bg` 兜底；`prefers-reduced-transparency: reduce` 时回退纯色
    无模糊；`prefers-contrast: more` 时导航/卡片补明确边框、次级文字加深；
  - **动效（Motion）**：模态/抽屉/遮罩「材质化入场」——新增
    `@keyframes surface-in`（scale .97 + translateY 8px + 淡入，模拟材质
    到达而非纯 opacity 淡入）/ `drawer-in`（从右滑入，空间一致性）/
    `overlay-in`（遮罩先淡入）；`.modal`/`.login-card`/`.guide-content`
    用 `--dur`（200ms）+ `--ease-spring`，`.drawer` 用 240ms，全部落在
    150–300ms 区间且受既有 `prefers-reduced-motion` 全局降级保护；
    高频动画元素（spinner/阶段节点/模态/抽屉）补 `will-change` 合成层
    提示（帧级流畅，只动 transform/opacity）；
  - **响应（Response）**：按钮按下升级为 Apple 风格微缩放
    `translateY(1px) scale(0.98)`；issue-link / section-toggle /
    modal-close / folder-item / add-method / remote-option /
    label-choice 补齐 active 按下即时反馈；概览 issue 行、仓库行、标签行
    补 hover 过渡反馈（运行中高亮行 hover 保持蓝色弱底不被覆盖）；
    概览流水线/开放 issue 卡片 hover 轻抬升 + 阴影加深；
  - **排版（Typography）**：正文启用 `font-optical-sizing: auto`；
    新增字号相关字距 token `--tracking-display`（大标题负字距收紧）与
    `--tracking-caption`，h1 引用负字距；表格启用 `tabular-nums` 等宽
    数字，数字跳变不抖动；折叠标题（任务详情/模版页）hover 变主色平滑
    过渡；
  - **测试**：新增 `apple-design.test.mjs` 17 用例逐条验收（毛玻璃材质/
    token 双主题/减弱透明/增强对比度/三类入场动画时长区间/响应按下态/
    排版/页面覆盖清单 8 页无一遗漏），前端 551 + 后端 1056 全量测试
    通过。
  - **补充（第二轮，issue #124）**：逐页复查交互反馈覆盖后发现三处
    可交互元素反馈缺口，统一收敛共享设计系统补齐（无页面遗漏）：
    - 可关闭提示条 `.alert`（概览/任务/仓库/设置等页 onClick 点击关闭）
      补 hover 阴影加深 + active 按下微缩放，反馈平滑过渡
      （`--dur-fast` 150ms + `--ease-out`）；
    - 任务详情页「思考过程」折叠条 `summary` 补 hover 变主色（提示可展开）；
    - 标签多选胶囊 `.label-choice`（新增 issue 弹窗/右边栏标记编辑）补
      hover 微降透明度（与既有 active 0.8 呼应）。
    `apple-design.test.mjs` 同步新增 3 个用例验收，前端 554 全量测试通过。

- **设置页新增 dsh 引擎推理等级设置（issue #123）**：
  需求「deepseek harness sdk是可以设置推理等级的，在设置页面添加deepseek
  harness推理等级的设置」。deepseek-harness runtime 的 `llm-deepseek`
  adapter 支持 `reasoningEffort`（`off` / `high` / `max` 三档，SDK 默认
  `high`），但 SDK 的 `DeepSeekHarnessConfig` 未直接暴露该参数。实现：
  - **后端**：`Settings` 新增 `dsh.reasoning_effort`（空 = 不设置），
    GET/PUT `/api/settings` 透出与校验（白名单 `off` / `high` / `max`，
    空串允许，非法值 400 提前拦截）；
  - **推理等级注入**（`dsh_runner.py`）：`DshRunner` 新增
    `reasoning_effort` 参数，非空时基于内置默认 Cordis 组合（或自定义
    `cordis` 文件）行级注入 `llm-deepseek` 条目的
    `config.reasoningEffort`（默认组合含 `!!js` 标签无法用 PyYAML 加载，
    故采用行级文本编辑），按内容哈希缓存到系统临时目录
    `botler-dsh-cordis/`，executor 执行时把 `cfg.dsh_reasoning_effort`
    透传给 `DshRunner`；自定义 cordis 缺失 / 无 `llm-deepseek` 条目时
    任务以可读错误失败，不静默忽略；
  - **前端**：设置页新增「dsh 引擎」卡片，推理等级下拉
    （默认 / off / high / max，含语义说明），跟随全局「保存」提交；
  - **文档**：`config.example.yaml`、`README.md` 设置表、
    `docs/dsh-engine-deployment.md` 同步补充配置说明。
  - **测试**：后端新增 cordis 注入（默认组合注入 / 已有 config 替换 /
    已有 config 首键插入 / 缺条目报错 / 非法值报错）、`resolve_dsh_cordis`
    派生缓存（空 effort 原样返回 / 自定义与内置基底 / 缺失文件报错）、
    settings API（GET 默认值 / PUT 持久化 / 非法值 400）、DshRunner 透传
    派生 cordis；前端新增设置页 dsh 卡片源码 + 渲染/保存交互用例；
    后端与前端全量测试通过。

- **新增 UI 优化参考文档——同类开源项目调研与设计借鉴（issue #121）**：
  issue「界面优化讨论，现在界面布局和样式都不够美观，帮助找一些类似的
  开源项目，可以参考ui设计」为讨论型需求，本期不实施界面改造，调研结论
  落库 `docs/ui-design-reference.md`：
  - **现状分析**：Botler 已基于 Vercel Geist + Apple HIG（issue #110/#111）
    完成设计令牌与基础规范；对照同类项目后短板集中在「关键状态可视化 /
    信息密度 / 微交互 / 品牌感」四方面，基底无需推翻；
  - **项目调研**：按「Git/DevOps 平台、自动化 Bot 平台、监控仪表盘、
    后台管理、组件库/设计系统」五类梳理 28 个同类开源项目（Star/License/
    主页经 GitHub/Codeberg/GitLab API 核实，截至 2026-08-16），逐项给出
    可借鉴点；优先参考 GitLab Pajamas > Gitea > Uptime Kuma / Grafana >
    Plane / Huly > shadcn/ui / Ant Design；
  - **分页建议**：概览页关键数字条与状态可视化、任务页过滤/排序/批量
    操作、空状态/骨架屏、登录页品牌化、暗色令牌调校、DropdownMenu 等
    组件补充共 6 组改进建议；
  - **落地路线**：建议按模块拆分为后续独立 issue 逐项实施，每项遵守
    `docs/design-system.md` 规范并补前端测试。
  - 纯文档变更（README 同步补充文档入口），无代码/测试改动；本地全量
    测试通过（后端 1034 + 前端 529）。


- **概览页开放 Issue 板块展示正在运行任务、删除独立任务板块（issue #114）**：
  需求「概览页面删除，正在运行任务组件，现在开放issue组件页面也显示正在
  运行的任务」。实现：
  - **删除独立任务板块**：概览页移除「正在执行的任务」板块（任务卡片
    网格），页面仅保留「开放 Issue」与「CI/CD 流水线」两板块；
  - **任务信息整合进开放 issue 列表**：正在运行任务的信息（任务状态
    徽章 / 执行引擎 / 实时输出日志）随「开放 Issue」板块 running 组
    的对应 issue 项内展示——按 repo_id+issue_iid 匹配（与 #99/#101
    高亮置顶同规则），新增 `tasksForIssue` / `engineLabel` 纯函数，
    同一 issue 的多条任务记录逐一渲染任务块；任务轮询与 SSE 事件流
    数据流保持不变，实时输出自动滚动同步迁入 issue 项内日志元素
    （`.issue-task-log`），任务轮询错误并入开放 Issue 板块错误横幅；
  - **样式**：styles.css 删除任务板块样式（overview-grid / overview-
    card / tasks-section 等），新增 issue-row / issue-task 任务块样式
    （左侧竖条与运行中高亮呼应）；
  - **测试**：新增 overview-issue-task.test.mjs 14 用例（板块删除
    源码断言 / tasksForIssue 边界 / 任务块渲染 / SSE 实时输出 / 多任务
    / 跨仓库不误显 / 样式断言），适配 overview-page、overview-section-
    order、hig-layout、overview-columns-unified、apple-hig 5 个既有
    测试（三板块断言改为两板块）；后端 1034 + 前端 529 全量测试通过。

- **概览页 issue 右边栏展示任务执行引擎类型（issue #118）**：
  需求「概览页面弹出的issue右边栏，显示任务执行引擎的类型」——概览页
  打开 issue 详情右边栏时，KV 表格新增「执行引擎」行，展示当前任务
  执行引擎类型（`worker.engine`：claude / hermes / dsh，issue #113
  引入的全局配置，对新领取任务生效）：
  - **数据源**：抽屉打开时从 `GET /api/settings` 读取
    `worker.engine`，复用现有接口无需新增后端改动；展示文案与设置页
    「任务调度」卡片下拉选项一致（Claude Code CLI / hermes-agent /
    deepseek-harness SDK）；
  - **前端**：IssueDrawer 新增 `ENGINE_META` 映射与 `engineDisplay`
    纯函数——null=加载中、空值/纯空白回退默认 claude、未知值原样
    展示兜底不崩溃；拉取失败显示「—」不阻塞其余信息展示；
  - **测试**：前端新增 `overview-issue-drawer-engine.test.mjs` 7 用例
    （源码契约 / ENGINE_META 三引擎映射 / engineDisplay 边界 / 渲染
    回显 dsh / 未返回回退 claude / 失败「—」/ 未知值兜底），并适配
    `overview-issue-notes.test.mjs` 4 用例（新增 /api/settings 调用后
    detail 路径计数与路由）；前端 516 全量测试通过。

- **概览页 issue 右边栏新增失败任务「重试」按钮（issue #117）**：
  需求「概览页面的issue详情页面右边栏，如果是失败任务，增加一个重试
  按钮，点击之后重新执行任务」——概览页开放 issue 列表中带 `bot-failed`
  标签（且无 `bot-done`，与列表分组判定一致）的失败任务，打开详情
  右边栏后在右上角显示「重试」按钮，点击二次确认后重新执行该 issue
  对应的任务：
  - **后端**：新增 `POST /api/issues/{project_id}/{iid}/retry`——按
    project_id+iid 定位该 issue 的任务：已有活跃任务（排队/执行/重试
    中）→ 409 防重复执行；最近任务为 failed/interrupted → 复用任务
    记录重试（重置 queued 重新入队，与任务页手动重试一致，保留断点
    续跑会话、清除历史停止请求残留）；无任务记录或最近任务已终态成功
    → 新建任务入队（triggered_by=manual，记录 issue 标签/更新时间供
    调度器排序）；`database.find_latest_task` 按 id 倒序取最近一条任务；
    成功后清空概览缓存，前端刷新列表即可看到 issue 进入「运行中」组；
  - **前端**：IssueDrawer 右上角新增「重试」按钮（btn-primary，失败
    任务 + 非运行中 + 本次会话未重试才显示）——二次确认（复用自定义
    对话框）→ 调 issue 级重试接口；成功后按钮消失、显示「任务 #N 已
    重新入队」提示并通知父组件刷新开放列表；失败显示错误按钮保留可重
    试；请求中按钮禁用防重复点击；`isFailedTask` 纯函数判定（bot-done
    优先级高于 bot-failed）；Overview 将 running 标记传入抽屉，任务
    运行中（重试已在进行）时不显示重试按钮；
  - **测试**：后端新增 14 用例（issue 级重试 API 11 用例：复用失败/
    中断任务、新建任务、成功任务兜底新建、活跃冲突 409、仓库不存在/
    未启用 404、GitLab 404/5xx/网络错误映射、清空缓存；`find_latest_task`
    db 层 3 用例）；前端新增 overview-issue-drawer-retry.test.mjs 10
    用例（isFailedTask 纯函数边界、按钮条件渲染、确认/取消、成功/失败、
    请求中禁用、Overview 传参联动）。


  需求「将中断恢复的提示词也做成类似全局模板一样，可以由用户编辑
  修改的提示词」——中断恢复引导语此前硬编码在 executor.py，用户
  无法修改，现与全局默认模版（templates.default）同机制开放编辑：
  - **后端**：内置默认 `DEFAULT_RESUME_PROMPT` 迁入 config.py（与
    DEFAULT_TEMPLATE 并列），新增 `templates.resume` 配置键——
    缺失/空串归一为内置默认（中断恢复必须有引导语，不允许空模版）；
    `GET /api/settings` 的 templates 段返回 `resume`，`PUT` 支持
    写入（非字符串 400 拒绝，空白 = 移除自定义键恢复内置默认）；
    executor._resume_prompt 改从 config 读取（claude/hermes/dsh
    三引擎断点续跑统一入口，自定义对三引擎同时生效）；
  - **政策修正（issue #109）**：旧 RESUME_PROMPT 含「用 GitLab API
    关闭 issue」指令，与「Agent 永不主动关闭 Issue」矛盾，迁入时
    同步修正为「不要关闭该 issue——关闭动作留给用户确认后手动执行」；
  - **前端**：模版页新增「中断恢复模版」视图（与全局默认/仓库级
    并列切换），复用现有编辑器与占位符表（全部 11 个占位符可用），
    界面注明「留空保存即恢复内置默认」；
  - **测试**：后端新增 14 用例（settings API 读写/清空恢复默认/
    非字符串 400/部分更新不影响 default；config 加载兜底与空串归一/
    内置默认不含关闭指令/占位符齐备/update_resume_template 写盘清键；
    executor 渲染默认/自定义/清空回退），前端新增
    `templates-resume-page.test.mjs` 6 用例（加载读取/视图切换/保存
    API/恢复默认提示/后端 GET·PUT 契约）；前端 499 + 后端 1006
    全量测试通过。

- **设置页新增「任务执行引擎」设置项（issue #113）**：
  需求「设置页面增加一个设置项，用来切换后端用来编写代码的agent」——
  `worker.engine` 引擎切换逻辑（issue #47/#84）此前只能在 config.yaml
  手工修改，本次起设置页可直接切换，保存后对新领取的任务生效：
  - **后端**：`GET /api/settings` 的 worker 段返回 `engine`；`PUT`
    校验引擎名白名单（claude / hermes / dsh，strip + 小写归一，
    与 executor._engine 合法集合一致），非法值 400 拒绝不落盘；
  - **前端**：「任务调度」卡片新增引擎下拉设置项（三引擎 + 默认
    claude 回退），跟随全局「保存」写回 config.yaml；
  - **测试**：后端新增 9 用例（默认值 / 三引擎持久化 / 大小写归一 /
    非法值·非字符串·空串拒绝 / 部分更新不影响其他字段），前端新增
    `settings-engine.test.mjs` 5 用例（下拉渲染 / 回显与回退 / 保存
    提交）；前端 493 + 后端 976 全量测试通过。

- **pm2 部署自动安装 deepseek-harness SDK（issue #112 跟进）**：
  issue #112 首轮仅覆盖 Docker 镜像内置 SDK，用户反馈 pm2 部署实例
  仍缺该依赖，本次补齐 pm2 部署形态：
  - **deploy/install-dsh-sdk.sh**：新增一键安装脚本（单一事实来源）
    ——阿里镜像 + 显式全版本号（`DSH_INDEX_URL` 环境变量可覆盖）、
    幂等（已装目标版本跳过）、安装后 import 校验 fail fast、优先
    uv pip（CI venv 无 pip seed）回退 venv 内 pip；
  - **.gitlab-ci.yml**：`deploy_to_code01`（pm2 部署）主依赖安装后
    自动调用脚本装入 `backend/.venv`，失败即部署失败；全局变量新增
    `DSH_INDEX_URL`（默认阿里镜像，UI 可覆盖）；
  - **文档同步**：`docs/dsh-engine-deployment.md` 第 2.2 节改为
    pm2 CI 部署自动安装 + 手动部署一键脚本，故障排查表、
    `dsh_runner.py` 安装指引与 README 部署步骤同步（README 的
    「CI/CD 自动部署」过时描述一并修正为 pm2）；
  - **测试**：新增 `test_deploy_dsh_sdk.py` 14 用例静态校验部署产物
    防回退，适配 `test_dockerfile_dsh.py` 中「pm2 保留手动安装」用例。

- **Docker 部署内置 deepseek-harness SDK（issue #112）**：
  需求「后端部署增加安装deepseke harness依赖」——dsh 引擎 SDK
  （`deepseek-harness-sdk==0.1.0rc6`）原为可选依赖（issue #84：镜像
  不含、部署机手动安装），本次起 Docker 镜像构建期自动安装，容器
  内 `worker.engine: dsh` 开箱即用：
  - **Dockerfile**：runtime 阶段新增 SDK 安装 RUN——阿里镜像（清华
    源未同步 rc 预发布版）+ 显式全版本号；镜像源可用 `DSH_INDEX_URL`
    build arg 覆盖（内网代理场景）；构建期 import 校验，装不上直接
    构建失败（fail fast）；
  - **requirements.txt** 保持不声明 SDK：主依赖继续走清华源，避免
    rc 版解析失败阻塞全部依赖安装；
  - **deploy/verify-docker.sh**：--full 冒烟新增第 12 项校验——容器
    内 `/opt/venv` 可 `from deepseek_harness import DeepSeekHarness`；
  - **文档同步**：`docs/dsh-engine-deployment.md` 第 2 节按部署形态
    拆分（Docker 已内置无需手动 / pm2·systemd 手动安装），故障排查
    表与 README Docker 部署章节、`dsh_runner.py` 安装指引注释同步；
  - **测试**：新增 `test_dockerfile_dsh.py` 10 用例，静态校验部署
    产物防回退（版本号锁定 / 阿里镜像 / build arg 可覆盖 / venv
    路径 / 构建期 import 校验 / 冒烟脚本 / 文档同步）。

- **按 Apple HIG 布局原则优化全部页面（issue #111）**：
  需求「优化页面，根据下面的设计原则，帮我优化项目中的每一个页面」
  （Apple HIG — 布局：安全区 / 布局指南 / 分组 / 视觉层次 / 适配性），
  依据 Issue 正文「给 Agent 的实现检查清单」逐项落地：
  - **8pt 网格间距体系（检查清单 #2）**：新增 `--gutter` 内容外边距
    token（桌面 20px，对应 HIG 数值速查「大屏 20pt」；≤640px 窄视口
    回落 16px，对应「iOS 默认 16pt」）；`.content`/`.topnav`/`.card`
    统一引用；按钮（7px 14px→8px 16px）、提示条、导航链接、统计条、
    聊天消息、评论卡片、弹窗/抽屉/目录对话框、分页、标记库、仓库
    列表等全部布局间距（padding/margin/gap）由非网格裸值（20/14/10/
    7/6px 等）统一改用 `--space-*` token，全部落在 4px 网格。
  - **视觉层次统一（检查清单 #4）**：概览页「正在执行的任务」板块
    补齐 section 容器 + h2 标题（`.tasks-section`），与「开放 Issue」
    「CI/CD 流水线」两板块结构/字号/间距节奏完全对齐；三板块间距
    统一 `--space-5`。
  - **布局响应式补全（检查清单 #5）**：顶导航窄视口可横向滚动
    （导航项 `flex-shrink: 0` 不压缩、隐藏滚动条，不再溢出截断）；
    设置页/详情页 kv 表格窄视口（≤640px）标签列 180px→120px 降级，
    输入控件不被挤压；内容区/卡片外边距随视口回落。
  - **导航浮于内容之上（检查清单 #7）**：`.topnav` sticky 断言防回退，
    内容区底部 60px 留白保证滚动内容延伸到底部。
  - **全宽按钮检查（检查清单 #6）**：断言 `.btn` 系列无 `width: 100%`
    规则（按钮遵循外边距、从一侧嵌入），防回退。
  - 测试：前端新增 `hig-layout.test.mjs` 9 用例（--gutter token 与窄屏
    回落、内容容器 token 引用、控件间距 4px 网格断言、三板块 h2 层级
    与顺序渲染、顶导航横向滚动/sticky、kv 表格窄屏降级、无全宽按钮、
    底部留白），先复现失败再修复通过；适配 2 个既有测试的 CSS 解析
    （支持 var() token 引用，间距语义值不变）；全量测试
    （前端 488 + 后端 942）通过。

- **按 Apple HIG 设计原则重新优化全部页面（issue #110）**：
  需求「根据下面的苹果的ui设计原则，帮我重新优化项目中的每一个页面」——
  依据 Apple Human Interface Guidelines 八原则（目标感/能动性/责任感/
  熟悉感/灵活/简洁/匠心/愉悦感）与验收检查清单逐项落地：
  - **交互状态补全（熟悉感/愉悦感）**：`.btn` 系列新增 `:active`
    按下反馈微交互（主/危险变体各自加深），导航链接 hover/active
    双态；transition 时长由 0.1s 统一为 150ms ease-out 缓动
    （HIG 匠心 150–300ms 建议区间）。
  - **design token 统一（匠心）**：新增动效 token（`--dur`/
    `--dur-fast`/`--ease-out`）、4px/8px 网格间距 token（`--space-1~6`）、
    主色实底拆分（`--primary-strong`/`--on-primary`）；散落硬编码
    中性色（rgba 遮罩/发丝线/弱底、阶段图节点色）统一 token 化。
  - **对比度达 WCAG AA（灵活）**：语义色加深（`--ok` #12823d、
    `--warn` #b45309、`--err` #d13438，原色白底对比度仅 3.2~3.9:1
    不达标），浅色/深色主题下语义色与 muted 文字对比度均 ≥ 4.5:1。
  - **跟随系统深色模式（灵活）**：新增 `prefers-color-scheme: dark`
    令牌翻转块（Geist Dark 基调），全部组件零改动自动适配。
  - **减弱动画支持（灵活）**：新增 `prefers-reduced-motion: reduce`
    降级规则，动画/过渡时长降为 0.01ms。
  - **空状态与加载态设计（匠心/愉悦感）**：新增 `.empty-state`
    （图标 + 文案）与 `.spinner`/`.loading-hint`/`.app-loading`
    加载态；概览页三板块（开放 Issue/运行任务/流水线）、任务表格、
    仓库列表、标记库、目录选择、App/任务详情/设置页加载态全部
    由裸文本升级为设计态（文案文本保持不变，既有测试兼容）。
  - **无障碍（灵活）**：键盘焦点可见性补全（`.section-toggle`/
    `.issue-link`/`.add-method`/`.remote-option`/`.label-choice`/
    `.folder-hidden-toggle`/`.modal-close` 等 focus-visible 焦点环）；
    全部弹窗/抽屉关闭按钮补充 `aria-label`；顶部导航补充
    `aria-label="主导航"`；触控设备（`pointer: coarse`）按钮最小
    触控目标 44px。
  - 测试：前端新增 `apple-hig.test.mjs` 15 用例（active 按下态、
    spinner/空态渲染、token 化断言、浅色/深色语义色对比度计算、
    reduced-motion、pointer: coarse、focus-visible 覆盖、aria-label、
    深色模式令牌翻转），先复现失败再修复通过；全量测试
    （前端 479 + 后端 942）通过。

- **概览页 issue 右边栏新增标记编辑功能（issue #108）**：
  需求「概览页面，点击issue弹出的右边栏，增加issue的标记编辑功能，
  可以删除和添加标记」——抽屉「标签」行由只读展示升级为可编辑：
  新增「编辑标记」按钮，点击进入编辑态加载项目标记池（checkbox
  多选、当前标记预勾选、池外标记（组标签/已从标记库删除）仍可取消
  勾选移除），保存时 diff 出 add/remove 一次提交，成功后抽屉标记
  即时更新并刷新概览列表；失败保留编辑态可重试、取消不调接口。
  - 后端：`api/issues.py` 新增 `GET /api/issues/{project_id}/labels`
    （项目标记池，复用 `_form_meta_labels` 颜色归一化）与
    `PUT /api/issues/{project_id}/{iid}/labels`（add/remove 一次提交，
    复用 `GitLabClient.add_labels`，标记名归一化去重、全空 400、
    成功后清概览缓存并返回更新后标记）；remove 只含当前实际存在的
    标记，规避 GitLab remove_labels 对不存在标记返回 404 的行为。
  - 前端：`IssueDrawer.jsx` 标签行新增编辑态（复用 label-picker/
    label-choice 多选样式与抽屉错误横幅/重试模式），新增
    `onLabelsUpdated` 回调；`Overview.jsx` 传入列表刷新回调；
    `styles.css` 新增 `.labels-edit`/`.labels-edit-btn` 样式。
  - 测试：后端 `test_api_issues.py` 新增 TestIssueLabels 18 用例
    （标记池获取/空池/仓库不存在/未启用/GitLab 错误/网络错误、
    更新成功/仅添加/仅移除/全空 400/归一化去重/缓存清空/issue 404/
    5xx/网络错误）；前端新增 `overview-issue-drawer-labels-edit.test.mjs`
    11 用例（编辑按钮显示条件、池加载与预勾选、add/remove 参数、
    成功更新与回调、无变更不调接口、失败重试、加载失败重试、空池、
    取消还原、防重复提交），先复现失败再修复通过。

- **弹窗全面改用自定义对话框，不再使用浏览器原生 alert/confirm（issue #105）**：
  用户反馈「不要使用 alert 来弹出通知，自定义一个对话框」——前端新增
  页面内自定义对话框体系，替换全部 10 处浏览器原生 `window.confirm` /
  `confirm` 弹窗调用（删除备份/恢复备份/上传恢复/删除标签/删除仓库/
  删除供应商/清空模版覆盖/关闭 issue/停止所有任务/重试任务），原生
  弹窗零残留。对话框为统一风格的自定义组件：遮罩 + 居中面板，沿用
  现有 Modal 的样式体系与交互约定（× 按钮/点击遮罩/Esc 键关闭），
  支持确认形态（取消+确定，danger 参数控制确定按钮 btn-danger 危险
  样式）与提示形态（单确定按钮），消息多行换行保留（pre-line），
  同一时刻只显示一个对话框、连续调用自动排队依次弹出。
  - 前端：新增 `dialog.js`（confirmDialog/alertDialog Promise 化
    接口 + 模块级队列 + DialogHost 订阅 + 测试注入 installAutoAnswer）
    与 `components/DialogHost.jsx`（挂在 App 根部）；`App.jsx` 挂载
    宿主；`styles.css` 新增 `.modal.dialog` 窄宽与 `.dialog-message`
    换行样式；7 个调用文件改为 `await confirmDialog({...})`。
  - 测试：新增 `tests/dialog.test.mjs` 18 用例（confirm/alert 形态
    各关闭路径、danger 样式、空消息/空标题边界、多行消息、排队、
    重复点击、无宿主兜底、注入应答、队列清理、调用点零残留源码
    断言）；适配 7 个既有测试文件（window.confirm mock → dialog.js
    autoAnswer 注入，点击后推进微任务链）；「刷新」「对账」低危操作
    断言升级为同时校验不存在 window.confirm 与 confirmDialog 防回归。

### Fixed

- **修复 graph2plan 任务 issue 被「自动关闭」的根因：GitLab autoclose 机制 + 三层防护（issue #109）**：
  用户反馈「graph2plan 的 issue 运行时 agent 总是自己 close issue，但提示词已标明
  严禁」——排查会话记录发现 agent **从未调用关闭 API**：graph2plan 提交信息写成
  `fix: #24 …`，命中 GitLab 实例开启的 `autoclose_referenced_issues` 默认关闭模式，
  推送默认主分支时 issue 被 GitLab 系统自动关闭（closed_by 为 project bot），
  用户侧即表现为「agent 自己 close issue」（agent 会话中自查发现误关后曾手动
  reopen + 说明评论）。三层防护：
  - 平台兜底（治本）：`executor.py` 新增 `_restore_autoclosed_issue()`，任务成功
    收尾时检测 issue 状态——closed 且 closed_by 是本项目 project bot（autoclose
    特征）→ 自动 reopen + 补说明评论 + warn 日志；人工关闭不干预；任意步骤
    失败仅记 warn 不阻塞任务成功。`gitlab_client.py` 新增 `reopen_issue()`
    （与 `close_issue` 对称的 `state_event=reopen`）。
  - 模板阻断（根因）：`config.example.yaml` 与内置兜底模板 `config.py
    DEFAULT_TEMPLATE` 增加提交信息规范——严禁 `fix: #N` / `fixes #N` /
    `closes #N` / `resolves #N` 等 autoclose 触发模式，issue 引用一律写全角
    括号 `（issue #NN）`；同时移除旧示例模板中「curl state_event=close 关闭
    issue」的指令（与「Agent 永不主动关闭 Issue」政策矛盾）。
  - 文档同步：`docs/labels.md` 新增「提交信息规范（防 GitLab autoclose 自动
    关闭）」章节。
  - 测试：`test_executor_autoclose.py` 新增 7 用例（autoclose 恢复/opened 不
    干预/人工关闭不干预/无 closed_by 容错/他项目 bot 不误判/查询失败与 reopen
    失败不阻塞收尾）；`test_config_template.py` 更新旧断言（模板不得含关闭
    指令、必须含 autoclose 禁用模式）；`test_gitlab_client.py` 新增
    TestReopenIssue 2 用例，先复现失败再修复通过。

- **概览页开放 Issue、正在执行任务、CI/CD 流水线三板块列数不统一（issue #107）**：
  用户反馈「开放issue、正在执行任务、ci/cd流水线的列数统一，以开放
  issue 为标准，先有些是三列，有些又是 4 列」——开放 Issue 板块
  （issue #96）已改为自适应网格（auto-fit + minmax(280px, 1fr)），
  宽屏下 4 列一行，而正在执行任务、CI/CD 流水线两个板块仍是固定
  3 列（repeat(3, 1fr)），同一页面列数错落不一致。
  - 前端：`styles.css` 中 `.overview-grid` 与 `.pipelines-list`
    改为 `repeat(auto-fit, minmax(280px, 1fr))`，与 `.issues-list`
    列数标准完全一致（最小列宽/网格间距一致，宽屏同为 4 列一行，
    窄视口同步降列回退不溢出），并同步更新注释说明。
  - 测试：新增 `tests/overview-columns-unified.test.mjs` 6 用例
    （三板块均 auto-fit 断言、防固定 3 列回退、minmax 最小列宽与
    间距三板块一致、多视口列数模拟一致+窄视口降列不溢出、任务/流水线
    多卡片渲染不丢卡），先复现失败（4 断言失败）再修复通过；
    `overview-page.test.mjs` 中编码旧固定 3 列行为的断言同步适配为
    auto-fit 标准。

- **「添加 issue」只输入标题时后端未在发送 GitLab API 时填充描述（issue #103，用户反馈修正）**：
  前一轮实现只在**前端**做标题→描述联动与提交兜底，后端创建
  issue 的 GitLab API 请求在描述为空时不发送 description 字段，
  用户反馈「没有实现」——要求后端在发送 API 请求时将标题内容填充
  到描述字段。修复：`GitLabClient.create_issue` 请求体 description
  兜底为 `description or title`（描述 None/空串时填充标题、非空
  （用户手写）保持原样、纯空白字符串的 strip 仍由 API 层负责），
  无论从哪个入口调用客户端创建的 issue 描述恒不为空。
  - 后端：`gitlab_client.py` create_issue 请求体 description 兜底
    填充标题（`{"title": title, "description": description or title}`）。
  - 测试：`test_gitlab_client.py` 新增 TestCreateIssue 5 用例
    （None 兜底/空串兜底/非空不覆盖/与标题相同不变/assignee 与
    labels 转发回归），先复现失败再修复通过。

- **数据备份卡片加载失败永久卡「加载中…」+ 上传恢复首次选文件不触发（issue #104）**：
  前端补测 BackupManager 时发现两个缺陷：① 首次加载 `/api/backups`
  失败时 `!data` 分支只渲染「加载中…」，catch 里写入的 error 永远
  不可见，用户永久卡在加载态且无任何报错提示；② 「上传备份恢复」
  文件选择框 onChange 里 setFile 后立即调用 restoreUpload()，但
  restoreUpload 读的是当前渲染闭包里的旧 file 状态（首次为 null）——
  首次选文件不弹确认也不上传，第二次选择会误用上一次的文件。修复：
  ① 无数据分支区分 error 渲染 alert-error 并支持点击重试（error 为
  空时才显示「加载中…」）；② restoreUpload 改为显式接收 picked 文件
  参数（onChange 直接传入，不再依赖异步 setState 后的状态），移除
  仅用于传参的 file 状态。
  - 前端：`components/BackupManager.jsx` 无数据分支增加错误渲染与
    点击重试；restoreUpload(picked) 参数化。
  - 测试：前端新增 BackupManager 全量交互测试 13 用例（加载/空列表/
    保存配置/立即备份/下载/删除确认/恢复确认/上传恢复/busy 防重复/
    首次加载失败错误展示），其中「首次加载失败错误展示」「首次选
    文件确认上传」2 用例先复现失败再修复通过。

- **概览页竖屏平板浏览时开放 issue 区域仓库名显示不出来（issue #102）**：
  概览页「开放 Issue」仓库卡片头 `.issue-repo-head` 为不换行 flex，
  仓库名 `.issue-repo-name` 设了 `white-space: nowrap` +
  `text-overflow: ellipsis` + `overflow: hidden`（flex 子项
  overflow 非 visible 时自动最小尺寸为 0，可被压缩至 0），竖屏平板
  窄视口下卡片网格 auto-fit 降为 1 列（约 280px 宽），优先级
  badge、issue 计数、添加按钮占掉固定宽度后，仓库名被压缩至几乎
  不可见。修复：头部 `flex-wrap: wrap` 允许换行；仓库名
  `flex-basis: 100%` 独占首行 + `overflow-wrap: anywhere` 任意断行
  （深路径 group/subgroup/project 无空格时也能断行完整显示），任何
  视口宽度下仓库名均完整显示不再截断。
  - 前端：`styles.css` 修改 `.issue-repo-head`（新增
    `flex-wrap: wrap`）与 `.issue-repo-name`（`flex-basis: 100%`
    + `overflow-wrap: anywhere`，移除 nowrap/ellipsis/overflow 截断
    三件套）。
  - 测试：前端新增 5 用例（CSS 规则断言头部换行、仓库名无
    nowrap+ellipsis、overflow-wrap: anywhere、flex-basis 独占首行；
    渲染级长仓库名深路径完整渲染进 issue-repo-name 不丢字符、多个
    仓库各完整渲染）。

- **「添加 issue」界面标签颜色不显示（issue #100）**：
  GitLab labels API 实际返回的颜色带 `#` 前缀（实测
  `{"color": "#6699cc", "text_color": "#FFFFFF"}`），而后端校验只
  接受不带 `#` 的 6 位 hex——真实环境中「添加 issue」弹窗标签多选、
  概览页 issue 列表、issue 右边栏三处的标签胶囊全部被置为无色中性
  降级，GitLab 标签颜色从未正确同步到界面。修复：`api/issues.py`
  新增 `_normalize_hex`（可选 `#` 前缀 + 6 位 hex → 无 `#` 的 6 位
  hex），`_label_entry` 统一经其归一化——与前端 `label-pill` 自行拼
  `#` 的约定对齐，三处标签胶囊即正确着色；非法值（畸形 `#` 前缀、
  非 hex 字符、数字类型、注入尝试）仍置 None 中性降级，防样式注入
  校验不因兼容 `#` 前缀而放宽。前端零改动（拼 `#` 渲染逻辑本就
  就绪）。
  - 测试：后端新增 overview 带 `#` 前缀颜色归一化、非法 `#` 变体
    拒绝（`#12345`/`##123456`/`#GGGGGG`/`#12 345`/数字）、form-meta
    带 `#` 前缀归一化与非法降级 4 用例；前端新增弹窗标签胶囊显示
    GitLab 颜色、无色标签无内联样式（中性兜底）2 用例。

### Added

- **前端补充测试：fmtSize/登录页/目录选择器/数据备份卡片（issue #104）**：
  前端测试框架为 Node.js 内置 node:test 运行器（`node --test
  'tests/**/*.test.mjs'`，非 Vitest/Jest）+ react-test-renderer 渲染
  + vite ssrLoadModule 加载 JSX + node:test mock.method 做 API mock。
  此前 Login.jsx、FolderPicker.jsx、BackupManager.jsx 三个组件与
  fmtSize 纯函数无任何测试覆盖，本次补齐：fmtSize B/KB/MB 三档
  边界 8 用例；登录页 SSO 错误映射（login_failed/access_denied/
  未知透传）与登录跳转 7 用例；目录选择器打开加载/隐藏目录过滤/
  上级与路径跳转/空目录与错误提示/选择回调/ESC 与遮罩关闭 16 用例；
  数据备份卡片交互 13 用例。新增 tests/helpers/mock-router-login.jsx
  （useSearchParams 查询参数可注入，供登录页 error 分支渲染测试）。

- **「添加 issue」界面只输入标题时描述自动复制标题内容（issue #103）**：
  概览页「添加 Issue」弹窗中，用户只输入标题、描述留空时，描述直接
  复制标题内容：输入时实时联动（标题 onChange 检测描述为空或仍为
  上次自动复制的旧标题时同步填充，用户可见），提交时兜底（描述
  trim 后为空则用标题填充），保证最终创建的 issue 描述等于标题；
  描述非空（用户手写）时标题改动不覆盖。
  - 前端：`components/AddIssueModal.jsx` 标题输入联动逻辑 + 提交
    兜底（`description: trimmedDesc || trimmedTitle`，原 `|| null`
    改为兜底复制）。
  - 测试：前端新增 8 用例（输入联动：只输标题描述框自动复制、继续
    改标题描述跟随、描述已输入时改标题不覆盖；提交兜底：只输标题
    提交 description=标题、自动复制后手动清空描述提交仍兜底、标题
    与描述都输入提交保留用户输入；回归：标题为空仍被校验拦截）。

- **概览页正在运行的 issue 置顶展示（issue #101）**：
  概览页「开放 Issue」板块中，正在被 bot 执行（任务状态
  running/retrying，即 LIVE_STATUSES）的 issue 从原 bot 终态分组
  （bot-failed / bot-done / 其他）中移出，单独成「⚙️ 运行中」组
  置于该仓库 issue 列表最上方，优先于终态分组展示；任务结束从任务
  列表消失后，issue 自动回落原分组。复用 issue #99 的
  `runningIssueKeys` 匹配逻辑（按 `repo_id + issue_iid`），零新增
  接口。覆盖边界：runningKeys 缺失/null/非 Set、repoId 数字/字符串
  归一、running 与 bot-done/bot-failed 并存（运行中优先终态分组）、
  多个运行中 issue 保持原始相对顺序、跨仓库同 iid 不误置顶、
  100 条混合数据分组计数正确且总量不丢、置顶项保留 #99 高亮与
  「运行中」徽章、任务结束后分组还原。
  - 前端：`Overview.jsx` 的 `groupIssuesByBotLabel` 增加可选参数
    `runningKeys`/`repoId`（命中即归 running 组，缺省行为与 #80
    完全一致），`ISSUE_GROUPS` 置顶新增 running 组（标题
    「⚙️ 运行中」），渲染处传入运行键集合与仓库 id；板块说明文案
    同步注明置顶规则。
  - 测试：前端新增 12 用例（纯函数分组归类/相对顺序/边界防御/
    100 条计数 + 渲染置顶顺序/多运行中/跨仓库/回落），更新 #99 与
    #80 既有 3 处断言以对齐置顶分组行为。

- **概览页开放 issue 高亮正在运行的 issue（issue #99）**：
  概览页「开放 Issue」板块中，正在被 bot 执行（任务状态
  running/retrying，即概览页活跃任务定义 LIVE_STATUSES）的 issue
  列表项高亮——浅蓝背景 + 左侧蓝色竖条 + 标题旁「⚙️ 运行中」蓝色
  徽章（与任务状态徽章 status-running 同色系）。数据复用概览页已有
  的任务轮询（每 3 秒），按 `repo_id + issue_iid` 匹配，零新增接口；
  任务结束从任务列表消失后高亮自动消失，任务失败重试（retrying）
  期间持续高亮。覆盖边界：任务缺 repo_id/issue_iid、非活跃状态
  （queued/succeeded/failed）不误标、跨仓库同 iid 不误高亮（repo_id
  参与匹配）、数字/字符串类型归一、高亮与 bot 终态徽章及标签胶囊
  并存不破坏既有分组。
  - 前端：`Overview.jsx` 新增导出纯函数 `runningIssueKeys`（活跃任务
    → `repo_id:iid` 键集合，Set 去重、字段缺失防御），渲染时命中项
    加 `issue-item-running` 类与「运行中」徽章；`styles.css` 新增
    `.issue-item-running`（浅蓝背景 + 左侧 3px 蓝色竖条）与
    `.issue-status-running`（蓝色弱底徽章）样式；
  - 测试：前端新增 `overview-issue-running-highlight.test.mjs`
    16 用例（纯函数正常/边界/类型归一/去重/100 条混合、渲染高亮、
    跨仓库同 iid、retrying、空任务列表、全不匹配、与分组及标签胶囊
    并存）。
- **概览页 issue 右边栏展示评论与活动（issue #97）**：
  点击开放 issue 打开右边栏后，描述下方新增「评论」与「活动」两个
  区块——评论（其他参与者的发言：作者头像/姓名/时间 + Markdown 正文）
  与活动（GitLab 系统事件：分配/标签/状态变更等，纯文本 + 时间）按
  note 的 system 标志分区展示。抽屉打开时按需拉取详情、切换 issue
  自动重新拉取；覆盖边界：加载中占位、接口失败错误横幅 + 重试按钮、
  无评论/无活动空占位、旧缓存数据缺 project_id 时不发请求显示占位、
  异常 note 字段（缺作者/时间/正文）兜底不崩溃。
  - 后端：`gitlab_client.py` 新增 `list_issue_notes`（notes API
    升序分页拉取，limit 截断防大 issue 翻页打爆 API）；`api/issues.py`
    新增 `GET /api/issues/{project_id}/{iid}/detail` 薄路由——仓库
    定位与关闭接口共用 `_enabled_repo_by_project_id`（不存在/未启用
    → 404），客户端选择与聚合一致（per-repo token 优先，回退全局
    bot token），note 精简 id/body/system/author{name,username,
    avatar_url}/created_at（UTC 无后缀，前端 fmtTime 解析约定），
    错误映射 GitLab 404 → 404、其他错误与网络错误 → 502；
  - 前端：`IssueDrawer.jsx` 新增评论/活动区块（评论正文 Markdown
    渲染复用 issue #27 组件、头像复用列表 assignee 的 avatar-fallback
    兜底），`styles.css` 新增区块/评论卡片/活动行样式；README API
    清单同步补充 detail 端点；
  - 测试：后端 `test_api_issues.py` 新增 TestIssueDetail 10 用例
    （字段精简与时间转换、limit 传参、空 notes、404/502 错误映射、
    异常字段兜底、per-repo client 优先）；前端新增
    `overview-issue-notes.test.mjs` 11 用例（接口路径、评论渲染、
    活动分区、加载中/失败重试/空占位、缺 project_id 不发请求、
    切换 issue 重拉、作者回退与空正文兜底）；关闭按钮测试随抽屉
    新增详情拉取补 api.get mock。
- **概览页「添加 Issue」按钮恢复——分支未合并回归修复（issue #95）**：
  排查发现 issue #92 实现的「添加 Issue」按钮只推送到
  `feat/overview-add-issue` 分支、从未合并回 main（也未创建 MR），
  main 上 issue #80/#94 独立演进，导致概览页开放 issue 板块仓库卡片
  右上角的按钮在 main 上缺失。修复方式：将 `feat/overview-add-issue`
  分支合并回 main（含 issue #92 按钮弹窗与 issue #93 分配人下拉修复），
  按钮、后端 API、测试随 main 一并发布。
  防回归：`frontend/tests/overview-add-issue.test.mjs` 11 用例随合并
  进入 main，CI frontend:test job 持续守护按钮渲染与弹窗行为——按钮
  再次丢失会立即测试失败阻断流水线。
- **概览页 issue 右边栏「关闭 issue」按钮（issue #94）**：
  点击开放 issue 打开右边栏后，右上角新增「关闭 issue」危险操作按钮
  （btn-danger 样式）——点击先二次确认（window.confirm），确认后调用
  后端关闭 GitLab issue；成功后按钮消失、状态徽章即时变「已关闭」并
  通知父组件刷新开放 issue 列表（该 issue 从列表消失），失败展示错误
  信息、按钮保留可重试，请求进行中按钮禁用（「关闭中…」）防重复点击。
  已关闭（closed）或无 project_id 的旧缓存数据不显示按钮。
  - 后端：`api/issues.py` 新增 `POST /api/issues/{project_id}/{iid}/close`
    薄路由——按 GitLab project_id 匹配「已启用」仓库（不存在/未启用 →
    404），复用 `GitLabClient.close_issue`（state_event=close，GitLab
    幂等，重复关闭安全），客户端选择与聚合一致（per-repo token 优先，
    回退全局 bot token）；错误映射 GitLab 404 → 404「issue 不存在」、
    GitLab 其他错误与网络错误 → 502；成功后清空概览缓存，下一轮轮询
    立即反映关闭状态；overview 聚合的每条 issue 注入 project_id 字段
    （前端关闭按钮定位仓库用）；
  - 前端：`IssueDrawer.jsx` 新增关闭按钮（confirm 二次确认、closing
    禁用、closed 本地标记即时更新徽章）、`Overview.jsx` 传
    onIssueClosed 回调刷新列表、`styles.css` 新增 `.issue-drawer-error`
    错误提示样式；
  - 测试：后端 `test_api_issues.py` 新增 TestCloseIssue 9 用例（正常
    关闭、缓存清空、仓库不存在/未启用 404、GitLab 404/5xx、网络错误、
    幂等重复关闭、project_id 注入），2 个既有字段断言随新字段更新；
    前端新增 `overview-issue-close-button.test.mjs` 8 用例（按钮显隐、
    确认/取消、接口参数、成功状态与回调、失败重试、请求中禁用）。
- **概览页「添加 Issue」按钮：弹窗表单直连 GitLab 创建 issue（issue #92）**：
  开放 issue 板块每个仓库卡片右上角新增「添加 Issue」按钮，点击弹出
  表单：标题（必填）、描述（选填）、分配人（项目成员下拉，必填，默认
  选中 agent）、标签（仓库已有标签多选，必填，不可新建）；提交后调用
  GitLab API 在对应仓库创建 issue，成功后关闭弹窗并立即刷新列表。
  - 后端：`gitlab_client.py` 新增 `list_project_members`（members/all
    含继承成员，取 user_id 作为 assignee_ids 的用户 id——members API
    顶层 id 是成员关系 id 不可用）与 `create_issue`（标题/描述/分配人/
    标签拼装）；`api/issues.py` 新增 `GET /api/issues/form-meta/{repo_id}`
    （成员+标签元数据，二者为必填字段数据来源，任一查询失败 502 不降级）
    与 `POST /api/issues`（标题/分配人/标签必填校验、空白标签元素过滤，
    per-repo client 优先与 issue 查询一致，创建成功后清空 overview 缓存
    保证前端刷新立即生效）；
  - 前端：新增 `AddIssueModal.jsx`（表单交互、成员含 agent 时默认选中、
    Esc/遮罩/× 三种关闭方式）；`Overview.jsx` 卡片头右上角按钮 + 创建
    成功后关闭弹窗并重新拉取列表；`styles.css` 弹窗字段与标签多选样式；
  - 测试：后端 `test_api_issues.py` 新增 19 用例（成员精简 user_id、
    404/400/502、成员标签空列表、异常成员元素过滤、per-repo client 优先、
    三项必填校验、描述选填、缓存失效后重新拉取）；前端
    `overview-add-issue.test.mjs` 11 用例（按钮渲染、默认选中 agent、
    三项必填校验不调 POST、提交成功参数/弹窗关闭/列表刷新、失败保持
    弹窗、空标签/元数据失败/遮罩关闭边界）。

- **概览页开放 Issue 按 bot 终态标签分组 + 状态徽章（issue #80）**：
  开放 issue 板块区分 bot 处理状态——每个仓库卡片内 issue 按三组分组
  展示：`bot-failed`（处理失败）/ `bot-done`（已完成待确认）/ `其他`
  （两种标签都不带，组顺序按用户指定 failed → done → other），只渲染
  非空组、组标题带计数；带终态标签的 issue 在标题旁显示醒目状态徽章
  （绿=done、红=failed，复用任务状态徽章弱底语义色风格），同时从普通
  标签胶囊中过滤这两个标签，避免同一标签重复显示。判定优先级 bot-done
  高于 bot-failed（失败后重试成功时两标签并存，成功为最终态）。
  - 前端：`Overview.jsx` 新增导出 `BOT_STATUS_NAMES` / `BOT_STATUS_META` /
    `ISSUE_GROUPS` / `botStatusKey` / `groupIssuesByBotLabel` 纯函数，
    分组保持组内原始相对顺序（后端已按 updated_at 降序）；`styles.css`
    新增组标题行与状态徽章样式；
  - 测试：`frontend/tests/overview-issue-groups.test.mjs` 18 用例
    （三组归类、组顺序、双标签并存归 done、labels 缺失/null/非数组/
    元素缺 name、空数组、单元素、100 条混合不丢失、组标题顺序与计数、
    徽章渲染与位置、标签胶囊过滤、全普通/空组/空仓库/缺字段渲染）。

- **CI 静态代码分析矩阵：5 种免费工具、高危/中危中止流水线（issue #86）**：
  security 阶段由单一 bandit 扩展为 5 个并行 job，覆盖前后端「代码 SAST +
  依赖 CVE + 密钥泄露」三个维度，任一 job 发现高危/中危问题即失败阻断
  后续所有 stage；审计服务不可用（基础设施故障）时警告放行不阻断。
  - `security:bandit`（既有，issue #58）：后端 Python 代码 SAST，中危
    及以上阻断（SARIF 报告），行为不变；
  - `security:deps-python`（新增）：pip-audit 扫描 requirements.txt 依赖
    已知 CVE（PyPI 数据源失败自动切 OSV 重试）；pip-audit 输出不含
    severity，任何已公开漏洞保守按中危阻断，CI 变量 `PIP_AUDIT_IGNORE`
    （逗号分隔漏洞 ID）可人工豁免；
  - `security:deps-frontend`（新增）：npm audit 扫描 package-lock.json
    依赖 CVE（npmmirror 审计端点不可用自动切官方 registry），moderate
    及以上阻断，低危/Info 仅记录；`NPM_AUDIT_IGNORE`（逗号分隔包名）
    可豁免；
  - `security:semgrep`（新增）：semgrep 社区版一次扫描前后端代码
    （Python + JS/TS），规则内置仓库 `ci/semgrep-rules.yaml`（12 条，
    不依赖外网规则下载），ERROR/WARNING（高危/中危）阻断、INFO 仅
    记录；扫描跑两遍（--json 阻断判定 + 规则健康检查防假绿、
    --sarif 报告上传）；
  - `security:secrets`（新增）：gitleaks 全仓库硬编码密钥检测（100+
    内置规则），任何发现即阻断；扫描排除运行时数据目录（data/、
    workspace、node_modules 等，配置见 `ci/gitleaks.toml`）；二进制
    复用 runner 持久目录 `~/.local/bin/gitleaks`（已预置），缺失时
    自动下载；
  - 报告统一上传 GitLab Security 页面：bandit/semgrep/gitleaks 走
    `reports:sast`（SARIF），pip-audit/npm audit 经新增
    `ci/convert-audit-to-gitlab.py` 转为 GitLab 依赖扫描报告走
    `reports:dependency_scanning`（JSON 15.0.0 schema），均为
    `when: always` 上传（阻断失败时页面同样可见漏洞详情）；
  - 依赖漏洞修复（门禁上线时暴露的既有漏洞）：react-router-dom
    6.26.0 → 7.18.2（2 个 CVE：open redirect 与构造器注入）、
    vite 5.4.0 → 7.3.6 + @vitejs/plugin-react 4.3.1 → 5.2.0
    （esbuild dev server 任意请求读取，vite 高危传递依赖），
    audit 清零、前端测试 280 通过、构建通过；
  - 测试：`ci/test_convert_audit_to_gitlab.py` 14 用例（pip/npm 转换、
    阻断判定、豁免机制、服务不可用 exit 2、severity 归一化边界），
    CI 的 deps-python job 每次流水线回归执行；
  - 文档：`docs/静态分析扫描结果查看指南.md` 汇总 5 种工具的报告
    查看入口与误报豁免方式。

- **概览页 issue 详情右边栏（issue #85）**：点击开放 issue 不再直接跳转
  GitLab，改为打开右侧抽屉展示 issue 具体信息与正文；跳转统一走抽屉
  右上角「在 GitLab 中打开」按钮（web_url 新窗口）。
  - 后端 `/api/issues/overview`：issue 对象新增透传 description（Markdown
    正文原样，前端渲染）、author（精简为 name/username）、state、
    created_at（转 UTC 无后缀，与 updated_at 同规则），缺失时均为 None
    兜底，不影响旧版缓存数据；
  - 前端：新增 `components/IssueDrawer.jsx` 详情抽屉——状态徽章 / 作者 /
    创建与更新时间 / 标签胶囊 / 里程碑 / 负责人 / 评论数 + 正文 Markdown
    渲染（复用 issue #27 的 Markdown 组件，空正文显示「暂无描述」占位）；
    关闭方式：右上角 × / 点击遮罩 / Esc 键；列表项标题由 `<a>` 改为
    `<button>` 打开抽屉；样式复用 issue #70 drawer 模式加宽至 640px 容纳
    正文；
  - 测试：TDD 先行（红灯确认后实现），新增 18 用例（后端详情字段透传与
    缺失兜底 6，前端抽屉打开 / 跳转按钮 / 三种关闭方式 / 切换与幂等 /
    旧数据与空正文兜底 12），另更新行为变更断言 4 处（后端 2、前端 2）；
    后端全量 853 通过、前端 280 通过。

- **issue 标签优先级调度（issue #76，方案 C）**：同仓库队列内按 issue 标签
  权重排序派发，默认 bug 最优先，标签顺序可在设置页自定义。
  - 调度器：队内派发改按「标签权重 → issue 更新时间 → task_id」选任务
    （权重 = 任务标签在 `worker.issue_priority` 中首个命中的索引，未命中
    配置标签或无标签排最后）；派发时动态读配置，设置页修改后已排队任务
    即时生效；仓库优先级（issue #51）仍优先于队列内标签权重；
  - 数据层：tasks 表新增 `issue_labels`（JSON 数组）与 `issue_updated_at`
    （UTC 串，GitLab ISO8601 归一化）列（迁移 v6）；webhook 与对账两条
    入队路径均落库（标签以 API 最新状态为准）；
  - 配置：新增 `worker.issue_priority`（默认 `["bug","test","feature"]`），
    设置页「任务调度」卡片可编辑（逗号分隔输入框，保存写回 config.yaml），
    API 校验（非空字符串数组 / 标签名合法 / 无重复，非法回退默认）；
  - 文档：`docs/labels.md` 优先级判定、`docs/设计方案.md` §5.4、README
    架构图与配置表同步；
  - 测试：TDD 先行（红灯确认后实现），新增 38 用例（调度器标签权重排序
    12、webhook/对账标签落库 5、迁移与时间归一化 8、设置 API 8，前端
    设置页 5，另更新迁移版本断言 1 处）；后端全量 847 通过、前端 268
    通过。

- **执行引擎集成 deepseek-harness（issue #84）**：按 issue #74 方案B 以
  Python SDK 进程内调用方式接入第三执行引擎 `dsh`（deepseek-harness 官方
  `deepseek-harness-sdk`，stdio JSON-RPC 驱动捆绑运行时，无需 Node.js）。
  `worker.engine: dsh` 即可切换，与 claude / hermes 共享全部既有设施
  （工作区、超时、停止、日志、SSE 实时流、CI 流水线等待与收尾流程）。
  - 后端新增 `botler/dsh_runner.py`：封装 SDK 生命周期（惰性导入、worker
    线程跑 `harness.run()`、停止/超时经 `close()` 强制终止运行时——语义
    等价 SIGKILL 进程组），SDK 通知映射为 hermes 风格事件行（文本/思考/
    工具调用/回合结束/会话状态），输出协议与 hermes 对齐，SSE 解析与
    回放零改动复用；
  - 后端 `executor.py`：引擎白名单加 `dsh`，新增 `_run_dsh_once`（轮询
    停止/超时）、`_dsh_result` 结果判定（finish_reason=completed 且
    final_response 非空才算成功；max-tokens/error/未知 reason 一律按失败
    重试，不静默成功）；
  - 断点续跑：SDK 在 session_root 持久化会话，tasks 表新增
    `dsh_session_id` 列（迁移 v5），重试/重启以同一会话 id 接续对话；
  - 配置：config.yaml 新增 `dsh` 段（provider/model/max_tokens/
    session_root/cordis/runtime_bin/base_url/api_key），设置页 API 可写
    （api_key 掩码），DeepSeek Key 走环境变量 DEEPSEEK_API_KEY
    （botler 不管理，同 hermes 模式）；SDK 为可选依赖（不在
    requirements.txt），未安装时任务报错并附安装指引（阿里镜像 +
    全版本号，清华源未同步 rc 版）；
  - 环境检测：设置页新增 dsh 项（pip 包检测 + PyPI 最新版本查询）；
  - 文档：新增 `docs/dsh-engine-deployment.md` 部署指南，README 配置表
    同步；
  - 测试：TDD 先行（红灯确认后实现），新增 65 用例（dsh_runner 线程
    模型/停止/超时/事件映射 26、executor 分派/判定/落库/断点续跑 29、
    迁移与环境检测 10，含既有引擎回归保护；另更新迁移版本断言 1 处）；
    全量 806 通过；设置页 API 补 dsh 段（掩码返回 + 校验，另增 8 用例，
    全量 814 通过）。

### Changed

- **全页面响应式布局优化——宽屏单边留白从最多 351px 缩至恒 50px（issue #98）**：
  概览页等所有页面在 1920~2559 视口区间内容区恒为 1840px，视口越宽两侧
  留白越大（2K 屏窗口化视口 2542px 时单边 351px）；4K 屏内容区封顶
  2480px，单边留白 680px。修复方式：宽屏断点（≥1440px）从固定档
  （1920→1840、2560→2480）改为动态跟随视口
  `max(1440px, calc(100vw - 100px))`——≥1540px 视口单边留白恒为 50px，
  1440~1539px 取下限 1440px 平滑过渡无跳变；1440px 以下断点体系（与任务
  表格列隐藏联动，issue #70）保持不变。所有页面共用 `.content` 容器，
  一处改动全局生效。
  - 前端：`styles.css` 宽屏断点动态化并删除 1920/2560 固定档；
    `Tasks.jsx` `contentWidthAt` 同步为 `max(1440, 视口 − 100)`；
  - 测试：三个布局测试文件的断点提取函数支持动态断点形式；
    `overview-responsive-layout.test.mjs` 新增单边留白恒 50px 断言（含
    2542 用户场景、1440/1539/1540 断点边界、4000 超大视口防封顶回退）；
    `tasks-responsive-cols.test.mjs` 与 `tasks-table-fit-content.test.mjs`
    一致性断言视口列表同步扩充。

### Fixed

- **概览页「添加 Issue」弹窗分配人下拉为空（issue #93）**：
  现象为点击仓库卡片「添加 Issue」按钮后，分配人下拉列表为空，但
  GitLab 平台上该仓库有三个可分配成员——排查确认并非 token 权限问题
  （仓库内嵌 token 实测可正常拿到成员清单），而是字段兼容缺陷：后端
  `_trim_member` 硬依赖 members/all 返回项的 `user_id` 字段，而本实例
  GitLab 19.0.1 的 members/all 实际只返回顶层 `id`（成员关系 id，不可
  用作 assignee_ids）与 `username`/`name`，不含 `user_id`——所有成员
  被当作异常元素过滤，form-meta 返回空 members，前端下拉自然为空。
  - 后端 `api/issues.py`：`_trim_member` 放宽为 user_id 缺失但 username
    存在时保留条目（id 暂置 None）；`issue_form_meta` 对这类成员按
    username 调 `client.get_user_id_by_username`（issue #65 已有方法）
    查 /users 补齐真实用户 id，查不到（用户已删除等）的成员剔除——
    下拉不出现无法分配的条目；user_id 存在的成员不发额外查询；
  - 测试：TDD 先行（红灯复现真实 GitLab 成员返回形态），新增 3 用例
    （无 user_id 成员按 username 补齐、查不到时剔除、有 user_id 时零
    额外查询），并更新原有异常元素过滤用例语义；后端全量 886 通过、
    前端全量 313 通过。

- **CI security 门禁两处失效与 venv 安装目标缺陷（issue #86 收尾，
  issue #91 流水线诊断 #850 暴露）**：
  - 门禁失效：`.docs_only_skip` 规则尾条款 `when: always` 会绕过
    stage 失败传播——security 阶段因高危/中危漏洞失败时 build/deploy
    仍照常执行（#846 起 deploy 在 security failed 下仍 success）；
    改为 `when: on_success`（与 GitLab 默认行为一致）。另
    `deploy_to_code01` 的 `needs` 仅依赖 frontend:build，needs 会
    绕过 stage 顺序与失败传播，security 失败不阻止部署；needs 显式
    补全 5 个 security job（任一失败即阻止部署）；
  - venv 安装目标缺陷：uv 只自动发现 `VIRTUAL_ENV` / `.venv`，不会
    使用 `.venv-audit` / `.venv-semgrep` 自定义名——deps-python 的
    pip-audit/pytest 被装进主 `.venv`（随后 `.venv-audit` 报
    No module named pytest），semgrep 则直接报 No virtual environment
    found；改用 `uv pip install --python <venv>/bin/python` 显式指定
    + 安装后 import 验证（shell executor 工作区跨 job 持久，坏 venv
    残留会让 uv 误判已安装而跳过，验证失败自动重建重装）；
  - 测试：本地模拟验证 `--python` 显式安装与坏 venv 重建路径。

- **git clean 权限受限残留导致任务重试耗尽失败（issue #91）**：
  任务 #136（daymark 仓库 issue #7）连续 3 次失败，错误均为
  `git clean 失败 (exit 1): Permission denied`——用户曾以 root 身份在
  local_path 工作区跑过 flutter build，留下 root 属主的
  `linux/flutter/ephemeral_root_bak_20260812/.plugin_symlinks/` 残留
  （内含指向 `/root/.pub-cache` 的符号链接），executor 以普通用户执行
  `git clean -fd` 时删除其中条目受父目录写权限约束而 EACCES，整个
  clean 退出非零 → ExecutorError → 重试耗尽 → 任务失败。
  此类残留不影响 fetch / checkout / reset（只涉及 tracked 文件），
  不应拖垮整个任务。
  - 后端 `executor.py`：`prepare_workspace` 的 clean 步骤改走新增
    `_clean_untracked`——首次 `git clean -fd` 因 Permission denied 失败
    时，列出残留 untracked 条目并尝试 Python 层删除（`_force_remove`：
    删除失败先恢复条目与父目录权限再重试一次），复检 clean 仍权限失败
    则降级为警告继续执行（残留留给用户手动清理），非权限类失败保持
    原行为直接抛错；新增 `_on_rmtree_error` 供 rmtree 恢复权限后重试
    深层条目删除；
  - 测试：TDD 先行（红灯复现与任务 #136 完全一致的错误信息），新增
    3 用例（无写权限残留清理干净、root 属主删不掉时警告放行不阻塞、
    非权限错误仍抛 ExecutorError）；全量 864（后端）+ 284（前端）+
    14（ci 转换脚本）通过。

- **CI security 矩阵三个 job 自上线即失败（issue #91 流水线诊断发现）**：
  issue #86 的 security 门禁从未真实跑通（历史流水线 #846/#847 同样
  failed），两处缺陷：
  - semgrep / deps-python：`.backend_setup` 的 before_script 会
    `cd backend`，venv 实际创建在 `backend/.venv-*`，但脚本
    `cd "$CI_PROJECT_DIR"` 后仍用相对路径 `.venv-semgrep/bin/...` /
    `.venv-audit/bin/...` 引用 → No such file or directory（exit 127）；
    semgrep 的 SARIF 报告输出到仓库根目录，与 artifacts 期望的
    `backend/semgrep-report.sarif` 不符（失败时报告也传不上去）；
  - deps-python / deps-frontend：`set -e` 下转换脚本 exit 2（审计服务
    不可用）直接终止整个 job，「切备用数据源重试 → 仍失败警告放行」
    的循环逻辑从未执行（deps-frontend 日志证实：npmmirror 不可用后
    未尝试官方 registry 即 exit 2 失败）；
  - 修复：semgrep 步骤 3/4/5 统一引用 `backend/.venv-semgrep/...` 且
    SARIF 输出至 `backend/semgrep-report.sarif`；deps-python 步骤 4
    改用 `backend/.venv-audit/bin/python`；两处转换脚本调用改为
    `CONVERT_EXIT=0; ... || CONVERT_EXIT=$?`（|| 左侧失败不触发
    set -e，重试循环得以执行）。

- **任务详情聊天记录中用户提示词被截断、与全局模版不一致（issue #90）**：
  任务详情页聊天记录只显示到「推送后必须用」附近（#114 号任务复现），
  与全局模版比对不完整；根因是展示层两处缺陷——发送给 AI 的提示词本身
  完整（7066 字符，与会话文件首条 user 消息逐字节相等），但聊天记录
  API 对每条消息硬截断到 5000 字符且前端不渲染截断标记，用户无感知；
  「查看提示词」按钮则因后端从不返回 prompt 字段而永远只显示占位文案
  （根因分析存档见 `docs/任务提示词截断诊断.md`，本次实施其方案一）。
  - 后端 `executor.py`：`parse_transcript` 对首条可见 user 消息（渲染后的
    完整提示词）跳过 5000 字符文本截断（后续长消息仍截断并标记
    truncated）；消息数量超上限时截断窗口置顶保留首条 user 行，提示词
    不因只保留最近消息而消失；新增 `read_session_prompt` 读取会话文件
    首条 user 消息全文（提示词不落库，仅存在于会话 jsonl）；
  - 后端 `api/tasks.py`：`GET /tasks/{id}/execution` 响应新增 `prompt`
    字段（首条 user 消息全文，会话文件不可读时为 None）；
  - 前端 `TaskDetail.jsx`：「查看提示词」按钮改为展示 execution 懒加载
    的 prompt 全文（无 prompt 时回退占位文案）；`LiveMsg` 对被截断的
    消息（助手回复 / 工具结果 / 后续用户消息）渲染「内容过长，已截断」
    标记；聊天记录消息数量超上限时显示「仅显示首条提示词与最近消息」
    提示（不再丢弃 transcript_truncated）；
  - 测试：TDD 先行（红灯确认后实现），后端新增 7 用例（首条 user 消息
    不截断与数量截断置顶保留、read_session_prompt 全文/空消息/文件缺失、
    execution prompt 字段契约），前端新增 4 用例
    （`task-detail-prompt-truncation.test.mjs`：截断标记渲染、数量截断
    提示、查看提示词懒加载、无 prompt 回退占位）；全量 861（后端）+
    284（前端）通过。

- **任务手动重试后过几秒又变中断（issue #69）**：一键停止所有任务时
  executor 把 task_id 登记进 `_stop_requests` 内存集合后从未清除，任务
  被停止后用户手动重试，worker 领取任务时 `run_task` 开头的
  `_stop_requested` 检查命中残留的旧停止请求，任务被 `_finish_stopped`
  立即打回 interrupted——表现为「每次手动重试过几秒就变成中断状态」，
  只有平台重启（集合随内存清空）才能逃脱（生产日志 task_93/115 证实）。
  - 后端 `executor.py`：新增 `clear_stop_request`（加锁 discard，幂等）；
    `_finish_stopped` 落终态后消费清除停止请求（防止集合无限膨胀）；
  - 后端 `api/tasks.py`：`POST /tasks/{id}/retry` 重置成功后清除该任务
    的停止请求残留——手动重试即用户明确恢复执行，历史停止请求不再
    影响新一次执行；重试后再次一键停止仍正常生效；
  - 测试：TDD 先行（红灯确认后实现），新增 3 用例（重试清除残留请求、
    停止收尾消费请求、重试后再次停止回归保障）；全量 744（后端）+
    263（前端）通过。

### Added

- **bandit 扫描结果在 GitLab 页面可视化查看（issue #72）**：CI 的
  `security:bandit` job 此前只把扫描结果打在日志里，用户在页面无从
  查看；现扫描时同步输出 SARIF 报告（`-f sarif`，安装
  bandit-sarif-formatter 补齐格式支持）并经 `artifacts:reports:sast`
  上传 GitLab，可在「安全 → 漏洞报告」/流水线 Security 标签/MR 安全
  组件中可视化查看漏洞详情（CWE、文件行号、代码片段），并保留原
  阻断门禁语义（中/高危退出码非 0 阻断流水线）。
  - `.gitlab-ci.yml`：`security:bandit` 新增 SARIF 输出与
    `artifacts:reports:sast` 上传（`when: always`——门禁失败时报告
    同样上传，页面仍能看到被阻断的漏洞），报告同时加入
    `artifacts:paths` 保留原始文件下载（reports 类型文件默认不进
    可下载归档），扫描后从 SARIF 提取摘要打印到 job 日志；
  - 文档：新增 `docs/bandit-扫描结果查看指南.md`（三个查看入口、
    原始报告下载、常见问题），README 安全说明章节同步补充入口。

- **设置页新增 Owner GitLab Token 配置与申请教程（issue #87）**：设置页
  增加 owner gitlab token 设置，该 token 专门用来编辑 issue（写评论/
  打标签），严禁用于推送代码与处理流水线；页面内展示「如何申请一个只有
  处理 issue 权限的 token」教程（折叠区，后端读 docs/ 单一文档来源）。
  - 后端 `config.py`：Settings 新增 `gitlab_owner_token`（空串 = 未配置）；
    KNOWN_FIELDS 新增 `gitlab` 段；`update_gitlab` 掩码值/空串 = 保持现有
    （与 sso.client_secret 同模式，支持 ${ENV} 引用）；
  - 后端 `api/settings.py`：GET 返回 `owner_token_masked`（明文不外流）；
    PUT 支持 `gitlab` 段部分更新（类型校验 400）；新增
    `GET /api/settings/owner-token-guide` 教程端点（缺失 404 前端降级）；
  - 后端 `executor.py`：`_call_with_fallback` 新增 `prefer_owner` 参数 +
    `_owner_gitlab_client`（按 token 值缓存重建）；7 处 issue 编辑调用点
    （处理中/成功/失败/提问的评论与标签）传 `prefer_owner=True` 优先
    owner token，owner 401/403 回退原链路（全局 → remote）；会话
    `GITLAB_TOKEN` 注入优先级改为 owner > remote > 全局。git 推送凭据
    （`_askpass_script`）与流水线操作仍只用 bot token——从代码路径保证
    「严禁推送代码/处理流水线」；
  - 后端 `reconciler.py`：终态标签补打（编辑 issue）同样优先 owner
    token，owner 失效回退原链路；
  - 文档：新增 `docs/GitLab-Owner-Token-申请教程.md`（推荐 Reporter 角色
    低权限账号申请 PAT，从账号层面杜绝越权；含方案对比/安全提示/FAQ）；
    `config.example.yaml` gitlab 段新增 owner_token 注释示例；
  - 前端 `Settings.jsx`：新增「Owner GitLab Token（issue 编辑专用）」卡片
    （GitLab 凭据卡片之前）：密码输入框（掩码占位 + 留空 = 保持现有）、
    独立保存按钮（只提交 `{gitlab:{owner_token}}` 段）、用途边界说明、
    「查看 token 申请教程」折叠区（Markdown 渲染，与 SSO 指南同模式）；
  - 测试：TDD 先行（红灯确认后实现），后端新增 22 用例（API 掩码/保存/
    掩码回传保持/空串保持/非字符串 400/${ENV} 展开/教程端点与 404、
    executor 编辑优先 owner/401/403 回退/默认调用不受影响/未配置无影响/
    _build_env 三级优先级/askpass 不含 owner、成功与失败收尾 prefer 分布、
    reconciler 补打优先 owner 与回退）；前端新增 8 用例（卡片位置、密码
    输入框与保存按钮、提交段隔离、用途边界文案、教程折叠、渲染掩码占位、
    保存交互 PUT 载荷、教程展开渲染）；全量 741（后端）+ 263（前端）通过。

- **概览页开放 Issue 板块美化（issue #71）**：参考 GitLab issue 列表页
  重新设计——每条 issue 左列 `#iid` 灰显 + 标题链接，下方渲染彩色
  标签胶囊（颜色与 GitLab 项目标签一致）与里程碑胶囊，右列显示
  assignee 头像（无头像回退首字符占位）、最后更新时间与 💬 评论数。
  - 后端：`gitlab_client.py` 新增 `list_project_labels`（labels API）；
    `api/issues.py` 聚合时每仓库额外查一次项目标签建 name→color 映射，
    issue 精简字段扩展 labels（带 color/text_color）/milestone（title）/
    assignees（name/username/avatar_url）/user_notes_count；标签颜色
    校验为 6 位 hex 防样式注入，labels API 失败或颜色非法时降级无色
    胶囊（不中断整体、不进 errors——标签色只是视觉增强）；
  - 前端：Overview.jsx issue 项改为 GitLab 列表页两列布局（issue-main
    标签/里程碑胶囊 + issue-side 头像/时间/评论数），无颜色标签走
    中性灰胶囊样式；styles.css 新增 label-pill/milestone-chip/
    assignee-avatar/avatar-fallback 等样式；
  - 测试：TDD 先行（红灯确认后实现），后端新增 4 用例（标签颜色附加、
    labels API 故障降级、非法颜色兜底、美化字段透传）并更新 1 例；
    前端新增 5 用例（彩色胶囊样式、里程碑/评论数、assignee 头像、
    美化字段缺失兜底、无头像首字符占位）；全量 707（后端）+ 248
    （前端）通过。

- **任务页响应式列隐藏与「⋯」抽屉（issue #70）**：任务列表宽度不够时
  按优先级隐藏列（尝试→来源→创建时间→失败原因→用时），有列被隐藏时
  操作列最右侧出现「⋯」按钮，点击弹出右侧抽屉显示该任务全部数据。
  - 前端：Tasks.jsx 新增 `hiddenColumnsForWidth` 纯函数（按内容区
    断点与列宽计算需隐藏的列，窗口缩放实时重算）；被隐藏列保留 DOM
    仅加 `col-hidden` 类（display:none，th:nth-child 列宽规则索引
    不受影响），表格 min-width 同步缩减为剩余列宽总和；「⋯」按钮打开
    `TaskDrawer` 右侧抽屉（kv 表格列出全部 12 字段，缺失显示「—」）；
  - 样式：styles.css 新增 col-hidden 隐藏规则、drawer 抽屉样式，
    并新增 1000/1120/1280/1360 四档 --content-width 媒体查询断点，
    使列隐藏按优先级渐进生效、窄屏下不出现水平滚动条（视口 <851px
    全隐藏后仍装不下时保留 .table-wrap 横向滚动兜底）；
  - 测试：TDD 先行（红灯确认后实现），前端新增 11 用例（纯函数断点
    边界与异常输入、窄/宽视口渲染、⋯ 抽屉交互、字段缺失、SSR 无
    window 全显示、JS 列宽常量与 styles.css 列宽规则一致性防漂移）；
    全量 703（后端）+ 243（前端）通过。

- **概览页板块排序调整（issue #68）**：概览页三个板块自上而下调整为
  开放 Issue → 运行中任务 → CI/CD 流水线（修复前为运行中任务 →
  CI/CD 流水线 → 开放 Issue，开放 Issue 板块在最底部不醒目）。
  - 前端：Overview.jsx 中「开放 Issue」板块（issues-section）整体
    上移至页面顶部（h1 之下），运行中任务区居中、流水线板块保持
    最后；数据拉取与轮询逻辑不变，仅调整 JSX 展示顺序；
  - 测试：TDD 先行（红灯确认顺序不符后实现），前端新增 7 用例
    （源码级板块顺序断言 + 渲染级三板块顺序断言，覆盖无任务 /
    无流水线 / 无 issue / 接口全失败等空板块边界）；全量 703
    （后端）+ 232（前端）通过。

- **「等待用户决策」提问反馈到 issue（issue #67）**：无人值守执行中
  Claude 停在需要用户决策的提问节点（如 fix-bug 流程评估多方案后
  提问「请选择 A 或 B」）时，提问不再只留在终端输出里——平台检测到
  该信号后把提问原文贴到 issue 评论、打 `blocked` 标签（不在领取
  过滤标签中），任务判 failed（未完成），用户回复后经重新指派/
  对账扫描再次入队继续处理。
  - 检测：`DECISION_QUESTION_RE` 匹配选项型提问引导（请选择 A 或 B /
    请回复 1 或 2 / 请问……？等），只认最终回复结尾 400 字符内的
    信号（中途提到不算）；再与「查不到该 issue 的引用提交」（无实际
    交付）双重确认，避免误伤正常完成任务（完成汇报的礼貌收尾
    「请确认后关闭本 issue」不在信号之列）；有提交时不判等待决策，
    走既有流水线等待路径；
  - 收尾：新增 `_finish_asked`——任务 failed（error_message 说明等待
    用户决策）、评论包含提问原文（`_extract_question` 从最后一个提问
    信号所在行截取）、打 `blocked` + 移除 `in-progress`、网页通知；
    claude / hermes 两引擎共用（接入 `_await_task_pipeline` 的
    「无提交」分支，两引擎成功判定都经过该入口）；
  - 附带修复：`GitLabClient.add_labels` 支持 `remove` 参数（同一次
    PUT 移除标签），成功/失败/提问收尾打终态标签时一并移除
    `in-progress`（修复前收尾后 issue 上 `in-progress` 与
    `bot-done`/`bot-failed` 并存，如生产 issue #66）；
  - 测试：TDD 先行（红灯复现生产任务 #90 的缺陷），后端新增 5 用例
    （提问+无提交 → failed+评论+blocked；正常完成/有提交不误伤；
    提问信号正则）；全量 703（后端）+ 225（前端）通过。

- **概览页开放 issue 聚合板块（issue #64）**：概览页新增「开放 Issue」
  板块，聚合读取所有已启用仓库的开放（opened）issue，按仓库优先级
  排序（外层），仓库内按最后更新时间降序（内层）。
  - 后端：新增 `GET /api/issues/overview`（api/issues.py）——遍历
    `list_repos()`（已按 priority, id 排序、过滤软删除，issue #62）
    中的已启用仓库，复用 pipelines 模块的 per-repo client 缓存
    （issue #60：remote 内嵌 token 优先、无 token 回退全局 bot
    token），单仓库失败进 errors 不中断整体（HTTP 200），结果带
    10 秒 TTL 缓存；每仓库最多 100 条（limit 防大仓库翻页打爆
    GitLab API），issue 的 updated_at 统一转 UTC 无后缀（复用
    pipelines._commit_time_utc，与前端 fmtAgo 解析约定一致）；
  - 客户端扩展：GitLabClient.list_open_issues 新增 order_by/sort/
    limit 可选参数（服务端按 updated_at 降序 + 条数截断，_paged
    支持 limit 停止翻页），不传时行为与扩展前一致（reconciler
    等既有调用不受影响）；
  - 前端：Overview.jsx 新增板块（15 秒轮询，与流水线板块同频），
    按仓库分组卡片展示（仓库名 + 优先级徽章 + 每仓库 issue 数），
    组内每条 issue 渲染 #iid、标题链接（web_url 新窗口打开）与
    最后更新时间（fmtAgo 相对时间）；无仓库 / 全部仓库无开放
    issue 时显示空状态；单仓库查询失败明细照常展示不影响整体；
  - 测试：TDD 先行（红灯确认后实现），后端新增 24 用例（聚合排序
    两级 / 未启用与软删除过滤 / 失败兜底 / 缓存 TTL / limit 截断 /
    参数透传 / per-repo token），前端新增 8 用例；全量 699（后端）
    + 225（前端）通过。

- **概览页流水线按仓库使用各自 token（issue #60）**：概览页获取流水线
  状态的 token 不再统一用全局 bot token，而是从各仓库本地目录
  `git remote -v` 输出的 URL 中解析——每个仓库使用自己 remote url
  内嵌的 token，仓库间互不相同；仓库 URL 展示统一脱敏。
  - 后端：概览页对每个仓库解析本地目录（local_path 优先，否则
    workspace/&lt;仓库名&gt;，与执行工作区一致）的 remote url，按
    remote_name（缺省 origin）取内嵌 token 建 per-repo GitLabClient
    （host 取 remote url 的 host:port，verify_ssl 沿用全局配置）；
    remote 无 token / 本地目录不存在 / 非 git 仓库时回退全局 bot
    token（兼容旧仓库）；per-repo client 60 秒 TTL 缓存（避免每轮
    轮询重复跑 git 子进程与重建 httpx client，token 轮换 60 秒内
    自动生效）；per-repo client 网络异常（httpx.HTTPError）同样
    进 errors 列表不中断整体；
  - 脱敏：git_remote 新增 `parse_remote_url`（解析 user:token@host，
    支持 URL 编码、token 含 @/冒号等边界）与 `mask_url_token`
    （脱敏为 user:***\@，幂等）；`GET /api/repos`、`POST /api/repos`、
    `PUT /api/repos/{id}`、`POST /api/repos/discover` 响应中的
    URL 统一脱敏；DB 与 config.yaml 仍保存真实 URL（clone 需要）；
    update 回传掩码 url（含 \*）时忽略该字段，防止把掩码写回
    DB（与 sso client_secret 掩码模式一致）；
  - 测试：TDD 先行（新增用例红灯确认后实现），后端新增 32 用例
    （URL 解析 9 + 脱敏 7 + per-repo client 7 + API 集成 4 + 仓库
    API 脱敏 5），全量 640（后端）+ 217（前端）通过。

- **任务列表页新增刷新按钮（issue #59）**：工具栏新增「↻ 刷新」按钮，
  点击重新拉取任务列表，更新所有任务的显示状态。
  - 背景：页面仅在存在活跃任务时每 5s 自动轮询，全部任务结束后轮询
    停止，列表状态可能陈旧；手动刷新补上这一缺口；
  - 低危操作无需确认；请求中按钮禁用防重复点击（文案变「刷新…」）；
    保持当前筛选（状态/仓库/搜索）与页码不变；接口失败展示错误提示；
  - 测试：前端新增 7 用例（源码断言 / 渲染可用 / 重新拉取 / 筛选保持 /
    防重复点击 / 失败提示），后端无改动。

- **CI/CD 接入 bandit 安全扫描（issue #58）**：流水线最前置安全门禁，
  存在高危/中危漏洞时阻断整条流水线，放在所有阶段之前。
  - GitLab 侧：新增 `security` stage（stages 首位）+ `security:bandit`
    job——复用 `.backend_setup`/`.docs_only_skip` 公共配置，扫描
    `botler` 包与 `hermes_runner.py`，`--severity-level medium`
    （高危/中危即 job 失败，后续 build/deploy/sync 全部不运行），
    `allow_failure: false` 显式声明阻断语义；bandit 装进持久化
    .venv，版本不 pin（新规则本应发现新漏洞）；
  - GitHub 侧：新增 `bandit-scan` job（jobs 首位），
    `frontend-build`/`backend-test` 加 `needs: bandit-scan` 门禁——
    bandit 失败时两者跳过、workflow 失败，与 GitLab 语义一致；
  - 误报处理：database.py 4 处动态列名拼接加 `# nosec B608`
    （列名均来自模块级常量白名单 `_TASK_FIELDS`/`allowed`/固定元组，
    非外部输入，SQL 列名无法参数化）；
  - 验证：本地 bandit 实测 4 个 Medium 告警（B608）处理后清零，
    exit 0；注入高危/中危漏洞时 exit 非 0 阻断（见 issue 评论）。

- **仓库设置编辑与调度优先级（issue #51）**：仓库页面每项新增「设置」
  按钮，弹窗可重新编辑显示名称、启用状态与新增的优先级字段（URL /
  本地路径不在编辑范围，涉及 webhook 重注册风险高）；列表展示各仓库
  优先级并按优先级排序。
  - 优先级为整数 1~999，默认 100，数字越小越优先；多个仓库同时有
    排队任务时调度器优先派发优先级高（数字小）的仓库，相同优先级按
    任务提交时间（tasks.created_at）排序，同仓库内仍保持 FIFO；
  - 后端：repos 表新增 priority 列（v3 迁移，存量默认 100）、
    `POST/PUT /api/repos` 支持 priority（1~999 校验）、config.yaml
    同步落盘；scheduler 派发选择改为按 (priority, 队首任务提交时间,
    repo_id) 排序；
  - 前端：新增 RepoEditModal 弹窗组件（名称/启用/优先级），保存前
    前端校验（名称必填、优先级 1~999 整数），失败展示后端错误；
  - 测试：后端新增 22 用例（调度优先级 6 + 迁移/数据库 5 + API 7 +
    回归更新 4），前端新增 7 用例；全量 608（后端）+ 210（前端）通过。

- **任务执行过程实时输出（事件流，SSE 推送）**：像在终端跑 Claude Code
  一样，任务页面逐事件实时看到引擎输出——模型文本、thinking（可折叠）、
  工具调用与工具结果、执行结果，边跑边出；任务结束后可回放完整事件流。
  - 后端：claude 引擎切 `--output-format stream-json --verbose`（逐行
    NDJSON 实时 flush；init 行即拿到 session_id，比原 result 更早落库）；
    hermes runner 注册 hermes-agent 自带回调（thinking/tool_start/
    tool_complete/stream_delta/status），stdout 改为 NDJSON 事件流
    + 最后一行结果 JSON（旧单行协议向后兼容）；
  - 新增 `botler/events.py`：引擎输出行 → 归一化事件解析
    （status/thinking/text/tool/tool_result/result）+ 进程内事件总线
    （EventBus，executor 发布 → SSE 订阅，队列满丢最旧不阻塞读流）；
  - 新增 `GET /api/tasks/{id}/events`（SSE）：连接先回放日志文件已有
    事件（先订阅再回放，回放间隙的实时事件在队列积累无缝衔接），再实时
    推送总线事件，终态后 done 收尾；断线重连自动补齐且不重复（前端按
    seq 去重）；
  - 任务详情页「实时输出」区替换为事件流面板（@tanstack/react-virtual
    虚拟滚动，thinking 默认折叠）；聊天记录面板并行保留；概览页任务卡片
    实时输出改走 SSE（每活跃任务一个连接，最后 N 行自动滚，保留
    trimLogTail 截尾）；
  - 成功判定适配：stream-json 多行输出下按尾部 result 行判定（首个 JSON
    对象是 init 行，不能作为成功依据）；错误提取同样取 result 行；
  - 测试：后端新增 67 用例（test_events 32、test_executor_stream 8、
    test_hermes_runner_stream 5、test_api_events 7，另回归更新）+ 前端
    新增 6 用例（SSE 封装 3 + 详情页事件流 3）+ overview 测试更新为
    SSE 断言；全量 591（后端）+ 188（前端）通过。

- **任务页面增加翻页组件（issue #50）**：任务列表页此前固定只显示
  最近 50 条（「最多显示 50 条」），更早的历史任务无法在页面浏览。
  现利用后端已有 limit/offset 分页能力，页面底部新增翻页组件：
  上一页/页码数字/下一页 + 当前页信息（`第 X / Y 页`），每页 50 条；
  状态/仓库/搜索筛选变化自动重置回第 1 页，5s 自动刷新保持当前页；
  单页（total ≤ 50）与空列表不渲染翻页组件。
  - 后端 `database.py`：`count_tasks` 扩展 `repo_id`/`search` 过滤
    （与 `list_tasks` 过滤一致），`api/tasks.py` 的 `total` 随之跟随
    当前筛选——否则筛选后 total 偏大、总页数错误；
  - 前端 `Tasks.jsx`：新增 `page` 状态与 `offset` 请求参数、导出
    `pageNumbers` 页码窗口函数（≤7 页全显示，多页显示首尾 + 当前 ±1 +
    省略号）；`styles.css` 新增 `.pagination` 样式；
  - 测试：`test_api_tasks.py` 新增 4 用例（total 跟随 repo/search/
    组合筛选、无匹配为 0）+ `tasks-pagination.test.mjs` 12 用例
    （翻页渲染与禁用态、点击翻页 offset 正确、筛选重置、单页/空列表
    隐藏、pageNumbers 边界）。

- **本地环境检测增加 hermes 检测（issue #48）**：设置页「本地环境检测」
  卡片新增 Hermes Agent 项（`hermes --version` 检测安装与版本）。hermes 为
  git 安装的内部 agent，无 npm/GitHub 公开发布源，不查最新版本
  （前端显示 "—"）。
  - 后端 `environment.py`：TOOLS 清单新增 hermes 项；前端/API 无需改动
    （环境检测卡片通用渲染 tools 列表）。
  - 测试：`test_environment.py` 新增 TestHermesTool 6 用例（清单配置 /
    真实版本输出格式 "Hermes Agent v0.20.0 (2026.8.3)" 提取 / 安装与
    未安装检测 / 无发布源不发网络请求 / 整体检测包含 hermes）。

- **任务执行引擎支持 hermes-agent（issue #47）**：`worker.engine` 可切换
  `claude`（Claude Code CLI，默认，现网行为不变）/ `hermes`（部署机已装好的
  hermes-agent，直接调用，botler 不打包不管理 hermes 的 LLM 配置）。
  - 后端 `hermes_runner.py`（新增）：独立 runner 脚本，由 hermes venv 的
    python 运行（`hermes.command`/`args` 配置），stdin/stdout JSON 协议
    （prompt/history/session_id → final_response/messages/session_id/error），
    进程内 `run_agent.AIAgent(quiet_mode=True)` 执行一次任务；
  - `executor.py`：`_run_once` 按引擎分派——hermes 引擎经 `TERMINAL_CWD`
    在仓库工作区执行 terminal 命令、git 凭据沿用 `GIT_ASKPASS` 注入；
    子进程读循环抽取 `_drain_process_output` 两引擎共用；结果判定
    `_hermes_result`（success/unresolvable/failed，自认无法解决不重试）；
  - 断点续跑等价实现（Q3-B）：执行结束把会话历史落库
    `tasks.hermes_history`（新增列，轻量迁移），重试/重启恢复时作为
    `conversation_history` 传入并保留工作区；损坏数据自动降级全新会话；
  - 成功收尾共用 `_await_pipeline_and_finish_succeeded`（CI 流水线等待 +
    bot-done + commit 记录，与 claude 引擎一致）；
  - 设置页无 hermes 配置（配置只走 config.yaml 文件，hermes 的模型/Key
    在 hermes 侧 `~/.hermes` 配好）；新增部署文档
    `docs/hermes-engine-deployment.md`（挂载、配置、断点续跑、故障排查）。
  - 测试：`test_hermes_runner.py` 17 用例（协议解析边界 / AIAgent 调用契约
    / import 失败与 agent 异常降级）+ `test_executor_hermes.py` 22 用例
    （引擎分派与非法值回退 / 命令与环境构造 / 历史落库与恢复 / 损坏降级 /
    结果判定），runner 已在真实 hermes venv 冒烟验证；后端全量 521 passed +
    前端全量 167 passed。

### Changed

- **docs-only 提交跳过 CI/CD 流水线（issue #57）**：此前任何 main 分支
  push 都会触发完整流水线（前端构建 + 后端测试 + 部署），即使提交只改了
  文档（README / CHANGELOG / docs/ 等）也会白跑 5 分钟以上的 runner
  资源。现改为：仅文档变更的提交跳过构建/测试/部署，只保留 GitHub
  镜像同步（docs 变更也需及时镜像）。
  - GitLab（`.gitlab-ci.yml`）：新增公共规则 `.docs_only_skip`
    （frontend:build / backend:test / deploy_to_code01 三 job 复用）——
    rules 按顺序求值，第一条「代码/配置白名单」优先于第二条「docs 跳过」，
    保证代码提交通常伴随的 CHANGELOG.md 变更不会把混合提交误判为
    docs-only；docs-only 判定仅对 main 分支 push 生效，其他分支 /
    MR / tag / schedule 维持原行为；
  - GitLab（sync_to_github）：docs-only 提交仍同步镜像，但按
    `CI_COMMIT_BEFORE_SHA..CI_COMMIT_SHA` 的变更文件判断跳过
    workflow_dispatch 触发（GitHub push 事件已被下方白名单拦截，
    dispatch 是补充入口，docs-only 时一并跳过避免白跑）；
  - GitHub（`.github/workflows/ci.yml`）：push 触发增加 paths 白名单
    （backend/frontend/deploy/scripts 等代码路径），docs-only 的镜像
    push 不再触发 Actions，与 GitLab 侧使用同一套「代码文件」定义；
  - 验证：GitLab API `ci/lint` 通过；本地全量测试通过；流水线实测
    ——代码提交跑全套 success，后续 docs-only 提交流水线只跑 sync
    （build/deploy skipped）。

- **模版页面默认全部展开，折叠方式对齐任务详情页聊天记录（issue #56）**：
  提示词模版页此前（issue #55）默认折叠为 6 行小窗口，每次进入页面都要
  点「展开全部」才能看到完整模版。现改为默认全部展开：textarea 高度
  自适应内容行数完整展示、无内层滚动条（滚动交给页面最外层，与
  issue #52 交互原则一致）；折叠方式改成与任务详情页聊天记录一致的
  标题行切换——「模版内容（N 行）」标题行带 chevron ▾/▸ 可点击
  折叠/展开，折叠时编辑器与操作按钮整体隐藏（非截断小窗口），再次
  展开内容不丢失。
  - 前端 `Templates.jsx`：`expanded` 默认改为 `true`；移除
    「展开全部（N 行）/收起」按钮，新增 `section-toggle` 风格标题行
    （chevron + 行数提示，`aria-expanded` 同步），折叠态不渲染
    textarea 与操作按钮行；
  - 前端 `styles.css`：移除不再使用的
    `.input.textarea.template-collapsed` 截断样式；
  - 测试：重写 `templates-collapsible-editor.test.mjs` 4 用例（默认展开
    rows 自适应 / 标题行折叠隐藏编辑器与保存按钮、再展开内容不丢失 /
    空模版与单行模版边界 / styles.css 无 template-collapsed 残留）；
    前端全量 203 passed + 后端全量 591 passed + vite build 成功。

- **任务详情页滚动与折叠优化（issue #52）**：详情页只保留页面最外层的
  垂直滚动条；事件流、聊天记录、执行日志、claude 输出尾部四个区块取消
  各自内部的垂直滚动条、内容直线完整展示，标题改为可点击折叠/展开
  （默认展开，事件流/聊天记录/执行日志为带 chevron 的标题按钮，
  claude 输出尾部沿用「展开/收起」按钮）。
  - 前端 `TaskDetail.jsx`：事件流移除 `@tanstack/react-virtual`
    虚拟滚动改为全量渲染（与「完整展示」需求一致，长事件流不再受
    420px 内部滚动窗限制）；新增 `SectionToggle` 折叠标题组件与
    `showEvents/showChat/showLogs` 状态；提示词与 claude 输出尾部
    加 `log-view-flat` 类取消内部滚动（概览页卡片不受影响）；
  - 前端 `styles.css`：`.event-list`/`.chat-list` 移除固定高度与
    `overflow-y`，`.log-list` 仅保留超长行横向滚动，新增
    `.section-toggle` 系列样式；清理不再使用的 `.live-log` 死样式；
  - 依赖：`@tanstack/react-virtual` 不再使用，已从 package.json 移除；
  - 测试：新增 `task-detail-collapsible-sections.test.mjs` 6 用例
    （三区块标题按钮默认展开 / 折叠隐藏后再次点击恢复 / claude 输出
    尾部按钮切换 / 120 条事件全量渲染无截断）；前端全量 194 passed +
    后端全量 591 passed。

### Fixed

- **概览页开放 Issue 长标题溢出遮挡评论数与时间（issue #81）**：概览页
  开放 Issue 列表的标题链接 `.issue-link` 是 inline `<a>`，inline 元素上
  `overflow:hidden` 与 `text-overflow:ellipsis` 不生效，长标题直接横向
  溢出容器，遮挡右侧的评论数与最后更新时间。
  - 前端 `styles.css`：`.issue-link` 显式声明 `display:block` 使省略号
    截断生效（保留 overflow/ellipsis/nowrap）；`.issue-main` 补
    `flex:1` 占据右侧元信息外的全部宽度（保留 `min-width:0`），
    右侧 `.issue-side` 保持 `flex-shrink:0` 不被压缩；
  - 测试：TDD 先行（复现测试红灯确认后实现），新增
    `overview-issue-overflow.test.mjs` 2 用例（源码断言 issue-link
    块级 display + 溢出三件套、issue-main flex:1 + min-width:0 与
    issue-side flex-shrink:0）；前端全量 255 + 后端全量 719 通过。

- **任务完成后任务报告没有输出到 issue 评论（issue #79）**：任务成功
  收尾只由平台打 bot-done 标签，结果评论依赖任务会话内 Claude 按模板
  自行留言；全局 bot token 失效期间 Claude 侧 API 401 失败，任务
  succeeded、bot-done 已打，但 issue 上没有任何报告评论。现改为平台
  兜底：成功收尾时检查最后一条非系统评论的作者——是 bot 本人（含
  remote token 账号，即 Claude 已用兜底 token 留过）则跳过，否则从
  执行输出提取结果摘要（claude 的 result / hermes 的 final_response，
  超长按 3000 字符截断）写一条完成报告评论（含提交 sha 与确认提示）；
  检查/写评论失败均不阻塞任务成功（仅记 warn，与打标签一致）。
  - 后端 `executor.py`：`_finish_succeeded` 新增 `_leave_success_comment`
    平台兜底评论（防重集合 = 配置 bot_id + 调用客户端的 /user id，
    经 `_call_with_fallback` 走 per-repo remote token 兜底）；新增
    `_success_summary` 提取两引擎结果摘要；`_build_env` 注入会话
    `GITLAB_TOKEN` 时优先取仓库 remote url 内嵌 token（新增
    `_task_gitlab_token`，remote 无 token / 解析失败回退全局 token），
    使全局 token 失效期间 Claude 会话内的 API（读 issue/写评论）仍可用；
  - 测试：TDD 先行（复现测试红灯确认后实现），新增 9 用例（无人评论
    时平台写报告评论、bot 已评论跳过、remote token 账号已评论跳过、
    写评论失败不阻塞成功、hermes final_response 摘要、条件终态不评论、
    remote token 注入 GITLAB_TOKEN / 无 token 回退全局 / 无 remote
    回退全局），并更新受影响测试的 mock；后端全量 719 + 前端全量
    253 通过。

- **添加本地仓库报 401「无法识别项目: token 无效或已过期」（issue #77）**：
  本地文件夹方式添加仓库时，识别项目与注册 webhook 一律走平台全局
  bot token——全局 token 失效期间，即使本地仓库 remote url 里内嵌了
  有效 token（用户 git pull/push 正常），添加仓库仍直接报 401。
  现与 executor / reconciler 的 per-repo 兜底模式对齐：识别项目遇
  401/403 且 remote URL 内嵌 token 时，用该 token 与 remote host 构建
  临时 client 重试；webhook 注册同理（识别已兜底则复用同一 client，
  全局注册遇 401/403 也兜底重试）。remote 无内嵌 token 或重试仍失败
  时保持原有错误信息（非认证类错误不兜底，原样抛出）。
  - 后端 `git_remote.py`：新增 `build_client_from_url`（从单个 remote
    URL 解析内嵌 token 构建 GitLabClient，添加场景无仓库行可用时的
    兜底入口，`webhook_base_url` 透传保证回调地址始终是平台地址）；
  - 后端 `api/repos.py`：`add_repo` 保存原始 remote URL（识别成功后
    url 会被替换为 API 返回的干净 url，兜底解析 token 必须用原始值）、
    webhook 回调地址计算提前；识别与 webhook 注册两个 401/403 点均
    按上述逻辑兜底；
  - 测试：TDD 先行（复现测试红灯确认后实现），新增 3 用例
    （全局 401 时用 remote token 识别成功且 webhook 注册同 token、
    remote 无 token 时保持 400 原错误、识别成功但注册 401 时单独
    兜底）；后端全量 710 + 前端全量 253 通过。

- **仓库管理页添加仓库方式选项顺序调整（issue #73）**：此前「添加仓库」
  表单中「GitLab URL / project_id」排在第一个选项、「本地文件夹（读取
  git remote）」排在第二个，而默认选中的方式是本地文件夹——视觉顺序
  与选中态脱节，用户容易误选。现调整为：本地文件夹（读取 git remote）
  为第一个选项（默认选中，展开本地路径输入框与 remote 选择），
  GitLab URL / project_id 为第二个选项。
  - 前端 `Repos.jsx`：交换两个 `add-method` radio 选项的渲染顺序，
    默认方式仍为 `'local'`，与第一个选项一致；
  - 测试：TDD 先行（复现测试红灯确认后实现），新增
    `repos-add-method-order.test.mjs` 5 用例（源码级选项顺序 /
    渲染级选项文本顺序 / 第一个选项默认选中、第二个未选中 /
    默认展开本地表单不渲染 URL 输入框 / 交互切换后表单随之切换）；
    前端全量 253 + 后端全量 707 通过。

- **全局 token 失效后任务领取/留评论全 401：executor 与 webhook 补上
  remote token 兜底（issue #65 补充）**：全局 bot token 被撤销期间，
  executor 全部 GitLab 操作（任务领取 get_issue、「处理中」评论、
  失败评论、bot-done/bot-failed 标签、流水线等待、提交查询）只走
  全局 client——任务 1 秒内即失败（生产任务 #88/#89），issue 上
  收不到任何评论；webhook 的 issue 查询与最后发言人查询 401 同样
  直接拒绝入队，事件只能等对账兜底补入队（实时性受损）。现与对账
  对齐：401/403 时按仓库 remote url 内嵌 token 构建 per-repo client
  重试一次，remote 无可用 token 或重试仍失败才报错（非认证类错误
  不兜底，原样抛出）。
  - 后端 `executor.py`：新增 `_call_with_fallback`（镜像对账
    `_call_with_fallback` 模式），应用到 get_issue、领取评论、
    `_issue_state`、`_await_task_pipeline`/`_wait_pipeline_for_commit`
    （流水线探测与轮询）、`_finish_succeeded`（bot-done 标签）、
    `_finish_failed`（失败评论 + bot-failed 标签，且评论失败不再
    阻断打标签）、`_record_commit`（提交查询）；
  - 后端 `webhook.py`：新增 `_repo_client`（按仓库 remote 解析兜底
    客户端）与 `_call_with_fallback`，issue 查询与最后发言人查询
    遇 401/403 时用 remote token 重试；`_repo_bot_ids` 复用
    `_repo_client`；
  - 测试：TDD 先行（复现测试红灯：run_task 领取 401 任务立即失败、
    `_finish_failed`/`_finish_succeeded` 401 评论标签落空、webhook
    401 拒绝入队），新增用例 8（executor 6 + webhook 2），后端全量
    675（+8）+ 前端 217 通过。

- **对账扫描为 0：全局 token 失效后 bot 身份漂移漏扫新 issue（issue #65）**：
  全局 bot token 被撤销后，对账降级以仓库 remote 内嵌 token 的账号
  （project access token 账号，如 project_123_bot / code01）作为 bot
  身份，而用户把新 issue 分配给 @agent——两者 id 不一致，
  `assignee_id` 过滤后扫描为 0，且 API 正常返回（无任何权限报错），
  新 issue 被静默漏扫。现改为 remote URL userinfo 的用户名（如 agent）
  也作为 bot 身份候选：对账以「remote token 账号 + remote 用户名对应
  账号」身份集合分别扫描、按 iid 去重合并；webhook 全局身份获取失败
  （401）时不再直接 500，同样按仓库 remote 身份集合判定 assignee。
  - 后端 `git_remote.py`：新增 `build_repo_client_with_username`
    （返回 (client, remote_username)，`build_repo_client` 改为其薄
    封装，概览页调用不变）；
  - 后端 `gitlab_client.py`：新增 `get_user_id_by_username`（按用户名
    查用户 id，用户不存在返回 None）；
  - 后端 `reconciler.py`：`_reconcile_repo` 全局 bot 身份不可用时，
    以身份集合（remote token 账号 + remote URL 用户名对应账号）分别
    扫描合并；「最后发言人是 bot」判定改用身份集合；
  - 后端 `webhook.py`：`get_bot_id` 401 时降级为 `_repo_bot_ids`
    （仓库 remote 身份集合），assignee 与最后发言人判定同样用集合，
    不再抛 500；
  - 测试：TDD 先行（复现测试红灯：身份漂移场景 scanned=0 无报错、
    webhook 直接抛 401），新增用例 6（对账扫到 remote 用户名账号
    assignee 的 issue / 用户名查无此人只扫 token 账号 / 多身份重复
    assignee 去重 / webhook 全局身份失效降级入队 / 身份不可用拒绝 /
    assignee 不匹配不误领取），更新 issue #63 用例 6 处
    （`build_repo_client` monkeypatch 改为新函数）；后端全量
    667（+6）通过，前端 217 全量通过。

- **仓库对账遇 token 失效时用 remote url 内嵌 token 兜底（issue #63）**：
  对账（定时兜底 + 仓库页「对账」按钮）完全依赖全局 bot token；全局
  token 失效（401）后 `get_bot_id` 失败即整体放弃，各仓库 remote url
  中明明有可用 token 也不尝试。现改为对账遇 401/403 时，从该仓库
  本地目录的 `git remote -v` URL 提取内嵌 token 构建 per-repo
  GitLabClient 重试（与 issue #60 概览页同一机制）。
  - 后端 `git_remote.py`：新增公共函数 `build_repo_client`（原
    `api/pipelines.py` 私有逻辑提取），概览页与对账共用；工作区根
    目录常量化 `_WORKSPACE_ROOT`（backend/botler/workspace，与
    executor 一致）；
  - 后端 `api/pipelines.py`：`_repo_client` 改用公共函数，删除
    本地重复实现；
  - 后端 `reconciler.py`：全局 bot 身份获取失败不再整体放弃对账，
    降级为逐仓库兜底；新增 `_call_with_fallback`（GitLab 调用遇
    401/403 且当前仍是全局 client 时，用 remote token client 重试
    一次，已兜底过不重复兜底）与 `_reconcile_repo`（单仓库扫描：
    `list_open_issues` / `last_note_author_id` 与终态标签补打的
    `get_issue` / `add_labels` 全部走 fallback；全局 bot 身份不可用
    时以 remote token 账号的 user id 作为该仓库 bot 身份；remote
    无可用 token 则记错跳过，行为不变）；
  - 测试：TDD 先行（复现测试红灯：全局 401 时对账直接失败），新增
    用例 6（全局 401/403 兜底补入队 / 无 remote token 记错 /
    全局 bot 身份不可用改用 remote token 账号身份 / 补打标签 401
    兜底 / 兜底也失败报错），更新 1 处（workspace 目录断言随
    `_WORKSPACE_ROOT` 提取调整）；后端全量 661（+6）通过，前端
    217 全量通过。

- **页面上删除仓库后列表仍显示该仓库（issue #62）**：`DELETE
  /api/repos/{id}` 返回 200，但 `GET /api/repos` 仍返回已删除的仓库
  （以「已停用」身份残留，可被重新「启用」成 webhook 已注销、config
  已移除的状态分裂仓库）。根因：「删除」与「停用」共用 `enabled`
  字段软删除，仓库列表接口对软删除行无任何过滤。
  - 后端 `database.py`：repos 表新增 `deleted_at` 列（迁移 v4，
    user_version 3→4）；新增 `soft_delete_repo`（写 `deleted_at`
    标记 + enabled=0，行保留供任务历史解析仓库名）；`list_repos`
    与 `get_repo_by_project_id` 增加 `include_deleted` 参数默认
    过滤已删除行（仓库列表、概览流水线、对账全局生效）；`upsert_repo`
    冲突更新时清除 `deleted_at`（支持删除后重新添加同仓库），
    id 改为按唯一键反查（冲突更新路径 lastrowid 不可靠，重新添加
    路径暴露）；
  - 后端 `api/repos.py`：`delete_repo` 的 db 软删除改调
    `c.db.soft_delete_repo`，与「停用」（enabled=False、行仍可见
    可重新启用）区分；
  - 后端 `api/tasks.py`：任务列表的仓库名映射改用
    `include_deleted=True`，已删除仓库的历史任务仍能显示仓库名；
  - 测试：TDD 先行（复现测试红灯：删除后列表仍含该仓库），新增
    用例 9（API 删除后列表不返回 / 停用仓库仍列表 / 删除后重新添加
    同仓库成功 + 迁移 v4 补列 + 软删除过滤/标记/反查/重新添加清标记），
    更新 3 处既有断言（deleted_at 标记、user_version 4）；后端全量
    655（+9）通过，前端 217 全量通过。

- **删除仓库报错 500（issue #61）**：仓库页删除仓库时后端 500，
  配置移除与软删除均未执行。根因：config 模块重构后
  `c.config.get()` 返回纯数据对象 `Settings`，而 `delete_repo`
  仍把它当 `ConfigManager` 调用其不存在的 `update_repos` 方法
  （`AttributeError`），该端点自重构后即无法删除任何仓库。
  - 后端 `config.py`：`ConfigManager` 新增领域方法 `remove_repo`
    （project_id）——重读磁盘、过滤掉指定仓库、落盘并刷新内存
    settings（与 `update_repos` 等 update_* 系列同一模式，避免
    覆盖并发的手动编辑）；
  - 后端 `api/repos.py`：`delete_repo` 改为调用
    `c.config.remove_repo(row["gitlab_project_id"])`，删除链路的
    config 移除逻辑内聚到 ConfigManager；
  - 测试：TDD 先行（复现测试红灯 `AttributeError` 与生产日志一致），
    新增 API 用例 3（删除成功含 webhook 注销/config 移除/db 软删除、
    404、webhook 注销失败不阻塞）+ config 层用例 3（落盘与内存刷新、
    缺失 id 幂等、保留并发手动编辑），后端全量 646（+6）通过。

- **全局模版取消内层的垂直滚动，改成折叠的方式（issue #55）**：提示词
  模版页的 textarea 此前固定 18 行，全局默认模版（issue-agent 完整提示词）
  远超 18 行，内容在 textarea 内部滚动查看（内层垂直滚动条）。现改为折叠
  方式：默认折叠为 6 行小窗口（overflow hidden 裁剪、无滚动条），按钮
  「展开全部（N 行）」展开后 textarea 高度自适应内容行数、完整展示，
  滚动交给页面最外层（与 issue #52 交互原则一致）；「收起」恢复小窗口，
  内容不丢失。
  - 前端 `Templates.jsx`：新增 `expanded` 状态（默认折叠）；textarea
    `rows` 折叠态 6 / 展开态内容行数 +1，折叠态追加
    `template-collapsed` class；保存按钮行新增「展开全部（N 行）/收起」
    切换按钮（`aria-expanded` 同步）；
  - 前端 `styles.css`：新增 `.input.textarea.template-collapsed`
    （`overflow: hidden; resize: none`）；
  - 测试：新增 `templates-collapsible-editor.test.mjs` 3 用例（展开态
    rows 自适应 40 行内容 / 默认折叠 + 展开收起往返切换内容不丢失 /
    折叠态样式 overflow hidden）；`tests/helpers/mock-router.jsx` 补
    `Navigate` 导出（issue #54 引入后 vite 依赖扫描报 missing export）；
    前端全量 202 passed + 后端全量 591 passed + vite build 成功。

- **默认页面改到概览页面（issue #54）**：打开平台默认落在仓库页，现改为
  默认进入概览页。根路径 `/` 重定向到 `/overview`（replace 导航，后退键
  不会卡在重定向环），仓库页迁至 `/repos`，顶部导航「仓库」链接同步指向
  `/repos`。
  - 前端 `App.jsx`：路由 `/` 由 `<Repos />` 改为
    `<Navigate to="/overview" replace />`，新增 `/repos` 路由挂载仓库页，
    「仓库」NavLink `to` 由 `/` 改为 `/repos`；
  - 测试：新增 `app-default-page.test.mjs` 2 用例（访问 `/` 渲染概览页且
    不渲染仓库页 / `/repos` 渲染仓库页）；`overview-page.test.mjs` 导航
    断言随仓库页迁址更新；前端全量 199 passed + 后端全量 591 passed。

- **任务页面两侧有空白时任务列表不再出现水平滚动条（issue #53）**：1440~
  1919px 视口下任务列表出现水平滚动条，页面左右两侧却还有大量空白。根因：
  border-box 下任务表格可用宽度 = `--content-width` − 80px（.content/.card
  左右 padding），而 `.table.tasks-table` min-width 为 1360px（12 列宽度
  总和，issue #37），需要 `--content-width ≥ 1440px` 才装得下；此前媒体
  查询 ≥1600px 才放宽到 1360px，导致 1440~1919px 视口下表格可用宽度仅
  1280px，必然滚动。
  - 前端 `styles.css`：媒体查询断点由 `min-width: 1600px`（1360px）提前为
    `min-width: 1440px`（1440px）——该区间表格恰好无滚动；≥1920px 保持
    1600px 封顶；视口 <1440px 本身装不下表格，保持 .table-wrap 横向滚动
    （issue #28/#37 既有行为不变）；
  - 测试：`tasks-table-fit-content.test.mjs` 3 用例（解析 styles.css 断点
    与 min-width，模拟 1440/1500/1600/1750/1919/1920/2560 视口断言表格
    可用宽度 ≥ min-width、窄视口滚动保留）。

- **任务「用时」改为完整处理周期动态计算（issue #49）**：任务页「用时」
  此前显示执行时长（started_at → finished_at），排队等待时间不计入，
  且终点不是 bot-done 打标时刻。现改为「系统接收到该问题的时间 →
  系统给 issue 打上 bot-done 标记的时间」的动态计算，不落库时长字段。
  - 后端 `executor.py`：`_finish_succeeded` 打 bot-done 标签成功后把
    `finished_at` 更新为打标时刻（`finished_at` 语义 = bot-done 打标
    时间）；打标失败保留收尾时刻兜底；
  - 前端 `Tasks.jsx` / `TaskDetail.jsx`：用时起点由 `started_at ||
    created_at` 改为固定 `created_at`（系统接收时间），详情页字段名
    「执行用时」→「处理用时」；
  - 测试：`test_task_duration.py`（finished_at 不早于打标时刻 / 打标失败
    兜底）+ `tasks-duration-calculation.test.mjs`（列表与详情渲染断言
    40 分钟完整周期、源码不依赖 started_at）。

- **任务「用时」仍显示不正确：存量 CST 时间戳数据迁移为 UTC**（issue #49
  第二轮）：用户以任务 #65 验证——该任务 14:38:41（本地）创建、14:48:10 打
  bot-done，18:20 查看时页面显示用时 8 小时（实际 9 分 29 秒）。根因：
  550e04f（issue #42）部署前旧版 executor 用 `time.strftime()`（无 gmtime）
  按容器本地 CST 写 `started_at`/`finished_at` 无时区后缀串，与 `created_at`
  （SQLite `datetime('now')` UTC）及前端「按 UTC 解析」契约不一致；第一轮只
  修了新数据写入，存量 CST 串按 UTC 解析偏移 +8 小时（终点落在未来）。
  - 后端 `database.py`：启动迁移新增 v2 步骤（`PRAGMA user_version` 版本化）
    `_fix_legacy_cst_timestamps`——以 `task_logs.ts`（恒为 UTC）为参照逐字段
    判定：串按 UTC 解析后与任一日志差 ≤ 10 分钟 → 已是 UTC 不动（H_UTC 优先，
    排队 8 小时以上的任务首条日志恰在 t-8h 附近不会被误减）；否则解析结果减
    8 小时与任一日志差 ≤ 10 分钟 → CST 串，改写为减 8 小时后的 UTC 串；均不
    命中（无日志等）→ 保守不动；幂等（修正后与日志直接吻合，重复执行不再
    命中）；
  - 生产数据实测：78 个任务 152 个字段迁移后与日志全部吻合（任务 #65
    finished_at 14:48:10 → 06:48:10，页面用时恢复 9 分钟；一键停止写 UTC 的
    #47 与 550e04f 后的 #66+ 均保持不动）；
  - 测试：新增 `test_database_legacy_cst.py` 7 用例（CST 修正 / UTC 不动 /
    一键停止 UTC 特例不动 / 排队 8 小时不误判 / 无日志保守不动 / 幂等 /
    user_version 标记）。

- **概览页 CI/CD 流水线阶段顺序反转（sync→deploy→build）**（issue #44）：
  概览页流水线区块的 stage 展示顺序与 `.gitlab-ci.yml` 定义顺序相反
  （执行 build→deploy→sync，显示 sync→deploy→build）。根因：GitLab
  `GET /projects/:id/pipelines/:pipeline_id/jobs` 默认按 job id 倒序
  返回且不响应 `sort` 参数（已对生产实例实证），后端按 API 返回顺序
  聚合 stage，导致顺序反转。
  - 后端 `pipelines.py`：`aggregate_stages` 聚合前先按 job id 升序排序
    （job id 为全局自增序列，同一 pipeline 内升序即 job 创建顺序，与
    stage 定义顺序一致），不再依赖 API 返回顺序；docstring 同步修正。
  - 测试：`test_api_pipelines.py` 新增
    `test_aggregate_reorders_reversed_api_jobs`（用生产流水线 #744 的
    真实倒序数据复现）；后端全量 458 passed + 前端全量 147 passed。

- **任务页面「用时」显示不正确（多 8 小时）**（issue #42）：
  生产任务 #64 创建于 13:52:43（本地），14:56 查看时页面显示用时 8 小时 24 分钟，
  实际执行仅约 24 分钟——恰好多 8 小时。根因：容器 TZ=Asia/Shanghai，
  tasks 表时间字段时区混合存储——`created_at` 由 SQLite `datetime('now')`
  写 UTC 串，`started_at`/`finished_at` 由 executor `time.strftime` 写本地
  CST 串；前端 `fmtDuration` 统一按 UTC 解析（api.js 契约），当 `started_at`
  为 NULL 回退 `created_at` 时 UTC/CST 串混算，时长多 8 小时。
  - 后端 `executor.py`：4 处 `time.strftime("%Y-%m-%d %H:%M:%S")` 改为
    `time.strftime(..., time.gmtime())`，started_at/finished_at 统一写 UTC，
    与 created_at 及前端解析契约一致。
  - 测试：新增 `tests/test_task_timestamps.py` 4 用例（TZ=Asia/Shanghai 下
    复现 + 验证 `_finish_failed`/`_finish_stopped`/`run_task` 全流程时间戳
    均为 UTC + 前端视角 created_at 与 finished_at 混算回归）。
  - 附带修复 flaky 测试：`test_executor.py::_shorten_ci_timeouts` 的等待
    窗口 20ms 太短，高负载机器上 `db.add_log` 真实写 SQLite（实测 27ms）
    即可耗尽窗口导致 `TestWaitPipelineForCommit` 误报 timeout——窗口放大
    到 1s（sleep 已 mock 为 no-op，不拖慢测试），「永不终态」用例的
    statuses 迭代器改 `itertools.repeat` 防耗尽；后端全量 457 passed +
    前端全量 147 passed。

- **任务在 CI 流水线运行中即显示已完成 + 完成后未给 issue 打终态标签**（issue #40）：
  生产任务 #63（issue #39 第二轮）于 13:31:45 被平台标记 succeeded，而其提交
  d08e104 触发的流水线 #737 的 backend:test 到 13:32:17、sync_to_github 到
  13:48:34 才结束——成功判定只检查 claude exit 0，不等待流水线终态；且该任务
  收尾打 bot-done 时（13:31:45）恰逢自身 push 的代码触发 deploy job 执行
  pm2 delete 重启平台，`PUT /issues/39` 未发出进程即被杀死，issue #39 至今无
  bot-done/bot-failed 标签，会被 webhook/对账当作新任务重复领取。
  - 后端 `executor.py`：成功收尾前新增流水线等待——`_await_task_pipeline` 用
    `find_commit_for_issue` 拿任务提交 sha（查不到提交即不等待，仓库无 CI
    不影响成功）；`_wait_pipeline_for_commit` 在探测窗口（默认 120s）内找
    sha 匹配的最新流水线（GitLab 收到 push 即创建流水线记录），命中后轮询
    至 success/failed/canceled/skipped 终态（总上限默认 1800s，轮询间隔
    默认 15s）；流水线 failed/canceled → 任务判失败（打 bot-failed + 留失败
    评论），超时未终态 → 判失败，success/skipped/无匹配 → 成功收尾（打
    bot-done）；等待期间用户一键停止 → interrupted。
  - 后端 `reconciler.py`：终态标签对账兜底——每轮对账回看每仓库最近 20 条
    succeeded/failed 任务，issue 仍 open 且无 bot-done/bot-failed 时按任务
    结果补打对应标签（幂等，GitLab 报错仅记日志下轮再试），覆盖「收尾打标签
    被部署重启打断」的窗口。
  - 后端 `gitlab_client.py`：新增模块级常量 `PIPELINE_TERMINAL_STATES` 与
    `get_pipeline`（单条流水线详情，轮询终态用）；`config.py` worker 配置
    新增 `ci_wait_detect_seconds` / `ci_wait_interval_seconds` /
    `ci_wait_timeout_seconds`（settings API 可写，默认 120/15/1800）。
  - 测试：后端 `test_executor.py` 新增 17 用例（流水线 success 才判成功/
    failed 与 canceled 判失败并打 bot-failed/skipped 视为成功/无匹配流水线
    不等待/超时判失败/等待中停止判 interrupted/`_wait_pipeline_for_commit`
    轮询细节 6 例）+ `test_reconciler.py` 新增 5 用例（succeeded 补打
    bot-done/failed 补打 bot-failed/已有终态标签跳过/issue 已关闭跳过/
    补打失败不影响补入队）；后端全量 449 passed + 前端全量 147 passed。

### Added

- **设置页增加 AI API 供应商配置**（issue #46）：
  设置页新增「AI API 供应商」卡片，可增删改 AI 供应商配置（名称 / 供应商
  类型 / Base URL / API Key / 默认模型 / 启用开关），为后续 AI 功能消费做
  准备（本期纯配置存储，不接入实际调用）。内置 11 个预设供应商
  （DeepSeek / OpenAI / Anthropic / Google Gemini / Moonshot / 通义千问 /
  智谱 GLM / 硅基流动 / Ollama / OpenRouter / 自定义），选择预设自动填充
  默认 Base URL 与模型，每个供应商显示各自品牌 logo（而非统一图标）。
  - 后端：`config.py` Settings 新增 `ai_providers` 列表（config.yaml 落盘，
    与 repos / custom_labels 同为整体替换模式）与 `update_ai_providers`；
    `api/settings.py` GET 返回 `ai_providers` 段（api_key 只返回掩码，明文
    不流转到界面），PUT 校验（name 必填且不重复 / base_url 须 http(s) 开头 /
    enabled 布尔），api_key 回传掩码值或留空 = 保持现有（按 name 匹配旧
    配置，与 sso.client_secret 同模式），config.yaml 中 api_key 支持
    `${ENV}` 引用（凭据不落明文）。
  - 前端：新增 `providers.jsx`（预设清单 + 各供应商品牌色内联 SVG logo，
    未知 key 回退 custom 通用图标）与 `components/AiProvidersCard.jsx`
    （列表展示 + 增删改表单 + 卡片内独立保存按钮，只提交 ai_providers 段）；
    `Settings.jsx` 在 SSO 卡片后挂载；styles.css 新增 provider-* 样式。
  - 测试：后端 `test_api_settings.py` 新增 11 用例（持久化 / 掩码不回传 /
    掩码与留空保持现有 / 整体替换 / 清空 / name 必填与去重 / base_url 校验 /
    非数组拒绝 / ${ENV} 引用展开），后端全量 482 passed；前端新增
    `settings-ai-providers-card.test.mjs` 8 用例（卡片挂载 / 表单字段 /
    PUT ai_providers 段 / 预设清单 / logo 差异化 / 样式类），前端全量
    167 passed。

- **概览页流水线卡片显示最近流水线对应提交的提交时间**（issue #43）：
  概览页 CI/CD 流水线区块每张仓库卡片在分支 · sha 下方显示最近一次流水线
  对应提交的提交时间（绝对时间，沿用页面时区配置）与距今多久（相对时间：
  刚刚 / X 分钟前 / X 小时前 / X 天前 / X 个月前 / X 年前）。
  - 后端：`gitlab_client.py` 新增 `get_commit`（单条提交详情）；`pipelines.py`
    每条结果新增 `commit_time` 字段（committed_date 转 UTC 无后缀时间串，
    与 executor 落库格式一致），commit 查询失败 / 不存在（force-push 后
    sha 失效）/ 缺字段时静默降级为 None、不进 errors（时间仅为展示增强
    信息，不影响卡片其余部分）；无流水线仓库不查 commit 避免无效 API 调用。
  - 前端：`api.js` 新增 `fmtAgo` 相对时间纯函数（60 秒内与未来时间按
    「刚刚」，30 天/365 天为月/年档边界，now 参数可注入供测试）；概览页
    卡片渲染 `pipeline-commit-time` 节点（绝对+相对时间，commit_time 为
    null 时不渲染）；`styles.css` 新增节点样式。
  - 测试：后端 `test_api_pipelines.py` 新增 13 用例（时区转换纯函数 7 例
    + API 正常/故障静默降级/404 静默降级/无流水线跳过查询/pipeline 缺
    sha/commit 缺 committed_date 6 例）；前端新增
    `pipeline-commit-time.test.mjs` 12 用例（fmtAgo 各档位与边界 9 例 +
    渲染 2 例 + 源码/样式断言 1 例）；后端全量 471 passed + 前端全量
    159 passed。

- **标记库内置默认标签 need-verify + 领取任务过滤**（issue #41）：标记库默认
  清单新增流程/状态标签 `need-verify`（黄色 `#ffcc00`，语义「需要人工验证，
  bot 不领取」）；bot 领取任务（webhook 入队与对账补入队两处）时跳过带
  need-verify 标签的 issue——用户给需人工验证的 issue 打上该标签后，bot
  不再自动领取处理（沿用「以 API 最新标签为准」的既有过滤路径）。
  - 后端：`labels.py` 默认清单新增 need-verify（内置 14 个）并新增统一常量
    `CLAIM_SKIP_LABELS`（bot-done / bot-failed / need-verify）供领取过滤
    引用；`webhook.py` 与 `reconciler.py` 的入队过滤改用该常量（终态标签
    对账补打仍只看 bot-done / bot-failed，不受影响）。
  - 文档/工具：`docs/labels.md` 与 `scripts/sync_labels.py` 同步新增
    need-verify（GitLab 项目侧标签需运行 sync 脚本同步）。
  - 测试：后端 `test_webhook.py` 新增 need-verify 拒绝入队 2 例、
    `test_reconciler.py` 新增对账跳过 2 例、`test_api_labels.py` 默认清单
    数量与包含断言更新；后端全量 453 passed + 前端全量 147 passed。

- **概览页 CI/CD 流水线状态展示未启用仓库**（issue #39 第二轮）：流水线区块由
  「所有启用仓库」扩展为「所有配置仓库」——未启用（enabled=false）的仓库同样
  查询并展示最新流水线状态，卡片标题旁显示灰色「未启用」徽章（`badge-muted`）
  以便区分；未启用仓库无流水线时显示「暂无流水线」占位，查询失败与启用仓库
  一致进 `errors` 列表。
  - 后端：`_collect` 移除 enabled 过滤，每条结果新增 `enabled` 字段透传前端。
  - 前端：`Overview.jsx` 卡片渲染未启用徽章，区块副标题改为「所有配置仓库的
    最新流水线」。
  - 测试：后端 `test_api_pipelines.py` 更新停用仓库用例为「未启用仓库也返回并
    查询」（enabled 字段透传断言），新增未启用仓库有流水线/查询失败 2 例；
    前端 `overview-pipelines.test.mjs` 新增未启用徽章渲染（启用仓库不显示、
    灰色样式类）与未启用仓库流水线卡片渲染 2 例。

- **概览页 CI/CD 流水线状态**（issue #39）：概览页面新增「CI/CD 流水线」区块，
  展示所有启用仓库的最新一次流水线状态——整体是否完成、成功还是失败、运行到
  哪个阶段、还有哪些阶段；每仓库一张卡片，参考 GitLab CI/CD 的阶段图以横向
  stage 节点条展示（绿=成功/红=失败/蓝=运行中带脉冲动画/灰=待运行），节点悬停
  显示 stage 名与状态，卡片链接跳转 GitLab pipeline 页面；无流水线仓库显示
  「暂无流水线」占位。
  - 前端：`Overview.jsx` 新增流水线区块，独立慢轮询（15 秒，任务轮询仍为
    3 秒）；导出 `PIPELINE_STATUS_META`（流水线状态徽章映射，复用 status-*
    样式类）与 `stageClass()`（stage 状态→节点样式类，未知状态兜底 pending）；
    `styles.css` 新增 pipeline 卡片/节点样式（3 列网格、运行中脉冲动画）。
  - 后端：新增 `GET /api/pipelines/overview`，遍历所有启用仓库（停用跳过），
    取最新流水线（`GitLabClient.get_latest_pipeline`，无流水线 pipeline=null）
    与全部 jobs（`list_pipeline_jobs`），按 stage 聚合状态（`aggregate_stages`：
    failed（allow_failure 的失败不计）> running > pending 系列 > canceled >
    success，manual/skipped 不影响；stage 顺序 = jobs 首次出现顺序即
    .gitlab-ci.yml 定义顺序）；多仓库场景下单仓库失败不中断整体（HTTP 200），
    失败明细进 `errors` 列表（与 issue #38 对账一致）；结果带 10 秒 TTL 内存
    缓存，避免轮询打爆 GitLab API。
  - 测试：后端 `tests/test_api_pipelines.py` 19 用例（stage 聚合规则 10 例含
    allow_failure/优先级/空列表 + API 正常/无流水线/部分与全部仓库失败/jobs
    查询失败/停用跳过/无仓库/缓存命中与过期）+ 前端
    `tests/overview-pipelines.test.mjs` 11 用例（轮询源码断言/状态映射纯函数/
    组件渲染含 stage 节点样式与 GitLab 链接/无流水线与 errors 兜底/样式）。
    后端全量 429 passed + 前端全量 145 passed。

- **一键对账所有仓库**（issue #38）：任务页面筛选行新增「对账所有仓库」按钮，
  点击后同步扫描全部启用仓库，把「assignee 是 bot 但任务表无活跃记录」的
  open issues 补入任务队列，并即时展示扫描数/补入队数。
  - 前端：`Tasks.jsx` 筛选行「停止所有任务」旁新增对账按钮（低危操作无需
    `confirm`，请求中禁用防重复点击）；调 `POST /api/tasks/reconcile-all` 后
    显示绿色成功提示（扫描 X 个 issue、补入队 Y 个任务）并刷新列表；部分仓库
    失败时展示失败明细，接口失败显示错误。
  - 后端：新增 `POST /api/tasks/reconcile-all`，同步执行全量对账扫描
    （复用 `Reconciler.reconcile_once()`，与仓库页单仓库对账 issue #17 一致）
    并直接返回 `{ok, scanned, enqueued, errors}`；多仓库场景下单个仓库失败
    不中断整体（HTTP 200），失败明细放入 `errors` 列表返回。
  - 测试：后端 `tests/test_task_reconcile.py` 8 用例（全量入队/无漏单/幂等/
    终态标签跳过/部分与全部仓库失败/停用仓库/无仓库）+ 前端
    `tests/tasks-reconcile-all-button.test.mjs` 7 用例（源码断言/按钮渲染/
    成功提示/部分失败明细/接口失败）。后端全量 410 passed + 前端全量
    134 passed + build 通过。

- **手动重试任务**（issue #36）：任务页面操作列新增「重试」按钮（仅失败/已中断
  任务显示），点击确认后任务重新入队执行——失败相关字段清空、尝试次数归零、
  来源标记「手动」，保留 claude 会话断点续跑（接续上次进度继续处理）。
  - 前端：`Tasks.jsx` 失败/中断任务行渲染「重试」按钮（`btn-gap-left` 与「执行」
    按钮分隔，请求中禁用防重复点击），点击需 `window.confirm` 确认防误触；调
    `POST /api/tasks/{id}/retry` 后显示绿色成功提示并刷新列表，失败显示错误；
    来源列支持「手动」展示。
  - 后端：新增 `POST /api/tasks/{task_id}/retry`（404 不存在 / 400 仅失败与
    中断可重试 / 409 同 issue 已有活跃任务 / 200 成功入队）；数据库
    `retry_task()` 条件更新重置失败字段（`triggered_by='manual'`，保留
    `claude_session_id` 与 `log_path`），同事务检查活跃任务去重防撞唯一索引，
    条件 UPDATE 兜底并发；成功后交调度器 `enqueue` 重新派发。
  - 测试：后端 `tests/test_task_retry.py` 12 用例（db 重置/拒绝/冲突/日志/
    重复重试 + API 契约/入队/404/400/409）+ 前端 `tests/tasks-retry-button.test.mjs`
    7 用例（源码断言/按钮渲染条件/确认流/成功失败提示/请求中禁用）。
    后端全量 402 passed + 前端全量 117 passed + build 通过。

- **一键停止所有任务**（issue #35）：任务页面新增「停止所有任务」危险按钮（显示当前
  活跃任务数），一键把排队/执行/重试中的任务全部标记为已中断（interrupted 终态），
  执行中的 claude 进程组被强制终止；被停止的任务不会在平台重启后自动恢复执行。
  - 前端：`Tasks.jsx` 筛选行新增 `btn-danger` 按钮（无活跃任务或请求中禁用），
    点击需 `window.confirm` 确认防误触；调 `POST /api/tasks/stop-all` 后显示
    绿色成功提示（已停止 N 个任务）并刷新列表，失败显示错误。
  - 后端：新增 `POST /api/tasks/stop-all`（返回 `{stopped, count}`）；调度器
    `stop_all()` 清空待派发队列并终止运行中任务；执行器维护运行中进程注册表 +
    停止请求集合（`request_stop` SIGKILL 进程组，`_run_once` 读循环轮询感知停止
    返回约定退出码 125，`run_task` 收到停止请求后不再执行/重试）；数据库
    `stop_active_tasks()` 统一落 interrupted 终态 + 错误信息 + warn 日志。
  - 测试：后端 `tests/test_task_stop.py` 11 用例（db 落库/调度清队列/进程终止/
    停止收尾/API 契约）+ 前端 `tests/stop-all-button.test.mjs` 8 用例（按钮
    禁用条件/确认流/成功失败提示，`tests/helpers/mock-router.jsx` mock
    react-router-dom 供 node --test 渲染）。
    后端全量 390 passed + 前端全量 110 passed + build 通过。

- **概览页面**（issue #32）：导航栏新增「概览」tab（位于「仓库」tab 左边），实时展示
  正在执行的任务（running + retrying）卡片：仓库名称、对应 issue（GitLab 链接）、
  agent 实时输出（每 3 秒轮询增量刷新、卡片内滚动跟随最新）。多任务以网格排布，
  最多 2 行 × 3 列（6 个卡片），超过 6 个保持 3 列自动换行、页面滚动；无任务时
  显示空状态。
  - 前端：新增 `frontend/src/pages/Overview.jsx`（`/overview` 路由 + 导航入口）；
    列表一次拉取活跃任务（多值 status 过滤），每个任务独立轮询
    `/api/tasks/{id}/execution?after_byte=` 增量续读日志（`trimLogTail` 截尾防
    卡片无限增长）；`styles.css` 新增 `overview-grid` 3 列网格样式。
  - 后端：`GET /api/tasks` 的 `status` 参数支持逗号分隔多值（如
    `running,retrying`，单值行为不变）；任务列表/详情新增 `issue_url` 字段
    （后端按仓库 URL 拼接 `/issues/<iid>`，前端零拼接）。
  - 测试：后端 `tests/test_api_tasks.py` 新增 8 用例（多值过滤/边界 400/
    issue_url 契约）+ 前端 `tests/overview-page.test.mjs` 14 用例（导航顺序/
    数据流断言/trimLogTail 边界/组件渲染含字段缺失与 API 失败兜底）。
    后端全量 361 passed + 前端全量 83 passed + build 通过。

- **标记库页面**（issue #29 第二轮）：用户澄清需求为「在 botler 项目上增加一个标记库，
  用户可以手动添加删除，上面的建议清单作为默认选项不可删除」——第一轮只交付了
  `docs/labels.md` + 同步脚本（无 UI），本轮补齐 Web UI 管理页面。
  - 新增「标记库」页面（导航入口 + `/labels` 路由）：默认清单 13 个标签
    （类型 8 + 流程 5，与 `docs/labels.md` 一致）展示为内置选项、标记「默认」徽标
    **不可删除**；用户可添加/删除自定义标签（名称/颜色/说明，名称与默认清单或
    已有自定义标签重复、格式非法均被拒绝）。
  - 后端：新增 `botler/labels.py`（默认清单 + 校验）、`api/labels.py`
    （GET 列表 / POST 添加 / DELETE 删除，默认标签删除返回 400）；
    `config.py` 新增 `labels.custom` 段（`update_custom_labels` 写盘前重读磁盘，
    与其它 update_* 一致）。
  - 测试：后端 `tests/test_api_labels.py` 12 用例（含默认清单与
    `scripts/sync_labels.py` 一致性校验）+ 前端 `tests/labels-page.test.mjs` 7 用例
    （页面结构/接口路径/样式类静态断言）。后端全量 345 passed + 前端全量 60 passed
    + build 通过。

- **标记库规范**（issue #29）：统一 chenkaidi 名下全部仓库（botler / shipyard / patio /
  graph2plan / daymark / glimmer）的 issue 标签，解决命名不统一（shipyard 用 `ci`、
  daymark 用 `gitlab-ci`）与部分仓库缺标签的问题。
  - 新增 `docs/labels.md` 标记库规范：类型标签 8 个（bug / feature / optimize / ui /
    docs / test / gitlab-ci / chore）+ 流程标签 5 个（in-progress / review / blocked /
    bot-done / bot-failed），含颜色、说明与使用约定。
  - 新增 `scripts/sync_labels.py` 批量同步脚本：枚举命名空间下全部项目，创建缺失
    标签、更新已有标签的颜色/描述，**不删除任何旧标签**（旧 `ci` 等保留）。
  - 已通过脚本将 13 个标签同步至全部 6 个仓库（创建 48 / 更新 30，重跑幂等验证
    全部"不变"）。

- **Synology SSO 登录**（issue #27）：接入群晖 SSO Server（OIDC / OAuth2 授权码
  模式）作为管理界面登录身份源。设置页新增「Synology SSO 登录」卡片（Well-known
  URL / Application ID / Secret / Scope / 登录有效期 / 回调地址 / 证书校验开关），
  启用后访问 Web UI 需用群晖账号登录（未启用时保持开放访问），会话为签名 cookie
  （HMAC-SHA256，默认 7 天，密钥懒生成于 `backend/data/session_secret.key`，
  Docker 部署由 compose 挂载持久化）。
  - 后端：新增 `botler/auth.py`（`OidcClient` discovery/token/userinfo、
    `create_session`/`verify_session` 签名会话、`SsoGuardMiddleware` 保护
    `/api/*`——登录流程与健康检查除外，webhook 天然放行）；新增 `api/auth.py`
    （`/api/auth/status|login|callback|logout|me`）；`config.py` 新增 `sso` 段
    （白名单 + `update_sso`，掩码 secret 不覆盖）；`main.py` 挂载中间件与路由。
  - 前端：新增 `pages/Login.jsx` 登录页；`App.jsx` 启动探测登录状态、未登录渲染
    登录页、导航栏显示当前账号与退出；`Settings.jsx` 新增 SSO 配置卡片；
    `api.js` 401 兜底跳登录页。
  - 文档：README 新增「Synology SSO 登录」小节与配置表；
    新增 `docs/Synology-SSO-配置指南.md`（群晖侧创建 OIDC 应用步骤 + Botler 侧
    配置 + 常见问题）。
  - 测试：新增 `tests/test_auth.py` 19 个用例（OIDC 全流程 mock：登录 302 参数、
    state 防回放、换 token/userinfo、签名校验、篡改/过期 cookie、401 保护、
    webhook/health 放行）+ `test_api_settings.py` 追加 sso 段 6 个用例。
    后端全量 330 passed + 前端 46 passed + build 通过。

### Changed

- **版本与构建时间显示移入设置页底部 + 登录用户信息放导航栏最右**（issue #9 第二轮）：
  用户反馈调整：版本号与构建时间不再占用导航栏右侧，改为设置页面底部「版本信息」
  卡片展示；登录后用户名称与退出按钮位于导航栏最右侧。
  - 前端：`App.jsx` 导航栏移除 `VersionBadge`（及 import），`user-chip` 承接
    原版本徽标"推到最右"职责；`Settings.jsx` 页面底部新增「版本信息」卡片
    （复用 `VersionBadge` 组件，构建产物 `version.json` 注入，开发模式无该
    文件时静默隐藏）；`styles.css` 移除 `.version-badge` 的 `margin-left:auto`
    （导航栏专属布局不再适用），新增 `.settings-version .version-badge` 放大
    字号跟随正文字色、`.user-chip` 增加 `margin-left:auto`。
  - 测试：前端 `tests/version-badge-settings-page.test.mjs` 4 用例（导航栏
    无版本徽标/设置页渲染/导航栏末尾为用户信息/user-chip 推右样式）。
    后端全量 402 passed + 前端全量 121 passed + build 通过。

- **SSO 配置指南直接显示在设置页 + 提示文字优化**（issue #27 第六轮）：
  平台使用者看不到代码仓库里的本地文档 `docs/Synology-SSO-配置指南.md`，
  SSO 卡片说明此前指向该本地文件路径（部署环境中不存在）。改为设置页
  从后端拉取指南 Markdown 并渲染展示，文档内容与 `docs/` 单一来源同步；
  同时修正登录有效期默认值 7 → 30 天（用户第三轮已确认 30 天，历史实现偏差）。
  - 后端：`GET /api/settings/sso-guide` 读取 `docs/Synology-SSO-配置指南.md`
    返回 Markdown 原文（文件缺失 404 降级，SSO 启用时同受登录保护）；
    `config.py` `sso_session_days` 默认值 7 → 30。
  - 前端：新增 `components/Markdown.jsx` 轻量渲染器（标题/段落/围栏代码块/
    嵌套列表/表格/引用块/行内粗体、代码、链接，全部走 React 文本节点天然
    防 XSS，无第三方依赖）；设置页 SSO 卡片内新增「查看 SSO 配置指南」折叠区
    （默认收起，点击展开渲染文档）；卡片说明文字去掉本地 docs/ 路径指向、
    改为指向页面内指南；`session_days` 输入框 fallback 7 → 30。
  - 文档：`docs/Synology-SSO-配置指南.md` 同步默认有效期 30 天（两处）。
  - 测试：后端 `tests/test_api_sso_guide.py` 4 用例（内容契约/文件缺失 404/
    SSO 保护/默认 30 天）+ 前端 `tests/markdown.test.mjs` 11 用例（各语法/
    空值边界/XSS 转义/长文档混合）与 `tests/settings-sso-guide.test.mjs`
    4 用例（不指向本地路径/折叠区/默认 30/渲染交互）。后端全量 379 passed +
    前端全量 102 passed + build 通过。

### Fixed

- **任务列表三列换行：页面宽度足够时「创建时间/用时/操作」数据仍折成两行**（issue #37）：
  任务表格 12 列固定宽度分配失衡——创建时间 135px（内容 `2026-08-13 12:06:19`
  需约 147px）、用时 60px（`12 天 23 小时` 需约 105px）、操作 65px（「执行」+「重试」
  两按钮需约 136px）均放不下单行内容，浏览器在空格处折行；而标题（30%）与失败
  原因（22%）两列在宽视口下吃掉富余空间，形成「页面两侧有空白、三列却换行」。
  - 前端：`styles.css` 三列加宽至 165/120/145px 并新增 `white-space: nowrap`
    规则（含空格内容恒为一行）；标题/失败原因列由百分比改为固定 254/140px
    （长文本仍由 ellipsis 截断 + title 悬浮 + 详情弹窗补偿）；提交列 104→90px
    （8 字符 shortSha 富余让出）；任务表格新增 `min-width: 1360px`（= 12 列总和）——
    宽视口（≥1360px）表格撑满容器、富余空间按列宽比例分配不换行，窄视口保持
    `.table-wrap` 横向滚动（issue #28 既有行为）。
  - 测试：前端 `tests/tasks-time-duration-actions-nowrap.test.mjs` 6 用例（三列
    最小列宽/nowrap 规则/min-width 与列宽总和一致/总和 ≤1600px 宽视口不滚动回归
    保护）。后端全量 402 passed + 前端全量 127 passed + build 通过。

- **领取任务重复入队：缺少「最后发言人」判断、bot-done 依赖 Claude 手打**（issue #34）：
  bot 提问或处理完留评论后，用户仅重新指派（无新回复）触发 webhook，或平台重启
  后对账扫描，都会把「最后发言人是 bot、等用户回复」的 issue 重复领取入队；
  且成功任务的 bot-done 标签依赖 Claude 按模板自行打——Claude 忘打时 issue 无
  终态标签，也会被重复领取。
  - 后端：`gitlab_client.py` 新增 `last_note_author_id`（取最后一条非系统评论的
    作者 id，系统事件不算「发言」，无发言返回 None）；`webhook.py` 与
    `reconciler.py` 在终态标签过滤之后新增该判定——最后发言人是 bot 时不入队，
    用户回复后（或新任务无评论）才领取；`executor.py` 成功收尾时由平台代码直接
    打 bot-done（幂等，打标签失败仅记 warn 不阻塞任务成功），模板同步改为
    「平台自动打标签」。
  - 测试：`test_gitlab_client.py` 新增 5 用例（排除系统评论/无评论/无 author
    字段容错）、`test_webhook.py` 与 `test_reconciler.py` 各新增 3 用例（bot 最后
    发言拒绝/用户最后发言放行/无评论放行）、`test_executor.py` 新增 3 用例
    （成功打 bot-done/打标签失败仍成功/条件终态跳过不打），并更新成功判定用例
    断言（成功路径由代码打 bot-done）。后端全量 375 passed + 前端全量 87 passed。

- **任务页面「操作」列表头与「执行」按钮左边缘不在同一竖线**（issue #33）：
  操作列单元格内容「执行」链接复用了 `.btn-mini`，而该 class 带
  `margin-left: 8px`（原为「详情」按钮与前置失败原因文字留间距而设）——
  表头文字与单元格内容都从相同的左右 padding（10px）处开始排布，
  「执行」按钮的 8px 左外边距使其左边缘比「操作」文字右移 8px，
  两者不在同一竖线上。
  - 修复：`.btn-mini` 移除 `margin-left`；「详情」按钮改用新增的
    `.btn-gap-left { margin-left: 8px }` 显式声明与前置文字的间距
    （通用 class 不再携带上下文相关的左外边距）。
  - 涉及 `frontend/src/styles.css`、`frontend/src/pages/Tasks.jsx`；新增复现
    测试 `tests/tasks-action-col-align.test.mjs` 3 用例（含「详情」按钮间距
    守卫断言，修复前「执行按钮左外边距合计 8px ≠ 0」稳定失败）。
    前端全量 87 passed + 后端全量 361 passed（零改动）。

- **测试通知按钮多次点击只有第一次弹出系统通知**（issue #21 第四轮）：
  设置页「弹出测试通知」使用固定 `tag: 'botler-test'` 构造通知——浏览器通知
  中心对相同 tag 的通知做「替换」而非「新弹」：第一条通知还在屏幕上时，后续
  同 tag 通知只更新旧条目、不触发新的弹出动画，用户看到的就是「后续点击没有
  反应」。
  - 修复：`sendTestNotification()` 的 tag 改为每次唯一（模块级自增序号
    `botler-test-<n>`），每次点击都是独立通知、必然弹出；仍保留 tag 机制
    （同一瞬间重复点击不堆积）。
  - 涉及 `frontend/src/notify.js`；新增复现测试 `tests/notify.test.mjs`
    「连续多次点击每次都独立弹出」用例（mock 浏览器通知中心同 tag 替换语义，
    修复前失败 `1 !== 3`）。前端全量 84 passed + 后端全量 361 passed（零改动）。

- **任务页面「尝试」列数值与「恢复」文字不在同一水平线上**（issue #31）：
  任务列表「尝试」列单元格内混排纯文本数值 `attempt_count`（继承 body 14px /
  line-height 1.6）与恢复任务的 `badge.resume`（inline-block，11px，padding
  1px 8px，border 1px）——`.badge` 默认 `vertical-align: baseline`，基线对齐下
  胶囊的 padding/border 下沉到基线以下、小字号视觉中心低于数值中心，两者不在
  同一水平线上。
  - 修复：`.badge.resume` 增加 `vertical-align: middle`（与 `h1 .badge` 的既有
    处理方式一致），胶囊与数值垂直居中对齐。
  - 涉及 `frontend/src/styles.css`；新增复现测试
    `tests/tasks-attempt-badge-align.test.mjs` 2 用例（修复前 1 个稳定失败）。
    前端全量 68 passed + 后端全量 353 passed。

- **任务页面「尝试」列数值与「恢复」文字不在同一水平线（第二轮）**（issue #31）：
  用户反馈第一轮修复后界面仍不正确——真实浏览器实测（Chromium 1280 视口）数字
  中心 y=78、badge 中心 y=102，垂直中心差 24px。根因是 `.tasks-table` 为
  `table-layout: fixed`，「尝试」列固定 68px（td padding 10px → 内容区仅 48px），
  放不下「数值（2 位约 16px）+ 空格（约 4px）+ margin-left 6px + badge
  （40px）」约 66px，`inline-block` badge 被整体折行到第二行——数字在上、恢复
  在下；`vertical-align` 只作用于同一行框，折行后第一轮修复完全无效。
  - 修复：「尝试」列 68px → 88px（内容区 68px ≥ 同行所需），badge 与数值同处
    一行，配合 `vertical-align: middle` 垂直居中对齐；修复后实测垂直中心差
    1.3px。固定列总宽 +20px 由第 4/8 弹性列（30%/22%）吸收，宽视口下无感。
  - 涉及 `frontend/src/styles.css`；复现测试
    `tests/tasks-attempt-badge-align.test.mjs` 新增「列宽足够容纳同行显示」
    静态计算用例（修复前失败，48px < 64px）。
    前端全量 69 passed + 后端全量 353 passed。

- **配置（启用）Synology SSO 后页面无限刷新**（issue #27 第五轮）：`App.jsx` 的
  「时区加载」effect 无条件请求 `/api/settings`（未像通知轮询那样加 auth 守卫）——
  SSO 启用后（配置即时生效）未登录访问该接口被 `SsoGuardMiddleware` 返回 401，
  `api.js` 的 401 兜底跳 `/login` 整页重载；重载后渲染登录页时该 effect 再次无条件
  发起 `/api/settings` → 又 401 → 又重载，**无限刷新循环**（页面停在登录页仍不停刷新）。
  - 修复（双保险）：① `App.jsx` 时区加载 effect 增加 auth 守卫（SSO 启用未登录时
    跳过，登录成功后 auth 变化重新触发加载），登录页不再发起任何受保护请求；
    ② `api.js` 401 兜底增加 SSO 启用判断（新增 `setSsoEnabled()` 由 App 从
    `/api/auth/status` 探测后设置）——仅 SSO 启用时未登录访问才跳登录页，非 SSO
    场景（或探测完成前）的 401 不跳转，杜绝类似循环。
  - 涉及 `frontend/src/App.jsx`、`frontend/src/api.js`。
  - 测试：新增 `tests/app-sso-refresh-loop.test.mjs`（模拟后端 SSO 启用未登录 +
    其余 API 401，断言登录页不得发起受保护请求/不得触发跳转；修复前稳定复现
    `/api/settings` 请求）。前端全量 66 passed + build 通过 + 后端全量 353 passed。

- **设置页 SSO 配置卡片无保存按钮**（issue #27 第四轮）：Synology SSO 登录卡片
  （设置页第一个卡片）内只有表单字段，全局「保存」按钮位于下方「任务调度」卡片中
  ——用户首屏只看到 SSO 卡片，找不到保存按钮，误以为无法保存配置。
  - 修复：SSO 卡片内新增独立「保存 SSO 配置」按钮（只提交 `sso` 段，后端
    `PUT /api/settings` 支持部分更新，不影响其他设置）；sso 段构建逻辑提取为
    `buildSsoPatch()` 供全局 save 与卡片内 saveSso 共用（client_secret 留空 =
    保持现有凭据）；卡片说明文字改为指向卡片内按钮。
  - 涉及 `frontend/src/pages/Settings.jsx`；新增前端测试
    `tests/settings-sso-save-button.test.mjs` 5 用例（修复前 4 个稳定失败）。

- **agent 重复处理已打 bot-done / bot-failed 的 issue**（issue #30）：对账兜底
  （reconciler）与 webhook 入队只按「assignee 是 bot + issue 未关闭」判定，不检查
  标签——平台重启、任务表清理或手动「对账」后，已完成（bot-done，等用户确认关闭）
  的 issue 会被重复入队重新开发；处理失败（bot-failed，需人工介入）的 issue 因失败
  任务不再活跃，会被再次领取→再失败→再入队，**无限重试循环**。
  - 修复：两条入队路径统一过滤终态标签——`reconciler.reconcile_once` 跳过带
    bot-done / bot-failed 标签的 issue（新增模块级常量 `TERMINAL_LABELS`）；
    `webhook` 入队前一律以 API 最新状态确认 assignee 与标签（事件快照 labels 格式
    不可靠，快照干净但 API 已打 bot-done 时同样拒绝），打 bot-done / bot-failed 的
    issue 返回「跳过」不创建任务。
  - 涉及 `backend/botler/reconciler.py`、`backend/botler/webhook.py`。
  - 测试：新增 `tests/test_reconciler.py`（4 用例：bot-done / bot-failed 跳过、
    混合队列只入队干净 issue、普通 issue 回归）+ `tests/test_webhook.py`
    （4 用例：bot-done / bot-failed 拒绝、快照标签不可靠以 API 为准、干净 issue
    回归）。修复前 6 个过滤用例稳定失败。后端全量 353 passed + 前端全量 60 passed。

- **线上前端静默回退旧版，标记库页面「看不到」**（issue #29 第三轮）：`sync_to_github`
  job 未声明 `dependencies`，runner 默认下载前序 stage 中 frontend:build 的 dist
  artifacts 并解压到构建目录，而 pm2 进程（FastAPI 静态托管）的工作目录就在构建目录。
  旧 pipeline 的 sync job 若延迟排队运行（实测 #674 的 sync 在 #675 部署成功 4 分钟后
  才执行，把 #675 部署的新版 dist/index.html 覆盖回 #674 旧版），线上前端即被静默
  降级——CI 显示 success、后端 API 正常，但用户看不到新功能页面。
  - 修复：`sync_to_github` 添加 `dependencies: []`（该 job 用完整克隆到 /tmp 工作，
    不需要任何构建产物），杜绝旧 artifacts 解压覆盖 dist。
  - 涉及 `.gitlab-ci.yml`；本修复推送触发的新流水线将重新部署新版 dist 恢复线上。

- **任务完成后 issue 被自动关闭，模版库「不关闭」规范不生效**（issue #25 第二轮）：
  运行进程已加载新版全局模版（含「任务完成后不关闭 issue，打 bot-done 等用户确认」），
  但 `executor.run_task` 的成功判定仍是「issue 已被关闭」——exit 0 且 issue 仍打开时
  判失败并重试（最多 2 次），**迫使 Claude 在完成任务后违规调用 API 关闭 issue**
  （生产日志 task_30/31：issue #28 完成开发即被关闭，与用户 20:37 反馈的现象吻合）。
  - 修复：成功判定改为「Claude 正常完成（exit 0 且输出为 JSON result，非『无法解决』
    报告）」即成功，**issue 是否关闭不再参与判定**；「无法解决」仍为失败终态不重试。
  - 同步更新 `config.py` 的 `DEFAULT_TEMPLATE` 兜底模版：删除「调用 API 关闭 issue」
    指令，改为「留结果评论 + 打 bot-done，关闭留给用户确认后手动执行」。
  - 涉及 `backend/botler/executor.py`、`backend/botler/config.py`、
    `backend/botler/gitlab_client.py`（docstring）。
  - 测试：新增 `TestRunTaskSuccessCriteria` 3 用例（修复前 `test_success_when_issue_
    stays_open` 稳定失败：exit 0 + 完成输出 + issue 未关闭 → 旧逻辑重试耗尽判失败）。
    后端全量 333 passed + 前端全量通过。

- **设置页找不到 Synology SSO 配置入口**（issue #27 第三轮）：SSO 配置卡片位于设置页
  第 4 位（实测 1440×1000 视口下在第 1267px 处），被 577px 高的「网页通知」卡片压到
  首屏（1000px）之下，用户打开设置页首屏只能看到「任务调度/界面显示/网页通知」三个
  卡片，误以为没有 SSO 配置功能。
  - 修复：将「Synology SSO 登录」卡片移到设置页**顶部第一位**（首屏顶部直接可见，
    top 76px），其余卡片顺序不变。
  - 涉及 `frontend/src/pages/Settings.jsx`。
  - 测试：新增 `tests/settings-sso-card-order.test.mjs`（断言 SSO 卡片是设置页第一个
    卡片、位于「任务调度/网页通知」之前；修复前该测试稳定失败）+ chromium 无头实测
    SSO 卡片首屏可见。前端全量 53 passed + 后端全量 330 passed + build 通过。

- **任务列表页面宽度缩小时数据行内容超出白色底**（issue #28）：任务列表为 12 列表格，
  各列内容最小宽度之和约 1306px，窄视口下 `.table` 的 min-content 宽度超过 `.card`
  容器可用宽度，表格直接撑破白色卡片（浏览器实测 1400px 视口溢出 266px，760px 视口
  超出视口 586px），`.card` 无 overflow 处理导致行内容画出卡片边缘。
  - 修复：表格外包一层 `.table-wrap` 滚动容器（`overflow-x: auto`），窄视口下表格在
    容器内横向滚动查看全部列，不再溢出卡片、页面不再整体横向滚动。
  - 涉及 `frontend/src/pages/Tasks.jsx`、`frontend/src/styles.css`。
  - 测试：新增 `tests/table-wrap.test.mjs`（断言 `.table-wrap` 滚动规则存在 + 任务
    表格被包裹；修复前该测试稳定失败）+ chromium 无头实测 1400/1100/900/760 四档
    视口全部无溢出。前端全量 49 passed + 后端全量 330 passed + build 通过。

- **页面宽度足够时任务列表仍出现水平滚动条**（issue #28 第二轮）：`.table` 为
  `table-layout: auto`，实际宽度被内容撑到 min-content（实测 1628px——≥1600px
  视口下标题/失败原因两个 ellipsis 列各占 520px，其余 10 列共约 625px），而
  `.content` 的 `--content-width` 封顶 1600px，`.table-wrap` 可用宽度最多 1520px，
  任何桌面视口都装不下表格，滚动条必然出现（浏览器实测 1920px 视口溢出 108px、
  1750px 溢出 348px，页面两侧却仍有空白）。
  - 修复：任务表格启用 `table-layout: fixed` 固定布局（专用类
  `.table.tasks-table`）+ 12 列显式分配宽度（固定列合计 800px，标题/失败原因列为
  弹性 30%/22%），表格宽度恒等于容器宽度——宽视口下撑满容器不再滚动，窄视口列宽随
  容器压缩、长文本由 ellipsis 列截断（仓库/提交列同步补上 ellipsis + title），
  760px 以下极小视口保留容器内横向滚动兜底。
  - 涉及 `frontend/src/pages/Tasks.jsx`、`frontend/src/styles.css`。
  - 测试：`tests/table-wrap.test.mjs` 追加 2 个断言（`.table.tasks-table` 必须含
    `table-layout: fixed` + 12 条 `th:nth-child(n)` 列宽规则；修复前稳定失败）。
    chromium 无头实测 1920/1750/1600/1500/1366/1100/900 七档视口滚动条全部消失
    （溢出 0），760px 保持滚动。前端全量 51 passed + 后端全量 330 passed + build
    通过。

- **启用 SSO 后 Web UI 打开白屏**（issue #27 第二轮）：`App.jsx` 中网页通知轮询的
  `useEffect` 被声明在两个条件 `return`（auth 检测中、SSO 启用未登录）之后——auth
  加载前后组件执行的 hooks 数量不一致（4 → 5），触发 React error #310
  「Rendered more hooks than during the previous render」，整棵组件树崩溃白屏
  （生产构建下即控制台的 Minified React error #310）。
  - 修复：通知轮询 effect 移到所有条件 `return` 之前声明（hooks 数量恒定），effect
    内部按 auth 状态跳过启动——auth 未就绪或 SSO 启用未登录时不轮询（同时避免登录页
    每 10s 轮询受保护接口 401 反复刷新）。
  - 涉及 `frontend/src/App.jsx`。
  - 测试：新增 `tests/app-hooks.test.mjs`（vite SSR 转译 + react-test-renderer 渲染
    App，驱动 auth 状态流转，断言主界面正常出现；修复前该测试稳定复现 #310）。
    新增 devDependency `react-test-renderer@18.3.1`。前端全量 47 passed + 后端
    全量 330 passed + build 通过。

- **手动编辑 config.yaml 后模板不生效 / 被 UI 保存覆盖**（issue #25）：`ConfigManager`
  只在进程启动时加载一次 config.yaml 并缓存——用户直接编辑 config.yaml（文档约定它
  是唯一事实来源）修改全局模板后，运行中的 botler 仍用旧模板渲染（"修改了全局模版，
  但是没有生效"）；更糟的是之后在 Web UI 保存任意设置，`save()` 用内存旧值整体覆盖
  写回 config.yaml，手动修改被静默丢弃。
  - 修复：`get()` 增加磁盘 mtime 检测，config.yaml 被外部修改后自动重载（无需重启、
  无需再走 Web UI）；所有 `update_*` 写盘前重新读取磁盘、以磁盘最新内容为基底，UI
  保存不再覆盖手动编辑；`update_worker`/`update_claude`/`update_default_template`/
  `update_repos` 此前未先加载 `_data` 就写盘（未 load 时保存会写坏整个配置），统一
  加 `_reload_from_disk()` 保护；磁盘文件缺失/损坏时降级保留当前配置不中断。
  - 涉及 `backend/botler/config.py`（`_loaded_mtime`、`_reload_from_disk`、`get()`
    mtime 检测、全部 `update_*`）。
  - 测试：新增 `tests/test_config_reload.py` 6 个用例（手动编辑后 `get()` 自动生效、
    连续 `get()` 稳定、UI 保存保留手动编辑、UI 保存模板本身回归、损坏/缺失文件降级）。
    后端全量 307 passed + 前端 46 passed + build 通过。

### Added

- **添加 MIT License**（issue #26）：新增 `LICENSE` 文件（MIT 协议，© 2026
  chenkaidi），README 末尾补充 License 小节。纯文档改动，不涉及代码。
- **任务页面显示完成 issue 所用时长**（issue #23）：任务列表新增「用时」列、
  任务详情新增「执行用时」行——从任务开始执行（`started_at`，未开始则退回
  创建时间）到完成（`finished_at`）的耗时，自动换算为 秒/分钟/小时/天；
  未完成或时间缺失显示占位符。纯前端实现：`api.js` 新增 `fmtDuration()`
  （与 `fmtTime` 同规则解析后端 UTC 时间串，非法日期/结束早于开始返回 null）。
  - 涉及 `frontend/src/api.js`（新增 `fmtDuration`）、`pages/Tasks.jsx`
    （新增「用时」列）、`pages/TaskDetail.jsx`（新增「执行用时」行）。
  - 测试：前端新增 11 个用例（秒/分钟/小时/天换算、取整、0 秒、缺字段、
    非法日期、时钟异常负值）。前端全量 46 passed + 后端 289 passed
    （零改动）+ build 通过。
- **设置页增加本地环境检测功能**（issue #22）：新增「本地环境检测」卡片，检测
  botler 服务器上常见 AI agent（claude/codex/gemini/aider/gh）与基础工具
  （git/docker/node/npm/python3/uv）是否安装、已装版本与最新版本；最新版本
  已安装且带发布源的工具并发查询（npm registry / GitHub API，网络不可达显示
  "—"），版本落后高亮提示「可升级」。进入设置页自动检测，可点「重新检测」刷新。
  - 涉及 `backend/botler/environment.py`（新增：工具清单 + which/`--version`
    版本解析 + 最新版本查询，线程池并发 + 整体超时，全部容错）、
    `backend/botler/api/environment.py`（新增 `GET /api/environment`）、
    `backend/botler/api/__init__.py`（注册路由）、`frontend/src/pages/
    Settings.jsx`（新增「本地环境检测」卡片）、`README.md`（API 一览）。
  - 测试：后端新增 28 个用例（版本解析 10 个：v 前缀/文本前缀/多行/无版本/
    空值/4 段取前三；工具检测 7 个：未安装/版本读取失败/超时/异常；最新版本
    查询 8 个：npm/GitHub 正常、404/网络异常/非法 JSON/缺字段/无发布源/未知
    源；整体检测 3 个：全流程/版本一致/网络失败）。后端全量 289 passed +
    前端 30 passed + build 通过。
- **设置页「弹出测试通知」按钮**（issue #21 增量）：通知卡片新增「测试通知」行，
  点击立即弹出一条浏览器系统通知（标题「Botler 测试通知」），用于验证网页通知
  功能是否正常。纯前端实现：`notify.js` 新增 `sendTestNotification()`——绕过设置
  开关直接弹（用户主动点击即验证通知能力本身），权限未决（default）时先请求授权，
  已拒绝/浏览器不支持/弹窗异常时返回原因，设置页按钮旁给出成功或失败提示。
  - 涉及 `frontend/src/notify.js`（新增 `sendTestNotification`）、
    `Settings.jsx`（通知卡片新增测试按钮与结果提示）、`styles.css`（`err-hint`）。
  - 测试：前端新增 6 个用例（已授权直接弹、default 先请求授权、授权被拒、
    已拒绝、浏览器不支持、构造异常），前端全量 30 passed + 后端 261 passed
    （后端零改动）+ build 通过。
- **网页端通知功能**（issue #21）：通过浏览器 Notification API 在用户电脑上弹
  系统通知，通知时机可在设置页「网页通知」卡片配置（总开关 + 4 个时机开关，
  存 `config.yaml notifications` 段，改设置立即生效无需重启）：
  ① 任务需要交互（任务失败，需人工介入）② issue 完成（任务成功）③ issue
  列表为空（对账扫描无待处理 issue）④ 无 issue 可处理（有 issue 但均在处理中）。
  后端新增 `notification_events` 表 + `GET /api/notifications/events` 增量拉取
  接口（游标 after，任务类事件以 task_id 唯一幂等，队列类事件同仓库同类型
  1 小时内节流去重）；前端 `notify.js` 全局轮询（10s）按设置过滤弹通知，
  首次拉取只记游标不弹历史事件。
  - 涉及 `backend/botler/notifier.py`（新增事件记录器）、`database.py`
    （事件表 + 迁移 + 查询）、`executor.py`（成功/失败收尾产生事件）、
    `reconciler.py`（对账扫描产生队列事件）、`api/notifications.py`（新增
    接口）、`api/settings.py` + `config.py`（notifications 段读写与布尔校验）、
    `frontend/src/notify.js`（新增：过滤/弹窗/轮询）、`App.jsx`（挂载轮询）、
    `Settings.jsx`（通知卡片 + 授权按钮）、`styles.css`。
  - 测试：后端新增 27 个用例（事件落库/游标/幂等/节流、executor 收尾事件、
    对账队列事件、notifications API、设置段校验），前端新增 12 个用例
    （开关过滤/未知类型/轮询游标/失败容错）。后端全量 261 passed + 前端
    24 passed + build 通过。
- **任务页面「查看任务执行」按钮，实时查看 agent 进度与聊天记录**（issue #20）：
  任务列表每行新增「操作」列「执行」按钮，点击跳转详情页实时面板——聊天记录
  （Claude Code 会话中的用户/助手文本、工具调用与结果，气泡式展示）每 3 秒
  自动刷新；实时输出（claude stdout 日志流）按字节偏移增量续读、自动滚底，
  任务运行中即可查看（session_id 在读循环里首次出现即提前落库，此前只在
  执行结束后才落库）。
  - 涉及 `backend/botler/executor.py`（新增 `find_session_file` /
    `parse_transcript` / `read_log_delta` 与 `_persist_session_from_chunk`
    读循环接入：运行中落库 session_id）、`backend/botler/api/tasks.py`
    （新增 `GET /api/tasks/{id}/execution`：`log_delta`/`log_offset` 增量
    日志 + `transcript` 聊天消息 + `session_id`）、`frontend/src/pages/
    Tasks.jsx`（「执行」按钮）、`TaskDetail.jsx`（实时执行面板：聊天气泡
    + 实时输出，终态/出错自动停止轮询）、`api.js`（`summarizeToolInput`
    工具调用摘要）、`styles.css`（聊天面板样式）。
  - 测试：后端新增 25 个用例（`parse_transcript` 解析/边界 7 个、
    `read_log_delta` 增量/半行回退/对齐 6 个、`find_session_file` 2 个、
    运行中落库 4 个、`/execution` API 契约 6 个），前端新增
    `summarizeToolInput` 6 个用例。全量 234 passed + 前端 12 passed +
    build 通过。
- **任务页面展示对应任务提交的链接**（issue #19）：任务成功时 executor 用 GitLab
  commits API 按提交信息（模板固定含 `issue #N`）匹配 Claude 推送的提交，完整 sha
  落库 `tasks.commit_sha`；列表页新增「提交」列、详情页新增「提交」行，点击跳转
  GitLab 查看对应 commit（新标签页）。查询失败/找不到不阻塞任务成功（页面显示
  占位符）。历史任务不回填，无提交信息时显示 `—`。
  - 涉及 `backend/botler/gitlab_client.py`（新增 `find_commit_for_issue`：
    message 按 `issue #N` 大小写/空格不敏感、数字边界精确匹配）、
    `backend/botler/database.py`（`tasks.commit_sha` 列 + 旧库迁移）、
    `backend/botler/executor.py`（`_finish_succeeded` 成功后 `_record_commit`
    查询落库）、`backend/botler/api/tasks.py`（透出 `commit_sha` / `commit_url`，
    仓库 URL 去 `.git` 后缀拼接）、`frontend/src/pages/Tasks.jsx` /
    `TaskDetail.jsx`（链接展示，`shortSha` 短 sha 显示）。
  - 测试：后端新增 19 个用例（`find_commit_for_issue` 匹配/边界/异常 9 个、
    executor 落库/降级/端到端 4 个、API 字段契约 4 个、数据库迁移 2 个），
    前端新增 `shortSha` 4 个用例。全量 209 passed + 前端 6 passed。
- **仓库页「对账」按钮**（issue #17）：仓库列表每行右侧按钮区新增「对账」按钮，
  点击后立即扫描该仓库，把「assignee 是 bot 但任务表无活跃记录」的 open issues
  补入队，行内直接显示结果——发现 N 个待处理已入队 / 无需处理（并显示扫描的
  issue 数）。与设置页的全局异步对账（`/settings/reconcile-now`）互补：单仓库
  对账同步执行、立即返回结果，用于"马上查看是否有需要处理的 issue"。
  - 涉及 `backend/botler/reconciler.py`（`reconcile_once` 支持 `repo_id` 参数，
    单仓库扫描，GitLab 报错记入 `errors`）、`backend/botler/api/repos.py`
    （新增 `POST /api/repos/{id}/reconcile`：仓库不存在 404、停用仓库返回
    `note` 提示、GitLab 故障 502）、`frontend/src/pages/Repos.jsx`（「对账」
    按钮 + 行内结果展示）。
  - 测试：后端新增 `tests/test_api_reconcile.py` 9 个用例（正常入队、无待处理、
    已有活跃任务不重复入队、仓库不存在 404、停用仓库、GitLab 故障 502、
    单仓库只扫指定仓库、未知仓库空结果、全量扫描回归）。全量 167 passed。

### Fixed

- **issue 实际状态与页面不一致：任务卡在排队/运行**（issue #24）：平台同时存在
  两个 botler 实例（历史遗留 supervisord/uvicorn 实例 + pm2 实例）时，两个
  实例的调度器各自从内存队列领取任务、并发执行同一任务（同秒内「任务成功」
  与「重新执行」并存），互踩同一工作区导致 claude 挂起、任务卡在 running
  占住执行槽，后续任务永久排队——issue 实际已实现但任务页面仍显示排队。
  修复（状态流转原子化，多实例并存时只有首个实例生效）：
  - `backend/botler/database.py`：新增 `claim_task()`（条件 UPDATE
    queued/retrying → running，原子抢占，抢不到即跳过）与 `finish_task()`
    （仅 running/retrying 可流转终态，先完成者生效，慢实例不覆盖）；
    附加字段白名单抽为模块级 `_TASK_FIELDS`
  - `backend/botler/executor.py`：`run_task` 开头原子抢占（已被其他实例
    领取/已终态的任务直接跳过），`_finish_succeeded`/`_finish_failed`
    改为条件终态（收尾被抢先时不再覆盖状态、不重复评论/通知）
  - 测试：新增 `tests/test_task_status.py` 10 个用例（抢占成功/已 running
    抢不到/终态抢不到/双连接只一个赢、条件终态/慢实例不覆盖）+ executor
    并发跳过 2 个用例；既有测试按真实状态流转补齐 claim 前置。
    后端全量 301 passed（+12）。
  - 部署提示（运维层面，非代码）：请停掉历史遗留的 supervisord/uvicorn
    实例，只保留 pm2 实例；`--reload` 模式仅限开发。
- **设置页「弹出测试通知」按钮不弹授权对话框**（issue #21 第三轮）：点击
  测试按钮期望弹出浏览器授权对话框，但 http 访问或自签名证书不受信任时
  页面处于非安全上下文（`isSecureContext === false`），浏览器规范规定此时
  `Notification.permission` 恒为 `'denied'` 且 `requestPermission()` 永不弹框
  ——原实现只检查 `'Notification' in window`，无法区分「不支持」与「非安全
  上下文」，用户只看到笼统的「已拒绝」提示，误以为是 bug。修复：
  - `frontend/src/notify.js`：新增 `notifyFailureReason()`（browser-unsupported
    / insecure-context / denied 三级原因判定），`canNotify()` 增加
    `isSecureContext` 检查，`sendTestNotification()` 非安全上下文时不再
    白调 `requestPermission()` 并返回 `insecure-context` 原因
  - `Settings.jsx`：测试按钮与浏览器授权行区分提示——非安全上下文明确告知
    「需 HTTPS 且证书受信任」；已拒绝时引导「点击地址栏左侧图标将通知权限
    改为允许」。
  - 测试：前端新增 5 个用例（`notifyFailureReason` 四态 + 非安全上下文不
    请求授权不弹通知）。前端全量 35 passed + 后端 289 passed（零改动）+
    build 通过。
- **全局模板写死单仓库路径，任务收到错误指令**（issue #18 诊断发现）：用户
  `data/backend/config.yaml` 的全局模板采用跨会话 issue-agent 模式，但模板
  硬编码 `chenkaidi/shipyard`、`https://home.chenkaidi.top:509`、共享进度文件
  ——botler 对任意仓库执行任务时 Claude 收到「处理 shipyard 队列」指令，
  不处理当前指派的 issue（exit 0 但 issue 不关，任务反复失败）。修复：
  - `backend/botler/templates.py`：新增 `{project_path}`（group/repo，从仓库
    URL 提取）、`{project_path_encoded}`（URL 编码 chenkaidi%2Fbotler）、
    `{gitlab_host}`（host:port 去 scheme）占位符；`build_variables` 支持
    `repo_url` 参数（无 URL 时兜底用仓库名）；executor 渲染时传入仓库 URL。
  - `data/backend/config.yaml`：全局模板中硬编码的仓库路径 / GitLab 地址 /
    进度文件路径参数化为占位符（`~/gitlab_issue_agent/progress.md` →
    `~/{repo_name}_issue_agent/progress.md`，多仓库进度文件隔离）。
  - 测试：`tests/test_config_template.py` 新增 4 个用例（URL 提取、无 .git
    后缀、无 URL 兜底、参数化模板渲染无残留占位符/无 shipyard）。全量
    190 passed + 前端 2 passed。

- **部署后任务频繁 git fetch 失败（403 job token）**（issue #18 任务失败根因）：
  executor 的 git/claude 子进程环境直接继承 `os.environ`——CI 部署在
  gitlab-runner 构建目录里 `pm2 start`，作业环境（`CI_JOB_TOKEN` 等）被继承，
  且 `~/.gitconfig` 的 credential store（`~/.git-credentials`）中失效的
  `gitlab-ci-token` 条目排在有效 `oauth2` 之前。git 凭据顺序中 store 优先于
  `GIT_ASKPASS`：失效 job token 被选用 → GitLab 403
  （`Authentication by CI/CD job token not allowed from shipyard to project
  #123`）→ 403 不触发凭据重试 → askpass 的 bot token 永远用不上 →
  fetch/push 必失败 → 重试耗尽 → `bot-failed` 标签。修复：
  - `backend/botler/executor.py`：新增 `_clean_process_env`（剔除 `CI_*` /
    `GITLAB_CI` / `GIT_CONFIG_*` 环境变量）、`_git_global_config`（生成净化版
    全局 gitconfig——仅剥离 `[credential]` section，保留 `user` / `http` /
    `safe.directory` 等，避免 `/dev/null` 连带丢失 user.name 致 commit 失败、
    自签证书握手失败），`prepare_workspace` 与 `_build_env` 统一使用；claude
    子进程同样注入 `GIT_ASKPASS`（bot token）并禁用 credential store，
    push 凭据与 API 保持一致。
  - 测试：新增 `tests/test_executor_credentials.py` 3 个用例（本地 http 服务器
    + git http-backend 代理模拟 GitLab 认证时序：无凭据 401 → job token 403 /
    bot token 200；断言修复后 fetch 使用 askpass 的 bot token、旧行为对照
    复现 store 抢先用 job token、`_build_env` 净化）。全量 186 passed + 前端
    2 passed。

- **失败详细原因中转义符未格式化**（issue #16）：任务「详情」弹窗与详情页
  「claude 输出尾部」直接展示 claude JSON 输出的 `result` 字段——该字段内嵌
  工具调用记录（再次序列化的 JSON 文本），`\n` `\"` 等转义按字面量存放，
  展示时 `\n` 显示为反斜杠+n 而非换行，可读性差。修复：新增
  `format_display_line` / `_decode_escapes` 逐层解码（先严格 `json.loads`
  展开内嵌 JSON 结构，失败则宽松解码常见转义 `\n \r \t \\ \' \"` 等后递归），
  JSON 行重排时只保留 `type / subtype / session_id / exit_code / error`
  等排查字段，丢弃 `ttft_ms / uuid` 等机器噪音。
  - 涉及 `backend/botler/executor.py`（`_extract_error` 解码 result 后再提取
    trace、`_tail_output` 逐行格式化——失败评论与日志摘要同步受益）、
    `backend/botler/api/tasks.py`（详情页 `log_file_tail` 逐行格式化）。
  - 测试：`tests/test_executor.py` 新增 7 个用例（嵌套工具调用记录解码、混合
    文本 + JSON 片段、纯文本不误伤、JSON 行重排、非 JSON 行原样、`_tail_output`
    解码）、`tests/test_api_tasks.py` 新增 1 个用例（log_file_tail 解码且丢弃
    机器字段）。全量 183 passed + 前端 2 passed。

- **部署后任务一直运行失败**（permission_denials）：`data/backend/config.yaml`
  的 `templates.default` 曾被替换为 gitlab-issue-agent 提示词（跨会话领取队列），
  executor 渲染后 Claude 收到错误指令，不处理当前指派的 issue；且 `claude -p`
  无人值守执行未跳过权限确认，Bash/curl/Read/MCP 等一切操作被权限系统拒绝
  （task_7/8/9 日志 `permission_denials`），Claude 自行终止（exit 0）→ 重试耗尽 →
  任务失败。修复：
  - `backend/botler/executor.py`：claude 命令追加 `--dangerously-skip-permissions`
    （无人值守无法交互授权，权限拒绝 → 任务必然失败）；
    `_extract_session_id` / `_extract_error` 新增 `_load_json_output` 容错——
    claude 无 stdin 时 stderr 打印 `Warning: no stdin data received...` 混入
    stdout，整串 `json.loads` 失败导致 session_id 永不落库、断点续跑（--resume）
    失效，现从首个 `{` 起用 `JSONDecoder.raw_decode` 只取首个 JSON 对象。
  - `backend/config.example.yaml` + `data/backend/config.yaml`：恢复标准
    「处理当前 issue」模板；关闭 issue 的 curl 加 `-k`（自建 GitLab 为自签
    证书，不带 -k 时证书错误 → issue 永远关不上）。
  - 测试：后端新增 `tests/test_executor_runtime.py` 5 个用例（cmd 含
    --dangerously-skip-permissions 且 resume 时保留、stderr 前缀下 session_id/
    错误提取/落库容错）、`tests/test_config_template.py` 4 个用例（模板面向
    当前 issue、不含 issue-agent 特征、curl -k、渲染结果）。全量 176 passed +
    前端 2 passed。

- **任务时间时区与浏览器不一致**（issue #14）：后端 SQLite `datetime('now')` 存
  UTC（如 `2026-08-12 01:25:54`），前端 `fmtTime` 原样拼接 `' UTC'` 直接展示，
  浏览器在本机（UTC+8）看到的任务创建/开始/完成时间与执行日志时间戳比本机慢
  8 小时。修复：前端 `fmtTime` 把 UTC 字符串补 `Z` 解析为时刻后，用
  `Intl.DateTimeFormat` 按配置时区格式化（`YYYY-MM-DD HH:mm:ss`，`hourCycle: h23`
  避免午夜 24:00:00）；设置页新增「界面显示 → 显示时区」选项（`ui.timezone`，
  IANA 时区名，可下拉选择常用时区或手动输入，留空 = 跟随浏览器本机时区，即
  "默认和本机一致"），写回 config.yaml 全局持久化，保存后立即生效无需刷新。
  后端 `GET/PUT /api/settings` 支持 `ui` 段并用 `zoneinfo` 校验非法时区名（400）。
  - 涉及 `frontend/src/api.js`（fmtTime 重构 + `setDisplayTz`）、
    `frontend/src/App.jsx`（启动加载时区）、`frontend/src/pages/Settings.jsx`
    （时区输入 + datalist）、`frontend/package.json`（新增 `npm test`）、
    `frontend/tests/fmt-time.test.mjs`（新增，node:test）、
    `backend/botler/config.py`（`ui_timezone` 字段 + `update_ui`）、
    `backend/botler/api/settings.py`（`ui` 段读写 + 校验）、`.gitlab-ci.yml`
    （frontend:build 构建前跑 `npm test`）。
  - 测试：前端新增 2 个用例（Asia/Shanghai +8h 转换、空值占位符，修复前失败
    复现 bug）；后端新增 4 个用例（timezone 默认空、持久化写回 config.yaml、
    清空回退、非法时区名 400）。全量 158 passed + 前端 2 passed。

### Added

- **前端版本号与构建时间显示**（issue #9）：每次构建（CI/CD `frontend:build` 或
  本地 `npm run build`）自动自增版本号（patch 位 +1，如 1.0.0 → 1.0.1）并记录
  构建时间，前端导航栏右侧显示 `v1.0.1 · 2026-08-11 22:19` 徽标（悬停显示构建
  时间）。版本号持久化在 `data/version.txt`（数据目录跨构建/部署持久，构建目录
  被清理不丢）；`frontend/scripts/gen-version.mjs`（新增）在 vite 构建前自增版本
  号并生成 `frontend/public/version.json`（含 version + buildTime），vite 构建时
  自动复制进 dist/；前端新增 `VersionBadge` 组件 fetch 渲染，开发模式（无该文件）
  自动隐藏。
  - 涉及 `frontend/scripts/gen-version.mjs`（新增）、
    `frontend/src/components/VersionBadge.jsx`（新增）、`frontend/package.json`
    （build 前置 gen:version）、`frontend/src/App.jsx`（导航栏挂载徽标）、
    `frontend/src/styles.css`（`.version-badge` 样式）、`.gitlab-ci.yml`
    （frontend:build 注入 `BOTLER_DATA_DIR` 指向持久数据目录，构建后输出
    `dist/version.json` 验证）、`.gitignore`（忽略生成的 version.json）。

- **任务断点续跑**（issue #8）：CI/CD 频繁重新部署导致正在执行的任务进程被杀后，
  重启不再从头重跑——executor 每次执行后把 claude 会话 id（`claude -p` JSON 输出
  的 `session_id`）持久化到 `tasks.claude_session_id`；重试或平台重启恢复
  （调度器 `requeue_interrupted` 重新入队）时用 `claude -p --resume <sid>` 接续
  上次会话，且工作区只 fetch 不清空（保留 Claude 已做的修改），并注入恢复引导语
  让 Claude 检查现状后从断点继续，不重复已完成的工作。会话文件丢失（如 Docker
  未挂载 `~/.claude`）时自动清掉无效 id 降级为全新会话。任务 API 新增 `resumed`
  字段，前端任务列表在「尝试」列显示「恢复」徽标。
  - 涉及 `backend/botler/executor.py`（`_extract_session_id` / `_session_file` /
    `_resume_prompt` / resume 工作区保留）、`database.py`（迁移新增列）、
    `api/tasks.py`、`frontend/src/pages/Tasks.jsx`、`frontend/src/styles.css`、
    `docker-compose.yml`（新增 `data/claude-home` 挂载，Docker 部署会话持久化）。
  - 测试：新增 9 个用例（session_id 解析落库、resume 参数与引导语、工作区保留、
    会话文件缺失降级、重启恢复任务自动 resume、API `resumed` 契约）。
  - 说明：pm2 部署下 `~/.claude` 在宿主 HOME 天然持久，无需额外配置；Docker
    部署需重建容器使新挂载生效。

### Changed

- **前端响应式适配 2K 分辨率**（issue #15）：原内容区固定 `max-width: 1100px`，
  2560x1440 屏幕上左右两侧各剩约 730px 空白。优化：内容区宽度改为 CSS 变量
  `--content-width` 驱动，媒体查询分档放宽——视口 ≥1600px 放宽到 1360px（1440p
  笔记本），≥1920px 放宽到 1600px（2K 2560x1440，两侧余量降至约 480px）；表格
  省略列（`--ellipsis-max`）同步由 340px 放宽到 520px，长标题/失败原因列不再被
  压缩。小屏（<1600px）行为不变，保持原 1100px。
  - 涉及 `frontend/src/styles.css`（CSS 变量 + 媒体查询，`.content` 与
    `.table td.ellipsis` 引用变量）。
  - 测试：前端 2 passed + build 通过（纯 CSS 静态改动，无逻辑变更）；后端全量
    158 passed。

- 「添加仓库」按钮移到表单底部单独一行并水平居中（issue #6）：原按钮与「显示名称」
  「webhook 回调地址」输入框挤在同一行，紧挨 webhook 字段易被误解为 webhook 专属
  操作；现拆为两行，按钮独占一行（水平居中，追加需求 note 158 后移除旁侧说明文字
  并加宽按钮——新增 `.btn-wide` 样式）。涉及 `frontend/src/pages/Repos.jsx`、
  `frontend/src/styles.css`。
- CI 健壮性修复：`sync_to_github` 的 git push 加 `timeout 60` 秒超时——git 自身
  无连接超时，github.com 直连被阻断时单次 push 会挂数分钟，既有 10 次重试循环
  演变成无限挂起（实测流水线卡死 20+ 分钟）；加超时后单次尝试快速失败进入
  下一次重试，最多 10 次 ≈ 11 分钟必然收敛。涉及 `.gitlab-ci.yml`。
- CI 部署残留容器清理（issue #13）：检查发现 code01 的 docker 存在 rootful
  （`/var/run/docker.sock`）与 rootless（`/run/user/$(id -u)/docker.sock`）两个
  daemon，历史 Docker 部署容器落在 rootless 侧而 CI 清理只查 rootful，且 runner
  构建目录被清理后 `docker compose down` 依赖配置文件会失败——导致 4 个临时
  容器（botler compose 孤儿 + 3 个 apt 安装中途失败容器）残留数周未被清理。
  修复：deploy job 第 4 步改为**双 daemon** 按容器 label（`com.docker.compose.
  project=botler`）/名称过滤直接 `docker rm -f`（不依赖 compose 配置文件），并
  新增 `after_script` 兜底清理——无论部署成功/失败都会执行；只清理 botler
  项目容器，不误删其他容器（如 shipyard）。已有残留已手动清理（本机 docker
  现存容器：rootful 侧 shipyard 正常运行中）。涉及 `.gitlab-ci.yml`。

### Added

- 新增**备份与恢复**功能（issue #5）：备份范围为 `config.yaml` + `botler.db`
  （SQLite，用 sqlite backup API 生成一致性快照，WAL 下安全），打包为
  `botler-backup-<时间戳>.tar.gz` 存于服务器 `data/backups/`（compose 新增挂载）。
  - 触发：手动（设置页「立即备份」/ `POST /api/backups`）+ 定时（每天 03:00
    Asia/Shanghai，`backup.enabled` 开关）；保留策略按 `retention_days`（前端
    可配置 1~365，默认 30）清理更旧备份。
  - 恢复：Web UI 上传备份文件 / 选择服务器历史备份（`POST /api/backups/restore`
    与 `/restore/upload`），校验成员名白名单（防路径穿越）+ manifest sha256
    校验和（缺 manifest / 篡改文件拒绝恢复），覆盖数据后 `os.execv` 自动重启
    进程（Docker / pm2 / systemd / dev 通用；重启后 running 任务自动重新入队）。
  - API：`GET/POST /api/backups`、`GET /api/backups/{name}/download`、
    `DELETE /api/backups/{name}`；settings API 新增 `backup` 段。
  - 涉及 `backend/botler/backup.py`（新）、`backend/botler/api/backup.py`（新）、
    `config.py`（KNOWN_FIELDS + Settings 新字段）、`api/settings.py`、`main.py`
    （AppContext + 定时任务启停）、`docker-compose.yml`（backups 挂载）、
    `frontend/src/components/BackupManager.jsx`（新，设置页备份管理卡片）、
    `frontend/src/api.js`（下载/上传封装）。新增 `backend/tests/test_backup.py`
    （21 个用例，含路径穿越 / 损坏包 / 校验和失败 / WAL 一致性 / 保留清理等
    边界）与 `test_api_backup.py`（17 个用例）。

- 任务列表**详细失败原因**查看（issue #4 追加需求）：失败摘要之外，executor 每次
  尝试失败时都会提取错误信息（claude JSON 输出中优先提取 `Traceback` 堆栈，否则
  取输出尾部），连同退出码按尝试次数序列化写入 `tasks.error_detail`；任务列表
  失败原因列在存在详细记录时显示「详情」按钮，点击弹窗逐次展示「第 N 次尝试 /
  退出码 / 错误与堆栈」（长内容可滚动），另附完整摘要与日志文件路径，界面默认
  不直接展示详细内容。API `error_detail` 解析为结构化对象，脏数据（非法 JSON）
  返回 `None` 不报错。涉及 `backend/botler/executor.py`、`backend/botler/database.py`
  （迁移新增列）、`backend/botler/api/tasks.py`、`frontend/src/pages/Tasks.jsx`、
  `frontend/src/styles.css`；新增 `backend/tests/test_executor.py`（10 个用例）覆盖
  错误提取（JSON/Traceback/截断/空输出）、error_detail 序列化与重试耗尽 /
  unresolvable 落库，`test_api_tasks.py` 补 error_detail 数据契约用例。

- 任务列表新增**失败原因**列（issue #4）：failed / interrupted 状态直接展示后端
  `error_message`（重试耗尽、Claude 无法解决、获取 issue 失败、平台中断等），
  长文本省略号截断、悬浮显示完整内容；其余状态显示 `—`。后端 `/api/tasks` 原有
  数据契约不变，新增 `backend/tests/test_api_tasks.py`（13 个用例）覆盖列表字段、
  状态过滤、搜索、分页、统计与活跃任务去重。涉及 `frontend/src/pages/Tasks.jsx`。

### Changed

- CI/CD 部署阶段改为 **Docker 部署**（issue #7）：`deploy_to_code01` 不再用 pm2
  直接部署，改为 `docker compose up -d --build`（前端已打进镜像，Dockerfile
  多阶段构建，不再依赖 frontend:build 的 dist 产物）；部署前自动停止旧 pm2
  服务避免 8000 端口冲突；`backend/.env` 缺失时用 CI 变量生成
  （GITLAB_BOT_TOKEN / WEBHOOK_SECRET / ANTHROPIC_*）；容器内 git 凭据
  （执行器 clone/push 依赖）通过挂载 `.git-credentials` + `GIT_CONFIG` 强制
  store helper 提供（优先复用宿主凭据，否则用 GITLAB_BOT_TOKEN 生成）；
  默认镜像源（Docker Hub）不可达时自动改用国内源
  `docker.m.daocloud.io` 重试；部署强制使用 rootful docker socket
  （`/var/run/docker.sock`，rootless context 有容器运行故障）；`docker-compose.yml`
  新增 `GIT_CREDENTIALS_FILE` 可调挂载、`deploy/verify-docker.sh` 同步适配。
  pm2 / systemd 仍保留为手动部署备选。

- Docker 部署**数据目录集中到 `data/`、时区固定 Asia/Shanghai**（issue #7 追加需求）：
  compose 挂载源默认改为 `./data`（相对部署目录，默认 = `data/` 文件夹），
  config.yaml / .env / botler.db / workspace / logs 全部集中在 `data/` 下；
  **CI 部署显式固定 `BOTLER_DATA_DIR=/home/ckd/codes/botler/data`**（用户指定
  绝对路径，不随 gitlab-runner 构建目录漂移——构建目录可能被清理导致数据
  丢失）；容器时区固定为亚洲/上海（compose `TZ: Asia/Shanghai` + 镜像安装
  tzdata 并写入 `/etc/localtime`）；`.gitignore` 忽略 `data/` 文件夹；CI
  `deploy_to_code01` 增加旧位置散落数据（backend/.env、config.yaml、botler.db、
  workspace/、logs/）到 `data/` 的**一次性自动迁移**（目标已有文件则保留 data/
  版本），`.env` 缺失时在 `data/backend/.env` 用 CI 变量生成；`deploy/verify-docker.sh`
  新增容器时区校验（`date +%Z` = CST）。涉及 `docker-compose.yml`、`Dockerfile`、
  `.gitignore`、`.gitlab-ci.yml`、`deploy/verify-docker.sh`、`README.md`。

- 「添加仓库」默认方式改为**本地文件夹方式**：打开仓库管理页时默认选中「本地文件夹
  （读取 git remote）」，而非 GitLab URL 方式；切换方式仍可随时手动选择。涉及
  `frontend/src/pages/Repos.jsx`。
- 前端 UI 全面改版为 **Geist Light 浅色设计体系**（Vercel 官方设计令牌，经过验证的方案）：
  页面背景 `#fafafa` + 白色卡片表面、主色 `#0070f3` 蓝、发丝线边框/轻阴影层次、
  6px 常规圆角、弱底+深字状态徽标、焦点环等交互规范。原有深蓝灰主题（`#0f1420`/`#4f8cff`）
  移除。全部页面（仓库/任务/模版/设置/目录选择对话框）只换皮肤，组件结构与功能不变。
  涉及 `frontend/src/styles.css`（令牌化全量重写）。
- 新增 **`docs/design-system.md` 前端设计规范**：设计原则、CSS 变量令牌总览、色板/
  字体/间距/圆角阴影/边框规范、全部组件与状态规范、交互规范、新增样式检查清单、
  迁移决策记录。后续前端样式变更以此为唯一规范来源。

### Added

- 新增 **Docker 部署方式**（与 pm2 / systemd 并存，三选一）：
  - `Dockerfile` 多阶段构建：node 构建 React/Vite 前端 → node + python 运行时
    （内置 `git`、`claude` CLI（`@anthropic-ai/claude-code`，执行器核心依赖）、
    后端 Python 依赖；pip 走清华源、npm 走 npmmirror）
  - `docker-compose.yml` 一键部署：端口 `BOTLER_HTTP_PORT`（默认 8000）与数据目录
    `BOTLER_DATA_DIR`（默认 `.`）可调；config.yaml / .env / botler.db / workspace / logs
    全部卷挂载持久化，容器重建不丢数据；内置 healthcheck（`/api/health`）与
    `restart: unless-stopped`
  - 国内无法访问 Docker Hub 时，构建参数 `NODE_IMAGE` / `RUNTIME_IMAGE` 可覆盖
    基础镜像前缀（如 `docker.m.daocloud.io/library/node:20-alpine`），CLI 与
    docker compose 两种方式均支持
  - `deploy/verify-docker.sh` 冒烟验证脚本：静态校验 → compose 配置校验 → 镜像构建
    → 起容器 → 健康检查 → API/前端页面 → 重复 up 幂等 → 数据落盘，共 10 项检查
    （`--full` 用临时数据目录与 18000 端口，不触碰真实数据）

- 目录选择对话框支持配置默认初始定位目录（`browse.default_path`）：打开「添加仓库 → 本地
  文件夹 → 浏览…」对话框时不再从 `/` 开始，而是定位到配置的目录；未配置时默认为服务器用户
  主目录（`~`）。支持 `~` 展开与相对路径，配置的路径不存在时自动回退主目录、主目录不可用
  时回退 `/`，保证对话框始终能打开。可在 Web UI「设置」页或 `config.yaml` 配置，所有用到
  目录选择的地方统一生效（`GET /api/repos/browse` 不带 path 即返回默认初始目录）。涉及
  `backend/botler/dir_browse.py`（新增 `resolve_default_path`，9 个边界用例）、`config.py`
  （新增 browse 配置段 + settings API 读写）、`api/repos.py`、`api/settings.py`、
  `frontend/src/components/FolderPicker.jsx`、`pages/Repos.jsx`、`config.example.yaml`。
- 「本地文件夹」方式添加仓库支持服务器目录选择对话框：路径输入框旁新增「浏览…」按钮，：路径输入框旁新增「浏览…」按钮，
  弹出对话框逐级浏览服务器文件系统（从 `/` 开始；隐藏目录默认隐藏可勾选显示、伪文件系统
  `/proc` `/sys` `/dev` `/run` 自动过滤、无权限目录禁用、含 `.git` 的文件夹标记为 git 仓库），
  选中文件夹后自动回填路径并读取 remote（也可继续手动输入路径）。涉及
  `backend/botler/dir_browse.py`（新模块，含 16 个边界用例）、`api/repos.py`（新增
  `GET /api/repos/browse`）、`frontend/src/components/FolderPicker.jsx`（新组件）、
  `pages/Repos.jsx`、`styles.css`。
- 新增「本地文件夹」方式添加仓库：填写服务器上的本地 git 仓库路径 + webhook 回调地址（可选），
  平台在服务端运行 `git remote -v` 读取 remote 列表，前端展示供用户选择（`POST /api/repos/discover`）；
  添加后 `local_path` / `remote_name` 持久化到 config.yaml 与数据库，任务执行直接在该本地文件夹
  进行（不再 clone 到平台工作区）。涉及 `backend/botler/git_remote.py`（新模块）、`api/repos.py`、
  `database.py`（repos 表新增 `remote_name` 列及旧库自动迁移）、`config.py`、`executor.py`、
  `main.py`、`frontend/src/pages/Repos.jsx`。

### Fixed

- **修复运行任务失败——origin/HEAD 缺失与 fetch 凭据间歇失败**（issue #12）：
  任务重试耗尽后退出码 -1。根因有二：(1) `prepare_workspace` 每次执行
  `git reset --hard origin/HEAD`，依赖 `origin/HEAD` 符号引用——手工添加
  remote 的仓库（如 local_path 方式注册的 botler 自身）无此引用，报
  `ambiguous argument 'origin/HEAD'` **必现失败**；修复为 reset 跟随实际
  checkout 的分支（main → master），不再依赖 origin/HEAD。(2) askpass
  凭据脚本在每次 prepare 结束后被删除，并发任务/重试时序下脚本不存在 →
  git 回退 credential helper 旧凭据 → `HTTP Basic: Access denied` **间歇性
  失败**；修复为保留脚本（每次 prepare 覆盖刷新，0700 权限，token 轮换自动
  生效），并调整 `git_env` 构造顺序防止外部环境变量覆盖凭据注入。涉及
  `backend/botler/executor.py`。测试：新增 2 个用例（origin/HEAD 缺失时
  prepare 应成功；prepare 后 askpass 脚本保留），本地全量 154 passed。

- **修复运行任务必现失败**（issue #11）：任务每次执行都在 `prepare_workspace` 抛
  `AttributeError: 'sqlite3.Row' object has no attribute 'get'`，重试耗尽后退出码
  -1。根因：`database` 层查询返回 `sqlite3.Row`（无 `.get()` 方法），而 `executor`
  的 `_repo_workdir` / `prepare_workspace` 按 dict 风格对 repo 调 `.get()` 访问
  `local_path` / `remote_name`。修复：新增 `_row_get(row, key, default)` 兼容
  访问器（同时支持 sqlite3.Row 与 dict），替换 3 处 `.get()` 调用；涉及
  `backend/botler/executor.py`。测试：新增 2 个用例（`_repo_workdir` 直接收
  sqlite3.Row；`run_task` 全流程从 db 取 Row 仓库正常 succeeded——与 CI 日志
  同调用路径的端到端复现）。
- 修复添加仓库时 webhook 注册报错「注册 webhook 失败: GitLab API 错误 422: Invalid url given」
  （webhook_url 留空时必现，URL 方式添加同样受影响）：根因是 Botler 部署在内网
  （10.0.0.122），GitLab 默认禁止向本地/私有网络地址注册 webhook（SSRF 防护，
  `allow_local_requests_from_web_hooks_and_services` 默认 false）。修复方案：
  ① GitLab 侧在 Admin → Settings → Network → Outbound requests 勾选「Allow requests
  to the local network from webhooks and integrations」；② Botler 侧
  `register_webhook` 检测到 422 + 内网回调地址时，错误信息自动追加可操作提示
  （新增 `_is_private_url` 判定 + 15 个测试用例），涉及 `backend/botler/gitlab_client.py`。
- 修复 `GitLabClient.resolve_project` 无法识别 scp-like SSH URL（`git@host:group/project.git`，
  `git remote -v` 最常见形态）：此前 urlparse 解析不出 scheme，整串被当作项目路径导致 404；
  现按最后一个 `:` 之后的仓库路径解析，并补充 `.git` 后缀剥离与嵌套 group 支持。
  同步新增 `backend/tests/` pytest 测试套件（26 个用例），CI `backend:test` 检测到 `tests/`
  目录后自动运行；`requirements.txt` 新增 `pytest` 依赖。
