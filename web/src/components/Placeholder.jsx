function EmptyIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 8 12 3 3 8v8l9 5 9-5V8Z" />
      <path d="M3 8l9 5 9-5" />
      <path d="M12 13v8" />
    </svg>
  )
}

function ErrorIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7.5V13" />
      <path d="M12 16.5v.01" />
    </svg>
  )
}

/** 空数据 / 请求失败的友好占位,避免白屏 */
export default function Placeholder({ loading, error, empty, emptyText = '暂无数据', onRetry }) {
  if (loading) {
    return (
      <div className="placeholder">
        <span className="icon">
          <span className="spinner" style={{ width: 22, height: 22, borderWidth: 3 }} />
        </span>
        加载中…
      </div>
    )
  }
  if (error) {
    return (
      <div className="placeholder error-box">
        <span className="icon">
          <ErrorIcon />
        </span>
        {error.network ? '后端未启动或网络不可用' : error.message || '请求失败'}
        {onRetry && (
          <div className="retry">
            <button className="btn" onClick={onRetry}>
              重试
            </button>
          </div>
        )}
      </div>
    )
  }
  if (empty) {
    return (
      <div className="placeholder">
        <span className="icon">
          <EmptyIcon />
        </span>
        {emptyText}
      </div>
    )
  }
  return null
}
