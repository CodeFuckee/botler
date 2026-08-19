// 本地环境检测卡片（issue #201 拆分）：从 Settings.jsx 抽出，进入设置页
// 自动检测一次（issue #22），表格展示工具安装状态 / 已装版本 / 最新版本
// 与状态提示，可「重新检测」；数据与处理函数经 props 注入
// （useSettingsData hook），行为与拆分前一致。
import { Icon } from '../Icon.jsx'
import { fmtTime } from '../../api.js'

// 单工具状态提示：未安装 / 无法获取最新版本 / 已是最新 / 可升级
function envStatus(t) {
  if (!t.installed) return <span className="muted">未安装</span>
  if (!t.latest) return <span className="muted">无法获取最新版本</span>
  return t.up_to_date
    ? <span className="ok-text"><Icon name="check" /> 已是最新</span>
    : <span className="err-hint"><Icon name="warning" /> 可升级</span>
}

export default function EnvironmentCard({
  env, envError, setEnvError, envBusy, loadEnv,
}) {
  return (
    <div className="card">
      <h2>本地环境检测</h2>
      <p className="muted small">
        检测 botler 服务器上常见 AI agent 与基础工具是否安装及其版本；
        最新版本来自 npm registry / GitHub API（网络不可达时显示 "—"）。
      </p>
      {envError && <div className="alert alert-error" onClick={() => setEnvError('')}>{envError}</div>}
      {!env && !envError && <p className="muted">检测中…</p>}
      {env && (
        <table className="table">
          <thead>
            <tr><th>工具</th><th>状态</th><th>已装版本</th><th>最新版本</th><th>提示</th></tr>
          </thead>
          <tbody>
            {env.tools.map((t) => (
              <tr key={t.key}>
                <td>{t.name} <code>{t.key}</code></td>
                <td>{t.installed
                  ? <span className="ok-text"><Icon name="check" /> 已安装</span>
                  : <span className="muted">未安装</span>}</td>
                <td>{t.version || <span className="muted">未知</span>}</td>
                <td>{t.latest || <span className="muted">—</span>}</td>
                <td>{envStatus(t)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="form-row">
        <button className="btn" disabled={envBusy} onClick={loadEnv}>
          {envBusy ? '检测中…' : '重新检测'}
        </button>
        {env && (
          <span className="muted small">
            {env.hostname} · {env.platform} · {fmtTime(env.detected_at)} 检测
          </span>
        )}
      </div>
    </div>
  )
}
