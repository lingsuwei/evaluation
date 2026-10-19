import { useState, useEffect, useCallback, useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** 从 React children 中提取纯文本 */
function getText(children: React.ReactNode): string {
  if (typeof children === 'string') return children
  if (typeof children === 'number') return String(children)
  if (Array.isArray(children)) return children.map(getText).join('')
  if (children && typeof children === 'object' && 'props' in children) {
    return getText((children as React.ReactElement).props.children)
  }
  return ''
}

/** GFM 风格的 slug 生成（须与标题 id 生成保持一致） */
function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\w\u4e00-\u9fff\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
}

interface TocNode {
  id: string
  text: string
  level: number
  children: TocNode[]
}

/** 从 markdown 文本解析标题，构建带层级的目录树 */
function buildTocTree(md: string): TocNode[] {
  const roots: TocNode[] = []
  const stack: TocNode[] = []
  let inCodeBlock = false
  for (const line of md.split('\n')) {
    if (/^\s*```/.test(line)) {
      inCodeBlock = !inCodeBlock
      continue
    }
    if (inCodeBlock) continue
    const m = line.match(/^(#{1,4})\s+(.+?)\s*$/)
    if (!m) continue
    const level = m[1].length
    const text = m[2].trim()
    // 跳过文档主标题（h1）与「目录」章节，避免左侧树出现无意义节点
    if (level === 1) continue
    if (text === '目录') continue
    const node: TocNode = { id: slugify(text), text, level, children: [] }
    while (stack.length && stack[stack.length - 1].level >= level) stack.pop()
    if (stack.length === 0) roots.push(node)
    else stack[stack.length - 1].children.push(node)
    stack.push(node)
  }
  return roots
}

/** 将树拍平为按文档顺序的列表，用于滚动高亮判定 */
function flattenToc(nodes: TocNode[]): TocNode[] {
  const out: TocNode[] = []
  const walk = (ns: TocNode[]) => {
    for (const n of ns) {
      out.push(n)
      if (n.children.length) walk(n.children)
    }
  }
  walk(nodes)
  return out
}

function TocTree({
  nodes,
  activeId,
  onNavigate,
}: {
  nodes: TocNode[]
  activeId: string
  onNavigate: (id: string) => void
}) {
  return (
    <ul className="help-toc-list">
      {nodes.map((node) => (
        <li key={node.id} className={`help-toc-item toc-level-${node.level}`}>
          <a
            href={`#${node.id}`}
            className={activeId === node.id ? 'active' : ''}
            onClick={(e) => {
              e.preventDefault()
              onNavigate(node.id)
            }}
            title={node.text}
          >
            {node.text}
          </a>
          {node.children.length > 0 && (
            <TocTree nodes={node.children} activeId={activeId} onNavigate={onNavigate} />
          )}
        </li>
      ))}
    </ul>
  )
}

export function HelpPage() {
  const [content, setContent] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeId, setActiveId] = useState('')

  useEffect(() => {
    fetch('/api/guide')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.text()
      })
      .then((text) => {
        setContent(text)
        setLoading(false)
      })
      .catch((err) => {
        setError(err.message)
        setLoading(false)
      })
  }, [])

  const tocTree = useMemo(() => (content ? buildTocTree(content) : []), [content])
  const flatToc = useMemo(() => flattenToc(tocTree), [tocTree])

  const navigateTo = useCallback((id: string) => {
    setActiveId(id)
    const el = document.getElementById(id)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [])

  // 滚动时高亮当前所在章节
  useEffect(() => {
    if (!flatToc.length) return
    const container = document.querySelector('.main-content') as HTMLElement | null
    if (!container) return
    let raf = 0
    const update = () => {
      raf = 0
      const containerTop = container.getBoundingClientRect().top
      let current = ''
      for (const h of flatToc) {
        const el = document.getElementById(h.id)
        if (!el) continue
        const top = el.getBoundingClientRect().top - containerTop
        if (top <= 80) current = h.id
        else break
      }
      setActiveId(current)
    }
    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(update)
    }
    container.addEventListener('scroll', onScroll, { passive: true })
    update()
    return () => {
      container.removeEventListener('scroll', onScroll)
      if (raf) cancelAnimationFrame(raf)
    }
  }, [flatToc])

  // 正文内的锚点链接（如文档内引用「第 6 章」）也需要 decode 后跳转
  const handleAnchorClick = useCallback(
    (e: React.MouseEvent<HTMLAnchorElement>, href: string) => {
      if (href.startsWith('#')) {
        e.preventDefault()
        let id = href.slice(1)
        try {
          id = decodeURIComponent(id)
        } catch {
          // decode 失败时保留原值
        }
        navigateTo(id)
      }
    },
    [navigateTo],
  )

  if (loading) {
    return <div className="card" style={{ padding: 40, textAlign: 'center' }}>加载使用指南...</div>
  }

  if (error) {
    return (
      <div className="card" style={{ padding: 40, textAlign: 'center', color: '#e74c3c' }}>
        使用指南加载失败: {error}
      </div>
    )
  }

  return (
    <div className="help-layout">
      <aside className="help-toc">
        <div className="help-toc-title">目录</div>
        <TocTree nodes={tocTree} activeId={activeId} onNavigate={navigateTo} />
      </aside>
      <div className="help-content help-page">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            h1: ({ children }) => <h1 id={slugify(getText(children))}>{children}</h1>,
            h2: ({ children }) => <h2 id={slugify(getText(children))}>{children}</h2>,
            h3: ({ children }) => <h3 id={slugify(getText(children))}>{children}</h3>,
            h4: ({ children }) => <h4 id={slugify(getText(children))}>{children}</h4>,
            img: ({ src, alt }) => {
              let imgSrc = src || ''
              if (imgSrc.startsWith('./images/')) {
                imgSrc = imgSrc.replace(/^\.\/images\//, '/api/guide-images/')
              }
              return <img src={imgSrc} alt={alt || ''} />
            },
            a: ({ href, children }) => (
              <a href={href} onClick={(e) => handleAnchorClick(e, href || '')}>
                {children}
              </a>
            ),
          }}
        >
          {content}
        </ReactMarkdown>
      </div>
    </div>
  )
}
