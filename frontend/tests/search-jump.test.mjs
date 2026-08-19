// 全局搜索跳转目标解析测试（issue #216）：lib/searchJump.js 纯函数——
// 任务/issue/灵感/仓库结果点击后的跳转路径与概览页深链消费。
//
// 断言：
// 1. taskTarget：任务详情页路径；
// 2. issueTarget：概览页深链 ?issue=<project_id>:<iid>（URLSearchParams
//    编码后冒号转 %3A，解析还原一致）；
// 3. repoTarget：默认定位开放 issue 板块、section=inspirations 定位
//    灵感板块；
// 4. repoScrollSelector：按 section 生成对应板块卡片选择器；
// 5. findIssueInRepos：按 project_id + iid 查找 issue（返回仓库名与
//    issue 对象），未命中 / 空数据 / 异常输入返回 null。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { taskTarget, issueTarget, repoTarget, repoScrollSelector, findIssueInRepos } =
  await vite.ssrLoadModule('/src/lib/searchJump.js')

import { after } from 'node:test'
after(() => vite.close())

test('taskTarget：任务详情页路径', () => {
  assert.equal(taskTarget(7), '/tasks/7')
  assert.equal(taskTarget('7'), '/tasks/7')
})

test('issueTarget：概览页深链（project_id:iid）', () => {
  assert.equal(issueTarget(1, 200), '/overview?issue=1%3A200')
  // 解析还原：URLSearchParams 取出后与原始值一致
  const got = new URLSearchParams('/overview?issue=1%3A200'.split('?')[1]).get('issue')
  assert.equal(got, '1:200')
})

test('repoTarget：默认定位 issue 板块，section=inspirations 定位灵感板块', () => {
  assert.equal(repoTarget(5), '/overview?repo=5')
  assert.equal(repoTarget(5, 'inspirations'), '/overview?repo=5&section=inspirations')
  assert.equal(repoTarget(5, 'issues'), '/overview?repo=5', '非 inspirations 不加 section')
})

test('repoScrollSelector：按 section 生成板块卡片选择器', () => {
  assert.equal(repoScrollSelector(5), '.issue-repo-card[data-repo-id="5"]')
  assert.equal(repoScrollSelector(5, 'issues'), '.issue-repo-card[data-repo-id="5"]')
  assert.equal(repoScrollSelector(5, 'inspirations'), '.inspiration-repo-card[data-repo-id="5"]')
})

test('findIssueInRepos：按 project_id + iid 命中 / 未命中 / 异常输入', () => {
  const repos = [
    { project_id: 1, repo_name: 'alpha', issues: [{ iid: 100, title: 'a' }] },
    { project_id: 2, repo_name: 'beta', issues: [{ iid: 200, title: 'b' }, { iid: 201, title: 'c' }] },
  ]
  assert.deepEqual(findIssueInRepos(repos, 2, 201), { repoName: 'beta', issue: { iid: 201, title: 'c' } })
  assert.equal(findIssueInRepos(repos, 2, 999), null, '未命中返回 null')
  assert.equal(findIssueInRepos(repos, 9, 100), null, '仓库不匹配返回 null')
  assert.equal(findIssueInRepos([], 1, 100), null, '空数据返回 null')
  assert.equal(findIssueInRepos(null, 1, 100), null, 'null 数据容错')
  assert.deepEqual(findIssueInRepos(repos, '2', '201'), { repoName: 'beta', issue: { iid: 201, title: 'c' } }, '字符串数字容错')
})
