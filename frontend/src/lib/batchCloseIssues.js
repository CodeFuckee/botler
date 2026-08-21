// 批量关闭 Issue 的纯逻辑（issue #412）：复用单条关闭接口，
// 在前端完成去重、参数校验和部分失败归集，避免改变既有权限校验链路。

function issueKey(issue) {
  return `${issue.project_id}:${issue.iid}`
}

function validIssue(issue) {
  return issue && Number.isInteger(issue.project_id) && issue.project_id > 0
    && Number.isInteger(issue.iid) && issue.iid > 0
}

/**
 * 逐条关闭 Issue，保证一个失败不会阻断其余请求。
 * @param {Array<object>|null} issues 待关闭 Issue
 * @param {(issue: object) => Promise<unknown>} closeIssue 单条关闭函数
 * @returns {Promise<{succeeded: object[], failed: Array<{issue: object, error: Error}>}>}
 */
export async function closeIssuesInBatch(issues, closeIssue) {
  const succeeded = []
  const failed = []
  if (!Array.isArray(issues) || typeof closeIssue !== 'function') {
    return { succeeded, failed }
  }

  const unique = []
  const seen = new Set()
  for (const issue of issues) {
    if (!validIssue(issue)) {
      failed.push({ issue, error: new Error('Issue 参数无效，无法关闭') })
      continue
    }
    const key = issueKey(issue)
    if (seen.has(key)) continue
    seen.add(key)
    unique.push(issue)
  }

  const settled = await Promise.allSettled(unique.map(async (issue) => {
    await closeIssue(issue)
    return issue
  }))
  settled.forEach((result, index) => {
    const issue = unique[index]
    if (result.status === 'fulfilled') {
      succeeded.push(issue)
    } else {
      const error = result.reason instanceof Error
        ? result.reason
        : new Error(String(result.reason || '关闭失败'))
      failed.push({ issue, error })
    }
  })
  return { succeeded, failed }
}
