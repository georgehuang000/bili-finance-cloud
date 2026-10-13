import { useCallback, useEffect, useState } from 'react'
import { getLoginStatus, getStatus, getSummaries, getUppers } from '../api'
import { fmtFullTime, todayStr } from '../utils'
import SummaryPanel from '../components/SummaryPanel'
import SubscribePanel from '../components/SubscribePanel'
import UpperCard from '../components/UpperCard'
import Placeholder from '../components/Placeholder'
import { useToast } from '../components/Toast'
import { useAccess } from '../access'

const STATUS_POLL_MS = 60000

/** Dashboard:今日总结面板 + UP 主卡片栅格 */
export default function Dashboard() {
  const { canManage } = useAccess()
  const readOnly = !canManage
  const toast = useToast()
  const [summary, setSummary] = useState(null)
  const [summaryFallback, setSummaryFallback] = useState(false) // 今天没有,展示的是最近一条
  const [summaryLoading, setSummaryLoading] = useState(true)
  const [summaryError, setSummaryError] = useState(null)

  const [uppers, setUppers] = useState([])
  const [uppersLoading, setUppersLoading] = useState(true)
  const [uppersError, setUppersError] = useState(null)
  const [loginRequired, setLoginRequired] = useState(false)
  const [loginCheckedTs, setLoginCheckedTs] = useState(0)
  const [loginChecking, setLoginChecking] = useState(false)

  const loadSummary = useCallback(async () => {
    setSummaryLoading(true)
    setSummaryError(null)
    try {
      // 优先取今天的总结;没有则回退到最近一条
      const today = await getSummaries(todayStr())
      if (today.length > 0) {
        setSummary(today[0])
        setSummaryFallback(false)
      } else {
        const recent = await getSummaries()
        setSummary(recent[0] || null)
        setSummaryFallback(recent.length > 0)
      }
    } catch (e) {
      setSummaryError(e)
    } finally {
      setSummaryLoading(false)
    }
  }, [])

  const loadUppers = useCallback(async () => {
    setUppersLoading(true)
    setUppersError(null)
    try {
      setUppers(await getUppers())
    } catch (e) {
      setUppersError(e)
    } finally {
      setUppersLoading(false)
    }
  }, [])

  useEffect(() => {
    loadSummary()
    loadUppers()
    // 登录态失效时 feed 会以 -352 风控表现，这里定期取状态，
    // 避免用户把「未登录」误当成「风控限流」而反复重试
    const syncLogin = () =>
      getStatus()
        .then((s) => {
          setLoginRequired(!!s.login_required)
          setLoginCheckedTs(s.login_checked_ts || 0)
        })
        .catch(() => {})
    syncLogin()
    const timer = setInterval(syncLogin, STATUS_POLL_MS)
    return () => clearInterval(timer)
  }, [loadSummary, loadUppers])

  /** 手动重新检测登录态（排错用：确认是不是登录掉了） */
  const recheckLogin = async () => {
    setLoginChecking(true)
    try {
      const r = await getLoginStatus()
      if (r.logged_in === true) {
        setLoginRequired(false)
        toast(r.message || '登录态有效')
      } else if (r.logged_in === false) {
        setLoginRequired(true)
        toast(r.message || '登录态已失效，请重新扫码登录', 'error')
      } else {
        toast(r.message || '无法连接 B站，请稍后再试', 'error')
      }
      setLoginCheckedTs(Math.floor(Date.now() / 1000))
    } catch (e) {
      toast(e.message, 'error')
    } finally {
      setLoginChecking(false)
    }
  }

  const errorCount = uppers.filter((u) => u.last_error).length

  return (
    <div className="container">
      <div className="page-head">
        <h2 className="page-title">
          今日<span className="accent">总结</span>
        </h2>
        <span className="page-sub">{todayStr()}</span>
        {summaryFallback && summary && <span className="tag">今日暂无,显示最近一期 · {summary.date}</span>}
      </div>
      {summaryLoading || summaryError || !summary ? (
        <div className="panel">
          <Placeholder
            loading={summaryLoading}
            error={summaryError}
            empty={!summary}
            emptyText={readOnly ? '暂无已发布总结，后台更新后会在这里展示' : '暂无总结,可点击顶栏「立即总结」生成'}
            onRetry={loadSummary}
          />
        </div>
      ) : (
        <SummaryPanel summary={summary} />
      )}

      <div className="page-head">
        <h2 className="page-title">
          监控<span className="accent">名单</span>
        </h2>
        <span className="page-sub">{uppers.length > 0 ? `${uppers.length} UPPERS TRACKED` : 'FINANCE WATCHLIST'}</span>
      </div>
      {!readOnly && <SubscribePanel onChanged={loadUppers} />}
      {readOnly && <p className="page-sub">公开展示已订阅作者，内容由后台自动更新。</p>}
      {!readOnly && loginRequired && (
        <div className="notice error">
          <span className="notice-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="9" />
              <path d="M12 7.5V13M12 16.5v.01" />
            </svg>
          </span>
          <div>
            <b>B站登录态已失效</b>，本轮爬取已跳过——未登录时空间接口会被 B站以
            <b>-352 风控</b>拦截（看起来像限流，实际是没登录）。
            请在 <code>.env</code> 更新 <code>BILI_SESSDATA</code> / <code>BILI_JCT</code>
            （浏览器 F12 → Application → Cookies → bilibili.com 复制），
            改完重启后端即可；扫码登录 SOP 见 README。
            <div className="notice-actions">
              <button className="btn" onClick={recheckLogin} disabled={loginChecking}>
                {loginChecking ? '检测中…' : '重新检测登录态'}
              </button>
              {loginCheckedTs > 0 && (
                <span className="notice-meta">最近检测：{fmtFullTime(loginCheckedTs)}</span>
              )}
            </div>
          </div>
        </div>
      )}
      {!readOnly && errorCount > 0 && (
        <div className="notice">
          <span className="notice-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
              <path d="M12 9v4M12 17v.01" />
            </svg>
          </span>
          <div>
            <b>{errorCount}</b> 个 UP 主最近一轮抓取被 B站风控限流，数据可能未更新。
            系统已自动退避重试；建议间隔一段时间再手动爬取，频繁重试会延长风控。
          </div>
        </div>
      )}
      {uppersLoading || uppersError || uppers.length === 0 ? (
        <div className="panel">
          <Placeholder
            loading={uppersLoading}
            error={uppersError}
            empty={uppers.length === 0}
            emptyText="暂无 UP 主数据,后端完成首次爬取后展示"
            onRetry={loadUppers}
          />
        </div>
      ) : (
        <div className="upper-grid">
          {uppers.map((u, i) => (
            <UpperCard key={u.uid} upper={u} index={i} onChanged={loadUppers} />
          ))}
        </div>
      )}
    </div>
  )
}
