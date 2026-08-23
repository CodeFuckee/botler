// 概览页 CI/CD 流水线详情右边栏（issue #317）：点击概览页流水线卡片
// 打开，展示该仓库最新一次流水线的运行详情——整体状态徽章、分支/提交、
// 创建/更新时间/时长、阶段与任务（job）明细；右上角「在 GitLab 中
// 打开」按钮（pipeline.web_url 新窗口）跳转 GitLab。
//
// 交互约定（与 issue 详情右边栏 issue #85 一致）：
// - 流水线卡片主体不再直接跳转 GitLab，跳转统一走抽屉右上角
//   「在 GitLab 中打开」按钮（web_url 新窗口）；
// - 关闭方式：右上角 × 按钮 / 点击遮罩 / Esc 键。
//
// 数据来源：复用 GET /api/pipelines/overview 已返回的 stages + jobs
// 明细（后端聚合时已按 GitLab jobs API 精简每个 job 的 id/name/status/
// allow_failure/web_url/artifacts），无需新增接口。issue #329：任务行
// 展示产物清单（文件名/大小，后端已过滤 trace/metadata 噪音），「下载
// 全部」经 GET /api/pipelines/{repo_id}/artifacts?job_id= 后端代理下载
// GitLab zip 归档（浏览器不持有 GitLab token）。
import { useEffect, useRef, useState } from 'react'
import { Icon } from './Icon.jsx'
import { ScrollContainerBackToTop } from './BackToTop.jsx'
import { api } from '../api.js'
import { shortSha, fmtTime, fmtSeconds, fmtSize } from '../api.js'

// 流水线整体状态 → 徽章映射（issue #39，自 Overview.jsx 移入本组件，
// 供详情抽屉与概览卡片共用）。样式类复用任务状态徽章 status-*
// （视觉语义一致：成功绿 / 失败红 / 运行蓝 / 其余灰）
export const PIPELINE_STATUS_META = {
  success: { label: '成功', cls: 'status-succeeded' },
  failed: { label: '失败', cls: 'status-failed' },
  running: { label: '运行中', cls: 'status-running' },
  pending: { label: '等待中', cls: 'status-queued' },
  created: { label: '已创建', cls: 'status-queued' },
  canceled: { label: '已取消', cls: 'status-interrupted' },
  skipped: { label: '已跳过', cls: 'status-interrupted' },
  manual: { label: '手动', cls: 'status-queued' },
}

// stage 状态 → 节点样式类（参考 GitLab CI/CD 阶段图颜色语义；
// pending/created/未知统一按待运行展示）
export function stageClass(status) {
  switch (status) {
    case 'success': return 'st-success'
    case 'failed': return 'st-failed'
    case 'running': return 'st-running'
    case 'canceled': return 'st-canceled'
    case 'skipped': return 'st-skipped'
    default: return 'st-pending'
  }
}

// Esc 键判定（纯函数导出，便于测试；与 IssueDrawer.isEscapeKey 同规则）
export function isEscapeKey(e) {
  return !!e && e.key === 'Escape'
}

// job 状态 → 短标签（详情区任务行展示；未知状态原样兜底不崩溃）
const JOB_STATUS_LABEL = {
  success: '成功', failed: '失败', running: '运行中',
  pending: '等待中', created: '已创建', canceled: '已取消',
  skipped: '已跳过', manual: '手动', waiting_for_resource: '等待资源',
  preparing: '准备中', scheduled: '已调度',
}
export function jobStatusLabel(status) {
  if (status == null || status === '') return '—'
  return JOB_STATUS_LABEL[status] || status
}

// ---- 报告查看（issue #337）----

// 可查看报告的产物 file_type：sast=静态分析（bandit/semgrep/gitleaks
// SARIF）、dependency_scanning=依赖扫描（deps-python/deps-frontend
// JSON）、junit=测试报告（pytest / node:test JUnit XML）
export const REPORT_FILE_TYPES = ['sast', 'dependency_scanning', 'junit']

// 报告类型 → 中文标题
const REPORT_TYPE_LABEL = {
  sast: '静态分析报告',
  dependency_scanning: '依赖扫描报告',
  junit: '测试报告',
}

// 严重级别 → 中文标签：sast 用归一化枚举（high/medium/low/info/unknown），
// 依赖扫描用 GitLab 枚举（Critical/High/...）
const SAST_SEVERITY_LABEL = { high: '高', medium: '中', low: '低', info: '信息', unknown: '未知' }
const DEPS_SEVERITY_LABEL = {
  Critical: '严重', High: '高', Medium: '中', Low: '低', Info: '信息', Unknown: '未知',
}
export function severityLabel(severity, kind) {
  const map = kind === 'deps' ? DEPS_SEVERITY_LABEL : SAST_SEVERITY_LABEL
  return (severity != null && map[severity]) || '未知'
}

// 测试用例状态 → 中文标签
const TEST_STATUS_LABEL = { passed: '通过', failed: '失败', error: '错误', skipped: '跳过' }
export function testStatusLabel(status) {
  return TEST_STATUS_LABEL[status] || status || '—'
}

// job 的报告产物列表（按类型白名单过滤；缺字段/非数组兜底）
export function reportArtifacts(job) {
  const arts = Array.isArray(job && job.artifacts) ? job.artifacts : []
  return arts.filter((a) => a && typeof a === 'object'
                      && REPORT_FILE_TYPES.includes(a.file_type)
                      && a.filename)
}

// 流水线详情右边栏：entry 为 /api/pipelines/overview 单条仓库聚合
// （repo_id / repo_name / enabled / pipeline / stages / commit_time）。
// 异常数据防御：entry 为 null / 非对象 / pipeline 缺失时展示空态；
// stages 非数组视为空；stage / job 字段缺失逐项兜底，不崩溃。
export default function PipelineDrawer({ entry, onClose }) {
  // issue #457：抽屉滚动容器 ref——右下角「回到顶部」按钮定位/监听于此
  const drawerRef = useRef(null)
  // Esc 关闭本层抽屉（SSR 测试环境无 document 时跳过，与 IssueDrawer 一致）
  useEffect(() => {
    if (typeof document === 'undefined') return
    const onKey = (e) => {
      if (isEscapeKey(e)) onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // 选中的报告（issue #337）：{repoId, jobId, jobName, file, fileType} 或 null
  const [report, setReport] = useState(null)
  // 选中的截图（issue #453）：{repoId, jobId, jobName} 或 null
  const [screenshot, setScreenshot] = useState(null)
  const repo = entry && typeof entry === 'object' ? entry : null
  const repoName = repo && repo.repo_name ? repo.repo_name : '—'
  const pl = repo ? repo.pipeline : null
  const stages = repo && Array.isArray(repo.stages) ? repo.stages : []
  const meta = pl ? (PIPELINE_STATUS_META[pl.status] || { label: pl.status, cls: '' }) : null

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer pipeline-drawer" role="dialog" aria-modal="true"
           ref={drawerRef}
           onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong className="issue-drawer-title">
            <Icon name="rocket" /> {repoName} — CI/CD 流水线
          </strong>
          <span className="issue-drawer-actions">
            {pl && (
              <a className="btn" href={pl.web_url} target="_blank" rel="noreferrer"
                 title="在 GitLab 中打开流水线">在 GitLab 中打开</a>
            )}
            <button className="btn modal-close" onClick={onClose} title="关闭"
                    aria-label="关闭右边栏"><Icon name="x" /></button>
          </span>
        </div>

        {!pl ? (
          <div className="empty-state">
            <span className="empty-icon" aria-hidden="true"><Icon name="rocket" /></span>
            <p className="muted">暂无流水线</p>
          </div>
        ) : (
          <>
            {/* 基本信息区：整体状态徽章 + 分支/提交 */}
            <div className="pipeline-detail-head">
              {meta && <span className={'badge ' + meta.cls}>{meta.label}</span>}
              <span className="pipeline-ref"
                    title={`分支 ${pl.ref || '—'} · 提交 ${pl.sha || '—'}`}>
                {pl.ref || '—'} · {shortSha(pl.sha)}
              </span>
            </div>
            {/* 流水线元信息：状态/分支/提交/时间/时长 */}
            <dl className="pipeline-detail-kv">
              <div className="pipeline-detail-kv-row">
                <dt>状态</dt>
                <dd>{meta ? meta.label : (pl.status || '—')}</dd>
              </div>
              <div className="pipeline-detail-kv-row">
                <dt>分支</dt>
                <dd className="mono">{pl.ref || '—'}</dd>
              </div>
              <div className="pipeline-detail-kv-row">
                <dt>提交</dt>
                <dd className="mono" title={pl.sha || ''}>{pl.sha || '—'}</dd>
              </div>
              <div className="pipeline-detail-kv-row">
                <dt>创建时间</dt>
                <dd>{fmtTime(pl.created_at)}</dd>
              </div>
              <div className="pipeline-detail-kv-row">
                <dt>更新时间</dt>
                <dd>{fmtTime(pl.updated_at)}</dd>
              </div>
              {pl.finished_at && (
                <div className="pipeline-detail-kv-row">
                  <dt>完成时间</dt>
                  <dd>{fmtTime(pl.finished_at)}</dd>
                </div>
              )}
              {pl.duration != null && (
                <div className="pipeline-detail-kv-row">
                  <dt>时长</dt>
                  <dd>{fmtSeconds(pl.duration) || '—'}</dd>
                </div>
              )}
            </dl>
            {/* 阶段与任务明细：每个 stage 一个区块，列出其下 job；
                issue #337：点击「查看报告」后切换为报告视图 */}
            {report ? (
              <ReportView {...report} onBack={() => setReport(null)} />
            ) : screenshot ? (
              <ScreenshotView {...screenshot} onBack={() => setScreenshot(null)} />
            ) : (
            <div className="pipeline-detail-stages">
              <h3>阶段与任务</h3>
              {stages.length === 0 ? (
                <p className="muted">暂无阶段信息</p>
              ) : stages.map((s) => (
                <div key={s.name || 'stage-' + stages.indexOf(s)}
                     className="pipeline-detail-stage">
                  <div className="pipeline-detail-stage-head">
                    <span className={'pipeline-detail-stage-dot pipeline-stage-dot '
                                     + stageClass(s.status)} />
                    <span className="pipeline-detail-stage-name">{s.name || '—'}</span>
                    <span className="muted">{jobStatusLabel(s.status)}</span>
                  </div>
                  {!Array.isArray(s.jobs) || s.jobs.length === 0 ? (
                    <p className="muted pipeline-detail-no-jobs">暂无任务</p>
                  ) : (
                    <ul className="pipeline-detail-jobs">
                      {s.jobs.map((j, i) => (
                        <li key={i} className="pipeline-detail-job">
                          <div className="pipeline-detail-job-row">
                            <span className={'pipeline-detail-job-dot pipeline-stage-dot '
                                             + stageClass(j.status)} />
                            <span className="pipeline-detail-job-name"
                                  title={jobStatusLabel(j.status)}>{j.name || '—'}</span>
                            <span className="muted pipeline-detail-job-status">
                              {jobStatusLabel(j.status)}
                            </span>
                            {j.web_url ? (
                              <a className="pipeline-detail-job-link" href={j.web_url}
                                 target="_blank" rel="noreferrer"
                                 title="在 GitLab 中打开任务">
                                <Icon name="externalLink" />
                              </a>
                            ) : null}
                          </div>
                          {/* issue #329：流水线产物——列出产物文件名/大小，
                              提供「下载全部」按钮（后端代理 GitLab zip 归档） */}
                          {Array.isArray(j.artifacts) && j.artifacts.length > 0 && (
                            <div className="pipeline-detail-artifacts">
                              <div className="pipeline-detail-artifacts-head">
                                <span className="pipeline-detail-artifacts-title">产物</span>
                                {j.id != null && repo.repo_id != null && (
                                  <a className="pipeline-detail-artifacts-download"
                                     href={`/api/pipelines/${repo.repo_id}/artifacts?job_id=${j.id}`}
                                     download
                                     title="下载该任务全部产物（zip 归档）">
                                    <Icon name="download" /> 下载全部
                                  </a>
                                )}
                              </div>
                              <ul className="pipeline-detail-artifact-list">
                                {j.artifacts.map((a, k) => (
                                  <li key={k} className="pipeline-detail-artifact"
                                      title={a.file_type ? `类型：${a.file_type}` : ''}>
                                    <span className="pipeline-detail-artifact-name"
                                          title={a.filename || ''}>{a.filename || '—'}</span>
                                    <span className="pipeline-detail-artifact-size">
                                      {fmtSize(a.size)}
                                    </span>
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}
                          {/* issue #453：job 成功且带 archive 产物（zip 归档）时
                              提供「查看截图」，点击在抽屉内列出并预览产物内的
                              png 截图（e2e:screenshots job）；无 png 时展示
                              空态提示，不崩溃 */}
                          {j.status === 'success' && j.id != null && repo.repo_id != null
                           && hasArchiveArtifact(j) && (
                            <div className="pipeline-detail-report">
                              <button type="button"
                                      className="pipeline-detail-screenshot-btn"
                                      onClick={() => setScreenshot({
                                        repoId: repo.repo_id,
                                        jobId: j.id,
                                        jobName: j.name,
                                      })}
                                      title="预览该任务产物中的截图">
                                <Icon name="image" /> 查看截图
                              </button>
                            </div>
                          )}
                          {/* issue #337：job 成功且带报告产物时提供「查看报告」，
                              点击在抽屉内直接渲染解析后的报告内容（issue 评论
                              确认交互 A；失败 job 不提供入口——评论确认） */}
                          {j.status === 'success' && j.id != null && repo.repo_id != null
                           && reportArtifacts(j).length > 0 && (
                            <div className="pipeline-detail-report">
                              {reportArtifacts(j).map((a, k) => (
                                <button key={k} type="button"
                                        className="pipeline-detail-report-btn"
                                        onClick={() => setReport({
                                          repoId: repo.repo_id,
                                          jobId: j.id,
                                          jobName: j.name,
                                          file: a.filename,
                                          fileType: a.file_type,
                                        })}
                                        title={`查看 ${a.filename}`}>
                                  <Icon name="fileText" /> 查看报告
                                </button>
                              ))}
                            </div>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
            )}
          </>
        )}
      <ScrollContainerBackToTop containerRef={drawerRef} />
      </div>
    </div>
  )
}

// ---- 报告视图（issue #337）----

// 严重级别/状态徽章（摘要区与明细行共用）
function SeverityBadge({ label, count, cls }) {
  return <span className={'pipeline-report-sev ' + cls}>{label} {count}</span>
}

// 报告视图：从后端 GET /api/pipelines/{repo_id}/report 拉取解析后报告
// （后端代理 GitLab 单文件产物并解析，浏览器不持有 GitLab token）。
// 加载中 / 接口失败 / 报告为空均有兜底，不崩溃。
export function ReportView({ repoId, jobId, jobName, file, fileType, onBack }) {
  const [state, setState] = useState({ loading: true, error: null, data: null })
  useEffect(() => {
    let cancelled = false
    setState({ loading: true, error: null, data: null })
    const qs = new URLSearchParams({ job_id: String(jobId), file, file_type: fileType })
    api.get(`/api/pipelines/${repoId}/report?${qs}`)
      .then((data) => { if (!cancelled) setState({ loading: false, data }) })
      .catch((e) => {
        if (!cancelled) setState({
          loading: false, error: (e && e.message) || '报告加载失败', data: null })
      })
    return () => { cancelled = true }
  }, [repoId, jobId, file, fileType])

  return (
    <div className="pipeline-report">
      <div className="pipeline-report-head">
        <button type="button" className="btn pipeline-report-back" onClick={onBack}
                title="返回阶段与任务明细">
          <Icon name="arrowLeft" /> 返回
        </button>
        <span className="pipeline-report-title"
              title={jobName || ''}>
          {jobName || '—'} · {REPORT_TYPE_LABEL[fileType] || '报告'}
        </span>
      </div>
      <div className="pipeline-report-file mono" title={file || ''}>{file || '—'}</div>
      {state.loading && <p className="muted pipeline-report-empty">报告加载中…</p>}
      {!state.loading && state.error && (
        <div className="alert alert-error pipeline-report-error">{state.error}</div>
      )}
      {!state.loading && !state.error && state.data && (
        <ReportBody report={state.data.report} />
      )}
    </div>
  )
}

// 按报告 kind 分派渲染明细；空/未知兜底
function ReportBody({ report }) {
  if (!report || typeof report !== 'object') {
    return <p className="muted pipeline-report-empty">报告内容为空</p>
  }
  if (report.kind === 'sast') return <SastReport report={report} />
  if (report.kind === 'deps') return <DepsReport report={report} />
  if (report.kind === 'test') return <TestReport report={report} />
  return <p className="muted pipeline-report-empty">未知的报告类型</p>
}

// 静态分析（SARIF）：摘要 + 问题列表（严重级别/规则/文件/行号/描述）
function SastReport({ report }) {
  const { summary, results } = report
  const by = summary && summary.by_severity ? summary.by_severity : {}
  return (
    <>
      <div className="pipeline-report-summary">
        <span>共 {summary ? summary.total : 0} 个问题</span>
        <SeverityBadge label="高" count={by.high || 0} cls="report-sev-high" />
        <SeverityBadge label="中" count={by.medium || 0} cls="report-sev-medium" />
        <SeverityBadge label="低" count={by.low || 0} cls="report-sev-low" />
        {(by.info || 0) > 0 && <SeverityBadge label="信息" count={by.info} cls="report-sev-info" />}
      </div>
      {!Array.isArray(results) || results.length === 0 ? (
        <p className="muted pipeline-report-empty">未发现静态分析问题</p>
      ) : (
        <ul className="pipeline-report-list">
          {results.map((r, i) => (
            <li key={i} className="pipeline-report-item">
              <div className="pipeline-report-item-row">
                <span className={'pipeline-report-sev report-sev-' + (r.severity || 'unknown')}>
                  {severityLabel(r.severity, 'sast')}
                </span>
                <span className="pipeline-report-rule mono" title={r.rule || ''}>{r.rule || '—'}</span>
                {r.file != null && (
                  <span className="pipeline-report-loc mono">
                    {r.file}{r.line != null ? ':' + r.line : ''}
                    {r.line != null && r.column != null ? ':' + r.column : ''}
                  </span>
                )}
              </div>
              {r.message && <div className="pipeline-report-msg">{r.message}</div>}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

// 依赖扫描：摘要 + 漏洞列表（包/版本/编号/文件/解决方案）
function DepsReport({ report }) {
  const { summary, results } = report
  const by = summary && summary.by_severity ? summary.by_severity : {}
  return (
    <>
      <div className="pipeline-report-summary">
        <span>共 {summary ? summary.total : 0} 个漏洞</span>
        <SeverityBadge label="严重" count={by.Critical || 0} cls="report-sev-critical" />
        <SeverityBadge label="高" count={by.High || 0} cls="report-sev-high" />
        <SeverityBadge label="中" count={by.Medium || 0} cls="report-sev-medium" />
      </div>
      {!Array.isArray(results) || results.length === 0 ? (
        <p className="muted pipeline-report-empty">未发现依赖漏洞</p>
      ) : (
        <ul className="pipeline-report-list">
          {results.map((r, i) => (
            <li key={i} className="pipeline-report-item">
              <div className="pipeline-report-item-row">
                <span className={'pipeline-report-sev report-sev-' + String(r.severity || 'Unknown').toLowerCase()}>
                  {severityLabel(r.severity, 'deps')}
                </span>
                <span className="pipeline-report-rule">{r.name || r.id || '—'}</span>
                {r.version != null && <span className="pipeline-report-loc mono">v{r.version}</span>}
              </div>
              <div className="pipeline-report-sub">
                {r.id && <span className="mono">{r.id}</span>}
                {r.file && <span className="mono">{r.file}</span>}
              </div>
              {r.solution && <div className="pipeline-report-msg">{r.solution}</div>}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

// 测试报告（JUnit）：汇总 + 用例明细（状态/名称/耗时/失败原因）
function TestReport({ report }) {
  const { summary, results } = report
  const s = summary || {}
  const passed = (s.tests || 0) - (s.failures || 0) - (s.errors || 0) - (s.skipped || 0)
  return (
    <>
      <div className="pipeline-report-summary">
        <span>共 {s.tests || 0} 个用例</span>
        <span className="pipeline-report-stat stat-pass">通过 {passed}</span>
        <span className="pipeline-report-stat stat-fail">失败 {(s.failures || 0) + (s.errors || 0)}</span>
        <span className="pipeline-report-stat stat-skip">跳过 {s.skipped || 0}</span>
        {s.time != null && <span className="muted">耗时 {s.time}s</span>}
      </div>
      {!Array.isArray(results) || results.length === 0 ? (
        <p className="muted pipeline-report-empty">无测试用例明细</p>
      ) : (
        <ul className="pipeline-report-list">
          {results.map((r, i) => (
            <li key={i} className="pipeline-report-item">
              <div className="pipeline-report-item-row">
                <span className={'pipeline-report-sev report-test-' + (r.status || 'unknown')}>
                  {testStatusLabel(r.status)}
                </span>
                <span className="pipeline-report-rule">{r.name || '—'}</span>
                {r.time != null && <span className="pipeline-report-loc">{r.time}s</span>}
              </div>
              {r.classname && <div className="pipeline-report-sub mono">{r.classname}</div>}
              {r.message && <div className="pipeline-report-msg">{r.message}</div>}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

// ---- 截图查看（issue #453）----

// job 是否带 archive 产物（zip 归档）：有归档才提供「查看截图」入口。
// e2e:screenshots job（issue #445）的 png 截图以 zip 归档形式存在于
// 产物中，缺字段 / 非数组兜底为 false。
export function hasArchiveArtifact(job) {
  return Array.isArray(job && job.artifacts)
    && job.artifacts.some((a) => a && typeof a === 'object' && a.file_type === 'archive')
}

// 截图列表 → 按页面分组 [{name, shots:[...]}]（保持后端排序稳定；脏数据逐项兜底）
export function groupScreenshots(shots) {
  const pages = []
  const byPage = new Map()
  for (const s of Array.isArray(shots) ? shots : []) {
    if (!s || typeof s !== 'object') continue
    const page = s.page || '—'
    if (!byPage.has(page)) {
      byPage.set(page, [])
      pages.push({ name: page, shots: byPage.get(page) })
    }
    byPage.get(page).push(s)
  }
  return pages
}

// 单张截图的后端代理 URL（图片字节直接返回，浏览器无需持有 GitLab token）
export function screenshotFileUrl(repoId, jobId, path) {
  const qs = new URLSearchParams({ job_id: String(jobId), path })
  return `/api/pipelines/${repoId}/screenshot-file?${qs}`
}

// 单张截图的缩略预览 URL（issue #456）：后端用 Pillow 把原图缩放出
// 小尺寸 JPEG 预览图。缩略图网格先加载预览图（e2e 整页截图原图可达
// 数 MB），用户点击放大进入大图预览时才用 screenshotFileUrl 拉原图。
export function screenshotPreviewUrl(repoId, jobId, path) {
  const qs = new URLSearchParams({ job_id: String(jobId), path })
  return `/api/pipelines/${repoId}/screenshot-preview?${qs}`
}

// 截图预览视图：从后端拉取 job 产物归档内的 png 截图列表，按页面分组
// 渲染缩略图网格，点击任一张进入大图预览（再点关闭）。加载中 / 接口
// 失败 / 归档内无截图均有兜底，不崩溃。与 ReportView 平行，复用
// pipeline-report-head 的「返回」交互。
export function ScreenshotView({ repoId, jobId, jobName, onBack }) {
  const [state, setState] = useState({ loading: true, error: null, screenshots: null })
  const [selected, setSelected] = useState(null)
  useEffect(() => {
    let cancelled = false
    setState({ loading: true, error: null, screenshots: null })
    setSelected(null)
    api.get(`/api/pipelines/${repoId}/screenshots?job_id=${jobId}`)
      .then((data) => {
        if (!cancelled) setState({
          loading: false,
          screenshots: data && Array.isArray(data.screenshots) ? data.screenshots : [],
        })
      })
      .catch((e) => {
        if (!cancelled) setState({
          loading: false, error: (e && e.message) || '截图加载失败', screenshots: [],
        })
      })
    return () => { cancelled = true }
  }, [repoId, jobId])

  const pages = state.screenshots ? groupScreenshots(state.screenshots) : []
  return (
    <div className="pipeline-screenshots">
      <div className="pipeline-report-head">
        <button type="button" className="btn pipeline-report-back" onClick={onBack}
                title="返回阶段与任务明细">
          <Icon name="arrowLeft" /> 返回
        </button>
        <span className="pipeline-report-title" title={jobName || ''}>
          {jobName || '—'} · 截图预览
        </span>
      </div>
      {state.loading && <p className="muted pipeline-report-empty">截图加载中…</p>}
      {!state.loading && state.error && (
        <div className="alert alert-error pipeline-report-error">{state.error}</div>
      )}
      {!state.loading && !state.error && state.screenshots
       && state.screenshots.length === 0 && (
        <p className="muted pipeline-report-empty">该任务产物中未发现截图</p>
      )}
      {!state.loading && !state.error && pages.map((page) => (
        <div key={page.name} className="pipeline-screenshots-page">
          <h4 className="pipeline-screenshots-page-name">{page.name}</h4>
          <div className="pipeline-screenshots-grid">
            {page.shots.map((s) => (
              <button key={s.path} type="button"
                      className="pipeline-screenshots-thumb"
                      onClick={() => setSelected(s)}
                      title={`${s.page || '—'}/${s.viewport || s.path}`}>
                <img src={screenshotPreviewUrl(repoId, jobId, s.path)}
                     alt={s.viewport || s.path} loading="lazy" />
                <span className="pipeline-screenshots-thumb-name">
                  {s.viewport || '—'}
                </span>
              </button>
            ))}
          </div>
        </div>
      ))}
      {selected && (
        <div className="pipeline-screenshots-lightbox"
             onClick={() => setSelected(null)}
             title="点击关闭大图预览">
          {/* 预览图作占位：点击放大瞬间先显示已缓存的缩略图，
              原图（issue #456）加载完成后覆盖，避免大图等待空白 */}
          <div className="pipeline-screenshots-lightbox-stage">
            <img className="pipeline-screenshots-lightbox-preview"
                 src={screenshotPreviewUrl(repoId, jobId, selected.path)}
                 alt="" aria-hidden="true" />
            <img className="pipeline-screenshots-lightbox-original"
                 src={screenshotFileUrl(repoId, jobId, selected.path)}
                 alt={selected.viewport || selected.path} />
          </div>
          <span className="pipeline-screenshots-lightbox-name">
            {selected.page || '—'} / {selected.viewport || selected.path}
          </span>
        </div>
      )}
    </div>
  )
}
