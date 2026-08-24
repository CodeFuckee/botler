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
import { forwardRef } from 'react'
import { useDrawerResize } from '../hooks/useDrawerResize.js'
// 存储 key 由 hook 模块统一定义，此处 re-export 供各抽屉导入
export { ISSUE_DRAWER_WIDTH_KEY, PIPELINE_DRAWER_WIDTH_KEY, CHAT_DRAWER_WIDTH_KEY }
  from '../hooks/useDrawerResize.js'

const ResizableDrawer = forwardRef(function ResizableDrawer(
  { drawerClass = '', storageKey, dialog = false, children, onClick }, ref
) {
  const { width, resizable, handleProps } = useDrawerResize({
    storageKey,
    drawerRef: ref,
  })
  return (
    <div className={'drawer ' + drawerClass} ref={ref}
         style={width != null ? { width } : undefined}
         role={dialog ? 'dialog' : undefined}
         aria-modal={dialog ? 'true' : undefined}
         onClick={onClick}>
      {/* 拖拽手柄：绝对定位于抽屉左缘（宽 8px 整高），窄视口不渲染 */}
      {resizable && <div {...handleProps} />}
      {children}
    </div>
  )
})

export default ResizableDrawer
