// 「网页通知」配置卡片（issue #201 拆分）：从 Settings.jsx 抽出。含启用
// 开关、各通知事件开关（NOTIFY_LABELS）、浏览器授权状态与「弹出测试通知」
// 按钮，以及「保存网页通知配置」独立保存按钮；数据与处理函数经 props
// 注入（useSettingsData hook），行为与拆分前一致。
import { Icon } from '../Icon.jsx'
import { NOTIFY_LABELS } from '../../hooks/useSettingsData.js'

export default function NotificationsCard({
  settings, setNotifyField, handleTestNotify, testNote,
  notifySaveBusy, saveNotify, notifySaved,
}) {
  return (
    <div className="card">
      <h2>网页通知</h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>启用通知 <code>notifications.enabled</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={settings.notifications?.enabled !== false}
                onChange={(e) => setNotifyField('enabled', e.target.checked)}
              />
            </td>
          </tr>
          {Object.entries(NOTIFY_LABELS).map(([key, label]) => (
            <tr key={key}>
              <th>{label} <code>{key}</code></th>
              <td>
                <input
                  type="checkbox"
                  className="check-input"
                  checked={settings.notifications?.[key] !== false}
                  onChange={(e) => setNotifyField(key, e.target.checked)}
                />
              </td>
            </tr>
          ))}
          <tr>
            <th>浏览器授权</th>
            <td>
              {typeof Notification === 'undefined' ? (
                <span className="muted">当前浏览器不支持通知</span>
              ) : window.isSecureContext === false ? (
                <span className="muted">当前页面非安全上下文（需 HTTPS 且证书受信任），通知不可用</span>
              ) : Notification.permission === 'granted' ? (
                <span className="ok-text"><Icon name="check" /> 已授权</span>
              ) : Notification.permission === 'denied' ? (
                <span className="muted">已拒绝（点击地址栏左侧图标将通知权限改为「允许」）</span>
              ) : (
                <button className="btn" onClick={() => Notification.requestPermission()}>
                  授权系统通知
                </button>
              )}
            </td>
          </tr>
          <tr>
            <th>测试通知</th>
            <td>
              <button className="btn" onClick={handleTestNotify}>弹出测试通知</button>
              {testNote && (
                <span className={testNote.ok ? 'saved-hint' : 'err-hint'}><Icon name={testNote.ok ? 'check' : 'x'} /> {testNote.text}</span>
              )}
            </td>
          </tr>
        </tbody>
      </table>
      <div className="form-row">
        <button className="btn btn-primary" disabled={notifySaveBusy} onClick={saveNotify}>
          {notifySaveBusy ? '保存中…' : '保存网页通知配置'}
        </button>
        {notifySaved && <span className="saved-hint"><Icon name="check" /> 网页通知配置已保存（已写回 config.yaml）</span>}
      </div>
      <p className="muted small">
        通过浏览器在电脑上弹出系统通知：任务需要交互（失败）、issue 完成、队列清空、无新任务可处理。
        修改后点击下方「保存网页通知配置」立即生效；需保持本页面打开（浏览器限制），首次启用时请授权系统通知。
      </p>
    </div>
  )
}
