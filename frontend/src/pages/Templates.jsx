import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { confirmDialog } from '../dialog.js'
import { Icon } from '../components/Icon.jsx'

export default function Templates() {
  const [params, setParams] = useSearchParams()
  const [repos, setRepos] = useState([])
  const [globalTemplate, setGlobalTemplate] = useState('')
  // 中断恢复模版（issue #116）：与全局默认模版同机制可编辑
  const [resumeTemplate, setResumeTemplate] = useState('')
  // 结果评论模版（issue #252）：结构化执行报告评论（改动文件/diff 统计/
  // 测试结果），未配置时后端返回空串、渲染层 fallback 内置模版
  const [commentTemplate, setCommentTemplate] = useState('')
  const [globalPlaceholders, setGlobalPlaceholders] = useState({})
  const [placeholders, setPlaceholders] = useState({})
  // kind: 'default' 全局默认 / 'resume' 中断恢复 / 'repo' 仓库级
  const [selected, setSelected] = useState(null) // {repoId|null, kind, isOverride}
  const [text, setText] = useState('')
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  // 模版编辑器折叠状态（issue #56）：默认全部展开，高度自适应内容
  // 完整展示、无内层垂直滚动；折叠方式与任务详情页聊天记录一致
  // （issue #52 SectionToggle 标题行切换），折叠时编辑器整体隐藏
  const [expanded, setExpanded] = useState(true)

  const load = async () => {
    const [reposData, settings] = await Promise.all([
      api.get('/api/repos'),
      api.get('/api/settings'),
    ])
    setRepos(reposData.repos)
    setGlobalTemplate(settings.templates.default)
    // 中断恢复模版（issue #116）：未配置时后端返回内置默认，作为可编辑基线
    setResumeTemplate(settings.templates.resume || '')
    // 结果评论模版（issue #252）：未配置时后端返回空串，渲染层 fallback 内置模版
    setCommentTemplate(settings.templates.comment || '')
    // 全局模板同样支持占位符（issue #25：此前全局视图占位符表格为空，
    // 用户误以为占位符未生效）
    const phs = settings.templates.placeholders || {}
    setGlobalPlaceholders(phs)
    const repoParam = Number(params.get('repo')) || null
    if (repoParam) {
      await selectRepo(repoParam)
    } else {
      setSelected({ repoId: null, kind: 'default', isOverride: false })
      setPlaceholders(phs)
      setText(settings.templates.default)
    }
  }

  useEffect(() => { load().catch((e) => setError(e.message)) }, [])

  const selectRepo = async (repoId) => {
    const data = await api.get(`/api/repos/${repoId}/template`)
    setSelected({ repoId, kind: 'repo', isOverride: data.is_override })
    setText(data.template)
    setPlaceholders(data.placeholders)
  }

  const save = async () => {
    setError('')
    setSaved(false)
    try {
      if (selected.kind === 'repo') {
        await api.put(`/api/repos/${selected.repoId}/template`, { template: text })
        await selectRepo(selected.repoId)
      } else if (selected.kind === 'resume') {
        // 中断恢复模版（issue #116）：留空保存 = 恢复内置默认
        const settings = await api.put('/api/settings', { templates: { resume: text } })
        setResumeTemplate(settings.templates.resume || '')
      } else if (selected.kind === 'comment') {
        // 结果评论模版（issue #252）：留空保存 = 恢复内置默认
        const settings = await api.put('/api/settings', { templates: { comment: text } })
        setCommentTemplate(settings.templates.comment || '')
      } else {
        const settings = await api.get('/api/settings')
        await api.put('/api/settings', { templates: { default: text } })
        setGlobalTemplate(text)
        void settings
      }
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      setError(e.message)
    }
  }

  const clearOverride = async () => {
    if (!(await confirmDialog({ message: '清空仓库级模版覆盖，回退为全局默认模版？' }))) return
    try {
      await api.put(`/api/repos/${selected.repoId}/template`, { template: '' })
      await selectRepo(selected.repoId)
    } catch (e) { setError(e.message) }
  }

  const selectGlobal = () => {
    setSelected({ repoId: null, kind: 'default', isOverride: false })
    setText(globalTemplate)
    setPlaceholders(globalPlaceholders)
  }

  const selectResume = () => {
    // 中断恢复模版（issue #116）：与全局默认同机制，支持全部占位符
    setSelected({ repoId: null, kind: 'resume', isOverride: false })
    setText(resumeTemplate)
    setPlaceholders(globalPlaceholders)
  }

  const selectComment = () => {
    // 结果评论模版（issue #252）：与全局默认同机制，支持全部占位符
    setSelected({ repoId: null, kind: 'comment', isOverride: false })
    setText(commentTemplate)
    setPlaceholders(globalPlaceholders)
  }

  return (
    <div>
      <h1>提示词模版</h1>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}

      <div className="card">
        <div className="form-row wrap">
          <button
            className={'btn ' + (selected?.kind === 'default' ? 'btn-primary' : '')}
            onClick={selectGlobal}
          >
            全局默认模版
          </button>
          <button
            className={'btn ' + (selected?.kind === 'resume' ? 'btn-primary' : '')}
            onClick={selectResume}
          >
            中断恢复模版
          </button>
          <button
            className={'btn ' + (selected?.kind === 'comment' ? 'btn-primary' : '')}
            onClick={selectComment}
          >
            结果评论模版
          </button>
          {repos.map((r) => (
            <button
              key={r.id}
              className={'btn ' + (selected?.kind === 'repo' && selected?.repoId === r.id ? 'btn-primary' : '')}
              onClick={() => selectRepo(r.id)}
            >
              {r.name}{!r.enabled ? '（停用）' : ''}
            </button>
          ))}
        </div>

        <p className="muted small">
          {selected?.kind === 'resume'
            ? '中断恢复模版：平台重启/中断后恢复会话时的引导语（claude/hermes/dsh 三引擎通用）。留空保存即恢复内置默认。'
            : selected?.kind === 'comment'
              ? '结果评论模版：任务收尾时在 issue 上留的结构化执行报告（改动文件表格 / 测试摘要 / commit 链接 / 用时）。'
                + '占位符 {diff_stat} / {test_summary} / {commit_link} / {duration} 等仅评论模版生效，空段落自动隐藏。留空保存即恢复内置默认。'
              : selected?.kind === 'repo'
              ? selected?.isOverride
                ? '仓库级模版：覆盖全局默认。'
                : '该仓库未配置覆盖，当前显示全局默认模版。编辑并保存即创建覆盖。'
              : '全局默认模版：所有未配置仓库级模版的仓库使用。'}
        </p>

        <button
          type="button"
          className="section-toggle section-toggle-h3"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          <span className="chevron">{expanded ? <Icon name="chevronDown" /> : <Icon name="chevronRight" />}</span>
          模版内容
          <span className="muted small">（{text.split('\n').length} 行）</span>
        </button>

        {expanded && (
          <>
            <textarea
              className="input textarea"
              rows={text.split('\n').length + 1}
              value={text}
              onChange={(e) => setText(e.target.value)}
              spellCheck={false}
            />

            <div className="form-row">
              <button className="btn btn-primary" onClick={save}>保存</button>
              {saved && <span className="saved-hint"><Icon name="check" /> 已保存</span>}
              {selected?.kind === 'repo' && selected?.isOverride && (
                <button className="btn" onClick={clearOverride}>清空覆盖</button>
              )}
            </div>
          </>
        )}
      </div>

      <div className="card">
        <h2>可用变量占位符</h2>
        <table className="table">
          <thead><tr><th>占位符</th><th>含义</th></tr></thead>
          <tbody>
            {Object.entries(placeholders).map(([k, v]) => (
              <tr key={k}>
                <td><code>{'{' + k + '}'}</code></td>
                <td>{v}</td>
              </tr>
            ))}
            {selected?.kind === 'repo' && Object.keys(placeholders).length === 0 && (
              <tr><td colSpan={2}>（仓库数据未加载）</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
