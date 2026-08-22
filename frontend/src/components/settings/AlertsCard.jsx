// 「聚合告警」配置卡片（issue #229）：平台异常主动通知——任务失败率过高 /
// 队列堆积无进度 / GitLab token 失效 / 磁盘空间不足时，经现有通知通道
// （网页通知 in_app + webhook 推送）主动提醒，替代「用户打开页面才发现」。
// 检测并入对账循环（reconciler 定时扫描），本卡片配置各告警开关与阈值，
// 独立保存（只提交 alerts 段，与「网页通知」卡片同模式）。
import { Icon } from '../Icon.jsx'

const num = (v) => Number(v)

export default function AlertsCard({
  settings, setAlertField, alertSaveBusy, saveAlerts, alertSaved,
}) {
  const a = settings.alerts || {}
  return (
    <div className="card">
      <h2>聚合告警</h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>启用聚合告警 <code>alerts.enabled</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={a.enabled !== false}
                onChange={(e) => setAlertField('enabled', e.target.checked)}
              />
            </td>
          </tr>
          <tr>
            <th>任务失败率告警 <code>notify_failure_rate</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={a.notify_failure_rate !== false}
                onChange={(e) => setAlertField('notify_failure_rate', e.target.checked)}
              />
              <span className="muted small"> 近 1 小时失败率超过阈值时通知</span>
            </td>
          </tr>
          <tr>
            <th>失败率阈值（%） <code>failure_rate_threshold</code></th>
            <td>
              <input
                className="input grow"
                type="number"
                min="0"
                max="100"
                step="1"
                value={a.failure_rate_threshold ?? 50}
                onChange={(e) => setAlertField('failure_rate_threshold', num(e.target.value))}
              />
              <div className="muted small">如 50 = 失败率超过 50% 触发（默认 50）</div>
            </td>
          </tr>
          <tr>
            <th>失败率统计窗口（秒） <code>failure_rate_window</code></th>
            <td>
              <input
                className="input grow"
                type="number"
                min="60"
                step="60"
                value={a.failure_rate_window ?? 3600}
                onChange={(e) => setAlertField('failure_rate_window', num(e.target.value))}
              />
              <div className="muted small">默认 3600 = 近 1 小时</div>
            </td>
          </tr>
          <tr>
            <th>队列堆积告警 <code>notify_queue_backlog</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={a.notify_queue_backlog !== false}
                onChange={(e) => setAlertField('notify_queue_backlog', e.target.checked)}
              />
              <span className="muted small"> 活跃任务积压且长时间无进度时通知</span>
            </td>
          </tr>
          <tr>
            <th>队列积压条数阈值 <code>queue_backlog_threshold</code></th>
            <td>
              <input
                className="input grow"
                type="number"
                min="1"
                step="1"
                value={a.queue_backlog_threshold ?? 5}
                onChange={(e) => setAlertField('queue_backlog_threshold', num(e.target.value))}
              />
              <div className="muted small">活跃任务（排队中 + 运行中）超过该值才可能触发（默认 5）</div>
            </td>
          </tr>
          <tr>
            <th>无进度判定窗口（分钟） <code>queue_stall_minutes</code></th>
            <td>
              <input
                className="input grow"
                type="number"
                min="1"
                step="1"
                value={a.queue_stall_minutes ?? 30}
                onChange={(e) => setAlertField('queue_stall_minutes', num(e.target.value))}
              />
              <div className="muted small">窗口内无任何任务收尾 = 无进度（默认 30）</div>
            </td>
          </tr>
          <tr>
            <th>GitLab token 失效告警 <code>notify_token_invalid</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={a.notify_token_invalid !== false}
                onChange={(e) => setAlertField('notify_token_invalid', e.target.checked)}
              />
              <span className="muted small"> 启动/对账时探测到 401/403 立即通知</span>
            </td>
          </tr>
          <tr>
            <th>Token 到期预警 <code>notify_token_expiry</code></th>
            <td>
              <input type="checkbox" className="check-input"
                     checked={a.notify_token_expiry !== false}
                     onChange={(e) => setAlertField('notify_token_expiry', e.target.checked)} />
              <span className="muted small"> 在 30 / 7 / 3 天阈值及到期后分级提醒，不重复刷屏</span>
            </td>
          </tr>
          <tr>
            <th>磁盘空间告警 <code>notify_disk_low</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={a.notify_disk_low !== false}
                onChange={(e) => setAlertField('notify_disk_low', e.target.checked)}
              />
            </td>
          </tr>
          <tr>
            <th>磁盘剩余阈值（MiB） <code>disk_min_free_mb</code></th>
            <td>
              <input
                className="input grow"
                type="number"
                min="1"
                step="1"
                value={a.disk_min_free_mb ?? 512}
                onChange={(e) => setAlertField('disk_min_free_mb', num(e.target.value))}
              />
              <div className="muted small">数据目录剩余空间低于该值触发（默认 512 MiB）</div>
            </td>
          </tr>
          <tr>
            <th>告警节流窗口（秒） <code>throttle_seconds</code></th>
            <td>
              <input
                className="input grow"
                type="number"
                min="60"
                step="60"
                value={a.throttle_seconds ?? 3600}
                onChange={(e) => setAlertField('throttle_seconds', num(e.target.value))}
              />
              <div className="muted small">同类告警在此窗口内不重复通知（默认 3600 = 1 小时）</div>
            </td>
          </tr>
        </tbody>
      </table>
      <div className="form-row">
        <button className="btn btn-primary" disabled={alertSaveBusy} onClick={saveAlerts}>
          {alertSaveBusy ? '保存中…' : '保存告警配置'}
        </button>
        {alertSaved && <span className="saved-hint"><Icon name="check" /> 告警配置已保存（已写回 config.yaml）</span>}
      </div>
      <p className="muted small">
        平台无人值守时主动通知异常：近 1 小时任务失败率超过阈值、队列积压且无进度、
        GitLab token 失效（401/403）、磁盘空间不足。检测并入对账循环自动执行；
        告警经现有通知通道分发——网页通知（受「网页通知」总开关与浏览器授权
        控制）与 Webhook 推送（受「消息推送 Webhook」启用开关控制）。修改后点击
        「保存告警配置」立即生效。
      </p>
    </div>
  )
}
