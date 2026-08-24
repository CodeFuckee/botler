// 灵感板块 + AI 对话抽屉（issue #201 拆分）：从 Overview.jsx 抽出的
// 灵感记录子组件与右侧对话面板，数据由 useOverviewData hook 注入。
import { useRef } from 'react'
import { useI18n } from '../../i18n.jsx'
import { Icon } from '../Icon.jsx'
import { ScrollContainerBackToTop } from '../BackToTop.jsx'
import ResizableDrawer, { CHAT_DRAWER_WIDTH_KEY } from '../ResizableDrawer.jsx'  // issue #466：聊天抽屉拖拽调整宽度
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
  // issue #247：批量转 issue——多选模式/已选列表/预览面板状态与操作
  inspirationSelectionMode, selectedInspirationIds,
  enterInspirationSelectionMode, exitInspirationSelectionMode,
  toggleInspirationSelected, openBatchConvert,
  batchConvertOpen, batchDrafts, batchSubmitting, batchResult, batchError, setBatchError,
  updateBatchDraft, resetBatchDraftToDefault, applyBatchDefaults,
  submitBatchConvert, closeBatchConvert,
  chatInspiration, closeInspirationChat, chatLoading, chatMessages,
  chatProviders, chatProvider, changeChatProvider,
  chatSending, chatDraft, setChatDraft, chatError, setChatError,
  sendInspirationChat,
}) {
  // issue #457：AI 对话抽屉滚动容器 ref——右下角「回到顶部」按钮定位/监听于此
  const chatDrawerRef = useRef(null)
  const { tr } = useI18n()
  return (
    <>
          <section className="inspirations-section">
            <div className="inspiration-section-head">
              <h2><Icon name="lightbulb" /> {tr('overview.inspirationsTitle')}</h2>
              <button type="button" className="btn btn-small inspiration-select-mode-btn"
                      onClick={inspirationSelectionMode ? exitInspirationSelectionMode : enterInspirationSelectionMode}
                      aria-pressed={inspirationSelectionMode}>
                {inspirationSelectionMode
                  ? <><Icon name="checkSquare" /> {tr('overview.inspirationSelectModeExit')}</>
                  : <><Icon name="checkSquare" /> {tr('overview.inspirationSelectMode')}</>}
              </button>
            </div>
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
            {inspirationSelectionMode && (
              <div className="inspiration-batch-toolbar" role="toolbar"
                   aria-label={tr('overview.inspirationBatchToolbar')}>
                <span className="muted small">
                  {tr('overview.selectedInspirationCount', { n: selectedInspirationIds.length })}
                </span>
                <button type="button" className="btn btn-small inspiration-batch-convert-btn"
                        onClick={openBatchConvert}
                        disabled={selectedInspirationIds.length === 0}
                        title={tr('overview.inspirationBatchConvertTitle')}>
                  <Icon name="pin" /> {tr('overview.inspirationBatchConvert')}
                </button>
                <button type="button" className="btn btn-small inspiration-batch-cancel-btn"
                        onClick={exitInspirationSelectionMode}>
                  {tr('common.cancel')}
                </button>
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
                            {inspirationSelectionMode && (
                              <label className="inspiration-select-label"
                                     title={tr('overview.inspirationSelectItem')}>
                                <input type="checkbox"
                                       className="inspiration-select-checkbox"
                                       checked={selectedInspirationIds.includes(ins.id)}
                                       onChange={() => toggleInspirationSelected(ins.id)} />
                              </label>
                            )}
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

      {/* issue #247：批量转 issue 预览面板——多选灵感后弹出，每条可
          单独编辑标题/描述/标签/目标仓库（或「全部应用默认」统一重置），
          逐条提交后展示「N 成功 / M 失败」汇总与逐条失败原因；遮罩点击
          / × / Esc 关闭 */}
      {batchConvertOpen && (
        <div className="modal-overlay" onClick={closeBatchConvert}>
          <div className="modal inspiration-batch-modal" role="dialog"
               aria-modal="true" aria-label={tr('overview.inspirationBatchTitle', { n: batchDrafts.length })}
               onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <strong><Icon name="pin" /> {tr('overview.inspirationBatchTitle', { n: batchDrafts.length })}</strong>
              <button type="button" className="btn modal-close"
                      onClick={closeBatchConvert} title={tr('common.close')}
                      aria-label={tr('common.close')}><Icon name="x" /></button>
            </div>
            <div className="inspiration-batch-body">
              {batchDrafts.length === 0 ? (
                <p className="muted small">{tr('overview.inspirationBatchEmpty')}</p>
              ) : batchDrafts.map((draft, index) => (
                <div key={draft.inspiration.id} className="inspiration-batch-item"
                     data-inspiration-id={draft.inspiration.id}>
                  <div className="inspiration-batch-item-head">
                    <span className="muted small">
                      {tr('overview.inspirationBatchRepo', { name: draft.inspiration.repo_name || tr('common.deleted') })}
                    </span>
                    <button type="button" className="btn btn-small inspiration-batch-reset-btn"
                            onClick={() => resetBatchDraftToDefault(index)}
                            disabled={batchSubmitting}>
                      {tr('overview.inspirationBatchReset')}
                    </button>
                  </div>
                  <label className="inspiration-batch-field">
                    <span className="muted small">{tr('overview.inspirationBatchTitleLabel')}</span>
                    <input className="input inspiration-batch-title" type="text"
                           value={draft.title}
                           onChange={(e) => updateBatchDraft(index, { title: e.target.value })}
                           disabled={batchSubmitting} />
                  </label>
                  <label className="inspiration-batch-field">
                    <span className="muted small">{tr('overview.inspirationBatchDescLabel')}</span>
                    <textarea className="input inspiration-batch-desc" rows={2}
                              value={draft.description}
                              onChange={(e) => updateBatchDraft(index, { description: e.target.value })}
                              disabled={batchSubmitting} />
                  </label>
                  <label className="inspiration-batch-field">
                    <span className="muted small">{tr('overview.inspirationBatchLabelsLabel')}</span>
                    <input className="input inspiration-batch-labels" type="text"
                           value={draft.labels}
                           onChange={(e) => updateBatchDraft(index, { labels: e.target.value })}
                           disabled={batchSubmitting} />
                  </label>
                  <label className="inspiration-batch-field">
                    <span className="muted small">{tr('overview.inspirationBatchRepoLabel')}</span>
                    <select className="input inspiration-batch-repo"
                            value={draft.repo_id}
                            onChange={(e) => updateBatchDraft(index, { repo_id: Number(e.target.value) })}
                            disabled={batchSubmitting}>
                      {inspirationRepos.map((r) => (
                        <option key={r.repo_id} value={r.repo_id}>
                          {r.repo_name || tr('common.deleted')}{r.enabled === false ? `（${tr('common.disabled')}）` : ''}
                        </option>
                      ))}
                    </select>
                  </label>
                  {batchResult && (batchResult.failed || []).some((f) => f.inspiration_id === draft.inspiration.id) && (
                    <div className="inspiration-batch-item-error" role="alert">
                      <Icon name="warning" />{' '}
                      {tr('overview.inspirationBatchFailure', {
                        msg: (batchResult.failed || []).find((f) => f.inspiration_id === draft.inspiration.id).error,
                      })}
                    </div>
                  )}
                </div>
              ))}
            </div>
            {batchError && (
              <div className="alert alert-error inspiration-batch-error" role="alert"
                   onClick={() => setBatchError('')}>{batchError}</div>
            )}
            {batchResult && (
              <div className={'inspiration-batch-result'
                   + (batchResult.failed.length ? ' inspiration-batch-result-error' : '')}
                   role="status">
                <div>
                  {tr('overview.inspirationBatchSummary', {
                    n: batchResult.succeeded.length, m: batchResult.failed.length,
                  })}
                </div>
                {batchResult.failed.length > 0 && (
                  <div className="inspiration-batch-result-failures">
                    {batchResult.failed.map((f) => (
                      <div key={f.inspiration_id}>
                        {tr('overview.inspirationBatchFailureDetail', { id: f.inspiration_id, msg: f.error })}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
            <div className="inspiration-batch-footer">
              <button type="button" className="btn btn-small inspiration-batch-apply-defaults-btn"
                      onClick={applyBatchDefaults} disabled={batchSubmitting || batchDrafts.length === 0}>
                {tr('overview.inspirationBatchApplyDefaults')}
              </button>
              <div className="inspiration-batch-actions">
                <button type="button" className="btn btn-small inspiration-batch-close-btn"
                        onClick={closeBatchConvert} disabled={batchSubmitting}>
                  {tr('common.cancel')}
                </button>
                <button type="button" className="btn btn-small btn-primary inspiration-batch-submit-btn"
                        onClick={submitBatchConvert}
                        disabled={batchSubmitting || batchDrafts.length === 0 || !!batchResult}
                        title={batchResult ? tr('overview.inspirationBatchSubmitted') : ''}>
                  {batchSubmitting
                    ? <><Icon name="hourglass" /> {tr('overview.inspirationBatchSubmitting')}</>
                    : <><Icon name="pin" /> {tr('overview.inspirationBatchSubmit')}</>}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* issue #166/#184：灵感 AI 对话右侧抽屉——与 AI agent 探讨当前
          灵感（遮罩点击 / × / Esc 关闭，从右侧滑入） */}
      {chatInspiration && (
        <div className="drawer-overlay" onClick={closeInspirationChat}>
          <ResizableDrawer drawerClass="chat-drawer" dialog
                           storageKey={CHAT_DRAWER_WIDTH_KEY}
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
            <div className="chat-provider-bar">
              <label htmlFor="chat-provider-select">{tr('overview.chatProvider')}</label>
              {chatProviders.length > 0 ? (
                <select id="chat-provider-select" className="chat-provider-select"
                        value={chatProvider || ''}
                        onChange={(e) => changeChatProvider(e.target.value)}
                        disabled={chatLoading || chatSending}>
                  {chatProviders.map((provider) => (
                    <option key={provider.provider} value={provider.provider}>
                      {provider.provider} / {provider.model || '默认模型'} · {provider.name}
                    </option>
                  ))}
                </select>
              ) : (
                <span className="chat-provider-empty">
                  {tr('overview.chatNoProviders')}{' '}
                  <a href="/settings#settings-ai-providers">{tr('overview.chatGoSettings')}</a>
                </span>
              )}
            </div>
            {/* issue #457：chat-drawer 自身 overflow: hidden 不滚动，
                实际滚动容器是 chat-body——回到顶部按钮的 ref 必须指向
                chat-body（scrollTop/scrollHeight 才有效） */}
            <div className="chat-body" ref={chatDrawerRef}>
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
              <ScrollContainerBackToTop containerRef={chatDrawerRef} />
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
          </ResizableDrawer>
        </div>
      )}
    </>
  )
}
