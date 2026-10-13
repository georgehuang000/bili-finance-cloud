import { useState } from 'react'
import { addUpper, searchUppers } from '../api'
import { fmtPlay } from '../utils'
import { useToast } from './Toast'

/**
 * 订阅管理面板:输入 UP 主名字(关键词搜索)或 UID(纯数字直查)加入订阅。
 * 搜索结果列出候选,点击「订阅」后后端会立即在后台抓取该 UP 主一轮。
 */
export default function SubscribePanel({ onChanged }) {
  const toast = useToast()
  const [q, setQ] = useState('')
  const [searching, setSearching] = useState(false)
  const [candidates, setCandidates] = useState(null) // null=还没搜索过
  const [addingUid, setAddingUid] = useState(null)

  const doSearch = async (e) => {
    e.preventDefault()
    const kw = q.trim()
    if (!kw) return
    setSearching(true)
    try {
      const res = await searchUppers(kw)
      setCandidates(res.candidates || [])
      if ((res.candidates || []).length === 0) {
        // 关键词搜索在风控下可能拿不到结果，UID 直查更稳，提示用户兜底
        toast(
          /^\d+$/.test(kw)
            ? '未找到该 UID 对应的 UP 主'
            : '没有找到相关 UP 主,换个关键词试试;若确有此博主,可直接输入其 UID 搜索'
        )
      }
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setSearching(false)
    }
  }

  const doAdd = async (u) => {
    setAddingUid(u.uid)
    try {
      const res = await addUpper(u.uid, u.name)
      toast(res.message || `已订阅 ${u.name}`)
      if (!res.already) {
        setCandidates(null)
        setQ('')
        onChanged?.()
      }
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setAddingUid(null)
    }
  }

  return (
    <div className="subscribe">
      <form className="sub-bar" onSubmit={doSearch}>
        <input
          className="sub-input"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="输入 UP 主名字或 UID 回车搜索,如「莫大韭菜」或 525121722"
          disabled={searching}
        />
        <button className="btn primary" type="submit" disabled={searching || !q.trim()}>
          {searching ? '搜索中…' : '搜索'}
        </button>
      </form>

      {candidates && candidates.length > 0 && (
        <div className="sub-candidates">
          {candidates.map((u) => (
            <div className="sub-candidate" key={u.uid}>
              {u.face ? (
                <img
                  className="avatar"
                  src={u.face}
                  alt={u.name}
                  referrerPolicy="no-referrer"
                />
              ) : (
                <div className="avatar-fallback">{(u.name || '?').slice(0, 1)}</div>
              )}
              <div className="sub-cand-info">
                <div className="sub-cand-name">
                  {u.name}
                  <span className="sub-cand-uid">UID {u.uid}</span>
                </div>
                <div className="sub-cand-meta">
                  粉丝 {fmtPlay(u.fans)}
                  {u.sign ? ` · ${u.sign}` : ''}
                </div>
              </div>
              <button
                className="btn"
                disabled={addingUid === u.uid}
                onClick={() => doAdd(u)}
              >
                {addingUid === u.uid ? '添加中…' : '订阅'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
