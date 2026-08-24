// 任务 token 用量卡片（issue #235）：展示引擎采集的模型调用 token 用量
// 与估算费用。usage 为 GET /api/tasks/{id} 返回的 usage 对象：
// - 无用量数据（usage 为 null）→ 显示「无数据」（而不是报错）；
// - 有数据 → 引擎 / 模型 / prompt / completion / total 与估算费用；
// - estimated_cost 为 null（未配置单价且引擎无自带费用）→ 只展示 token 数；
// - 缓存命中率（issue #473）：dsh 任务 usage.cache_hit_rate 非空时展示
//   「缓存命中率」行（百分比 + 命中/未命中明细）；claude/hermes 或缓存
//   未启用（cache_hit_rate 为 null）不展示该行，不报错。
// compact 模式（任务列表列）：单行 total + 费用摘要。
import { Icon } from './Icon.jsx'

export function fmtTokens(n) {
  if (n == null) return '—'
  return Number(n).toLocaleString('en-US')
}

export function fmtCost(cost, currency) {
  if (cost == null) return null
  const cur = (currency || 'USD').toUpperCase()
  return `${cur} ${Number(cost).toFixed(4)}`
}

export function fmtRate(rate) {
  // 缓存命中率显示：90 → "90%"，90.5 → "90.5%"，无数据 → null（不展示）
  if (rate == null || Number.isNaN(Number(rate))) return null
  return `${Number(rate)}%`
}

export function UsageSummary({ usage }) {
  // 列表/摘要形态：total tokens + 估算费用（无费用只显示 token 数）；
  // 有缓存命中率（dsh 任务）时 tooltip 附带缓存信息（issue #473）
  if (!usage) return null
  const cost = fmtCost(usage.estimated_cost, usage.currency)
  const rate = fmtRate(usage.cache_hit_rate)
  const cache = rate ? ` · 缓存 ${rate}` : ''
  return (
    <span className="usage-summary" title={
      `模型 ${usage.model || usage.engine || '—'} · ${fmtTokens(usage.prompt_tokens)} 输入 / ${fmtTokens(usage.completion_tokens)} 输出${cache}`
    }>
      {fmtTokens(usage.total_tokens)} tokens
      {cost && <span className="muted"> · {cost}</span>}
    </span>
  )
}

export default function UsageCard({ usage }) {
  if (!usage) {
    return (
      <div className="usage-card">
        <h3><Icon name="coins" /> Token 用量</h3>
        <p className="muted">无数据（该任务未采集到模型调用用量）</p>
      </div>
    )
  }
  const cost = fmtCost(usage.estimated_cost, usage.currency)
  const rate = fmtRate(usage.cache_hit_rate)
  const rows = [
    ['引擎', usage.engine || '—'],
    ['模型', usage.model || '—'],
    ['输入 tokens', fmtTokens(usage.prompt_tokens)],
    ['输出 tokens', fmtTokens(usage.completion_tokens)],
    ['总 tokens', fmtTokens(usage.total_tokens)],
  ]
  return (
    <div className="usage-card">
      <h3><Icon name="coins" /> Token 用量</h3>
      <table className="table kv">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}><th>{k}</th><td>{v}</td></tr>
          ))}
          {rate != null && (
            <tr>
              <th>缓存命中率</th>
              <td title={`命中 ${fmtTokens(usage.cache_hit_tokens)} tokens · 未命中 ${fmtTokens(usage.cache_miss_tokens)} tokens`}>
                <b>{rate}</b>
                <span className="muted">{`（命中 ${fmtTokens(usage.cache_hit_tokens)} · 未命中 ${fmtTokens(usage.cache_miss_tokens)} tokens）`}</span>
              </td>
            </tr>
          )}
          <tr>
            <th>估算费用</th>
            <td>
              {cost ? (
                <b>{cost}</b>
              ) : (
                <span className="muted" title="未配置模型单价，仅展示 token 数">
                  未估算（未配置单价）
                </span>
              )}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  )
}
