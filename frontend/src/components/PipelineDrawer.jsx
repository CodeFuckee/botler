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
import { useEffect } from 'react'
import { Icon } from './Icon.jsx'
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

// 流水线详情右边栏：entry 为 /api/pipelines/overview 单条仓库聚合
// （repo_id / repo_name / enabled / pipeline / stages / commit_time）。
// 异常数据防御：entry 为 null / 非对象 / pipeline 缺失时展示空态；
// stages 非数组视为空；stage / job 字段缺失逐项兜底，不崩溃。
export default function PipelineDrawer({ entry, onClose }) {
  // Esc 关闭本层抽屉（SSR 测试环境无 document 时跳过，与 IssueDrawer 一致）
  useEffect(() => {
    if (typeof document === 'undefined') return
    const onKey = (e) => {
      if (isEscapeKey(e)) onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const repo = entry && typeof entry === 'object' ? entry : null
  const repoName = repo && repo.repo_name ? repo.repo_name : '—'
  const pl = repo ? repo.pipeline : null
  const stages = repo && Array.isArray(repo.stages) ? repo.stages : []
  const meta = pl ? (PIPELINE_STATUS_META[pl.status] || { label: pl.status, cls: '' }) : null

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer pipeline-drawer" role="dialog" aria-modal="true"
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
            {/* 阶段与任务明细：每个 stage 一个区块，列出其下 job */}
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
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
