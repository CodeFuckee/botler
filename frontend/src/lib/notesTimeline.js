// 概览页 issue 详情右边栏「评论与活动」合并时间线显示（issue #342）：
// 设置页「界面显示」卡片新增开关（localStorage 键 botler.timeline，与
// 快捷键开关 botler.shortcuts / 语言 botler.lang 同模式），开启后评论
// （用户发言）与活动（系统事件）不再分两个区块，而是按时间交错合并为
// 一条时间线（类似 GitLab issue 时间线）；未配置默认关闭，保持 issue
// #97 的分开显示现状。

/** 合并时间线开关存储键（与主题 botler.theme / 语言 botler.lang 同模式） */
export const TIMELINE_STORAGE_KEY = 'botler.timeline'

/** 读取合并时间线开关：仅显式写入 '1' 视为开启，其余（未写入/写 0/
 *  非法值）一律为关闭——默认分开显示，不改变存量用户观感。
 *  storage：localStorage 兼容对象（测试可注入）；无存储环境（SSR）或
 *  getItem 抛异常（隐私模式）时兜底关闭，不影响页面使用。 */
export function loadTimelineEnabled(storage) {
  try {
    if (!storage) return false
    return storage.getItem(TIMELINE_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

/** 保存合并时间线开关（'1' = 开启 / '0' = 关闭）；存储不可用时静默
 *  忽略，不抛错（与 saveShortcutsEnabled 同模式）。 */
export function saveTimelineEnabled(storage, enabled) {
  try {
    storage?.setItem(TIMELINE_STORAGE_KEY, enabled ? '1' : '0')
  } catch {
    /* 无存储环境：静默忽略 */
  }
}

/** 合并评论与活动为时间线（issue #342）：把 detail 接口返回的 notes
 *  （system=false 评论 / system=true 活动）按 created_at 升序交错为一条
 *  时间线（与后端 notes 升序拉取一致）；同一时刻按 note id 升序（GitLab
 *  note id 单调递增，保证同秒内顺序稳定）；异常元素（非对象/缺 id）防御
 *  性跳过；缺 created_at 按空串参与排序（排最前），不因坏数据崩溃。 */
export function buildTimeline(notes) {
  const items = (notes || [])
    .filter((n) => n && typeof n === 'object' && typeof n.id === 'number')
    .map((n) => ({ ...n }))
  items.sort((a, b) => {
    const ta = typeof a.created_at === 'string' ? a.created_at : ''
    const tb = typeof b.created_at === 'string' ? b.created_at : ''
    if (ta < tb) return -1
    if (ta > tb) return 1
    return a.id - b.id
  })
  return items
}

/** 合并时间线（含标记活动，issue #351）：把 detail 接口返回的 notes
 *  （system=false 评论 / system=true 活动）与 label_events（标记活动——
 *  谁添加/移除了哪个标记）按 created_at 升序交错为一条时间线（排序规则
 *  与 buildTimeline 完全一致：同一时刻按 id 升序、异常元素防御性跳过、
 *  缺 created_at 按空串排最前）；每条统一标注 _kind（comment / activity
 *  / label）供渲染方区分条目类型，另标注唯一 _key——GitLab notes 与
 *  resource_label_events 的 id 是不同自增序列，跨序列可能相同，渲染 key
 *  必须带来源前缀（note-/label-）保证唯一，避免 React 节点复用错乱；
 *  不因坏数据崩溃。 */
export function buildMergedTimeline(notes, labelEvents) {
  const items = []
  for (const n of (notes || [])) {
    if (n && typeof n === 'object' && typeof n.id === 'number') {
      items.push({ ...n, _kind: n.system ? 'activity' : 'comment',
                   _key: `note-${n.id}` })
    }
  }
  for (const e of (labelEvents || [])) {
    if (e && typeof e === 'object' && typeof e.id === 'number') {
      items.push({ ...e, _kind: 'label', _key: `label-${e.id}` })
    }
  }
  items.sort((a, b) => {
    const ta = typeof a.created_at === 'string' ? a.created_at : ''
    const tb = typeof b.created_at === 'string' ? b.created_at : ''
    if (ta < tb) return -1
    if (ta > tb) return 1
    return a.id - b.id
  })
  return items
}
