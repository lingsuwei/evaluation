import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  /** 自定义渲染 fallback；不提供则使用默认错误页 */
  fallback?: (error: Error, retry: () => void) => ReactNode
}
interface State {
  error: Error | null
}

/** 轻量错误边界：捕获子树渲染异常，避免 React 18 整树卸载导致白屏 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: { componentStack: string }) {
    console.error('[ErrorBoundary]', error, info.componentStack)
  }

  retry = () => this.setState({ error: null })

  render() {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback(this.state.error, this.retry)
      return (
        <div style={{ padding: 40, textAlign: 'center', color: '#721c24' }}>
          <h3 style={{ marginBottom: 8 }}>页面渲染异常</h3>
          <pre style={{ fontSize: 12, color: '#888', whiteSpace: 'pre-wrap', textAlign: 'left', maxWidth: 600, margin: '0 auto' }}>
            {this.state.error.message}
          </pre>
          <button
            onClick={this.retry}
            style={{ marginTop: 16, padding: '6px 20px', borderRadius: 4, border: '1px solid #ddd', background: '#fff', cursor: 'pointer' }}
          >
            重试
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
