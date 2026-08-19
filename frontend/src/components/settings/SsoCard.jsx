import { Icon } from '../Icon.jsx'
import Markdown from '../Markdown.jsx'
// Synology SSO 登录配置卡片（issue #201 拆分）：从 Settings.jsx 抽出，
// 表单字段与「保存 SSO 配置」独立保存按钮、配置指南折叠区；数据与
// 处理函数经 props 注入（useSettingsData hook），行为与拆分前一致。
export default function SsoCard({
  settings, ssoSecretInput, setSsoSecretInput, ssoBusy, ssoSaved, saveSso,
  setSsoField, guide, guideError, guideOpen, setGuideOpen, setGuideError,
}) {
  return (
    <div className="card">
      <h2>Synology SSO 登录</h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>启用 SSO <code>sso.enabled</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={settings.sso?.enabled === true}
                onChange={(e) => setSsoField('enabled', e.target.checked)}
              />
              <span className="muted small">
                {settings.sso?.enabled
                  ? '启用后访问本界面需用群晖账号登录'
                  : '未启用时保持开放访问（无需登录）'}
              </span>
            </td>
          </tr>
          <tr>
            <th>Well-known URL <code>well_known_url</code></th>
            <td>
              <input
                className="input grow"
                placeholder="https://群晖地址/.well-known/openid-configuration"
                value={settings.sso?.well_known_url || ''}
                onChange={(e) => setSsoField('well_known_url', e.target.value.trim())}
              />
            </td>
          </tr>
          <tr>
            <th>Application ID（Client ID）<code>client_id</code></th>
            <td>
              <input
                className="input grow"
                placeholder="群晖 SSO Server 应用配置中的 Application ID"
                value={settings.sso?.client_id || ''}
                onChange={(e) => setSsoField('client_id', e.target.value.trim())}
              />
            </td>
          </tr>
          <tr>
            <th>Application Secret <code>client_secret</code></th>
            <td>
              <input
                className="input grow"
                type="password"
                placeholder={settings.sso?.client_secret_masked
                  ? `已配置（${settings.sso.client_secret_masked}），留空 = 保持现有`
                  : '群晖 SSO Server 应用配置中的 Application Secret'}
                value={ssoSecretInput}
                onChange={(e) => setSsoSecretInput(e.target.value)}
              />
            </td>
          </tr>
          <tr>
            <th>Scope <code>scope</code></th>
            <td>
              <input
                className="input grow"
                placeholder="openid profile email"
                value={settings.sso?.scope || ''}
                onChange={(e) => setSsoField('scope', e.target.value.trim())}
              />
            </td>
          </tr>
          <tr>
            <th>登录有效期（天）<code>session_days</code></th>
            <td>
              <input
                className="input num-input"
                type="number"
                min={1}
                max={365}
                value={settings.sso?.session_days ?? 30}
                onChange={(e) => setSsoField('session_days', e.target.value)}
              />
            </td>
          </tr>
          <tr>
            <th>回调地址 <code>redirect_uri</code></th>
            <td>
              <input
                className="input grow"
                placeholder="留空 = 按浏览器访问地址自动生成（https://主机/api/auth/callback）"
                value={settings.sso?.redirect_uri || ''}
                onChange={(e) => setSsoField('redirect_uri', e.target.value.trim())}
              />
            </td>
          </tr>
          <tr>
            <th>校验群晖证书 <code>verify_ssl</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={settings.sso?.verify_ssl !== false}
                onChange={(e) => setSsoField('verify_ssl', e.target.checked)}
              />
              <span className="muted small">群晖为自签名证书时取消勾选</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div className="form-row">
        <button className="btn btn-primary" disabled={ssoBusy} onClick={saveSso}>
          {ssoBusy ? '保存中…' : '保存 SSO 配置'}
        </button>
        {ssoSaved && <span className="saved-hint"><Icon name="check" /> SSO 配置已保存（已写回 config.yaml）</span>}
      </div>
      <p className="muted small">
        接入群晖 SSO Server（OIDC 协议）：先在群晖「SSO Server → 应用程序」新增 OIDC 应用，
        填写回调地址并记下 Application ID / Secret，再回此页填写并保存。
        修改后点击下方「保存 SSO 配置」生效；启用后当前会话不受影响，下次访问需登录。
        完整的配置步骤（含群晖侧设置与常见问题）见下方「查看 SSO 配置指南」。
      </p>
      <div className="guide-box">
        <button className="btn" onClick={() => setGuideOpen((v) => !v)}>
          {guideOpen ? '收起 SSO 配置指南' : '查看 SSO 配置指南'}
        </button>
        {guideOpen && (
          <div className="guide-content">
            {guideError && (
              <div className="alert alert-error" onClick={() => setGuideError('')}>
                指南文档不可用：{guideError}
              </div>
            )}
            {!guide && !guideError && <p className="muted">指南加载中…</p>}
            {guide && <Markdown content={guide} />}
          </div>
        )}
      </div>
    </div>
  )
}
