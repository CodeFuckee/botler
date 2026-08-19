// 概览页（issue #201 拆分）：巨型组件按板块拆分为独立组件——
// IssueListSection / InspirationSection / PipelineSection /
// DeepSeekBalanceCard，数据加载与轮询收敛到 useOverviewData hook，
// 本文件只做组合编排（主文件 ≤400 行），行为与拆分前一致。
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
  LIVE_STATUSES,
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
  runningIssueKeys,
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

export default function Overview() {
  const { tr } = useI18n()
  const {
    dsBalance,
    selectedIssue,
    selectedPipeline,
    addIssueRepo,
    setSelectedIssue,
    setSelectedPipeline,
    setAddIssueRepo,
    loadIssues,
    ...data
  } = useOverviewData()

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
                        setSelectedIssue={setSelectedIssue}
                        setAddIssueRepo={setAddIssueRepo} />

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
                     running={selectedIssue.running}
                     onClose={() => setSelectedIssue(null)}
                     onIssueClosed={() => loadIssues()}
                     onLabelsUpdated={() => loadIssues()}
                     onRetried={() => loadIssues()}
                     onAssigneeUpdated={() => loadIssues()} />
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
                       onCreated={() => {
                         setAddIssueRepo(null)
                         loadIssues()
                       }} />
      )}
    </div>
  )
}
