// 远程服务器（SSH）配置卡片：设置页「执行引擎」分组。管理
// config.yaml remotes 段（远程项目的工作目录所在主机清单，仓库页
// 「远程服务器」方式添加项目时引用），卡片内独立保存（整段列表替换，
// 与 AI 供应商卡片同模式）+ 单台主机连通性测试（SSH echo + zcode 探测）。
import { useEffect, useState } from 'react'
import { api } from '../../api.js'

const EMPTY_FORM = { name: '', host: '', port: 22, user: '', key_path: '', extra_options: '' }

export default function RemotesCard() {
  const [remotes, setRemotes] = useState(null) // null = 加载中
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  // 编辑表单：null = 列表模式；{ index, form }（index 为 null 表示新增）
  const [editing, setEditing] = useState(null)
  // 测试结果：{ key: 主机标识, ok, text }（连通性 + zcode 两段拼合展示）
  const [testNote, setTestNote] = useState(null)
  const [testBusy, setTestBusy] = useState(false)

  const load = () => {
    setError('')
    api.get('/api/settings')
      .then((s) => setRemotes(s.remotes || []))
      .catch((e) => setError(e.message))
  }

  useEffect(() => { load() }, [])

  const startAdd = () => {
    setError(''); setSaved(false); setTestNote(null)
    setEditing({ index: null, form: { ...EMPTY_FORM } })
  }

  const startEdit = (i) => {
    setError(''); setSaved(false); setTestNote(null)
    setEditing({
      index: i,
      form: {
        ...remotes[i],
        // textarea 每行一个 -o 选项 ↔ 数组存储
        extra_options: (remotes[i].extra_options || []).join('\n'),
      },
    })
  }

  const setForm = (key, val) =>
    setEditing((e) => ({ ...e, form: { ...e.form, [key]: val } }))

  const buildList = () => remotes.map((r, i) => {
    if (editing && editing.index === i) {
      const f = editing.form
      return {
        name: String(f.name || '').trim(),
        host: String(f.host || '').trim(),
        port: Number(f.port) || 22,
        user: String(f.user || '').trim(),
        key_path: String(f.key_path || '').trim(),
        extra_options: String(f.extra_options || '').split('\n')
          .map((x) => x.trim()).filter(Boolean),
      }
    }
    return r
  })

  const save = async () => {
    if (editing && (!String(editing.form.name || '').trim()
        || !String(editing.form.host || '').trim())) {
      setError('名称与主机地址不能为空')
      return
    }
    setBusy(true); setError(''); setSaved(false)
    try {
      const list = buildList()
      const res = await api.put('/api/settings', { remotes: list })
      setRemotes(res.remotes || [])
      setEditing(null)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const remove = async (i) => {
    setError(''); setSaved(false); setTestNote(null)
    setRemotes((rs) => rs.filter((_, idx) => idx !== i))
    setEditing(null)
  }

  // 连通性测试：编辑中且该行为新增（index null）时先保存再测（后端按
  // name 查已保存配置）；列表行为直接按 name 测
  const test = async (i) => {
    const r = remotes[i]
    setTestBusy(true); setTestNote(null)
    try {
      const res = await api.post('/api/settings/remotes-test', { name: r.name })
      const parts = []
      if (res.error) {
        setTestNote({ ok: false, text: res.error })
        return
      }
      const c = res.connectivity || {}
      parts.push(c.ok
        ? `SSH 连接正常（${c.latency_ms ?? '—'}ms）`
        : `SSH 连接失败：${c.detail || '未知原因'}`)
      const z = res.zcode
      if (z) parts.push(z.ok ? `zcode：${z.detail}` : `zcode 不可用：${z.detail}`)
      setTestNote({ ok: !!c.ok, text: parts.join('；') })
    } catch (e) {
      setTestNote({ ok: false, text: e.message })
    } finally { setTestBusy(false) }
  }

  return (
    <div className="card">
      <h2>远程服务器 <code>remotes</code></h2>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {saved && <div className="alert alert-ok">已保存（已写回 config.yaml）</div>}
      {remotes === null ? (
        <p className="muted">加载中…</p>
      ) : (
        <>
          <table className="table">
            <thead>
              <tr><th>名称</th><th>地址</th><th>用户</th><th>操作</th></tr>
            </thead>
            <tbody>
              {remotes.length === 0 && (
                <tr><td colSpan={4} className="muted">尚未配置远程服务器</td></tr>
              )}
              {remotes.map((r, i) => (
                <tr key={r.name}>
                  <td><code>{r.name}</code></td>
                  <td>{r.user ? `${r.user}@` : ''}{r.host}
                    {r.port !== 22 ? `:${r.port}` : ''}</td>
                  <td className="muted small">{r.key_path ? '密钥已配置' : '系统默认凭据'}</td>
                  <td>
                    <button type="button" className="btn btn-small"
                            onClick={() => startEdit(i)}>编辑</button>{' '}
                    <button type="button" className="btn btn-small"
                            disabled={testBusy}
                            onClick={() => test(i)}>{testBusy ? '测试中…' : '测试'}</button>{' '}
                    <button type="button" className="btn btn-small btn-danger"
                            onClick={() => remove(i)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {testNote && (
            <div className={testNote.ok ? 'alert alert-ok' : 'alert alert-error'}
                 onClick={() => setTestNote(null)}>{testNote.text}</div>
          )}
          {editing && (
            <div className="remote-edit-form">
              <label className="edit-field">名称（仓库添加时引用）
                <input className="input" value={editing.form.name}
                       onChange={(e) => setForm('name', e.target.value)} />
              </label>
              <label className="edit-field">主机地址
                <input className="input" placeholder="192.168.1.20"
                       value={editing.form.host}
                       onChange={(e) => setForm('host', e.target.value)} />
              </label>
              <label className="edit-field">端口
                <input className="input" type="number" value={editing.form.port}
                       onChange={(e) => setForm('port', e.target.value)} />
              </label>
              <label className="edit-field">用户名（可空）
                <input className="input" value={editing.form.user}
                       onChange={(e) => setForm('user', e.target.value)} />
              </label>
              <label className="edit-field">私钥路径（可空，缺省走 ssh-agent）
                <input className="input" placeholder="~/.ssh/botler_ed25519"
                       value={editing.form.key_path}
                       onChange={(e) => setForm('key_path', e.target.value)} />
              </label>
              <label className="edit-field">附加 ssh 选项（每行一条 -o 配置）
                <textarea className="input" rows={2}
                          value={editing.form.extra_options}
                          onChange={(e) => setForm('extra_options', e.target.value)} />
              </label>
              <button type="button" className="btn btn-small"
                      onClick={() => setEditing(null)}>取消编辑</button>
            </div>
          )}
          <div>
            <button type="button" className="btn btn-small"
                    onClick={startAdd} disabled={!!editing}>添加远程服务器</button>{' '}
            <button type="button" className="btn btn-small btn-primary"
                    onClick={save} disabled={busy}>
              {busy ? '保存中…' : '保存'}
            </button>
          </div>
          <p className="muted small">
            远程服务器用于「远程项目」：项目代码位于其他服务器上的文件夹，
            botler 经 SSH 在该服务器上准备工作区并拉起执行引擎（zcode）。
            前置条件：botler 主机到远程主机 SSH 密钥免密登录，远程主机装有
            git 与 zcode CLI 且能访问 GitLab。新增/修改后需点「保存」再「测试」。
          </p>
        </>
      )}
    </div>
  )
}
