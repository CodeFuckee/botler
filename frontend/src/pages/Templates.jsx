import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, fmtTime } from '../api.js'
import { confirmDialog } from '../dialog.js'
import { Icon } from '../components/Icon.jsx'

export default function Templates() {
  const [params] = useSearchParams()
  const [repos, setRepos] = useState([])
  const [globalTemplate, setGlobalTemplate] = useState('')
  // 中断恢复模版（issue #116）：与全局默认模版同机制可编辑
  const [resumeTemplate, setResumeTemplate] = useState('')
  // 结果评论模版（issue #252）：结构化执行报告评论（改动文件/diff 统计/
  // 测试结果），未配置时后端返回空串、渲染层 fallback 内置模版
  const [commentTemplate, setCommentTemplate] = useState('')
  const [globalPlaceholders, setGlobalPlaceholders] = useState({})
  const [placeholders, setPlaceholders] = useState({})
  // kind: 'default' 全局默认 / 'resume' 中断恢复 / 'comment' 结果评论 / 'repo' 仓库级
  const [selected, setSelected] = useState(null) // {repoId|null, kind, isOverride}
  const [text, setText] = useState('')
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  // issue #223：正文注入控制——原始描述是否进 prompt 开关 + 注入长度上限
  const [rawBodyInPrompt, setRawBodyInPrompt] = useState(true)
  const [bodyMaxChars, setBodyMaxChars] = useState(8000)
  const [controlsSaved, setControlsSaved] = useState(false)
  // 模版编辑器折叠状态（issue #56）：默认全部展开，高度自适应内容
  // 完整展示、无内层垂直滚动；折叠方式与任务详情页聊天记录一致
  // （issue #52 SectionToggle 标题行切换），折叠时编辑器整体隐藏
  const [expanded, setExpanded] = useState(true)
  // issue #262：模板版本历史——当前模板 key 的版本信息（最新版本号/时间）
  // 与历史版本列表；保存生成新版本、历史可查看、可回滚
  const [versionInfo, setVersionInfo] = useState(null)
  const [versions, setVersions] = useState([])
  const [showHistory, setShowHistory] = useState(false)

  // 拉取版本历史（最新在前），版本信息加载失败不阻塞模板编辑
  const loadVersions = async (key) => {
    if (!key) return
    try {
      const data = await api.get(`/api/template-versions?key=${encodeURIComponent(key)}`, { silent: true })
      setVersionInfo(data.latest)
      setVersions(data.versions || [])
    } catch {
      setVersionInfo(null)
      setVersions([])
    }
  }

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
    // issue #223：正文注入控制（原始描述开关 + 长度上限）
    setRawBodyInPrompt(settings.templates.raw_body_in_prompt !== false)
    setBodyMaxChars(settings.templates.body_max_chars ?? 8000)
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
      // issue #262：加载全局默认模板版本历史
      await loadVersions('global:default')
    }
  }

  useEffect(() => {
    // 挂载时仅加载一次：load 只使用模块级 api 与稳定 setter，每次渲染
    // 重建引用，加入 deps 会形成「加载→setState→重渲染→再加载」死循环
    load().catch((e) => setError(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selectRepo = async (repoId) => {
    const data = await api.get(`/api/repos/${repoId}/template`)
    setSelected({ repoId, kind: 'repo', isOverride: data.is_override })
    setText(data.template)
    setPlaceholders(data.placeholders)
    // issue #262：加载仓库级模板版本历史
    await loadVersions(`repo:${repoId}`)
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
        await loadVersions('global:resume')
      } else if (selected.kind === 'comment') {
        // 结果评论模版（issue #252）：留空保存 = 恢复内置默认
        const settings = await api.put('/api/settings', { templates: { comment: text } })
        setCommentTemplate(settings.templates.comment || '')
        await loadVersions('global:comment')
      } else {
        const settings = await api.get('/api/settings')
        await api.put('/api/settings', { templates: { default: text } })
        setGlobalTemplate(text)
        void settings
        await loadVersions('global:default')
      }
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      setError(e.message)
    }
  }

  const saveInjectionControls = async () => {
    setError('')
    setControlsSaved(false)
    const max = Number(bodyMaxChars)
    if (!Number.isInteger(max) || max < 0) {
      setError('正文注入长度上限必须是非负整数')
      return
    }
    try {
      const settings = await api.put('/api/settings', { templates: {
        raw_body_in_prompt: !!rawBodyInPrompt,
        body_max_chars: max,
      } })
      setRawBodyInPrompt(settings.templates.raw_body_in_prompt !== false)
      setBodyMaxChars(settings.templates.body_max_chars ?? 8000)
      setControlsSaved(true)
      setTimeout(() => setControlsSaved(false), 2000)
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

  // issue #262：回滚到指定历史版本——确认后调用回滚 API，回滚立即生效，
  // 更新当前编辑文本与对应模板 state，并刷新版本历史
  const rollback = async (vid) => {
    const v = versions.find((x) => x.id === vid)
    if (!v) return
    if (!(await confirmDialog({
      message: `确定回滚到版本 v${v.version_no}（${fmtTime(v.created_at)}）？回滚后模板立即生效，且回滚本身会生成一条新版本记录。`,
    }))) return
    try {
      setError('')
      const data = await api.post(`/api/template-versions/${vid}/rollback`, {})
      if (selected.kind === 'repo') {
        await selectRepo(selected.repoId)
      } else if (selected.kind === 'resume') {
        setResumeTemplate(data.content)
        setText(data.content)
        await loadVersions('global:resume')
      } else if (selected.kind === 'comment') {
        setCommentTemplate(data.content)
        setText(data.content)
        await loadVersions('global:comment')
      } else {
        setGlobalTemplate(data.content)
        setText(data.content)
        await loadVersions('global:default')
      }
      setShowHistory(false)
    } catch (e) { setError(e.message) }
  }

  const selectGlobal = () => {
    setSelected({ repoId: null, kind: 'default', isOverride: false })
    setText(globalTemplate)
    setPlaceholders(globalPlaceholders)
    loadVersions('global:default')
  }

  const selectResume = () => {
    // 中断恢复模版（issue #116）：与全局默认同机制，支持全部占位符
    setSelected({ repoId: null, kind: 'resume', isOverride: false })
    setText(resumeTemplate)
    setPlaceholders(globalPlaceholders)
    loadVersions('global:resume')
  }

  const selectComment = () => {
    // 结果评论模版（issue #252）：与全局默认同机制，支持全部占位符
    setSelected({ repoId: null, kind: 'comment', isOverride: false })
    setText(commentTemplate)
    setPlaceholders(globalPlaceholders)
    loadVersions('global:comment')
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
          {' 每次保存自动生成新版本（内容不变不重复记录），历史版本可查看与回滚（issue #262）。'}
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
          {versionInfo && (
            <span className="muted small"> · 当前版本 v{versionInfo.version_no}（{fmtTime(versionInfo.created_at)}）</span>
          )}
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
              <button
                type="button"
                className="btn"
                onClick={() => setShowHistory(!showHistory)}
                disabled={!versions.length}
              >
                历史版本{versions.length ? `（${versions.length}）` : ''}
              </button>
              {selected?.kind === 'repo' && selected?.isOverride && (
                <button className="btn" onClick={clearOverride}>清空覆盖</button>
              )}
            </div>

            {showHistory && (
              <div style={{ marginTop: 8 }}>
                <h3>历史版本</h3>
                <p className="muted small">
                  每次保存生成新版本（相同内容不重复记录）；回滚到旧版后模板立即生效，任务将使用回滚后的版本。
                </p>
                {!versions.length ? (
                  <p className="muted small">暂无历史版本</p>
                ) : (
                  <table className="table">
                    <thead>
                      <tr><th>版本</th><th>保存时间</th><th>备注</th><th>内容预览</th><th>操作</th></tr>
                    </thead>
                    <tbody>
                      {versions.map((v) => (
                        <tr key={v.id}>
                          <td>
                            v{v.version_no}
                            {versionInfo?.id === v.id && <span className="muted small">（当前）</span>}
                          </td>
                          <td>{fmtTime(v.created_at)}</td>
                          <td className="muted small">{v.note || '—'}</td>
                          <td className="muted small">
                            <code>{v.content.slice(0, 50)}{v.content.length > 50 ? '…' : ''}</code>
                          </td>
                          <td>
                            {versionInfo?.id !== v.id && (
                              <button className="btn btn-sm" onClick={() => rollback(v.id)}>回滚</button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </>
        )}
      </div>

      <div className="card">
        <h2>正文注入控制</h2>
        <p className="muted small">
          issue 标题/描述常含 <code>#</code>、<code>%</code>、反引号、换行等特殊字符，
          直接拼进 prompt 可能破坏模板结构或被模型误解（issue #223）。
          平台额外提供 URL 编码占位符 <code>{'{issue_title_urlenc}'}</code> /
          <code>{'{issue_body_urlenc}'}</code>（特殊字符安全，见下方占位符表）；
          此处控制原始正文的注入行为。
        </p>
        <label className="form-row">
          <input
            type="checkbox"
            checked={rawBodyInPrompt}
            onChange={(e) => setRawBodyInPrompt(e.target.checked)}
          />
          <span>原始描述进 prompt（关闭后 <code>{'{issue_body}'}</code> 渲染为指向 issue 链接的提示，防 prompt injection）</span>
        </label>
        <div className="form-row">
          <label className="muted small">正文注入最大字符数（0 = 不截断，超长自动截断并标注长度与链接）</label>
          <input
            type="number"
            min="0"
            step="1"
            className="input"
            style={{ maxWidth: 160 }}
            value={bodyMaxChars}
            onChange={(e) => setBodyMaxChars(e.target.value)}
          />
        </div>
        <div className="form-row">
          <button className="btn btn-primary" onClick={saveInjectionControls}>保存</button>
          {controlsSaved && <span className="saved-hint"><Icon name="check" /> 已保存</span>}
        </div>
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
