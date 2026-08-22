// 概览页（issue #201 拆分）：巨型组件按板块拆分为独立组件——
// IssueListSection / InspirationSection / PipelineSection /
// DeepSeekBalanceCard，数据加载与轮询收敛到 useOverviewData hook，
// 本文件只做组合编排（主文件 ≤400 行），行为与拆分前一致。
import { useEffect, useRef } from 'react'
import { useInRouterContext, useLocation, useNavigate } from 'react-router-dom'
import { useI18n } from '../i18n.jsx'
import { useOverviewData } from '../hooks/useOverviewData.js'
import IssueListSection from '../components/overview/IssueListSection.jsx'
import InspirationSection from '../components/overview/InspirationSection.jsx'
import PipelineSection from '../components/overview/PipelineSection.jsx'
import DeepSeekBalanceCard from '../components/overview/DeepSeekBalanceCard.jsx'
import IssueDrawer from '../components/IssueDrawer.jsx'
import PipelineDrawer from '../components/PipelineDrawer.jsx'
import AddIssueModal from '../components/AddIssueModal.jsx'

// 纯函数 / 常量 / 流水线状态映射：统一从 lib 再导出，保持旧导入路径兼容
export {
  LIVE_STATUSES, ACTIVE_TASK_STATUSES,
  MAX_CARD_LINES,
  OVERVIEW_POLL_MS,
  PIPELINE_POLL_MS,
  ISSUE_POLL_MS,
  INSPIRATION_POLL_MS,
  DEEPSEEK_BALANCE_POLL_MS,
  DEEPSEEK_TOPUP_URL,
  BOT_STATUS_NAMES,
  BOT_STATUS_META,
  ISSUE_GROUPS,
  runningIssueKeys, activeIssueKeys,
  tasksForIssue,
  fmtRateWindowText,
  engineLabel,
  botStatusKey,
  groupIssuesByBotLabel,
  ISSUE_FILTER_STORAGE_KEY,
  ISSUE_STATUS_FILTERS,
  loadIssueFilter,
  saveIssueFilter,
  GROUP_COLLAPSE_STORAGE_KEY,
  loadCollapsedGroups,
  saveCollapsedGroups,
  toggleGroupCollapsed,
  ISSUE_SORT_STORAGE_KEY,
  DEFAULT_ISSUE_PRIORITY,
  ISSUE_SORTS,
  loadIssueSort,
  saveIssueSort,
  issueLabelWeight,
  schedulerOrderKey,
  sortIssuesByMethod,
  MANUAL_ORDER_LOCAL_TTL_MS,
  applyManualOrder,
  moveItem,
  pinIssueToTop,
  issueLabelNames,
  collectLabelOptions,
  matchesIssueStatus,
  matchesIssueLabels,
  filterIssuesByFilter,
  trimLogTail,
  eventToLine,
} from '../lib/overview.jsx'
export { PIPELINE_STATUS_META, stageClass } from '../components/PipelineDrawer.jsx'
import { findIssueInRepos, repoScrollSelector } from '../lib/searchJump.js'

export default function Overview() {
  // 深链消费（?issue=/?repo=，issue #216）依赖 useLocation/useNavigate，
  // 仅在 Router 上下文内可用；单组件测试（无 Router）渲染静态版不抛
  // invariant（lib/searchJump 纯函数已单测，Router 版另建集成测试）
  const inRouter = useInRouterContext()
  return inRouter ? <OverviewWithRouter /> : <OverviewStatic />
}

function OverviewStatic() {
  return <OverviewBody location={null} navigate={null} />
}

function OverviewWithRouter() {
  const location = useLocation()
  const navigate = useNavigate()
  return <OverviewBody location={location} navigate={navigate} />
}

function OverviewBody({ location, navigate }) {
  const { tr } = useI18n()
  const {
    dsBalance,
    selectedIssue,
    selectedPipeline,
    addIssueRepo,
    setSelectedIssue,
    setSelectedPipeline,
    setAddIssueRepo,
    setReconcileResults,
    loadIssues,
    ...data
  } = useOverviewData()

  // 全局搜索跳转（issue #216）：消费 SearchOverlay 生成的概览页深链——
  //   ?issue=<project_id>:<iid>      → 数据加载后打开该 issue 详情抽屉
  //   ?repo=<repo_id>[&section=inspirations] → 滚动定位该仓库卡片并短暂高亮
  // 消费一次后清理 URL 参数（replace，不产生新历史记录）。repoIssues /
  // inspirationRepos 为空说明数据未到（首次轮询中），等下一次更新再尝试；
  // 数据已到但找不到目标（issue 已关闭/超出每仓库条数上限）则只清理参数。
  const jumpHandledRef = useRef(false)
  useEffect(() => {
    if (!location || !navigate) return // 无 Router 上下文（测试）：不做深链消费
    if (jumpHandledRef.current) return
    const params = new URLSearchParams(location.search)
    const issue = params.get('issue')
    const repo = params.get('repo')
    if (!issue && !repo) return
    const clearParams = () => {
      if (location.search) navigate(location.pathname, { replace: true })
    }
    if (repo) {
      const section = params.get('section')
      const ready = section === 'inspirations'
        ? data.inspirationRepos.length > 0
        : data.repoIssues.length > 0
      if (!ready) return // 板块数据未到，等轮询更新后重试
      const selector = repoScrollSelector(repo, section)
      const doScroll = () => {
        if (typeof document !== 'undefined' && typeof document.querySelector === 'function') {
          const el = document.querySelector(selector)
          if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'start' })
            el.classList.add('search-jump-highlight')
            setTimeout(() => el.classList.remove('search-jump-highlight'), 2000)
          }
        }
      }
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(doScroll)
      else doScroll()
      jumpHandledRef.current = true
      clearParams()
      return
    }
    if (issue) {
      const [pid, iid] = issue.split(':')
      const found = findIssueInRepos(data.repoIssues, pid, iid)
      if (found) {
        setSelectedIssue({ issue: found.issue, repoName: found.repoName })
        jumpHandledRef.current = true
        clearParams()
      } else if (data.repoIssues.length > 0) {
        // 数据已加载但未命中（issue 已关闭/超出上限）：只清理参数不报错
        jumpHandledRef.current = true
        clearParams()
      }
      // repoIssues 为空：数据未到，等下次轮询更新后重试
    }
  }, [data.repoIssues, data.inspirationRepos, location, navigate,
      setSelectedIssue])

  return (
    <div>
      <h1>{tr('overview.title')}</h1>

      {/* issue #138：DeepSeek 账户余额卡片——设置里配置了 deepseek api 时
          展示（未配置时整卡不渲染），数据由 useOverviewData 轮询注入 */}
      {dsBalance && dsBalance.configured && (
        <DeepSeekBalanceCard {...data} dsBalance={dsBalance} />
      )}

      {/* 开放 Issue 板块（含过滤条 / 仓库卡片 / 分组 / issue 项） */}
      <IssueListSection {...data}
                        addIssueRepo={addIssueRepo}
                        setSelectedIssue={setSelectedIssue}
                        setAddIssueRepo={setAddIssueRepo}
                        loadIssues={loadIssues} />

      {/* 灵感板块（含 AI 对话右侧抽屉） */}
      <InspirationSection {...data} />

      {/* CI/CD 流水线板块 */}
      <PipelineSection {...data} setSelectedPipeline={setSelectedPipeline} />

      {/* issue #85：issue 详情右边栏——点击列表项打开，显示具体信息与正文。
          issue #94：关闭 issue 成功后刷新列表（后端已清缓存，该 issue
          从开放列表消失）；抽屉保持打开，状态徽章由抽屉内部更新。
          issue #108：标记编辑成功后同样刷新列表（后端已清缓存，
          列表卡片标记即时同步） */}
      {selectedIssue && (
        <IssueDrawer issue={selectedIssue.issue} repoName={selectedIssue.repoName}
                     running={selectedIssue.active ?? selectedIssue.running}
                     onClose={() => setSelectedIssue(null)}
                     onIssueClosed={() => loadIssues()}
                     onLabelsUpdated={() => loadIssues()}
                     onRetried={() => loadIssues()}
                     onRun={() => loadIssues()}
                     onAssigneeUpdated={() => loadIssues()}
                     onPrioritized={() => loadIssues()} />
      )}

      {/* issue #317：流水线详情右边栏——点击流水线卡片打开，展示该仓库
          最新流水线运行详情；右上角「在 GitLab 中打开」按钮跳转 GitLab */}
      {selectedPipeline && (
        <PipelineDrawer entry={selectedPipeline}
                        onClose={() => setSelectedPipeline(null)} />
      )}

      {/* issue #92：添加 issue 弹窗——创建成功后关闭并立即刷新列表 */}
      {addIssueRepo && (
        <AddIssueModal repo={addIssueRepo}
                       onClose={() => setAddIssueRepo(null)}
                       onCreated={({ reconcileError } = {}) => {
                         setAddIssueRepo(null)
                         // issue #425：创建已成功时，无论后续自动对账是否失败都
                         // 刷新列表；失败只展示可手动重试的提示，避免误报创建失败。
                         if (reconcileError) {
                           setReconcileResults((prev) => ({
                             ...prev,
                             [addIssueRepo.repo_id]: { error: `已创建 Issue，但自动对账失败：${reconcileError}` },
                           }))
                         }
                         loadIssues()
                       }} />
      )}
    </div>
  )
}
