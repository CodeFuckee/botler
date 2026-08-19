// Claude Code 引擎配置卡片（issue #201 拆分）：从 Settings.jsx 抽出，
// 命令 / 参数输入与认证环境说明；数据与处理函数经 props 注入
// （useSettingsData hook），行为与拆分前一致。
export default function ClaudeCard({ settings, setSettings }) {
  return (
    <div className="card">
      <h2>Claude Code</h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>命令 <code>command</code></th>
            <td>
              <input
                className="input grow"
                value={settings.claude.command}
                onChange={(e) => setSettings((s) => ({ ...s, claude: { ...s.claude, command: e.target.value } }))}
              />
            </td>
          </tr>
          <tr>
            <th>参数 <code>args</code></th>
            <td>
              <input
                className="input grow"
                value={settings.claude.args.join(' ')}
                onChange={(e) => setSettings((s) => ({
                  ...s,
                  claude: { ...s.claude, args: e.target.value.split(/\s+/) },
                }))}
              />
            </td>
          </tr>
        </tbody>
      </table>
      <p className="muted small">
        认证继承自服务器环境变量：
        {settings.env.anthropic_base_url
          ? <> <code>ANTHROPIC_BASE_URL={settings.env.anthropic_base_url}</code> · <code>ANTHROPIC_MODEL={settings.env.anthropic_model}</code></>
          : '（未配置 ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY，请检查 .env）'}
      </p>
    </div>
  )
}
