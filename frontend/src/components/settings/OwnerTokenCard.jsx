// Owner GitLab Token 配置卡片（issue #201 拆分）：从 Settings.jsx 抽出。
// 该 token 专用于编辑 issue（评论/标签），严禁用于推送代码与处理流水线，
// 系统架构层已隔离（issue #130）——所有 Agent 均不可使用。卡片含密码
// 输入框、独立「保存 Owner Token」按钮与「查看 token 申请教程」折叠区；
// 数据与处理函数经 props 注入（useSettingsData hook），行为与拆分前一致。
import { Icon } from '../Icon.jsx'
import Markdown from '../Markdown.jsx'

export default function OwnerTokenCard({
  settings, ownerTokenInput, setOwnerTokenInput, ownerBusy, saveOwnerToken,
  ownerSaved, ownerGuide, ownerGuideError, ownerGuideOpen,
  setOwnerGuideOpen, setOwnerGuideError, setOwnerTokenExpiry, saveOwnerTokenExpiry,
}) {
  return (
    <div className="card">
      <h2>
        Owner GitLab Token（issue 编辑专用）
        <span className="badge badge-muted owner-token-isolated">已隔离 · Agent 不可用</span>
      </h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>Token <code>gitlab.owner_token</code></th>
            <td>
              <input
                className="input grow"
                type="password"
                placeholder={settings.gitlab?.owner_token_masked
                  ? `已配置（${settings.gitlab.owner_token_masked}），留空 = 保持现有`
                  : '粘贴 GitLab Personal Access Token（glpat-xxxx）'}
                value={ownerTokenInput}
                onChange={(e) => setOwnerTokenInput(e.target.value)}
              />
            </td>
          </tr>
        </tbody>
      </table>
      <div className="form-row">
        <label className="edit-field grow">
          到期日 <code>owner_token_expires_at</code>
          <input className="input" type="date" value={settings.gitlab?.owner_token_expires_at || ''}
                 onChange={(e) => setOwnerTokenExpiry?.(e.target.value)} />
          <span className="muted small">留空表示未记录；可由 GitLab token 信息自动回填。</span>
        </label>
        <button className="btn btn-primary" disabled={ownerBusy} onClick={saveOwnerToken}>
          {ownerBusy ? '保存中…' : '保存 Owner Token'}
        </button>
        <button className="btn" disabled={ownerBusy} onClick={saveOwnerTokenExpiry}>
          保存到期日
        </button>
        {ownerSaved && (
          <span className="saved-hint"><Icon name="check" /> Owner token 已保存（已写回 config.yaml）</span>
        )}
      </div>
      {settings.gitlab?.owner_token_expiry?.level && settings.gitlab.owner_token_expiry.level !== 'unknown' && (
        <p className={`badge token-expiry-${settings.gitlab.owner_token_expiry.level}`}>
          Token 到期状态：{settings.gitlab.owner_token_expiry.level}（剩余 {settings.gitlab.owner_token_expiry.days_remaining} 天）
        </p>
      )}
      <p className="muted small">
        <Icon name="lock" /> <strong>隔离状态</strong>：该 token 已由系统架构隔离，<strong>所有 Agent
        均不可使用</strong>——Agent 处理 issue 时只能使用自己仓库的认证 token 进行
        issue 编辑，会话环境中绝不会注入此 token。
      </p>
      <p className="muted small">
        <strong>允许使用范围</strong>：仅限在概览页面上编辑 issue、添加 issue、关闭 issue、
        在 issue 添加评论以及回复 issue 评论时由平台使用；其他场景（推送代码、处理流水线
        等）一律不得使用，botler 绝不会用它推送代码或处理流水线——推送与流水线操作仍
        使用 bot token。推荐用仓库 Reporter 角色的低权限账号申请（账号权限层面杜绝越权
        使用），申请步骤见下方「查看 token 申请教程」。留空保存 = 保持现有 token。
      </p>
      <div className="guide-box">
        <button className="btn" onClick={() => setOwnerGuideOpen((v) => !v)}>
          {ownerGuideOpen ? '收起 token 申请教程' : '查看 token 申请教程'}
        </button>
        {ownerGuideOpen && (
          <div className="guide-content">
            {ownerGuideError && (
              <div className="alert alert-error" onClick={() => setOwnerGuideError('')}>
                教程文档不可用：{ownerGuideError}
              </div>
            )}
            {!ownerGuide && !ownerGuideError && <p className="muted">教程加载中…</p>}
            {ownerGuide && <Markdown content={ownerGuide} />}
          </div>
        )}
      </div>
    </div>
  )
}
