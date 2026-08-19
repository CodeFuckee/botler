// 「界面显示」配置卡片（issue #201 拆分）：从 Settings.jsx 抽出。含显示未
// 启用项目开关、界面主题三态下拉（切换即时预览 + 写 localStorage）、界面
// 语言、键盘快捷键开关、显示时区，以及「保存界面显示配置」独立保存按钮；
// 数据与处理函数经 props 注入（useSettingsData hook），行为与拆分前一致。
import { Icon } from '../Icon.jsx'
import { COMMON_TZ, themeStorage } from '../../hooks/useSettingsData.js'
import { THEME_MODE_LABELS, applyTheme, saveThemePreference } from '../../theme.js'
import { saveShortcutsEnabled } from '../../keymap.js'
import { LANG_LABELS } from '../../i18n.jsx'

export default function UiCard({
  settings, setSettings, uiSaveBusy, saveUi, uiSaved,
  shortcutsEnabled, setShortcutsEnabled, t, lang, setLang,
}) {
  return (
    <div className="card">
      <h2>界面显示</h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>显示未启用项目 <code>ui.show_disabled_repos</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={settings.ui?.show_disabled_repos !== false}
                onChange={(e) => setSettings((s) => ({
                  ...s,
                  ui: { ...(s.ui || {}), show_disabled_repos: e.target.checked },
                }))}
              />
            </td>
          </tr>
          <tr>
            <th>界面主题 <code>ui.theme</code></th>
            <td>
              <select
                className="input"
                value={settings.ui?.theme || 'system'}
                onChange={(e) => {
                  const theme = e.target.value
                  setSettings((s) => ({ ...s, ui: { ...(s.ui || {}), theme } }))
                  // 立即预览（issue #217）：切换三态即时生效，无需等保存；
                  // 保存按钮负责持久化到后端 config.yaml 与本地 localStorage
                  applyTheme(theme)
                  saveThemePreference(themeStorage, theme)
                }}
              >
                {Object.entries(THEME_MODE_LABELS).map(([mode, label]) => (
                  <option key={mode} value={mode}>{label}</option>
                ))}
              </select>
            </td>
          </tr>
          <tr>
            {/* 界面语言（issue #268）：切换即时生效并持久化到 localStorage
                botler.lang（刷新后保持）；语言名以母语展示 */}
            <th>{t('settings.ui.language')} <code>botler.lang</code></th>
            <td>
              <select
                className="input"
                value={lang}
                onChange={(e) => setLang(e.target.value)}
              >
                {Object.entries(LANG_LABELS).map(([code, label]) => (
                  <option key={code} value={code}>{label}</option>
                ))}
              </select>
            </td>
          </tr>
          <tr>
            {/* 键盘快捷键（issue #269）：全站快捷键启用开关，持久化
                localStorage（键 botler.shortcuts，默认开启）；帮助
                面板同步展示同一开关，任意一处切换即时全局生效 */}
            <th>启用键盘快捷键 <code>botler.shortcuts</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input shortcuts-toggle-input"
                checked={shortcutsEnabled}
                onChange={(e) => {
                  setShortcutsEnabled(e.target.checked)
                  saveShortcutsEnabled(
                    typeof localStorage !== 'undefined' ? localStorage : null,
                    e.target.checked)
                }}
              />
            </td>
          </tr>
          <tr>
            <th>显示时区 <code>ui.timezone</code></th>
            <td>
              <input
                className="input grow"
                list="timezone-options"
                placeholder="留空 = 跟随本机（浏览器时区）"
                value={settings.ui?.timezone || ''}
                onChange={(e) => setSettings((s) => ({ ...s, ui: { timezone: e.target.value.trim() } }))}
              />
              <datalist id="timezone-options">
                {COMMON_TZ.map((tz) => <option key={tz} value={tz} />)}
              </datalist>
            </td>
          </tr>
        </tbody>
      </table>
      <div className="form-row">
        <button className="btn btn-primary" disabled={uiSaveBusy} onClick={saveUi}>
          {uiSaveBusy ? '保存中…' : '保存界面显示配置'}
        </button>
        {uiSaved && <span className="saved-hint"><Icon name="check" /> 界面显示配置已保存（已写回 config.yaml）</span>}
      </div>
      <p className="muted small">
        灵感板块与 CI/CD 流水线板块是否显示未启用项目：勾选 = 显示（未启用仓库带
        「未启用」徽章，默认）；取消 = 两个板块只展示已启用仓库。
        界面主题三态：跟随系统（prefers-color-scheme 自动适配）/ 浅色 / 深色，
        切换即时预览，保存后写回 config.yaml 并同步浏览器本地偏好，刷新不闪变。
        任务创建/开始/完成时间与执行日志时间戳按显示时区展示；留空则跟随本机浏览器时区
        （默认与访问者本机一致），修改后点击下方「保存界面显示配置」立即生效，无需刷新。
        键盘快捷键（n 新建 issue / r 刷新 / t 跳转任务 / g o 概览 / g s 设置 / / 聚焦搜索）：
        勾选 = 启用（默认），取消 = 全站快捷键立即失效；输入框聚焦时快捷键不触发，避免误操作。
        {t('settings.ui.languageHint')}
      </p>
    </div>
  )
}
