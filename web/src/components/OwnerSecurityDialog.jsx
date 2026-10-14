import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { useAccess } from '../access'

export default function OwnerSecurityDialog({ open, onClose }) {
  const { mfaEnabled, refresh } = useAccess()
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [useRecovery, setUseRecovery] = useState(false)
  const [setup, setSetup] = useState(null)
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [complete, setComplete] = useState(false)

  useEffect(() => {
    if (!open) {
      setPassword(''); setCode(''); setSetup(null); setSaved(false)
      setUseRecovery(false); setError(''); setComplete(false)
      return
    }
    const previous = document.body.style.overflow
    const onKey = event => { if (event.key === 'Escape' && !busy) onClose() }
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', onKey)
    return () => {
      document.body.style.overflow = previous
      window.removeEventListener('keydown', onKey)
    }
  }, [open, busy, onClose])

  if (!open) return null
  const submit = async event => {
    event.preventDefault(); setBusy(true); setError('')
    const endpoint = setup ? 'confirm' : 'setup'
    const payload = setup ? { password, code } : { password, code: useRecovery ? '' : code, recovery_code: useRecovery ? code : '' }
    try {
      const response = await fetch(`/api/auth/mfa/${endpoint}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || '设置失败')
      if (setup) {
        setPassword(''); setCode(''); setSetup(null); setComplete(true)
        await refresh()
      } else { setSetup(result); setCode(''); setUseRecovery(false) }
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  return createPortal(<div className="modal-mask" onClick={() => !busy && onClose()}>
    <section className="modal" role="dialog" aria-modal="true" aria-labelledby="security-title" onClick={event => event.stopPropagation()} style={{ maxHeight: '90dvh', overflowY: 'auto' }}>
      <div className="modal-head"><h3 id="security-title" className="modal-title">登录安全</h3><button className="modal-close" aria-label="关闭" disabled={busy} onClick={onClose}>×</button></div>
      <div className="modal-body">
        {complete ? <><p role="status">双重验证已启用，其他浏览器的管理登录已退出。</p><p>以后登录需要管理密码和验证器动态码。手机丢失时，可用已保存的一次性恢复码代替动态码。</p><button className="btn primary" onClick={onClose}>完成</button></> :
          <form onSubmit={submit}>
            {!setup ? <><p>{mfaEnabled ? '双重验证已启用。更换验证器前，请再次验证管理密码和当前动态码；也可以使用一次性恢复码。' : '用手机验证器绑定动态码。完成扫码并验证前，现有登录方式继续可用。'}</p>
              <label htmlFor="security-password">再次输入管理密码</label><input id="security-password" type="password" autoComplete="current-password" required autoFocus value={password} onChange={event => setPassword(event.target.value)} style={inputStyle} />
              {mfaEnabled && <><label htmlFor="security-current-code">{useRecovery ? '一次性恢复码' : '当前验证器的六位动态码'}</label><input id="security-current-code" autoComplete={useRecovery ? 'off' : 'one-time-code'} inputMode={useRecovery ? 'text' : 'numeric'} required value={code} onChange={event => setCode(event.target.value.trim())} style={inputStyle} /><button type="button" className="btn" onClick={() => { setUseRecovery(!useRecovery); setCode('') }}>{useRecovery ? '使用动态码' : '手机不可用，使用恢复码'}</button><p>刚用于登录的动态码不能重复使用，请等待下一组。</p></>}
            </> : <>
              <p>1. 用验证器 App 扫码添加“财经哨站”，二维码十分钟有效。</p>
              <img src={setup.qr_image} alt="管理登录验证器绑定二维码" style={{ display: 'block', width: 220, maxWidth: '100%', margin: '12px auto' }} />
              <details><summary>无法扫码，手动输入绑定密钥</summary><code style={{ overflowWrap: 'anywhere' }}>{setup.manual_key}</code></details>
              <p>2. 保存以下恢复码到安全的密码管理器或离线位置。每个只能使用一次，关闭后无法再次查看。</p>
              <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12 }}>{setup.recovery_codes.join('\n')}</pre>
              <label style={{ display: 'block', margin: '12px 0' }}><input type="checkbox" required checked={saved} onChange={event => setSaved(event.target.checked)} /> 我已安全保存恢复码</label>
              <label htmlFor="security-new-code">3. 输入新验证器的六位动态码</label><input id="security-new-code" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required value={code} onChange={event => setCode(event.target.value)} style={inputStyle} />
            </>}
            {error && <p role="alert">{error}</p>}
            <button className="btn primary" disabled={busy || (setup && !saved)}>{busy ? '验证中…' : setup ? '确认启用双重验证' : mfaEnabled ? '绑定新的验证器' : '开始绑定验证器'}</button>
          </form>}
      </div>
    </section>
  </div>, document.body)
}

const inputStyle = { width: '100%', margin: '12px 0', padding: 12 }
