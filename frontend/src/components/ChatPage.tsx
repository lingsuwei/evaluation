import { useState, useEffect, useCallback, useRef } from 'react'
import axios from 'axios'
import { EvalDraftPreviewCard, DraftFields } from './EvalDraftPreviewCard'
import { ToolCallBubble } from './ToolCallBubble'
import { ConfirmCard, PendingConfirm } from './ConfirmCard'

const API_BASE = '/api'

/**
 * AI 对话式配置页（阶段 2：完整对话模式 + 阶段 1 快速模式）。
 *
 * 对话模式：输入消息 → POST /messages → SSE /stream → 流式渲染工具调用 + 最终文本 → 展示草稿卡片
 * 快速模式：输入意图 → POST /interpret → 同步等草稿
 */

interface ChatSessionItem {
  id: string
  title: string | null
  status: string | null
  intent: string | null
  model_key: string | null
  confidence: number | null
  created_at: string | null
  updated_at: string | null
}

interface ChatSessionDetail extends ChatSessionItem {
  intent_text?: string | null
  draft_request?: DraftFields | null
  missing_fields?: string[]
  error_message?: string | null
}

interface ChatMessageItem {
  id: number
  session_id: string
  role: string
  content: string | null
  tool_name: string | null
  tool_args: any
  tool_result: any
  tokens: number | null
  created_at: string | null
}

interface ChatLine {
  id: number
  role: 'user' | 'assistant' | 'tool'
  content: string
  type: 'message' | 'tool_call' | 'tool_result' | 'error' | 'phase'
  toolName?: string
  toolArgs?: Record<string, any>
  toolResult?: any
  round?: number
}

interface Props {
  onApplyToEvalPage: (draft: Record<string, any>, onlineMode: boolean) => void
  /** launch_eval 确认成功后跳转到评估详情页 */
  onNavigateToDetail?: (taskId: string) => void
}

function safeParseSSE(e: MessageEvent, label: string): any | null {
  try {
    return JSON.parse(e.data)
  } catch (err) {
    console.error(`SSE ${label} parse error:`, err)
    return null
  }
}

const SAMPLE_INTENTS = [
  '用 GLM 评估 agent-001 在数据集 ds_faq 上的忠实度和正确性，跑3轮',
  '帮我注册一个新 Agent，推理地址 http://...',
  '添加一个 GLM-4.7 的 LLM 模型配置',
  '我要做忠实度评估，帮我推荐合适的指标',
]

export function ChatPage({ onApplyToEvalPage, onNavigateToDetail }: Props) {
  const [sessions, setSessions] = useState<ChatSessionItem[]>([])
  const [sessionsTotal, setSessionsTotal] = useState(0)
  const [loadingSessions, setLoadingSessions] = useState(false)
  const [currentId, setCurrentId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ChatSessionDetail | null>(null)

  const [inputText, setInputText] = useState('')
  const [chatLines, setChatLines] = useState<ChatLine[]>([])
  const [streaming, setStreaming] = useState(false)
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(null)

  const eventSourceRef = useRef<EventSource | null>(null)
  const lineIdRef = useRef(0)
  const sseRetryRef = useRef(0)
  const chatScrollRef = useRef<HTMLDivElement | null>(null)
  // 流式 chunk 跟踪：当前 round 是否已有正在流的 assistant 行
  const streamingLineRef = useRef<{ round: number; lineId: number } | null>(null)

  const appendLine = useCallback((line: Omit<ChatLine, 'id'>) => {
    setChatLines(prev => [...prev, { ...line, id: lineIdRef.current++ }])
  }, [])

  // 流式增量更新最后一条 assistant 行（按 round 匹配）
  const appendToStreamingLine = useCallback((round: number, delta: string) => {
    if (!delta) return
    setChatLines(prev => {
      // 已有正在流的行且 round 匹配 → 追加
      if (streamingLineRef.current && streamingLineRef.current.round === round) {
        const targetId = streamingLineRef.current.lineId
        return prev.map(l =>
          l.id === targetId ? { ...l, content: (l.content || '') + delta } : l
        )
      }
      // 否则新建一条 assistant 行
      const newId = lineIdRef.current++
      streamingLineRef.current = { round, lineId: newId }
      return [...prev, { id: newId, role: 'assistant', content: delta, type: 'message' as const }]
    })
  }, [])

  // chat_message 定稿：用完整 content 替换流式行（防止 chunk 拼接与最终不一致）
  const finalizeStreamingLine = useCallback((round: number, finalContent: string) => {
    if (streamingLineRef.current && streamingLineRef.current.round === round) {
      const targetId = streamingLineRef.current.lineId
      setChatLines(prev => prev.map(l =>
        l.id === targetId ? { ...l, content: finalContent } : l
      ))
      streamingLineRef.current = null
    } else {
      // 没有流式行（如重试提示），照旧追加
      appendLine({ role: 'assistant', content: finalContent, type: 'message' })
    }
  }, [appendLine])

  const fetchSessions = useCallback(async () => {
    setLoadingSessions(true)
    try {
      const res = await axios.get(`${API_BASE}/chat/sessions`, { params: { page: 1, page_size: 50 } })
      setSessions(res.data.items || [])
      setSessionsTotal(res.data.total || 0)
    } catch (e: any) {
      setError(`加载会话列表失败: ${e.message || e}`)
    } finally {
      setLoadingSessions(false)
    }
  }, [])

  useEffect(() => {
    fetchSessions()
  }, [fetchSessions])

  // 自动滚动到底部
  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight
    }
  }, [chatLines])

  const fetchDetail = useCallback(async (id: string) => {
    try {
      const res = await axios.get(`${API_BASE}/chat/sessions/${id}`)
      setDetail(res.data)
      setError(null)
    } catch (e: any) {
      setError(`加载会话详情失败: ${e.message || e}`)
    }
  }, [])

  /**加载历史消息到 chatLines */
  const loadMessages = useCallback(async (id: string) => {
    try {
      const res = await axios.get(`${API_BASE}/chat/sessions/${id}/messages`)
      const items: ChatMessageItem[] = res.data.items || []
      const lines: ChatLine[] = []
      for (const m of items) {
        if (m.role === 'user') {
          lines.push({ id: lineIdRef.current++, role: 'user', content: m.content || '', type: 'message' })
        } else if (m.role === 'assistant') {
          if (m.tool_name === 'tool_calls' && m.tool_args) {
            // assistant 发起 tool_calls 的标记消息（不在对话流单独渲染，由后续 tool 消息体现）
            continue
          }
          lines.push({ id: lineIdRef.current++, role: 'assistant', content: m.content || '', type: 'message' })
        } else if (m.role === 'tool') {
          lines.push({
            id: lineIdRef.current++, role: 'tool', content: m.content || '',
            type: 'tool_result',
            toolName: m.tool_name || '',
            toolArgs: m.tool_args,
            toolResult: m.tool_result,
          })
        }
      }
      setChatLines(lines)
    } catch (e: any) {
      // 消息加载失败不阻断主流程
      console.error('加载消息失败:', e)
    }
  }, [])

  const handleSelectSession = async (id: string) => {
    // 关闭旧 SSE
    if (eventSourceRef.current) {
      eventSourceRef.current.close()
      eventSourceRef.current = null
    }
    setStreaming(false)
    setPendingConfirm(null)
    setCurrentId(id)
    await fetchDetail(id)
    await loadMessages(id)
  }

  const handleNewSession = async () => {
    const text = inputText.trim()
    if (!text) {
      setError('请先输入消息内容')
      return
    }
    setError(null)
    try {
      // 创建会话（intent_text 留空，首条消息走 POST /messages）
      const res = await axios.post(`${API_BASE}/chat/sessions`, { intent_text: null })
      const newSession = res.data
      setCurrentId(newSession.id)
      setDetail(newSession)
      setChatLines([])
      await fetchSessions()
      // 发送首条消息
      await sendMessage(newSession.id, text)
    } catch (e: any) {
      setError(`创建会话失败: ${e.message || e}`)
    }
  }

  /**开启全新空白会话：清除当前选中态，等待用户在输入框发起首条消息 */
  const handleStartBlankSession = () => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close()
      eventSourceRef.current = null
    }
    setStreaming(false)
    setPendingConfirm(null)
    setCurrentId(null)
    setDetail(null)
    setChatLines([])
    setError(null)
    setInputText('')
  }

  /**发送消息（对话模式）*/
  const sendMessage = async (sessionId?: string, content?: string) => {
    const sid = sessionId || currentId
    const text = (content ?? inputText).trim()
    if (!sid || !text) return

    setError(null)
    setInputText('')

    // 追加 user 消息到 UI
    appendLine({ role: 'user', content: text, type: 'message' })

    try {
      await axios.post(`${API_BASE}/chat/sessions/${sid}/messages`, { content: text })
      // 启动 SSE 流
      startStream(sid)
    } catch (e: any) {
      const msg = e?.response?.data?.detail || e.message || String(e)
      setError(`发送消息失败: ${msg}`)
    }
  }

  /**阶段 1 快速模式：单轮 interpret */
  const handleQuickInterpret = async () => {
    const text = inputText.trim()
    if (!text) {
      setError('请先输入意图描述')
      return
    }
    setError(null)
    setStreaming(true)
    try {
      // 创建会话 + interpret
      const createRes = await axios.post(`${API_BASE}/chat/sessions`, { intent_text: text })
      const newSession = createRes.data
      setCurrentId(newSession.id)
      setDetail(newSession)
      setChatLines([])
      await fetchSessions()

      const res = await axios.post(`${API_BASE}/chat/sessions/${newSession.id}/interpret`)
      await fetchDetail(newSession.id)
      await fetchSessions()

      // 追加 user + assistant 消息
      appendLine({ role: 'user', content: text, type: 'message' })
      appendLine({
        role: 'assistant',
        content: `已解析为配置草稿（置信度 ${Math.round((res.data.confidence || 0) * 100)}%）`,
        type: 'message',
      })
    } catch (e: any) {
      const msg = e?.response?.data?.detail || e.message || String(e)
      setError(`意图解析失败: ${msg}`)
    } finally {
      setStreaming(false)
    }
  }

  /**启动 SSE 流接收 agent loop 中间态 */
  const startStream = (sessionId: string) => {
    setStreaming(true)
    sseRetryRef.current = 0

    const connect = () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close()
      }

      const es = new EventSource(`${API_BASE}/chat/sessions/${sessionId}/stream`)
      eventSourceRef.current = es

      es.addEventListener('open', () => {
        sseRetryRef.current = 0
        setError(null)
      })

      es.addEventListener('chat_start', () => {
        // 静默：不打断对话流
      })

      es.addEventListener('chat_tool_call', (e) => {
        const data = safeParseSSE(e, 'chat_tool_call')
        if (!data) return
        appendLine({
          role: 'tool',
          content: `调用工具：${data.tool_name}`,
          type: 'tool_call',
          toolName: data.tool_name,
          toolArgs: data.tool_args,
          round: data.round,
        })
      })

      es.addEventListener('chat_tool_result', (e) => {
        const data = safeParseSSE(e, 'chat_tool_result')
        if (!data) return
        appendLine({
          role: 'tool',
          content: `${data.tool_name} 返回`,
          type: 'tool_result',
          toolName: data.tool_name,
          toolResult: data.result,
          round: data.round,
        })
        // launch_eval 自主执行后，结果含 task_uuid → 跳转评估详情页
        if (
          data.tool_name === 'launch_eval' &&
          data.result?.ok &&
          data.result?.task_uuid &&
          onNavigateToDetail
        ) {
          onNavigateToDetail(data.result.task_uuid)
        }
        // export_report 自主执行后，结果含 file_token → 自动触发下载
        if (data.tool_name === 'export_report' && data.result?.ok && data.result?.file_token) {
          const link = document.createElement('a')
          link.href = `${API_BASE}/chat/export-download/${data.result.file_token}`
          link.download = data.result.file_name || '评测报告.xlsx'
          document.body.appendChild(link)
          link.click()
          document.body.removeChild(link)
        }
      })

      es.addEventListener('chat_message_chunk', (e) => {
        const data = safeParseSSE(e, 'chat_message_chunk')
        if (!data) return
        appendToStreamingLine(data.round, data.content || '')
      })

      es.addEventListener('chat_message', (e) => {
        const data = safeParseSSE(e, 'chat_message')
        if (!data) return
        finalizeStreamingLine(data.round, data.content)
      })

      es.addEventListener('chat_confirm_required', (e) => {
        const data = safeParseSSE(e, 'chat_confirm_required')
        if (!data) return
        setPendingConfirm({
          toolName: data.tool_name,
          toolArgs: data.tool_args || {},
          round: data.round,
        })
      })

      es.addEventListener('chat_complete', () => {
        setStreaming(false)
        setPendingConfirm(null)
        streamingLineRef.current = null
        // 拉取最新草稿展示卡片
        fetchDetail(sessionId)
        es.close()
        eventSourceRef.current = null
      })

      es.addEventListener('chat_error', (e) => {
        const data = safeParseSSE(e, 'chat_error')
        setStreaming(false)
        setPendingConfirm(null)
        streamingLineRef.current = null
        setError(data?.error || 'agent loop 出错')
        es.close()
        eventSourceRef.current = null
      })

      es.onerror = () => {
        if (streaming) return // 已被 chat_complete 关闭
        setError('实时连接中断，正在尝试重连…')
        es.close()
        const delay = Math.min(30000, 1000 * 2 ** sseRetryRef.current)
        sseRetryRef.current += 1
        if (sseRetryRef.current <= 5) {
          setTimeout(connect, delay)
        } else {
          setStreaming(false)
          setError('实时连接重连失败，请重新发送消息')
        }
      }
    }

    connect()
  }

  // 清理 SSE
  useEffect(() => {
    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close()
      }
    }
  }, [])

  const handleDraftChange = async (patch: Partial<DraftFields>) => {
    if (!currentId) return
    try {
      const res = await axios.patch(`${API_BASE}/chat/sessions/${currentId}/draft`, { fields: patch })
      setDetail(res.data)
    } catch (e: any) {
      setError(`保存草稿失败: ${e.message || e}`)
    }
  }

  const handleApplyToForm = () => {
    if (!detail?.draft_request) return
    onApplyToEvalPage(detail.draft_request, detail.draft_request.online_mode !== false)
  }

  const handleApplyDirect = async () => {
    if (!currentId) return
    setApplying(true)
    setError(null)
    try {
      const res = await axios.post(`${API_BASE}/chat/sessions/${currentId}/apply`)
      const taskUuid = res.data.task_uuid
      // 直接发起评测：跳转到评估详情页（与 launch_eval 确认路径一致）
      if (taskUuid && onNavigateToDetail) {
        onNavigateToDetail(taskUuid)
      } else {
        // 兜底：未返回 task_uuid 时回到评估表单页
        onApplyToEvalPage({}, detail?.draft_request?.online_mode !== false)
      }
    } catch (e: any) {
      const msg = e?.response?.data?.detail || e.message || String(e)
      setError(`启动评测失败: ${msg}`)
    } finally {
      setApplying(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await axios.delete(`${API_BASE}/chat/sessions/${id}`)
      if (currentId === id) {
        setCurrentId(null)
        setDetail(null)
        setChatLines([])
      }
      await fetchSessions()
    } catch (e: any) {
      setError(`删除会话失败: ${e.message || e}`)
    }
  }

  const draft = detail?.draft_request || null
  const missingFields = detail?.missing_fields || []

  return (
    <div className="chat-page">
      {/* 左栏：会话列表 */}
      <aside className="chat-sidebar">
        <div className="chat-sidebar-header">
          <h3>配置会话</h3>
          <span className="chat-count">{sessionsTotal}</span>
        </div>
        <button
          className="chat-new-session-btn"
          onClick={handleStartBlankSession}
          title="开启新会话"
        >
          + 新会话
        </button>
        <div className="chat-session-list">
          {loadingSessions && <div className="chat-empty">加载中…</div>}
          {!loadingSessions && sessions.length === 0 && (
            <div className="chat-empty">暂无会话，在右侧输入消息开始</div>
          )}
          {sessions.map(s => (
            <div
              key={s.id}
              className={`chat-session-item${currentId === s.id ? ' active' : ''}`}
              onClick={() => handleSelectSession(s.id)}
            >
              <div className="chat-session-title">{s.title || '未命名'}</div>
              <div className="chat-session-meta">
                <span className={`chat-status chat-status-${s.status}`}>{s.status}</span>
                {s.intent && <span className="chat-intent-tag">{s.intent}</span>}
              </div>
              <button
                className="chat-session-del"
                onClick={e => { e.stopPropagation(); handleDelete(s.id) }}
                title="删除"
              >×</button>
            </div>
          ))}
        </div>
      </aside>

      {/* 中栏：对话区 */}
      <main className="chat-main">
        <div className="chat-main-header">
          <h2>AI 助手</h2>
          <p className="chat-subtitle">对话式配置：AI 自动查询资源、推荐指标、组装草稿；支持 Agent / LLM 模型 / 评估脚本的管理</p>
        </div>

        {/* 对话流 / 草稿 / 占位（上方，可滚动） */}
        <div className="chat-stream" ref={chatScrollRef}>
          {error && <div className="chat-error">{error}</div>}

          {/* 对话流 */}
          {(chatLines.length > 0 || pendingConfirm) && (
            <div className="chat-messages">
              {chatLines.map(line => {
                if (line.type === 'tool_call' || line.type === 'tool_result') {
                  return <ToolCallBubble key={line.id} line={line} />
                }
                const bubbleClass = line.role === 'user' ? 'chat-bubble-user' : 'chat-bubble-assistant'
                return (
                  <div key={line.id} className={`chat-bubble ${bubbleClass}`}>
                    <div className="chat-bubble-content">{line.content}</div>
                  </div>
                )
              })}
              {pendingConfirm && currentId && (
                <ConfirmCard
                  pending={pendingConfirm}
                  sessionId={currentId}
                  onConfirmed={(result) => {
                    setPendingConfirm(null)
                    // launch_eval 确认成功后跳转到评估详情页
                    if (
                      pendingConfirm.toolName === 'launch_eval' &&
                      result?.ok &&
                      result?.task_uuid &&
                      onNavigateToDetail
                    ) {
                      onNavigateToDetail(result.task_uuid)
                    }
                  }}
                  onCancelled={() => setPendingConfirm(null)}
                  onError={(msg) => setError(msg)}
                />
              )}
              {streaming && (
                <div className="chat-typing">
                  <div className="chat-spinner" />
                  <span>AI 正在思考…</span>
                </div>
              )}
            </div>
          )}

          {/* 草稿卡片 */}
          {draft && (
            <EvalDraftPreviewCard
              draft={draft}
              missingFields={missingFields}
              suggestions={[]}
              confidence={detail?.confidence}
              onChange={handleDraftChange}
              onApplyToForm={handleApplyToForm}
              onApplyDirect={handleApplyDirect}
              applying={applying}
            />
          )}

          {!draft && chatLines.length === 0 && !streaming && !error && (
            <div className="chat-placeholder">
              <div className="chat-placeholder-title">AI 助手能帮你做</div>
              <ul className="chat-cap-list">
                <li>配置 LLM 评估模型（增 / 改 / 删）</li>
                <li>注册和管理 Agent（增 / 改 / 删）</li>
                <li>推荐评测指标组合</li>
                <li>组装评估任务并直接启动</li>
                <li>创建 / 删除评估脚本</li>
              </ul>
              <div className="chat-placeholder-title chat-placeholder-bound">以下操作请前往对应页面</div>
              <ul className="chat-cap-list chat-cap-list-muted">
                <li>导入评测集 → 评测管理</li>
                <li>配置 Langfuse → 观测打点</li>
                <li>查看评估结果 → 评估历史</li>
              </ul>
              <div className="chat-placeholder-hint">在下方输入框描述需求，或点击示例快速开始。</div>
            </div>
          )}
        </div>

        {/* 输入区（下方，固定） */}
        <div className="chat-input-area">
          <div className="chat-input-row">
            <textarea
              className="chat-input-textarea"
              value={inputText}
              onChange={e => setInputText(e.target.value)}
              placeholder="例如：用 GLM 评估 agent-001 在数据集 ds_faq 上的忠实度，跑3轮"
              rows={2}
              disabled={streaming}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  if (currentId) {
                    sendMessage()
                  } else {
                    handleNewSession()
                  }
                }
              }}
            />
            <div className="chat-input-buttons">
              <button
                className="chat-btn chat-btn-secondary"
                onClick={handleQuickInterpret}
                disabled={streaming || !inputText.trim()}
                title="阶段1快速模式：单轮 LLM 解析意图"
              >
                快速解析
              </button>
              <button
                className="chat-btn chat-btn-primary"
                onClick={() => currentId ? sendMessage() : handleNewSession()}
                disabled={streaming || !inputText.trim()}
              >
                {streaming ? '对话中…' : (currentId ? '发送' : '开始对话')}
              </button>
            </div>
          </div>
          <div className="chat-samples">
            {SAMPLE_INTENTS.map((s, i) => (
              <button key={i} className="chat-sample" onClick={() => setInputText(s)} disabled={streaming}>
                {s}
              </button>
            ))}
          </div>
        </div>
      </main>
    </div>
  )
}
