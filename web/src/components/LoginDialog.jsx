import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { createQrLogin, pollQrLogin } from '../api'

const POLL_MS = 2000

/**
 * 扫码登录弹窗：展示 B站登录二维码，轮询扫码结果。
 * 成功后端会把登录态写入 .env 并即时生效（无需重启），这里只负责反馈。
 */
export default function LoginDialog({ open, onClose, onLoggedIn }) {
  const [qr, setQr] = useState(null) // {key, image}
  const [phase, setPhase] = useState('loading') // loading|waiting|scanned|confirmed|expired|error
  const [message, setMessage] = useState('正在获取二维码…')
  const [account, setAccount] = useState(null)
  const [reloadKey, setReloadKey] = useState(0)

  const timerRef = useRef(null)
  const stoppedRef = useRef(false)

  const stopPolling = useCallback(() => {
    stoppedRef.current = true
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [])

  // 打开时生成二维码并开始轮询；关闭时停止
  useEffect(() => {
    if (!open) {
      stopPolling()
      setQr(null)
      setPhase('loading')
      setAccount(null)
      setMessage('正在获取二维码…')
      return
    }

    stoppedRef.current = false
    let cancelled = false
    // 锁住背景滚动，避免弹窗打开时页面跟着滚
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const start = async () => {
      try {
        const res = await createQrLogin()
        if (cancelled) return
        setQr(res)
        setPhase('waiting')
        setMessage('请用 B站 App 扫码登录')
        poll(res.key)
      } catch (e) {
        if (cancelled) return
        setPhase('error')
        setMessage(e.message || '二维码获取失败')
      }
    }

    const poll = async (key) => {
      if (stoppedRef.current || cancelled) return
      try {
        const r = await pollQrLogin(key)
        if (stoppedRef.current || cancelled) return
        setPhase(r.status)
        setMessage(r.message || '')
        if (r.status === 'confirmed') {
          setAccount(r.account || null)
          stopPolling()
          onLoggedIn?.()
          return
        }
        if (r.status === 'expired' || r.status === 'error') {
          stopPolling()
          return
        }
      } catch (e) {
        if (stoppedRef.current || cancelled) return
        setPhase('error')
        setMessage(e.message || '轮询失败')
        stopPolling()
        return
      }
      timerRef.current = setTimeout(() => poll(key), POLL_MS)
    }

    start()
    return () => {
      cancelled = true
      document.body.style.overflow = prevOverflow
      stopPolling()
    }
  }, [open, reloadKey, onLoggedIn, stopPolling])

  // ESC 关闭
  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && onClose?.()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const done = phase === 'confirmed'

  // 用 portal 挂到 body：顶栏有 backdrop-filter（会把自己变成 position:fixed
  // 后代的包含块），弹窗若留在顶栏内部就会"卡在最顶上"、无法居中覆盖全屏。
  return createPortal(
    <div className="modal-mask" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="B站扫码登录"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3 className="modal-title">B站扫码登录</h3>
          <button className="modal-close" onClick={onClose} title="关闭" aria-label="关闭">
            ✕
          </button>
        </div>

        <div className="modal-body">
          {done ? (
            <div className="login-done">
              <div className="login-done-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M20 6 9 17l-5-5" />
                </svg>
              </div>
              <div className="login-done-title">登录成功</div>
              <div className="login-done-sub">
                {account?.uname ? `已登录为 ${account.uname}，` : ''}
                登录态已写入 .env，爬虫下一轮即生效（无需重启）
              </div>
              <button className="btn primary" onClick={onClose}>
                完成
              </button>
            </div>
          ) : (
            <>
              <div className={`qr-box ${phase === 'expired' || phase === 'error' ? 'qr-box-dim' : ''}`}>
                {qr?.image ? (
                  <img className="qr-img" src={qr.image} alt="B站登录二维码" />
                ) : (
                  <span className="spinner" style={{ width: 26, height: 26, borderWidth: 3 }} />
                )}
                {(phase === 'expired' || phase === 'error') && (
                  <div className="qr-overlay">
                    <button className="btn primary" onClick={() => setReloadKey((v) => v + 1)}>
                      刷新二维码
                    </button>
                  </div>
                )}
              </div>

              <div className={`login-msg login-msg-${phase}`}>
                {phase === 'scanned' && <span className="spinner" />}
                {message}
              </div>

              <ol className="login-steps">
                <li>打开手机 B站 App，点右上角「扫一扫」</li>
                <li>扫描上方二维码，并在手机上确认登录</li>
                <li>成功后本页自动跳转，登录态自动保存</li>
              </ol>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body
  )
}