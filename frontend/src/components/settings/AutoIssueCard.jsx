// 「任务失败自动上报」配置卡片（issue #347）：任务失败收尾时自动在任务
// 所属项目创建失败上报 issue——标题写明失败任务 id，正文含失败原因 / 失败
// 分类 / 处理建议 / 原始 issue 链接，标签 bug + bot-failed（bot-failed 防止
// 对账调度器把上报 issue 重新领取形成死循环），负责人按 assignee 指定。
// 含启用开关 / 负责人输入与「保存配置」独立保存按钮；数据与处理函数经
// props 注入（useSettingsData hook），与 Webhook / 网页通知卡片同模式。
import { Icon } from '../Icon.jsx'

export default function AutoIssueCard({
  settings, setAutoIssueField,
  autoIssueSaveBusy, saveAutoIssue, autoIssueSaved,
}) {
  return (
    <div className="card">
      <h2>任务失败自动上报</h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>启用自动上报 <code>auto_issue.enabled</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={settings.auto_issue?.enabled !== false}
                onChange={(e) => setAutoIssueField('enabled', e.target.checked)}
              />
              <div className="muted small">
                任务失败收尾时自动创建 GitLab issue（标题写明失败任务 id，
                分配 bug 标签）。默认开启。
              </div>
            </td>
          </tr>
          <tr>
            <th>负责人 <code>assignee</code></th>
            <td>
              <input
                className="input grow"
                placeholder="agent"
                value={settings.auto_issue?.assignee || ''}
                onChange={(e) => setAutoIssueField('assignee', e.target.value)}
              />
              <div className="muted small">
                上报 issue 的 GitLab 负责人用户名（默认 <code>agent</code>）；
                解析失败时不指定负责人，不阻塞上报。
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div className="form-row">
        <button className="btn btn-primary" disabled={autoIssueSaveBusy} onClick={saveAutoIssue}>
          {autoIssueSaveBusy ? '保存中…' : '保存自动上报配置'}
        </button>
        {autoIssueSaved && <span className="saved-hint"><Icon name="check" /> 自动上报配置已保存（已写回 config.yaml）</span>}
      </div>
      <p className="muted small">
        任务失败（重试耗尽 / 无法解决 / 等待用户决策等失败终态）时，自动在任务所属项目创建
        失败上报 issue 并指定你为负责人，方便集中排查失败任务；同一任务只上报一次。
      </p>
    </div>
  )
}
