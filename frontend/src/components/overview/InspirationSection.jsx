// 灵感板块 + AI 对话抽屉（issue #201 拆分）：从 Overview.jsx 抽出的
// 灵感记录子组件与右侧对话面板，数据由 useOverviewData hook 注入。
import { useI18n } from '../../i18n.jsx'
import { Icon } from '../Icon.jsx'
import { fmtAgo } from '../../api.js'
import { INSPIRATION_POLL_MS } from '../../lib/overview.jsx'

export default function InspirationSection({
  inspirationError, setInspirationError,
  inspirationCreatedIssue, setInspirationCreatedIssue,
  inspirationRepos,
  editingInspiration, setEditingInspiration,
  editInspirationDraft, setEditInspirationDraft,
  saveInspiration, deleteInspiration,
  addIssueFromInspiration, addingIssueInspIds, openInspirationChat,
  submitNewInspiration, newInspirationDrafts, setNewInspirationDrafts,
  chatInspiration, closeInspirationChat, chatLoading, chatMessages,
  chatSending, chatDraft, setChatDraft, chatError, setChatError,
  sendInspirationChat,
}) {
  const { tr } = useI18n()
  return (
    <>
          <section className="inspirations-section">
            <h2><Icon name="lightbulb" /> {tr('overview.inspirationsTitle')}</h2>
            <p className="muted">{tr('overview.inspirationsDesc', { seconds: INSPIRATION_POLL_MS / 1000 })}</p>
            {inspirationError && (
              <div className="alert alert-error" onClick={() => setInspirationError('')}>{inspirationError}</div>
            )}
            {inspirationCreatedIssue && (
              <div className="alert alert-ok" onClick={() => setInspirationCreatedIssue(null)}
                   title="点击关闭">
                <Icon name="checkCircle" /> {tr('overview.inspirationCreated')}{' '}
                <a href={inspirationCreatedIssue.web_url || '#'} target="_blank" rel="noreferrer"
                   onClick={(e) => e.stopPropagation()}>
                  {'issue #' + inspirationCreatedIssue.iid}
                </a>
                {tr('overview.defaultLabels')}
              </div>
            )}
            {inspirationRepos.length === 0 ? (
              <div className="empty-state">
                <span className="empty-icon" aria-hidden="true"><Icon name="lightbulb" /></span>
                <p className="muted">{tr('overview.noInspirations')}</p>
              </div>
            ) : (
              <div className="inspirations-list">
                {inspirationRepos.map((r) => (
                  <div key={r.repo_id} className="card inspiration-repo-card" data-repo-id={r.repo_id}>
                    <div className="inspiration-repo-head">
                      <span className="inspiration-repo-name" title={tr('overview.repoTitle')}><Icon name="folder" /> {r.repo_name || tr('common.deleted')}</span>
                      {r.enabled === false && (
                        <span className="badge badge-muted" title={tr('overview.repoDisabledTitle')}>{tr('common.disabled')}</span>
                      )}
                      <span className="muted">{tr('overview.inspirationCount', { n: (r.inspirations || []).length })}</span>
                    </div>
                    {(r.inspirations || []).length === 0 ? (
                      <div className="empty-state small">
                        <span className="empty-icon" aria-hidden="true"><Icon name="lightbulb" /></span>
                        <p className="muted">{tr('overview.noInspirationPlaceholder')}</p>
                      </div>
                    ) : (
                      <ul className="inspiration-list">
                        {r.inspirations.map((ins) => (
                          <li key={ins.id} className="inspiration-item">
                            {editingInspiration && editingInspiration.id === ins.id ? (
                              <div className="inspiration-edit">
                                <textarea className="input inspiration-textarea"
                                          value={editInspirationDraft}
                                          onChange={(e) => setEditInspirationDraft(e.target.value)}
                                          rows={3} />
                                <div className="inspiration-actions">
                                  <button type="button" className="btn btn-small inspiration-save-btn"
                                          onClick={() => saveInspiration(ins)}
                                          disabled={!editInspirationDraft.trim()}>{tr('common.save')}</button>
                                  <button type="button" className="btn btn-small"
                                          onClick={() => {
                                            setEditingInspiration(null)
                                            setEditInspirationDraft('')
                                          }}>{tr('common.cancel')}</button>
                                </div>
                              </div>
                            ) : (
                              <>
                                <p className="inspiration-content">{ins.content}</p>
                                <div className="inspiration-meta">
                                  <span className="inspiration-time" title={tr('overview.lastUpdated')}>
                                    {fmtAgo(ins.updated_at) || '—'}
                                  </span>
                                  <span className="inspiration-actions">
                                    <button type="button" className="inspiration-action-btn inspiration-add-issue-btn"
                                            title={tr('overview.inspirationAddIssueTitle')}
                                            onClick={() => addIssueFromInspiration(ins)}
                                            disabled={!!addingIssueInspIds[ins.id]}>
                                      {addingIssueInspIds[ins.id] ? <><Icon name="hourglass" /> {tr('overview.submitting')}</> : <><Icon name="pin" /> {tr('overview.addIssue')}</>}
                                    </button>
                                    <button type="button" className="inspiration-action-btn inspiration-chat-btn"
                                            title={tr('overview.inspirationChatTitle')}
                                            onClick={() => openInspirationChat(ins)}><Icon name="message" /> {tr('overview.chat')}</button>
                                    <button type="button" className="inspiration-action-btn"
                                            title={tr('overview.editInspirationTitle')}
                                            onClick={() => {
                                              setEditingInspiration(ins)
                                              setEditInspirationDraft(ins.content)
                                            }}><Icon name="pencil" /> {tr('common.edit')}</button>
                                    <button type="button" className="inspiration-action-btn inspiration-delete-btn"
                                            title={tr('overview.deleteInspirationTitle')}
                                            onClick={() => deleteInspiration(ins)}><Icon name="trash" /> {tr('common.delete')}</button>
                                  </span>
                                </div>
                              </>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                    {/* 随手记录表单：内容去首尾空白非空才允许提交 */}
                    <form className="inspiration-add-form"
                          onSubmit={(e) => { e.preventDefault(); submitNewInspiration(r.repo_id) }}>
                      <textarea className="input inspiration-textarea"
                                placeholder={tr('overview.inspirationPlaceholder')}
                                value={newInspirationDrafts[r.repo_id] || ''}
                                onChange={(e) => setNewInspirationDrafts((prev) => ({ ...prev, [r.repo_id]: e.target.value }))}
                                rows={2} />
                      <button type="submit" className="btn btn-small inspiration-add-btn"
                              disabled={!(newInspirationDrafts[r.repo_id] || '').trim()}><Icon name="plus" /> {tr('overview.record')}</button>
                    </form>
                  </div>
                ))}
              </div>
            )}
          </section>

      {/* issue #166/#184：灵感 AI 对话右侧抽屉——与 AI agent 探讨当前
          灵感（遮罩点击 / × / Esc 关闭，从右侧滑入） */}
      {chatInspiration && (
        <div className="drawer-overlay" onClick={closeInspirationChat}>
          <div className="drawer chat-drawer" role="dialog" aria-modal="true"
               onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <strong><Icon name="message" /> {tr('overview.chatTitle')}</strong>
              <button type="button" className="btn modal-close"
                      onClick={closeInspirationChat} title={tr('common.close')} aria-label={tr('common.close')}><Icon name="x" /></button>
            </div>
            <div className="chat-subject" title={chatInspiration.content}>
              <span className="muted">{tr('overview.chatRepo', { name: chatInspiration.repo_name || '—' })}</span>
              <p className="chat-subject-content">{chatInspiration.content}</p>
            </div>
            <div className="chat-body">
              {chatLoading ? (
                <div className="chat-empty muted">{tr('overview.chatLoading')}</div>
              ) : chatMessages.length === 0 ? (
                <div className="chat-empty muted">{tr('overview.chatEmpty')}</div>
              ) : (
                chatMessages.map((m) => (
                  <div key={m.id}
                       className={'chat-msg ' + (m.role === 'user' ? 'chat-msg-user' : 'chat-msg-ai')}>
                    <div className="chat-msg-bubble">{m.content}</div>
                    <div className="chat-msg-meta">
                      {m.role === 'user' ? tr('overview.me') : 'AI'} · {fmtAgo(m.created_at) || '—'}
                    </div>
                  </div>
                ))
              )}
              {chatSending && <div className="chat-empty muted"><Icon name="bot" /> {tr('overview.aiThinking')}</div>}
              {chatError && (
                <div className="alert alert-error chat-error"
                     onClick={() => setChatError('')}>{chatError}</div>
              )}
            </div>
            <form className="chat-input-row"
                  onSubmit={(e) => { e.preventDefault(); sendInspirationChat() }}>
              <textarea className="input chat-input" rows={2}
                        placeholder={tr('overview.chatPlaceholder')}
                        value={chatDraft}
                        onChange={(e) => setChatDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && !e.shiftKey && !chatSending) {
                            e.preventDefault()
                            sendInspirationChat()
                          }
                        }}
                        disabled={chatSending} />
              <button type="submit" className="btn btn-small chat-send-btn"
                      disabled={chatSending || !chatDraft.trim()}>
                {chatSending ? tr('overview.sending') : tr('overview.send')}
              </button>
            </form>
          </div>
        </div>
      )}
    </>
  )
}
