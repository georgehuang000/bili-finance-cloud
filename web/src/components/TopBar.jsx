import { useCallback, useEffect, useRef, useState } from 'react'
import { NavLink } from 'react-router-dom'
import { getStatus, postCrawl, postSummarize, testLlm } from '../api'
import LoginDialog from './LoginDialog'
import { useToast } from './Toast'
import { useAccess } from '../access'
import { fmtFullTime } from '../utils'

const POLL_MS = 30000

function TvIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M7.5 2.5 5 5.5M16.5 2.5 19 5.5" />
      <rect x="2.5" y="5.5" width="19" height="14" rx="4" />
      <path d="M8.5 11v3M15.5 11v3" />
    </svg>
  )
}

function RefreshIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3v6h-6" />
    </svg>
  )
}

function PenIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  )
}

function LoginIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <path d="M14 14h3v3h-3zM20 14v3M17.5 20h3" />
    </svg>
  )
}

function GearIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6h.09A1.65 1.65 0 0 0 10.6 3.09V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9v.09a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  )
}

/** 全局顶栏:导航 + 立即爬取/总结 + 30s 轮询状态指示 */
export default function TopBar() {
  const { canManage, cloud, testMode, login, logout } = useAccess()
  const readOnly = !canManage
  const [ownerOpen, setOwnerOpen] = useState(false)
  const [ownerPassword, setOwnerPassword] = useState('')
  const [ownerError, setOwnerError] = useState('')
  const [ownerPending, setOwnerPending] = useState(false)
  const toast = useToast()
  const [status, setStatus] = useState(null) // null=未知(后端可能没起)
  const [crawlPending, setCrawlPending] = useState(false)
  const [sumPending, setSumPending] = useState(false)
  const [loginOpen, setLoginOpen] = useState(false)
  const [modelPending, setModelPending] = useState(false)
  const timerRef = useRef(null)

  const poll = useCallback(async () => {
    try {
      const s = await getStatus()
      setStatus(s)
    } catch {
      setStatus(null)
    }
  }, [])
  const onBiliLoggedIn = useCallback(() => {
    toast('B站登录成功，登录态已生效')
    poll()
  }, [toast, poll])

  useEffect(() => {
    poll()
    timerRef.current = setInterval(poll, POLL_MS)
    return () => clearInterval(timerRef.current)
  }, [])

  const handleCrawl = async () => {
    setCrawlPending(true)
    try {
      const r = await postCrawl()
      toast(r.message || '已触发爬取,后台执行中')
      setTimeout(poll, 1500) // 触发后尽快刷新状态
    } catch (e) {
      toast(e.message, 'error')
    } finally {
      setCrawlPending(false)
    }
  }

  const handleSummarize = async () => {
    setSumPending(true)
    try {
      const r = await postSummarize()
      toast(r.message || '已触发总结,后台执行中')
      setTimeout(poll, 1500)
    } catch (e) {
      toast(e.message, 'error')
    } finally {
      setSumPending(false)
    }
  }

  const crawling = status?.crawling
  const summarizing = status?.summarizing

  return (
    <header className="topbar">
      <div className="topbar-inner">
        <NavLink to="/" className="brand">
          <span className="brand-logo">
            <TvIcon />
          </span>
          <span className="brand-mark">财经哨站</span>
          <span className="brand-sub">UP MONITOR</span>
        </NavLink>
        <nav className="nav-links">
          <NavLink to="/" end className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            总览
          </NavLink>
          <NavLink to="/history" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            历史总结
          </NavLink>
          <NavLink to="/research" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            观点研究
          </NavLink>
        </nav>
        <div className="topbar-right">
          {cloud && <button className="btn" onClick={async () => {
            if (canManage) { await logout(); setLoginOpen(false) }
            else { setOwnerOpen(true); setOwnerError('') }
          }}>{canManage ? '退出管理' : '管理登录'}</button>}
          {cloud && testMode && canManage && <span className="status-chip">手动测试</span>}
          {readOnly ? (
            <span className="status-chip" title={status?.published_ts ? `公网页面更新于 ${fmtFullTime(status.published_ts)}` : '查看已发布内容'}><span className="dot" /><span>公开只读</span></span>
          ) : status === null ? (
            <span className="status-chip">
              <span className="dot gray" />
              <span className="chip-text">后端离线</span>
            </span>
          ) : status.login_required ? (
            <span className="status-chip error" title="B站登录态已失效，请在 .env 更新 BILI_SESSDATA / BILI_JCT（见 README 登录 SOP）">
              <span className="dot red" />
              <span className="chip-text">登录态失效</span>
            </span>
          ) : crawling || summarizing ? (
            <span className="status-chip busy">
              <span className="spinner" />
              <span className="chip-text">{crawling ? '爬取中' : '总结中'}</span>
            </span>
          ) : (
            <span className="status-chip">
              <span className="dot" />
              <span className="chip-text">空闲</span>
            </span>
          )}
          {!readOnly && <>
          {status && status.llm_configured === false ? (
            <span className="status-chip error" title="分析服务未配置，请由管理员在部署平台配置">
              <span className="dot red" />
              <span className="chip-text">分析服务未配置</span>
            </span>
          ) : null}
          <button
            className={`btn ${status?.login_required ? 'primary' : ''}`}
            onClick={() => setLoginOpen(true)}
            aria-label="扫码登录"
            title="用 B站 App 扫码登录"
          >
            <LoginIcon />
            <span className="btn-text">扫码登录</span>
          </button>
          <button
            className={`btn ${status && status.llm_configured === false ? 'primary' : ''}`}
            onClick={async () => {
              setModelPending(true)
              try { const result = await testLlm(); toast(result.ok ? `模型连接正常 · ${result.latency_ms}ms${result.usage?.total_tokens != null ? ` · ${result.usage.total_tokens} tokens` : ''}` : result.error || '连接失败', result.ok ? undefined : 'error') }
              catch (error) { toast(error.message, 'error') }
              finally { setModelPending(false) }
            }}
            disabled={modelPending}
            title="用一次小请求检查已配置的分析服务"
            aria-label="测试模型连接"
          >
            <GearIcon />
            <span className="btn-text">{modelPending ? '测试中…' : '测试模型连接'}</span>
          </button>
          {!(cloud && testMode) && <><button className="btn" aria-label="立即爬取" onClick={handleCrawl} disabled={crawlPending || crawling}>
            {crawling ? <span className="spinner" /> : <RefreshIcon />}
            <span className="btn-text">立即爬取</span>
          </button>
          <button className="btn primary" aria-label="立即总结" onClick={handleSummarize} disabled={sumPending || summarizing}>
            {summarizing ? <span className="spinner" /> : <PenIcon />}
            <span className="btn-text">立即总结</span>
          </button>
          </>}
          </>}
        </div>
      </div>
      {ownerOpen && <div className="modal-mask" onClick={() => setOwnerOpen(false)}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="owner-title" onClick={e => e.stopPropagation()}>
        <div className="modal-head"><h3 id="owner-title" className="modal-title">管理登录</h3><button className="modal-close" aria-label="关闭" onClick={() => { setOwnerOpen(false); setOwnerPassword('') }}>×</button></div>
        <form className="modal-body" onSubmit={async e => {
          e.preventDefault(); setOwnerPending(true); setOwnerError('')
          try { await login(ownerPassword); setOwnerPassword(''); setOwnerOpen(false); poll() }
          catch (error) { setOwnerError(error.message) }
          finally { setOwnerPending(false) }
        }}><label htmlFor="owner-password">管理密码</label><input id="owner-password" type="password" autoComplete="current-password" required value={ownerPassword} onChange={e => setOwnerPassword(e.target.value)} style={{ width: '100%', margin: '12px 0', padding: 12 }}/>
          {ownerError && <p role="alert">{ownerError}</p>}<button className="btn primary" disabled={ownerPending}>{ownerPending ? '登录中…' : '登录'}</button>
        </form></section></div>}
      {!readOnly && <LoginDialog
        open={loginOpen}
        onClose={() => setLoginOpen(false)}
        onLoggedIn={onBiliLoggedIn}
      />}
    </header>
  )
}
