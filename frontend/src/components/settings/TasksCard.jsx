// 任务调度配置卡片（issue #201 拆分）：从 Settings.jsx 抽出。含数字字段
// 表单（跨仓库并行上限等）、issue 标签优先级、定时暂停窗口（textarea +
// 生效星期 + 时区 + 豁免优先级）、任务执行引擎下拉，以及全局「保存」与
// 「立即对账一次」按钮；数据与处理函数经 props 注入（useSettingsData
// hook），行为与拆分前一致。
import { Icon } from '../Icon.jsx'
import { FIELD_LABELS, COMMON_TZ, WEEKDAY_LABELS } from '../../hooks/useSettingsData.js'

export default function TasksCard({
  settings, setWorkerField, setIssuePriority, setFallbackEngines, pauseWindowsInput,
  setPauseWindowsText, togglePauseWeekday, busy, save, reconcileNow, reconcileNote,
}) {
  return (
    <div className="card">
      <h2>任务调度</h2>
      <table className="table kv">
        <tbody>
          {Object.entries(FIELD_LABELS).map(([key, label]) => (
            <tr key={key}>
              <th>{label} <code>{key}</code></th>
              <td>
                <input
                  className="input num-input"
                  type="number"
                  min={key === 'reconcile_jitter_min_seconds' || key === 'reconcile_jitter_max_seconds' ? 0 : 0.1}
                  step={key.includes('seconds') && key !== 'reconcile_interval_seconds' ? 0.1 : 1}
                  value={settings.worker[key]}
                  onChange={(e) => setWorkerField(key, e.target.value)}
                />
              </td>
            </tr>
          ))}
          <tr>
            <th>issue 标签优先级 <code>worker.issue_priority</code></th>
            <td>
              <input
                className="input grow"
                placeholder="bug, test, feature"
                value={(settings.worker.issue_priority || []).join(', ')}
                onChange={(e) => setIssuePriority(e.target.value)}
              />
            </td>
          </tr>
          <tr>
            <th>
              定时暂停窗口
              <br /><code>worker.pause_windows</code>
            </th>
            <td>
              {settings.worker?.pause_active && (
                <div className="alert alert-warning small">
                  <Icon name="warning" /> 当前处于暂停窗口：新任务暂缓开始，运行中任务不受影响，
                  未开始任务将在窗口结束后自动执行。
                </div>
              )}
              <textarea
                className="input grow"
                rows={3}
                placeholder={'09:00-12:00\n14:00-18:00'}
                value={pauseWindowsInput}
                onChange={(e) => setPauseWindowsText(e.target.value)}
              />
              <div className="weekday-row">
                <span className="muted">生效星期 <code>worker.pause_weekdays</code>：</span>
                {WEEKDAY_LABELS.map(([label, day]) => (
                  <label key={day} className="weekday-check">
                    <input
                      type="checkbox"
                      className="check-input"
                      checked={(settings.worker?.pause_weekdays || []).includes(day)}
                      onChange={() => togglePauseWeekday(day)}
                    />
                    {label}
                  </label>
                ))}
                <span className="muted">（不勾选 = 每天都生效）</span>
              </div>
              <div className="weekday-row">
                <span className="muted">判断时区 <code>worker.pause_timezone</code></span>
              </div>
              <input
                className="input grow"
                list="pause-timezone-options"
                placeholder="时区（IANA 名，留空 = 服务器本地时区）"
                value={settings.worker?.pause_timezone || ''}
                onChange={(e) => setWorkerField('pause_timezone', e.target.value.trim())}
              />
              <datalist id="pause-timezone-options">
                {COMMON_TZ.map((tz) => <option key={tz} value={tz} />)}
              </datalist>
              <div className="weekday-row">
                <span className="muted">豁免优先级 <code>worker.pause_priority_threshold</code>：</span>
              </div>
              <input
                className="input num-input"
                type="number"
                min={0}
                max={999}
                placeholder="0（关闭）"
                value={settings.worker?.pause_priority_threshold ?? 0}
                onChange={(e) => setWorkerField('pause_priority_threshold', e.target.value)}
              />
              <span className="muted small">
                仓库调度优先级（数字越小越优先）不差于该值的仓库，在暂停窗口内
                仍可开始新任务；0 = 关闭（所有仓库都受暂停窗口约束）。
              </span>
            </td>
          </tr>
          <tr>
            <th>任务执行引擎 <code>worker.engine</code></th>
            <td>
              <select
                className="input"
                value={settings.worker?.engine || 'claude'}
                onChange={(e) => setWorkerField('engine', e.target.value)}
              >
                <option value="claude">claude — Claude Code CLI（默认）</option>
                <option value="hermes">hermes — hermes-agent SDK</option>
                <option value="dsh">dsh — deepseek-harness SDK</option>
              </select>
              <span className="muted small">切换后对新领取的任务生效。</span>
            </td>
          </tr>
          <tr>
            <th>
              备用引擎
              <br /><code>worker.fallback_engines</code>
            </th>
            <td>
              <input
                className="input grow"
                placeholder="dsh, hermes"
                value={(settings.worker?.fallback_engines || []).join(', ')}
                onChange={(e) => setFallbackEngines(e.target.value)}
              />
              <span className="muted small">
                主引擎健康探测不可用或连续
                <code>fallback_after_failures</code> 次引擎类失败时，按此顺序
                自动降级到备用引擎重试（空 = 不降级）。任务记录与 issue 评论
                会注明降级原因。
              </span>
            </td>
          </tr>
          <tr>
            <th>引擎健康状态</th>
            <td>
              {(settings.worker?.engine_health || []).length === 0 ? (
                <span className="muted small">暂无探测数据（保存配置后重试）</span>
              ) : (
                <div className="engine-health-row">
                  {settings.worker.engine_health.map((h) => (
                    <span
                      key={h.engine}
                      className={'engine-health-badge engine-health-' + (h.status || 'unknown')}
                      title={`${h.detail || ''}（探测于 ${h.checked_at || ''}）`}
                    >
                      {h.engine}
                      {' '}
                      <span className="engine-health-status">
                        {h.status === 'ok' ? '正常' : h.status === 'fail' ? '异常' : '未知'}
                      </span>
                    </span>
                  ))}
                </div>
              )}
              <span className="muted small">
                每次任务开始前实时探测（claude 执行 <code>claude --version</code>、
                hermes 检查 runner 可加载、dsh 检查 SDK 导入）；此展示为 30s 缓存结果。
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <p className="muted small">
        GitLab API 全局限速与对账抖动（issue #195）：所有 GitLab API 请求（包括
        webhook、定时与手动对账、429 重试）共享同一匀速通道，默认上限为 10 请求/秒。
        全量对账在每两个启用仓库之间按下限/上限随机等待；设为 0 可关闭对应抖动。
        GitLab 返回 HTTP 429 时会记录日志并执行指数退避重试。
      </p>
      <p className="muted small">
        issue 标签优先级：同仓库有多个排队任务时，按此顺序优先派发标签命中靠前的
        issue（默认 bug 最优先）；未列出的标签排在最后，同优先级按 issue 更新时间
        升序处理。逗号分隔、可增删调整顺序，修改后点击「保存」对已排队任务立即生效。
      </p>
      <p className="muted small">
        定时暂停窗口：窗口内停止开始新任务，已经开始执行的任务可以继续执行，
        未开始执行的任务等到窗口结束后自动开始执行。每行一个窗口（
        HH:MM-HH:MM，24 小时制，支持跨天如 22:00-02:00）；星期勾选生效
        范围（不勾选 = 每天都生效）；时区留空 = 服务器本地时区。修改后
        点击「保存」立即生效，对运行中任务无影响。
      </p>
      <p className="muted small">
        暂停窗口豁免优先级（issue #299）：填写仓库调度优先级阈值（1~999，
        数字越小越优先）后，优先级不差于该值（priority ≤ 阈值）的仓库在
        暂停窗口内仍可开始新任务，不受窗口影响；填 0 或留空 = 关闭豁免，
        所有仓库都受暂停窗口约束。
      </p>
      <p className="muted small">
        任务执行引擎：切换后端编写代码的 agent，默认 claude（Claude Code CLI）；
        hermes 为 hermes-agent SDK（进程内调用，源码经
        deploy/install-hermes-agent.sh editable 安装进 botler venv），dsh 为
        deepseek-harness SDK（DeepSeek API Key 走部署机环境变量或「dsh 引擎」
        配置段）。切换后点击「保存」立即生效，对新领取的任务使用新引擎，
        运行中任务不受影响。
      </p>
      <div className="form-row">
        <button className="btn btn-primary" disabled={busy} onClick={save}>
          {busy ? '保存中…' : '保存'}
        </button>
        <button className="btn" onClick={reconcileNow}>立即对账一次</button>
        {reconcileNote && <span className={reconcileNote.ok ? 'saved-hint' : 'err-hint'}><Icon name={reconcileNote.ok ? 'check' : 'x'} /> {reconcileNote.text}</span>}
      </div>
    </div>
  )
}
