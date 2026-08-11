import { useEffect, useRef, useState } from 'react'
import { api, fmtSize } from '../api.js'

const RESTORE_WARNING = '恢复将覆盖现有数据（config.yaml 与 botler.db）并自动重启服务，重启后正在执行的任务会被重新入队。确定继续？'

export default function BackupManager() {
  const [data, setData] = useState(null)   // {backups, config}
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [file, setFile] = useState(null)
  const fileInput = useRef(null)

  const load = async () => {
    try {
      setData(await api.get('/api/backups'))
    } catch (e) { setError(e.message) }
  }

  useEffect(() => { load() }, [])

  const setConfigField = (key, val) =>
    setData((d) => ({ ...d, config: { ...d.config, [key]: val } }))

  const saveConfig = async () => {
    setBusy(true); setError(''); setNote('')
    try {
      await api.put('/api/settings', {
        backup: {
          enabled: !!data.config.enabled,
          retention_days: Number(data.config.retention_days),
        },
      })
      setNote('✓ 备份配置已保存（写回 config.yaml）')
      await load()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const backupNow = async () => {
    setBusy(true); setError(''); setNote('')
    try {
      const info = await api.post('/api/backups')
      setNote(`✓ 备份完成：${info.name}`)
      await load()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const download = async (name) => {
    try {
      setError('')
      await api.download(`/api/backups/${encodeURIComponent(name)}/download`, name)
    } catch (e) { setError(e.message) }
  }

  const del = async (name) => {
    if (!window.confirm(`确定删除备份 ${name}？`)) return
    try {
      setError('')
      await api.del(`/api/backups/${encodeURIComponent(name)}`)
      await load()
    } catch (e) { setError(e.message) }
  }

  const restoreLocal = async (name) => {
    if (!window.confirm(`${RESTORE_WARNING}\n\n备份：${name}`)) return
    setBusy(true); setError(''); setNote('')
    try {
      await api.post('/api/backups/restore', { name })
      setNote('✓ 恢复完成，服务正在自动重启，稍后请刷新页面')
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const restoreUpload = async () => {
    if (!file) return
    if (!window.confirm(`${RESTORE_WARNING}\n\n文件：${file.name}`)) return
    setBusy(true); setError(''); setNote('')
    try {
      await api.upload('/api/backups/restore/upload', file)
      setNote('✓ 上传恢复完成，服务正在自动重启，稍后请刷新页面')
      setFile(null)
      if (fileInput.current) fileInput.current.value = ''
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (!data) return <p className="muted">加载中…</p>

  return (
    <div className="card">
      <h2>数据备份</h2>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {note && <div className="alert alert-ok" onClick={() => setNote('')}>{note}</div>}

      <table className="table kv">
        <tbody>
          <tr>
            <th>定时备份 <code>enabled</code></th>
            <td>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={!!data.config.enabled}
                  onChange={(e) => setConfigField('enabled', e.target.checked)}
                />
                每天 03:00 自动备份（Asia/Shanghai）
              </label>
            </td>
          </tr>
          <tr>
            <th>保留天数 <code>retention_days</code></th>
            <td>
              <input
                className="input num-input"
                type="number"
                min={1}
                max={365}
                value={data.config.retention_days}
                onChange={(e) => setConfigField('retention_days', e.target.value)}
              />
              <span className="muted small"> 只保留最近 N 天的备份，更旧的自动清理</span>
            </td>
          </tr>
        </tbody>
      </table>

      <div className="form-row">
        <button className="btn btn-primary" disabled={busy} onClick={saveConfig}>
          {busy ? '保存中…' : '保存配置'}
        </button>
        <button className="btn" disabled={busy} onClick={backupNow}>
          {busy ? '备份中…' : '立即备份'}
        </button>
        <label className="btn btn-upload">
          上传备份恢复
          <input
            ref={fileInput}
            type="file"
            accept=".tar.gz,application/gzip"
            style={{ display: 'none' }}
            onChange={(e) => { setFile(e.target.files[0] || null); if (e.target.files[0]) restoreUpload() }}
          />
        </label>
        <span className="saved-hint">备份内容：config.yaml + botler.db，存于服务器 <code>data/backups/</code></span>
      </div>

      <table className="table">
        <thead>
          <tr>
            <th>备份文件</th>
            <th>大小</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {data.backups.length === 0 && (
            <tr><td colSpan={4} className="muted">暂无备份</td></tr>
          )}
          {data.backups.map((b) => (
            <tr key={b.name}>
              <td><code>{b.name}</code></td>
              <td>{fmtSize(b.size)}</td>
              <td>{b.created_at}</td>
              <td>
                <button className="btn btn-sm" onClick={() => download(b.name)}>下载</button>{' '}
                <button className="btn btn-sm" disabled={busy} onClick={() => restoreLocal(b.name)}>恢复</button>{' '}
                <button className="btn btn-sm btn-danger" onClick={() => del(b.name)}>删除</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        恢复会覆盖现有数据并自动重启服务；上传文件将进行完整性校验（manifest + 校验和），
        非 Botler 备份包会被拒绝。
      </p>
    </div>
  )
}
