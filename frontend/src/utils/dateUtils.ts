/**
 * 日期时间格式化工具函数
 * 统一处理 UTC 时间到本地时间的转换
 */

/**
 * 将 ISO 时间字符串格式化为本地时间字符串
 * @param dateStr - ISO 8601 格式的时间字符串
 * @returns 格式化后的本地时间字符串 (YYYY-MM-DD HH:mm:ss)
 */
export function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '-'
  
  try {
    const date = new Date(dateStr)
    if (isNaN(date.getTime())) return '-'
    
    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    const hours = String(date.getHours()).padStart(2, '0')
    const minutes = String(date.getMinutes()).padStart(2, '0')
    const seconds = String(date.getSeconds()).padStart(2, '0')
    
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`
  } catch {
    return '-'
  }
}

/**
 * 将 ISO 时间字符串格式化为本地时间字符串（不含秒）
 * @param dateStr - ISO 8601 格式的时间字符串
 * @returns 格式化后的本地时间字符串 (YYYY-MM-DD HH:mm)
 */
export function formatDateShort(dateStr: string | null | undefined): string {
  if (!dateStr) return '-'
  
  try {
    const date = new Date(dateStr)
    if (isNaN(date.getTime())) return '-'
    
    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    const hours = String(date.getHours()).padStart(2, '0')
    const minutes = String(date.getMinutes()).padStart(2, '0')
    
    return `${year}-${month}-${day} ${hours}:${minutes}`
  } catch {
    return '-'
  }
}

/**
 * 格式化为相对时间（如：3分钟前、1小时前）
 * @param dateStr - ISO 8601 格式的时间字符串
 * @returns 相对时间字符串
 */
export function formatRelativeTime(dateStr: string | null | undefined): string {
  if (!dateStr) return ''
  
  const timestamp = Date.parse(dateStr)
  if (Number.isNaN(timestamp)) return ''
  
  const diffSeconds = Math.round((timestamp - Date.now()) / 1000)
  const formatter = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto' })
  
  const units = [
    { limit: 60, value: 1, unit: 'second' as const },
    { limit: 3600, value: 60, unit: 'minute' as const },
    { limit: 86400, value: 3600, unit: 'hour' as const },
    { limit: 604800, value: 86400, unit: 'day' as const },
  ]
  
  for (const item of units) {
    if (Math.abs(diffSeconds) < item.limit) {
      return formatter.format(Math.round(diffSeconds / item.value), item.unit)
    }
  }
  
  return formatter.format(Math.round(diffSeconds / 604800), 'week')
}

/**
 * 格式化持续时间（毫秒转换为可读格式）
 * @param ms - 毫秒数
 * @returns 格式化后的持续时间字符串
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || ms <= 0) return '-'
  const totalMs = Math.round(ms)
  if (totalMs < 1000) return `${totalMs}ms`

  const totalSeconds = totalMs / 1000

  if (totalSeconds >= 3600) {
    const hours = Math.floor(totalSeconds / 3600)
    const minutes = Math.floor((totalSeconds % 3600) / 60)
    const seconds = Math.floor(totalSeconds % 60)
    return `${hours}h${minutes}m${seconds}s`
  }

  if (totalSeconds >= 60) {
    const minutes = Math.floor(totalSeconds / 60)
    const seconds = Math.floor(totalSeconds % 60)
    return `${minutes}m${seconds}s`
  }

  // 1~59 秒：整数显示 Ns，非整数保留一位小数 N.Xs
  return totalSeconds % 1 === 0 ? `${totalSeconds}s` : `${totalSeconds.toFixed(1)}s`
}
