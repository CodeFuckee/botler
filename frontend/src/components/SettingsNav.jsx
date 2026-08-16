import { useMemo, useState } from 'react'

// 设置页左侧导航配置（issue #139）：
// 设置项按功能分组，导航栏分组可折叠/展开；items 的 id 为页面内
// 各设置区块的锚点（Settings.jsx 中对应 <section id=...>），
// label 为导航显示名，keywords 参与搜索命中（含中英文别名与配置键）。
export const SETTINGS_GROUPS = [
  {
    id: 'external',
    title: '外部服务接入',
    items: [
      { id: 'settings-sso', label: 'Synology SSO 登录', keywords: ['sso', 'oidc', '登录', '群晖', 'client_id'] },
      { id: 'settings-ai-providers', label: 'AI API 供应商', keywords: ['ai', 'deepseek', 'openai', 'api', '供应商'] },
      { id: 'settings-image-models', label: '生图模型', keywords: ['生图', 'gemini', 'gpt', '图像', '模型'] },
    ],
  },
  {
    id: 'system',
    title: '系统设置',
    items: [
      { id: 'settings-tasks', label: '任务调度', keywords: ['worker', '并行', '重试', '超时', '对账', 'priority'] },
      { id: 'settings-ui', label: '界面显示', keywords: ['ui', '时区', 'timezone', '显示'] },
      { id: 'settings-notifications', label: '网页通知', keywords: ['notification', '通知', '浏览器'] },
      { id: 'settings-webhook', label: '消息推送 Webhook', keywords: ['webhook', '推送', '消息'] },
    ],
  },
  {
    id: 'engines',
    title: '执行引擎',
    items: [
      { id: 'settings-claude', label: 'Claude Code', keywords: ['claude', '命令', '参数', 'anthropic', 'command'] },
      { id: 'settings-dsh', label: 'dsh 引擎', keywords: ['dsh', 'deepseek', 'reasoning', '推理', 'effort'] },
    ],
  },
  {
    id: 'ops',
    title: '运维与数据',
    items: [
      { id: 'settings-environment', label: '本地环境检测', keywords: ['环境', '工具', '版本', '检测', 'env'] },
      { id: 'settings-backup', label: '数据备份', keywords: ['backup', '备份', '恢复', 'restore', '数据'] },
    ],
  },
  {
    id: 'account',
    title: '账号与安全',
    items: [
      { id: 'settings-owner-token', label: 'Owner GitLab Token', keywords: ['token', 'owner', 'gitlab', '令牌'] },
      { id: 'settings-gitlab-cred', label: 'GitLab 凭据（只读）', keywords: ['凭据', 'gitlab', 'bot', 'secret', '凭据'] },
    ],
  },
  {
    id: 'about',
    title: '关于',
    items: [
      { id: 'settings-version', label: '版本信息', keywords: ['version', '版本', '构建', 'build'] },
    ],
  },
]

// 设置页左侧导航栏（issue #139）：分组展示全部设置项，支持
// 1) 顶部搜索框按名称/关键字过滤设置项（搜索时自动展开命中分组）；
// 2) 每个分组可折叠/展开其子项；
// 3) 点击子项平滑滚动到页面对应设置区块并高亮。
export default function SettingsNav({ groups = SETTINGS_GROUPS }) {
  const [query, setQuery] = useState('')
  // 折叠的分组 id 集合（空 = 全部展开）；搜索时强制展开命中分组
  const [collapsed, setCollapsed] = useState(() => new Set())
  // 当前高亮的设置区块 id（点击导航项后高亮，滚动离开由页面自然接管）
  const [activeId, setActiveId] = useState(null)

  const q = query.trim().toLowerCase()
  const searching = q.length > 0

  // 搜索命中：label 与 keywords 任一包含查询词即命中；
  // 未命中任何子项的分组整体隐藏
  const visibleGroups = useMemo(() => {
    if (!searching) return groups
    return groups
      .map((g) => ({
        ...g,
        items: g.items.filter(
          (it) =>
            it.label.toLowerCase().includes(q) ||
            (it.keywords || []).some((k) => k.toLowerCase().includes(q)),
        ),
      }))
      .filter((g) => g.items.length > 0)
  }, [groups, q, searching])

  const matchCount = searching
    ? visibleGroups.reduce((n, g) => n + g.items.length, 0)
    : null

  const toggleGroup = (gid) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(gid)) next.delete(gid)
      else next.add(gid)
      return next
    })
  }

  // 全部展开 / 全部收起（搜索时隐藏，避免与自动展开逻辑打架）
  const allCollapsed = !searching && groups.every((g) => collapsed.has(g.id))
  const toggleAll = () => {
    setCollapsed(allCollapsed ? new Set() : new Set(groups.map((g) => g.id)))
  }

  const isCollapsed = (gid) => !searching && collapsed.has(gid)

  const scrollTo = (id) => {
    setActiveId(id)
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <aside className="settings-sidebar" aria-label="设置导航">
      <div className="settings-nav">
        <div className="settings-nav-head">
          <span className="settings-nav-title">设置导航</span>
          {!searching && (
            <button className="settings-nav-toggle-all" onClick={toggleAll}>
              {allCollapsed ? '全部展开' : '全部收起'}
            </button>
          )}
        </div>
        <div className="settings-nav-search">
          <span className="settings-nav-search-icon" aria-hidden="true">🔍</span>
          <input
            className="input settings-nav-input"
            type="search"
            placeholder="搜索设置项…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="搜索设置项"
          />
          {query && (
            <button
              className="settings-nav-clear"
              onClick={() => setQuery('')}
              aria-label="清空搜索"
              title="清空搜索"
            >×</button>
          )}
        </div>
        {searching && matchCount > 0 && (
          <p className="settings-nav-hits">
            共命中 <strong>{matchCount}</strong> 个设置项
          </p>
        )}
        <nav className="settings-nav-groups">
          {visibleGroups.map((g) => {
            const closed = isCollapsed(g.id)
            return (
              <div className="settings-nav-group" key={g.id}>
                <button
                  className="settings-nav-group-head"
                  onClick={() => toggleGroup(g.id)}
                  aria-expanded={!closed}
                >
                  <span className={'settings-nav-chevron' + (closed ? '' : ' open')} aria-hidden="true">▸</span>
                  <span className="settings-nav-group-title">{g.title}</span>
                  <span className="settings-nav-count">{g.items.length}</span>
                </button>
                {!closed && (
                  <ul className="settings-nav-items">
                    {g.items.map((it) => (
                      <li key={it.id}>
                        <a
                          href={'#' + it.id}
                          className={'settings-nav-item' + (activeId === it.id ? ' active' : '')}
                          onClick={(e) => { e.preventDefault(); scrollTo(it.id) }}
                        >
                          {it.label}
                        </a>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )
          })}
        </nav>
        {searching && visibleGroups.length === 0 && (
          <p className="settings-nav-empty">未找到与「{query}」匹配的设置项，换个关键词试试</p>
        )}
      </div>
    </aside>
  )
}
