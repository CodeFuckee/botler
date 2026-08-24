// 右侧边栏（抽屉）拖拽调整宽度 hook（issue #466）：
// 概览页 issue 详情 / CI/CD 流水线 / 灵感 AI 对话 / 任务执行详情四个右边栏，在视口宽度
// 足够（>860px，跟随项目移动断点）的情况下，用户可拖动抽屉左缘改变宽度。
//
// 设计：
// - 「宽度足够」= 视口宽度 > 860px（与项目既有 860px 移动断点约定一致），
//   窄视口不渲染手柄（抽屉已受 .drawer max-width: 92vw 限制）；
// - 宽度范围：最小 320px，最大 floor(92vw)（与 .drawer max-width: 92vw 对齐）；
// - 拖拽：Pointer Events（pointerdown → window pointermove/pointerup），
//   拖动中锁定 body 文本选择并保持 col-resize 光标（指针可能移出手柄）；
// - 持久化：拖拽/键盘调整结束后写入 localStorage（key 按抽屉类型区分，
//   刷新/重开保持，与概览页过滤/排序等偏好持久化约定一致）；
// - 键盘可达性：手柄 role="separator"、可聚焦，ArrowLeft/ArrowRight 步进
//   16px 调整（与拖拽共用 clamp 与持久化逻辑）。
//
// 纯函数（导出便于单测，SSR/无 window 环境安全回退）。
import { useCallback, useEffect, useRef, useState } from 'react'

// 视口宽度阈值：>860px 才允许调整（与 styles.css 移动断点 860px 一致）
export const DRAWER_RESIZE_MIN_VIEWPORT = 861
// 最小宽度：内容可用的下限
export const DRAWER_RESIZE_MIN_WIDTH = 320
// 最大宽度系数：floor(92vw)，与 .drawer { max-width: 92vw } 对齐
export const DRAWER_RESIZE_MAX_VW = 0.92
// 键盘步进（px）
export const DRAWER_RESIZE_STEP = 16

// localStorage 存储 key：按抽屉类型区分（概览页偏好统一 botler.overview.* 前缀）
export const ISSUE_DRAWER_WIDTH_KEY = 'botler.overview.drawerWidth.issue'
export const PIPELINE_DRAWER_WIDTH_KEY = 'botler.overview.drawerWidth.pipeline'
export const CHAT_DRAWER_WIDTH_KEY = 'botler.overview.drawerWidth.chat'
export const TASK_DETAIL_DRAWER_WIDTH_KEY = 'botler.overview.drawerWidth.taskDetail'

/** 视口宽度是否足够支持拖拽调整（>860px）；空值/非法输入回退 false（SSR 安全）。 */
export function canResizeDrawer(viewportWidth) {
  return typeof viewportWidth === 'number' && Number.isFinite(viewportWidth) &&
    viewportWidth >= DRAWER_RESIZE_MIN_VIEWPORT
}

/** 视口下允许的最大宽度：floor(92vw)，且不低于最小宽度（窄视口保底）。 */
export function drawerMaxWidth(viewportWidth) {
  const vw = typeof viewportWidth === 'number' && Number.isFinite(viewportWidth)
    ? viewportWidth : 0
  return Math.max(DRAWER_RESIZE_MIN_WIDTH, Math.floor(vw * DRAWER_RESIZE_MAX_VW))
}

/** 宽度钳制到 [320, floor(92vw)]；非法输入回退最小宽度。 */
export function clampDrawerWidth(width, viewportWidth) {
  const w = typeof width === 'number' && Number.isFinite(width)
    ? width : DRAWER_RESIZE_MIN_WIDTH
  return Math.min(Math.max(w, DRAWER_RESIZE_MIN_WIDTH), drawerMaxWidth(viewportWidth))
}

/** 解析 localStorage 存储宽度：非法 JSON/非数字回退 null；越界钳制。 */
export function parseStoredDrawerWidth(raw, viewportWidth) {
  if (!raw) return null
  let n = null
  try {
    n = JSON.parse(raw)
  } catch (_) {
    return null
  }
  return typeof n === 'number' && Number.isFinite(n)
    ? clampDrawerWidth(n, viewportWidth) : null
}

/**
 * useDrawerResize(options)
 * @param {string}  [options.storageKey] localStorage 持久化 key（按抽屉区分）
 * @param {object}  [options.drawerRef]  抽屉根元素 ref（键盘/拖拽起始宽度读取）
 * @returns {{ width: number|null, resizable: boolean, dragging: boolean,
 *             handleProps: object }}
 *   - width：当前内联宽度（null = 未调整，走 CSS 默认宽度）
 *   - resizable：视口宽度是否足够（决定是否渲染手柄）
 *   - handleProps：拖拽手柄元素属性（onPointerDown/onKeyDown/aria/tabIndex）
 */
export function useDrawerResize({ storageKey, drawerRef } = {}) {
  // 初始宽度：优先读 localStorage（钳制后），无存储则 null（走 CSS 默认）
  const [width, setWidth] = useState(() => {
    if (!storageKey || typeof window === 'undefined' ||
        typeof window.localStorage === 'undefined') return null
    return parseStoredDrawerWidth(
      window.localStorage.getItem(storageKey), window.innerWidth)
  })
  // 视口宽度：决定是否可调（窄视口不渲染手柄）
  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window !== 'undefined' && typeof window.innerWidth === 'number'
      ? window.innerWidth : 0)
  const [dragging, setDragging] = useState(false)
  // 拖拽会话数据：{ startX, startWidth, width }
  const dragRef = useRef(null)
  // 视口宽度是否足够（>860px）——决定是否渲染手柄（需在下方 useCallback
  // 依赖数组求值前初始化，避免 TDZ）
  const resizable = canResizeDrawer(viewportWidth)

  // 窗口尺寸变化时刷新视口宽度（横竖屏/窗口缩放即时开关手柄）
  useEffect(() => {
    if (typeof window === 'undefined' ||
        typeof window.addEventListener !== 'function') return undefined
    const onResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // 持久化宽度（隐私模式/配额满写入失败静默，不影响拖拽）
  const persist = useCallback((w) => {
    if (!storageKey || typeof window === 'undefined' ||
        typeof window.localStorage === 'undefined') return
    try {
      window.localStorage.setItem(storageKey, String(w))
    } catch (_) { /* 静默：持久化失败不阻断本次调整 */ }
  }, [storageKey])

  // 拖拽会话：dragging 置真后挂 window 级 pointermove/pointerup/pointercancel，
  // 清理时恢复 body 文本选择与光标（指针拖出抽屉外仍持续跟随）
  useEffect(() => {
    if (!dragging) return undefined
    if (typeof window === 'undefined' ||
        typeof window.addEventListener !== 'function') return undefined
    const onMove = (e) => {
      const d = dragRef.current
      if (!d) return
      const vw = typeof window.innerWidth === 'number'
        ? window.innerWidth : viewportWidth
      const w = clampDrawerWidth(d.startWidth + (d.startX - e.clientX), vw)
      d.width = w
      setWidth(w)
    }
    const onUp = () => {
      const d = dragRef.current
      if (d && typeof d.width === 'number') persist(d.width)
      dragRef.current = null
      setDragging(false)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
    const body = typeof document !== 'undefined' ? document.body : null
    const prevUserSelect = body ? body.style.userSelect : null
    const prevCursor = body ? body.style.cursor : null
    if (body) {
      body.style.userSelect = 'none'
      body.style.cursor = 'col-resize'
    }
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
      if (body) {
        body.style.userSelect = prevUserSelect
        body.style.cursor = prevCursor
      }
    }
  }, [dragging, viewportWidth, persist])

  // 手柄按下：记录起始位置与起始宽度，进入拖拽会话
  const onPointerDown = useCallback((e) => {
    if (!resizable || !e) return
    if (typeof e.preventDefault === 'function') e.preventDefault()
    const el = drawerRef && drawerRef.current
    const rendered = el && typeof el.offsetWidth === 'number' && el.offsetWidth > 0
      ? el.offsetWidth : DRAWER_RESIZE_MIN_WIDTH
    const start = typeof width === 'number' ? width : rendered
    dragRef.current = {
      startX: typeof e.clientX === 'number' ? e.clientX : 0,
      startWidth: start,
      width: start,
    }
    setDragging(true)
  }, [resizable, width, drawerRef])

  // 键盘调整：ArrowLeft/ArrowRight 步进 16px（复用 clamp 与持久化）
  const onKeyDown = useCallback((e) => {
    if (!resizable || !e) return
    const el = drawerRef && drawerRef.current
    const rendered = el && typeof el.offsetWidth === 'number' && el.offsetWidth > 0
      ? el.offsetWidth : DRAWER_RESIZE_MIN_WIDTH
    const cur = typeof width === 'number' ? width : rendered
    let next = null
    if (e.key === 'ArrowLeft') {
      next = clampDrawerWidth(cur - DRAWER_RESIZE_STEP, viewportWidth)
    } else if (e.key === 'ArrowRight') {
      next = clampDrawerWidth(cur + DRAWER_RESIZE_STEP, viewportWidth)
    }
    if (next == null) return
    if (typeof e.preventDefault === 'function') e.preventDefault()
    setWidth(next)
    persist(next)
  }, [resizable, width, viewportWidth, persist, drawerRef])

  return {
    width,
    resizable,
    dragging,
    handleProps: {
      className: 'drawer-resize-handle' + (dragging ? ' dragging' : ''),
      onPointerDown,
      onKeyDown,
      role: 'separator',
      'aria-orientation': 'vertical',
      'aria-label': '拖动调整右侧边栏宽度',
      'aria-valuenow': typeof width === 'number' ? width : DRAWER_RESIZE_MIN_WIDTH,
      'aria-valuemin': DRAWER_RESIZE_MIN_WIDTH,
      'aria-valuemax': drawerMaxWidth(viewportWidth),
      tabIndex: 0,
    },
  }
}
