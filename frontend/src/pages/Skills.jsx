import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { confirmDialog } from '../dialog.js'
import { Icon } from '../components/Icon.jsx'
import Markdown from '../components/Markdown.jsx'

// 技能管理页面（issue #282）：显示所有配置的执行引擎（executor 插件）
// 拥有的技能，并查看 / 编辑 skill.md（SKILL.md）以及其他 skill 相关
// md 文件。
//
// 数据来自 /api/skills：
//   {engine, engines: [{name, description, default, roots: [{path, exists}],
//                       skills: [{name, description, path, root}]}]}
// 技能 = 引擎技能根目录下含 SKILL.md 的目录（name 为相对根目录的路径，
// 可嵌套如 software-development/spike）；每个技能目录内的 md 文件可
// 查看 / 编辑（保存走 PUT /api/skills/{engine}/file，安全约束见后端）。
//
// 交互：引擎 tab 切换 → 技能列表 → md 文件 chips → 编辑器（textarea +
// Markdown 预览切换 + 保存）；切换文件/技能前有未保存改动会先确认。

// 引擎技能根目录展示名（内置引擎惯例路径，与后端 skills.engine_skills_roots 对应）
const ENGINE_ROOT_HINTS = {
  claude: '~/.claude/skills',
  hermes: '$HERMES_HOME/skills',
  dsh: '$DSH_HOME/skills + ~/.agents/skills',
}

function skillUrl(engine, skill, path) {
  // 技能名 / 文件路径含斜杠，统一 query 参数编码（与后端路由一致）
  const params = new URLSearchParams({ skill })
  if (path !== undefined) params.set('path', path)
  return `/api/skills/${encodeURIComponent(engine)}/files?${params}`
}

export default function Skills() {
  const [data, setData] = useState(null) // GET /api/skills 结果
  const [error, setError] = useState('')
  const [note, setNote] = useState(null) // {ok, text} 操作结果提示
  const [engine, setEngine] = useState('') // 当前引擎名
  const [skill, setSkill] = useState(null) // 当前技能 {name, root}
  const [files, setFiles] = useState([]) // 当前技能 md 文件（相对路径）
  const [file, setFile] = useState('') // 当前文件相对路径
  const [content, setContent] = useState('') // 编辑器内容
  const [original, setOriginal] = useState('') // 加载时的原内容（脏检查）
  const [preview, setPreview] = useState(false) // Markdown 预览开关
  const [busy, setBusy] = useState(false) // 加载 / 保存中

  const load = async () => {
    const d = await api.get('/api/skills')
    setData(d)
    // 默认选中：配置的默认引擎优先，其次第一个有技能的引擎
    const preferred = d.engines.find((e) => e.skills.length > 0)
    const first = d.engines.find((e) => e.name === d.engine && e.skills.length > 0)
      || preferred || d.engines[0]
    if (first) {
      setEngine(first.name)
      if (first.skills.length) await openSkill(first.name, first.skills[0])
    }
  }

  useEffect(() => {
    // 挂载时仅加载一次：load 只使用模块级 api 与稳定 setter，每次渲染
    // 重建引用，加入 deps 会形成「加载→setState→重渲染→再加载」死循环
    load().catch((e) => setError(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 打开技能：拉取 md 文件列表，默认选中 SKILL.md（无则第一个文件）
  const openSkill = async (engineName, skillObj) => {
    setBusy(true); setError(''); setNote(null)
    try {
      const resp = await api.get(
        skillUrl(engineName, skillObj.name))
      const list = resp.files || []
      setSkill(skillObj)
      setFiles(list)
      const preferred = list.find((f) => f.toLowerCase().endsWith('skill.md')) || list[0]
      if (preferred) {
        await openFile(engineName, skillObj.name, preferred, list)
      } else {
        setFile(''); setContent(''); setOriginal('')
      }
    } catch (e) {
      setError(e.message)
    } finally { setBusy(false) }
  }

  // 打开文件：读取内容并记录原始值（脏检查基线）
  const openFile = async (engineName, skillName, rel, list = files) => {
    setPreview(false)
    try {
      const doc = await api.get(
        `/api/skills/${encodeURIComponent(engineName)}/file?${new URLSearchParams({ skill: skillName, path: rel })}`)
      setFile(rel)
      setContent(doc.content)
      setOriginal(doc.content)
    } catch (e) {
      setError(e.message)
      setFile(''); setContent(''); setOriginal('')
      if (list) setFiles(list)
    }
  }

  // 切换引擎：清空选择并打开该引擎第一个技能（有未保存修改先确认）
  const switchEngine = async (name) => {
    if (name === engine) return
    if (!(await confirmDiscard())) return
    const e = data.engines.find((x) => x.name === name)
    setEngine(name); setSkill(null); setFiles([]); setFile(''); setPreview(false)
    if (e && e.skills.length) {
      await openSkill(name, e.skills[0])
    } else {
      setContent(''); setOriginal('')
    }
  }

  // 未保存改动确认（切换技能/文件前）
  const confirmDiscard = async () => {
    if (file && content !== original) {
      return confirmDialog({
        message: `「${file}」有未保存的修改，确定放弃并切换？`,
      })
    }
    return true
  }

  const selectSkill = async (skillObj) => {
    if (skillObj === skill) return
    if (!(await confirmDiscard())) return
    await openSkill(engine, skillObj)
  }

  const selectFile = async (rel) => {
    if (rel === file) return
    if (!(await confirmDiscard())) return
    await openFile(engine, skill.name, rel)
  }

  // 保存：PUT /api/skills/{engine}/file（后端校验技能/文件路径合法性）
  const save = async () => {
    setBusy(true); setNote(null); setError('')
    try {
      const resp = await api.put(`/api/skills/${encodeURIComponent(engine)}/file`, {
        skill: skill.name, path: file, content,
      })
      setOriginal(content)
      setNote({ ok: true, text: `已保存 ${resp.path}（${resp.size} 字节）` })
    } catch (e) {
      setNote({ ok: false, text: e.message })
    } finally { setBusy(false) }
  }

  const dirty = !!file && content !== original

  if (!data) return (
    <div className="loading-hint">
      <span className="spinner" aria-hidden="true" />
      <span className="muted">加载中…</span>
    </div>
  )

  const engineData = data.engines.find((e) => e.name === engine) || data.engines[0]

  return (
    <div>
      <h1>技能管理</h1>
      <p className="muted">
        展示所有配置的执行引擎（executor 插件）所拥有的技能：每个技能是引擎
        技能目录下含 SKILL.md 的目录，可查看 / 编辑 SKILL.md 及其他技能相关
        md 文件（仅支持 md 文档，保存即时生效，删除请通过文件系统操作）。
      </p>

      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {note && (
        <div className={'alert ' + (note.ok ? 'alert-ok' : 'alert-error')} onClick={() => setNote(null)}>
          <Icon name={note.ok ? 'check' : 'x'} /> {note.text}
        </div>
      )}

      {/* 引擎切换（与插件页「执行引擎」同源：executor 插件列表） */}
      <div className="skills-engine-tabs" role="tablist" aria-label="执行引擎">
        {data.engines.map((e) => (
          <button
            key={e.name}
            role="tab"
            aria-selected={e.name === engine}
            className={'skills-engine-tab' + (e.name === engine ? ' active' : '')}
            onClick={() => switchEngine(e.name)}
          >
            {e.name}
            {e.default && <span className="badge badge-default">默认</span>}
            <span className="muted small">（{e.skills.length} 个技能）</span>
          </button>
        ))}
      </div>

      {engineData && (
        <div className="skills-body">
          {/* 左：技能列表 */}
          <div className="skills-list">
            <h2>
              技能列表
              <span className="badge badge-muted">{engineData.skills.length}</span>
            </h2>
            {engineData.skills.length === 0 ? (
              <p className="muted small">
                该引擎暂无技能（{ENGINE_ROOT_HINTS[engineData.name] || '未配置技能目录'}
                {engineData.roots.length === 0 ? '，外部引擎可在插件类声明 skills_dir' : ''}）。
              </p>
            ) : (
              <ul className="skills-list-items">
                {engineData.skills.map((s) => (
                  <li key={s.name}>
                    <button
                      className={'skills-skill-btn' + (skill && skill.name === s.name ? ' active' : '')}
                      onClick={() => selectSkill(s)}
                      title={s.description || s.path}
                    >
                      <span className="skills-skill-name">{s.name}</span>
                      <span className="muted small skills-skill-desc">
                        {s.description || '（无描述）'}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* 右：文件列表 + 编辑器 */}
          <div className="skills-editor">
            {!skill ? (
              <p className="muted small">从左侧选择一个技能查看 / 编辑其 md 文件。</p>
            ) : (
              <>
                <div className="skills-file-head">
                  <h2>{skill.name}</h2>
                  <span className="muted small skills-root-path">
                    <Icon name="folder" /> {skill.root}
                  </span>
                </div>
                {files.length === 0 ? (
                  <p className="muted small">该技能目录下没有 md 文件。</p>
                ) : (
                  <>
                    <div className="skills-files">
                      {files.map((f) => (
                        <button
                          key={f}
                          className={'skills-file-chip' + (f === file ? ' active' : '')}
                          onClick={() => selectFile(f)}
                        >
                          <Icon name="fileText" /> {f}
                          {f.toLowerCase().endsWith('skill.md') &&
                            <span className="badge badge-default">技能说明</span>}
                        </button>
                      ))}
                    </div>
                    {file && (
                      <div className="skills-edit">
                        <div className="form-row">
                          <span className="muted small skills-file-path">
                            {skill.name}/{file}
                            {dirty && <span className="badge badge-warn">未保存</span>}
                          </span>
                          <span className="form-row-spacer" />
                          <button className="btn btn-sm" onClick={() => setPreview(!preview)}>
                            <Icon name={preview ? 'pencil' : 'eye'} />
                            {preview ? '编辑' : '预览'}
                          </button>
                          <button className="btn btn-sm btn-primary" disabled={busy || !dirty} onClick={save}>
                            {busy ? '保存中…' : '保存'}
                          </button>
                        </div>
                        {preview ? (
                          <div className="skills-preview">
                            <Markdown content={content} />
                          </div>
                        ) : (
                          <textarea
                            className="input textarea skills-textarea"
                            rows={22}
                            value={content}
                            onChange={(e) => setContent(e.target.value)}
                            aria-label={`编辑 ${file}`}
                            spellCheck="false"
                          />
                        )}
                      </div>
                    )}
                  </>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
