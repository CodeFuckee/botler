// 全局搜索跳转目标（issue #216）：SearchOverlay 选中结果后生成概览页
// 深链参数，Overview 页消费这些参数打开 issue 抽屉 / 滚动定位仓库卡片。
//
// 深链协议（纯前端约定，无后端路由）：
//   /overview?issue=<project_id>:<iid>          → 打开该 issue 详情右边栏
//   /overview?repo=<repo_id>[&section=inspirations] → 滚动到该仓库卡片
//     （默认开放 issue 板块卡片；section=inspirations 定位灵感板块卡片）
// 集中成纯函数便于单元测试与前后端搜索跳转逻辑同步。

// 任务跳转：任务详情页
export function taskTarget(taskId) {
  return `/tasks/${String(taskId)}`
}

// issue 跳转：概览页深链（打开 issue 抽屉）
export function issueTarget(projectId, iid) {
  const q = new URLSearchParams({ issue: `${projectId}:${iid}` })
  return `/overview?${q}`
}

// 灵感 / 仓库跳转：概览页深链（滚动定位仓库卡片）
export function repoTarget(repoId, section) {
  const q = new URLSearchParams({ repo: String(repoId) })
  if (section === 'inspirations') q.set('section', 'inspirations')
  return `/overview?${q}`
}

// 概览页滚动定位选择器：section=inspirations 定位灵感板块仓库卡片，
// 其余（含缺省）定位开放 issue 板块仓库卡片（data-repo-id 由
// IssueListSection / InspirationSection 渲染时写入）
export function repoScrollSelector(repoId, section) {
  const cls = section === 'inspirations' ? 'inspiration-repo-card' : 'issue-repo-card'
  return `.${cls}[data-repo-id="${String(repoId)}"]`
}

// 从概览 repoIssues 聚合数据中按 project_id + iid 查找 issue（Overview
// 打开抽屉用）：返回 {repoName, issue}；未找到返回 null（issue 可能已
// 关闭/超出每仓库条数上限，此时仅清理 URL 参数不报错）。
export function findIssueInRepos(repoIssues, projectId, iid) {
  const pid = Number(projectId)
  const iidN = Number(iid)
  for (const entry of repoIssues || []) {
    if (Number(entry.project_id) !== pid) continue
    const issue = (entry.issues || []).find((it) => Number(it.iid) === iidN)
    if (issue) return { repoName: entry.repo_name, issue }
  }
  return null
}
