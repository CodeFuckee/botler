// 可调整宽度的右侧边栏容器（issue #466）：包裹任意 .drawer 抽屉内容，
// 在抽屉左缘渲染拖拽手柄（视口宽度 > 860px 才渲染，见 useDrawerResize），
// 拖拽/键盘调整的宽度经 clamp 后以内联 style 覆盖 CSS 默认宽度，结束后
// 写入 localStorage（storageKey 按抽屉类型区分）持久化。
//
// 用法：
//   <ResizableDrawer drawerClass="issue-drawer" storageKey={...}
//                    ref={drawerRef} onClick={onClick}>
//     ...抽屉内容...
//   </ResizableDrawer>
// 抽屉基础类名 .drawer 由本组件统一输出，传入 drawerClass 追加具体抽屉类
// （.issue-drawer / .pipeline-drawer / .chat-drawer），不影响既有 CSS
// 选择器（.drawer-overlay / .issue-drawer .modal-header 等）。
//
// issue #475：拖拽手柄必须渲染在 .drawer 滚动容器之外——.drawer 自身是
// overflow-y: auto 的滚动容器，又是手柄的定位参考系（position: relative +
// will-change: transform），绝对定位的手柄会随抽屉内容一起滚动（页面滚到
// 下方时只剩一半手柄可见）。因此外层套 .drawer-shell（position: relative、
// 自身不滚动）作为手柄的定位参考系，手柄作为 .drawer 的兄弟节点渲染于
// 外壳内，抽屉内容滚动时手柄保持整高固定。
import { forwardRef } from 'react'
import { useDrawerResize } from '../hooks/useDrawerResize.js'
// 存储 key 由 hook 模块统一定义，此处 re-export 供各抽屉导入
export { ISSUE_DRAWER_WIDTH_KEY, PIPELINE_DRAWER_WIDTH_KEY, CHAT_DRAWER_WIDTH_KEY,
           TASK_DETAIL_DRAWER_WIDTH_KEY }
  from '../hooks/useDrawerResize.js'

const ResizableDrawer = forwardRef(function ResizableDrawer(
  { drawerClass = '', storageKey, dialog = false, children, onClick }, ref
) {
  const { width, resizable, handleProps } = useDrawerResize({
    storageKey,
    drawerRef: ref,
  })
  return (
    /* issue #475：非滚动定位外壳——手柄的绝对定位参考系。
       .drawer 内部 overflow-y: auto 滚动，外壳本身不滚动，
       手柄位于外壳内、.drawer 之外，内容滚动时手柄不跟随 */
    <div className="drawer-shell" onClick={(e) => e.stopPropagation()}>
      <div className={'drawer ' + drawerClass} ref={ref}
           style={width != null ? { width } : undefined}
           role={dialog ? 'dialog' : undefined}
           aria-modal={dialog ? 'true' : undefined}
           onClick={onClick}>
        {children}
      </div>
      {/* 拖拽手柄：绝对定位于外壳左缘（宽 8px 整高），窄视口不渲染 */}
      {resizable && <div {...handleProps} />}
    </div>
  )
})

export default ResizableDrawer
