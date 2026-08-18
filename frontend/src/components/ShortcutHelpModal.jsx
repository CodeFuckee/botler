// 快捷键帮助面板（issue #269）：页面右上角「快捷键帮助」按钮打开的
// 弹窗——展示全部快捷键键位 + 「启用键盘快捷键」开关。
//
// 设计要点：
//   1. 键位数据源 = keymap.js 的 SHORTCUT_DEFS（集中管理，验收标准 4），
//      新增快捷键自动出现在这里，无需维护第二份列表；
//   2. 开关持久化到 localStorage（键 botler.shortcuts，默认开启）：
//      一键禁用后分发处理器每次按键实时读取开关，立即全局生效
//      （验收标准 3），无需刷新；
//   3. 关闭方式与现有 Modal 一致：× 按钮 / 点击遮罩 / Esc
//      （SSR 测试环境无 document 时跳过 Esc 监听，与 AddIssueModal 一致）；
//   4. 文案经 i18n（issue #268），中英文切换即时生效。
import { useEffect, useState } from 'react'
import { useI18n } from '../i18n.jsx'
import { Icon } from './Icon.jsx'
import { SHORTCUT_DEFS, loadShortcutsEnabled, saveShortcutsEnabled } from '../keymap.js'

export default function ShortcutHelpModal({ onClose, storage }) {
  const { t } = useI18n()
  // 初始值读 localStorage；未配置（默认）为开启
  const [enabled, setEnabled] = useState(() => loadShortcutsEnabled(storage))

  // Esc 关闭（SSR 测试环境无 document 时跳过，与 AddIssueModal 一致）
  useEffect(() => {
    if (typeof document === 'undefined') return
    const onKey = (e) => {
      if (e && e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // 切换启用开关：立即持久化，分发处理器实时读取即全局生效
  const toggleEnabled = (next) => {
    setEnabled(next)
    saveShortcutsEnabled(storage, next)
  }

  // 生效范围文案（帮助面板分组展示：全站 / 概览页 / 任务页）
  const scopeLabel = (scopes) => {
    const key = Array.isArray(scopes) && scopes.length > 0 ? scopes[0] : ''
    return key ? t(`shortcuts.scope.${key}`) : ''
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal shortcuts-help" role="dialog" aria-modal="true"
           onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>{t('shortcuts.helpTitle')}</strong>
          <button className="btn modal-close" onClick={onClose} title={t('common.close')}
                  aria-label={t('common.close')}><Icon name="x" /></button>
        </div>
        <div className="shortcuts-help-body">
          <label className="checkbox-label shortcuts-toggle" title={t('shortcuts.enabledHint')}>
            <input type="checkbox" className="check-input shortcuts-toggle-input"
                   checked={enabled}
                   onChange={(e) => toggleEnabled(e.target.checked)} />
            {t('shortcuts.enabled')}
          </label>
          <p className="muted small shortcuts-hint">{t('shortcuts.enabledHint')}</p>
          <table className="table kv shortcuts-table">
            <tbody>
              {SHORTCUT_DEFS.map((d) => (
                <tr key={d.id}>
                  <th className="shortcut-keys"><kbd>{d.keys}</kbd></th>
                  <td>
                    <span className="shortcut-label">{t(d.labelKey)}</span>
                    <span className="muted small shortcut-scope">{scopeLabel(d.scope)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
