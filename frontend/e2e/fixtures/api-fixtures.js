// E2E mock API 夹具（issue #212）
// 仅覆盖依赖真实 GitLab 的接口（开放 issue 聚合 / CI/CD 流水线 / 添加
// issue 表单元数据 / 创建 issue），其余接口（settings / tasks / 灵感 /
// 通知 / auth / SSE 事件流）走真实后端（uvicorn），保证前后端契约
// 真实验证 + GitLab 零依赖的确定性。

// 概览页开放 issue 聚合（GET /api/issues/overview）
// 函数形式返回，避免用例间共享同一对象被意外改写
export function ISSUES_FIXTURE() {
  return {
    repos: [
      {
        repo_id: 1,
        repo_name: 'botler',
        priority: 10,
        issues: [
          {
            iid: 212,
            title: '无端到端测试（Playwright），关键用户流程无浏览器级保障',
            labels: [{ name: 'test', color: 'F0AD4E', text_color: 'FFFFFF' }],
            milestone: null,
            updated_at: '2026-08-17 23:23:38',
            web_url: 'https://gitlab.example.com/botler/-/issues/212',
            assignees: [{ username: 'agent', name: 'Agent', avatar_url: '' }],
            user_notes_count: 2,
          },
          {
            iid: 101,
            title: 'E2E 示例任务：修复概览页按钮样式',
            labels: [
              { name: 'bot-done', color: '1F75CB', text_color: 'FFFFFF' },
              { name: 'ui', color: '69D100', text_color: 'FFFFFF' },
            ],
            milestone: 'v1.0',
            updated_at: '2026-08-17 22:00:00',
            web_url: 'https://gitlab.example.com/botler/-/issues/101',
            assignees: [],
            user_notes_count: 1,
          },
        ],
      },
      {
        repo_id: 2,
        repo_name: 'shipyard',
        priority: 20,
        issues: [],
      },
    ],
    errors: [],
    total: 2,
  }
}

// CI/CD 流水线概览（GET /api/pipelines/overview）：空列表即可
export const PIPELINES_FIXTURE = { pipelines: [], errors: [] }

// 添加 Issue 弹窗表单元数据（GET /api/issues/form-meta/{repo_id}）
export const FORM_META_FIXTURE = {
  members: [
    { id: 20, username: 'agent', name: 'Agent' },
    { id: 21, username: 'dev', name: 'Dev' },
  ],
  labels: [
    { name: 'bug', color: 'FF0000', text_color: 'FFFFFF' },
    { name: 'ui', color: '69D100', text_color: 'FFFFFF' },
  ],
}

// 创建 issue 成功返回（POST /api/issues 的 mock 响应）
export function CREATED_ISSUE(body) {
  return {
    iid: 999,
    project_id: body.repo_id,
    title: body.title,
    web_url: `https://gitlab.example.com/botler/-/issues/999`,
  }
}
