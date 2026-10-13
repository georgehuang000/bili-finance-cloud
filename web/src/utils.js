// 通用工具:时间/数字格式化(时间戳均为 unix 秒)

export function pad2(n) {
  return String(n).padStart(2, '0')
}

/** unix 秒 → "MM-DD HH:mm" */
export function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

/** unix 秒 → "YYYY-MM-DD HH:mm" */
export function fmtFullTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

/** 相对时间,如 "3 分钟前" */
export function fmtAgo(ts) {
  if (!ts) return '从未'
  const diff = Math.floor(Date.now() / 1000) - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

/** 播放量:12345 → "1.2万" */
export function fmtPlay(n) {
  if (n == null) return '—'
  if (n >= 100000000) return (n / 100000000).toFixed(1) + '亿'
  if (n >= 10000) return (n / 10000).toFixed(1) + '万'
  return String(n)
}

/** 本地今天的 YYYY-MM-DD */
export function todayStr() {
  const d = new Date()
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

export const PERIOD_LABEL = {
  morning: '晨报',
  evening: '晚报',
  manual: '手动',
}

/**
 * 图片识别结果 → 可展示文本。
 * 后端在识别失败时会写入 "[识别失败] xxx" 占位（防止每轮重试同一张坏图），
 * 那是运维信息不是内容，这里过滤掉，不要展示给用户、也不要进日报。
 */
export const IMAGE_FAIL_PREFIX = '[识别失败]'

export function cleanImageDesc(raw) {
  const t = (raw || '').trim()
  if (!t || t.startsWith(IMAGE_FAIL_PREFIX)) return ''
  return t
}

/**
 * 抓取错误标签 → 简短人话说明。
 * 后端 last_error 形如 risk_control(-352) / empty_feed(soft_throttle) / cookie_expired,
 * 直接展示给用户看不懂，这里统一翻译成「短标签 + 悬停详情」。
 */
export function explainCrawlError(err) {
  if (!err) return null
  const e = String(err)
  if (e.startsWith('risk_control')) {
    return { short: '风控限流', detail: `B站风控限流（${e}），已自动退避重试；频繁手动爬取会延长风控` }
  }
  if (e.startsWith('empty_feed')) {
    return { short: '软限流空包', detail: `B站返回空数据（${e}），已自动重试；通常稍后自行恢复` }
  }
  if (e === 'cookie_expired') {
    return { short: '登录态失效', detail: 'B站 Cookie 已失效，需要在 .env 重新配置登录态' }
  }
  if (e === 'login_required') {
    return { short: '需要登录', detail: 'B站要求重新登录（扫码），见 README 登录 SOP' }
  }
  if (e.startsWith('wbi_expired')) {
    return { short: '签名过期', detail: `WBI 签名过期（${e}），已自动刷新重试` }
  }
  if (e === 'request_too_frequent') {
    return { short: '请求过频', detail: '请求过于频繁，建议稍后再试' }
  }
  if (e.startsWith('timeout')) {
    return { short: '抓取超时', detail: `单次抓取超时（${e}）` }
  }
  if (e.startsWith('no_data')) {
    return { short: '无数据', detail: '该 UP 主本轮没有可解析的内容' }
  }
  return { short: '抓取异常', detail: e }
}
