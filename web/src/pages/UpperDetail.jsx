import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { getDynamics, getUppers, getVideos, postCrawl } from '../api'
import { useAccess } from '../access'
import { useToast } from '../components/Toast'
import { fmtAgo } from '../utils'
import VideoList from '../components/VideoList'
import DynamicList from '../components/DynamicList'

const PAGE = 30

/** UP 主详情页:Tab 切换 视频/动态,支持加载更多 */
export default function UpperDetail() {
  const { uid } = useParams()
  const { canManage } = useAccess()
  const toast = useToast()
  const [backfilling, setBackfilling] = useState(false)
  const [upper, setUpper] = useState(null)
  const [tab, setTab] = useState('videos')

  const [videos, setVideos] = useState([])
  const [videosLoading, setVideosLoading] = useState(true)
  const [videosError, setVideosError] = useState(null)
  const [videosDone, setVideosDone] = useState(false)

  const [dynamics, setDynamics] = useState([])
  const [dynLoading, setDynLoading] = useState(true)
  const [dynError, setDynError] = useState(null)
  const [dynDone, setDynDone] = useState(false)

  // 契约没有单个 UP 主接口,从列表里捞基础信息
  useEffect(() => {
    getUppers()
      .then((list) => setUpper(list.find((u) => String(u.uid) === String(uid)) || null))
      .catch(() => setUpper(null))
  }, [uid])

  const loadVideos = useCallback(
    async (offset = 0) => {
      setVideosLoading(true)
      setVideosError(null)
      try {
        const list = await getVideos(uid, PAGE, offset)
        setVideos((prev) => (offset === 0 ? list : [...prev, ...list]))
        setVideosDone(list.length < PAGE)
      } catch (e) {
        setVideosError(e)
      } finally {
        setVideosLoading(false)
      }
    },
    [uid],
  )

  const loadDynamics = useCallback(
    async (offset = 0) => {
      setDynLoading(true)
      setDynError(null)
      try {
        const list = await getDynamics(uid, PAGE, offset)
        setDynamics((prev) => (offset === 0 ? list : [...prev, ...list]))
        setDynDone(list.length < PAGE)
      } catch (e) {
        setDynError(e)
      } finally {
        setDynLoading(false)
      }
    },
    [uid],
  )

  useEffect(() => {
    setVideos([])
    setDynamics([])
    loadVideos(0)
    loadDynamics(0)
  }, [loadVideos, loadDynamics])

  const isVideos = tab === 'videos'

  return (
    <div className="container">
      <Link className="back-link" to="/">
        ← 返回总览
      </Link>
      <div className="detail-head">
        {upper?.face ? (
          <img className="avatar" src={upper.face} alt={upper.name} referrerPolicy="no-referrer" />
        ) : (
          <div className="avatar-fallback">{(upper?.name || '?').slice(0, 1)}</div>
        )}
        <div>
          <h2 className="page-title">{upper?.name || `UID ${uid}`}</h2>
          <div className="page-sub">
            UID {uid}
            {upper?.last_crawl_ts ? ` · 上次爬取 ${fmtAgo(upper.last_crawl_ts)}` : ''}
          </div>
          <Link className="research-source" to={`/research?uid=${encodeURIComponent(uid)}`}>查看该UP主的自动观点分析 →</Link>
          {canManage && <button className="btn" disabled={backfilling} onClick={async () => {
            setBackfilling(true)
            try {
              const result = await postCrawl(uid, true)
              toast(result.message || '已提交历史补齐任务')
            } catch (error) {
              toast(error.message, 'error')
            } finally {
              setBackfilling(false)
            }
          }}>{backfilling ? '提交中…' : '补齐历史内容'}</button>}
        </div>
      </div>

      <div className="tabs">
        <button className={`tab ${isVideos ? 'active' : ''}`} onClick={() => setTab('videos')}>
          视频<span className="count">{videos.length}</span>
        </button>
        <button className={`tab ${!isVideos ? 'active' : ''}`} onClick={() => setTab('dynamics')}>
          动态<span className="count">{dynamics.length}</span>
        </button>
      </div>

      {isVideos ? (
        <>
          <VideoList
            videos={videos}
            loading={videosLoading && videos.length === 0}
            error={videos.length === 0 ? videosError : null}
            onRetry={() => loadVideos(0)}
          />
          {videos.length > 0 && !videosDone && (
            <div className="load-more">
              <button className="btn" disabled={videosLoading} onClick={() => loadVideos(videos.length)}>
                {videosLoading ? '加载中…' : '加载更多'}
              </button>
            </div>
          )}
        </>
      ) : (
        <>
          <DynamicList
            dynamics={dynamics}
            loading={dynLoading && dynamics.length === 0}
            error={dynamics.length === 0 ? dynError : null}
            onRetry={() => loadDynamics(0)}
          />
          {dynamics.length > 0 && !dynDone && (
            <div className="load-more">
              <button className="btn" disabled={dynLoading} onClick={() => loadDynamics(dynamics.length)}>
                {dynLoading ? '加载中…' : '加载更多'}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
