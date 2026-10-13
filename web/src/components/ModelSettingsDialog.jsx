import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { getLlmSettings, listLlmModels, saveLlmSettings, testLlm } from '../api'
import { useToast } from './Toast'

/**
 * 模型设置弹窗：填 API Key / 切服务商 / 选模型，保存后写回 .env 并立刻生效。
 *
 * 安全约定：密钥只以掩码形式展示（sk-***E768），原文永远不从后端返回；
 * 密钥栏留空 = 不修改现有密钥。
 *
 * 弹窗用 portal 挂到 body：顶栏有 backdrop-filter，会把自己变成 position:fixed
 * 后代的包含块，弹窗留在顶栏内会"卡在最顶上"（扫码登录弹窗踩过这个坑）。
 */
export default function ModelSettingsDialog({ open, onClose, onSaved }) {
  const toast = useToast()

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [fetchingModels, setFetchingModels] = useState(false)

  const [meta, setMeta] = useState(null) // 后端返回的预设/掩码等元信息
  const [provider, setProvider] = useState('deepseek')
  const [apiKey, setApiKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [visionModel, setVisionModel] = useState('')
  const [thinking, setThinking] = useState('disabled')
  const [visionEnabled, setVisionEnabled] = useState(true)
  const [visionMax, setVisionMax] = useState(20)
  const [models, setModels] = useState([])
  const [result, setResult] = useState(null) // {ok, text}

  const presetOf = useCallback(
    (key) => (meta?.providers || []).find((p) => p.key === key) || null,
    [meta]
  )

  // 打开时拉取当前配置
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setResult(null)
    setApiKey('')
    setShowKey(false)
    setModels([])

    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    getLlmSettings()
      .then((s) => {
        if (cancelled) return
        setMeta(s)
        setProvider(s.provider || 'deepseek')
        setBaseUrl(s.base_url || '')
        setModel(s.model || '')
        setVisionModel(s.vision_model || '')
        setThinking(s.thinking || 'disabled')
        setVisionEnabled(s.vision_enabled !== false)
        setVisionMax(s.vision_max_images ?? 20)
      })
      .catch((e) => {
        if (!cancelled) setResult({ ok: false, text: e.message || '读取失败' })
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
      document.body.style.overflow = prevOverflow
    }
  }, [open])

  // ESC 关闭
  useEffect(() => {
    if (!open) return
    const onKey = (e) => {
      if (e.key === 'Escape') onClose?.()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  // 切预设：把该服务商的默认地址/模型填进表单（密钥与其它字段不动）
  const pickProvider = (key) => {
    setProvider(key)
    const p = presetOf(key)
    if (!p) return
    if (p.base_url) setBaseUrl(p.base_url)
    if (p.model) setModel(p.model)
    setVisionModel(p.model || '')
    setResult(null)
  }

  const payload = () => ({
    provider,
    api_key: apiKey.trim(),
    base_url: baseUrl.trim(),
    model: model.trim(),
    vision_model: visionModel.trim(),
    thinking,
    vision_enabled: visionEnabled,
    vision_max_images: Number(visionMax) || 0,
  })

  const handleTest = async (withVision) => {
    setTesting(true)
    setResult(null)
    try {
      const r = await testLlm({
        api_key: apiKey.trim(),
        base_url: baseUrl.trim(),
        model: (withVision ? visionModel : model).trim(),
        vision: withVision,
        thinking,
      })
      if (r.ok) {
        setResult({
          ok: true,
          text: `${withVision ? '图片识别' : '文本'}连通正常 · 模型 ${r.model} · 耗时 ${r.latency_ms}ms${
            r.reply ? ` · 返回「${r.reply}」` : ''
          }`,
        })
      } else {
        setResult({ ok: false, text: r.error || '测试失败' })
      }
    } catch (e) {
      setResult({ ok: false, text: e.message || '测试失败' })
    } finally {
      setTesting(false)
    }
  }

  const handleFetchModels = async () => {
    setFetchingModels(true)
    setResult(null)
    try {
      const r = await listLlmModels({ api_key: apiKey.trim(), base_url: baseUrl.trim() })
      if (r.ok) {
        setModels(r.models || [])
        setResult({ ok: true, text: `拉到 ${r.models.length} 个可用模型，点下面的名字即可选用` })
      } else {
        setResult({ ok: false, text: r.error || '拉取模型列表失败' })
      }
    } catch (e) {
      setResult({ ok: false, text: e.message || '拉取模型列表失败' })
    } finally {
      setFetchingModels(false)
    }
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const r = await saveLlmSettings(payload())
      setMeta(r)
      setApiKey('') // 保存后清空密钥栏，回落到掩码展示
      const extra = r.warning ? `（${r.warning}）` : ''
      toast(`模型设置已保存并立即生效${extra}`, r.warning ? 'error' : undefined)
      setResult({
        ok: !r.warning,
        text: r.warning
          ? r.warning
          : `设置已保存　当前模型 ${r.model}`,
      })
      onSaved?.()
    } catch (e) {
      setResult({ ok: false, text: e.message || '保存失败' })
    } finally {
      setSaving(false)
    }
  }

  if (!open) return null

  const preset = presetOf(provider)
  const visionLikelyBad = /pro/i.test(visionModel || '')

  return createPortal(
    <div className="modal-mask" onClick={onClose}>
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-label="模型设置"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3 className="modal-title">模型设置</h3>
          <button className="modal-close" onClick={onClose} title="关闭" aria-label="关闭">
            ✕
          </button>
        </div>

        <div className="modal-body">
          {loading ? (
            <div className="login-msg">
              <span className="spinner" />
              正在读取当前配置…
            </div>
          ) : (
            <>
              {/* 服务商 */}
              <div className="field">
                <label className="field-label">服务商</label>
                <div className="preset-row">
                  {(meta?.providers || []).map((p) => (
                    <button
                      key={p.key}
                      className={`preset-chip ${provider === p.key ? 'active' : ''}`}
                      onClick={() => pickProvider(p.key)}
                      type="button"
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
                {preset?.note ? <div className="field-hint">{preset.note}</div> : null}
              </div>

              {/* API Key */}
              <div className="field">
                <label className="field-label" htmlFor="llm-api-key">
                  API Key
                  {meta?.key_set ? (
                    <span className="field-badge">
                      已保存 {meta.key_masked}
                      {meta.key_source ? `（来自 .env 的 ${meta.key_source}）` : ''}
                    </span>
                  ) : (
                    <span className="field-badge warn">未配置</span>
                  )}
                </label>
                <div className="key-row">
                  <input
                    id="llm-api-key"
                    className="input"
                    type={showKey ? 'text' : 'password'}
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder={meta?.key_set ? '留空 = 不修改现有密钥' : '粘贴你的 API Key'}
                    autoComplete="off"
                    spellCheck="false"
                  />
                  <button className="btn" type="button" onClick={() => setShowKey((v) => !v)}>
                    {showKey ? '隐藏' : '显示'}
                  </button>
                </div>
                <div className="field-hint">
                  密钥只保存在服务器上的 .env 里，接口永远不会把原文发回浏览器。
                  {preset?.key_url ? (
                    <>
                      {' '}
                      申请地址：
                      <a href={preset.key_url} target="_blank" rel="noreferrer">
                        {preset.key_url}
                      </a>
                    </>
                  ) : null}
                </div>
              </div>

              {/* base_url */}
              <div className="field">
                <label className="field-label" htmlFor="llm-base-url">
                  接口地址（base_url）
                </label>
                <input
                  id="llm-base-url"
                  className="input"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://api.deepseek.com/v1"
                  spellCheck="false"
                />
              </div>

              {/* 模型 */}
              <div className="field">
                <label className="field-label" htmlFor="llm-model">
                  解读模型
                </label>
                <div className="key-row">
                  <input
                    id="llm-model"
                    className="input"
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    placeholder="deepseek-flash"
                    spellCheck="false"
                    list="llm-model-options"
                  />
                  <button
                    className="btn"
                    type="button"
                    onClick={handleFetchModels}
                    disabled={fetchingModels}
                  >
                    {fetchingModels ? <span className="spinner" /> : null}
                    拉取可用模型
                  </button>
                </div>
                <datalist id="llm-model-options">
                  {models.map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
                {models.length ? (
                  <div className="model-chips">
                    {models.map((m) => (
                      <button
                        key={m}
                        type="button"
                        className={`model-chip ${model === m ? 'active' : ''}`}
                        onClick={() => setModel(m)}
                      >
                        {m}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>

              {/* 图片识别模型 */}
              <div className="field">
                <label className="field-label" htmlFor="llm-vision-model">
                  图片识别模型
                </label>
                <input
                  id="llm-vision-model"
                  className="input"
                  value={visionModel}
                  onChange={(e) => setVisionModel(e.target.value)}
                  placeholder="deepseek-flash"
                  spellCheck="false"
                />
                <div className="field-hint">
                  {visionLikelyBad
                    ? '⚠ v4-pro 不支持图片识别，图片任务会失败'
                    : 'DeepSeek 目前只有 deepseek-flash 支持读图'}
                </div>
              </div>

              {/* 思考模式 */}
              <div className="field">
                <label className="field-label">思考模式</label>
                <div className="preset-row">
                  {[
                    ['disabled', '关闭（推荐）'],
                    ['low', '低'],
                    ['high', '高'],
                    ['max', '最高'],
                  ].map(([val, label]) => (
                    <button
                      key={val}
                      type="button"
                      className={`preset-chip ${thinking === val ? 'active' : ''}`}
                      onClick={() => setThinking(val)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <div className="field-hint">
                  DeepSeek 默认开启思考且强度为 high，对"把字幕压成摘要"又慢又贵。
                  日常摘要建议关闭；想让日报多做推演再调高。
                  <br />
                  注意：<b>图片识别固定不使用思考模式</b>（实测同一张真实封面：
                  关闭思考 1.9 秒返回 195 字且数字全抄对；开到最高要 69 秒、输出
                  token 多 140 倍，而且正文还会被推理吃光变成空）。
                </div>
              </div>

              {/* 图片识别 */}
              <div className="field">
                <label className="field-label">图片识别（读图）</label>
                <div className="preset-row">
                  <button
                    type="button"
                    className={`preset-chip ${visionEnabled ? 'active' : ''}`}
                    onClick={() => setVisionEnabled(true)}
                  >
                    开启
                  </button>
                  <button
                    type="button"
                    className={`preset-chip ${!visionEnabled ? 'active' : ''}`}
                    onClick={() => setVisionEnabled(false)}
                  >
                    关闭
                  </button>
                  <span className="vision-cap">
                    每轮最多
                    <input
                      id="llm-vision-max"
                      className="input vision-cap-input"
                      type="number"
                      min="0"
                      max="200"
                      value={visionMax}
                      onChange={(e) => setVisionMax(e.target.value)}
                      disabled={!visionEnabled}
                    />
                    张
                  </span>
                </div>
                <div className="field-hint">
                  让模型读视频封面和动态配图，把画面里的文字（板块名、研报标题、数据）
                  抽出来喂给日报。实测：动态配图在 feed 里多数拿不到（充电专属会被剥离），
                  所以实际主要作用在<b>视频封面</b>上；封面常常写着当期最核心的观点。
                </div>
                {visionEnabled && meta?.vision_capable === false ? (
                  <div className="field-hint vision-warn">
                    ⚠ 当前服务商/模型不支持读图，开了也不会生效：不支持读图的网关
                    收到图片会直接返回 HTTP 400。请切到 DeepSeek 官方（deepseek-flash）
                    并点「测试图片识别」确认。
                  </div>
                ) : null}
              </div>

              {/* 操作区 */}
              <div className="llm-actions">
                <button className="btn" type="button" onClick={() => handleTest(false)} disabled={testing}>
                  {testing ? <span className="spinner" /> : null}
                  测试连接
                </button>
                <button className="btn" type="button" onClick={() => handleTest(true)} disabled={testing}>
                  测试图片识别
                </button>
                <button className="btn primary" type="button" onClick={handleSave} disabled={saving}>
                  {saving ? <span className="spinner" /> : null}
                  保存并生效
                </button>
              </div>

              {result ? (
                <div className={`llm-result ${result.ok ? 'ok' : 'err'}`}>{result.text}</div>
              ) : null}

              {meta?.env_path ? (
                <div className="field-hint llm-env-path">
                  保存后写入：{meta.env_path}（密钥只留在服务器本地，不进 git）
                </div>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>,
    document.body
  )
}
