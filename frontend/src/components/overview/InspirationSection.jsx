// 灵感板块 + AI 对话抽屉（issue #201 拆分）：从 Overview.jsx 抽出的
// 灵感记录子组件与右侧对话面板，数据由 useOverviewData hook 注入。
import { useRef } from 'react'
import { useI18n } from '../../i18n.jsx'
import { Icon } from '../Icon.jsx'
import { ScrollContainerBackToTop } from '../BackToTop.jsx'
import ResizableDrawer, { CHAT_DRAWER_WIDTH_KEY } from '../ResizableDrawer.jsx'  // issue #466：聊天抽屉拖拽调整宽度
import { fmtAgo } from '../../api.js'

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
  // issue #246：标签筛选 / 归档开关 / 归档操作 / 转 issue 保留确认
  inspirationLabelFilter, changeInspirationLabelFilter,
  inspirationShowArchived, toggleInspirationShowArchived,
  archiveInspiration, unarchiveInspiration,
  inspirationKeepDraft, closeInspirationKeepModal,
  setInspirationKeepValue, confirmAddIssueWithKeep,
  editInspirationLabel, setEditInspirationLabel,
  chatInspiration, closeInspirationChat, chatLoading, chatMessages,
  chatProviders, chatProvider, changeChatProvider,
  chatSending, chatDraft, setChatDraft, chatError, setChatError,
  sendInspirationChat,
}) {
  // issue #457：AI 对话抽屉滚动容器 ref——右下角「回到顶部」按钮定位/监听于此
  const chatDrawerRef = useRef(null)
  const { tr } = useI18n()
  // issue #246：标签筛选候选 = 已加载灵感条目的标签去重排序（含分页缓存），
  // 标签输入 datalist 复用同一候选（既有标签快速选择，也允许自由输入）
  const labelOptions = Array.from(new Set(
    inspirationRepos.flatMap((r) => (r.inspirations || [])
      .concat(inspirationPages[r.repo_id]?.inspirations || [])
      .map((ins) => ins.label)
      .filter(Boolean))
  )).sort()
  // 归档总数（概览响应 archived_total 汇总）：未归档视图在筛选栏提示
  // 「归档 N 条」入口，引导用户打开归档开关查看
  const archivedCount = inspirationRepos.reduce(
    (sum, r) => sum + (typeof r.archived_total === 'number' ? r.archived_total : 0), 0)
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
            <div className="inspiration-filter-bar">
              <label className="inspiration-label-filter-wrap"
                     title={tr('overview.inspirationLabelFilterTitle')}>
                <Icon name="tag" />
                <select className="input inspiration-label-filter"
                        value={inspirationLabelFilter}
                        onChange={(e) => changeInspirationLabelFilter(e.target.value)}
                        aria-label={tr('overview.inspirationLabelFilter')}>
                  <option value="">{tr('overview.inspirationLabelAll')}</option>
                  {labelOptions.map((name) => (
                    <option key={name} value={name}>{name}</option>
                  ))}
                </select>
              </label>
              <label className="inspiration-archive-toggle"
                     title={tr('overview.inspirationShowArchivedTitle')}>
                <input type="checkbox" className="inspiration-archive-checkbox"
                       checked={inspirationShowArchived}
                       onChange={(e) => toggleInspirationShowArchived(e.target.checked)} />
                <Icon name={inspirationShowArchived ? 'archive' : 'archiveRestore'} />
                {tr('overview.inspirationShowArchived')}
                {!inspirationShowArchived && archivedCount > 0 && (
                  <span className="muted small inspiration-archived-count"
                        title={tr('overview.archivedCountTitle')}>
                    {tr('overview.archivedCount', { n: archivedCount })}
                  </span>
                )}
              </label>
            </div>
            <p className="muted">{tr('overview.inspirationsDesc')}</p>
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
                          <li key={ins.id}
                              className={'inspiration-item'
                                + (ins.archived ? ' inspiration-item-archived' : '')}>
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
                                {/* issue #246：单标签输入——datalist 提供已有
                                    标签候选，也允许自由输入；空串=无标签 */}
                                <input className="input inspiration-label-input"
                                       list="inspiration-label-options"
                                       value={editInspirationLabel}
                                       onChange={(e) => setEditInspirationLabel(e.target.value)}
                                       placeholder={tr('overview.inspirationLabelPlaceholder')} />
                                <datalist id="inspiration-label-options">
                                  {labelOptions.map((name) => (
                                    <option key={name} value={name} />
                                  ))}
                                </datalist>
                                <div className="inspiration-actions">
                                  <button type="button" className="btn btn-small inspiration-save-btn"
                                          onClick={() => saveInspiration(ins)}
                                          disabled={!editInspirationDraft.trim()}>{tr('common.save')}</button>
                                  <button type="button" className="btn btn-small"
                                          onClick={() => {
                                            setEditingInspiration(null)
                                            setEditInspirationDraft('')
                                            setEditInspirationLabel('')
                                          }}>{tr('common.cancel')}</button>
                                </div>
                              </div>
                            ) : (
                              <>
                                <div className="inspiration-content-row">
                                  <p className="inspiration-content">{ins.content}</p>
                                  {ins.archived && (
                                    <span className="inspiration-archived-badge"
                                          title={tr('overview.inspirationArchivedBadgeTitle')}>
                                      <Icon name="archive" /> {tr('overview.inspirationArchivedBadge')}
                                    </span>
                                  )}
                                </div>
                                {ins.label && (
                                  <span className="inspiration-label-badge"
                                        title={tr('overview.inspirationLabelBadgeTitle')}>
                                    <Icon name="tag" /> {ins.label}
                                  </span>
                                )}
                                {/* issue #246：转 issue 时选择「保留灵感并关联」
                                    后展示的 issue 关联链接 */}
                                {ins.linked_issue_url && ins.linked_issue_iid != null && (
                                  <a className="inspiration-linked-issue"
                                     href={ins.linked_issue_url}
                                     target="_blank" rel="noreferrer"
                                     title={tr('overview.inspirationLinkedIssueTitle')}>
                                    <Icon name="externalLink" />
                                    {tr('overview.inspirationLinkedIssue', { n: ins.linked_issue_iid })}
                                  </a>
                                )}
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
                                    {/* issue #246：归档/取消归档按钮——归档视图
                                        显示「取消归档」，正常视图显示「归档」 */}
                                    {ins.archived ? (
                                      <button type="button"
                                              className="inspiration-action-btn inspiration-unarchive-btn"
                                              title={tr('overview.inspirationUnarchiveTitle')}
                                              onClick={() => unarchiveInspiration(ins)}>
                                        <Icon name="archiveRestore" /> {tr('overview.inspirationUnarchive')}
                                      </button>
                                    ) : (
                                      <button type="button"
                                              className="inspiration-action-btn inspiration-archive-btn"
                                              title={tr('overview.inspirationArchiveTitle')}
                                              onClick={() => archiveInspiration(ins)}>
                                        <Icon name="archive" /> {tr('overview.inspirationArchive')}
                                      </button>
                                    )}
                                    <button type="button" className="inspiration-action-btn"
                                            title={tr('overview.editInspirationTitle')}
                                            onClick={() => {
                                              setEditingInspiration(ins)
                                              setEditInspirationDraft(ins.content)
                                              setEditInspirationLabel(ins.label || '')
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
                    {/* 随手记录表单：内容去首尾空白非空才允许提交；归档
                        视图只查看历史，不提供新增入口（issue #246） */}
                    {inspirationShowArchived ? null : (
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
                    )}
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
                  {/* issue #246：批量条目可选「保留灵感并关联」——勾选后转
                      issue 成功不删除灵感（与单条保留语义一致） */}
                  <label className="inspiration-batch-field inspiration-batch-keep">
                    <span className="muted small" />
                    <span className="inspiration-batch-keep-label">
                      <input type="checkbox" className="inspiration-batch-keep-checkbox"
                             checked={!!draft.keep_inspiration}
                             onChange={(e) => updateBatchDraft(index, { keep_inspiration: e.target.checked })}
                             disabled={batchSubmitting} />
                      {tr('overview.inspirationBatchKeepLabel')}
                    </span>
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

      {/* issue #246：转 issue 保留确认弹窗——点击「添加 Issue」后弹出，
          勾选「保留灵感并关联」则创建成功后保留灵感并展示关联链接，
          不勾选保持旧行为（创建成功后删除灵感，issue #162） */}
      {inspirationKeepDraft && (
        <div className="modal-overlay" onClick={closeInspirationKeepModal}>
          <div className="modal inspiration-keep-modal" role="dialog"
               aria-modal="true" aria-label={tr('overview.inspirationKeepTitle')}
               onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <strong><Icon name="pin" /> {tr('overview.inspirationKeepTitle')}</strong>
              <button type="button" className="btn modal-close"
                      onClick={closeInspirationKeepModal} title={tr('common.close')}
                      aria-label={tr('common.close')}><Icon name="x" /></button>
            </div>
            <div className="inspiration-keep-body">
              <p className="inspiration-keep-content"
                 title={inspirationKeepDraft.insp.content}>
                {inspirationKeepDraft.insp.content}
              </p>
              <label className="inspiration-keep-checkbox-label"
                     title={tr('overview.inspirationKeepLabelTitle')}>
                <input type="checkbox" className="inspiration-keep-checkbox"
                       checked={!!inspirationKeepDraft.keep}
                       onChange={(e) => setInspirationKeepValue(e.target.checked)} />
                <Icon name="archiveRestore" /> {tr('overview.inspirationKeepLabel')}
              </label>
            </div>
            <div className="inspiration-keep-footer">
              <button type="button" className="btn btn-small inspiration-keep-cancel-btn"
                      onClick={closeInspirationKeepModal}>
                {tr('common.cancel')}
              </button>
              <button type="button"
                      className="btn btn-small btn-primary inspiration-keep-confirm-btn"
                      onClick={confirmAddIssueWithKeep}
                      disabled={!!addingIssueInspIds[inspirationKeepDraft.insp.id]}>
                {addingIssueInspIds[inspirationKeepDraft.insp.id]
                  ? <><Icon name="hourglass" /> {tr('overview.submitting')}</>
                  : <><Icon name="pin" /> {tr('overview.inspirationKeepConfirm')}</>}
              </button>
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
