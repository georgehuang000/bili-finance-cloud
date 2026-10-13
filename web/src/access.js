import React, { createContext, useContext, useEffect, useState } from 'react'

// Static public builds only fetch published JSON. Cloud builds additionally
// require a server-issued owner session before exposing management controls.
export const READ_ONLY = import.meta.env.VITE_PUBLIC_READONLY === 'true'
const Access = createContext({ canManage: false, cloud: false, testMode: false })
export const useAccess = () => useContext(Access)

export function AccessProvider({ children }) {
  const [state, setState] = useState({ canManage: false, cloud: false, testMode: false })
  const refresh = async () => {
    if (READ_ONLY) return
    try {
      const response = await fetch('/api/auth/session', { cache: 'no-store' })
      if (!response.ok) throw new Error('session unavailable')
      const data = await response.json()
      setState({ canManage: data.can_manage === true, cloud: data.cloud === true, testMode: data.test_mode === true })
    } catch { setState({ canManage: false, cloud: false, testMode: false }) }
  }
  useEffect(() => { refresh() }, [])
  const login = async password => {
    const response = await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }) })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '管理登录失败')
    await refresh()
  }
  const logout = async () => {
    await fetch('/api/auth/logout', { method: 'POST' })
    await refresh()
  }
  return React.createElement(Access.Provider, { value: { ...state, refresh, login, logout } }, children)
}
