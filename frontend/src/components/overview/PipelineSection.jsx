// CI/CD 流水线板块（issue #201 拆分）：从 Overview.jsx 抽出的
// 概览页流水线板块子组件，数据由 useOverviewData hook 注入。
import { useI18n } from '../../i18n.jsx'
import { Icon } from '../Icon.jsx'
import { shortSha, fmtTime, fmtAgo } from '../../api.js'
import {
  PIPELINE_STATUS_META,
  stageClass,
} from '../../lib/overview.jsx'

export default function PipelineSection({
  pipeError, setPipeError, pipeErrors, pipelines, setSelectedPipeline,
}) {
  const { tr } = useI18n()
  return (
          <section className="pipelines-section">
            <h2>{tr('overview.pipelinesTitle')}</h2>
            <p className="muted">{tr('overview.pipelinesDesc')}</p>
            {pipeError && (
              <div className="alert alert-error" onClick={() => setPipeError('')}>{pipeError}</div>
            )}
            {pipeErrors.length > 0 && (
              <div className="alert alert-error">
                {pipeErrors.map((e, i) => <div key={i}>{e}</div>)}
              </div>
            )}
            {pipelines.length === 0 ? (
              <div className="empty-state">
                <span className="empty-icon" aria-hidden="true"><Icon name="rocket" /></span>
                <p className="muted">{tr('overview.noPipelines')}</p>
              </div>
            ) : (
              <div className="pipelines-list">
                {pipelines.map((p) => {
                  const pl = p.pipeline
                  const meta = pl
                    ? (PIPELINE_STATUS_META[pl.status] || { label: pl.status, cls: '' })
                    : null
                  return (
                    <div key={p.repo_id} className="card pipeline-card">
                      <div className="pipeline-head">
                        <span className="pipeline-repo" title={tr('overview.repoTitle')}><Icon name="folder" /> {p.repo_name || tr('common.deleted')}</span>
                        {p.enabled === false && (
                          <span className="badge badge-muted" title={tr('overview.repoDisabledTitle')}>{tr('common.disabled')}</span>
                        )}
                        {meta ? (
                          <span className={'badge ' + meta.cls}>{meta.label}</span>
                        ) : (
                          <span className="muted">{tr('overview.noPipelines')}</span>
                        )}
                      </div>
                      {pl && (
                        /* issue #317：卡片主体由 <a> 改为按钮——点击打开
                           流水线详情右边栏，跳转 GitLab 统一走抽屉右上角
                           「在 GitLab 中打开」按钮（与 issue 详情右边栏
                           issue #85 交互约定一致） */
                        <button type="button" className="pipeline-link"
                                onClick={() => setSelectedPipeline(p)}
                                title={tr('overview.viewPipelineDetail')}>
                          <span className="pipeline-ref" title={tr('overview.pipelineRefTitle', { ref: pl.ref, sha: pl.sha })}>
                            {pl.ref} · {shortSha(pl.sha)}
                          </span>
                          {/* 最近流水线对应提交的提交时间 + 距今多久（issue #43） */}
                          {p.commit_time && (
                            <span className="pipeline-commit-time">
                              {fmtTime(p.commit_time)}（{fmtAgo(p.commit_time) || '—'}）
                            </span>
                          )}
                          <div className="pipeline-stages">
                            {(p.stages || []).map((s, i) => (
                              <span key={i}
                                    className={`pipeline-stage ${stageClass(s.status)}`}
                                    title={`${s.name}: ${s.status}`}>
                                <span className="pipeline-stage-name">{s.name}</span>
                                <span className="pipeline-stage-dot" />
                              </span>
                            ))}
                          </div>
                        </button>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </section>
  )
}
