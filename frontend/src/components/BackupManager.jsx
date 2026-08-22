import { useEffect, useRef, useState } from 'react'
import { Icon } from './Icon.jsx'
import { api, fmtSize } from '../api.js'
import { confirmDialog } from '../dialog.js'

const RESTORE_WARNING = '恢复将覆盖现有数据（config.yaml 与 botler.db）并自动重启服务，重启后正在执行的任务会被重新入队。确定继续？'

export default function BackupManager() {
  const [data, setData] = useState(null)   // {backups, config}
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const fileInput = useRef(null)

  const load = async () => {
    try {
      setData(await api.get('/api/backups'))
    } catch (e) { setError(e.message) }
  }

  useEffect(() => { load() }, [])

  const setConfigField = (key, val) =>
    setData((d) => ({ ...d, config: { ...d.config, [key]: val } }))

  const setRetentionField = (key, val) =>
    setData((d) => ({ ...d, retention: { ...(d.retention || {}), [key]: val } }))

  const saveConfig = async () => {
    setBusy(true); setError(''); setNote('')
    try {
      await api.put('/api/settings', {
        backup: {
          enabled: !!data.config.enabled,
          retention_days: Number(data.config.retention_days),
        },
        retention: {
          enabled: data.retention?.enabled !== false,
          task_logs_days: Number(data.retention?.task_logs_days ?? 90),
          notification_events_days: Number(data.retention?.notification_events_days ?? 30),
          log_files_days: Number(data.retention?.log_files_days ?? 90),
          pm2_max_log_size_mb: Number(data.retention?.pm2_max_log_size_mb ?? 10),
        },
      })
      setNote('备份配置已保存（写回 config.yaml）')
      await load()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const cleanupNow = async () => {
    if (!(await confirmDialog({ message: '将立即按当前保留策略删除过期任务明细、通知和日志文件；任务摘要不会删除。确定继续？', danger: true }))) return
    setBusy(true); setError(''); setNote('')
    try {
      const result = await api.post('/api/retention/cleanup')
      setNote(`清理完成：任务日志 ${result.task_logs} 条、通知 ${result.notification_events} 条、日志文件 ${result.log_files} 个`)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const backupNow = async () => {
    setBusy(true); setError(''); setNote('')
    try {
      const info = await api.post('/api/backups')
      setNote(`备份完成：${info.name}`)
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
    if (!(await confirmDialog({ message: `确定删除备份 ${name}？`, danger: true }))) return
    try {
      setError('')
      await api.del(`/api/backups/${encodeURIComponent(name)}`)
      await load()
    } catch (e) { setError(e.message) }
  }

  const restoreLocal = async (name) => {
    if (!(await confirmDialog({ message: `${RESTORE_WARNING}\n\n备份：${name}`, danger: true }))) return
    setBusy(true); setError(''); setNote('')
    try {
      await api.post('/api/backups/restore', { name })
      setNote('恢复完成，服务正在自动重启，稍后请刷新页面')
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  // picked 由 onChange 直接传入：file 状态在调用 restoreUpload 时尚未更新
  // （setState 异步），旧实现读闭包里的旧值导致首次选文件不触发确认/上传、
  // 二次选择误用上一次的文件（issue #104 补测发现）
  const restoreUpload = async (picked) => {
    if (!picked) return
    if (!(await confirmDialog({ message: `${RESTORE_WARNING}\n\n文件：${picked.name}`, danger: true }))) return
    setBusy(true); setError(''); setNote('')
    try {
      await api.upload('/api/backups/restore/upload', picked)
      setNote('上传恢复完成，服务正在自动重启，稍后请刷新页面')
      if (fileInput.current) fileInput.current.value = ''
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (!data) {
    // 首次加载失败时不能只渲染「加载中…」：错误被吞掉用户会永久卡在加载态，
    // 展示错误并支持点击重试（issue #104 补测发现）
    return (
      <div className="card">
        <h2>数据备份</h2>
        {error
          ? <div className="alert alert-error" onClick={load}>{error}（点击重试）</div>
          : <p className="muted">加载中…</p>}
      </div>
    )
  }

  return (
    <div className="card">
      <h2>数据备份</h2>
      {error && <div className="alert alert-error" onClick={() => setError('')}>{error}</div>}
      {note && <div className="alert alert-ok" onClick={() => setNote('')}><Icon name="check" /> {note}</div>}

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

      <h3>运行数据保留</h3>
      <table className="table kv">
        <tbody>
          <tr>
            <th>启用清理 <code>retention.enabled</code></th>
            <td><label className="checkbox-label"><input type="checkbox" checked={data.retention?.enabled !== false} onChange={(e) => setRetentionField('enabled', e.target.checked)} /> 每天 04:00 自动清理（Asia/Shanghai）</label></td>
          </tr>
          <tr>
            <th>任务明细</th>
            <td><input className="input num-input" type="number" min={1} max={3650} value={data.retention?.task_logs_days ?? 90} onChange={(e) => setRetentionField('task_logs_days', e.target.value)} /> <span className="muted small"> 天；仅清理终态任务的 task_logs，任务摘要保留</span></td>
          </tr>
          <tr>
            <th>执行日志文件</th>
            <td><input className="input num-input" type="number" min={1} max={3650} value={data.retention?.log_files_days ?? 90} onChange={(e) => setRetentionField('log_files_days', e.target.value)} /> <span className="muted small"> 天；清理过期的 task_&lt;id&gt;.log</span></td>
          </tr>
          <tr>
            <th>通知事件</th>
            <td><input className="input num-input" type="number" min={1} max={3650} value={data.retention?.notification_events_days ?? 30} onChange={(e) => setRetentionField('notification_events_days', e.target.value)} /> <span className="muted small"> 天</span></td>
          </tr>
          <tr>
            <th>PM2 日志轮转</th>
            <td><input className="input num-input" type="number" min={1} max={1024} value={data.retention?.pm2_max_log_size_mb ?? 10} onChange={(e) => setRetentionField('pm2_max_log_size_mb', e.target.value)} /> <span className="muted small"> MiB；超过阈值压缩归档</span></td>
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
        <button className="btn" disabled={busy} onClick={cleanupNow}>
          {busy ? '清理中…' : '立即清理过期数据'}
        </button>
        <label className="btn btn-upload">
          上传备份恢复
          <input
            ref={fileInput}
            type="file"
            accept=".tar.gz,application/gzip"
            style={{ display: 'none' }}
            onChange={(e) => {
              const picked = e.target.files[0] || null
              if (picked) restoreUpload(picked)
            }}
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
