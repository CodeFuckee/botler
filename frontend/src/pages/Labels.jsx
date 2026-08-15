import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { confirmDialog } from '../dialog.js'

// 标记库页面（issue #29）：默认清单（内置，不可删除）+ 用户自定义标签（可增删）。
// 数据来自 /api/labels：{default: [...], custom: [...]}，增删走 POST/DELETE。
export default function Labels() {
  const [data, setData] = useState(null) // {default, custom}
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const [color, setColor] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
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
      setNote({ ok: true, text: `✓ 已添加自定义标签「${name}」` })
      setName(''); setColor(''); setDescription('')
    } catch (e) {
      setNote({ ok: false, text: `✗ ${e.message}` })
    } finally { setBusy(false) }
  }

  const remove = async (label) => {
    if (!(await confirmDialog({ message: `确认删除自定义标签「${label.name}」？`, danger: true }))) return
    setNote(null)
    try {
      const res = await api.del(`/api/labels/${encodeURIComponent(label.name)}`)
      setData(res)
      setNote({ ok: true, text: `✓ 已删除自定义标签「${label.name}」` })
    } catch (e) {
      setNote({ ok: false, text: `✗ ${e.message}` })
    }
  }

  const LabelChip = ({ label, removable, onRemove }) => (
    <li className="label-chip">
      <span className="label-color" style={{ background: label.color || '#6699cc' }} />
      <span className="label-name">{label.name}</span>
      <span className="muted small label-desc">{label.description}</span>
      {removable
        ? <button className="btn btn-small" onClick={() => onRemove(label)}>删除</button>
        : <span className="badge badge-default">默认</span>}
    </li>
  )

  return (
    <div>
      <h1>标记库</h1>
      <p className="muted">
        统一全部仓库的 issue 标签（规范见 <code>docs/labels.md</code>）。
        默认清单为内置选项，不可删除；可自行添加/删除自定义标签。
      </p>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {note && (
        <div className={'alert ' + (note.ok ? 'alert-ok' : 'alert-error')} onClick={() => setNote(null)}>
          {note.text}
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
        <ul className="label-list">
          {(data?.default || []).map((l) => (
            <LabelChip key={l.name} label={l} removable={false} />
          ))}
        </ul>
      </div>

      <div className="card">
        <h2>自定义标签（{data?.custom?.length ?? 0} 个）</h2>
        {data?.custom?.length === 0
          ? <p className="muted">还没有自定义标签，用上方表单添加。</p>
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
