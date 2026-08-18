// ============================================================
// 前端界面国际化（issue #268）：轻量自研 i18n——React Context +
// JSON 字典（locales/zh-CN.json / en-US.json），不引入第三方依赖
// （对比 i18next 方案，前端规模中等、仅需中英双语，自研成本更低）。
//
// 设计要点：
//   1. 默认语言 zh-CN；en-US 未翻译的 key 自动回退中文，key 完全缺失时
//      原样返回 key（验收标准：未翻译文案回退中文不报错）；
//   2. 语言选择持久化到 localStorage（键 botler.lang，与主题
//      botler.theme issue #217 同模式），刷新后保持；
//   3. 语言即时切换：setLang 更新 Context 状态，全树文案即时重渲染；
//   4. 无 Provider 环境（SSR / 单组件测试）回退默认上下文（中文 + 空
//      操作），现有组件与测试不受影响；
//   5. 切换语言时同步 <html lang>，利于屏幕阅读器与浏览器翻译。
// ============================================================
import { createContext, useContext, useMemo, useState } from 'react'
import zhCN from './locales/zh-CN.json'
import enUS from './locales/en-US.json'

/** 支持的语言（en-US 缺失的 key 回退 zh-CN） */
export const LANGS = ['zh-CN', 'en-US']

/** 回退语言：未翻译 key 按中文展示（验收标准 2） */
export const FALLBACK_LANG = 'zh-CN'

/** 本地语言偏好存储键（与主题 botler.theme 同模式） */
export const LANG_STORAGE_KEY = 'botler.lang'

/** 语言名始终以母语展示（中文界面显示「中文」、英文界面显示「English」） */
export const LANG_LABELS = { 'zh-CN': '中文', 'en-US': 'English' }

const DICTS = { 'zh-CN': zhCN, 'en-US': enUS }

/** 字典表导出（供调试/测试验证回退逻辑，勿在生产代码修改） */
export const I18N_DICTS = DICTS

/** 是否为受支持语言（非法值一律视为未配置，回退默认） */
export function isValidLang(lang) {
  return LANGS.includes(lang)
}

/** 读取本地语言偏好：非法值（手改/旧版本写入）一律回退 null（= 默认中文）。
 *  storage：localStorage 兼容对象（测试可注入）；无存储环境（SSR）或
 *  getItem 抛异常（隐私模式）时兜底 null，不影响页面使用。 */
export function loadLangPreference(storage) {
  try {
    if (!storage) return null
    const raw = storage.getItem(LANG_STORAGE_KEY)
    return isValidLang(raw) ? raw : null
  } catch {
    return null
  }
}

/** 保存本地语言偏好：只接受 zh-CN / en-US；
 *  存储不可用（SSR/隐私模式）时静默忽略，不抛错。 */
export function saveLangPreference(storage, lang) {
  try {
    if (!isValidLang(lang)) return
    storage?.setItem(LANG_STORAGE_KEY, lang)
  } catch {
    /* 无存储环境：静默忽略，不影响页面使用 */
  }
}

/** 查词：当前语言 → 中文回退 → 原样返回 key（不抛错）。
 *  vars：可选插值参数，{name} 占位符替换为对应值（值为空串时替换为空）。 */
export function translate(lang, key, vars) {
  let text = null
  if (DICTS[lang] && typeof DICTS[lang][key] === 'string') {
    text = DICTS[lang][key]
  } else if (lang !== FALLBACK_LANG && typeof DICTS[FALLBACK_LANG][key] === 'string') {
    text = DICTS[FALLBACK_LANG][key]
  } else {
    return key
  }
  if (vars && typeof vars === 'object') {
    for (const [k, v] of Object.entries(vars)) {
      text = text.split(`{${k}}`).join(String(v ?? ''))
    }
  }
  return text
}

/** 应用 <html lang>：doc 可注入兼容对象（测试用）；无 DOM 环境静默跳过。 */
export function applyHtmlLang(lang, doc) {
  try {
    const d = doc || (typeof document !== 'undefined' ? document : null)
    if (d?.documentElement) d.documentElement.lang = lang
  } catch {
    /* 无 DOM 环境：静默忽略 */
  }
}

// 默认上下文：无 Provider（SSR / 单组件测试）时按中文静态文案渲染，
// setLang 为空操作——组件直接渲染也不会崩溃，现有测试保持稳定。
const I18nContext = createContext({
  lang: FALLBACK_LANG,
  setLang: () => {},
  t: (key, vars) => translate(FALLBACK_LANG, key, vars),
  tr: (key, vars) => translate(FALLBACK_LANG, key, vars),
})

/** i18n Provider：读取本地偏好初始化语言；setLang 即时切换并持久化。
 *  storage：localStorage 兼容对象（测试可注入）；无存储环境时按默认中文。 */
export function I18nProvider({ children, storage }) {
  const [lang, setLangState] = useState(() => loadLangPreference(storage) || FALLBACK_LANG)
  const setLang = (next) => {
    if (!isValidLang(next)) return
    setLangState(next)
    saveLangPreference(storage, next)
    applyHtmlLang(next)
  }
  const value = useMemo(() => {
    const tf = (key, vars) => translate(lang, key, vars)
    return { lang, setLang, t: tf, tr: tf }
  }, [lang])
  return (
    <I18nContext.Provider value={value}>
      {children}
    </I18nContext.Provider>
  )
}

/** 组件内获取 i18n：{ lang, setLang, t } */
export function useI18n() {
  return useContext(I18nContext)
}
