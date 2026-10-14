import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { explainCrawlError, fmtAgo } from '../utils'
import { removeUpper } from '../api'
import { useToast } from './Toast'
import { useAccess } from '../access'

function WarnIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4M12 17v.01" />
    </svg>
  )
}

/** UP 主卡片:头像 / 名称 / 视频·动态数 / 最后爬取时间 / 取消订阅 */
export default function UpperCard({ upper, index = 0, onChanged }) {
  const { canManage } = useAccess()
  const readOnly = !canManage
  const navigate = useNavigate()
  const toast = useToast()
  const [imgFailed, setImgFailed] = useState(false)
  const [removing, setRemoving] = useState(false)
  const crawlIssue = explainCrawlError(upper.last_error)

  const onRemove = async (e) => {
    e.stopPropagation() // 不要触发卡片跳转
    if (!window.confirm(`确定取消订阅「${upper.name}」吗?\n其已抓取的历史数据会保留,重新订阅即恢复。`)) {
      return
    }
    setRemoving(true)
    try {
      const res = await removeUpper(upper.uid)
      toast(res.message || '已取消订阅')
      onChanged?.()
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setRemoving(false)
    }
  }

  return (
    <div
      className="upper-card"
      style={{ animationDelay: `${index * 0.05}s` }}
      onClick={() => navigate(`/upper/${upper.uid}`)}
      title={`查看 ${upper.name} 的视频与动态`}
    >
      {!readOnly && <button
        className="card-remove"
        title="取消订阅"
        disabled={removing}
        onClick={onRemove}
      >
        {removing ? '…' : '✕'}
      </button>}
      {upper.face && !imgFailed ? (
        <img
          className="avatar"
          src={upper.face}
          alt={upper.name}
          referrerPolicy="no-referrer"
          onError={() => setImgFailed(true)}
        />
      ) : (
        <div className="avatar-fallback">{(upper.name || '?').slice(0, 1)}</div>
      )}
      <div className="upper-info">
        <div className="upper-name">{upper.name}</div>
        <div className="upper-stats">
          <span>
            <span className="stat-label">视频</span>
            <span className="stat-num">{upper.video_count ?? 0}</span>
          </span>
          <span>
            <span className="stat-label">动态</span>
            <span className="stat-num">{upper.dynamic_count ?? 0}</span>
          </span>
        </div>
        <div className="upper-crawl">{readOnly ? '最近更新' : '上次爬取'} · {fmtAgo(upper.last_crawl_ts)}</div>
        {upper.history_status && (
          <div className="upper-crawl">
            {upper.history_status === 'complete' ? '历史采集完成' : upper.history_status === 'limited' ? '历史范围未核全 · 接口返回不完整' : upper.history_status === 'error' ? '历史补齐待重试' : '历史内容补齐中'}
          </div>
        )}
        {!readOnly && crawlIssue && (
          <div className="upper-error" title={crawlIssue.detail}>
            <WarnIcon />
            {crawlIssue.short}
          </div>
        )}
      </div>
    </div>
  )
}
