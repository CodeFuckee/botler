// 「仓库健康巡检」配置卡片（issue #265）：定时检查每个启用仓库的 webhook
// 有效性（存在且 secret 匹配）/ token 有效性 / 项目可达性，结果落库
// repo_health 表并展示健康徽章；webhook 缺失/secret 不匹配时自动重新注册；
// 异常聚合告警（in_app 网页通知 + webhook 推送，按 alerts.throttle_seconds
// 节流防刷屏）。独立保存（只提交 inspection 段，与「聚合告警」卡片同模式）。
import { Icon } from '../Icon.jsx'

const num = (v) => Number(v)

export default function InspectionCard({
  settings, setInspectionField, inspectionSaveBusy, saveInspection, inspectionSaved,
}) {
  const v = settings.inspection || {}
  return (
    <div className="card">
      <h2>仓库健康巡检</h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>启用巡检 <code>inspection.enabled</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={v.enabled !== false}
                onChange={(e) => setInspectionField('enabled', e.target.checked)}
              />
              <span className="muted small"> 定时检查启用仓库的健康状态（关闭后手动重检仍可用）</span>
            </td>
          </tr>
          <tr>
            <th>巡检间隔（秒） <code>inspection.interval_seconds</code></th>
            <td>
              <input
                className="input grow"
                type="number"
                min="300"
                step="300"
                value={v.interval_seconds ?? 21600}
                onChange={(e) => setInspectionField('interval_seconds', num(e.target.value))}
              />
              <div className="muted small">默认 21600 = 6 小时；下限 300（5 分钟）</div>
            </td>
          </tr>
          <tr>
            <th>自动修复 webhook <code>inspection.auto_repair</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={v.auto_repair !== false}
                onChange={(e) => setInspectionField('auto_repair', e.target.checked)}
              />
              <span className="muted small"> webhook 缺失 / secret 不匹配时自动重新注册（默认开）</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div className="form-row center">
        <button className="btn btn-primary" disabled={inspectionSaveBusy} onClick={saveInspection}>
          {inspectionSaveBusy ? '保存中…' : '保存'}
        </button>
        {inspectionSaved && <span className="alert-ok small"><Icon name="check" /> 已保存（已写回 config.yaml）</span>}
      </div>
    </div>
  )
}
