// 概览页 issue 详情右边栏「标记活动」显示（issue #349）：
// 展示谁在什么时间添加/移除了哪个标记（数据源 GitLab
// resource_label_events API——实测 notes 系统活动不含标记加/删事件，
// 后端 detail 接口独立拉取精简后随 label_events 字段返回）。

/** 标记事件动作 → 展示文案（issue #349）：add=添加、remove=移除；
 *  未知动作（后端透传的异常值）兜底「变更了标记」，不因坏数据崩溃。 */
export const LABEL_ACTION_META = {
  add: { text: '添加了标记' },
  remove: { text: '移除了标记' },
}

/** 事件操作人显示名：user.name 优先回退 username，全无显示「—」
 *  （与 noteAuthorName 兜底逻辑一致）。 */
export function labelActorName(event) {
  const u = event && typeof event === 'object' ? event.user : null
  if (!u || typeof u !== 'object') return '—'
  return u.name || u.username || '—'
}

/** 标记事件展示文本（issue #349）：`{操作人} {动作} {标记名}`，
 *  如「chenkaidi 添加了标记 feature」；标记名缺失显示「标记」兜底。 */
export function labelEventText(event) {
  const actor = labelActorName(event)
  const action = (event && typeof event === 'object'
    && LABEL_ACTION_META[event.action])
    ? LABEL_ACTION_META[event.action].text : '变更了标记'
  const name = (event && typeof event === 'object'
    && typeof event.label === 'string' && event.label.trim())
    ? event.label : '标记'
  return `${actor} ${action} ${name}`
}

/** 归一化标记事件列表（issue #349）：防御性过滤异常元素（非对象/
 *  缺 id），按 created_at 升序、同一时刻按事件 id 升序（与
 *  buildTimeline 同排序规则）；缺 created_at 按空串排最前，不因
 *  坏数据崩溃。 */
export function buildLabelEvents(events) {
  const items = (events || [])
    .filter((e) => e && typeof e === 'object' && typeof e.id === 'number')
    .map((e) => ({ ...e }))
  items.sort((a, b) => {
    const ta = typeof a.created_at === 'string' ? a.created_at : ''
    const tb = typeof b.created_at === 'string' ? b.created_at : ''
    if (ta < tb) return -1
    if (ta > tb) return 1
    return a.id - b.id
  })
  return items
}
