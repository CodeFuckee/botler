// ============================================================
// 键盘快捷键（issue #269）：全站快捷键绑定集中管理（keymap.js）
//
// 背景：平台操作依赖鼠标点击（新建 issue 点按钮、刷新点按钮、打开
// 任务详情点行），高频操作无快捷键效率低。本模块统一管理：
//   1. SHORTCUT_DEFS —— 全部快捷键定义表。帮助面板与按键分发共用
//      同一数据源：新增快捷键只需在此登记一条定义，页面侧注册同名
//      action 即生效，无需改动分发与帮助面板；
//   2. 启用开关 —— localStorage（键 botler.shortcuts）持久化，默认
//      开启；帮助面板 / 设置页可一键禁用（验收标准 3）。分发处理器
//      每次按键实时读取开关，改设置后无需刷新立即全局生效；
//   3. 防误触 —— 输入框 / 文本域 / 下拉 / 可编辑元素聚焦时不触发
//      （验收标准 1）；Esc 不拦截（交由 DialogHost 与各弹窗已有
//      处理）；ctrl/meta/alt 组合键留给浏览器与系统，不抢占；
//   4. 组合键 —— g o / g s 序列（先按 g 再按 o/s，COMBO_TIMEOUT_MS
//      内完成），前缀键本身不触发动作；组合失败降级按单键匹配。
// ============================================================
import { useEffect, useRef } from 'react'

/** 快捷键启用开关存储键（与主题 botler.theme / 语言 botler.lang 同模式） */
export const SHORTCUTS_STORAGE_KEY = 'botler.shortcuts'

/** 组合键序列超时（毫秒）：g 之后需在窗口内按下第二键，超时前缀失效 */
export const COMBO_TIMEOUT_MS = 2000

// 快捷键定义表：
//   id = 页面注册 action 的键名；keys = 按键（含空格的 'g o' 为组合
//   序列，先按前缀键再按第二键）；scope = 生效范围（global = 全站
//   任意页面 / overview / tasks 页面）；labelKey = 帮助面板展示文案
//   的 i18n 键。全部绑定集中在此，便于维护（验收标准 4）。
export const SHORTCUT_DEFS = [
  { id: 'new-issue', keys: 'n', scope: ['overview'], labelKey: 'shortcuts.newIssue' },
  { id: 'refresh', keys: 'r', scope: ['overview', 'tasks'], labelKey: 'shortcuts.refresh' },
  { id: 'focus-search', keys: '/', scope: ['tasks'], labelKey: 'shortcuts.focusSearch' },
  { id: 'go-tasks', keys: 't', scope: ['global'], labelKey: 'shortcuts.goTasks' },
  { id: 'go-overview', keys: 'g o', scope: ['global'], labelKey: 'shortcuts.goOverview' },
  { id: 'go-settings', keys: 'g s', scope: ['global'], labelKey: 'shortcuts.goSettings' },
]

/** 读取快捷键启用开关：未写入 = 默认开启；'0' = 关闭；其余 = 开启。
 *  storage：localStorage 兼容对象（测试可注入）；无存储环境（SSR）或
 *  getItem 抛异常（隐私模式）时兜底开启，不影响页面使用。 */
export function loadShortcutsEnabled(storage) {
  try {
    if (!storage) return true
    const raw = storage.getItem(SHORTCUTS_STORAGE_KEY)
    return raw === null || raw !== '0'
  } catch {
    return true
  }
}

/** 保存快捷键启用开关（'1' = 开启 / '0' = 关闭）；存储不可用时静默
 *  忽略，不抛错（与 loadLangPreference 同模式）。 */
export function saveShortcutsEnabled(storage, enabled) {
  try {
    storage?.setItem(SHORTCUTS_STORAGE_KEY, enabled ? '1' : '0')
  } catch {
    /* 无存储环境：静默忽略 */
  }
}

/** 聚焦元素（/ 快捷键聚焦搜索框用）：防御性调用 focus——元素为
 *  null（未挂载 / SSR / 测试渲染器）或无 focus 方法时静默跳过，不抛错。
 *  独立成纯函数便于单元测试（传入假元素断言调用关系）。 */
export function focusElement(el) {
  if (el && typeof el.focus === 'function') {
    el.focus()
    return true
  }
  return false
}

/** 防误触（验收标准 1）：事件目标是否可输入——输入框 / 文本域 / 下拉
 *  / 可编辑元素聚焦时按快捷键不应触发动作（如任务页搜索框里输入 n
 *  不能新建 issue）。 */
export function isTypingTarget(el) {
  if (!el) return false
  if (el.isContentEditable) return true
  const tag = String(el.tagName || '').toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select'
}

// ---- 快捷键匹配器：单键 + 组合序列（g o / g s）----
// 闭包维护组合前缀状态（pending 前缀键 + 超时定时器），匹配失败或
// 超时自动复位，避免前缀滞留污染后续按键。返回 { match, reset }。
export function createShortcutMatcher(defs) {
  const single = new Map() // 'n' → def（单键）
  const combos = new Map() // 'g' → [{ second: 'o', def }]（组合前缀）
  for (const d of defs || []) {
    const parts = String(d.keys || '')
      .split(' ').map((s) => s.trim().toLowerCase()).filter(Boolean)
    if (parts.length === 1) {
      single.set(parts[0], d)
    } else if (parts.length === 2) {
      const first = parts[0]
      if (!combos.has(first)) combos.set(first, [])
      combos.get(first).push({ second: parts[1], def: d })
    }
    // 超过两段的组合键定义不支持：忽略（当前快捷键集无此场景）
  }
  let pending = null // 组合前缀键（如 'g'），等待第二键
  let timer = null
  const reset = () => {
    pending = null
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }
  const match = (key) => {
    const k = String(key || '').toLowerCase()
    if (!k) return null
    if (pending) {
      const cands = combos.get(pending) || []
      const hit = cands.find((c) => c.second === k)
      reset()
      if (hit) return hit.def.id
      // 第二键不匹配组合：降级按单键继续匹配（如 g 后按 r 仍触发刷新）
    }
    if (combos.has(k)) {
      pending = k
      timer = setTimeout(reset, COMBO_TIMEOUT_MS)
      return null // 前缀键本身不触发动作，等待第二键
    }
    const d = single.get(k)
    return d ? d.id : null
  }
  return { match, reset }
}

// ---- 全局 keydown 处理器：命中快捷键时执行对应 action ----
// defs：快捷键定义表（默认 SHORTCUT_DEFS）；getActions：读取当前生效
// 动作表的函数（页面动作随渲染更新，用 getter 保证分发时拿到最新
// 引用，避免闭包捕获首次渲染的过期动作）；actions：静态动作表（与
// getActions 二选一，纯逻辑/测试直接用）；getEnabled：启用开关读取
// 函数（默认走 localStorage）。返回可直接挂到 document 的 keydown
// 监听函数。
export function createKeydownHandler({
  defs = SHORTCUT_DEFS,
  actions = {},
  getActions,
  getEnabled,
} = {}) {
  const matcher = createShortcutMatcher(defs)
  return function onKeydown(e) {
    if (!e || !e.key) return
    if (getEnabled && !getEnabled()) return // 开关关闭：全部快捷键失效
    if (isTypingTarget(e.target)) return // 输入框聚焦：防误触
    if (e.metaKey || e.ctrlKey || e.altKey) return // 系统/浏览器组合键不抢占
    if (e.repeat) return // 长按不重复触发
    if (e.key === 'Escape') return // Esc 已有 DialogHost/弹窗处理
    const id = matcher.match(e.key)
    // 分发时经 getter 取最新动作表（useShortcuts 每次渲染更新 ref，
    // 动作闭包内的页面状态始终是当前渲染值）
    const current = typeof getActions === 'function' ? getActions() : actions
    if (id && typeof current[id] === 'function') {
      e.preventDefault()
      current[id]()
    }
  }
}

// ---- React hook：页面注册快捷键动作，卸载时自动移除监听 ----
// actions：{ id: fn }，每次渲染的最新引用经 ref 透传给监听器（页面
// 内联定义动作对象也不会重复绑定/解绑）；storage：localStorage 兼容
// 对象；getEnabled：可选覆盖开关读取函数。无 document 环境（SSR /
// 单组件测试）静默跳过，不抛错（与 DialogHost Esc 监听同模式）。
export function useShortcuts(actions, { storage, getEnabled } = {}) {
  const actionsRef = useRef(actions)
  actionsRef.current = actions
  const getEnabledRef = useRef(getEnabled)
  getEnabledRef.current = getEnabled
  useEffect(() => {
    if (typeof document === 'undefined' || typeof document.addEventListener !== 'function') return
    const onKeydown = createKeydownHandler({
      getActions: () => actionsRef.current,
      getEnabled: () => (getEnabledRef.current
        ? getEnabledRef.current()
        : loadShortcutsEnabled(storage)),
    })
    document.addEventListener('keydown', onKeydown)
    return () => document.removeEventListener('keydown', onKeydown)
  }, [storage])
}
