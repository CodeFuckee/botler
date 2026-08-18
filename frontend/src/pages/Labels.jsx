import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { confirmDialog } from '../dialog.js'
import { Icon } from '../components/Icon.jsx'

// 标记库页面（issue #29）：默认清单（内置，不可删除）+ 用户自定义标签（可增删）。
// 数据来自 /api/labels：{default: [...], custom: [...]}，增删走 POST/DELETE。
// issue #307：每个默认标签提供「同步到所有仓库」按钮，点击后经
// POST /api/labels/{name}/sync 把该默认标签一键补齐到全部已添加仓库
// （含启用与未启用的），只创建缺失、不覆盖已有颜色/描述。
export default function Labels() {
  const [data, setData] = useState(null) // {default, custom}
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const [color, setColor] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [syncingName, setSyncingName] = useState(null) // 正在同步的默认标签名
  const [note, setNote] = useState(null) // {ok, text}

  const load = async () => {
    setData(await api.get('/api/labels'))
  }

  useEffect(() => { load().catch((e) => setError(e.message)) }, [])

  const add = async () => {
    setBusy(true); setNote(null); setError('')
    try {
      const res = await api.post('/api/labels', { name, color, description })
      setData(res)
      setNote({ ok: true, text: `已添加自定义标签「${name}」` })
      setName(''); setColor(''); setDescription('')
    } catch (e) {
      setNote({ ok: false, text: e.message })
    } finally { setBusy(false) }
  }

  const remove = async (label) => {
    if (!(await confirmDialog({ message: `确认删除自定义标签「${label.name}」？`, danger: true }))) return
    setNote(null)
    try {
      const res = await api.del(`/api/labels/${encodeURIComponent(label.name)}`)
      setData(res)
      setNote({ ok: true, text: `已删除自定义标签「${label.name}」` })
    } catch (e) {
      setNote({ ok: false, text: e.message })
    }
  }

  const syncDefault = async (label) => {
    setSyncingName(label.name); setNote(null); setError('')
    try {
      const res = await api.post(`/api/labels/${encodeURIComponent(label.name)}/sync`)
      const failed = res.failed || []
      const created = res.created?.length ?? 0
      const exists = res.already_exists?.length ?? 0
      let text = `已同步「${label.name}」到 ${res.total_repos ?? 0} 个仓库：新建 ${created} 个、已存在 ${exists} 个`
      if (failed.length) {
        text += `、失败 ${failed.length} 个（${failed.map((f) => f.repo).join('、')}）`
      }
      setNote({ ok: failed.length === 0, text })
    } catch (e) {
      setNote({ ok: false, text: `同步「${label.name}」失败：${e.message}` })
    } finally { setSyncingName(null) }
  }

  const LabelChip = ({ label, removable, onRemove, onSync, syncing }) => (
    <li className="label-chip">
      <span className="label-color" style={{ background: label.color || '#6699cc' }} />
      <span className="label-name">{label.name}</span>
      <span className="muted small label-desc">{label.description}</span>
      {removable
        ? <button className="btn btn-small" onClick={() => onRemove(label)}>删除</button>
        : (
          <>
            <span className="badge badge-default">默认</span>
            <button
              className="btn btn-small"
              title={`同步「${label.name}」到所有已添加仓库`}
              onClick={() => onSync(label)}
              disabled={syncing !== null}
            >
              {syncing === label.name ? '同步中…' : '同步到所有仓库'}
            </button>
          </>
        )}
    </li>
  )

  return (
    <div>
      <h1>标记库</h1>
      <p className="muted">
        统一全部仓库的 issue 标签（规范见 <code>docs/labels.md</code>）。
        默认清单为内置选项，不可删除；可自行添加/删除自定义标签。
        默认标签可一键同步到全部已添加仓库（含启用与未启用的），只补齐缺失、不覆盖已有。
      </p>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {note && (
        <div className={'alert ' + (note.ok ? 'alert-ok' : 'alert-error')} onClick={() => setNote(null)}>
          <Icon name={note.ok ? 'check' : 'x'} /> {note.text}
        </div>
      )}

      <div className="card">
        <h2>添加自定义标签</h2>
        <div className="form-row wrap">
          <input
            className="input"
            placeholder="标签名（字母/数字开头，可含空格 _ -）"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            className="input input-color"
            placeholder="#6699cc"
            value={color}
            onChange={(e) => setColor(e.target.value)}
            title="颜色（#RRGGBB，留空用默认色）"
          />
          <input
            className="input input-desc"
            placeholder="说明（可选）"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
          <button className="btn btn-primary" onClick={add} disabled={busy || !name.trim()}>
            添加
          </button>
        </div>
      </div>

      <div className="card">
        <h2>默认标签（{data?.default?.length ?? '…'} 个，内置不可删除）</h2>
        <p className="muted small">
          点击「同步到所有仓库」将该默认标签自动同步到已添加的全部仓库（包括启用和未启用的），
          目标仓库缺失该标签时创建，已存在则不覆盖。
        </p>
        <ul className="label-list">
          {(data?.default || []).map((l) => (
            <LabelChip key={l.name} label={l} removable={false} onSync={syncDefault} syncing={syncingName} />
          ))}
        </ul>
      </div>

      <div className="card">
        <h2>自定义标签（{data?.custom?.length ?? 0} 个）</h2>
        {data?.custom?.length === 0
          ? (
            <div className="empty-state">
              <span className="empty-icon" aria-hidden="true"><Icon name="tag" /></span>
              <p className="muted">还没有自定义标签，用上方表单添加。</p>
            </div>
          )
          : (
            <ul className="label-list">
              {(data?.custom || []).map((l) => (
                <LabelChip key={l.name} label={l} removable onRemove={remove} />
              ))}
            </ul>
          )}
      </div>
    </div>
  )
}
