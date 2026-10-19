// frontend/src/components/LangfuseTraceBlock.tsx
import { useState } from 'react'
import { useLangfuseHost } from '../utils/useLangfuseHost'

export interface LangfuseTraceBlockProps {
  traceUrl?: string | null
  traceId?: string | null
  observationUrl?: string | null
  langfuseProjectId?: string | null
  langfuseHost?: string | null
  label?: string
  className?: string
}

export function LangfuseTraceBlock({
  traceUrl,
  traceId,
  observationUrl,
  langfuseProjectId,
  langfuseHost,
  label = '在 Langfuse 中查看完整 Trace ↗',
  className = 'edp-sect',
}: LangfuseTraceBlockProps) {
  const hookHost = useLangfuseHost()

  // fallback 构造，对齐后端 langfuse_link_service.py:82-85
  // 优先用调用方传入的 langfuseHost（run 级别精确 host），
  // 退到 useLangfuseHost hook（/api/trace/health 返回的默认 project host）
  const effectiveHost = langfuseHost || hookHost
  const fallbackUrl = traceId && effectiveHost
    ? langfuseProjectId
      ? `${effectiveHost}/project/${langfuseProjectId}/traces/${traceId}`
      : `${effectiveHost}/trace/${traceId}`
    : null
  const finalUrl = traceUrl || fallbackUrl

  if (!finalUrl && !traceId) return null

  return (
    <div className={className}>
      <h4>🔗 Langfuse Trace</h4>
      {finalUrl ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', flexWrap: 'wrap' }}>
          <a
            href={observationUrl || finalUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="edp-btn"
            style={{ color: 'var(--edp-primary)' }}
          >
            {label}
          </a>
          <CopyButton text={observationUrl || finalUrl} label="复制链接" />
          {traceId && (
            <span style={{ color: 'var(--edp-text3)', fontSize: 'var(--edp-font-base)' }}>
              Trace ID: <code>{traceId}</code>
            </span>
          )}
          {traceId && <CopyButton text={traceId} label="复制 ID" />}
        </div>
      ) : (
        <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)' }}>
          Trace ID: <code>{traceId}</code>（Langfuse host 未配置，可复制此 ID 到 Langfuse 搜索）
          <CopyButton text={traceId || ''} label="复制 ID" />
        </div>
      )}
    </div>
  )
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      className="edp-btn"
      onClick={() => {
        copyText(text)
          .then(() => {
            setCopied(true)
            setTimeout(() => setCopied(false), 1500)
          })
          .catch(() => {
            window.prompt('复制失败，请手动选择并复制：', text)
          })
      }}
    >
      {copied ? '已复制' : label}
    </button>
  )
}

/**
 * 复制文本到剪贴板。
 * 优先用 navigator.clipboard（HTTPS / localhost）；
 * 非安全上下文（HTTP）下 clipboard API 不可用，降级到 textarea + execCommand。
 */
function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text)
  }
  return new Promise<void>((resolve, reject) => {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy') ? resolve() : reject(new Error('execCommand copy returned false'))
    } catch (e) {
      reject(e instanceof Error ? e : new Error(String(e)))
    } finally {
      document.body.removeChild(ta)
    }
  })
}