// 回到顶部按钮（issue #455）：所有需要竖向滚动的页面右下角提供
// 「回到顶部」浮动按钮。设计要点：
// 1. 全局挂载：App.jsx 根部渲染一次，全站页面自动生效（无需每页接入）；
// 2. 显示条件：页面可竖向滚动（内容高度 > 视口高度）且当前滚动位置
//    超过阈值 BACK_TO_TOP_THRESHOLD（400px，约两屏）——内容不足一屏
//    的页面或页面顶部附近永不显示，避免遮挡与闪烁；
// 3. 滚动容器：本应用统一由窗口（body）竖向滚动（侧边栏 sticky 悬浮），
//    监听 window scroll（passive）+ resize，路由切换（react-router
//    location.key 变化）时重新评估——新页面内容高度/滚动位置可能变化
//    （如长页切到不足一屏的页面时浏览器钳制滚动位置且不触发 scroll）；
// 4. 点击平滑回顶：window.scrollTo({top:0, behavior})；系统开启
//    「减弱动态效果」时改用 auto（instant），尊重用户无障碍偏好
//    （scrollBehaviorFor 纯函数，测试可注入）；
// 5. 无障碍：aria-label/title 经 i18n（common.backToTop，issue #268），
//    Tab 可聚焦、focus-visible 焦点环；不可见时整颗按钮不渲染
//    （不会残留隐藏的可聚焦元素）；
// 6. 深色模式：样式全部走设计令牌（--bg-card/--border/--shadow-card），
//    深浅色自动适配（issue #217）；
// 7. 与版本更新横幅共存：App 在横幅显示时传 raised=true，按钮上移
//    避免遮挡右下角横幅（横幅 z-index 100 > 按钮 90，横幅优先）。
import { useEffect, useState } from 'react'
import { useInRouterContext, useLocation } from 'react-router-dom'
import { Icon } from './Icon.jsx'
import { useI18n } from '../i18n.jsx'

/** 显示阈值：滚动超过 400px（约两屏）后按钮出现 */
export const BACK_TO_TOP_THRESHOLD = 400

/** 是否显示按钮：滚动位置超过阈值（非法/非数值输入一律 false） */
export function shouldShowBackToTop(scrollY, threshold = BACK_TO_TOP_THRESHOLD) {
  return typeof scrollY === 'number' && Number.isFinite(scrollY) && scrollY > threshold
}

/** 页面是否可竖向滚动：内容高度大于视口高度（非法输入一律 false） */
export function canScrollVertically(scrollHeight, clientHeight) {
  return (
    typeof scrollHeight === 'number' && Number.isFinite(scrollHeight)
    && typeof clientHeight === 'number' && Number.isFinite(clientHeight)
    && scrollHeight > clientHeight
  )
}

/** 滚动行为：开启「减弱动态效果」时用 auto（instant），否则平滑滚动 */
export function scrollBehaviorFor(reducedMotion) {
  return reducedMotion ? 'auto' : 'smooth'
}

/** 是否开启「减弱动态效果」（无 window/matchMedia 环境按未开启处理） */
export function prefersReducedMotion() {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
    : false
}

/** 读取当前滚动位置（SSR/测试无 window 时按 0 处理，按钮不显示） */
export function currentScrollY() {
  return typeof window !== 'undefined' ? (window.scrollY || 0) : 0
}

/** 读取文档可滚动高度（SSR/测试无 document 时按 0 处理） */
export function currentScrollHeight() {
  if (typeof document === 'undefined') return 0
  const el = document.scrollingElement || document.documentElement
  return el ? el.scrollHeight : 0
}

/** 当前视口高度（SSR/测试无 window 时按 0 处理） */
export function currentViewportHeight() {
  return typeof window !== 'undefined' ? (window.innerHeight || 0) : 0
}

/** 可见性状态钩子：滚动/窗口尺寸变化时重算；routeKey 变化（路由切换）
 *  时重跑 effect——新页面内容高度与滚动位置可能变化 */
function useBackToTopVisibility(routeKey) {
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    if (typeof window === 'undefined') return
    const evaluate = () => {
      setVisible(
        canScrollVertically(currentScrollHeight(), currentViewportHeight())
        && shouldShowBackToTop(currentScrollY()),
      )
    }
    evaluate()
    window.addEventListener('scroll', evaluate, { passive: true })
    window.addEventListener('resize', evaluate, { passive: true })
    return () => {
      window.removeEventListener('scroll', evaluate)
      window.removeEventListener('resize', evaluate)
    }
  }, [routeKey])
  return visible
}

/** 按钮本体（无 Router 依赖，SSR/单组件测试安全；routeKey 为空则不做
 *  路由追踪，仅依赖滚动/尺寸事件） */
function BackToTopButton({ raised, routeKey }) {
  const { t } = useI18n()
  const visible = useBackToTopVisibility(routeKey)
  const scrollToTop = () => {
    if (typeof window === 'undefined') return
    window.scrollTo({ top: 0, left: 0, behavior: scrollBehaviorFor(prefersReducedMotion()) })
  }
  if (!visible) return null
  const label = t('common.backToTop')
  return (
    <button
      type="button"
      className={'back-to-top' + (raised ? ' raised' : '')}
      onClick={scrollToTop}
      aria-label={label}
      title={label}
    >
      <Icon name="arrowUp" aria-hidden="true" />
    </button>
  )
}

/** 路由环境包装（React Hooks 规则：useLocation 必须在组件顶层无条件调用，
 *  故拆成独立组件——有 Router 时经它取 location 并驱动路由切换重评估） */
function BackToTopWithRouter({ raised }) {
  const location = useLocation()
  return <BackToTopButton raised={raised} routeKey={location.key} />
}

export default function BackToTop({ raised = false }) {
  const inRouter = useInRouterContext()
  return inRouter
    ? <BackToTopWithRouter raised={raised} />
    : <BackToTopButton raised={raised} routeKey={null} />
}

/* ==================== 容器滚动版（issue #457） ====================
 * 面向**自身竖向滚动**的容器（右侧边栏抽屉 .drawer 系列：issue 详情 /
 * 流水线详情 / 任务执行详情 / 灵感 AI 对话 / 任务详情快览）——这些
 * 容器 overflow-y: auto 内部滚动，全局版按钮监听 window scroll 不生效，
 * 需要按钮滚动**容器自身**。
 * 设计要点：
 * 1. 挂载：由各抽屉组件在滚动容器（.drawer）内渲染，传入容器 ref；
 * 2. 显示条件与全局版一致：容器可竖向滚动（内容高 > 可视高）且滚动
 *    位置超过 BACK_TO_TOP_THRESHOLD（400px）——复用 canScrollVertically /
 *    shouldShowBackToTop 纯函数；
 * 3. 事件：容器 scroll（passive）+ window resize + ResizeObserver
 *    （容器尺寸变化重估——抽屉内数据异步加载/转屏时及时更新）；
 * 4. 点击：滚动**容器自身**到顶部（scrollTo top:0），系统开启「减弱
 *    动态效果」时改 auto（尊重无障碍偏好，复用 scrollBehaviorFor）；
 * 5. 无障碍与样式：与全局版一致（i18n aria-label/title、Tab 可聚焦、
 *    focus-visible 焦点环、design token 配色）；定位用 .back-to-top
 *    .in-drawer（absolute 相对 .drawer 右下角，见 styles.css）。
 * 6. containerRef 为空（容器未挂载/SSR）时安全跳过，不渲染不抛错。 */

/** 读取容器当前滚动位置（无容器按 0，按钮不显示） */
export function containerScrollTop(el) {
  return el ? el.scrollTop : 0
}

/** 读取容器可滚动高度（无容器按 0，视为不可滚动） */
export function containerScrollHeight(el) {
  return el ? el.scrollHeight : 0
}

/** 读取容器可视高度（无容器按 0，视为不可滚动） */
export function containerClientHeight(el) {
  return el ? el.clientHeight : 0
}

/** 容器滚动可见性钩子：监听容器 scroll / window resize / ResizeObserver，
 *  任一变化时按「可竖向滚动 && 滚动超阈值」重算显隐 */
function useContainerBackToTopVisibility(containerRef) {
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const el = containerRef && containerRef.current
    if (!el) return undefined
    const evaluate = () => {
      setVisible(
        canScrollVertically(containerScrollHeight(el), containerClientHeight(el))
        && shouldShowBackToTop(containerScrollTop(el)),
      )
    }
    evaluate()
    el.addEventListener('scroll', evaluate, { passive: true })
    window.addEventListener('resize', evaluate, { passive: true })
    let observer = null
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(evaluate)
      observer.observe(el)
    }
    return () => {
      el.removeEventListener('scroll', evaluate)
      window.removeEventListener('resize', evaluate)
      if (observer) observer.disconnect()
    }
  }, [containerRef])
  return visible
}

/** 容器滚动版回到顶部按钮：containerRef 指向自身竖向滚动的容器 DOM
 *  （右侧边栏抽屉 .drawer），点击滚动容器自身到顶部 */
export function ScrollContainerBackToTop({ containerRef }) {
  const { t } = useI18n()
  const visible = useContainerBackToTopVisibility(containerRef)
  const scrollToTop = () => {
    const el = containerRef && containerRef.current
    if (!el || typeof el.scrollTo !== 'function') return
    el.scrollTo({ top: 0, left: 0, behavior: scrollBehaviorFor(prefersReducedMotion()) })
  }
  if (!visible) return null
  const label = t('common.backToTop')
  return (
    <button
      type="button"
      className="back-to-top in-drawer"
      onClick={scrollToTop}
      aria-label={label}
      title={label}
    >
      <Icon name="arrowUp" aria-hidden="true" />
    </button>
  )
}
