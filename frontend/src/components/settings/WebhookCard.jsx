// 消息推送 Webhook 配置卡片（issue #201 拆分）：从 Settings.jsx 抽出。含
// 启用开关 / 地址 / Content-Type / Authorization / POST 结构体（展示全局
// 模板占位符）、「发送测试推送」与「保存 Webhook 配置」独立保存按钮；
// 数据与处理函数经 props 注入（useSettingsData hook），行为与拆分前一致。
import { Icon } from '../Icon.jsx'

export default function WebhookCard({
  settings, setWebhookField, webhookAuthInput, setWebhookAuthInput,
  webhookBusy, webhookTestNote, testWebhook,
  webhookSaveBusy, saveWebhook, webhookSaved,
}) {
  return (
    <div className="card">
      <h2>消息推送 Webhook</h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>启用推送 <code>webhook.enabled</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={settings.webhook?.enabled === true}
                onChange={(e) => setWebhookField('enabled', e.target.checked)}
              />
            </td>
          </tr>
          <tr>
            <th>Webhook 地址 <code>url</code></th>
            <td>
              <input
                className="input grow"
                placeholder="https://example.com/webhook/botler"
                value={settings.webhook?.url || ''}
                onChange={(e) => setWebhookField('url', e.target.value)}
              />
            </td>
          </tr>
          <tr>
            <th>Content-Type <code>content_type</code></th>
            <td>
              <input
                className="input grow"
                placeholder="application/json"
                value={settings.webhook?.content_type || 'application/json'}
                onChange={(e) => setWebhookField('content_type', e.target.value)}
              />
            </td>
          </tr>
          <tr>
            <th>Authorization <code>authorization</code></th>
            <td>
              <input
                type="password"
                className="input grow"
                placeholder={settings.webhook?.authorization_masked
                  ? settings.webhook.authorization_masked
                  : '可选，如 Bearer xxxxx'}
                value={webhookAuthInput}
                onChange={(e) => setWebhookAuthInput(e.target.value)}
              />
              <div className="muted small">
                可选，如 <code>Bearer xxxxx</code>；留空 = 保持现有凭据
              </div>
            </td>
          </tr>
          <tr>
            <th>POST 结构体 <code>body_template</code></th>
            <td>
              <textarea
                className="input textarea"
                rows="8"
                value={settings.webhook?.body_template || ''}
                onChange={(e) => setWebhookField('body_template', e.target.value)}
              />
              <div className="muted small">
                可使用全局模板占位符：
                {Object.keys(settings.templates?.placeholders || {}).map((k) => (
                  <span key={k}> <code>{'{' + k + '}'}</code></span>
                ))}
                ，请求时自动填充；留空 = 内置默认 JSON 模板。
              </div>
            </td>
          </tr>
          <tr>
            <th>测试推送</th>
            <td>
              <button className="btn" onClick={testWebhook} disabled={webhookBusy}>
                {webhookBusy ? '发送中…' : '发送测试推送'}
              </button>
              {webhookTestNote && (
                <span className={webhookTestNote.ok ? 'saved-hint' : 'err-hint'}><Icon name={webhookTestNote.ok ? 'check' : 'x'} /> {webhookTestNote.text}</span>
              )}
            </td>
          </tr>
        </tbody>
      </table>
      <div className="form-row">
        <button className="btn btn-primary" disabled={webhookSaveBusy} onClick={saveWebhook}>
          {webhookSaveBusy ? '保存中…' : '保存 Webhook 配置'}
        </button>
        {webhookSaved && <span className="saved-hint"><Icon name="check" /> Webhook 配置已保存（已写回 config.yaml）</span>}
      </div>
      <p className="muted small">
        任务完成（成功收尾）时调用 webhook 进行消息推送。修改后点击下方「保存 Webhook 配置」立即生效；
        可先点「发送测试推送」验证配置是否可用（推送失败不会影响任务收尾）。
      </p>
    </div>
  )
}
