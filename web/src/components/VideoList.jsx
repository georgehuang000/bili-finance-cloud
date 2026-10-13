import { useState } from 'react'
import { fmtTime, fmtPlay, cleanImageDesc } from '../utils'
import Placeholder from './Placeholder'

function VideoCard({ video, index }) {
  const [coverFailed, setCoverFailed] = useState(false)
  const imgDesc = cleanImageDesc(video.image_desc)
  return (
    <div
      className="video-card"
      style={{ animationDelay: `${Math.min(index, 12) * 0.04}s` }}
      onClick={() => window.open(video.url, '_blank', 'noopener')}
      title={video.desc || video.title}
    >
      <div className="video-cover-wrap">
        {video.cover && !coverFailed ? (
          <img
            className="video-cover"
            src={video.cover}
            alt={video.title}
            loading="lazy"
            referrerPolicy="no-referrer"
            onError={() => setCoverFailed(true)}
          />
        ) : (
          <div className="video-cover" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-faint)' }}>
            无封面
          </div>
        )}
        <span className="video-play">▶ {fmtPlay(video.play)}</span>
      </div>
      <div className="video-body">
        <div className="video-title">{video.title}</div>
        {video.summary && <div className="video-summary">{video.summary}</div>}
        {imgDesc && (
          <div className="img-desc" title={imgDesc}>
            <span className="img-desc-tag">封面识别</span>
            {imgDesc}
          </div>
        )}
        <div className="video-meta">
          <span>{fmtTime(video.pub_ts)}</span>
          <span>{video.bvid}</span>
        </div>
      </div>
    </div>
  )
}

/** 视频列表:封面(no-referrer 防盗链)/ 标题 / 时间 / 播放量,点击新窗口打开 */
export default function VideoList({ videos, loading, error, onRetry }) {
  if (loading || error || !videos?.length) {
    return (
      <Placeholder
        loading={loading}
        error={error}
        empty={!videos?.length}
        emptyText="暂无视频数据,可点击顶栏「立即爬取」"
        onRetry={onRetry}
      />
    )
  }
  return (
    <div className="video-grid">
      {videos.map((v, i) => (
        <VideoCard key={v.bvid} video={v} index={i} />
      ))}
    </div>
  )
}
