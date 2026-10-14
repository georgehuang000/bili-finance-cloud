// Same-origin /api is routed to FastAPI by Vercel Services (including vercel dev).
// 字段名严格遵循后端契约,勿改。
import { READ_ONLY } from './access'
import { requestPublished } from './publishedApi'

async function request(path, options = {}) {
  if (READ_ONLY) return requestPublished(path, options)
  let res
  try {
    res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    })
  } catch (e) {
    // 网络层失败(后端未启动等)
    const err = new Error('无法连接后端服务(后端可能未启动)')
    err.cause = e
    err.network = true
    throw err
  }
  if (!res.ok) {
    // FastAPI 的 HTTPException 返回 {detail: '中文说明'}，优先透出
    let detail = `请求失败:HTTP ${res.status}`
    try {
      const body = await res.json()
      if (body && typeof body.detail === 'string') detail = body.detail
    } catch {
      // 响应体不是 JSON 时维持默认文案
    }
    const err = new Error(detail)
    err.status = res.status
    throw err
  }
  return res.json()
}

/** GET /api/uppers */
export function getUppers() {
  return request('/api/uppers')
}

export function getResearch(uid) {
  return request(`/api/research${uid ? `?uid=${encodeURIComponent(uid)}` : ''}`)
}

export function analyzeResearch(uid) {
  return request('/api/research/analyze', { method: 'POST', body: JSON.stringify({ uid: uid || null }) })
}

export function verifyResearch(claimId, url) {
  return request('/api/research/evidence', { method: 'POST', body: JSON.stringify({ claim_id: claimId, url }) })
}

/** GET /api/uppers/{uid}/videos?limit=&offset= */
export function getVideos(uid, limit = 30, offset = 0) {
  return request(`/api/uppers/${uid}/videos?limit=${limit}&offset=${offset}`)
}

/** GET /api/uppers/{uid}/dynamics?limit=&offset= */
export function getDynamics(uid, limit = 30, offset = 0) {
  return request(`/api/uppers/${uid}/dynamics?limit=${limit}&offset=${offset}`)
}

/** GET /api/summaries?date=YYYY-MM-DD,date 省略返回最近 10 条 */
export function getSummaries(date) {
  const q = date ? `?date=${encodeURIComponent(date)}` : ''
  return request(`/api/summaries${q}`)
}

/** GET /api/status */
export function getStatus() {
  return request('/api/status')
}

/** POST /api/crawl */
export function postCrawl(uid, fullHistory = false) {
  return request('/api/crawl', { method: 'POST', body: JSON.stringify({ uid: uid || null, full_history: fullHistory }) })
}

/** POST /api/summarize */
export function postSummarize() {
  return request('/api/summarize', { method: 'POST' })
}

/** GET /api/uppers/search?q= 输入UP主名字或UID,返回候选列表 */
export function searchUppers(q) {
  return request(`/api/uppers/search?q=${encodeURIComponent(q)}`)
}

/** POST /api/uppers {uid, name?} 加入订阅,后端会立即抓一轮该UP主 */
export function addUpper(uid, name = '') {
  return request('/api/uppers', {
    method: 'POST',
    body: JSON.stringify({ uid, name }),
  })
}

/** DELETE /api/uppers/{uid} 取消订阅(保留历史数据) */
export function removeUpper(uid) {
  return request(`/api/uppers/${uid}`, { method: 'DELETE' })
}

/** POST /api/login/qrcode 生成 B站登录二维码,返回 {key, image(SVG data URI)} */
export function createQrLogin() {
  return request('/api/login/qrcode', { method: 'POST' })
}

/** GET /api/login/qrcode/poll?key= 轮询扫码状态 */
export function pollQrLogin(key) {
  return request('/api/login/qrcode/poll', { method: 'POST', body: JSON.stringify({ key }) })
}

/** GET /api/login/status 主动检测登录态是否有效 */
export function getLoginStatus() {
  return request('/api/login/status')
}

/** GET /api/diagnostics 一站式诊断快照(排错用,不含凭据) */
export function getDiagnostics() {
  return request('/api/diagnostics')
}

/** Only test the server's existing model configuration; never submit a key. */
export function testLlm() {
  return request('/api/llm/test', {
    method: 'POST',
    body: '{}',
  })
}

export function clearBiliLogin() {
  return request('/api/login', { method: 'DELETE' })
}
