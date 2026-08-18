// 概览页 DeepSeek 余额卡片「每小时余额变化速率」（issue #304）：
// 如果有账户余额信息时，概览增加账户余额减少（变化）速度显示，按小时
// 为单位。纯前端实现：每次余额轮询 / 手动刷新成功时，把观测样本
// {ts, infos:[{currency, total_balance}]} 追加到 localStorage 历史
// （键 botler.overview.dsBalanceHistory），按最早/最近观测窗口计算每小时
// 平均变化速率（余额减少为负、增加为正、无变化为 0，增加通常是充值/
// 赠送到账）。样本容量与有效期有界；存储不可用（SSR/隐私模式）或数据
// 损坏时全部兜底回退，不影响页面使用（与概览页其他 localStorage 偏好
// 同策略）。

// 历史样本存储键（与 botler.lang / botler.overview.issueFilter 同命名约定）
export const BALANCE_RATE_STORAGE_KEY = 'botler.overview.dsBalanceHistory'

// 历史样本容量上限：60 秒轮询约可覆盖 3.3 小时（跨会话样本按 7 天
// 有效期保留，突破容量时只留最近样本）
export const MAX_BALANCE_SAMPLES = 200

// 样本有效期：超过 7 天的样本视为过期丢弃（跨周数据对「每小时速率」
// 无参考价值，且避免陈旧基线拉偏速率）
export const MAX_SAMPLE_AGE_MS = 7 * 24 * 60 * 60 * 1000

// 最短观测窗口：1 分钟——窗口太短（如两次连续轮询间隔）时余额差极
// 小、噪声大，不计速率（界面展示「暂无速率数据」）
export const MIN_RATE_WINDOW_MS = 60 * 1000

// 每小时的毫秒数（速率按小时为单位的换算系数）
export const MS_PER_HOUR = 60 * 60 * 1000

// 单条样本清洗：仅保留合法样本 {ts, infos}，infos 元素只保留
// {currency, total_balance}（币种非空、总余额可数值化）；非法样本返回
// null。total_balance 统一数值化（DeepSeek 接口返回字符串，如 "110.00"）
export function normalizeBalanceSample(s) {
  if (!s || typeof s !== 'object') return null
  const ts = Number(s.ts)
  if (!Number.isFinite(ts) || !Array.isArray(s.infos)) return null
  const infos = []
  for (const info of s.infos) {
    if (!info || typeof info !== 'object') continue
    const currency = String(info.currency || '').trim()
    // 余额缺失（null/undefined/空串）不参与速率计算：Number(null)=0、
    // Number('')=0 会误当成「余额为 0」，须显式排除
    if (info.total_balance == null) continue
    const total = Number(info.total_balance)
    if (!currency || !Number.isFinite(total)) continue
    infos.push({ currency, total_balance: total })
  }
  // 样本里没有任何可用余额信息时视为无效样本（丢弃）
  if (infos.length === 0) return null
  return { ts, infos }
}

// 追加一条观测样本：balanceInfos 为余额接口 balance_infos（原始结构），
// 只保留币种与总余额（数值化）；时间戳非法时回退当前时间；历史非数组
// （旧版本/损坏数据）视为空历史；返回新数组（不改动入参）
export function appendBalanceSample(history, now, balanceInfos) {
  const infos = []
  for (const info of Array.isArray(balanceInfos) ? balanceInfos : []) {
    if (!info || typeof info !== 'object') continue
    const currency = String(info.currency || '').trim()
    // 余额缺失（null/undefined/空串）不参与速率计算（Number(null)=0 会
    // 误当成「余额为 0」）
    if (info.total_balance == null) continue
    const total = Number(info.total_balance)
    if (!currency || !Number.isFinite(total)) continue
    infos.push({ currency, total_balance: total })
  }
  const ts = Number(now)
  const samples = (Array.isArray(history) ? history : [])
    .map(normalizeBalanceSample)
    .filter(Boolean)
  const next = [...samples, { ts: Number.isFinite(ts) ? ts : Date.now(), infos }]
  if (next.length > MAX_BALANCE_SAMPLES) {
    return next.slice(next.length - MAX_BALANCE_SAMPLES)
  }
  return next
}

// 读取历史样本：非法 JSON / 非数组 / 非法样本逐条清洗，丢弃超过
// MAX_SAMPLE_AGE_MS 的过期样本，截断到 MAX_BALANCE_SAMPLES；无存储
// 环境（SSR）或 getItem 抛异常（隐私模式）时返回空数组，不抛错
export function loadBalanceHistory(storage, now = Date.now()) {
  try {
    if (!storage || typeof storage.getItem !== 'function') return []
    const raw = storage.getItem(BALANCE_RATE_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    const nowNum = Number(now)
    const deadline = (Number.isFinite(nowNum) ? nowNum : Date.now()) - MAX_SAMPLE_AGE_MS
    let out = parsed
      .map(normalizeBalanceSample)
      .filter(Boolean)
      .filter((s) => s.ts >= deadline)
    if (out.length > MAX_BALANCE_SAMPLES) {
      out = out.slice(out.length - MAX_BALANCE_SAMPLES)
    }
    return out
  } catch {
    return []
  }
}

// 保存历史样本：只写清洗后的合法样本（截断容量）；存储不可用或 setItem
// 抛异常（隐私模式/配额）时静默忽略，不影响页面使用
export function saveBalanceHistory(storage, history) {
  try {
    if (!storage || typeof storage.setItem !== 'function') return
    const out = (Array.isArray(history) ? history : [])
      .map(normalizeBalanceSample)
      .filter(Boolean)
      .slice(-MAX_BALANCE_SAMPLES)
    storage.setItem(BALANCE_RATE_STORAGE_KEY, JSON.stringify(out))
  } catch {
    /* 无存储环境：静默忽略 */
  }
}

// 计算每小时余额变化速率（按币种）：
// 对每个币种取最早与最近两条含该币种的观测样本，速率 =
// (最近总余额 - 最早总余额) × 每小时毫秒数 ÷ 观测窗口毫秒数；
// 窗口小于 MIN_RATE_WINDOW_MS 或样本不足时不计该币种（返回对象不含
// 该键）。返回 { [currency]: { ratePerHour, windowMs } }，ratePerHour
// 负数=减少、正数=增加（充值/赠送到账）、0=无变化
export function computeBalanceRate(history) {
  const samples = (Array.isArray(history) ? history : [])
    .map(normalizeBalanceSample)
    .filter(Boolean)
    .sort((a, b) => a.ts - b.ts)
  if (samples.length < 2) return {}
  const currencies = new Set()
  for (const s of samples) {
    for (const i of s.infos) currencies.add(i.currency)
  }
  const rate = {}
  for (const currency of currencies) {
    let earliest = null
    let latest = null
    for (const s of samples) {
      const hit = s.infos.find((i) => i.currency === currency)
      if (!hit) continue
      // 记录样本时间戳与余额（info 本身不含 ts，须从样本上取）
      const point = { ts: s.ts, total_balance: hit.total_balance }
      if (earliest === null) earliest = point
      latest = point
    }
    if (!earliest || !latest) continue
    const windowMs = latest.ts - earliest.ts
    if (windowMs < MIN_RATE_WINDOW_MS) continue
    rate[currency] = {
      ratePerHour: (latest.total_balance - earliest.total_balance)
        * MS_PER_HOUR / windowMs,
      windowMs,
    }
  }
  return rate
}

// 观测窗口人话化：≥1 小时返回 {kind:'hour', value}（保留 1 位小数），
// 不足 1 小时返回 {kind:'minute', value}（整分钟，至少 1 分钟）。
// 界面按 kind 选 i18n 单位文案（overview.rateWindowHour/Minute）
export function fmtRateWindow(ms) {
  const hours = Number(ms) / MS_PER_HOUR
  if (hours >= 1) {
    return { kind: 'hour', value: Math.round(hours * 10) / 10 }
  }
  const minutes = Math.max(1, Math.round(Number(ms) / 60000))
  return { kind: 'minute', value: minutes }
}
