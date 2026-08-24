// 开放 Issue 板块（issue #201 拆分）：从 Overview.jsx 抽出的概览页
// 第一大板块——过滤条、仓库卡片、分组折叠、issue 列表项，以及对账 /
// 自省 / 发掘结果组件；数据与处理器由 useOverviewData hook 注入
// （组件只接数据）。
import { useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n.jsx'
import { Icon } from '../Icon.jsx'
import { api, STATUS_META, fmtAgo } from '../../api.js'
import { confirmDialog } from '../../dialog.js'
import { closeIssuesInBatch } from '../../lib/batchCloseIssues.js'
import {
  ISSUE_GROUPS,
  BOT_STATUS_META,
  BOT_STATUS_NAMES,
  ISSUE_SORTS,
  ISSUE_STATUS_FILTERS,
  ISSUE_LAYOUTS,
  botStatusKey,
  groupIssuesByBotLabel,
  applyManualOrder,
  toggleGroupCollapsed,
  engineLabel,
  tasksForIssue,
} from '../../lib/overview.jsx'

function issueKey(issue) {
  if (!issue || !Number.isInteger(issue.project_id) || issue.project_id <= 0
      || !Number.isInteger(issue.iid) || issue.iid <= 0) return ''
  return `${issue.project_id}:${issue.iid}`
}

export default function IssueListSection({
  hasAnyIssue,
  issueFilter, setIssueFilter,
  issueSort, setIssueSort, issueFilterActive,
  issueLabelOptions,
  ownerTokenOk,
  issueError, setIssueError,
  error, setError,
  issueErrors,
  filteredRepoIssues,
  introspectRepo, introspectResults,
  discoverRepo, discoverResults,
  reconcileRepo, reconcileResults,
  setAddIssueRepo, addIssueRepo,
  manualErrors, setManualErrors,
  collapsedGroups, setCollapsedGroups,
  issueLayout, setIssueLayout,
  collapsedRepos, setCollapsedRepos,
  manualOrders, manualSaving,
  dragFrom, setDragFrom, dragOverIndex, setDragOverIndex,
  commitManualReorder,
  runningKeys, activeKeys, pinIssue,
  tasks, liveLines, setSelectedIssue, loadIssues,
}) {
  const { tr } = useI18n()
  const [selectedIssueKeys, setSelectedIssueKeys] = useState(() => new Set())
  const [batchMode, setBatchMode] = useState(false)
  const [batchClosing, setBatchClosing] = useState(false)
  const [batchError, setBatchError] = useState('')
  const [batchResult, setBatchResult] = useState(null)

  // 当前列表是批量操作范围：过滤/排序后的所有仓库卡片内的开放 issue。
  const visibleIssues = useMemo(() => filteredRepoIssues.flatMap((repo) => {
    const projectId = repo.project_id ?? (repo.issues[0] && repo.issues[0].project_id)
    return (repo.issues || []).map((issue) => ({
      ...issue,
      project_id: issue.project_id ?? projectId,
      repo_name: repo.repo_name,
    }))
  }), [filteredRepoIssues])
  const visibleIssueKeys = useMemo(
    () => new Set(visibleIssues.map((issue) => issueKey(issue)).filter(Boolean)),
    [visibleIssues])
  const selectedIssues = visibleIssues.filter((issue) => selectedIssueKeys.has(issueKey(issue)))
  const allVisibleSelected = visibleIssues.length > 0
    && selectedIssues.length === visibleIssues.length

  // 轮询/刷新后移除已经不在当前列表中的选择，避免统计与实际操作对象不一致。
  useEffect(() => {
    setSelectedIssueKeys((prev) => {
      const next = new Set([...prev].filter((key) => visibleIssueKeys.has(key)))
      return next.size === prev.size ? prev : next
    })
  }, [visibleIssueKeys])

  function toggleIssue(issue, checked) {
    const key = issueKey(issue)
    if (!key) return
    setSelectedIssueKeys((prev) => {
      const next = new Set(prev)
      if (checked) next.add(key)
      else next.delete(key)
      return next
    })
    setBatchError('')
  }

  function toggleAll(checked) {
    setSelectedIssueKeys(checked ? new Set(visibleIssueKeys) : new Set())
    setBatchError('')
  }

  function enterBatchMode() {
    setBatchMode(true)
    setBatchError('')
    setBatchResult(null)
  }

  function exitBatchMode() {
    if (batchClosing) return
    setBatchMode(false)
    setSelectedIssueKeys(new Set())
    setBatchError('')
    setBatchResult(null)
  }

  async function handleBatchClose() {
    if (batchClosing) return
    if (selectedIssues.length === 0) {
      setBatchError(tr('overview.batchCloseEmpty'))
      setBatchResult(null)
      return
    }
    const confirmed = await confirmDialog({
      message: tr('overview.batchCloseConfirm', { n: selectedIssues.length }),
      danger: true,
    })
    if (!confirmed || batchClosing) return
    setBatchClosing(true)
    setBatchError('')
    setBatchResult(null)
    try {
      const result = await closeIssuesInBatch(selectedIssues, (issue) =>
        api.post(`/api/issues/${issue.project_id}/${issue.iid}/close`))
      setBatchResult(result)
      setSelectedIssueKeys(new Set(result.failed.map(({ issue }) => issueKey(issue)).filter(Boolean)))
      if (result.succeeded.length > 0) await loadIssues()
    } catch (e) {
      // 纯函数已归集单条失败，这里仅兜底处理未知错误，避免按钮永久锁定。
      setBatchError(e.message || tr('overview.batchCloseFailed'))
    } finally {
      setBatchClosing(false)
    }
  }

  // issue #471：issue 列表项渲染（卡片布局与单列分组布局共用）——标题/
  // 状态徽章/运行中高亮/标签/里程碑/assignee/更新时间/评论数/置顶按钮/
  // 任务信息块/批量勾选/拖拽（仅卡片布局按规则启用）。groupKey 为 bot
  // 状态分组 key（单列分组平铺时传 'other'，置顶按钮对所有 issue 可用）；
  // dragEnabled 由调用方决定（卡片布局按排序/过滤/分组计算，单列布局
  // 固定关闭）；ordered 为分组当前渲染顺序（拖拽落点提交用）。
  function renderIssueItem(r, i, groupKey, idx,
                           { dragEnabled = false, ordered = [] } = {}) {
    const bot = botStatusKey(i)
    const statusMeta = bot ? BOT_STATUS_META[bot] : null
    // issue #99：任务（running/retrying）命中则该 issue 高亮
    const running = runningKeys.has(`${r.repo_id}:${i.iid}`)
    const active = activeKeys.has(`${r.repo_id}:${i.iid}`)
    // issue #80：终态标签由状态徽章替代展示，其余标签保留胶囊
    const otherLabels = (i.labels || []).filter(
      (l) => l && !BOT_STATUS_NAMES.has(l.name))
    // issue #308：置顶状态——手动调度顺序首位即已置顶
    // （仅「其他」分组展示置顶按钮）
    const manualIids = manualOrders[r.repo_id] || []
    const pinned = groupKey === 'other' && manualIids[0] === i.iid
    const repoProjectId = r.project_id != null
      ? r.project_id
      : (r.issues[0] && r.issues[0].project_id)
    // issue #287：拖拽状态类——拖起项半透明、悬停目标高亮落点
    const isDragging = dragEnabled && dragFrom && dragFrom.repoId === r.repo_id
      && dragFrom.from === idx
    const isDragOver = dragEnabled && dragOverIndex === idx
    const itemCls = (running
      ? 'issue-item issue-item-running'
      : 'issue-item')
      + (isDragging ? ' issue-item-dragging' : '')
      + (isDragOver ? ' issue-item-drag-over' : '')
    return (
      <li key={i.iid}
          draggable={dragEnabled}
          onDragStart={dragEnabled ? (e) => {
            // HTML5 拖放：标记移动语义 + 记录拖起
            // 位置（按当前渲染索引），拖拽结束后清除
            e.dataTransfer.effectAllowed = 'move'
            e.dataTransfer.setData('text/plain', String(i.iid))
            setDragFrom({ repoId: r.repo_id, from: idx })
          } : undefined}
          onDragOver={dragEnabled ? (e) => {
            // 必须 preventDefault 才允许落点（drop）
            e.preventDefault()
            e.dataTransfer.dropEffect = 'move'
            if (dragOverIndex !== idx) setDragOverIndex(idx)
          } : undefined}
          onDrop={dragEnabled ? (e) => {
            e.preventDefault()
            commitManualReorder(r, ordered, idx)
          } : undefined}
          onDragEnd={dragEnabled ? () => {
            setDragFrom(null)
            setDragOverIndex(null)
          } : undefined}
          className={itemCls}>
        {/* issue #71：参考 GitLab issue 列表页布局——左列编号+标题+
            标签/里程碑胶囊，右列 assignee 头像+更新时间+评论数
            issue #85：标题改为按钮——点击打开右边栏，不再直接
            跳转 GitLab（跳转统一走右边栏右上角按钮）
            issue #114：issue 行（issue-row）与任务信息块
            纵向排布——任务板块删除后任务详情随项展示 */}
        <div className="issue-row">
        {!addIssueRepo && batchMode && (
          <input type="checkbox" className="issue-select-checkbox"
                 checked={selectedIssueKeys.has(issueKey({ ...i, project_id: i.project_id ?? repoProjectId }))}
                 onChange={(e) => toggleIssue({ ...i, project_id: i.project_id ?? repoProjectId }, e.target.checked)}
                 disabled={batchClosing}
                 aria-label={tr('overview.selectIssue', { iid: i.iid })} />
        )}
        {/* issue #287：「其他」分组拖动排序手柄——
            装饰性图标（gripVertical），li 整体可拖，
            图标只是视觉提示与抓取点 */}
        {dragEnabled && (
          <span className="issue-drag-handle"
                title={tr('overview.manualOrderTitle')}
                aria-hidden="true">
            <Icon name="gripVertical" />
          </span>
        )}
        <div className="issue-main">
          <button type="button" className="issue-link"
                  onClick={() => setSelectedIssue({
                    issue: i, repoName: r.repo_name,
                    running, active,
                  })}
                  title={tr('overview.viewIssueDetail')}>
            <span className="issue-iid">#{i.iid}</span>
            {statusMeta && (
              <span className={`issue-status ${statusMeta.cls}`}
                    title={tr(`overview.botStatusHint.${bot}`)}><Icon name={statusMeta.icon} /> {statusMeta.label}</span>
            )}
            {/* issue #99：正在运行的 issue 显示「运行中」徽章
                （任务结束后随任务列表轮询自动消失） */}
            {running && (
              <span className="issue-status issue-status-running"
                    title={tr('overview.runningBadgeTitle')}><Icon name="settings" /> {tr('overview.runningBadge')}</span>
            )}
            {i.title || '—'}
          </button>
          {(otherLabels.length > 0 || i.milestone) && (
            <div className="issue-meta">
              {otherLabels.map((l) => (
                <span key={l.name} className="label-pill"
                      style={l.color
                        ? { background: `#${l.color}`, color: `#${l.text_color}` }
                        : undefined}
                      title={tr('overview.labelPillTitle', { name: l.name })}>{l.name}</span>
              ))}
              {i.milestone && (
                <span className="milestone-chip" title={tr('overview.milestoneTitle', { name: i.milestone })}>
                  <Icon name="tag" /> {i.milestone}
                </span>
              )}
            </div>
          )}
        </div>
        <div className="issue-side">
          {(i.assignees || []).map((a) => (
            a.avatar_url ? (
              <img key={a.username || a.name}
                   className="assignee-avatar" src={a.avatar_url}
                   alt={a.name || a.username || ''}
                   title={tr('overview.assigneeTitle', { name: a.name || a.username || '' })} />
            ) : (
              <span key={a.username || a.name}
                    className="assignee-avatar avatar-fallback"
                    title={tr('overview.assigneeTitle', { name: a.name || a.username || '' })}>
                {(a.name || a.username || '?').slice(0, 1).toUpperCase()}
              </span>
            )
          ))}
          {i.updated_at && (
            <span className="issue-updated" title={tr('overview.lastUpdated')}>
              {fmtAgo(i.updated_at) || ''}
            </span>
          )}
          {typeof i.user_notes_count === 'number' && (
            <span className="issue-notes-count" title={tr('overview.notesCount')}>
              <Icon name="message" /> {i.user_notes_count}
            </span>
          )}
          {/* issue #308：置顶按钮——仅「其他」分组展示：点击
              把 issue 移到手动调度顺序最前并保存（调度器优先
              派发，置顶即第一个处理，复用 #287 手动顺序机制）。
              已置顶（手动顺序首位）时高亮主色 + aria-pressed；
              保存中隐藏避免并发覆盖；过滤/其他排序下仍可用
              （仅写手动顺序，不依赖可见子集） */}
          {groupKey === 'other' && repoProjectId != null
            && !manualSaving.has(r.repo_id) && (
            <button type="button"
                    className={'issue-pin' + (pinned ? ' issue-pin-active' : '')}
                    onClick={() => pinIssue(r, i)}
                    title={pinned ? tr('overview.pinIssuePinned') : tr('overview.pinIssueTitle')}
                    aria-label={pinned ? tr('overview.pinIssuePinned') : tr('overview.pinIssue')}
                    aria-pressed={pinned}>
              <Icon name="pin" />
            </button>
          )}
        </div>
        </div>
        {/* issue #114：正在运行任务的信息块——任务板块已删除，
            任务状态徽章 / 执行引擎 / 实时输出随对应 issue 项
            展示（同一 issue 的多条任务记录逐一渲染） */}
        {running && tasksForIssue(tasks, r.repo_id, i.iid).map((t) => {
          const meta = STATUS_META[t.status]
            || { label: t.status || '—', cls: '' }
          const lines = liveLines[t.id] || []
          const eng = engineLabel(t.engine)
          return (
            <div key={t.id} className="issue-task">
              <div className="issue-task-head">
                <span className={'badge ' + meta.cls}>{meta.label}</span>
                {eng && (
                  <span className="issue-task-engine"
                        title={tr('overview.taskEngineTitle')}>{eng}</span>
                )}
                {t.issue_url ? (
                  <a className="issue-task-link" href={t.issue_url}
                     target="_blank" rel="noreferrer"
                     title={tr('overview.openIssueInGitlab')}>{tr('overview.openInGitlab')}</a>
                ) : null}
              </div>
              <pre className="log-view issue-task-log">
                {lines.length > 0
                  ? lines.map((line, i) => <span key={i}>{line}{'\n'}</span>)
                  : tr('overview.noTaskOutput')}
              </pre>
            </div>
          )
        })}
      </li>
    )
  }

  // issue #471：仓库级操作按钮组（对账/自省/发掘/添加 Issue）——卡片
  // 布局与单列分组布局的仓库组头共用，整体推到组头最右侧
  function repoActionButtons(r) {
    return (
      <div className="issue-repo-actions">
        {/* issue #187：卡片右上角「自省」按钮——调用 AI
            agent 审查仓库功能与实现，把改进建议写入该仓库
            issue（分配人 = 仓库 owner）。请求中禁用防重复
            点击，与「对账」按钮同风格 */}
        <button type="button" className="btn btn-small introspect-btn"
                onClick={() => introspectRepo(r)}
                disabled={introspectResults[r.repo_id]?.loading}
                title={tr('overview.introspectTitle')}>
          {introspectResults[r.repo_id]?.loading ? <><Icon name="refresh" /> {tr('overview.introspecting')}</> : <><Icon name="search" /> {tr('overview.introspect')}</>}
        </button>
        {/* issue #189：卡片右上角「发掘」按钮——让 agent
            根据该仓库实现的功能去 GitHub 搜索类似仓库、翻找
            用户需求 issue，整理成需求写入该仓库 issue（分配人
            = 仓库 owner，一条需求一个 issue）。请求中禁用防
            重复点击，与「自省」按钮同风格 */}
        <button type="button" className="btn btn-small discover-btn"
                onClick={() => discoverRepo(r)}
                disabled={discoverResults[r.repo_id]?.loading}
                title={tr('overview.discoverTitle')}>
          {discoverResults[r.repo_id]?.loading ? <><Icon name="refresh" /> {tr('overview.discovering')}</> : <><Icon name="compass" /> {tr('overview.discover')}</>}
        </button>
        <button type="button" className="btn btn-small reconcile-btn"
                onClick={() => reconcileRepo(r)}
                disabled={reconcileResults[r.repo_id]?.loading}
                title={tr('overview.reconcileTitle')}>
          {reconcileResults[r.repo_id]?.loading ? <><Icon name="refresh" /> {tr('overview.reconciling')}</> : <><Icon name="refresh" /> {tr('overview.reconcile')}</>}
        </button>
        {/* issue #92：卡片右上角「添加 Issue」按钮——打开弹窗，
            提交后调用 GitLab API 在对应仓库创建 issue */}
        <button type="button" className="btn btn-small add-issue-btn"
                onClick={() => setAddIssueRepo(r)}
                title={tr('overview.addIssueTitle')}><Icon name="plus" /> {tr('overview.addIssue')}</button>
      </div>
    )
  }

  return (
          <section className="issues-section">
            <h2>{tr('overview.issuesTitle')}</h2>
            <p className="muted">{tr('overview.issuesDesc')}</p>

            {/* issue #230：过滤条——按状态（全部/开放/进行中）+ 标签多选
                过滤，仅过滤条目、保留仓库分组结构；偏好存 localStorage
                刷新后保持。状态「开放」= 无运行中任务（含 bot-failed /
                bot-done/其他分组），「进行中」= 有 running/retrying 任务
                （与置顶 running 组同源判定） */}
            {hasAnyIssue && (
              <div className="issue-filter-bar">
                {/* issue #471：布局切换——「仓库卡片」（默认，每个仓库
                    一张卡片，卡片内按 bot 状态分组）/「单列分组」（所有
                    仓库 issue 同一列展示，同仓库 issue 归为一个分组，分组
                    可折叠展开）；偏好存 localStorage 刷新后保持 */}
                <div className="issue-filter-row">
                  <span className="issue-filter-label" title={tr('overview.layoutTitle')}>{tr('overview.layout')}</span>
                  <div className="issue-filter-layouts" role="group" aria-label={tr('overview.layoutAria')}>
                    {ISSUE_LAYOUTS.map((l) => (
                      <button key={l.key} type="button"
                              className={'issue-layout-option' + (issueLayout === l.key ? ' active' : '')}
                              title={tr(`overview.layoutHint.${l.key}`)} aria-pressed={issueLayout === l.key}
                              onClick={() => setIssueLayout(l.key)}>
                        {/* issue #479：布局切换按钮图标——「仓库卡片」网格
                            图标、「单列分组」列表图标，与文字并排垂直居中 */}
                        <Icon name={l.key === 'cards' ? 'layoutGrid' : 'layoutList'} />
                        {tr(`overview.layoutBy.${l.key}`)}
                      </button>
                    ))}
                  </div>
                </div>
                {/* issue #286：排序方法切换——默认「调度器执行顺序」，与
                    任务调度器派发语义一致（仓库优先级 → issue 标签优先级
                    → 创建时间升序），方便预判各分组 issue 的处理顺序；可
                    切「最近更新」（原默认展示顺序）/「创建时间」；偏好存
                    localStorage 刷新后保持 */}
                <div className="issue-filter-row">
                  <span className="issue-filter-label" title={tr('overview.sortTitle')}>{tr('overview.sort')}</span>
                  <div className="issue-filter-sorts" role="group" aria-label={tr('overview.sortAria')}>
                    {ISSUE_SORTS.map((s) => (
                      <button key={s.key} type="button"
                              className={'issue-sort-option' + (issueSort === s.key ? ' active' : '')}
                              title={tr(`overview.sortHint.${s.key}`)} aria-pressed={issueSort === s.key}
                              onClick={() => setIssueSort(s.key)}>
                        {tr(`overview.sortBy.${s.key}`)}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="issue-filter-row">
                  <span className="issue-filter-label" title={tr('overview.filterByStatusTitle')}>{tr('overview.status')}</span>
                  <div className="issue-filter-statuses" role="group" aria-label={tr('overview.filterStatusAria')}>
                    {ISSUE_STATUS_FILTERS.map((s) => (
                      <button key={s.key} type="button"
                              className={'issue-filter-status' + (issueFilter.status === s.key ? ' active' : '')}
                              title={tr(`overview.filterStatusHint.${s.key}`)} aria-pressed={issueFilter.status === s.key}
                              onClick={() => setIssueFilter((prev) => ({ ...prev, status: s.key }))}>
                        {tr(`overview.filterStatus.${s.key}`)}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="issue-filter-row">
                  <span className="issue-filter-label" title={tr('overview.labelsTitle')}>{tr('overview.labels')}</span>
                  <div className="issue-filter-labels">
                    {issueLabelOptions.length === 0 ? (
                      <span className="muted small">{tr('overview.noLabels')}</span>
                    ) : (
                      issueLabelOptions.map((name) => {
                        const active = issueFilter.labels.includes(name)
                        return (
                          <button key={name} type="button"
                                  className={'issue-filter-label-chip' + (active ? ' active' : '')}
                                  title={active ? tr('overview.cancelFilter', { name }) : tr('overview.showOnlyWithLabel', { name })}
                                  aria-pressed={active}
                                  onClick={() => setIssueFilter((prev) => ({
                                    ...prev,
                                    labels: active
                                      ? prev.labels.filter((l) => l !== name)
                                      : prev.labels.concat(name),
                                  }))}>
                            {active && <Icon name="check" />}
                            {name}
                          </button>
                        )
                      })
                    )}
                  </div>
                </div>
                {issueFilterActive && (
                  <button type="button" className="btn btn-small issue-filter-reset"
                          onClick={() => setIssueFilter({ status: 'all', labels: [] })}
                          title={tr('overview.clearFilterTitle')}>
                    {tr('overview.clearFilter')}
                  </button>
                )}
              </div>
            )}
            {hasAnyIssue && !addIssueRepo && !batchMode && (
              <div className="issue-batch-entry">
                <button type="button" className="btn btn-small btn-danger batch-close-btn batch-close-enter-btn"
                        onClick={enterBatchMode}>
                  {tr('overview.batchClose')}
                </button>
              </div>
            )}
            {hasAnyIssue && !addIssueRepo && batchMode && (
              <div className="issue-batch-toolbar" role="toolbar" aria-label={tr('overview.batchCloseToolbar')}>
                <label className="issue-select-all-label">
                  <input type="checkbox" className="issue-select-all"
                         checked={allVisibleSelected}
                         ref={(input) => {
                           if (input) input.indeterminate = selectedIssues.length > 0 && !allVisibleSelected
                         }}
                         onChange={(e) => toggleAll(e.target.checked)}
                         disabled={batchClosing || visibleIssues.length === 0} />
                  {tr('overview.selectAllIssues')}
                </label>
                <span className="muted small">{tr('overview.selectedIssueCount', { n: selectedIssues.length })}</span>
                <button type="button" className="btn btn-small btn-danger batch-close-btn"
                        onClick={handleBatchClose} disabled={batchClosing}>
                  {batchClosing ? tr('overview.batchClosing') : tr('overview.batchClose')}
                </button>
                <button type="button" className="btn btn-small batch-close-cancel-btn"
                        onClick={exitBatchMode} disabled={batchClosing}>
                  {tr('common.cancel')}
                </button>
                {batchResult && (
                  <div className={batchResult.failed.length ? 'small batch-close-result batch-close-result-error' : 'small batch-close-result'}>
                    <div>
                      {tr('overview.batchCloseSuccess', { n: batchResult.succeeded.length })}
                      {batchResult.failed.length > 0 && `；${tr('overview.batchClosePartial', { n: batchResult.failed.length })}`}
                    </div>
                    {batchResult.failed.map(({ issue, error }) => (
                      <div key={issueKey(issue)}>
                        {tr('overview.batchCloseFailureDetail', { iid: issue.iid, msg: error.message })}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
            {batchError && (
              <div className="alert alert-error batch-close-error" role="alert" onClick={() => setBatchError('')}>
                {batchError}
              </div>
            )}
            {ownerTokenOk === false && (
              <div className="alert alert-warning" role="alert">
                <Icon name="warning" /> <strong>{tr('overview.ownerTokenWarning')}</strong>
                {tr('overview.ownerTokenBefore')}<code>gitlab.owner_token</code>{tr('overview.ownerTokenAfter')}
              </div>
            )}
            {issueError && (
              <div className="alert alert-error" onClick={() => setIssueError('')}>{issueError}</div>
            )}
            {error && (
              <div className="alert alert-error" onClick={() => setError('')}>{error}</div>
            )}
            {issueErrors.length > 0 && (
              <div className="alert alert-error">
                {issueErrors.map((e, i) => <div key={i}>{e}</div>)}
              </div>
            )}
            {!hasAnyIssue ? (
              <div className="empty-state">
                <span className="empty-icon" aria-hidden="true"><Icon name="clipboard" /></span>
                <p className="muted">{tr('overview.noOpenIssues')}</p>
              </div>
            ) : filteredRepoIssues.length === 0 ? (
              <div className="empty-state">
                <span className="empty-icon" aria-hidden="true"><Icon name="search" /></span>
                <p className="muted">{tr('overview.noMatchingIssues')}</p>
                <button type="button" className="btn btn-small"
                        onClick={() => setIssueFilter({ status: 'all', labels: [] })}>
                  清除过滤
                </button>
              </div>
            ) : issueLayout === 'column' ? (
                /* issue #471：单列分组布局——所有仓库 issue 在同一列展示，
                   同一个仓库的 issue 归为一个分组，分组头带折叠开关，折叠
                   后隐藏组内 issue 列表、保留组头（仓库名/优先级/计数/操作
                   按钮/结果提示）；折叠偏好存 localStorage 刷新后保持 */
                <div className="issues-list issues-list-column">
                  {filteredRepoIssues.map((r) => {
                    const repoId = r.repo_id
                    const collapsed = collapsedRepos.has(String(repoId))
                    const repoIssues = r.issues || []
                    return (
                      <div key={repoId} className={'card issue-repo-group' + (collapsed ? ' issue-repo-group-collapsed' : '')} data-repo-id={repoId}>
                        <div className="issue-repo-head issue-repo-group-head">
                          <button type="button"
                                  className="issue-repo-toggle"
                                  onClick={() => setCollapsedRepos((prev) => toggleGroupCollapsed(prev, String(repoId)))}
                                  aria-expanded={!collapsed}
                                  aria-label={tr(collapsed ? 'overview.expandRepo' : 'overview.collapseRepo')}
                                  title={tr(collapsed ? 'overview.expandRepoHint' : 'overview.collapseRepoHint')}>
                            <Icon name={collapsed ? 'chevronRight' : 'chevronDown'} />
                          </button>
                          <span className="issue-repo-name issue-repo-group-name" title={tr('overview.repoTitle')}><Icon name="folder" /> {r.repo_name || tr('common.deleted')}</span>
                          <span className="badge badge-muted" title={tr('overview.repoPriorityTitle')}>
                            优先级 {r.priority ?? 100}
                          </span>
                          {r.token_expiry?.level && r.token_expiry.level !== 'unknown' && (
                            <span className={`badge token-expiry-${r.token_expiry.level}`}
                                  title="请前往 GitLab 新建 Personal Access Token 后，在仓库管理页更新到期日">
                              Token {r.token_expiry.days_remaining < 0 ? '已到期' : `剩余 ${r.token_expiry.days_remaining} 天`}
                            </span>
                          )}
                          <span className="muted" title={issueFilterActive
                            ? '当前过滤条件下匹配的开放 issue 数量'
                            : '该仓库开放 issue 总数'}>
                            {issueFilterActive ? `匹配 ${repoIssues.length} 个` : `${repoIssues.length} 个开放 issue`}
                          </span>
                          {repoActionButtons(r)}
                        </div>
                        {/* 对账/自省/发掘结果与手动顺序保存失败提示——与卡片
                            布局一致紧贴组头展示，折叠时也保留操作反馈 */}
                        {reconcileResults[r.repo_id] && <ReconcileResult result={reconcileResults[r.repo_id]} />}
                        {introspectResults[r.repo_id] && <IntrospectResult result={introspectResults[r.repo_id]} />}
                        {discoverResults[r.repo_id] && <DiscoverResult result={discoverResults[r.repo_id]} />}
                        {manualErrors[r.repo_id] && (
                          <div className="alert alert-error issue-manual-error"
                               title={tr('overview.manualOrderTitle')}
                               onClick={() => setManualErrors((prev) => {
                                 const next = { ...prev }
                                 delete next[r.repo_id]
                                 return next
                               })}>
                            <Icon name="warning" /> {manualErrors[r.repo_id]}
                          </div>
                        )}
                        {!collapsed && (
                          repoIssues.length === 0 ? (
                            <div className="empty-state small">
                              <span className="empty-icon" aria-hidden="true"><Icon name="clipboard" /></span>
                              <p className="muted">{tr('overview.repoNoOpenIssues')}</p>
                            </div>
                          ) : (
                            /* issue #471：组内平铺该仓库全部 issue（保持当前
                               排序/过滤结果），状态由 issue 项上的状态徽章与
                               running 高亮传达；置顶按钮对所有 issue 可用 */
                            <ul className="issue-list">
                              {repoIssues.map((i, idx) =>
                                renderIssueItem(r, i, 'other', idx, { dragEnabled: false }))}
                            </ul>
                          )
                        )}
                      </div>
                    )
                  })}
                </div>
              ) : (
              <div className="issues-list">
                {filteredRepoIssues.map((r) => (
                  <div key={r.repo_id} className="card issue-repo-card" data-repo-id={r.repo_id}>
                    <div className="issue-repo-head">
                      <span className="issue-repo-name" title={tr('overview.repoTitle')}><Icon name="folder" /> {r.repo_name || tr('common.deleted')}</span>
                      <span className="badge badge-muted" title={tr('overview.repoPriorityTitle')}>
                        优先级 {r.priority ?? 100}
                      </span>
                      {r.token_expiry?.level && r.token_expiry.level !== 'unknown' && (
                        <span className={`badge token-expiry-${r.token_expiry.level}`}
                              title="请前往 GitLab 新建 Personal Access Token 后，在仓库管理页更新到期日">
                          Token {r.token_expiry.days_remaining < 0 ? '已到期' : `剩余 ${r.token_expiry.days_remaining} 天`}
                        </span>
                      )}
                      <span className="muted" title={issueFilterActive
                        ? '当前过滤条件下匹配的开放 issue 数量'
                        : '该仓库开放 issue 总数'}>
                        {issueFilterActive ? `匹配 ${r.issues.length} 个` : `${r.issues.length} 个开放 issue`}
                      </span>
                      {/* issue #134：卡片右上角操作组——「对账」按钮 + issue #92
                          「添加 Issue」按钮，整体推到卡片头最右侧 */}
                      {repoActionButtons(r)}
                    </div>
                    {/* issue #134：对账结果——与仓库页对账结果一致，小字展示
                        扫描/补入队结果，请求失败显示错误 */}
                    {reconcileResults[r.repo_id] && <ReconcileResult result={reconcileResults[r.repo_id]} />}
                    {/* issue #187：自省结果——AI 审查进行中 / 已创建自省
                        issue（带跳转链接）/ 失败原因 */}
                    {introspectResults[r.repo_id] && <IntrospectResult result={introspectResults[r.repo_id]} />}
                    {/* issue #189：发掘结果——AI 发掘进行中 / 已创建发掘
                        issue 链接列表 / 失败原因 */}
                    {discoverResults[r.repo_id] && <DiscoverResult result={discoverResults[r.repo_id]} />}
                    {/* issue #287：手动调度顺序保存失败提示——点击可关闭，
                        与概览页其他 alert 交互一致 */}
                    {manualErrors[r.repo_id] && (
                      <div className="alert alert-error issue-manual-error"
                           title={tr('overview.manualOrderTitle')}
                           onClick={() => setManualErrors((prev) => {
                             const next = { ...prev }
                             delete next[r.repo_id]
                             return next
                           })}>
                        <Icon name="warning" /> {manualErrors[r.repo_id]}
                      </div>
                    )}
                    {(r.issues || []).length === 0 ? (
                      <div className="empty-state small">
                        <span className="empty-icon" aria-hidden="true"><Icon name="clipboard" /></span>
                        <p className="muted">{tr('overview.repoNoOpenIssues')}</p>
                      </div>
                    ) : (
                      /* issue #80：按 bot 终态标签分组（bot-failed / bot-done /
                         其他），只渲染非空组，组标题带计数
                         issue #101：正在运行的 issue 独立成 running 组置顶，
                         任务结束键消失后自动回落原分组 */
                      ISSUE_GROUPS.map((g) => {
                        const items = groupIssuesByBotLabel(r.issues, runningKeys, r.repo_id)[g.key]
                        if (items.length === 0) return null
                        // issue #285：分组折叠开关——折叠态隐藏组内 issue
                        // 列表、保留组标题与计数（chevron 方向指示状态）
                        const collapsed = collapsedGroups.has(g.key)
                        // issue #287：手动调度顺序——仅「调度器执行顺序」
                        // 排序下应用（其余排序视图按时间重排，手动顺序仍
                        // 影响实际调度）；「其他」分组 + 无过滤 + 保存中
                        // 除外时才允许拖动（过滤子集拖动会误清未显示条目
                        // 的顺序；保存中禁用防并发覆盖）
                        const manualIids = manualOrders[r.repo_id] || []
                        const ordered = issueSort === 'scheduler'
                          ? applyManualOrder(items, manualIids) : items
                        const repoProjectId = r.project_id != null
                          ? r.project_id
                          : (r.issues[0] && r.issues[0].project_id)
                        const dragEnabled = issueSort === 'scheduler'
                          && g.key === 'other' && !issueFilterActive
                          && ordered.length > 1 && repoProjectId != null
                          && !manualSaving.has(r.repo_id)
                        return (
                          <div key={g.key} className={'issue-group' + (collapsed ? ' issue-group-collapsed' : '')}>
                            <div className="issue-group-head">
                              <button type="button"
                                      className="issue-group-toggle"
                                      onClick={() => setCollapsedGroups((prev) => toggleGroupCollapsed(prev, g.key))}
                                      aria-expanded={!collapsed}
                                      aria-label={tr(collapsed ? 'overview.expandGroup' : 'overview.collapseGroup')}
                                      title={tr(collapsed ? 'overview.expandGroupHint' : 'overview.collapseGroupHint')}>
                                <Icon name={collapsed ? 'chevronRight' : 'chevronDown'} />
                              </button>
                              <span className="issue-group-title" title={tr(`overview.groupHint.${g.key}`)}><Icon name={g.icon} /> {tr(`overview.group.${g.key}`)}</span>
                              <span className="issue-group-count"
                                    title={tr('overview.groupCountTitle')}>{tr('overview.groupCount', { n: items.length })}</span>
                              {/* issue #287：「其他」分组拖动排序提示——仅
                                  调度器执行顺序 + 无过滤 + 多条目时显示 */}
                              {dragEnabled && (
                                <span className="issue-drag-note"
                                      title={tr('overview.manualOrderTitle')}>
                                  <Icon name="gripVertical" /> {tr('overview.manualOrderHint')}
                                </span>
                              )}
                            </div>
                            {!collapsed && (
                            <ul className="issue-list">
                              {ordered.map((i, idx) =>
                                renderIssueItem(r, i, g.key, idx,
                                                { dragEnabled, ordered }))}
                            </ul>
                            )}
                          </div>
                        )
                      })
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>
  )
}


// 对账结果（issue #134）：与仓库页对账结果（issue #17）一致——
// 入队 N 个 = 发现待处理；0 个 = 无需处理；仓库停用时后端返回 note
function ReconcileResult({ result }) {
  const { tr } = useI18n()
  if (result.error) return <div className="alert alert-error small reconcile-result">{result.error}</div>
  if (result.note) return <div className="small muted reconcile-result">{result.note}</div>
  return (
    <div className="small reconcile-result">
      {result.enqueued > 0
        ? <span className="test-chip ok"><Icon name="check" /> {tr('overview.reconcileEnqueued', { n: result.enqueued })}</span>
        : <span className="test-chip ok"><Icon name="check" /> {tr('overview.reconcileNoop')}</span>}
      {result.scanned > 0 && <span className="muted">{tr('overview.reconcileScanned', { n: result.scanned })}</span>}
    </div>
  )
}

// 自省结果（issue #187）：AI 审查进行中显示加载提示；成功显示已创建的
// 自省 issue 链接（点击跳转 GitLab）；失败显示后端错误信息
function IntrospectResult({ result }) {
  const { tr } = useI18n()
  if (result.error) return <div className="alert alert-error small introspect-result">{result.error}</div>
  if (result.loading) return <div className="small muted introspect-result"><Icon name="refresh" /> {tr('overview.introspectLoading')}</div>
  if (result.created?.web_url) {
    return (
      <div className="small introspect-result">
        <span className="test-chip ok"><Icon name="check" /> {tr('overview.introspectCreated')}</span>{' '}
        <a href={result.created.web_url} target="_blank" rel="noreferrer"
           title={tr('overview.openIntrospectInGitlab')}>
          #{result.created.iid} <Icon name="externalLink" />
        </a>
      </div>
    )
  }
  return null
}

// 发掘结果（issue #189）：AI 发掘进行中显示加载提示；失败显示后端错误信息；
// 成功时无论是否创建了需求 issue，都展示相似仓库列表（issue #301）——
// 有创建的 issue 时显示「已创建 N 个发掘 issue」+ 跳转链接；未找到用户需求
// issue 时提示「未找到用户需求 issue」，两种情况下方均列出相似仓库
// （仓库名链接 + star + 描述）
function DiscoverResult({ result }) {
  const { tr } = useI18n()
  if (result.error) return <div className="alert alert-error small discover-result">{result.error}</div>
  if (result.loading) return <div className="small muted discover-result"><Icon name="refresh" /> {tr('overview.discoverLoading')}</div>
  // issue #301：响应始终携带 similar_repos（后端保证），无列表则视为无结果
  if (!result.similar_repos?.length) return null
  const createdCount = result.created?.length || 0
  return (
    <div className="small discover-result">
      {createdCount > 0 ? (
        <div className="discover-created-line">
          <span className="test-chip ok"><Icon name="check" /> {tr('overview.discoverCreated', { n: createdCount })}</span>{' '}
          {result.created.map((issue, i) => (
            <a key={issue.web_url || i} href={issue.web_url} target="_blank" rel="noreferrer"
               title={tr('overview.openDiscoverInGitlab')}>
              #{issue.iid}{i < createdCount - 1 ? '、' : ''} <Icon name="externalLink" />
            </a>
          ))}
        </div>
      ) : (
        <div className="muted discover-no-issue">{tr('overview.discoverNoIssue')}</div>
      )}
      <div className="discover-repos">
        <div className="discover-repos-title">{tr('overview.discoverRepos')}</div>
        {result.similar_repos.map((repo, i) => (
          <div key={repo.full_name || i} className="discover-repo">
            <a href={repo.html_url} target="_blank" rel="noreferrer"
               title={tr('overview.openSimilarRepo')}>
              {repo.full_name} <Icon name="externalLink" />
            </a>
            {repo.stars != null && <span className="muted discover-repo-stars">⭐ {repo.stars}</span>}
            {repo.description && <span className="muted discover-repo-desc">— {repo.description}</span>}
          </div>
        ))}
      </div>
    </div>
  )
}
