import { useSearchParams } from 'react-router-dom'
import { Icon } from '../components/Icon.jsx'

// Synology SSO 登录页（issue #27）：SSO 启用后未登录访问 → 跳此页，
// 点击按钮跳转群晖 SSO Server 认证，回调成功后由后端 302 回首页。
const ERROR_TEXT = {
  login_failed: '登录失败：与群晖 SSO 服务器通信出错，请检查配置后重试',
  access_denied: '已在群晖 SSO 登录页取消授权',
  // 会话失效（issue #221）：api.js 统一拦截 401 时带此参数跳转，明确提示
  // 「登录已过期」而非静默失败——引导用户重新登录
  session_expired: '登录已过期，请重新登录',
  // 其他群晖返回的 error 原样展示
}

export default function Login() {
  const [params] = useSearchParams()
  const error = params.get('error')

  const startLogin = () => {
    window.location.href = '/api/auth/login'
  }

  return (
    <div className="login-page">
      <div className="card login-card">
        {/* HIG 目标感：品牌标识让首屏 3 秒内明白「这是什么」 */}
        <div className="login-brand" aria-hidden="true"><Icon name="bot" /></div>
        <h2>登录 Botler</h2>
        <p className="muted small">
          此实例启用了 Synology SSO 登录，请使用群晖账号认证后使用管理界面。
        </p>
        {error && <div className="alert alert-error">{ERROR_TEXT[error] || error}</div>}
        <div className="form-row center">
          <button className="btn btn-primary btn-wide" onClick={startLogin}>
            使用群晖账号登录
          </button>
        </div>
      </div>
    </div>
  )
}
