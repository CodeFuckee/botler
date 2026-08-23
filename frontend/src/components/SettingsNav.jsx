import { useEffect, useLayoutEffect, useMemo, useState } from 'react'
import { Icon } from './Icon.jsx'

// 设置页左侧导航栏（issue #139 架构重构 / issue #155）：
//
// 【架构约定（issue #155）】导航栏结构**不再硬编码**，而是通过
// collectSettingsGroups() 在运行时读取设置页（Settings.jsx 的
// .settings-content 内容区）中**实际渲染**的设置区块：
//   - 分组标题：`<h2 className="settings-group-title">…</h2>`
//     （每个分组的显示名即标题文本，组 id 取标题文本）；
//   - 设置区块：`<section id="…" className="settings-section">…</section>`
//     （id 为锚点，label 默认取区块内首个 `<h2>` 的文本，
//     需要短导航名时用 `data-nav-label` 属性覆盖，
//     搜索关键词走下方 SETTING_KEYWORDS 辅助映射，仅用于搜索不参与结构）。
// 这样左侧导航栏与右侧设置页设置选项**天然一一对应**：任何新设置卡片挂载到
// 设置页后导航栏自动出现对应子选项（如 issue #152 新增「识图模型」卡片后
// 导航栏自动生成「识图模型」子选项），彻底消除两边对不上的 bug；
// 未归属任何分组的区块会进「其他设置」兜底分组，不会悄悄丢失。
//
// 整体折叠（issue #168）：导航面板可整体收成 44px 窄栏（仅保留「展开侧边栏」
// 入口），设置内容区占满全宽；偏好持久化 localStorage，刷新后保持。
//
// 交互能力（issue #139，保持）：1) 顶部搜索框按名称/关键字过滤设置项
// （搜索时自动展开命中分组）；2) 每个分组可折叠/展开其子项；
// 3) 点击子项平滑滚动到页面对应设置区块并高亮。

// 搜索关键词辅助映射（仅用于搜索命中，**不参与导航结构生成**；
// 结构一律来自设置页 DOM，保证两边永远对得上）
export const SETTING_KEYWORDS = {
  'settings-sso': ['sso', 'oidc', '登录', '群晖', 'client_id'],
  'settings-ai-providers': ['ai', 'deepseek', 'openai', 'api', '供应商'],
  'settings-image-models': ['生图', 'gemini', 'gpt', '图像', '模型'],
  'settings-vision-models': ['识图', '视觉', 'vision', 'gemini', 'gpt', '模型', '图片'],
  'settings-minio': ['minio', '对象存储', 's3', '图片上传', 'public_base_url', '桶'],
  'settings-tasks': ['worker', '并行', '重试', '超时', '对账', 'priority'],
  'settings-ui': ['ui', '时区', 'timezone', '显示'],
  'settings-notifications': ['notification', '通知', '浏览器'],
  'settings-alerts': ['alert', '告警', '失败率', '队列', 'token', '磁盘', '阈值'],
  'settings-inspection': ['inspection', '巡检', '健康', 'webhook', 'auto_repair', '间隔'],
  'settings-webhook': ['webhook', '推送', '消息'],
  'settings-auto-issue': ['auto_issue', '失败', '上报', 'issue', 'bug'],
  'settings-claude': ['claude', '命令', '参数', 'anthropic', 'command'],
  'settings-dsh': ['dsh', 'deepseek', 'reasoning', '推理', 'effort'],
  'settings-environment': ['环境', '工具', '版本', '检测', 'env'],
  'settings-backup': ['backup', '备份', '恢复', 'restore', '数据'],
  'settings-audit-logs': ['audit', '审计', '日志', '操作记录', 'admin'],
  'settings-owner-token': ['token', 'owner', 'gitlab', '令牌'],
  'settings-gitlab-cred': ['凭据', 'gitlab', 'bot', 'secret', '凭据'],
  'settings-version': ['version', '版本', '构建', 'build'],
}

// 未归属任何分组标题的设置区块的兜底分组名（保证每个区块都进导航）
const FALLBACK_GROUP_TITLE = '其他设置'

// 侧边栏整体折叠偏好存储键（issue #168）：偏好本地持久化，
// 刷新/重新进入设置页后保持用户上次的折叠/展开选择
export const SIDEBAR_STORAGE_KEY = 'botler.settings.sidebarCollapsed'

/** 读取侧边栏整体折叠偏好：存储值 '1' / 'true' 视为折叠，其余一律展开。
 *  storage：localStorage 兼容对象（测试可注入）；无存储环境（SSR）或
 *  getItem 抛异常（隐私模式）时兜底为展开，不影响页面使用。 */
export function loadSidebarCollapsed(storage) {
  try {
    if (!storage) return false
    const raw = storage.getItem(SIDEBAR_STORAGE_KEY)
    return raw === '1' || raw === 'true'
  } catch {
    return false
  }
}

/** 保存侧边栏整体折叠偏好：true 写 '1'、false 写 '0'；
 *  存储不可用（SSR/隐私模式）时静默忽略，不抛错。 */
export function saveSidebarCollapsed(storage, collapsed) {
  try {
    storage?.setItem(SIDEBAR_STORAGE_KEY, collapsed ? '1' : '0')
  } catch {
    /* 无存储环境：静默忽略，不影响页面使用 */
  }
}

/** 读取设置页内容区，按渲染顺序生成导航分组结构。
 *  contentEl：.settings-content 元素（或测试中结构等价的最小对象）。
 *  返回 [{ id, title, items: [{ id, label, keywords }] }] */
export function collectSettingsGroups(contentEl) {
  const groups = []
  let current = null
  const ensureGroup = () => {
    if (!current) {
      current = { id: FALLBACK_GROUP_TITLE, title: FALLBACK_GROUP_TITLE, items: [] }
      groups.push(current)
    }
    return current
  }

  // 按文档顺序取分组标题与设置区块（querySelectorAll 天然保序；
  // 测试环境可传入结构等价对象，提供 querySelectorAll 或 children）
  const nodes = contentEl?.querySelectorAll
    ? Array.from(contentEl.querySelectorAll('h2.settings-group-title, section.settings-section'))
    : (Array.isArray(contentEl?.children) ? contentEl.children : [])

  for (const el of nodes) {
    // 分组标题：h2.settings-group-title，标题文本即分组名
    const cls = typeof el.className === 'string' ? el.className : ''
    if (el.tagName === 'H2' && cls.split(/\s+/).includes('settings-group-title')) {
      const title = (el.textContent || '').trim()
      current = { id: title, title, items: [] }
      groups.push(current)
      continue
    }
    // 设置区块：section.settings-section；无锚点 id 的区块不进导航
    if (!el.id) continue
    // label：优先 data-nav-label（区块自带短导航名），否则取区块内首个 h2
    const override = typeof el.getAttribute === 'function' ? el.getAttribute('data-nav-label') : null
    const label = (override && override.trim())
      ? override.trim()
      : ((typeof el.querySelector === 'function' && el.querySelector('h2'))?.textContent || '').trim() || el.id
    ensureGroup().items.push({
      id: el.id,
      label,
      keywords: SETTING_KEYWORDS[el.id] || [],
    })
  }
  return groups
}

export default function SettingsNav() {
  // 导航结构从设置页 DOM 读取（issue #155）：组件挂载后扫描
  // .settings-content 内容区生成；useLayoutEffect 在绘制前完成，
  // 避免首帧空导航闪烁
  const [groups, setGroups] = useState([])
  const [query, setQuery] = useState('')
  // 折叠的分组 id 集合（空 = 全部展开）；搜索时强制展开命中分组
  const [collapsed, setCollapsed] = useState(() => new Set())
  // 侧边栏整体折叠（issue #168）：默认展开；偏好存 localStorage，
  // 无存储环境（SSR/隐私模式）默认展开且不崩溃
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    if (typeof localStorage === 'undefined') return false
    return loadSidebarCollapsed(localStorage)
  })
  // 当前高亮的设置区块 id（点击导航项后高亮，滚动离开由页面自然接管）
  const [activeId, setActiveId] = useState(null)

  useLayoutEffect(() => {
    if (typeof document === 'undefined') return
    const content = document.querySelector('.settings-content')
    if (content) setGroups(collectSettingsGroups(content))
  }, [])

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

  // 侧边栏整体折叠开关（issue #168）：状态变化后同步持久化到 localStorage
  useEffect(() => {
    if (typeof localStorage !== 'undefined') {
      saveSidebarCollapsed(localStorage, sidebarCollapsed)
    }
  }, [sidebarCollapsed])

  const toggleSidebar = () => setSidebarCollapsed((prev) => !prev)

  const toggleGroup = (gid) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(gid)) next.delete(gid)
      else next.add(gid)
      return next
    })
  }

  // 全部展开 / 全部收起（搜索时隐藏，避免与自动展开逻辑打架）
  const allCollapsed = !searching && groups.length > 0 && groups.every((g) => collapsed.has(g.id))
  const toggleAll = () => {
    setCollapsed(allCollapsed ? new Set() : new Set(groups.map((g) => g.id)))
  }

  const isCollapsed = (gid) => !searching && collapsed.has(gid)

  const scrollTo = (id) => {
    setActiveId(id)
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <aside
      className={'settings-sidebar' + (sidebarCollapsed ? ' collapsed' : '')}
      aria-label="设置导航"
    >
      {sidebarCollapsed ? (
        <div className="settings-nav-rail">
          <button
            className="settings-nav-rail-toggle"
            onClick={toggleSidebar}
            aria-label="展开侧边栏"
            aria-expanded={false}
            aria-controls="settings-nav-panel"
            title="展开侧边栏"
          >»</button>
        </div>
      ) : (
      <div className="settings-nav" id="settings-nav-panel">
        <div className="settings-nav-head">
          <span className="settings-nav-title">设置导航</span>
          <div className="settings-nav-head-actions">
            {!searching && groups.length > 0 && (
              <button className="settings-nav-toggle-all" onClick={toggleAll}>
                {allCollapsed ? '全部展开' : '全部收起'}
              </button>
            )}
            <button
              className="settings-nav-collapse"
              onClick={toggleSidebar}
              aria-label="收起侧边栏"
              aria-expanded={true}
              aria-controls="settings-nav-panel"
              title="收起侧边栏"
            >«</button>
          </div>
        </div>
        <div className="settings-nav-search">
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
            ><Icon name="x" /></button>
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
                  <span className={'settings-nav-chevron' + (closed ? '' : ' open')} aria-hidden="true"><Icon name="chevronRight" /></span>
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
      )}
    </aside>
  )
}
