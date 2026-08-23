// 灵感板块 + AI 对话抽屉（issue #201 拆分）：从 Overview.jsx 抽出的
// 灵感记录子组件与右侧对话面板，数据由 useOverviewData hook 注入。
import { useRef } from 'react'
import { useI18n } from '../../i18n.jsx'
import { Icon } from '../Icon.jsx'
import { ScrollContainerBackToTop } from '../BackToTop.jsx'
import { fmtAgo } from '../../api.js'
import { INSPIRATION_POLL_MS } from '../../lib/overview.jsx'

// 对话输入框自动增高（issue #443 codex 风格）：内容超出单行时随内容撑高，
// 达到上限（CHAT_INPUT_MAX_HEIGHT）后内部滚动；测试环境事件对象无真实
// DOM（无 style 属性）时静默跳过，不抛错。
export const CHAT_INPUT_MAX_HEIGHT = 160
export function autoGrowChatInput(e) {
  const el = e && e.target
  if (!el || !el.style) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight || 0, CHAT_INPUT_MAX_HEIGHT) + 'px'
}

export default function InspirationSection({
  inspirationError, setInspirationError,
  inspirationCreatedIssue, setInspirationCreatedIssue,
  inspirationRepos, expandedInspirationRepoIds, inspirationPages, inspirationPageLoading,
  toggleInspirationRepo, loadMoreInspirations,
  editingInspiration, setEditingInspiration,
  editInspirationDraft, setEditInspirationDraft,
  saveInspiration, deleteInspiration,
  addIssueFromInspiration, addingIssueInspIds, openInspirationChat,
  submitNewInspiration, newInspirationDrafts, setNewInspirationDrafts,
  chatInspiration, closeInspirationChat, chatLoading, chatMessages,
  chatSending, chatDraft, setChatDraft, chatError, setChatError,
  sendInspirationChat,
}) {
  // issue #457：AI 对话抽屉滚动容器 ref——右下角「回到顶部」按钮定位/监听于此
  const chatDrawerRef = useRef(null)
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
                    {(() => {
                      // 旧服务端响应未带 total 时兼容其内嵌数组；新响应只传计数，
                      // 大列表保持折叠直到用户明确展开，避免轮询反复生成大 DOM。
                      const hasPagedTotal = typeof r.inspiration_total === 'number'
                      const total = r.inspiration_total ?? (r.inspirations || []).length
                      const embedded = r.inspirations || []
                      const page = inspirationPages[r.repo_id]
                      // 新接口不携带条目，所有非空仓库都按需展开；旧响应保留
                      // 原有直接展示行为，确保渐进部署期间前后端互相兼容。
                      const isExpanded = !hasPagedTotal || total === 0 || !!expandedInspirationRepoIds[r.repo_id]
                      const items = page?.inspirations || embedded
                      const loading = !!inspirationPageLoading[r.repo_id]
                      return <>
                    <div className="inspiration-repo-head">
                      <span className="inspiration-repo-name" title={tr('overview.repoTitle')}><Icon name="folder" /> {r.repo_name || tr('common.deleted')}</span>
                      {r.enabled === false && (
                        <span className="badge badge-muted" title={tr('overview.repoDisabledTitle')}>{tr('common.disabled')}</span>
                      )}
                      <span className="muted">{tr('overview.inspirationCount', { n: total })}</span>
                      {total > 0 && hasPagedTotal && (
                        <button type="button" className="inspiration-toggle-btn"
                                aria-expanded={isExpanded}
                                onClick={() => toggleInspirationRepo(r)}>
                          <Icon name={isExpanded ? 'chevronDown' : 'chevronRight'} />
                          {isExpanded ? tr('overview.collapseInspirations') : tr('overview.expandInspirations')}
                        </button>
                      )}
                    </div>
                    {!isExpanded ? null : total === 0 ? (
                      <div className="empty-state small">
                        <span className="empty-icon" aria-hidden="true"><Icon name="lightbulb" /></span>
                        <p className="muted">{tr('overview.noInspirationPlaceholder')}</p>
                      </div>
                    ) : loading && items.length === 0 ? (
                      <p className="muted small">{tr('common.loading')}</p>
                    ) : (
                      <ul className="inspiration-list">
                        {items.map((ins) => (
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
                    {isExpanded && page?.has_more && (
                      <button type="button" className="btn btn-small inspiration-load-more-btn"
                              disabled={loading} onClick={() => loadMoreInspirations(r)}>
                        {loading ? tr('common.loading') : tr('overview.loadMoreInspirations')}
                      </button>
                    )}
                    </>
                    })()}
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
               ref={chatDrawerRef}
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
              <textarea className="input chat-input" rows={1}
                        placeholder={tr('overview.chatPlaceholder')}
                        value={chatDraft}
                        onChange={(e) => setChatDraft(e.target.value)}
                        onInput={autoGrowChatInput}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && !e.shiftKey && !chatSending) {
                            e.preventDefault()
                            sendInspirationChat()
                          }
                        }}
                        disabled={chatSending} />
              <button type="submit" className="btn chat-send-btn"
                      aria-label={tr('overview.send')}
                      title={tr('overview.send')}
                      disabled={chatSending || !chatDraft.trim()}>
                {chatSending ? <Icon name="hourglass" /> : <Icon name="arrowUp" />}
              </button>
            </form>
            <ScrollContainerBackToTop containerRef={chatDrawerRef} />
          </div>
        </div>
      )}
    </>
  )
}
