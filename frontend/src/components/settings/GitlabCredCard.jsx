// GitLab 凭据（只读）卡片（issue #201 拆分）：从 Settings.jsx 抽出，
// 展示地址 / bot 账号 / bot token / webhook secret 掩码；数据经 props
// 注入（useSettingsData hook），行为与拆分前一致。
export default function GitlabCredCard({ settings }) {
  return (
    <div className="card">
      <h2>GitLab 凭据（只读）</h2>
      <table className="table kv">
        <tbody>
          <tr><th>地址</th><td>{settings.gitlab.url}</td></tr>
          <tr><th>bot 账号</th><td>{settings.gitlab.bot_username || '（自动识别）'}</td></tr>
          <tr><th>bot token</th><td><code>{settings.gitlab.bot_token_masked || '未配置'}</code></td></tr>
          <tr><th>webhook secret</th><td><code>{settings.gitlab.webhook_secret_masked || '未配置'}</code></td></tr>
        </tbody>
      </table>
      <p className="muted small">
        凭据修改请直接编辑 <code>backend/config.yaml</code> 与 <code>.env</code>，然后重启服务。
      </p>
    </div>
  )
}
