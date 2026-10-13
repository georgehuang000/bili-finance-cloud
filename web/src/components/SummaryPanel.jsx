import SafeMarkdown from './SafeMarkdown'
import { PERIOD_LABEL } from '../utils'

/** 单条总结面板:日期 + 时段标签 + 模型名 + Markdown 正文 */
export default function SummaryPanel({ summary }) {
  if (!summary) return null
  return (
    <div className="panel summary-card">
      <div className="summary-head">
        <span className="summary-date">{summary.date}</span>
        <span className="tag accent">{PERIOD_LABEL[summary.period] || summary.period}</span>
        {summary.model && <span className="summary-model">{summary.model}</span>}
      </div>
      <div className="md">
        <SafeMarkdown>{summary.content || ''}</SafeMarkdown>
      </div>
    </div>
  )
}
