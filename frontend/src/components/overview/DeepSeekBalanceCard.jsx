// DeepSeek 账户余额卡片（issue #201 拆分）：从 Overview.jsx 抽出
// 的余额展示子组件，数据由 useOverviewData hook 注入（组件只接数据）。
import { useI18n } from '../../i18n.jsx'
import { Icon } from '../Icon.jsx'
import { fmtTime } from '../../api.js'
import {
  DEEPSEEK_BALANCE_POLL_MS,
  DEEPSEEK_TOPUP_URL,
  fmtRateWindowText,
} from '../../lib/overview.jsx'

export default function DeepSeekBalanceCard({
  dsBalance, dsBalanceError, dsRate, loadDeepSeekBalance,
}) {
  const { tr } = useI18n()
  return (
            <section className="deepseek-balance-section">
              <h2>{tr('overview.balanceTitle')}</h2>
              <p className="muted">
                {tr('overview.balanceDesc', { seconds: DEEPSEEK_BALANCE_POLL_MS / 1000 })}
              </p>
              {(dsBalance.error || dsBalanceError) && (
                <div className="alert alert-error" role="alert">
                  {dsBalance.error || dsBalanceError}
                </div>
              )}
              {dsBalance.balance && (
                <div className="deepseek-balance-body">
                  <div className="deepseek-balance-head">
                    {dsBalance.balance.is_available ? (
                      <span className="ok-text"><Icon name="check" /> {tr('overview.balanceAvailable')}</span>
                    ) : (
                      <span className="muted">{tr('overview.balanceUnavailable')}</span>
                    )}
                    {dsBalance.balance.fetched_at && (
                      <span className="muted small" title={tr('overview.queryTime')}>
                        {tr('overview.updatedAt', { time: fmtTime(dsBalance.balance.fetched_at) })}
                      </span>
                    )}
                  </div>
                  {(dsBalance.balance.balance_infos || []).length === 0 ? (
                    <div className="empty-state small">
                      <span className="empty-icon" aria-hidden="true"><Icon name="wallet" /></span>
                      <p className="muted">{tr('overview.noBalance')}</p>
                    </div>
                  ) : (
                    <ul className="deepseek-balance-list">
                      {(dsBalance.balance.balance_infos || []).map((info, i) => {
                        // issue #304：该币种的每小时余额变化速率（无样本/
                        // 窗口过短时 undefined，展示「暂无速率数据」）
                        const rateInfo = dsRate && dsRate[info.currency]
                        return (
                        <li key={i} className="deepseek-balance-item">
                          <span className="deepseek-balance-currency" title={tr('overview.currency')}>
                            {info.currency || '—'}
                          </span>
                          <span className="deepseek-balance-total" title={tr('overview.totalBalance')}>
                            {info.total_balance != null ? `${info.total_balance}` : '—'}
                          </span>
                          <span className="muted small" title="赠送余额">
                            {tr('overview.grantedBalance', { amount: info.granted_balance ?? '—' })}
                          </span>
                          <span className="muted small" title="充值余额">
                            {tr('overview.rechargeBalance', { amount: info.topped_up_balance ?? '—' })}
                          </span>
                          <span className="deepseek-balance-rate"
                                title={rateInfo
                                  ? tr('overview.rateHint', { window: fmtRateWindowText(tr, rateInfo.windowMs) })
                                  : tr('overview.rateNone')}>
                            {rateInfo ? (
                              rateInfo.ratePerHour < 0
                                ? tr('overview.rateDecrease', { amount: Math.abs(rateInfo.ratePerHour).toFixed(2) })
                                : rateInfo.ratePerHour > 0
                                  ? tr('overview.rateIncrease', { amount: Math.abs(rateInfo.ratePerHour).toFixed(2) })
                                  : tr('overview.rateStable')
                            ) : tr('overview.rateNone')}
                          </span>
                        </li>
                        )
                      })}
                    </ul>
                  )}
                </div>
              )}
              <div className="form-row">
                <button type="button" className="btn btn-small"
                        onClick={loadDeepSeekBalance}><Icon name="refresh" /> {tr('common.refresh')}</button>
                <a className="btn btn-small deepseek-topup-link"
                   href={DEEPSEEK_TOPUP_URL} target="_blank" rel="noreferrer"
                   title={tr('overview.rechargeTitle')}><Icon name="externalLink" /> {tr('overview.recharge')}</a>
              </div>
            </section>
  )
}
