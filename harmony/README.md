# Botler 鸿蒙端（Web 套壳）

> 对应 Issue #173：额外实现一个鸿蒙端，使用 web 套壳，同时在 CI/CD 流程中加入鸿蒙的编译。

鸿蒙端使用 **HarmonyOS NEXT（Stage 模型）原生工程** 承载一个 **系统 Web 组件（WebView）套壳**：
应用本身只提供原生壳与加载体验（启动页 / 加载动画 / 失败重试 / 返回键历史回退），
页面内容全部加载 Botler 的 Web 前端（React/Vite 构建产物由 FastAPI 同源托管）。
因此 Web 端的全部能力（任务列表 / 详情 / 设置 / 标签管理等）在鸿蒙端开箱即用，
后续 Web 功能迭代无需改动鸿蒙工程。

## 目录结构

```
harmony/
├── AppScope/                  # 应用级配置（bundle 信息 / 图标 / 应用名）
│   ├── app.json5              # bundleName / versionCode / versionName / icon / label
│   └── resources/base/        # 应用级资源（app_name / app_icon）
├── entry/                     # entry 模块（HAP）
│   ├── build-profile.json5    # 模块构建配置（stageMode / release 混淆规则 / targets）
│   ├── hvigorfile.ts          # 模块 Hvigor 入口（hapTasks）
│   ├── oh-package.json5       # 模块 ohpm 依赖声明（当前无三方依赖）
│   └── src/main/
│       ├── module.json5       # 模块清单（EntryAbility / 设备类型 / INTERNET 权限）
│       ├── ets/
│       │   ├── common/AppConfig.ets        # ⚠️ Web 套壳加载地址配置（WEB_URL）
│       │   ├── entryability/EntryAbility.ets  # 应用入口 Ability
│       │   └── pages/Index.ets             # WebView 套壳主页（加载动画/失败重试/返回键）
│       └── resources/         # 模块资源（字符串 / 颜色 / 图标 / 路由表）
├── hvigor/hvigor-config.json5 # Hvigor 构建配置（modelVersion 6.0.2）
├── build-profile.json5        # 工程级构建配置（products / 模块注册）
├── hvigorfile.ts              # 工程级 Hvigor 入口（appTasks）
├── oh-package.json5           # 工程级 ohpm 依赖声明
└── scripts/
    └── validate_harmony.py    # 工程结构校验脚本（CI 第 0 步 + backend 回归测试复用）
```

## 环境要求

| 工具 | 版本 | 说明 |
| ---- | ---- | ---- |
| HarmonyOS SDK | 6.1.1（API 24） | 本机命令行工具链自带（`~/command-line-tools/sdk/default`） |
| hvigor | 6.24.3 | 华为命令行构建工具（`~/command-line-tools/bin/hvigorw`） |
| ohpm | 6.1.2+ | 华为包管理器（`~/command-line-tools/bin/ohpm`） |
| DevEco Studio | 6.x（可选） | 真机运行 / 调试 / 自动签名推荐使用 |

> 本机 CI（code01，shell executor）已安装完整命令行工具链，流水线 `harmony:build`
> 作业直接复用，无需额外下载。

## 命令行编译（与 CI 一致）

```bash
cd harmony
export PATH="$HOME/command-line-tools/bin:$PATH"          # 鸿蒙命令行工具链
export DEVECO_SDK_HOME="$HOME/command-line-tools/sdk"      # SDK 根目录（hvigorw 默认也指向这里）

# 1. 结构校验（秒级快速失败，CI 第 0 步）
python3 scripts/validate_harmony.py

# 2. 依赖安装（当前无三方依赖，秒级完成）
ohpm install

# 3. 编译 HAP（debug 模式，未签名）
hvigorw assembleHap --mode module -p product=default -p buildMode=debug --no-daemon

# 产物
ls entry/build/default/outputs/default/*.hap
#   entry-default-unsigned.hap   ← 未签名 HAP（CI 编译验证产物）
```

## 修改 Web 加载地址（WEB_URL）

默认加载地址为部署机内网地址 `http://your-server.example.com:8000`（pm2 部署，ZeroTier 内网）。
按实际部署环境修改 `entry/src/main/ets/common/AppConfig.ets`：

```typescript
// 鸿蒙端加载的 Botler Web 前端地址（支持 http/https）
export const WEB_URL: string = 'http://your-server.example.com:8000';
```

## 使用 DevEco Studio 打开 / 真机运行

1. 安装 DevEco Studio 6.x（含 HarmonyOS 6.x SDK），File → Open 选择 `harmony/` 目录；
2. 等待工程 Sync 完成（模型版本 6.0.2，会自动匹配 SDK）；
3. File → Project Structure → Signing Configs，勾选 **Automatically generate signature** 完成自动签名；
4. 连接鸿蒙手机 / 平板（开启开发者模式），点击 Run 安装运行；
5. 若真机与部署机不在同一内网，请先在 `AppConfig.ets` 中把 `WEB_URL` 改为可达地址。

> ⚠️ **签名说明**：CI 只做「编译验证」（debug 未签名 HAP，`SignHap` 自动跳过）。
> 正式安装到真机必须在 DevEco Studio 中完成自动签名后重新构建。

## CI/CD 集成

流水线 `build` 阶段新增 **`harmony:build`** 作业（与 `frontend:build` / `backend:test` 并行）：

1. `python3 scripts/validate_harmony.py` —— 工程结构校验（配置缺失 / 引用断裂秒级失败）；
2. `ohpm install` —— 依赖安装；
3. `hvigorw assembleHap` —— 真实 ArkTS 编译，失败即阻断流水线（部署门禁）。

构建产物 `*.hap` 以 artifact 形式上传（有效期 1 周），可在流水线页面下载。

## 回归测试

后端全量测试包含 `backend/tests/test_harmony_project.py`（10 例）：

- JSON5 迷你解析器：注释 / 尾逗号 / 单引号 / 无引号键 / 字符串内注释符不误判；
- 真实工程通过全部结构校验（防回退）；
- 破坏性用例：移除 INTERNET 权限、移除 Web 组件、移除/篡改 WEB_URL、
  移除 targetSdkVersion、删除必需文件——均能被检出。

```bash
cd backend && .venv/bin/python -m pytest tests/test_harmony_project.py -v
```
