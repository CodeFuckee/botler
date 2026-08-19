// 搜索结果关键词高亮（issue #216）：把文本按关键词（大小写不敏感）
// 切分为 {text, hit} 片段数组，渲染时命中片段包 <mark>。
//
// 纯函数便于单元测试；关键词为空 / 文本为空时原样返回单个非命中
// 片段（渲染层不包 <mark>，行为与无高亮一致）。命中片段按首次出现
// 逐个切出，未命中部分保留原文（含大小写原文，不改变展示内容）。
export function splitKeyword(text, keyword) {
  const s = String(text ?? '')
  const k = String(keyword ?? '')
  if (!s || !k) return [{ text: s, hit: false }]
  const lower = s.toLowerCase()
  const kl = k.toLowerCase()
  const out = []
  let i = 0
  let idx = lower.indexOf(kl, i)
  while (idx !== -1) {
    if (idx > i) out.push({ text: s.slice(i, idx), hit: false })
    out.push({ text: s.slice(idx, idx + k.length), hit: true })
    i = idx + k.length
    idx = lower.indexOf(kl, i)
  }
  if (i < s.length) out.push({ text: s.slice(i), hit: false })
  return out
}
