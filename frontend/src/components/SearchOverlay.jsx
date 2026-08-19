// 全局搜索浮层（issue #216）：跨模块（任务 / issue / 灵感 / 仓库）搜索。
//
// 触发方式：侧边栏搜索框 / 移动端顶栏搜索按钮 / 全站 / 快捷键。
// 交互约定与现有弹窗一致：
// - 关闭：× 按钮 / 点击遮罩 / Esc；
// - 输入防抖（300ms）调 GET /api/search?q=...，结果按模块分组展示，
//   命中关键词以 <mark> 高亮（splitKeyword）；
// - 键盘导航：↑ / ↓ 移动选中项，Enter 跳转（无选中时跳第一条），
//   输入框聚焦时 `/` 等全站快捷键不触发（keymap 防误触，issue #269）；
// - 跳转目标见 lib/searchJump.js——任务 → /tasks/:id；issue →
//   /overview?issue=pid:iid（概览页打开抽屉）；灵感/仓库 →
//   /overview?repo=id[&section=inspirations]（概览页滚动定位）。
//
// 空关键词不请求（提示输入）；响应竞态用自增序号丢弃过期结果
// （快速连续输入时旧响应不得覆盖新响应）。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { Icon } from './Icon.jsx'
import { useI18n } from '../i18n.jsx'
import { splitKeyword } from '../lib/highlightKeyword.js'
import { issueTarget, repoTarget, taskTarget } from '../lib/searchJump.js'

// 输入防抖间隔（毫秒）：键入停顿后发起请求，避免每键一发
const DEBOUNCE_MS = 300
// 模块展示顺序（与后端返回字段一致）：任务 / issue / 灵感 / 仓库
const MODULES = ['tasks', 'issues', 'inspirations', 'repos']

/** 高亮渲染：把文本按关键词切成片段，命中段包 <mark>（语义化高亮）。 */
function Highlight({ text, keyword }) {
  return (
    <>
      {splitKeyword(text, keyword).map((seg, i) => (
        seg.hit ? <mark key={i}>{seg.text}</mark> : <span key={i}>{seg.text}</span>
      ))}
    </>
  )
}

// 结果行副标题：任务显示仓库 · 状态；issue 显示仓库 · 编号；灵感/仓库
// 显示仓库名（灵感）/ URL（仓库），缺失字段静默省略，不展示 undefined
function subtitleOf(module, item) {
  if (module === 'tasks') {
    return [item.repo_name, item.issue_iid != null ? `#${item.issue_iid}` : '']
      .filter(Boolean).join(' · ')
  }
  if (module === 'issues') {
    return [item.repo_name, item.iid != null ? `#${item.iid}` : ''].filter(Boolean).join(' · ')
  }
  if (module === 'inspirations') return item.repo_name || ''
  return item.url || ''
}

export default function SearchOverlay({ onClose }) {
  const { tr } = useI18n()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  // null = 尚未搜索（空关键词提示态）；搜索成功后为 {tasks, issues, ...}
  const [results, setResults] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // 键盘导航选中项下标（-1 = 未选中，Enter 时跳第一条）
  const [activeIndex, setActiveIndex] = useState(-1)
  const inputRef = useRef(null)
  const seqRef = useRef(0) // 响应竞态序号
  const timerRef = useRef(null)

  // 扁平化全部结果（含模块归属），供键盘导航与「Enter 跳第一条」
  const flatItems = useMemo(() => {
    if (!results) return []
    const items = []
    for (const mod of MODULES) {
      for (const item of results[mod] || []) items.push({ module: mod, item })
    }
    return items
  }, [results])

  const go = useCallback((module, item) => {
    let to
    if (module === 'tasks') to = taskTarget(item.id)
    else if (module === 'issues') to = issueTarget(item.project_id, item.iid)
    else if (module === 'inspirations') to = repoTarget(item.repo_id, 'inspirations')
    else to = repoTarget(item.id)
    navigate(to)
    onClose()
  }, [navigate, onClose])

  // 输入防抖搜索：竞态用自增序号丢弃过期响应
  useEffect(() => {
    const term = query.trim()
    if (!term) {
      // 清空关键词：递增序号使 in-flight 旧响应作废（不得渲染过期结果）
      seqRef.current += 1
      setResults(null)
      setError('')
      setActiveIndex(-1)
      return undefined
    }
    setLoading(true)
    setError('')
    const seq = ++seqRef.current
    timerRef.current = setTimeout(() => {
      api.get(`/api/search?q=${encodeURIComponent(term)}`, { silent: true })
        .then((data) => {
          if (seq !== seqRef.current) return // 过期响应：丢弃
          setResults(data)
          setActiveIndex(-1)
        })
        .catch((e) => {
          if (seq !== seqRef.current) return
          setResults({ tasks: [], issues: [], inspirations: [], repos: [] })
          setError(e.message)
        })
        .finally(() => {
          if (seq === seqRef.current) setLoading(false)
        })
    }, DEBOUNCE_MS)
    return () => clearTimeout(timerRef.current)
  }, [query])

  // 自动聚焦输入框（/ 快捷键打开后可直接键入）
  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const onKeyDown = (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      if (flatItems.length) setActiveIndex((i) => (i + 1) % flatItems.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      if (flatItems.length) setActiveIndex((i) => (i <= 0 ? flatItems.length - 1 : i - 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const idx = activeIndex >= 0 ? activeIndex : 0
      const hit = flatItems[idx]
      if (hit) go(hit.module, hit.item)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
    }
  }

  const moduleTitle = (mod) => ({
    tasks: tr('search.tasks'),
    issues: tr('search.issues'),
    inspirations: tr('search.inspirations'),
    repos: tr('search.repos'),
  }[mod])

  return (
    <div className="modal-overlay search-overlay-backdrop" onClick={onClose}>
      <div
        className="search-overlay"
        role="dialog"
        aria-modal="true"
        aria-label={tr('search.title')}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="search-overlay-input-row">
          <Icon name="search" className="search-overlay-icon" aria-hidden="true" />
          <input
            ref={inputRef}
            className="search-overlay-input"
            placeholder={tr('search.placeholder')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            role="searchbox"
            aria-label={tr('search.placeholder')}
          />
          <button
            type="button"
            className="btn modal-close"
            onClick={onClose}
            title={tr('common.close')}
            aria-label={tr('common.close')}
          >
            <Icon name="x" />
          </button>
        </div>
        <div className="search-overlay-hint" aria-hidden="true">{tr('search.jumpHint')}</div>

        <div className="search-overlay-results" role="listbox" aria-label={tr('search.title')}>
          {!query.trim() && (
            <div className="search-overlay-empty">
              <Icon name="search" aria-hidden="true" />
              <p className="muted">{tr('search.emptyHint')}</p>
            </div>
          )}
          {loading && (
            <div className="search-overlay-loading">
              <span className="spinner" aria-hidden="true" />
              <p className="muted">{tr('search.loading')}</p>
            </div>
          )}
          {!loading && error && results && (
            <div className="search-overlay-empty">
              <p className="muted">{tr('search.error', { msg: error })}</p>
            </div>
          )}
          {!loading && !error && results && flatItems.length === 0 && (
            <div className="search-overlay-empty">
              <p className="muted">{tr('search.noResults', { q: query.trim() })}</p>
            </div>
          )}
          {!loading && results && flatItems.length > 0 && (
            MODULES.map((mod) => {
              const items = results[mod] || []
              if (items.length === 0) return null
              return (
                <section key={mod} className="search-group">
                  <h3 className="search-group-title">
                    {moduleTitle(mod)}
                    <span className="badge badge-muted">{items.length}</span>
                  </h3>
                  <ul className="search-group-list">
                    {items.map((item) => {
                      const flatIdx = flatItems.findIndex(
                        (f) => f.module === mod && f.item === item)
                      const active = flatIdx === activeIndex
                      const title = mod === 'tasks' ? item.issue_title
                        : mod === 'issues' ? item.title
                        : mod === 'inspirations' ? item.content
                        : item.name
                      const sub = subtitleOf(mod, item)
                      return (
                        <li key={mod === 'tasks' ? item.id : mod === 'issues' ? `${item.project_id}:${item.iid}` : mod === 'inspirations' ? `i${item.id}` : `r${item.id}`}>
                          <button
                            type="button"
                            role="option"
                            aria-selected={active}
                            className={'search-result' + (active ? ' active' : '')}
                            onMouseEnter={() => setActiveIndex(flatIdx)}
                            onClick={() => go(mod, item)}
                          >
                            <span className="search-result-main">
                              <Highlight text={title} keyword={query.trim()} />
                            </span>
                            {sub && <span className="search-result-sub muted">{sub}</span>}
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                </section>
              )
            })
          )}
        </div>
      </div>
    </div>
  )
}
