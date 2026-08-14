# 在 GitLab 页面查看 bandit 扫描结果

> issue #72：CI 的 `security:bandit` job 将扫描结果以 **SARIF** 格式
> （`bandit-report.sarif`）上传至 GitLab SAST 报告，本页说明如何在
> GitLab 网页界面查看扫描结果，无需翻阅 job 日志。

## 扫描报告在哪

每次流水线运行，`security:bandit` job 都会生成一份 SARIF 报告并上传：

- **有漏洞时**：job 失败（severity ≥ medium 阻断门禁），报告同样上传
  （`artifacts: when: always`），页面照常展示漏洞详情；
- **无漏洞时**：job 成功，报告记录「0 个问题」。

## 查看入口

### 1. 流水线 Security 标签页（最常用）

`项目页面 → 构建 → 流水线 → 点击某次流水线 → Security 标签`。

- 列出该次流水线的安全扫描结果（当前仅 Bandit 扫描器），
  显示发现的漏洞数与严重级别分布；
- 点击扫描器条目可查看每条漏洞详情：规则编号（如 B602）、
  严重级别（High/Medium/Low）、置信度、所在文件与行号、
  触发代码片段；
- 门禁失败的流水线，可直接跳转漏洞详情，与 job 日志同屏对照。

### 2. Merge Request 安全组件

创建 MR 时，MR 页面自动展示 **Security scanning** 组件：

- 列出本次变更**新增**的漏洞（与目标分支比较）；
- 所有漏洞已修复时显示绿色「No vulnerabilities found」。

### 3. 漏洞报告页（Ultimate 版专属）

`项目页面 → 安全 → 漏洞报告`：以列表/卡片汇总历次扫描的所有漏洞，
支持按状态/严重级别/扫描工具筛选、展开完整详情（CWE 编号、代码
上下文）。**注：此页面为 GitLab Ultimate 功能，本实例（社区版 CE）
无此入口，请使用入口 1/2 查看。**

## 附：直接下载原始报告

流水线的 `security:bandit` job 右侧「浏览产物」按钮中，可直接下载
`bandit-report.sarif` 原始文件（保留 1 周；SARIF 为 JSON 格式，
可用任意编辑器或 SARIF 查看器打开）。

## 常见问题

- **为什么 job 失败还能看到报告？**
  阻断门禁失败正是「发现了中/高危漏洞」，此时更需要看到漏洞详情，
  因此报告配置为 `when: always` 上传。
- **低危漏洞为何不阻断但会显示？**
  扫描参数 `--severity-level medium`：低危（LOW）仅记录于报告，
  不导致 job 失败；中危及以上才阻断流水线。
- **页面显示的严重级别与 bandit 的对应关系？**
  高危/中危对应页面中的 High/Medium，映射关系见 SARIF 报告中的
  `properties.issue_severity` 字段。
