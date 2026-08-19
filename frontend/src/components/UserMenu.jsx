import { useEffect, useRef, useState } from 'react'
import { api, fmtSeconds, fmtTime } from '../api.js'
import { showToast } from '../toast.js'
import { useI18n } from '../i18n.jsx'

// 会话临近过期阈值（issue #221）：剩 1 天（24 小时）内提示续期
const EXPIRE_SOON_MS = 24 * 3600 * 1000

// 导航栏用户区（issue #271）：SSO 登录后右上角展示昵称/头像（OIDC claims
// 的 name/picture，经 /api/auth/me 获取）与「退出登录」按钮。
// - 头像：优先 picture 图片，加载失败回退首字母占位（验收标准 3）；
// - 会话信息：tooltip 展示过期时间（exp 为 unix 秒，fmtTime 兼容转换，
//   与 #221 的过期提示联动）；
// - 未启用 SSO 时显示「未登录（开放模式）」弱提示，不打扰（验收标准 2）。
export default function UserMenu({ user: initialUser, ssoEnabled }) {
  const { t } = useI18n()
  // 用户信息（初始来自 /api/auth/status，随后用 /api/auth/me 刷新）
  const [user, setUser] = useState(initialUser)
  // 头像加载失败标记：true 时回退首字母占位
  const [avatarFailed, setAvatarFailed] = useState(false)
  // 当前时刻（issue #221）：每分钟刷新，保证会话剩余时间/临期判断不随
  // 页面停留而过时；node 测试环境 interval unref，不阻塞进程退出
  const [now, setNow] = useState(() => Date.now())
  // 临期提醒去重（issue #221）：同一段临期状态只弹一次 toast
  const expiringNotified = useRef(false)

  // 复用 /api/auth/me 获取最新用户信息（含 OIDC picture 与会话过期 exp）：
  // 刷新失败（未登录 401 等）保持 status 探测到的用户，不报错不打扰
  useEffect(() => {
    if (!initialUser) return
    let cancelled = false
    api.get('/api/auth/me')
      .then((me) => {
        if (cancelled) return
        setUser(me)
        setAvatarFailed(false)
      })
      .catch(() => { /* 保持初始用户，静默降级 */ })
    return () => { cancelled = true }
  }, [initialUser])

  // 会话剩余时间（issue #221）：exp 为 unix 秒，距当前时刻的剩余时长；
  // 剩余 ≤ 1 天判定为临近过期（提示续期）
  const remainMs = user?.exp ? user.exp * 1000 - now : null
  const remainText = remainMs != null && remainMs > 0
    ? fmtSeconds(Math.floor(remainMs / 1000))
    : null
  const expiring = remainMs != null && remainMs <= EXPIRE_SOON_MS

  // 每分钟刷新当前时刻（剩余时间/临期判断随页面停留实时更新）
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60000)
    if (typeof timer.unref === 'function') timer.unref() // node 测试环境
    return () => clearInterval(timer)
  }, [])

  // 临近过期主动提醒（issue #221）：进入临期（剩 ≤1 天）时弹一次 toast
  // 引导续期；退出临期（重新登录等）后重置，下次临期可再次提醒
  useEffect(() => {
    if (expiring && !expiringNotified.current) {
      expiringNotified.current = true
      showToast(t('nav.sessionExpiring', { remain: remainText || t('common.unknown') }), {
        type: 'warning',
      })
    } else if (!expiring) {
      expiringNotified.current = false
    }
  }, [expiring, remainText, t])

  // 未登录：SSO 未启用 → 开放模式弱提示；SSO 启用未登录由 App 渲染登录页，
  // 此处兜底不渲染
  if (!user) {
    if (!ssoEnabled) {
      return (
        <span className="user-chip user-chip-guest" title={t('nav.notLoggedIn')}>
          {t('nav.notLoggedIn')}
        </span>
      )
    }
    return null
  }

  // 昵称与首字母（头像回退占位用）：name → username → sub 依次兜底
  const displayName = user.name || user.username || user.sub || ''
  const initial = displayName ? displayName.trim().charAt(0).toUpperCase() : '?'

  // 会话信息 tooltip（issue #221）：过期时间 + 剩余时长；临期时明确提示
  // 「即将过期，请重新登录以续期」（剩余时长经 fmtSeconds 人类可读）
  const expiry = fmtTime(user.exp)
  const remainSec = remainText
    ? ` · ${t('nav.sessionRemain', { remain: remainText })}`
    : ''
  const tooltip = expiring
    ? `${t('nav.userTitle')} · ${t('nav.sessionExpiring', { remain: remainText || t('common.unknown') })}`
    : expiry && expiry !== '—'
      ? `${t('nav.userTitle')} · ${t('nav.sessionExpiry', { time: expiry })}${remainSec}`
      : t('nav.userTitle')

  // 退出登录：调用现有 POST /api/auth/logout，成功后回登录页
  const logout = async () => {
    try { await api.post('/api/auth/logout') } catch { /* 忽略 */ }
    window.location.href = '/login'
  }

  return (
    <span className={expiring ? 'user-chip user-chip-expiring' : 'user-chip'} title={tooltip}>
      {expiring && <span className="user-chip-warn" aria-hidden="true">⚠</span>}
      {user.picture && !avatarFailed ? (
        <img
          className="user-avatar"
          src={user.picture}
          alt=""
          onError={() => setAvatarFailed(true)}
        />
      ) : (
        <span className="user-avatar user-avatar-fallback" aria-hidden="true">{initial}</span>
      )}
      <span className="user-chip-name">{displayName}</span>
      {/* 临近过期续期入口（issue #221）：会话为签名 cookie 无法服务端延长，
          续期 = 跳 SSO 登录页重新登录（获得新的 session_days 会话） */}
      {expiring && (
        <button
          type="button"
          className="btn btn-sm btn-warning"
          onClick={() => { window.location.href = '/api/auth/login' }}
        >
          {t('nav.renew')}
        </button>
      )}
      <button type="button" className="btn btn-sm" onClick={logout}>
        {t('nav.logout')}
      </button>
    </span>
  )
}
