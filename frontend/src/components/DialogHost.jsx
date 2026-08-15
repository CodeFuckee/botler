// 自定义对话框宿主（issue #105）：渲染 dialog.js 队列队头的对话框，
// 替代浏览器原生 alert/confirm。挂在 App 根部，全局唯一（同一时刻
// 只显示一个对话框，后续调用排队）。
//
// 交互约定与现有 Modal（AddIssueModal / RepoEditModal 等）一致：
// - 关闭方式：× 按钮 / 点击遮罩 / Esc 键；
// - confirm 形态：点「确定」resolve(true)，其余关闭方式视为取消 resolve(false)；
// - alert 形态：单「确定」按钮，任何关闭方式均 resolve(undefined)；
// - danger 参数：确定按钮使用 btn-danger 危险样式（删除/覆盖类操作）；
// - 消息多行时用 .dialog-message 的 white-space: pre-line 保留换行
//   （如恢复备份警告的 \n\n 分段文案）。
import { useEffect, useReducer } from 'react'
import { currentDialog, settleDialog, subscribeDialogHost } from '../dialog.js'

export default function DialogHost() {
  const [, force] = useReducer((x) => x + 1, 0)

  // 订阅队列变化（新对话框入队 / 结算出队）触发重渲染
  useEffect(() => subscribeDialogHost(force), [])

  // Esc 关闭（SSR 测试环境无 document 时跳过，与 AddIssueModal 一致）
  useEffect(() => {
    if (typeof document === 'undefined') return
    const onKey = (e) => {
      const d = currentDialog()
      if (d && e.key === 'Escape') settleDialog(d.id, d.kind === 'confirm' ? false : undefined)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  const d = currentDialog()
  if (!d) return null
  // confirm 关闭值：确定 true / 取消类 false；alert 恒为 undefined
  const okValue = d.kind === 'confirm' ? true : undefined
  const dismissValue = d.kind === 'confirm' ? false : undefined
  return (
    <div className="modal-overlay" onClick={() => settleDialog(d.id, dismissValue)}>
      <div
        className="modal dialog"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <strong>{d.title || (d.kind === 'confirm' ? '请确认' : '提示')}</strong>
          <button
            className="btn modal-close"
            onClick={() => settleDialog(d.id, dismissValue)}
            title="关闭"
          >
            ×
          </button>
        </div>
        <div className="dialog-message">{d.message}</div>
        <div className="modal-footer">
          {d.kind === 'confirm' && (
            <button className="btn" onClick={() => settleDialog(d.id, false)}>
              {d.cancelText || '取消'}
            </button>
          )}
          <button
            className={'btn dialog-ok ' + (d.danger ? 'btn-danger' : 'btn-primary')}
            onClick={() => settleDialog(d.id, okValue)}
          >
            {d.confirmText || '确定'}
          </button>
        </div>
      </div>
    </div>
  )
}
