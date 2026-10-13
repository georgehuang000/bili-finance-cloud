// Public visitors read one published snapshot; no management server is contacted.
let cached
let cachedAt = 0
let pending

async function snapshot() {
  if (cached && Date.now() - cachedAt < 300000) return cached
  if (!pending) {
    pending = fetch(`${import.meta.env.BASE_URL}data/snapshot.json`, { cache: 'no-cache' })
      .then(async response => {
        if (!response.ok) throw new Error('暂时无法读取已发布内容，请稍后重试')
        const data = await response.json()
        if (data.schema_version !== 1 || !Array.isArray(data.uppers) || !Array.isArray(data.summaries) || !Array.isArray(data.claims)) {
          throw new Error('已发布内容格式异常，请稍后重试')
        }
        cached = data; cachedAt = Date.now()
        return data
      })
      .finally(() => { pending = undefined })
  }
  // Keep author switches usable while a slow network checks for a newer snapshot.
  if (cached) { pending.catch(() => {}); return cached }
  return pending
}

export async function requestPublished(path, options = {}) {
  if ((options.method || 'GET').toUpperCase() !== 'GET') throw new Error('公网页面仅供查看，管理操作请使用私有入口')
  const url = new URL(path, 'https://published.invalid')
  const allowed = ['/api/uppers', '/api/summaries', '/api/research', '/api/status']
  const contentRoute = url.pathname.match(/^\/api\/uppers\/(\d{3,})\/(videos|dynamics)$/)
  if (!allowed.includes(url.pathname) && !contentRoute) throw new Error('公开入口不提供此查询或管理功能')
  const data = await snapshot()
  if (url.pathname === '/api/status') return { read_only: true, published_ts: data.generated_ts }
  if (url.pathname === '/api/uppers') return data.uppers
  if (url.pathname === '/api/summaries') {
    const date = url.searchParams.get('date')
    return date ? data.summaries.filter(s => s.date === date) : data.summaries.slice(0, 10)
  }
  const uid = contentRoute?.[1] || url.searchParams.get('uid')
  if (uid && !data.uppers.some(u => u.uid === uid)) throw new Error('该作者未在公开名单中')
  if (contentRoute) {
    const limit = Math.min(200, Math.max(1, Number(url.searchParams.get('limit')) || 30))
    const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0)
    return (data[contentRoute[2]]?.[uid] || []).slice(offset, offset + limit)
  }
  const topic = url.searchParams.get('topic')
  return {
    claims: data.claims.filter(c => (!uid || c.uid === uid) && (!topic || c.topic === topic)).slice(0, 150),
    status: { read_only: true, published_ts: data.generated_ts },
    evidence_jobs: [],
  }
}
