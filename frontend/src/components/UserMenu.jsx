import { useEffect, useState } from 'react'
import { api, fmtTime } from '../api.js'
import { useI18n } from '../i18n.jsx'

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

  // 会话过期时间 tooltip（exp 为 unix 秒；fmtTime 解析失败返回原样，不展示）
  const expiry = fmtTime(user.exp)
  const tooltip = expiry && expiry !== '—'
    ? `${t('nav.userTitle')} · ${t('nav.sessionExpiry', { time: expiry })}`
    : t('nav.userTitle')

  // 退出登录：调用现有 POST /api/auth/logout，成功后回登录页
  const logout = async () => {
    try { await api.post('/api/auth/logout') } catch { /* 忽略 */ }
    window.location.href = '/login'
  }

  return (
    <span className="user-chip" title={tooltip}>
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
      <button type="button" className="btn btn-sm" onClick={logout}>
        {t('nav.logout')}
      </button>
    </span>
  )
}
