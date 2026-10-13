import { useCallback, useEffect, useState } from 'react'
import { getSummaries } from '../api'
import SummaryPanel from '../components/SummaryPanel'
import Placeholder from '../components/Placeholder'

/** 历史总结页:默认最近 10 条,可按日期筛选 */
export default function SummaryHistory() {
  const [date, setDate] = useState('') // 空 = 最近 10 条
  const [summaries, setSummaries] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async (d) => {
    setLoading(true)
    setError(null)
    try {
      setSummaries(await getSummaries(d || undefined))
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load(date)
  }, [date, load])

  return (
    <div className="container">
      <div className="page-head">
        <h2 className="page-title">
          历史<span className="accent">总结</span>
        </h2>
        <span className="page-sub">{date ? date : 'RECENT 10 REPORTS'}</span>
      </div>

      <div className="date-filter">
        <input
          type="date"
          className="date-input"
          value={date}
          onChange={(e) => setDate(e.target.value)}
          aria-label="按日期筛选"
        />
        {date && (
          <button className="btn" onClick={() => setDate('')}>
            清除筛选
          </button>
        )}
      </div>

      {loading || error || summaries.length === 0 ? (
        <div className="panel">
          <Placeholder
            loading={loading}
            error={error}
            empty={summaries.length === 0}
            emptyText={date ? `${date} 没有总结记录` : '暂无历史总结'}
            onRetry={() => load(date)}
          />
        </div>
      ) : (
        <div className="history-list">
          {summaries.map((s) => (
            <SummaryPanel key={s.id} summary={s} />
          ))}
        </div>
      )}
    </div>
  )
}
