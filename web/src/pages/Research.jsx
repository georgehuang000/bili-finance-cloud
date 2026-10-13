import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { analyzeResearch, getResearch, getUppers, verifyResearch, postCrawl } from '../api'
import { fmtFullTime } from '../utils'
import { useToast } from '../components/Toast'
import { useAccess } from '../access'

function sourceUrl(claim) {
  try {
    const url = new URL(claim.url)
    if (url.protocol !== 'https:') return undefined
    if (claim.video_seconds != null) url.searchParams.set('t', claim.video_seconds)
    return url.href
  } catch { return undefined }
}

function Source({ claim }) {
  const url = sourceUrl(claim)
  return url ? <a className="research-source" href={url} target="_blank" rel="noopener noreferrer">{claim.video_seconds != null ? '定位视频原文' : '打开原始内容'} ↗</a> : null
}

export default function Research() {
  const { canManage, cloud, testMode } = useAccess()
  const readOnly = !canManage
  const [params, setParams] = useSearchParams()
  const uid = params.get('uid') || ''
  const toast = useToast()
  const [uppers, setUppers] = useState([])
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [topic, setTopic] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [tab, setTab] = useState('views')
  const [url, setUrl] = useState('')
  const [verifyPending, setVerifyPending] = useState(false)
  const generation = useRef(0)
  const selectedRef = useRef(null)

  useEffect(() => { getUppers().then(setUppers).catch(e => setError(e.message)) }, [])
  useEffect(() => {
    let active = true
    setData(null); setTopic(''); setSelectedId(null); setError('')
    const refresh = async () => {
      const requestId = ++generation.current
      try {
        const result = await getResearch(uid)
        if (active && requestId === generation.current) { setData(result); setError('') }
      } catch (e) { if (active && requestId === generation.current) setError(e.message) }
    }
    refresh()
    const timer = setInterval(refresh, readOnly ? 30000 : 6000)
    return () => { active = false; clearInterval(timer); ++generation.current }
  }, [uid, readOnly])

  const topics = useMemo(() => [...new Set((data?.claims || []).map(c => c.topic))], [data])
  const claims = useMemo(() => (data?.claims || []).filter(c => !topic || c.topic === topic), [data, topic])
  const selected = claims.find(c => c.id === selectedId) || claims[0]
  selectedRef.current = selected?.id
  const changes = claims.filter(c => c.change && c.previous)
  useEffect(() => { setUrl('') }, [selected?.id])

  const run = async () => {
    setPending(true)
    try { const result = await analyzeResearch(uid); toast(result.message) }
    catch (e) { toast(e.message, 'error') }
    finally { setPending(false) }
  }
  const verify = async event => {
    event.preventDefault()
    if (!selected) return
    const claimId = selected.id
    setVerifyPending(true)
    try { const result = await verifyResearch(claimId, url); toast(result.message); if (selectedRef.current === claimId) setUrl('') }
    catch (e) { toast(e.message, 'error') }
    finally { setVerifyPending(false) }
  }
  const jobs = data?.evidence_jobs || []
  const busyEvidence = jobs.some(j => j.status === 'running')
  const currentJobs = jobs.filter(j => j.claim_id === selected?.id)
  const lastRun = data?.status?.last_run

  return <div className="container research-page">
    <div className="research-head">
      <div><h1 className="page-title">自动观点研究</h1><p className="page-sub">看原始观点、跟踪变化，再核对依据</p></div>
      {!readOnly && <button className="btn primary" onClick={run} disabled={pending || data?.status?.running || (cloud && testMode && !uid)}>{data?.status?.running ? '等待或分析中…' : cloud && testMode ? '测试一条素材' : '分析已有素材'}</button>}
      {cloud && testMode && canManage && <button className="btn" disabled={!uid || pending} onClick={async () => { try { const result = await postCrawl(uid); toast(result.message) } catch (error) { toast(error.message, 'error') } }}>采集此UP主</button>}
    </div>
    {cloud && testMode && canManage && <p className="research-auto-note">测试阶段：先选择一位 UP 主，每次最多分析一条尚未分析的素材。</p>}
    <div className="research-filters">
      <label>UP 主<select aria-label="切换UP主" value={uid} onChange={e => setParams(e.target.value ? { uid: e.target.value } : {})}>
        <option value="">全部 UP 主</option>{uppers.map(u => <option key={u.uid} value={u.uid}>{u.name}</option>)}
      </select></label>
      <label>主题<select aria-label="筛选主题" value={topic} onChange={e => { setTopic(e.target.value); setSelectedId(null) }}><option value="">全部主题</option>{topics.map(t => <option key={t}>{t}</option>)}</select></label>
      <p className="research-auto-note">{readOnly ? '查看已发布的自动分析' : data?.status?.automatic === false ? '自动分析未开启，可手动分析已有素材' : '每轮采集后自动分析新素材'} · 切换仅查看结果</p>
    </div>
    {!readOnly && <div className="research-run" aria-live="polite">
      {data?.status?.running ? <span>正在分析 {data.status.current_author || '已有素材'} · 本轮已处理 {data.status.processed} 条</span> : lastRun ? <span>最近任务（{lastRun.uid ? uppers.find(u=>u.uid===lastRun.uid)?.name || '单个UP主' : '全部UP主'}）{fmtFullTime(lastRun.finished_ts || lastRun.started_ts)} · 已处理 {lastRun.processed} 条素材{lastRun.failed ? ` · ${lastRun.failed} 条失败，稍后自动重试` : ''}{lastRun.status === 'interrupted' ? ' · 上次任务被中断，将增量续跑' : ''}</span> : <span>新内容会自动进入分析；也可补分析已有素材。</span>}
      {data?.status?.configured === false && <span className="research-warning">请先在模型设置中配置分析模型。</span>}
    </div>}
    {error && <div className="research-error" role="alert">{error}</div>}
    <div className="tabs" role="tablist" aria-label="观点研究步骤">
      {[['views','观点对照'],['changes','变化记录'],['research','研究核验']].map(([key,label]) => <button id={`research-tab-${key}`} aria-controls={`research-panel-${key}`} aria-selected={tab===key} role="tab" className={`tab ${tab===key?'active':''}`} key={key} onClick={() => setTab(key)}>{label}</button>)}
    </div>
    {!data && !error && <p className="research-empty">正在读取分析结果…</p>}
    {data && !claims.length && <div className="research-empty"><h2>当前范围还没有可展示的观点</h2><p>{readOnly ? '后台分析完成后会在这里展示。' : '可点击“分析已有素材”。'}只有有足够正文且原文引用可定位的结果才会保存。</p>{!readOnly && data.status.errors?.length > 0 && <p className="research-warning">最近错误：{data.status.errors[0].error}</p>}</div>}
    {!!claims.length && <>
      <div className="research-layout">
        <aside className="research-list" aria-label="选择观点">
          <p className="research-list-count">当前范围 {claims.length} 条观点 · 按发表时间排序</p>
          {claims.map(c => <button key={c.id} className={`research-choice ${selected?.id===c.id?'selected':''}`} aria-pressed={selected?.id===c.id} onClick={() => setSelectedId(c.id)}><span className="research-choice-meta">{c.author}<span>{fmtFullTime(c.pub_ts)}</span></span><strong>{c.topic}</strong><span>{c.claim}</span><span className="research-choice-tags">{c.market} · {c.kind} · {c.stance}</span></button>)}
        </aside>
        <main className="research-detail">
          {tab === 'views' && selected && <section id="research-panel-views" role="tabpanel" aria-labelledby="research-tab-views">
            <div className="research-detail-title"><h2>{selected.author}的判断</h2><span>{selected.kind}</span></div>
            <dl><dt>核心观点</dt><dd>{selected.claim}</dd><dt>作用对象</dt><dd>{selected.subject} · {selected.market}</dd><dt>适用期限</dt><dd>{selected.horizon}</dd><dt>作者依据</dt><dd>{selected.rationale.length ? selected.rationale.join('；') : '原文未明确给出'}</dd><dt>证据状态</dt><dd>{selected.evidence_status}</dd></dl>
            <blockquote>{selected.quote}</blockquote><p className="research-caption">{selected.source_kind==='video'?'保存的字幕节选，可能有识别错误':'保存的动态原文'}{selected.source_kind==='video' && selected.video_seconds==null?' · 该历史素材没有视频时间戳':''}{selected.truncated?' · 分析素材已截断':''}</p>
            <div className="research-actions"><Source claim={selected}/><button className="btn" onClick={() => setTab('changes')}>查看变化</button><button className="btn primary" onClick={() => setTab('research')}>核验依据</button></div>
            <p className="research-caption">来源：{selected.title} · 模型：{selected.model} · 分析时间：{fmtFullTime(selected.created_ts)}</p>
          </section>}
          {tab === 'changes' && <section id="research-panel-changes" role="tabpanel" aria-labelledby="research-tab-changes"><h2>当前范围的历史变化</h2><p className="research-caption">只比较同一作者此前发表的观点，保留前后原文。变化类型是模型判断。</p>{!changes.length && <p className="research-empty">暂无有可比历史依据的变化记录。随着新内容分析，记录会逐步补充。</p>}{changes.map(c => <article className="research-change" key={c.id}><div className="research-detail-title"><h3>{c.author} · {c.topic}</h3><span>{c.change.kind}</span></div><p>{c.change.summary}</p><div className="research-before-after"><div><p className="research-caption">此前 · {fmtFullTime(c.previous.pub_ts)}</p><p>{c.previous.claim}</p><blockquote>{c.previous.quote}</blockquote><Source claim={c.previous}/></div><div><p className="research-caption">本次 · {fmtFullTime(c.pub_ts)}</p><p>{c.claim}</p><blockquote>{c.quote}</blockquote><Source claim={c}/></div></div></article>)}</section>}
          {tab === 'research' && selected && <section id="research-panel-research" role="tabpanel" aria-labelledby="research-tab-research"><h2>{selected.topic}的证据核验</h2><p className="research-caption">核验对象：{selected.author}“{selected.claim}”</p><h3 className="research-section-title">自动整理的待核验事项</h3>{selected.checks.length ? <ul className="research-checks">{selected.checks.map((check,i)=><li key={i}>{check}</li>)}</ul> : <p>尚未提取到明确核验条件。</p>}
            {!readOnly && <form className="research-evidence-form" onSubmit={verify}><label htmlFor="research-evidence-url">补充公告或财报链接</label><input id="research-evidence-url" type="url" placeholder="https://…（网页或有文字层的PDF）" required value={url} onChange={e => setUrl(e.target.value)} maxLength={2000}/><button className="btn primary" disabled={verifyPending || busyEvidence || !url.trim()}>读取资料并自动核验</button><p className="research-caption">支持沪深交易所、巨潮、港交所、SEC及部分公司官网。核验当前资料时会标明时间，不能直接代替历史证据。</p></form>}
            {currentJobs.map(job=><p key={job.id} className={job.status==='failed'?'research-warning':'research-caption'} role={job.status==='failed'?'alert':undefined}>{job.status==='running'?'正在读取资料并核验…':job.message}</p>)}
            <h3 className="research-section-title">已保存的核验结果</h3>{!selected.evidence.length && <p className="research-empty">尚无外部文档核验结果。作者说法保持为未独立核验。</p>}{selected.evidence.map(e=><article className="research-evidence" key={e.id}><div className="research-detail-title"><h3>{e.status}</h3><span>{fmtFullTime(e.created_ts)}</span></div><p>{e.summary}</p>{e.quote && <blockquote>{e.quote}</blockquote>}<p className="research-caption">报告期或时间：{e.period} · {e.source_label}</p>{e.limitations && <p className="research-warning">限制：{e.limitations}</p>}<p className="research-caption">{e.extraction_note}</p><p className="research-caption">{e.historical_warning}</p><a className="research-source" href={e.url} target="_blank" rel="noopener noreferrer">查看核验文档 ↗</a></article>)}
          </section>}
        </main>
      </div>
    </>}
  </div>
}
