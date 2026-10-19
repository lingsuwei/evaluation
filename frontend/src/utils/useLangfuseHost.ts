// frontend/src/utils/useLangfuseHost.ts
import { useEffect, useState } from 'react'

const API_BASE = '/api'

/**
 * 封装 /api/trace/health 拉取，返回 Langfuse host（已去尾斜杠）。
 * 未加载完或 fetch 失败返回 ''。
 * 调用方传入 traceId 即可构造 trace URL。
 */
export function useLangfuseHost(): string {
  const [host, setHost] = useState('')
  useEffect(() => {
    let alive = true
    fetch(`${API_BASE}/trace/health`)
      .then(r => (r.ok ? r.json() : null))
      .then(h => {
        if (alive && h?.host) setHost(String(h.host).replace(/\/$/, ''))
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])
  return host
}