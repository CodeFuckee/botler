// 概览页「添加 Issue」弹窗（issue #92）：仓库卡片右上角按钮打开，
// 表单包含标题（必填）/ 描述（选填）/ 分配人（项目成员下拉，必填，
// 默认选中 agent）/ 标签（仓库已有标签多选，必填，不可新建）。
//
// issue #103：只输入标题时描述直接复制标题内容——输入时实时联动
// （描述为空则跟随标题更新，用户可见）+ 提交时兜底（描述为空则用
// 标题填充，保证最终创建的 issue 描述等于标题）；描述非空时标题
// 改动不会覆盖用户手写内容。
//
// issue #165：标题输入框右侧新增语音输入按钮——点击后通过浏览器
// Web Speech API（SpeechRecognition，Chrome / Edge / Safari 支持）
// 将语音实时转文字填入标题；识别中再点按钮停止；浏览器不支持 /
// 权限拒绝 / 无语音等场景给出中文错误提示；语音填入标题同样走
// issue #103 的「描述为空自动复制标题」联动（键盘输入与语音输入
// 行为一致，统一由 applyTitle 处理）。
//
// 交互约定（与 IssueDrawer / RepoEditModal 一致）：
// - 打开时加载 /api/issues/form-meta/{repo_id}（项目成员 + 项目标签），
//   成员含 agent 时分配人默认选中 agent（用户确认的默认值）；
// - 关闭方式：右上角 × 按钮 / 点击遮罩 / Esc 键；
// - 提交成功回调 onCreated（关闭弹窗并立即刷新 issue 列表）。
import { useEffect, useRef, useState } from 'react'
import { Icon } from './Icon.jsx'
import { api } from '../api.js'

export default function AddIssueModal({ repo, onClose, onCreated }) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [members, setMembers] = useState([])
  const [labels, setLabels] = useState([])
  const [assigneeId, setAssigneeId] = useState('')
  const [selectedLabels, setSelectedLabels] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // issue #165：标题语音输入状态——listening 识别中、speechError 错误提示
  const [listening, setListening] = useState(false)
  const [speechError, setSpeechError] = useState('')
  const titleRef = useRef('')          // 最新标题（描述联动判断用，避免闭包过期）
  const recognitionRef = useRef(null)  // 当前 SpeechRecognition 实例

  // issue #103：标题→描述联动统一入口——描述为空、或描述仍是上次自动
  // 复制的旧标题时跟随新标题；用户手写描述不被覆盖。键盘输入与语音输入
  // （issue #165）共用，保证两种输入方式联动行为一致。
  const applyTitle = (v) => {
    const prevTitle = titleRef.current
    setTitle(v)
    titleRef.current = v
    setDescription((prevDesc) => {
      const d = (prevDesc || '').trim()
      if (!d || d === prevTitle.trim()) return v
      return prevDesc
    })
  }

  // Esc 关闭弹窗（SSR 测试环境无 document 时跳过）
  useEffect(() => {
    if (typeof document === 'undefined') return
    const onKey = (e) => {
      if (e && e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // 卸载时中止进行中的语音识别，避免识别实例泄漏
  useEffect(() => () => {
    const rec = recognitionRef.current
    if (rec) {
      try { rec.abort() } catch (_e) { /* 忽略：识别已结束 */ }
    }
  }, [])

  // 打开时加载成员与标签；成员含 agent 时默认选中（用户确认：必填、默认 agent）
  useEffect(() => {
    let alive = true
    api.get(`/api/issues/form-meta/${repo.repo_id}`).then((d) => {
      if (!alive) return
      setMembers(d.members || [])
      setLabels(d.labels || [])
      const agent = (d.members || []).find((m) => m.username === 'agent')
      if (agent && agent.id != null) setAssigneeId(String(agent.id))
      setLoading(false)
    }).catch((e) => {
      if (!alive) return
      setLoadError(e.message)
      setLoading(false)
    })
    return () => { alive = false }
  }, [repo.repo_id])

  // issue #165：开始语音识别——识别结果实时填入标题（interim 预览、
  // final 确认），识别中按钮显示「停止」态，再次点击可停止。
  const startListening = () => {
    setSpeechError('')
    if (typeof window === 'undefined') return
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SR) {
      setSpeechError('当前浏览器不支持语音输入，请使用 Chrome / Edge / Safari 的最新版本')
      return
    }
    const rec = new SR()
    rec.lang = 'zh-CN'
    rec.interimResults = true
    rec.continuous = false
    rec.onresult = (e) => {
      let interim = ''
      let final = ''
      for (let i = 0; i < e.results.length; i++) {
        const t = e.results[i][0].transcript
        if (e.results[i].isFinal) final += t
        else interim += t
      }
      applyTitle(final + interim)
    }
    rec.onerror = (e) => {
      const map = {
        'not-allowed': '麦克风权限被拒绝，请在浏览器地址栏允许使用麦克风后重试',
        'service-not-allowed': '语音识别服务不可用，请检查浏览器设置',
        'no-speech': '未检测到语音，请靠近麦克风后重试',
        'audio-capture': '未找到可用麦克风，请检查设备连接',
        'network': '语音识别网络错误，请检查网络后重试',
      }
      setSpeechError(map[e.error] || `语音识别失败（${e.error}）`)
    }
    rec.onend = () => {
      setListening(false)
      recognitionRef.current = null
    }
    recognitionRef.current = rec
    setListening(true)
    try {
      rec.start()
    } catch (_err) {
      setListening(false)
      recognitionRef.current = null
      setSpeechError('语音识别启动失败，请重试')
    }
  }

  // issue #165：停止语音识别（识别中再次点击按钮；识别自动结束后
  // onend 也会重置状态，这里幂等处理）
  const stopListening = () => {
    const rec = recognitionRef.current
    recognitionRef.current = null
    if (rec) {
      try { rec.stop() } catch (_e) { /* 忽略：识别已结束 */ }
    }
    setListening(false)
  }

  const toggleListening = () => {
    if (listening) stopListening()
    else startListening()
  }

  const toggleLabel = (name) => {
    setSelectedLabels((prev) => (prev.includes(name)
      ? prev.filter((n) => n !== name)
      : [...prev, name]))
  }

  const submit = async () => {
    setError('')
    const trimmedTitle = title.trim()
    if (!trimmedTitle) {
      setError('标题不能为空')
      return
    }
    if (!assigneeId) {
      setError('请选择分配人')
      return
    }
    if (selectedLabels.length === 0) {
      setError('请至少选择一个标签')
      return
    }
    setBusy(true)
    try {
      // issue #103：描述为空时兜底复制标题，保证「只输入标题」创建的
      // issue 描述等于标题；描述非空时保留用户输入。
      const trimmedDesc = description.trim()
      await api.post('/api/issues', {
        repo_id: repo.repo_id,
        title: trimmedTitle,
        description: trimmedDesc || trimmedTitle,
        assignee_id: Number(assigneeId),
        labels: selectedLabels,
      })
      onCreated()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal add-issue" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>添加 Issue「{repo.repo_name}」</strong>
          <button className="btn modal-close" onClick={onClose} title="关闭"
                  aria-label="关闭弹窗"><Icon name="x" /></button>
        </div>

        {loading ? (
          <p className="muted">加载仓库成员与标签中…</p>
        ) : loadError ? (
          <div className="alert alert-error">{loadError}</div>
        ) : (
          <>
            <label className="edit-field">
              Issue 标题
              <span className="add-issue-title-row">
                <input
                  className="input add-issue-title"
                  placeholder="必填"
                  value={title}
                  onChange={(e) => applyTitle(e.target.value)}
                />
                {/* issue #165：标题语音输入按钮——识别中显示「停止」态，
                    再次点击停止；不支持/异常在下方给出中文提示 */}
                <button
                  type="button"
                  className={listening
                    ? 'btn add-issue-voice listening'
                    : 'btn add-issue-voice'}
                  onClick={toggleListening}
                  title={listening ? '点击停止语音输入' : '语音输入标题'}
                  aria-label={listening ? '停止语音输入' : '语音输入标题'}
                >
                  <Icon name="mic" />
                </button>
              </span>
              {speechError && (
                <span className="add-issue-voice-error" role="alert">
                  {speechError}
                </span>
              )}
            </label>

            <label className="edit-field">
              描述
              <textarea
                className="input add-issue-desc"
                rows={6}
                placeholder="选填"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>

            <label className="edit-field">
              分配人
              <select
                className="input add-issue-assignee"
                value={assigneeId}
                onChange={(e) => setAssigneeId(e.target.value)}
              >
                {assigneeId === '' && <option value="">请选择…</option>}
                {members.map((m) => (
                  <option key={m.id} value={String(m.id)}>
                    {m.name || m.username || m.id}
                  </option>
                ))}
              </select>
            </label>
            <div className="muted small">必填 · 默认选择 agent</div>

            <div className="edit-field">
              标签
              {labels.length === 0 ? (
                <p className="muted">该仓库暂无标签</p>
              ) : (
                <div className="label-picker">
                  {labels.map((l) => (
                    <label key={l.name} className="label-choice">
                      <input
                        type="checkbox"
                        checked={selectedLabels.includes(l.name)}
                        onChange={() => toggleLabel(l.name)}
                      />
                      <span
                        className="label-pill"
                        style={l.color
                          ? { background: `#${l.color}`, color: `#${l.text_color}` }
                          : undefined}
                      >
                        {l.name}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </div>
            <div className="muted small">必填 · 仅可选仓库已有标签</div>

            {error && <div className="alert alert-error">{error}</div>}

            <div className="modal-footer">
              <button className="btn" onClick={onClose}>取消</button>
              <button className="btn btn-primary add-issue-submit"
                      disabled={busy} onClick={submit}>
                {busy ? '创建中…' : '创建 Issue'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
