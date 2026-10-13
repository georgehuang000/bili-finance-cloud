import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** 仅放行 http/https 协议的链接 */
const SAFE_HREF = /^https?:\/\//i

/**
 * 安全链接渲染:
 * - http/https 链接 -> 新标签页打开,带 noopener noreferrer
 * - 其他协议(javascript:/data:/vbscript: 等)-> 渲染为纯文本,不产生可点击链接
 */
function SafeLink({ href, children }) {
  if (typeof href === 'string' && SAFE_HREF.test(href)) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    )
  }
  return <span>{children}</span>
}

function ScrollableTable({ children }) {
  return (
    <div className="md-table-scroll" role="region" aria-label="数据表格，可横向滚动" tabIndex={0}>
      <table>{children}</table>
    </div>
  )
}

const REMARK_PLUGINS = [remarkGfm]
const COMPONENTS = { a: SafeLink, table: ScrollableTable }

/**
 * 统一的安全 Markdown 渲染组件。
 * 所有渲染 LLM 输出等不可信 Markdown 的地方都应使用本组件,
 * 而不要直接使用 ReactMarkdown。
 */
export default function SafeMarkdown({ children }) {
  return <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={COMPONENTS}>{children}</ReactMarkdown>
}
